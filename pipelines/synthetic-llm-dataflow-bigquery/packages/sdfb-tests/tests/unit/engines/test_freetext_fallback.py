"""Loud fallback taxonomy — per-call generation failures still fall back to
exemplars, but must never do so silently.

Production defect (E2E report §4.2): when the LLM fails (e.g. vLLM can't
load Gemma on a T4), both B.2's ``FreeTextHook`` and B.1's
``_infer_free_text_pool`` silently fell back to copying observed reference
exemplars — 100% memorization that nobody noticed. Both except-paths must
now emit a WARNING milestone ``freetext_llm_fallback`` (Task 1's
``log_milestone``) before returning the exemplar fallback.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import logging
import re

import numpy as np
import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import (
    FreeTextEmptyYieldError,
    GenerationConfig,
    GenerationContext,
)
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.b2_library.fidelity import profile_table
from sdfb_core.engines.b2_library.freetext import FreeTextHook


class _BoomClient:
  """A `ModelClient` whose `generate_json` always raises."""

  def generate_json(self, *a, **k):
    raise RuntimeError("boom")


class _EmptyYieldClient:
  """A `ModelClient` whose `generate_json` succeeds but yields nothing.

    Models the 2026-07-15 E2E failure: vLLM answered HTTP 200 but every
    choice was dropped at JSON parse, so the call returns `[]` without
    raising — the second silent-memorization path.
    """

  def __init__(self):
    self.calls: list[dict] = []

  def generate_json(self, *a, **k):
    self.calls.append(k)
    return []


# ---------------------------------------------------------------------------
# Fixtures — mirrors of the shapes in test_b2_library.py / test_b1_rag.py,
# kept local since those modules define theirs as file-local fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def wide_ctx() -> GenerationContext:
  ddl = {
      "table_info": {
          "table_id": "demo.support_tickets"
      },
      "schema": [
          {
              "name": "ticket_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "region",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 8
          },
          {
              "name": "summary",
              "type": "STRING",
              "mode": "REQUIRED"
          },
      ],
      "primary_keys": ["ticket_id"],
  }
  reference = [
      {
          "ticket_id":
              1001,
          "region":
              "EMEA",
          "summary":
              "Customer reports the export job hangs at 90 percent for large tables.",
      },
      {
          "ticket_id":
              1002,
          "region":
              "AMER",
          "summary":
              "User cannot reset password; the reset email never arrives.",
      },
      {
          "ticket_id":
              1003,
          "region":
              "APAC",
          "summary":
              "Dashboard widgets render blank after the latest browser update.",
      },
  ]
  return GenerationContext(
      table_schema=TableSchema.model_validate(ddl),
      reference_rows=reference,
      reference_digest="wide-digest",
      pipeline_run_id="b2-wide-run",
  )


@pytest.fixture
def free_text_ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.profiles"
      },
      "schema": [
          {
              "name": "user_id",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "region",
              "type": "STRING",
              "mode": "REQUIRED",
              "max_length": 8
          },
          {
              "name": "bio",
              "type": "STRING",
              "mode": "NULLABLE",
              "max_length": 500
          },
      ],
      "primary_keys": ["user_id"],
  })
  rows = [
      {
          "user_id":
              i,
          "region":
              "EU",
          "bio":
              f"User number {i} enjoys long-form descriptive prose and writes a lot.",
      }
      # 32 distinct bios, not 12: with pool-target scaling (WS2 §4b.2, Task
      # 7) the target is min(num_rows, distinct(bio), 512); num_rows is
      # unset (0 = unknown) here, so distinct(bio) is the binding bound.
      # 32 rows keeps that bound at exactly 32, preserving the pre-Task-7
      # escalation-ladder tests below that assert today's target=32
      # behavior verbatim.
      for i in range(1, 33)
  ]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="ft-digest",
      pipeline_run_id="b1-ft",
  )


# ---------------------------------------------------------------------------
# B.2 — FreeTextHook._generate_pool
# ---------------------------------------------------------------------------


def test_b2_fallback_emits_warning_milestone(caplog, wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(_BoomClient())
  rng = np.random.default_rng(7)
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pool = hook.sample(profiles["summary"], 5, GenerationConfig(seed=7), rng)
  assert pool  # exemplar fallback still returns values
  assert any(v is not None for v in pool)
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_llm_fallback" in text
  assert "error=RuntimeError" in text


# ---------------------------------------------------------------------------
# B.1 — B1RagEngine._infer_free_text_pool (invoked via setup()).
# ---------------------------------------------------------------------------


def test_b1_fallback_emits_warning_milestone(caplog, free_text_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_BoomClient(), free_text_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_llm_fallback" in text
  assert "error=RuntimeError" in text
  # The pool still falls back to observed exemplars — not empty.
  assert engine._free_text_pools["bio"]


# ---------------------------------------------------------------------------
# strict_freetext=True — real-vLLM runs must fail loudly, never fall back.
# ---------------------------------------------------------------------------


def test_b2_strict_reraises(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(_BoomClient(), strict=True)
  rng = np.random.default_rng(7)
  with pytest.raises(RuntimeError, match="boom"):
    hook.sample(profiles["summary"], 5, GenerationConfig(seed=7), rng)


def test_b1_strict_reraises(free_text_ctx):
  strict_ctx = free_text_ctx.model_copy(update={"strict_freetext": True})
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with pytest.raises(RuntimeError, match="boom"):
    engine.setup(_BoomClient(), strict_ctx)


# ---------------------------------------------------------------------------
# Empty yield — generate_json returns [] without raising (all choices were
# dropped at parse). Must be as loud as an exception: milestone in lax mode,
# FreeTextEmptyYieldError under strict_freetext.
# ---------------------------------------------------------------------------


def test_b1_empty_yield_emits_fallback_milestone(caplog, free_text_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_EmptyYieldClient(), free_text_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_llm_fallback" in text
  assert "error=EmptyYield" in text
  # Exemplar fallback still fills the pool — the run degrades, not crashes.
  assert engine._free_text_pools["bio"]


def test_b1_strict_raises_on_empty_yield(free_text_ctx):
  strict_ctx = free_text_ctx.model_copy(update={"strict_freetext": True})
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with pytest.raises(FreeTextEmptyYieldError, match="bio"):
    engine.setup(_EmptyYieldClient(), strict_ctx)


def test_b2_empty_yield_emits_fallback_milestone(caplog, wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(_EmptyYieldClient())
  rng = np.random.default_rng(7)
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pool = hook.sample(profiles["summary"], 5, GenerationConfig(seed=7), rng)
  assert pool
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_llm_fallback" in text
  assert "error=EmptyYield" in text


def test_b2_strict_raises_on_empty_yield(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(_EmptyYieldClient(), strict=True)
  rng = np.random.default_rng(7)
  with pytest.raises(FreeTextEmptyYieldError, match="summary"):
    hook.sample(profiles["summary"], 5, GenerationConfig(seed=7), rng)


def test_b2_strict_empty_yield_emits_milestone_before_raise(caplog, wide_ctx):
  # 2026-07-22 b2 E2E: all 63 batches died on a strict empty yield, and the
  # worker logs never named the column — the raise went straight into the
  # DLQ envelope. The strict path must be as visible in logs as the lax one.
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  hook = FreeTextHook(_EmptyYieldClient(), strict=True)
  with (
      caplog.at_level(logging.ERROR, logger="sdfb.milestone"),
      pytest.raises(FreeTextEmptyYieldError),
  ):
    hook._pool_for(profiles["summary"], GenerationConfig(seed=7))
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_pool_empty" in text
  assert "column=summary" in text


def test_b2_strict_empty_yield_is_negative_cached(caplog, wide_ctx):
  # A deterministic empty yield (saturated domain / exemplar echo) fails
  # identically on every rebuild. 2026-07-22 b2 E2E: 63 batches each
  # re-paid 3 escalating LLM calls (~35 min GPU) against a run the gate
  # was already guaranteed to fail. The failure must be cached so later
  # batches on the worker re-raise immediately.
  client = _EmptyYieldClient()
  hook = FreeTextHook(client, strict=True)
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  cfg = GenerationConfig(seed=7)
  with pytest.raises(FreeTextEmptyYieldError, match="summary"):
    hook._pool_for(profiles["summary"], cfg)
  calls_after_first = len(client.calls)
  assert calls_after_first > 0
  with (
      caplog.at_level(logging.DEBUG, logger="sdfb.milestone"),
      pytest.raises(FreeTextEmptyYieldError, match="summary"),
  ):
    hook._pool_for(profiles["summary"], cfg)
  assert len(client.calls) == calls_after_first  # no fresh LLM spend
  # The cached fail-fast is envelope-only in the DLQ; a DEBUG milestone
  # keeps it findable in worker logs (2026-07-22 re-run: 55 silent deaths).
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_pool_empty_cached" in text
  assert "column=summary" in text


# ---------------------------------------------------------------------------
# Reference-blend privacy bound — the 2026-07-23 b2 E2E landed COL_048/053/054
# (source_distinct 19 815 / 1 298 / 3 030) at copy_ratio ≈ 0.51: the
# similarity=0.5 blend mass drawn verbatim from the reference pool. Columns
# above the memorization rule's cardinality bound (source_distinct > 100)
# must never blend observed values, whatever `similarity` says.
# ---------------------------------------------------------------------------


def _highcard_profiles():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.requests"
      },
      "schema": [{
          "name": "req_id",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })
  # 150 distinct short ids: FREE_TEXT (unique-ratio 1.0), no strict
  # identifier shape (below min length), > 100 observed distinct.
  rows = [{"req_id": f"RQ-{i}"} for i in range(100, 250)]
  return profile_table(schema, rows)


def test_b2_high_cardinality_free_text_never_blends_reference():
  profiles = _highcard_profiles()
  p = profiles["req_id"]
  client = _NovelBatchClient(per_call=32)
  hook = FreeTextHook(client, strict=True)
  rng = np.random.default_rng(5)
  # similarity=1.0 puts ALL blend mass on the reference pool — the
  # strongest possible leak — yet every landed value must be novel.
  values = hook.sample(p, 200, GenerationConfig(seed=5, similarity=1.0), rng)
  observed = set(p.text_pool)
  non_null = [v for v in values if v is not None]
  assert non_null
  assert all(v not in observed for v in non_null)


def test_b2_low_cardinality_free_text_keeps_reference_blend(wide_ctx):
  # Below the bound the blend is the intended mimic primitive: at
  # similarity=1.0 a small prose pool reproduces observed values.
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  p = profiles["summary"]
  hook = FreeTextHook(_NovelBatchClient(per_call=32))
  rng = np.random.default_rng(5)
  values = hook.sample(p, 200, GenerationConfig(seed=5, similarity=1.0), rng)
  observed = set(p.text_pool)
  non_null = [v for v in values if v is not None]
  assert non_null
  assert all(v in observed for v in non_null)


# ---------------------------------------------------------------------------
# Shape-template fallback — copy-saturated pools (2026-07-22 b2 E2E:
# COL_052, 96/96 prompt echoes on every escalation attempt, run FAILED
# with blocker_ratio=1.0). When the LLM parses values but every one is an
# observed copy, a relaxed per-position template generates novel in-format
# values instead of killing the batch. Parse failures (parsed=0) still raise.
# ---------------------------------------------------------------------------


class _EchoShownClient:
  """Echoes exactly the exemplars it was built with — the COL_052
    signature (prompt_echoes == parsed, novel = 0, identically every call)."""

  def __init__(self, shown):
    self._shown = list(shown)
    self.calls: list[dict] = []

  def generate_json(self, *a, **k):
    self.calls.append(k)
    return [{"values": list(self._shown)}]


def _userid_profiles():
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.audit"
      },
      "schema": [{
          "name": "col_052",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
      "primary_keys": None,
  })
  # 60 distinct 7-char ids: unique-ratio 1.0 → FREE_TEXT, but below the
  # strict identifier-shape minimum length → the LLM pool route.
  rows = [{"col_052": f"USR_{i}"} for i in range(100, 160)]
  return profile_table(schema, rows)


def test_b2_echo_saturated_pool_falls_back_to_shape_template(caplog):
  profiles = _userid_profiles()
  p = profiles["col_052"]
  assert p.identifier_shape is None  # would never reach the LLM otherwise
  client = _EchoShownClient(p.text_pool[:8])
  hook = FreeTextHook(client, pool_size=8, strict=True)
  cfg = GenerationConfig(seed=3, engine_specific={"pool_seed": 41})
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pool = hook._pool_for(p, cfg)
  assert len(pool) == 8
  observed = set(p.text_pool)
  assert all(v not in observed for v in pool)  # novel by construction
  assert all(re.fullmatch(r"USR_\d{3}", v) for v in pool)  # format-preserving
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_pool_shape_fallback" in text
  assert "column=col_052" in text
  # A genuine novel pool → cached: the next batch pays no LLM calls.
  n_calls = len(client.calls)
  assert hook._pool_for(p, cfg) == pool
  assert len(client.calls) == n_calls


def test_b2_shape_fallback_applies_in_lax_mode_over_exemplars(caplog):
  # Non-strict used to degrade to exemplar memorization; novel-by-template
  # is strictly better and must win when a template exists.
  profiles = _userid_profiles()
  p = profiles["col_052"]
  hook = FreeTextHook(_EchoShownClient(p.text_pool[:8]), pool_size=8)
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pool = hook._pool_for(p, GenerationConfig(seed=3))
  assert all(v not in set(p.text_pool) for v in pool)
  text = "\n".join(r.message for r in caplog.records)
  assert "freetext_pool_shape_fallback" in text
  assert "freetext_llm_fallback" not in text


def test_b2_prose_echo_saturation_still_raises_strict(wide_ctx):
  # Prose has no relaxed template (whitespace guard) — the strict raise
  # path is unchanged when the shape fallback cannot apply.
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  p = profiles["summary"]
  hook = FreeTextHook(
      _EchoShownClient(p.text_pool[:8]), pool_size=8, strict=True)
  with pytest.raises(FreeTextEmptyYieldError, match="summary"):
    hook._pool_for(p, GenerationConfig(seed=1))


def test_b2_escalation_attempts_vary_seed(wide_ctx):
  # All three escalation attempts used to share one pinned seed, so a
  # seeded echo repeated identically and the ladder's diversity was
  # partly illusory. Attempts must walk the seed (base, base+1, base+2)
  # while staying P6-reproducible from the same base.
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  client = _EmptyYieldClient()
  hook = FreeTextHook(client, strict=True)
  cfg = GenerationConfig(seed=3, engine_specific={"pool_seed": 100})
  with pytest.raises(FreeTextEmptyYieldError):
    hook._pool_for(profiles["summary"], cfg)
  assert [c["seed"] for c in client.calls] == [100, 101, 102]


def test_b2_strict_transient_error_is_not_negative_cached(wide_ctx):
  # Transport/client exceptions are transient-shaped: the next batch must
  # retry the build rather than inherit a poisoned cache entry.
  class _CountingBoomClient:

    def __init__(self):
      self.calls = 0

    def generate_json(self, *a, **k):
      self.calls += 1
      raise RuntimeError("boom")

  client = _CountingBoomClient()
  hook = FreeTextHook(client, strict=True)
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  with pytest.raises(RuntimeError, match="boom"):
    hook._pool_for(profiles["summary"], GenerationConfig(seed=7))
  first = client.calls
  with pytest.raises(RuntimeError, match="boom"):
    hook._pool_for(profiles["summary"], GenerationConfig(seed=7))
  assert client.calls == 2 * first  # retried, not cached


# ---------------------------------------------------------------------------
# All-copies yield — the LLM "succeeds" but every value is a verbatim
# exemplar copy. After the novelty filter that is an empty yield: same
# milestone, same strict behavior.
# ---------------------------------------------------------------------------


class _CopyingClient:
  """A `ModelClient` that only echoes observed reference values."""

  def __init__(self, copies: list[dict]):
    self._copies = copies

  def generate_json(self, *a, **k):
    return list(self._copies)


def test_b1_all_copy_yield_counts_as_empty(caplog, free_text_ctx):
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_CopyingClient(copies), free_text_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_llm_fallback" in text
  assert "error=EmptyYield" in text


# ---------------------------------------------------------------------------
# Escalating-temperature retries — an empty NOVEL yield (all verbatim copies
# or all parse-drops) retries the pool call at higher temperatures before
# giving up. 2026-07-16 corp run: Qwen3-4B echoed the seed exemplars verbatim
# for PK_COL on every choice → strict kill after a 90-min setup.
# ---------------------------------------------------------------------------


class _CopyThenNovelClient:
  """Echoes observed values on the first call, novel values afterwards."""

  def __init__(self, copies: list[dict], novel: list[dict]):
    self._copies = copies
    self._novel = novel
    self.calls: list[dict] = []

  def generate_json(self, *a, **k):
    self.calls.append(k)
    if len(self.calls) == 1:
      return list(self._copies)
    return list(self._novel)


def test_b1_retries_with_escalating_temperature_on_all_copies(free_text_ctx):
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  client = _CopyThenNovelClient(copies, [{"bio": "A brand-new synthetic bio."}])
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  assert len(client.calls) >= 2, "expected a retry after the all-copies yield"
  temps = [c.get("temperature") for c in client.calls]
  assert all(t is not None for t in temps)
  assert temps[1] > temps[0], f"retry must escalate temperature, got {temps}"
  assert "A brand-new synthetic bio." in engine._free_text_pools["bio"]


def test_b1_stops_once_pool_reaches_target(free_text_ctx):

  class _FullYieldClient(_CopyThenNovelClient):

    def generate_json(self, *a, **k):
      self.calls.append(k)
      return [{"values": [f"Novel bio {j}" for j in range(32)]}]

  client = _FullYieldClient([], [])
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  assert len(client.calls) == 1


def test_b1_strict_all_copy_message_reports_counts(free_text_ctx):
  strict_ctx = free_text_ctx.model_copy(update={"strict_freetext": True})
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  # 3 escalation attempts x 4 parsed-but-copied values each: the error must
  # say WHY the yield was unusable (copies, not parse failures) — the
  # 2026-07-16 run's "0 of 32 choices parsed" message misdiagnosed itself.
  with pytest.raises(
      FreeTextEmptyYieldError, match=r"parsed=12.*verbatim_copies=12"):
    engine.setup(_CopyingClient(copies), strict_ctx)


def test_b1_strict_empty_yield_message_reports_zero_parsed(free_text_ctx):
  strict_ctx = free_text_ctx.model_copy(update={"strict_freetext": True})
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with pytest.raises(FreeTextEmptyYieldError, match=r"parsed=0"):
    engine.setup(_EmptyYieldClient(), strict_ctx)


def test_b1_fallback_milestone_reports_counts(caplog, free_text_ctx):
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(_CopyingClient(copies), free_text_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "parsed=12" in text
  assert "verbatim_copies=12" in text
  assert "attempts=3" in text


def test_b2_retries_with_escalating_temperature_on_all_copies(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  exemplar = profiles["summary"].text_pool[0]
  client = _CopyThenNovelClient(
      [{
          "values": [exemplar]
      }],
      [{
          "values": ["A clearly novel ticket summary."]
      }],
  )
  hook = FreeTextHook(client)
  pool = hook._pool_for(profiles["summary"], GenerationConfig(seed=1))
  assert "A clearly novel ticket summary." in pool
  assert len(client.calls) >= 2
  temps = [c.get("temperature") for c in client.calls]
  assert temps[1] > temps[0], f"retry must escalate temperature, got {temps}"


def test_b2_strict_all_copy_message_reports_counts(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  exemplar = profiles["summary"].text_pool[0]
  hook = FreeTextHook(_CopyingClient([{"values": [exemplar]}]), strict=True)
  with pytest.raises(
      FreeTextEmptyYieldError, match=r"parsed=3.*verbatim_copies=3"):
    hook._pool_for(profiles["summary"], GenerationConfig(seed=1))


def test_b2_pool_excludes_verbatim_copies(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  exemplar = profiles["summary"].text_pool[0]
  client = _CopyingClient([{
      "values": [exemplar, "A fresh, clearly novel ticket summary."]
  }])
  hook = FreeTextHook(client)
  pool = hook._pool_for(profiles["summary"], GenerationConfig(seed=1))
  assert "A fresh, clearly novel ticket summary." in pool
  # The LLM pool is the "diverge" side of B.2's similarity blend — observed
  # values reach the output via the reference pool, never via the LLM pool.
  assert exemplar not in pool


# ---------------------------------------------------------------------------
# Sampling-truncation unclamp — a served model can pin top_k/top_p via its own
# generation_config.json (Qwen3-4B ships top_k=20, top_p=0.8; the 2026-07-16
# corp run logged vLLM's override warning). Under that truncation the nucleus
# collapses to the echo token and temperature escalation is inert: 96/96
# verbatim copies at 0.7, 1.0 AND 1.3. Retries must therefore send explicit
# top_p/top_k overrides; only the first attempt keeps the vendor defaults.
# ---------------------------------------------------------------------------


def test_escalating_sampling_unclamps_truncation_on_retries():
  from sdfb_core.engines.base import escalating_sampling

  levels = escalating_sampling()
  assert [lv.temperature for lv in levels] == [0.7, 1.0, 1.3]
  # First attempt: vendor-tuned model defaults stay in force.
  assert levels[0].top_p is None
  assert levels[0].top_k is None
  # Retries: full nucleus, no top-k truncation (0 = vLLM "all tokens").
  assert all(lv.top_p == 1.0 and lv.top_k == 0 for lv in levels[1:])


def test_escalating_sampling_at_ceiling_still_gets_an_unclamped_retry():
  from sdfb_core.engines.base import escalating_sampling

  # B.2 with similarity=0 starts at the 1.3 ceiling — temperature alone has
  # nowhere to go, but the truncation unclamp must still get its retry.
  levels = escalating_sampling(1.3)
  assert len(levels) == 2
  assert levels[0] == (1.3, None, None)
  assert levels[1] == (1.3, 1.0, 0)


def test_b1_retry_unclamps_sampling_truncation(free_text_ctx):
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  client = _CopyThenNovelClient(copies, [{"bio": "A brand-new synthetic bio."}])
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  first, second = client.calls[0], client.calls[1]
  assert first.get("top_p") is None and first.get("top_k") is None
  assert second.get("top_p") == 1.0
  assert second.get("top_k") == 0


def test_b2_retry_unclamps_sampling_truncation(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  exemplar = profiles["summary"].text_pool[0]
  client = _CopyThenNovelClient(
      [{
          "values": [exemplar]
      }],
      [{
          "values": ["A clearly novel ticket summary."]
      }],
  )
  hook = FreeTextHook(client)
  hook._pool_for(profiles["summary"], GenerationConfig(seed=1))
  first, second = client.calls[0], client.calls[1]
  assert first.get("top_p") is None and first.get("top_k") is None
  assert second.get("top_p") == 1.0
  assert second.get("top_k") == 0


# ---------------------------------------------------------------------------
# Echo-vs-collision diagnostics — `verbatim_copies` alone conflates two very
# different failures: the model parroting the few exemplars it was SHOWN
# (sampling/prompt defect) vs. in-format generations that happen to exist in
# the full reference sample it never saw (saturated key space — per-column
# novelty is unattainable there). The 2026-07-16 rerun kept reporting 96/96
# copies even with truncation unclamped, which only a distinct/prompt_echoes
# breakdown can explain. Counts only — never values.
# ---------------------------------------------------------------------------


class _KeyProf:
  """Minimal stand-in for ColumnProfile in direct _pool_llm_yield tests."""

  name = "key"
  observed_values = tuple(f"K-{i:04d}" for i in range(100))


def test_b1_pool_yield_separates_prompt_echoes_from_reference_collisions():
  from sdfb_core.engines.b1_rag.engine import _pool_llm_yield

  class _CollidingClient:
    # K-0050 / K-0060 exist in the reference but were NOT shown to the
    # model; K-0001 is a shown exemplar. All are copies, only one an echo.
    def generate_json(self, *a, **k):
      return [{"key": "K-0050"}, {"key": "K-0060"}, {"key": "K-0001"}]

  y = _pool_llm_yield(_CollidingClient(), "p", {}, _KeyProf(),
                      ["K-0001", "K-0002"])
  assert y.pool == []
  assert y.parsed == 3 * y.attempts
  assert y.copies == y.parsed
  assert y.prompt_echoes == 1 * y.attempts
  assert y.distinct == 3


def test_b1_pool_yield_counts_pure_echo():
  from sdfb_core.engines.b1_rag.engine import _pool_llm_yield

  class _EchoClient:

    def generate_json(self, *a, **k):
      return [{"key": "K-0001"}, {"key": "K-0001"}, {"key": "K-0002"}]

  y = _pool_llm_yield(_EchoClient(), "p", {}, _KeyProf(), ["K-0001", "K-0002"])
  assert y.prompt_echoes == y.parsed
  assert y.distinct == 2


def test_b1_strict_message_reports_distinct_and_prompt_echoes(free_text_ctx):
  strict_ctx = free_text_ctx.model_copy(update={"strict_freetext": True})
  copies = [{"bio": r["bio"]} for r in free_text_ctx.reference_rows[:4]]
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with pytest.raises(
      FreeTextEmptyYieldError, match=r"distinct=4.*prompt_echoes=\d+"):
    engine.setup(_CopyingClient(copies), strict_ctx)


def test_b2_strict_message_reports_distinct_and_prompt_echoes(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  exemplar = profiles["summary"].text_pool[0]
  hook = FreeTextHook(_CopyingClient([{"values": [exemplar]}]), strict=True)
  # The exemplar IS in the prompt for B.2, so every copy is also an echo.
  with pytest.raises(
      FreeTextEmptyYieldError, match=r"distinct=1.*prompt_echoes=3"):
    hook._pool_for(profiles["summary"], GenerationConfig(seed=1))


# ---------------------------------------------------------------------------
# One array completion, not n blind siblings — B.1 used to request n=32
# choices whose schema allowed exactly ONE value each: every choice was blind
# to the others, so "generate 32 distinct values" was unsatisfiable per
# completion and vLLM collapsed all 32 into the identical modal echo
# (2026-07-16 runs: distinct=1, prompt_echoes=96 at every sampling level).
# The pool must be ARRAY completions — each choice carries a values array,
# where the model sees what it already wrote and can actually be distinct
# (B.2's shape). Since 2026-07-25, n=_POOL_PARALLEL_CHOICES independent
# array completions ride one round trip (unseeded, so choices diverge) —
# never n single-value choices, which is the 2026-07-16 collapse shape.
# ---------------------------------------------------------------------------


def test_b1_pool_requests_parallel_array_completions_not_single_values(
    free_text_ctx):
  from sdfb_core.engines.b1_rag.engine import _POOL_PARALLEL_CHOICES

  class _RecordingNovelClient:

    def __init__(self):
      self.calls: list[dict] = []

    def generate_json(self, prompt, json_schema, **k):
      self.calls.append({"json_schema": json_schema, **k})
      return [{"values": ["A fresh synthetic bio."]}]

  client = _RecordingNovelClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  call = client.calls[0]
  assert call["n"] == _POOL_PARALLEL_CHOICES
  assert call["json_schema"]["properties"]["values"]["type"] == "array"
  assert "A fresh synthetic bio." in engine._free_text_pools["bio"]


def test_b1_string_values_extracts_array_and_column_shapes():
  from sdfb_core.engines.b1_rag.engine import _string_values

  results = [
      {
          "values": ["a", "b", ""]
      },  # the guided array shape
      {
          "bio": "c"
      },  # echoed column-keyed dict (FakeModelClient echo mode)
      {
          "other": 1
      },
      "junk",
  ]
  assert _string_values(results, "bio") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# B.1 pool inference must not pin a request seed — a fixed seed with n>1
# collapses all n vLLM choices to a single completion (2026-07-15 run:
# identical choice lengths per request).
# ---------------------------------------------------------------------------


def test_b1_pool_inference_does_not_pin_seed(free_text_ctx):

  class _RecordingClient(_EmptyYieldClient):

    def generate_json(self, *a, **k):
      self.calls.append(k)
      return [{"bio": f"generated value {i}"} for i in range(3)]

  client = _RecordingClient()
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  assert client.calls, "expected a pool-inference LLM call for 'bio'"
  for call in client.calls:
    assert call.get("seed") is None


# ---------------------------------------------------------------------------
# Pool fill-to-target — accepting the FIRST non-empty yield let one
# conservative completion (still under Qwen's generation_config pin) define
# the whole pool: the 2026-07-17 B.1 run landed COL_048 with 4 distinct
# values over 1000 rows, and the unclamp retries of 71aaaa1 never ran. The
# escalation loop must accumulate novel values ACROSS levels until the pool
# target is met, and exhausting all levels below target must be loud.
# ---------------------------------------------------------------------------


class _NovelBatchClient:
  """Returns `per_call` FRESH novel values on every call (batch i differs
    from batch i-1), so accumulation across escalation levels is testable."""

  def __init__(self, per_call: int, repeat: bool = False):
    self._per_call = per_call
    self._repeat = repeat
    self.calls: list[dict] = []

  def generate_json(self, *a, **k):
    i = 0 if self._repeat else len(self.calls)
    self.calls.append(k)
    return [{"values": [f"Novel value {i}-{j}" for j in range(self._per_call)]}]


def test_b1_pool_accumulates_across_levels_until_target(free_text_ctx):
  client = _NovelBatchClient(per_call=20)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  engine.setup(client, free_text_ctx)
  # 20 novel at level 1 < 32 target → keep escalating; 40 ≥ 32 → stop.
  assert len(client.calls) == 2
  assert len(engine._free_text_pools["bio"]) == 32


def test_b1_pool_undersized_emits_milestone(caplog, free_text_ctx):
  # The same 4 novel values on every attempt: levels exhaust below target.
  client = _NovelBatchClient(per_call=4, repeat=True)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(client, free_text_ctx)
  assert len(client.calls) == 3, "all escalation levels must run"
  assert len(engine._free_text_pools["bio"]) == 4
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_pool_undersized" in text
  assert "column=bio" in text
  assert "pool_size=4" in text
  assert "target=32" in text


def test_b1_full_first_yield_emits_no_undersized_milestone(
    caplog, free_text_ctx):
  client = _NovelBatchClient(per_call=32)
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    engine.setup(client, free_text_ctx)
  assert len(client.calls) == 1
  text = "\n".join(r.message for r in caplog.records)
  assert "freetext_pool_undersized" not in text


def test_b2_pool_accumulates_across_levels_until_target(wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  client = _NovelBatchClient(per_call=5)
  hook = FreeTextHook(client, pool_size=8)
  pool = hook._pool_for(profiles["summary"], GenerationConfig(seed=1))
  assert len(client.calls) == 2
  assert len(pool) == 8


def test_b2_pool_undersized_emits_milestone(caplog, wide_ctx):
  profiles = profile_table(wide_ctx.table_schema, wide_ctx.reference_rows)
  client = _NovelBatchClient(per_call=3, repeat=True)
  hook = FreeTextHook(client, pool_size=8)
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    pool = hook._pool_for(profiles["summary"], GenerationConfig(seed=1))
  assert len(client.calls) == 3
  assert len(pool) == 3
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=freetext_pool_undersized" in text
  assert "column=summary" in text
  assert "pool_size=3" in text
  assert "target=8" in text


# ---------------------------------------------------------------------------
# B.1 setup phase milestones — embed / index / pool timings
# ---------------------------------------------------------------------------


def test_b1_setup_emits_phase_milestones(caplog, free_text_ctx):
  engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_BoomClient(), free_text_ctx)
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=b1_embed_done" in text
  # Trailing space delimiter — a loose "rows=32" substring would also
  # match "rows=320" and silently stop catching a wrong row count.
  assert "rows=32 " in text
  assert "SDFB_MILESTONE name=b1_index_built" in text
  assert "SDFB_MILESTONE name=b1_pools_built" in text
