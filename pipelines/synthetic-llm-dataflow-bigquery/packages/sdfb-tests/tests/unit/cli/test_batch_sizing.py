"""batch_size must scale with num_rows (WS5 T3).

2026-07-26 1M E2E: batch_size defaulted to 16 regardless of num_rows, so a
1M-row job produced 62,500 elements and paid per-element Python overhead
62,500 times over instead of amortising it across vectorized draws.
"""

from __future__ import annotations

from sdfb_beam.cli.run_pipeline import DEFAULT_BATCH_SIZE, resolve_batch_size


def test_small_runs_keep_todays_batch_size():
  """Goldens and CI smoke runs must be byte-identical to before."""
  assert resolve_batch_size(DEFAULT_BATCH_SIZE, 1_000) == DEFAULT_BATCH_SIZE


def test_million_row_run_scales_up():
  got = resolve_batch_size(DEFAULT_BATCH_SIZE, 1_000_000)
  assert got >= 1_000
  assert 1_000_000 / got <= 2_000, "still too many elements"


def test_explicit_override_is_respected():
  """An operator who passes --batch_size gets exactly that."""
  assert resolve_batch_size(64, 1_000_000) == 64


def test_never_below_the_floor():
  assert resolve_batch_size(DEFAULT_BATCH_SIZE, 1) == DEFAULT_BATCH_SIZE


def test_zero_or_unknown_num_rows_is_safe():
  assert resolve_batch_size(DEFAULT_BATCH_SIZE, 0) == DEFAULT_BATCH_SIZE
  assert resolve_batch_size(DEFAULT_BATCH_SIZE, -5) == DEFAULT_BATCH_SIZE


def test_scaling_is_monotonic_in_num_rows():
  sizes = [
      resolve_batch_size(DEFAULT_BATCH_SIZE, n)
      for n in (10_000, 100_000, 1_000_000, 10_000_000)
  ]
  assert sizes == sorted(sizes)
