"""Joint FK key draws — referential integrity by construction (ADR 0031).

The 2026-08-23 relational run measured **81.8% orphans** on a 3-column FK
edge. Per-column pools (v1) cannot do better: a child drawing each FK
column independently lands on a tuple the parent actually holds only with
probability ``|parent keys| / prod(per-column distincts)`` — 2.7% on that
run's measured cardinalities, i.e. it would have made integrity WORSE.

The fix is to draw the whole key TUPLE as one unit. These tests pin the
three properties that makes it usable in production: integrity (every
drawn tuple exists in the parent), marginal fidelity (the child's own
value shares survive the restriction), and determinism.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring

from __future__ import annotations

import random

import pytest
from sdfb_core.engines.fk_keys import FkKeyPool, build_fk_key_pools

# A parent whose 2-column key space is SPARSE inside the product grid:
# 3 x 3 = 9 combinations exist, only 4 are real parent keys.
_PARENT = (("ES", 10), ("ES", 20), ("FR", 30), ("DE", 10))


def _child_rows(n: int = 100) -> list[dict]:
  """Child sample: 'ES' dominates (70%), 'FR' is rare, 'DE' unseen."""
  rows = [{"CC": "ES", "BR": 10 if i % 2 else 20} for i in range(70)]
  rows += [{"CC": "FR", "BR": 30} for _ in range(30)]
  return rows[:n]


def _pool(rows=None, keys=_PARENT) -> FkKeyPool:
  return FkKeyPool.from_reference(
      cols=("CC", "BR"),
      keys=keys,
      reference_rows=_child_rows() if rows is None else rows,
  )


@pytest.mark.parametrize("use_numpy", [False, True])
class TestIntegrityByConstruction:

  def test_every_drawn_tuple_is_a_real_parent_key(self, use_numpy):
    pool = _pool()
    drawn = pool.draw(500, _rng(7, use_numpy), use_numpy=use_numpy)
    assert len(drawn) == 500
    assert set(drawn) <= set(_PARENT)  # zero orphans, by construction

  def test_draw_is_deterministic_for_a_seed(self, use_numpy):
    pool = _pool()
    a = pool.draw(200, _rng(11, use_numpy), use_numpy=use_numpy)
    b = pool.draw(200, _rng(11, use_numpy), use_numpy=use_numpy)
    assert a == b


def _rng(seed: int, use_numpy: bool):
  if use_numpy:
    import numpy as np

    return np.random.default_rng(seed)
  return random.Random(seed)


class TestMarginalFidelity:

  def test_child_value_shares_survive_the_restriction(self):
    """Uniform-over-parent-keys would give 'ES' 50% (2 of 4 keys);
        the child sample says 70%. The weighted draw must track the
        child, which is the whole point of not drawing uniformly."""
    pool = _pool()
    drawn = pool.draw(4000, random.Random(3))
    es = sum(1 for t in drawn if t[0] == "ES") / len(drawn)
    assert 0.60 < es < 0.80  # child share 0.70, not the uniform 0.50

  def test_parent_values_unseen_in_the_child_stay_reachable(self):
    """'DE' never appears in the child sample. A zero-weight tail
        would make the synthetic child's domain strictly smaller than
        the parent's — the Good-Turing floor keeps it drawable."""
    drawn = _pool().draw(4000, random.Random(5))
    assert any(t[0] == "DE" for t in drawn)

  def test_falls_back_to_uniform_when_nothing_overlaps(self):
    pool = _pool(rows=[{"CC": "XX", "BR": 99} for _ in range(50)])
    assert pool.weighting == "uniform"
    drawn = pool.draw(400, random.Random(9))
    assert set(drawn) <= set(_PARENT)
    assert len(set(drawn)) == len(_PARENT)

  def test_no_child_sample_means_uniform(self):
    assert _pool(rows=[]).weighting == "uniform"


class TestNullPattern:

  def test_optional_fk_keeps_the_child_null_rate(self):
    """A child row with no parent is legitimate (SQL MATCH SIMPLE).
        Forcing every row to reference a parent invents relationships
        the source does not have — and moves the null marginal."""
    rows = [{"CC": None, "BR": None} for _ in range(30)]
    rows += [{"CC": "ES", "BR": 10} for _ in range(70)]
    pool = _pool(rows=rows)
    assert pool.null_fraction == pytest.approx(0.30)
    drawn = pool.draw(2000, random.Random(4))
    nulls = sum(1 for t in drawn if t == (None, None)) / len(drawn)
    assert 0.25 < nulls < 0.35
    assert set(drawn) - {(None, None)} <= set(_PARENT)

  def test_mandatory_fk_never_emits_a_null_tuple(self):
    drawn = _pool().draw(500, random.Random(6))
    assert (None, None) not in drawn


class TestPoolConstruction:

  def test_empty_parent_pool_is_a_loud_error(self):
    with pytest.raises(ValueError, match="empty parent key pool"):
      FkKeyPool.from_reference(cols=("CC",), keys=(), reference_rows=[])

  def test_keys_are_ordered_deterministically(self):
    """Same key SET, different arrival order (a side input's order is
        not stable) ⇒ same pool ⇒ same seeded draw."""
    a = FkKeyPool.from_reference(("CC", "BR"), _PARENT, _child_rows())
    b = FkKeyPool.from_reference(("CC", "BR"), tuple(reversed(_PARENT)),
                                 _child_rows())
    assert a.keys == b.keys
    assert a.draw(50, random.Random(2)) == b.draw(50, random.Random(2))

  def test_build_from_side_input_payloads(self):
    pools = build_fk_key_pools(
        [{
            "cols": ["CC", "BR"],
            "keys": [list(k) for k in _PARENT]
        }],
        _child_rows(),
    )
    assert [p.cols for p in pools] == [("CC", "BR")]
    assert set(pools[0].draw(100, random.Random(1))) <= set(_PARENT)


class TestEnginesDrawWholeTuples:
  """The end-to-end claim, on both engines: a composite FK edge lands
    only tuples the parent holds. This is what the 2026-08-23 run could
    not do — its 3-column edge drew each column from its own pool."""

  @staticmethod
  def _ctx():
    from sdfb_core.contracts.schema import TableSchema
    from sdfb_core.engines import GenerationContext

    schema = TableSchema.model_validate({
        "table_info": {
            "table_id": "p.d.child"
        },
        "schema": [
            {
                "name": "CC",
                "type": "STRING",
                "mode": "REQUIRED"
            },
            {
                "name": "BR",
                "type": "INT64",
                "mode": "REQUIRED"
            },
            {
                "name": "AMOUNT",
                "type": "INT64",
                "mode": "REQUIRED"
            },
        ],
    })
    rows = [{**r, "AMOUNT": i % 11} for i, r in enumerate(_child_rows())]
    return GenerationContext(
        table_schema=schema,
        reference_rows=rows,
        reference_digest="fk-joint",
        num_rows=400,
        fk_key_pools=[{
            "cols": ["CC", "BR"],
            "keys": [list(k) for k in _PARENT]
        }],
    )

  def _assert_no_orphans(self, engine):
    from sdfb_beam.handlers.fake_client import FakeModelClient
    from sdfb_core.engines import GenerationConfig

    ctx = self._ctx()
    engine.setup(FakeModelClient(reference_pool=ctx.reference_rows), ctx)
    rows = list(engine.generate_batch(400, GenerationConfig(seed=5)))
    assert rows
    drawn = {(r.CC, r.BR) for r in rows}
    assert drawn <= set(
        _PARENT), f"orphan tuples generated: {drawn - set(_PARENT)}"
    return rows

  def test_b1_generates_no_orphan_tuples(self):
    from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder

    rows = self._assert_no_orphans(
        B1RagEngine(embedder=HashingEmbedder(dim=64)))
    # …and the child's own marginal is still visible (not uniform).
    es = sum(1 for r in rows if r.CC == "ES") / len(rows)
    assert es > 0.55

  def test_b2_generates_no_orphan_tuples(self):
    from sdfb_core.engines.b2_library import B2LibraryEngine

    self._assert_no_orphans(B2LibraryEngine())

  def test_integrity_wins_over_a_route_llm_constraint(self):
    """An FK column carrying `route: llm` + a pattern must still land
        real parent keys: a format hint cannot outrank the reference.
        The clause would otherwise send it down the free-text/pool path,
        where the value is invented rather than referenced."""
    from sdfb_beam.handlers.fake_client import FakeModelClient
    from sdfb_core.contracts.schema import TableSchema
    from sdfb_core.engines import GenerationConfig, GenerationContext
    from sdfb_core.engines.b1_rag import B1RagEngine, HashingEmbedder

    schema = TableSchema.model_validate({
        "table_info": {
            "table_id": "p.d.child"
        },
        "schema": [
            {
                "name":
                    "CC",
                "type":
                    "STRING",
                "mode":
                    "REQUIRED",
                "description": ('{"llm_prompt_constraint": {"route": "llm", '
                                '"pattern": "^[A-Z]{2}$"}}'),
            },
            {
                "name": "BR",
                "type": "INT64",
                "mode": "REQUIRED"
            },
        ],
    })
    rows = [{"CC": "ES", "BR": 10 + (i % 3) * 10} for i in range(60)]
    ctx = GenerationContext(
        table_schema=schema,
        reference_rows=rows,
        reference_digest="fk-vs-clause",
        num_rows=200,
        fk_key_pools=[{
            "cols": ["CC", "BR"],
            "keys": [list(k) for k in _PARENT]
        }],
    )
    engine = B1RagEngine(embedder=HashingEmbedder(dim=64))
    engine.setup(FakeModelClient(reference_pool=rows), ctx)
    out = list(engine.generate_batch(200, GenerationConfig(seed=3)))
    assert out
    assert {(r.CC, r.BR) for r in out} <= set(_PARENT)


class TestPoolConstructionExtras:

  def test_single_column_edge_is_the_same_machinery(self):
    pool = FkKeyPool.from_reference(("CC",), (("ES",), ("FR",)), _child_rows())
    drawn = pool.draw(300, random.Random(8))
    assert set(drawn) <= {("ES",), ("FR",)}
    es = sum(1 for t in drawn if t == ("ES",)) / len(drawn)
    assert es > 0.55  # child-weighted, not 50/50
