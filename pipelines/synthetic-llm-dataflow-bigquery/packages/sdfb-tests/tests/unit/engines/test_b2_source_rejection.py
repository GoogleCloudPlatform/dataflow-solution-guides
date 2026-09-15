"""ADR 0023 seam in B.2's FreeTextHook — the R5 prerequisite.

B.2's pool novelty filter and shape fallback reject only against
`profile.text_pool` (the profiled sample) — the same full-domain gap that
memorized 33-99% of B_TABLE's columns on B.1 before ADR 0023. R5 compares
the engines on the same tables; without this seam its memorization numbers
would measure the missing filter, not the engine.

The reference blend is untouched: it is already confined to enum-like
columns (≤ `_REFERENCE_BLEND_MAX_DISTINCT` observed distinct values) —
the same by-design category reuse the substantive copy metric exempts.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

from __future__ import annotations

import logging

import numpy as np
from sdfb_core.engines import GenerationConfig
from sdfb_core.engines.b2_library.fidelity import ColumnKind, ColumnProfile
from sdfb_core.engines.b2_library.freetext import FreeTextHook

_OBSERVED = tuple(
    f"Observation number {i} carries plenty of descriptive prose text."
    for i in range(1, 21))
_UNOBSERVED = [
    f"Observation number {i} carries plenty of descriptive prose text."
    for i in range(21, 60)
]
_DOMAIN = frozenset(_OBSERVED) | frozenset(_UNOBSERVED)
_NOVEL = [
    f"Synthetic person {i} curates miniature bonsai gardens weekly."
    for i in range(64)
]


class _FakeStore:

  def __init__(self, values: frozenset[str] | None):
    self.values = values
    self.calls: list[str] = []

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    self.calls.append(column)
    return self.values


class _BoomStore:

  def fetch_distinct(self, column: str) -> frozenset[str] | None:
    raise RuntimeError("bq unavailable")


class _CollidingClient:
  """Returns unobserved-but-real source values mixed with novel ones."""

  def generate_json(self, **kwargs):
    return [{"values": _UNOBSERVED[:24] + _NOVEL}]


class _EchoClient:
  """Parses fine but only ever echoes observed values (copy-saturated)."""

  def __init__(self, observed: tuple[str, ...]):
    self.observed = observed

  def generate_json(self, **kwargs):
    return [{"values": list(self.observed)}]


def _prose_profile() -> ColumnProfile:
  return ColumnProfile(
      name="summary",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      text_pool=_OBSERVED,
  )


def _sample(hook: FreeTextHook, profile: ColumnProfile, n: int = 200) -> set:
  rng = np.random.default_rng(3)
  out = hook.sample(profile, n, GenerationConfig(seed=7, similarity=0.0), rng)
  return {v for v in out if v}


def test_pool_rejects_full_source_domain(caplog) -> None:
  store = _FakeStore(_DOMAIN)
  hook = FreeTextHook(_CollidingClient(), source_value_store=store)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    drawn = _sample(hook, _prose_profile())
  assert drawn, "pool must still build from the novel values"
  assert not drawn & _DOMAIN
  assert "summary" in store.calls
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_source_filter" in text


def test_without_store_behavior_is_unchanged() -> None:
  hook = FreeTextHook(_CollidingClient())
  drawn = _sample(hook, _prose_profile())
  # Unobserved source values pass the sample-only filter — pre-seam
  # behavior, kept bit-for-bit when no store is attached.
  assert drawn & frozenset(_UNOBSERVED)


def test_store_error_degrades_loudly_not_fatally(caplog) -> None:
  hook = FreeTextHook(_CollidingClient(), source_value_store=_BoomStore())
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    drawn = _sample(hook, _prose_profile())
  assert drawn
  text = "\n".join(r.message for r in caplog.records)
  assert "name=freetext_pool_source_filter_error" in text


def test_shape_fallback_rejects_source_values() -> None:
  # Three varying digit positions (template space REF000A-REF999A, 1000
  # values) with half of it in the live domain: novel values exist, and
  # every one the fallback emits must avoid the domain half.
  observed = tuple(f"REF{i:03d}A" for i in (111, 222, 333, 444, 155))
  domain = frozenset(f"REF{i:03d}A" for i in range(500))
  profile = ColumnProfile(
      name="ref_code",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      text_pool=observed,
  )
  hook = FreeTextHook(
      _EchoClient(observed), source_value_store=_FakeStore(domain))
  drawn = _sample(hook, profile)
  assert drawn, "copy-saturated build must land on the shape fallback"
  assert not drawn & domain


def test_b2_engine_threads_store_from_ctx() -> None:
  from sdfb_core.contracts import TableSchema
  from sdfb_core.engines import GenerationContext, get_engine

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "summary",
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })
  store = _FakeStore(frozenset())
  ctx = GenerationContext(
      table_schema=schema,
      reference_rows=[{
          "summary": s
      } for s in _OBSERVED],
      pipeline_run_id="b2-seam",
      source_value_store=store,
  )
  engine = get_engine("b2_library")()
  engine.setup(_CollidingClient(), ctx)
  assert engine._freetext_hook._source_value_store is store
  engine.teardown()


def test_source_values_fetched_once_per_column() -> None:
  store = _FakeStore(_DOMAIN)
  hook = FreeTextHook(_CollidingClient(), source_value_store=store)
  profile = _prose_profile()
  _sample(hook, profile)
  _sample(hook, profile)
  assert store.calls.count("summary") == 1
