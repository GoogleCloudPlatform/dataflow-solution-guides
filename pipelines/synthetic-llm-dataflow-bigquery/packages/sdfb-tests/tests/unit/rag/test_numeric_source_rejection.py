"""Identity-like INT64 columns reject the full source domain (wave 4).

2026-08-20 A_TABLE R1: COL_009 (INT64, 34,622 source-distinct account
numbers) landed `copy_ratio_substantive = 0.52` — the inverse-CDF draw
interpolates between observed order statistics, and in a dense integer
band the rounded interpolant IS another real (rare) account number. The
STRING identifier route already rejects the full domain through the
ADR 0023 `source_value_store` seam; integral NUMERIC columns above the
memorization rule's cardinality floor (source_distinct > 100) now use the
same seam:

  - draws whose rounded value hits a rare source value redraw, then nudge
    to the nearest non-source integer (marginal moves by ±few units);
  - values the SAMPLE saw repeatedly (multi-knot: sample frequency >= 2)
    are enum mass under the probe's k-anonymity floor and stay exact —
    frequent codes keep their head fidelity.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument

from __future__ import annotations

import logging

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.base import GenerationConfig

# Domain: every EVEN number in [1000, 3000) — 1000 distinct values with an
# odd-integer gap beside each, so a nudge always has somewhere to go.
_DOMAIN = frozenset(str(v) for v in range(1000, 3000, 2))
# Sample: 250 singleton evens plus one heavily repeated enum value.
_ENUM_VALUE = 2000
_SAMPLE_VALUES = [1000 + 2 * i for i in range(250)] + [_ENUM_VALUE] * 50


class _FakeSourceValueStore:

  def __init__(self, values_by_column):
    self.values_by_column = values_by_column
    self.calls: list[str] = []

  def fetch_distinct(self, column):
    self.calls.append(column)
    return self.values_by_column.get(column)


class _NoLLMClient:

  def generate_json(self, *, n=1, **kwargs):  # pragma: no cover - unused
    return [{"values": []} for _ in range(n)]


def _ctx(store=None) -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.accounts"
      },
      "schema": [
          {
              "name": "acct",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "code",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })
  rows = [{
      "acct": v,
      "code": 100 + (i % 30)
  } for i, v in enumerate(_SAMPLE_VALUES)]
  update = {}
  if store is not None:
    update["source_value_store"] = store
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="acct-digest",
      pipeline_run_id="acct-run",
  ).model_copy(update=update)


def _drawn(engine: B1RagEngine, n: int = 400) -> list[int]:
  records = list(engine.generate_batch(n, GenerationConfig(seed=3)))
  return [r.acct for r in records]  # type: ignore[attr-defined]


def test_numeric_draws_reject_rare_source_values(caplog) -> None:
  store = _FakeSourceValueStore({"acct": _DOMAIN})
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_NoLLMClient(), _ctx(store))
  assert "acct" in store.calls
  text = "\n".join(r.message for r in caplog.records)
  assert "name=numeric_source_filter" in text
  drawn = _drawn(engine)
  assert drawn
  # Rare (singleton-knot) source values are scrubbed; the multi-knot enum
  # value is k-anonymous head mass and stays exact.
  collided = {v for v in drawn if str(v) in _DOMAIN}
  assert collided <= {_ENUM_VALUE}, sorted(collided)[:10]
  assert _ENUM_VALUE in collided  # head fidelity preserved
  # Marginal stays in the observed band (nudges are +/- a few units).
  assert min(drawn) >= 990 and max(drawn) <= 2510
  engine.teardown()


def test_small_domain_numeric_columns_skip_the_fetch() -> None:
  # `code` has 30 distinct values — below the memorization rule's
  # source_distinct > 100 floor. Collisions there are enum reuse, not a
  # privacy signal; no domain query is spent on it.
  store = _FakeSourceValueStore({"acct": _DOMAIN, "code": frozenset()})
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_NoLLMClient(), _ctx(store))
  assert "code" not in store.calls
  engine.teardown()


def test_without_store_numeric_behavior_is_unchanged() -> None:
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_NoLLMClient(), _ctx())
  drawn = _drawn(engine)
  assert drawn
  # Sample-bound inverse-CDF: in-range draws, no crash, no filtering.
  assert min(drawn) >= 1000 and max(drawn) <= 2498
  engine.teardown()


class _FrequentAwareStore(_FakeSourceValueStore):
  """Store that also serves source-side k-anonymous values (wave 4 v2)."""

  def __init__(self, values_by_column, frequent_by_column):
    super().__init__(values_by_column)
    self.frequent_by_column = frequent_by_column
    self.frequent_calls: list[tuple[str, int]] = []

  def fetch_frequent(self, column, min_count):
    self.frequent_calls.append((column, min_count))
    return self.frequent_by_column.get(column)


def test_kanon_keep_set_comes_from_source_frequencies() -> None:
  # 2026-08-21 four-run cycle: COL_009 measured substantive copy 0.25
  # while the scrub's own telemetry said ~0.14 — the gap was PSEUDO
  # multi-knots: values seen >=2x in the 10k sample that are still rare
  # in the full source (at ~21x subsampling, sample-frequency 2 does not
  # imply source-frequency >= 10). The keep-set now comes from the
  # source itself (HAVING COUNT(*) >= 10 — the probe's own k-anonymity
  # floor); the sample heuristic stays only as the no-store fallback.
  pseudo = 1400  # even → in the domain; appears twice in the sample
  rows = [{
      "acct": v,
      "code": 100 + (i % 30)
  } for i, v in enumerate([*_SAMPLE_VALUES, pseudo])]
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.accounts"
      },
      "schema": [
          {
              "name": "acct",
              "type": "INT64",
              "mode": "REQUIRED"
          },
          {
              "name": "code",
              "type": "INT64",
              "mode": "REQUIRED"
          },
      ],
  })
  store = _FrequentAwareStore({"acct": _DOMAIN},
                              {"acct": frozenset({str(_ENUM_VALUE)})})
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="acct-digest-2",
      pipeline_run_id="acct-run-2",
  ).model_copy(update={"source_value_store": store})
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_NoLLMClient(), ctx)
  assert ("acct", 10) in store.frequent_calls
  drawn = _drawn(engine)
  collided = {v for v in drawn if str(v) in _DOMAIN}
  # The genuinely frequent enum value survives; the pseudo multi-knot
  # (sample freq 2, absent from the source-frequent set) is scrubbed.
  assert pseudo not in collided
  assert collided <= {_ENUM_VALUE}
  engine.teardown()


class _RedrawForbiddenSampler:

  def sample_numpy(self, rng, n, similarity):  # pragma: no cover - guard
    raise AssertionError("nudge-first scrub must not redraw here")

  sample_python = sample_numpy


def test_scrub_is_nudge_first_when_neighbors_are_free() -> None:
  # 2026-08-21 four-run cycle: the redraw-first scrub redistributed the
  # rejected mass across the whole marginal — a version-number column's
  # decile-KS rose 0.04 → 0.17 while nudges stayed at ~1%. Nudging first
  # keeps each scrubbed value inside its quantile neighborhood; redraws
  # remain the fallback for saturated neighborhoods only.
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine._numeric_domains = {"acct": _DOMAIN}
  engine._numeric_multi_knots = {"acct": frozenset()}
  engine._numeric_scrub_logged = set()
  values = [1000, 1502, 2004, 999, 2001]  # evens collide, odds are free
  out = engine._scrub_numeric_collisions("acct", _RedrawForbiddenSampler(),
                                         None, list(values), 0.5, True)
  for before, after in zip(values, out, strict=True):
    if str(before) in _DOMAIN:
      assert str(after) not in _DOMAIN
      assert abs(after - before) <= 24  # locality preserved
    else:
      assert after == before


@pytest.mark.parametrize("n", [64])
def test_rejection_is_seed_reproducible(n: int) -> None:
  store = _FakeSourceValueStore({"acct": _DOMAIN})
  a = B1RagEngine(embedder=HashingEmbedder())
  a.setup(_NoLLMClient(), _ctx(store))
  first = [r.acct for r in a.generate_batch(n, GenerationConfig(seed=11))
          ]  # type: ignore[attr-defined]
  a.teardown()
  b = B1RagEngine(embedder=HashingEmbedder())
  b.setup(_NoLLMClient(), _ctx(store))
  second = [r.acct for r in b.generate_batch(n, GenerationConfig(seed=11))
           ]  # type: ignore[attr-defined]
  b.teardown()
  assert first == second
