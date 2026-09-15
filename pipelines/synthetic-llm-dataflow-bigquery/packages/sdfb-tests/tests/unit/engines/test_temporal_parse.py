"""Lock-free temporal string parsing (2026-08-06 10M E2E remediation).

The run-3 postmortem traceback pinned 600-900 s generate stalls to
`datetime.strptime` inside `_temporal_obs_floats`: every call funnels
through CPython's global `_strptime._cache_lock`, and with more temporal
formats in flight than the 5-slot TimeRE cache holds, 16 DoFn threads
serialize on regex recompilation. `parse_temporal_string` must therefore
parse the profiler's known formats without ever reaching `strptime`,
while agreeing with it bit-for-bit on both accepts and rejects.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

from datetime import datetime

import pytest
from sdfb_core.engines import temporal_parse
from sdfb_core.engines.temporal_parse import parse_temporal_string
from sdfb_core.engines.text_shapes import _TEMPORAL_FORMATS

_CANONICAL = {
    "%Y-%m-%d": ["2026-08-06", "1999-01-01", "2026-2-7", "2026-12-31"],
    "%Y-%m-%dT%H:%M:%S": ["2026-08-06T21:05:58", "2026-8-6T1:2:3"],
    "%Y-%m-%d %H:%M:%S": ["2026-08-06 21:05:58", "1972-08-01 00:00:00"],
    "%Y-%m-%dT%H:%M:%S.%f": [
        "2026-08-06T21:05:58.123456", "2026-08-06T21:05:58.5"
    ],
    "%Y-%m-%d %H:%M:%S.%f": ["2026-08-06 21:05:58.000001"],
    "%Y-%m-%dT%H:%M:%SZ": ["2026-08-06T21:05:58Z"],
    "%Y-%m-%dT%H:%M:%S.%fZ": ["2026-08-06T21:05:58.42Z"],
}

_REJECTS = [
    ("%Y-%m-%d", "2026-13-05"),  # month out of range
    ("%Y-%m-%d", "2026-02-30"),  # day invalid for month
    ("%Y-%m-%d", "2026/08/06"),  # wrong separator
    ("%Y-%m-%d", "2026-08-06T10:00:00"),  # unconverted data remains
    ("%Y-%m-%d", "26-08-06"),  # two-digit year
    ("%Y-%m-%d", ""),  # empty
    ("%Y-%m-%dT%H:%M:%S", "2026-08-06 21:05:58"),  # T vs space
    ("%Y-%m-%dT%H:%M:%SZ", "2026-08-06T21:05:58"),  # missing Z
    ("%Y-%m-%dT%H:%M:%S.%f", "2026-08-06T21:05:58"),  # missing fraction
    ("%Y-%m-%d", "2026-08-061"),  # trailing digit
]


@pytest.mark.parametrize(
    ("fmt", "value"),
    [(fmt, v) for fmt, values in _CANONICAL.items() for v in values],
)
def test_agrees_with_strptime_on_accepts(fmt: str, value: str) -> None:
  assert parse_temporal_string(value, fmt) == datetime.strptime(value, fmt)


@pytest.mark.parametrize(("fmt", "value"), _REJECTS)
def test_agrees_with_strptime_on_rejects(fmt: str, value: str) -> None:
  with pytest.raises(ValueError):
    datetime.strptime(value, fmt)  # the contract being mirrored
  with pytest.raises(ValueError):
    parse_temporal_string(value, fmt)


def test_known_formats_never_reach_strptime(monkeypatch) -> None:
  """Every profiler format parses on the lock-free path.

    `_strptime_fallback` is the module's only route into
    `datetime.strptime`; poisoning it proves the fast path handled the
    call. This is the production property the 10M-run fix depends on.
    """

  def _boom(s: str, fmt: str) -> datetime:
    raise AssertionError(f"strptime fallback reached for {fmt!r}")

  monkeypatch.setattr(temporal_parse, "_strptime_fallback", _boom)
  for fmt in _TEMPORAL_FORMATS:
    sample = datetime(2026, 8, 6, 21, 5, 58, 123456).strftime(fmt)
    assert parse_temporal_string(sample, fmt) == datetime.strptime(sample, fmt)
  # Definite mismatches must also reject without the fallback.
  with pytest.raises(ValueError):
    parse_temporal_string("not-a-date", "%Y-%m-%d")


def test_unknown_format_falls_back_to_strptime() -> None:
  # %j (day-of-year) is outside the scanner's directive set.
  assert parse_temporal_string("2026-219", "%Y-%j") == datetime.strptime(
      "2026-219", "%Y-%j")


def test_profiler_hot_paths_are_lock_free(monkeypatch) -> None:
  """`temporal_string_to_float` and `detect_temporal_format` must ride
    the fast path — they are the two call sites the stalled bundles and
    the 32x setup storms actually execute."""

  def _boom(s: str, fmt: str) -> datetime:
    raise AssertionError("strptime fallback reached from a hot path")

  monkeypatch.setattr(temporal_parse, "_strptime_fallback", _boom)

  from sdfb_core.engines.b1_rag.profile import temporal_string_to_float
  from sdfb_core.engines.text_shapes import detect_temporal_format

  assert temporal_string_to_float(
      "2026-08-06", "%Y-%m-%d") == datetime.strptime(
          "2026-08-06",
          "%Y-%m-%d").replace(tzinfo=temporal_parse.UTC).timestamp()
  assert detect_temporal_format(["2026-08-06", "2025-01-01"]) == "%Y-%m-%d"
  assert detect_temporal_format(
      ["2026-08-06T21:05:58Z",
       "2025-01-01T00:00:00Z"]) == ("%Y-%m-%dT%H:%M:%SZ")
  assert detect_temporal_format(["plainly not a date", "also not"]) is None
