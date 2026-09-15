"""Dominant-value re-emission + identifier mask-mix (2026-08-07 A_TABLE R1).

Two crosscheck findings from the post-ADR-0023 cold baseline:

  - COL_053/COL_054 miss their DOMINANT value entirely (`ZZ3000` /
    `BATCH` at 77%+ share): an enum-like literal hiding in a free-text
    column can never come out of the pool (ADR 0023 rightly rejects all
    source values), so the head mass must be re-emitted at its observed
    frequency — like temporal sentinels, and k-anonymous for the same
    reason (a value shared by hundreds of rows identifies nobody).
  - COL_001 reproduces 0% of source masks: the collapsed per-position
    identifier template merges variant masks into digit+upper classes.
    Columns whose shape_mix covers most DISTINCT values (rigid masks)
    must draw from the mix; random-mask columns (36-hex ids) must keep
    the collapsed template — a top-8 mask mix would collapse THEIR
    diversity instead.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,protected-access

from __future__ import annotations

import collections

from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationConfig
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler
from sdfb_core.engines.b1_rag.profile import (
    ColumnKind,
    ColumnProfile,
    profile_columns,
)


def _mask(v: str) -> str:
  return "".join(
      "9" if c.isdigit() else "A" if c.isupper() else "a" if c.islower() else c
      for c in v)


def _schema(name: str) -> TableSchema:
  return TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": name,
          "type": "STRING",
          "mode": "NULLABLE"
      }],
  })


class TestHeadValueProfiling:

  def test_dominant_literal_is_captured_with_its_share(self) -> None:
    values = ["ZZ3000"] * 770 + [f"ZZ1{i:03d}" for i in range(230)]
    rows = [{"code": v} for v in values]
    profiles = profile_columns(_schema("code"), rows)
    prof = profiles["code"]
    assert prof.kind is ColumnKind.FREE_TEXT
    assert len(prof.head_values) == 1
    value, share = prof.head_values[0]
    assert value == "ZZ3000"
    assert abs(share - 0.77) < 0.01

  def test_low_share_and_low_count_values_are_not_heads(self) -> None:
    # 3% share (30/1000) is below the 5% floor; nothing qualifies.
    values = ["ZZ3000"] * 30 + [f"ZZ1{i:03d}" for i in range(970)]
    rows = [{"code": v} for v in values]
    prof = profile_columns(_schema("code"), rows)["code"]
    assert prof.kind is ColumnKind.FREE_TEXT
    assert prof.head_values == ()


class TestHeadValueSampling:

  def _engine_with(self, prof: ColumnProfile, pool: list[str]) -> B1RagEngine:
    engine = B1RagEngine()
    engine._samplers = {prof.name: ColumnSampler(prof)}
    engine._column_order = [prof.name]
    engine._free_text_pools = {prof.name: pool}
    return engine

  def test_heads_emitted_at_observed_share_after_null_and_empty(self) -> None:
    prof = ColumnProfile(
        name="code",
        bq_type="STRING",
        kind=ColumnKind.FREE_TEXT,
        nullable=True,
        null_fraction=0.2,
        empty_fraction=0.3,
        head_values=(("ZZ3000", 0.5),),
        observed_values=("ZZ3000", "ZZ1001", "ZZ1002"),
        is_unique_valued=False,
    )
    engine = self._engine_with(prof, ["tail-1", "tail-2"])
    out = engine._sample_free_text(8000, GenerationConfig(seed=7), 0.5)["code"]
    counts = collections.Counter(out)
    n = len(out)
    assert abs(counts[None] / n - 0.2) < 0.03
    assert abs(counts[""] / n - 0.3) < 0.03
    # P(head) = (1 - null - empty) * share = 0.5 * 0.5 = 0.25
    assert abs(counts["ZZ3000"] / n - 0.25) < 0.03
    tail = n - counts[None] - counts[""] - counts["ZZ3000"]
    assert abs(tail / n - 0.25) < 0.03

  def test_no_heads_means_behavior_unchanged(self) -> None:
    prof = ColumnProfile(
        name="code",
        bq_type="STRING",
        kind=ColumnKind.FREE_TEXT,
        nullable=False,
        null_fraction=0.0,
        observed_values=("a", "b"),
    )
    engine = self._engine_with(prof, ["tail-1"])
    out = engine._sample_free_text(50, GenerationConfig(seed=7), 0.5)["code"]
    assert set(out) == {"tail-1"}


class TestIdentifierMaskMix:

  def _identifier_profile(self, values: list[str]) -> ColumnProfile:
    rows = [{"ident": v} for v in values]
    prof = profile_columns(_schema("ident"), rows)["ident"]
    assert prof.kind is ColumnKind.FREE_TEXT
    assert prof.identifier_shape is not None
    return prof

  def _draw(self, prof: ColumnProfile, n: int) -> list[str]:
    engine = B1RagEngine()
    engine._samplers = {prof.name: ColumnSampler(prof)}
    engine._column_order = [prof.name]
    engine._free_text_pools = {}
    out = engine._sample_free_text(n, GenerationConfig(seed=11), 0.5)["ident"]
    return [v for v in out if v]

  def test_rigid_mask_column_preserves_the_observed_mask_mix(self) -> None:
    # Two mask variants (60/40) whose letter/digit ROLES sit at
    # different positions: the collapsed template merges every position
    # into digit+upper and scrambles both masks.
    values = [f"XY{i:010d}Q{i:011d}" for i in range(60)
             ] + [f"{i:04d}QRST{i:016d}" for i in range(40)]
    prof = self._identifier_profile(values)
    drawn = self._draw(prof, 400)
    assert drawn
    source_masks = {_mask(v) for v in values}
    assert {_mask(v) for v in drawn} <= source_masks
    assert sum(1 for v in drawn if v.startswith("XY")) > 0
    assert sum(1 for v in drawn if v[4:8] == "QRST") > 0

  def test_random_mask_column_keeps_mask_diversity(self) -> None:
    # High-entropy masks (36-hex-style): top-8 mask mix would collapse
    # diversity to 8 skeletons; the full mask table must not.
    import random as _r

    rng = _r.Random(3)
    values = [
        "".join(rng.choice("0123456789ABCDEF")
                for _ in range(16))
        for _ in range(200)
    ]
    values = list(dict.fromkeys(values))
    prof = self._identifier_profile(values)
    drawn = self._draw(prof, 300)
    assert len({_mask(v) for v in drawn}) > 8

  def test_long_tail_mask_column_keeps_alphabet_prefix_and_entropy(
      self) -> None:
    # COL_001-class. 2026-08-09: the collapsed template scrambled every
    # position from MERGED digit+upper alphabets (0% observed masks).
    # 2026-08-20 (wave 4): the opposite failure — confining draws to
    # the capped mask table collapsed mask ENTROPY (52k source masks →
    # 1024, each inflated 1/recall; COL_064 plateaued at ~0.2% per
    # shape). Near-unique-mask columns now synthesize tail masks per
    # position from observed char frequencies: novel masks are correct
    # here (each real mask is itself ~unique) — what must hold is the
    # alphabet, the literal prefix, novelty, and mask diversity.
    import random as _r

    rng = _r.Random(9)
    values = list(
        dict.fromkeys(
            "C2E" + "".join(rng.choice("0123456789ABCDEF")
                            for _ in range(21))
            for _ in range(120)))
    prof = self._identifier_profile(values)
    drawn = self._draw(prof, 300)
    assert drawn
    hex_chars = set("0123456789ABCDEF")
    assert all(set(v) <= hex_chars for v in drawn)
    assert all(v.startswith("C2E") for v in drawn)
    assert len(set(drawn) & set(values)) == 0  # novelty holds
    # Entropy preserved: far more distinct masks than a top-8 collapse.
    assert len({_mask(v) for v in drawn}) > 8
