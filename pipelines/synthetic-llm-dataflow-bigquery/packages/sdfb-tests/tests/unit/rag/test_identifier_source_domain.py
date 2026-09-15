"""Identifier columns draw against the FULL source domain (2026-08-11 R1).

A_TABLE COL_001: 52,549 distinct source identifiers, but the mask table is
built from the ≤10k-row reference sample — synthetic reproduced only 38% of
the source's exact masks, and novelty was only guaranteed against the
sample. The pool ladder already fetches each free-text column's full
distinct domain through `GenerationContext.source_value_store` (ADR 0023);
identifier-shaped columns now use the same seam:

  - the mask table + positional alphabets learn masks the sample never
    showed (recall lifts toward source coverage);
  - the rejection set covers the full domain (novelty by construction,
    the same guarantee pools already have).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,redefined-outer-name,unused-argument

from __future__ import annotations

import logging
import random

import pytest
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder
from sdfb_core.engines.base import GenerationConfig


def _mask(v: str) -> str:
  return "".join(
      "9" if c.isdigit() else "A" if c.isupper() else "a" if c.islower() else c
      for c in v)


# Sampled (observed) identifiers: C2E3 prefix + hex tail, one mask family.
_RNG = random.Random(31)
_OBSERVED_IDS = list(
    dict.fromkeys(
        # A letter is forced at position 4 so the digit-only tail mask can
        # only ever come from the source-only portion of the domain.
        "C2E3" + _RNG.choice("ABCDEF") +
        "".join(_RNG.choice("0123456789ABCDEF")
                for _ in range(7))
        for _ in range(60)))
# Source-only identifiers: same prefix, but a mask family the sample never
# showed (digit-only tail).
_SOURCE_ONLY_IDS = [f"C2E3{i:08d}" for i in range(200)]
_FULL_DOMAIN = frozenset(_OBSERVED_IDS) | frozenset(_SOURCE_ONLY_IDS)


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


@pytest.fixture
def ident_ctx() -> GenerationContext:
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.idents"
      },
      "schema": [{
          "name": "ident",
          "type": "STRING",
          "mode": "REQUIRED"
      },],
  })
  rows = [{"ident": v} for v in _OBSERVED_IDS]
  return GenerationContext(
      table_schema=schema,
      reference_rows=rows,
      reference_digest="ident-src-digest",
      pipeline_run_id="ident-src-run",
  )


def _drawn_idents(engine: B1RagEngine, n: int = 400) -> list[str]:
  records = list(engine.generate_batch(n, GenerationConfig(seed=5)))
  return [r.ident for r in records]  # type: ignore[attr-defined]


def test_identifier_draws_reject_full_source_domain(ident_ctx, caplog) -> None:
  store = _FakeSourceValueStore({"ident": _FULL_DOMAIN})
  ctx = ident_ctx.model_copy(update={"source_value_store": store})
  engine = B1RagEngine(embedder=HashingEmbedder())
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    engine.setup(_NoLLMClient(), ctx)
  assert "ident" in store.calls
  drawn = _drawn_idents(engine)
  assert drawn
  assert not set(drawn) & _FULL_DOMAIN
  text = "\n".join(r.message for r in caplog.records)
  assert "name=identifier_source_filter" in text
  engine.teardown()


def test_mask_table_learns_source_only_masks(ident_ctx) -> None:
  store = _FakeSourceValueStore({"ident": _FULL_DOMAIN})
  ctx = ident_ctx.model_copy(update={"source_value_store": store})
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_NoLLMClient(), ctx)
  drawn = _drawn_idents(engine, 600)
  source_only_mask = _mask(_SOURCE_ONLY_IDS[0])
  assert source_only_mask not in {_mask(v) for v in _OBSERVED_IDS}
  assert source_only_mask in {_mask(v) for v in drawn}
  assert all(v.startswith("C2E3") for v in drawn)
  engine.teardown()


def test_domain_supplements_support_without_swamping_row_mass() -> None:
  # 2026-08-20 wave-4 (D1): the full distinct domain was CONCATENATED
  # onto the row-weighted evidence, so a 146k-value domain out-voted a
  # 10k-row sample — the dominant mask fell 89.8% → 8.2% of draws and
  # the coverage pivot always fired. Weights must come from rows;
  # the domain feeds only novelty rejection, alphabets and (when the
  # rows' own singleton masks say unseen masks exist) tail support.
  from sdfb_core.engines.text_shapes import (
      build_identifier_artifacts,
      identifier_sampler_from,
  )

  rows = ["QX" + f"{i % 40:08d}" for i in range(360)
         ] + [f"{i:04d}QRSTUV" for i in range(40)]
  domain = frozenset(f"J{i:07d}ZZ" for i in range(5000))  # a third mask family
  artifacts = build_identifier_artifacts(
      tuple("x" * 10), None, rows, domain=domain)
  rng = random.Random(13)
  draw = identifier_sampler_from(artifacts, rng.randrange)
  drawn = [draw() for _ in range(600)]
  dominant = sum(1 for v in drawn if _mask(v) == "AA99999999") / len(drawn)
  assert dominant > 0.75  # source row mass is 90%; domain must not dilute it


def test_without_store_identifier_behavior_is_sample_bound(ident_ctx) -> None:
  engine = B1RagEngine(embedder=HashingEmbedder())
  engine.setup(_NoLLMClient(), ident_ctx)
  drawn = _drawn_idents(engine)
  assert drawn
  # Novelty vs the sample still holds; the unobserved domain is invisible.
  assert not set(drawn) & set(_OBSERVED_IDS)
  assert all(v.startswith("C2E3") for v in drawn)
  engine.teardown()
