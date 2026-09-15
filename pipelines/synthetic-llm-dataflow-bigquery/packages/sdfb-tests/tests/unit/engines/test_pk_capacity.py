"""PK capacity under random draws + FK key-sample cap sizing (ADR 0035).

The 2026-09-09 three-table launch (job …-16364509521974163594): C_TABLE's
PK is (FK to B_TABLE, two small categoricals). With B_TABLE enabled the
FK member collapsed to the 100k side-input cap, the tuple capacity fell
to ~1.2M, and 8 789 594 of 10M rows diverted as pk.duplicate — 3h16m
after launch. These are the numbers preflight must do at second zero.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,missing-class-docstring,redefined-outer-name,reimported

from __future__ import annotations

import math

import pytest
from sdfb_core.engines.pk_capacity import (
    FK_KEY_SAMPLE_CEILING,
    FK_KEY_SAMPLE_FLOOR,
    FK_KEY_SAMPLE_MARGIN,
    expected_duplicate_share,
    fk_key_sample_cap,
    max_rows_under_share,
)


class TestExpectedDuplicateShare:
  """N uniform draws into K slots land K(1 - e^-N/K) distinct tuples;
    the rest are duplicates the uniqueness barrier diverts."""

  def test_reproduces_the_2026_09_09_collapse(self):
    # 10M draws into ~1.21M tuples -> 87.9% measured pk.duplicate.
    share = expected_duplicate_share(10_000_000, 1_210_000)
    assert share == pytest.approx(0.879, abs=0.002)

  def test_capacity_equal_to_rows_still_loses_a_third(self):
    # K == N is NOT enough: 1 - (1 - e^-1) = 36.8% duplicates.
    assert expected_duplicate_share(1_000, 1_000) == pytest.approx(
        1 - (1 - math.exp(-1)), abs=1e-9)

  def test_margin_of_ten_keeps_duplicates_under_five_percent(self):
    share = expected_duplicate_share(1_000_000, 10_000_000)
    assert 0.04 < share < 0.05

  def test_unbounded_capacity_means_no_duplicates(self):
    assert expected_duplicate_share(1_000_000, None) == 0.0

  def test_zero_rows_means_no_duplicates(self):
    assert expected_duplicate_share(0, 100) == 0.0

  def test_share_is_monotone_in_rows(self):
    shares = [
        expected_duplicate_share(n, 1_000) for n in (10, 100, 1_000, 10_000)
    ]
    assert shares == sorted(shares)


class TestFkKeySampleCap:
  """How many parent key tuples a child whose PK contains the FK must
    see: enough that (keys x other PK members) covers MARGIN x num_rows,
    clamped to the side-input floor (today's flat cap) and ceiling."""

  def test_constants_bracket_the_historic_cap(self):
    assert FK_KEY_SAMPLE_FLOOR == 100_000
    assert FK_KEY_SAMPLE_CEILING > FK_KEY_SAMPLE_FLOOR
    assert FK_KEY_SAMPLE_MARGIN >= 10

  def test_c_table_shape_at_one_million_rows(self):
    # other members contribute x12; 10 x 1M / 12 = 833 334 keys.
    assert fk_key_sample_cap(1_000_000, 12) == math.ceil(FK_KEY_SAMPLE_MARGIN *
                                                         1_000_000 / 12)

  def test_c_table_shape_at_ten_million_rows_hits_the_ceiling(self):
    assert fk_key_sample_cap(10_000_000, 12) == FK_KEY_SAMPLE_CEILING

  def test_small_runs_keep_the_floor(self):
    assert fk_key_sample_cap(1_000, 12) == FK_KEY_SAMPLE_FLOOR

  def test_unbounded_siblings_keep_the_floor(self):
    # A temporal/numeric PK member already covers the tuple.
    assert fk_key_sample_cap(10_000_000, None) == FK_KEY_SAMPLE_FLOOR

  def test_fk_alone_as_the_pk_needs_margin_times_rows(self):
    assert fk_key_sample_cap(50_000, 1) == FK_KEY_SAMPLE_MARGIN * 50_000


class TestMaxRowsUnderShare:

  def test_inverse_of_expected_share(self):
    capacity = 12_000_000
    rows = max_rows_under_share(capacity, 0.2)
    assert expected_duplicate_share(rows, capacity) <= 0.2
    assert expected_duplicate_share(rows + rows // 100, capacity) > 0.2

  def test_gate_of_one_never_limits(self):
    assert max_rows_under_share(1_000, 1.0) is None

  def test_unbounded_capacity_never_limits(self):
    assert max_rows_under_share(None, 0.2) is None


class TestCellWeightedShare:
  """The 2026-09-09_16_44_42 run (job …-563627394951127087): the sized
    1M-key sample reached the DAG (`key_tuples=1000000`) and C_TABLE
    still lost 56.5% of 10M rows — the uniform model said 32%. The two
    categorical PK members are SKEWED and jointly cover fewer cells than
    the product of their distinct counts, so collisions concentrate in
    the heavy cells. The model must take the joint cell weights."""

  def test_uniform_cells_equal_the_flat_formula(self):
    from sdfb_core.engines.pk_capacity import expected_duplicate_share_cells

    flat = expected_duplicate_share(10_000_000, 12_000_000)
    cells = expected_duplicate_share_cells(10_000_000, 1_000_000, [1.0] * 12)
    assert cells == pytest.approx(flat, abs=1e-9)

  def test_skew_raises_the_share_at_the_same_cell_count(self):
    from sdfb_core.engines.pk_capacity import expected_duplicate_share_cells

    skewed = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    share = expected_duplicate_share_cells(10_000_000, 1_000_000, skewed)
    assert share > expected_duplicate_share(10_000_000, 12_000_000)
    # The run-2 neighbourhood: a 12-cell skew of this shape sits
    # between 45% and 65% duplicates at 10M rows over 1M keys.
    assert 0.45 < share < 0.65

  def test_weights_need_not_be_normalised(self):
    from sdfb_core.engines.pk_capacity import expected_duplicate_share_cells

    a = expected_duplicate_share_cells(1_000, 100, [3, 1])
    b = expected_duplicate_share_cells(1_000, 100, [0.75, 0.25])
    assert a == pytest.approx(b)

  def test_effective_cells_is_the_inverse_simpson_index(self):
    from sdfb_core.engines.pk_capacity import effective_cells

    assert effective_cells([1, 1]) == pytest.approx(2.0)
    assert effective_cells([0.9, 0.1]) == pytest.approx(1 / 0.82)
    assert effective_cells([5]) == pytest.approx(1.0)

  def test_max_rows_honours_cells(self):
    from sdfb_core.engines.pk_capacity import (
        expected_duplicate_share_cells,
        max_rows_under_share,
    )

    skewed = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    rows = max_rows_under_share(1_000_000, 0.2, cell_weights=skewed)
    assert expected_duplicate_share_cells(rows, 1_000_000, skewed) <= 0.2
    assert expected_duplicate_share_cells(rows + rows // 100, 1_000_000,
                                          skewed) > 0.2
    # Skew lowers the gate-safe run vs the same count of uniform cells.
    assert rows < max_rows_under_share(1_000_000, 0.2, cell_weights=[1.0] * 12)


class TestAstronomicalCapacity:
  """2026-09-11 launch …-3742133137251240056: C_TABLE's PK member
    D_COL_001 became a pattern sampler (3.6e27 strings) once its parent
    was disabled, and P4 predicted 100 % duplicates at 10M rows over a
    2.2e29-tuple space — `1 - exp(-x)` cancels to 0.0 for x ≈ 1e-20.
    The share must be computed with `expm1`."""

  _D_COL_001 = 3_626_777_458_843_887_524_118_528  # ^(C2E[13]|7301)[0-9A-F]{20}$
  _UNIFORM = _D_COL_001 * 2 * 37

  def test_flat_share_over_an_astronomical_space_is_zero(self):
    assert expected_duplicate_share(10_000_000, self._UNIFORM) < 1e-12

  def test_cell_share_over_an_astronomical_space_is_zero(self):
    from sdfb_core.engines.pk_capacity import expected_duplicate_share_cells

    skewed = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    share = expected_duplicate_share_cells(10_000_000, self._D_COL_001, skewed)
    assert share < 1e-12

  def test_the_share_stays_exact_where_it_used_to_work(self):
    from sdfb_core.engines.pk_capacity import expected_duplicate_share_cells

    # The 2026-09-09_16_44 neighbourhood (56.5 % measured) is unchanged.
    skewed = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    assert 0.45 < expected_duplicate_share_cells(10_000_000, 1_000_000,
                                                 skewed) < 0.65
    assert expected_duplicate_share(
        10_000_000,
        12_000_000) == pytest.approx(1 - 1.2 * (1 - math.exp(-1 / 1.2)))

  def test_max_rows_under_the_gate_is_far_beyond_ten_million(self):
    skewed = [40, 20, 12, 8, 6, 4, 3, 2, 2, 1, 1, 1]
    assert max_rows_under_share(self._D_COL_001, 0.2, skewed) > 10**20
