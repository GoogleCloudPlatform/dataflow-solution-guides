"""Mask-table sampling + collapsed masks (wave-2 design doc §4a/§4c).

COL_001 evidence (2026-08-09 A_TABLE R1): below top-8 mask coverage the
collapsed template scrambles digit/letter arrangement — 0% mask recall.
Drawing a whole observed mask and filling class positions from the column's
observed per-class alphabets reproduces the mask marginal by construction.

COL_038 evidence (2026-08-09 B_TABLE R1): the LLM normalized a literal
three-space run to one space and the (inactive) format gate accepted all of
it. The collapsed mask keeps whitespace runs literal while letting
digit/letter run lengths vary.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring

import random
import re

from sdfb_core.engines.text_shapes import (
    build_mask_table,
    build_shape_mix,
    collapsed_mask,
    identifier_sampler,
    mask_alphabets,
    positional_alphabets,
    sample_from_mask,
    shape_mix_is_identifier_like,
)


def _mask(v: str) -> str:
  return "".join(
      "9" if c.isdigit() else "A" if c.isupper() else "a" if c.islower() else c
      for c in v)


class TestCollapsedMask:

  def test_alnum_runs_collapse_whitespace_stays_literal(self) -> None:
    assert collapsed_mask("CQZWD1   CS0912345678B") == "A+9   A+9+A"

  def test_single_symbol_runs_do_not_gain_plus(self) -> None:
    assert collapsed_mask("A1 B2") == "A9 A9"

  def test_punctuation_is_literal(self) -> None:
    assert collapsed_mask("TRF.EX-090000123") == "A+.A+-9+"

  def test_distinguishes_space_run_lengths(self) -> None:
    # The COL_038 failure: single-space output must NOT look like the
    # triple-space source.
    assert collapsed_mask("AB   CD") != collapsed_mask("AB CD")


class TestMaskTable:

  def test_weights_are_distinct_value_counts_heaviest_first(self) -> None:
    table = build_mask_table(["AB12", "CD34", "EF56", "12XY"])
    assert table is not None
    assert table[0] == (3, "AA99")
    assert table[1] == (1, "99AA")

  def test_weights_are_row_mass_not_distinct_counts(self) -> None:
    # 2026-08-11 R1 (COL_054/COL_015/COL_024-class): distinct-value
    # weighting inverted row-mass marginals — a heavily repeated value's
    # mask must outweigh a diverse-but-rare mask family.
    table = build_mask_table(["BATCH"] * 8 + ["101A", "202B", "303C"])
    assert table is not None
    assert table[0] == (8, "AAAAA")
    assert table[1] == (3, "999A")

  def test_cap_keeps_heaviest(self) -> None:
    values = [f"A{i:03d}" for i in range(50)] + ["9999", "8888"]
    table = build_mask_table(values, cap=1)
    assert table is not None
    assert len(table) == 1
    assert table[0] == (50, "A999")

  def test_too_few_values_is_none(self) -> None:
    assert build_mask_table(["X1"]) is None
    assert build_mask_table([]) is None


class TestMaskAlphabets:

  def test_alphabets_are_observed_chars_per_class(self) -> None:
    # Hex identifiers: uppercase alphabet is A-F only — a mask fill
    # must not emit G-Z (COL_001 stays hexadecimal).
    alphabets = mask_alphabets(["C2E3B715", "D0C4A9F1"])
    assert alphabets["A"] == "ABCDEF"
    assert alphabets["9"] == "01234579"

  def test_sample_fills_classes_and_keeps_literals(self) -> None:
    alphabets = {"9": "0123456789", "A": "ABCDEF"}
    rng = random.Random(5)
    for _ in range(30):
      v = sample_from_mask("A9-9A x", alphabets, rng.randrange)
      assert len(v) == 7
      assert v[0] in "ABCDEF" and v[1].isdigit()
      assert v[2] == "-" and v[3].isdigit() and v[4] in "ABCDEF"
      assert v[5:] == " x"


class TestPositionalAlphabets:
  """Per-position observed charsets (2026-08-11 A_TABLE R1, COL_001).

    Column-wide class alphabets scrambled fixed positional literals: every
    source value started with the literal run `C2E3` yet synthetic values
    opened with any observed hex character (shape recall 0.38, prefix lost).
    A position whose observed charset is a singleton is a literal by the
    same evidence rule `detect_identifier_shape` uses.
    """

  def test_singleton_positions_become_literals(self) -> None:
    pos = positional_alphabets(["C2E3AB", "C2E3CD", "C2E39F"])
    assert pos[6][0] == "C"
    assert pos[6][1] == "2"
    assert pos[6][2] == "E"
    assert pos[6][3] == "3"
    assert set(pos[6][4]) == {"A", "C", "9"}

  def test_single_value_length_buckets_are_skipped(self) -> None:
    # One value proves nothing about a shared per-position alphabet —
    # pinning it would regenerate the observed value verbatim.
    pos = positional_alphabets(["AB12", "CD34", "ONLYONE"])
    assert 4 in pos
    assert 7 not in pos

  def test_sample_from_mask_pins_positional_literals(self) -> None:
    values = [f"C2E3{i:02X}" for i in range(32)]
    alphabets = mask_alphabets(values)
    pos = positional_alphabets(values)
    rng = random.Random(11)
    for _ in range(50):
      v = sample_from_mask("A9A999", alphabets, rng.randrange, positional=pos)
      assert v.startswith("C2E3")

  def test_sample_from_mask_intersects_position_with_class(self) -> None:
    # A class position draws from the characters observed AT THAT
    # POSITION, not from the column-wide class alphabet.
    values = ["A1X9", "B2X8", "C3X7"]
    alphabets = mask_alphabets(values)
    pos = positional_alphabets(values)
    rng = random.Random(3)
    for _ in range(30):
      v = sample_from_mask("A9A9", alphabets, rng.randrange, positional=pos)
      assert v[0] in "ABC"
      assert v[1] in "123"
      assert v[2] == "X"
      assert v[3] in "789"

  def test_identifier_sampler_preserves_fixed_prefix(self) -> None:
    # End-to-end through the mask-table path (coverage below the mix
    # pivot): every draw keeps the literal C2E3 prefix.
    rng = random.Random(9)
    values = list(
        dict.fromkeys(
            "C2E3" + "".join(rng.choice("0123456789ABCDEF")
                             for _ in range(20))
            for _ in range(120)))
    shape = tuple(["C", "2", "E", "3"] + ["0123456789ABCDEF"] * 20)
    draw = identifier_sampler(shape, None, values, rng.randrange)
    drawn = [draw() for _ in range(200)]
    assert all(v.startswith("C2E3") for v in drawn)
    assert not set(drawn) & set(values)

  def test_identifier_sampler_preserves_uuid_v4_nibbles(self) -> None:
    # COL_064-class (2026-08-11 A_TABLE R1): RFC 4122 v4 pins position
    # 14 to '4' and position 19 to the variant class {8,9,a,b} — the
    # column-wide fill emitted arbitrary hex there (shape precision
    # 0.10). No uuid special-case: positional evidence carries it.
    import uuid

    rng = random.Random(21)
    values = [
        str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(200)
    ]
    draw = identifier_sampler(tuple("x" * 36), None, values, rng.randrange)
    for _ in range(100):
      v = draw()
      assert re.fullmatch(
          r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
          v,
      ), v


class TestMaskTableBeyondCap:
  """2026-08-20 R1 pair (wave 4): high-entropy identifier columns collapse
    through the 1024-mask cap.

    COL_064 (UUID v4, every mask ~unique): the kept 1024 all-count-1 masks
    were selected by the LEXICOGRAPHIC tie-break ('-' < '9' < 'a'), i.e. the
    most digit-front-loaded masks in the column (measured avg digit share
    0.74 vs population 0.63), then drawn uniformly — synthetic shapes
    plateaued at ~0.2% each while real v4 masks are ~unique. COL_001
    (24-hex): the cap dropped a long tail of real mass and renormalized the
    survivors (source top mask 0.5% → synthetic 1.2% = 0.5/recall 0.46).
    The fix: count ties break on a stable hash (no digit skew), and the
    dropped/singleton mass moves to a TAIL bucket synthesized per position
    from observed character frequencies (Good-Turing style: the singleton
    count estimates unseen-mask mass)."""

  def test_cap_tie_break_is_not_digit_skewed(self) -> None:
    rng = random.Random(17)
    values = list(
        dict.fromkeys("".join(
            rng.choice("0123456789abcdef")
            for _ in range(16))
                      for _ in range(2000)))
    table = build_mask_table(values, cap=200)
    assert table is not None
    masks = [m for _, m in table]

    def digit_share(ms: list[str]) -> float:
      joined = "".join(ms)
      return sum(1 for c in joined if c == "9") / len(joined)

    population = [_mask(v) for v in values]
    # Lexicographic tie-break kept ~0.95 digit share in this fixture;
    # a stable-hash tie-break tracks the population (~0.62).
    assert abs(digit_share(masks) - digit_share(population)) < 0.05

  def test_tail_draws_survive_the_cap_with_literals_intact(self) -> None:
    # > cap distinct masks, all singletons: draws must not be confined
    # to the kept table — and every draw keeps the literal 'ID-' prefix
    # and the column alphabet.
    rng = random.Random(23)
    values = list(
        dict.fromkeys(
            "ID-" + "".join(rng.choice("0123456789ABCDEF")
                            for _ in range(12))
            for _ in range(1600)))
    table = build_mask_table(values)
    assert table is not None
    kept_masks = {m for _, m in table}
    draw = identifier_sampler(tuple("x" * 15), None, values, rng.randrange)
    drawn = [draw() for _ in range(400)]
    assert all(v.startswith("ID-") and len(v) == 15 for v in drawn)
    assert all(set(v[3:]) <= set("0123456789ABCDEF") for v in drawn)
    assert not set(drawn) & set(values)  # novelty holds
    drawn_masks = {_mask(v) for v in drawn}
    # The tail bucket reaches masks the capped table cannot express.
    assert drawn_masks - kept_masks

  def test_uuid_v4_discipline_survives_beyond_cap(self) -> None:
    import uuid

    rng = random.Random(29)
    values = [
        str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(1600)
    ]
    draw = identifier_sampler(tuple("x" * 36), None, values, rng.randrange)
    for _ in range(200):
      v = draw()
      assert re.fullmatch(
          r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
          v,
      ), v

  def test_rigid_mask_columns_have_no_tail(self) -> None:
    # Repeated-mask columns (no singletons, nothing dropped): the tail
    # bucket must stay empty — masks remain exactly the observed set.
    values = [f"XY{i:06d}" for i in range(200)
             ] + [f"{i:04d}QRST" for i in range(100)]
    draw = identifier_sampler(
        tuple("x" * 8), None, values,
        random.Random(7).randrange)
    drawn = [draw() for _ in range(300)]
    assert {_mask(v) for v in drawn} <= {_mask(v) for v in values}


class TestMaskStableNoveltyRetry:

  def test_novelty_retry_does_not_migrate_mask_mass(self) -> None:
    # 2026-08-20 wave-4 (D2): on a collision the retry redrew the WHOLE
    # draw (new mask included), so mass migrated from saturated
    # low-cardinality mask families to high-cardinality ones (COL_026:
    # dominant 0.890 → 0.838, a rare variant inflated 42x). The retry
    # now redraws only the FILL within the chosen mask; a saturated
    # keyspace accepts the collision (its values are k-anonymous by
    # pigeonhole) instead of abandoning the mask.
    values = ["QRST" + f"{i % 10:04d}" for i in range(900)
             ] + [f"{i:08d}" for i in range(3000, 3100)]
    rng = random.Random(19)
    draw = identifier_sampler(tuple("x" * 8), None, values, rng.randrange)
    drawn = [draw() for _ in range(1000)]
    dominant = sum(1 for v in drawn if _mask(v) == "AAAA9999") / len(drawn)
    assert dominant > 0.85  # source row mass is 90%


class TestClassKindPreservation:
  """2026-08-20 B_TABLE R1, COL_038: `_CHAR_CLASSES` is ordered
    narrowest-first and `digits+ABCDEF` (16 chars) precedes uppercase (26),
    so a letter-only position whose chars happen to fall in A-F bound to
    the HEX class and started emitting digits 62.5% of the time ('CQZWD1
    DN…' → 'CQZWD1   6C…', 'TN  F4'). A class must never introduce a
    character KIND (digit/upper/lower) the position never showed."""

  def test_shape_mix_letter_positions_never_gain_digits(self) -> None:
    # Positions 0-1 are letters ⊆ A-F; position 2+ carries G-Z letters
    # so the column-wide alphabet is not hex-bound.
    values = [
        f"{p}{q}Z{i:05d}" for i, (p, q) in enumerate((a, b)
                                                     for a in "CDEF"
                                                     for b in "ABCD")
    ] * 3
    shapes = build_shape_mix(values)
    assert shapes is not None
    for _, shape in shapes:  # pylint: disable=not-an-iterable  # asserted non-None above
      assert not any(c.isdigit() for c in shape[0]), shape[0]
      assert not any(c.isdigit() for c in shape[1]), shape[1]

  def test_detect_identifier_shape_keeps_hex_for_mixed_positions(self) -> None:
    # A genuinely mixed digit+A-F position must still bind to the hex
    # class — kind preservation only blocks classes that ADD a kind.
    from sdfb_core.engines.text_shapes import detect_identifier_shape

    values = ["A1B2C3D4", "3C4D5E6F", "B2C3D4E5", "9F8E7D6C"]
    shape = detect_identifier_shape(values)
    assert shape is not None
    assert all(set(entry) <= set("0123456789ABCDEF") for entry in shape)


class TestIdentifierLikeLiteralSpaces:

  def test_literal_space_padding_is_identifier_like(self) -> None:
    # COL_038-class: rigid space-padded codes must expand per-row
    # instead of pinning at the pool cap.
    vals = [f"CQZWD{i%10}   CS{i:010d}B" for i in range(40)]
    assert shape_mix_is_identifier_like(build_shape_mix(vals))

  def test_prose_with_varying_masks_stays_excluded(self) -> None:
    # Word-diverse prose: singleton masks carry no class positions.
    assert not shape_mix_is_identifier_like(
        build_shape_mix(["SEG.DE CAMBIO 12", "ABONO CANON A 34"]))
