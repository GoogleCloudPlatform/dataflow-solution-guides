#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Tests for canonical encoding, digests, uint64 hashing and linear/LOO row hashes.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext

import numpy as np
import pytest

from sdfb_evaluation.canonical import (
    NULL_CODE,
    canonical_json,
    canonical_value,
    hash64,
    hash_matrix,
    hashed_label,
    json_safe,
    linear_hash,
    loo_hashes,
    multipliers,
    numeric_value,
    row_digest,
)

# ---------------------------------------------------------------------------
# canonical_value
# ---------------------------------------------------------------------------


def test_canonical_value_all_bq_types():
  assert canonical_value(None) is None
  assert canonical_value(True) is True
  assert canonical_value(False) is False
  assert canonical_value(42) == 42
  assert canonical_value("text") == "text"
  assert canonical_value(Decimal("10")) == "10"
  assert canonical_value(Decimal("1.50")) == "1.5"
  assert canonical_value(Decimal("0E-10")) == "0"
  assert canonical_value(1.5) == 1.5
  assert canonical_value(float("nan")) is None
  assert canonical_value(float("inf")) is None
  assert canonical_value(float("-inf")) is None
  assert canonical_value(date(2026, 1, 2)) == "2026-01-02"
  assert canonical_value(time(3, 4, 5)) == "03:04:05"
  aware = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
  assert canonical_value(aware) == "2026-01-02T03:04:05.123456+00:00"
  assert canonical_value(b"\x00\x01\xff") == base64.b64encode(
      b"\x00\x01\xff").decode("ascii")
  assert canonical_value({"b": 1, "a": 2}) == {"a": 2, "b": 1}
  assert canonical_value([Decimal("1.0"), None, "x"]) == ["1", None, "x"]


def test_canonical_value_decimal_strips_trailing_zeros():
  assert canonical_value(Decimal("10.50")) == canonical_value(Decimal("10.5"))
  assert canonical_value(Decimal("10.50")) == "10.5"
  assert canonical_value(Decimal("1E+2")) == "100"


def test_canonical_value_decimal_does_not_round_bignumeric_width_values():
  # NUMERIC(38, ...): 38 significant digits. `.normalize()` would apply the
  # ambient decimal context (28 digits by default) and silently round this.
  thirty_eight_digits = Decimal("12345678901234567890123456789012345678")
  assert canonical_value(thirty_eight_digits) == (
      "12345678901234567890123456789012345678")

  # BIGNUMERIC width: up to 76-77 significant digits.
  seventy_six_digits = Decimal("1" * 76)
  assert canonical_value(seventy_six_digits) == "1" * 76

  # A fractional BIGNUMERIC-width value keeps its exact digits too.
  whole_part = "9" * 40
  fraction_part = "8" * 36
  fractional = Decimal(f"{whole_part}.{fraction_part}")
  assert canonical_value(fractional) == f"{whole_part}.{fraction_part}"


def test_canonical_value_decimal_is_independent_of_ambient_context():
  value = Decimal("12345678901234567890123456789012345678")
  with localcontext() as ctx:
    ctx.prec = 5
    # Under the buggy `.normalize()` implementation this rounds to 5
    # significant digits; the fix must ignore the ambient context entirely.
    assert canonical_value(value) == ("12345678901234567890123456789012345678")


def test_canonical_value_decimal_negative_zero_folds_to_positive_zero():
  assert canonical_value(Decimal("-0.00")) == "0"
  assert canonical_value(Decimal("-0")) == "0"
  assert canonical_value(Decimal("-0")) == canonical_value(Decimal("0"))


def test_canonical_value_float_negative_zero_folds_to_positive_zero():
  assert canonical_value(-0.0) == 0.0
  assert canonical_value(-0.0) == canonical_value(0.0)


def test_canonical_value_float_rounds_to_15_significant_digits():
  value = 1.0 / 3.0
  assert canonical_value(value) == float(f"{value:.15g}")


def test_canonical_value_datetime_naive_is_treated_as_utc():
  naive = datetime(2026, 1, 2, 3, 4, 5, 123456)
  aware = naive.replace(tzinfo=UTC)
  assert canonical_value(naive) == canonical_value(aware)


def test_canonical_value_datetime_non_utc_converts_to_utc():
  tz = timezone(timedelta(hours=5))
  local = datetime(2026, 1, 2, 8, 4, 5, 123456, tzinfo=tz)
  assert canonical_value(local) == "2026-01-02T03:04:05.123456+00:00"


def test_canonical_value_mapping_sorts_keys_recursively():
  result = canonical_value({"b": {"z": 1, "y": 2}, "a": Decimal("2.50")})
  assert list(result.keys()) == ["a", "b"]
  assert list(result["b"].keys()) == ["y", "z"]
  assert result == {"a": "2.5", "b": {"y": 2, "z": 1}}


def test_canonical_value_other_type_becomes_str():

  class Thing:

    def __str__(self) -> str:
      return "thing"

  assert canonical_value(Thing()) == "thing"


# ---------------------------------------------------------------------------
# canonical_json / row_digest
# ---------------------------------------------------------------------------


def test_digest_is_type_stable_across_read_paths():
  row_a = {"amount": Decimal("10.50")}
  row_b = {"amount": Decimal("10.5")}
  assert canonical_json(row_a, ["amount"]) == canonical_json(row_b, ["amount"])
  assert row_digest(row_a, ["amount"]) == row_digest(row_b, ["amount"])
  assert hash64("amount", Decimal("10.50")) == hash64("amount", Decimal("10.5"))


def test_row_digest_column_subset_ignores_other_columns():
  row_1 = {"a": 1, "b": 2, "c": 3}
  row_2 = {"a": 1, "b": 2, "c": 999}
  assert row_digest(row_1, ["a", "b"]) == row_digest(row_2, ["a", "b"])
  assert row_digest(row_1,
                    ["a", "b", "c"]) != row_digest(row_2, ["a", "b", "c"])


def test_row_digest_is_independent_of_key_and_column_order():
  row_1 = {"a": 1, "b": 2}
  row_2 = {"b": 2, "a": 1}
  assert row_digest(row_1, ["a", "b"]) == row_digest(row_2, ["a", "b"])
  assert row_digest(row_1, ["a", "b"]) == row_digest(row_1, ["b", "a"])


def test_row_digest_is_16_bytes_hex():
  digest = row_digest({"a": 1}, ["a"])
  assert len(digest) == 32
  int(digest, 16)  # does not raise


def test_row_digest_treats_decimal_negative_zero_as_zero():
  assert row_digest({"a": Decimal("-0.00")},
                    ["a"]) == row_digest({"a": Decimal("0")}, ["a"])
  assert hash64("a", Decimal("-0.00")) == hash64("a", Decimal("0"))


def test_row_digest_treats_float_negative_zero_as_zero():
  assert row_digest({"a": -0.0}, ["a"]) == row_digest({"a": 0.0}, ["a"])
  assert hash64("a", -0.0) == hash64("a", 0.0)


# ---------------------------------------------------------------------------
# hash64 / hash_matrix / multipliers / linear_hash / loo_hashes
# ---------------------------------------------------------------------------


def test_hash64_null_and_column_separation():
  assert hash64("a", None) == hash64("b", None) == NULL_CODE
  assert hash64("a", "x") != hash64("b", "x")


def test_hash64_is_a_uint64():
  value = hash64("a", "x")
  assert 0 <= value < 2**64


def test_hash64_is_type_stable_for_equal_decimals():
  assert hash64("amount", Decimal("10.50")) == hash64("amount", Decimal("10.5"))


def test_multipliers_are_odd_and_deterministic():
  cols = ["a", "b", "c"]
  first = multipliers(cols, salt="s")
  second = multipliers(cols, salt="s")
  assert np.array_equal(first, second)
  assert first.dtype == np.uint64
  assert all(int(value) % 2 == 1 for value in first)


def test_multipliers_differ_by_salt():
  cols = ["a", "b"]
  assert not np.array_equal(
      multipliers(cols, salt="s1"), multipliers(cols, salt="s2"))


def test_linear_hash_matches_row_equality():
  cols = ["a", "b", "c"]
  rows = [{
      "a": 1,
      "b": "x",
      "c": None
  }, {
      "a": 1,
      "b": "x",
      "c": None
  }, {
      "a": 2,
      "b": "x",
      "c": None
  }]
  h = hash_matrix(rows, cols)
  a = multipliers(cols, salt="s")
  t = linear_hash(h, a)
  assert t[0] == t[1] and t[0] != t[2]


def test_loo_detects_single_field_difference():
  cols = ["a", "b", "c"]
  r = [{"a": 1, "b": "x", "c": "q"}]
  s = [{"a": 1, "b": "DIFF", "c": "q"}]
  a = multipliers(cols, salt="s")
  hr, hs = hash_matrix(r, cols), hash_matrix(s, cols)
  lr, ls = loo_hashes(hr, a,
                      linear_hash(hr, a)), loo_hashes(hs, a, linear_hash(hs, a))
  assert lr[0, 1] == ls[0, 1]  # dropping column b makes them equal
  assert lr[0, 0] != ls[0, 0] and lr[0, 2] != ls[0, 2]


def test_uint64_wraparound_is_silent():
  h = np.array([[np.iinfo(np.uint64).max, 3]], dtype=np.uint64)
  a = np.array([3, 5], dtype=np.uint64)
  assert linear_hash(h, a).dtype == np.uint64


def test_hash_matrix_shape_and_dtype():
  cols = ["a", "b"]
  rows = [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
  h = hash_matrix(rows, cols)
  assert h.shape == (2, 2)
  assert h.dtype == np.uint64
  assert h[1, 1] == NULL_CODE


def test_loo_hashes_shape_and_dtype():
  cols = ["a", "b", "c"]
  rows = [{"a": 1, "b": "x", "c": "q"}]
  a = multipliers(cols, salt="s")
  h = hash_matrix(rows, cols)
  loo = loo_hashes(h, a, linear_hash(h, a))
  assert loo.shape == (1, 3)
  assert loo.dtype == np.uint64


# ---------------------------------------------------------------------------
# numeric_value
# ---------------------------------------------------------------------------


def test_numeric_value_scalars():
  assert numeric_value(True) == 1.0
  assert numeric_value(False) == 0.0
  assert numeric_value(3) == 3.0
  assert numeric_value(2.5) == 2.5
  assert numeric_value(Decimal("4.25")) == 4.25


def test_numeric_value_temporal_is_unix_micros():
  # Ruling R54: the planning scale — UNIX_MICROS, DATE/DATETIME read as UTC.
  naive = datetime(1970, 1, 1)
  aware = datetime(1970, 1, 1, tzinfo=UTC)
  assert numeric_value(naive) == 0.0
  assert numeric_value(aware) == 0.0
  assert numeric_value(date(1970, 1, 1)) == 0.0
  assert numeric_value(date(1970, 1, 2)) == 86_400_000_000.0
  assert numeric_value(date(1969, 12, 31)) == -86_400_000_000.0
  assert numeric_value(datetime(1970, 1, 1, 0, 0, 1, 7)) == 1_000_007.0
  plus_one = timezone(timedelta(hours=1))
  assert numeric_value(datetime(1970, 1, 1, 1, tzinfo=plus_one)) == 0.0


def test_numeric_value_micros_are_exact_integers():
  moment = datetime(2026, 1, 2, 3, 4, 5, 123_457, tzinfo=UTC)
  expected = (moment -
              datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
  assert numeric_value(moment) == float(expected)
  assert int(numeric_value(moment) or 0) == expected


def test_numeric_value_time_is_micros_since_midnight():
  # BigQuery's TIME_DIFF(x, TIME '00:00:00', MICROSECOND), as planned.
  assert numeric_value(time(0, 0)) == 0.0
  assert numeric_value(time(1, 2, 3)) == 3_723_000_000.0
  assert numeric_value(time(23, 59, 59, 999_999)) == 86_399_999_999.0


def test_numeric_value_non_numeric_is_none():
  assert numeric_value("x") is None
  assert numeric_value(None) is None
  assert numeric_value(b"x") is None
  assert numeric_value([1, 2]) is None


# ---------------------------------------------------------------------------
# json_safe
# ---------------------------------------------------------------------------


def test_json_safe_replaces_non_finite_floats_with_none():
  assert json_safe(float("nan")) is None
  assert json_safe(float("inf")) is None
  assert json_safe(float("-inf")) is None
  assert json_safe(1.5) == 1.5


def test_json_safe_recurses_into_mappings_and_sequences():
  payload = {"a": float("nan"), "b": [1, float("inf"), 2], "c": "text"}
  assert json_safe(payload) == {"a": None, "b": [1, None, 2], "c": "text"}


def test_json_safe_leaves_other_types_unchanged():
  assert json_safe(None) is None
  assert json_safe("text") == "text"
  assert json_safe(7) == 7


def test_hashed_label_is_keyed_blake2b_of_the_code():
  code = hash64("status", "Complete")
  key = b"operator-secret"
  label = hashed_label(code, key=key)
  expected = hashlib.blake2b(
      code.to_bytes(8, "big"), key=key, digest_size=4).hexdigest()
  assert label == f"h:{expected}"
  assert len(label) == 10


def test_hashed_label_differs_by_key_and_resists_enumeration():
  statuses = ("Complete", "Shipped", "Processing", "Cancelled", "Returned")
  codes = [hash64("status", s) for s in statuses]
  first = [hashed_label(c, key=b"key-one") for c in codes]
  second = [hashed_label(c, key=b"key-two") for c in codes]
  assert all(a != b for a, b in zip(first, second, strict=True))
  # the old unkeyed label (the code's top 32 bits) matches nothing
  unkeyed = {f"h:{c >> 32:08x}" for c in codes}
  assert not unkeyed & set(first)
  assert "key-one" not in "".join(first)


def test_hashed_label_refuses_an_empty_or_text_key():
  for bad in (b"", "text"):
    with pytest.raises(ValueError, match="R64"):
      hashed_label(1, key=bad)  # type: ignore[arg-type]
