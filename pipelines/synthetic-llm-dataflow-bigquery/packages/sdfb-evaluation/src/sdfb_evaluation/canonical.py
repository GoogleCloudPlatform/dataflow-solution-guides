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
"""Canonical value encoding, row digests, uint64 column hashing, and the
linear/leave-one-out (LOO) row hashes built on top of them.

Two BigQuery client libraries can read the same cell as different Python
types (a `Decimal("10.50")` from one read path, a `Decimal("10.5")` from
another; a naive `datetime` from one, a UTC-aware one from another).
`canonical_value` maps every BigQuery-relevant Python type to one
type-stable representation, so `canonical_json`/`row_digest`/`hash64` give
the same digest regardless of which client produced the value.

`hash64` turns one `(column, value)` cell into a uint64 that is stable
under that same canonicalisation and gives NULL its own, column-independent
code (`NULL_CODE`) so that a NULL never collides with a real hashed value.
`hash_matrix` batches this over rows x columns; `multipliers`/`linear_hash`
combine the per-column hashes into one row-level uint64 (a fast approximate
row-equality test — see `row_digest` for the exact, digest-based version);
`loo_hashes` derives, from that same matrix, the row hash *without* each
column in turn, which is what a leave-one-out near-match search needs
(Ruling R2): it must work for any column set and any value type, numeric
included, which is why it is built on `hash64` rather than a numeric-only
shortcut.

All of the uint64 arithmetic below is meant to wrap silently at 2**64 (it
is a hash, not a checksum), hence `np.errstate(over="ignore")` around every
numpy integer op that could overflow.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

import numpy as np

# The golden-ratio-derived 64-bit constant used both as NULL's hash code
# (so every NULL collapses to the same value, independent of column) and as
# the per-column multiplier in `loo_hashes` (so column j's leave-one-out key
# depends on j). Reusing one constant for both roles is deliberate: neither
# use case needs a second unrelated magic number.
NULL_CODE = 0x9E3779B97F4A7C15

_SIG_DIGITS = 15
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIX_EPOCH_DATE = date(1970, 1, 1)
_ONE_MICROSECOND = timedelta(microseconds=1)
_MICROS_PER_SECOND = 1_000_000
_MICROS_PER_DAY = 86_400 * _MICROS_PER_SECOND


def canonical_value(  # noqa: PLR0911 — type dispatch, clearer flat than nested
    v: Any,) -> Any:
  """One BigQuery-relevant Python value, mapped to a type-stable form.

  `None`/`bool`/`int`/`str` pass through unchanged. `Decimal` becomes a
  trailing-zero-stripped fixed-point string of its own exact digits (no
  context rounding, so a full-width BIGNUMERIC survives), so `Decimal("10.5")`
  and `Decimal("10.50")` canonicalize identically; every zero (`0`, `-0`,
  `0E-10`, ...) canonicalizes to `"0"`. `float` is rounded to 15 significant
  digits (non-finite -> `None`; every zero, including `-0.0`, -> `0.0`).
  `datetime` becomes a UTC ISO-8601 string with microseconds; a naive
  `datetime` is treated as already UTC. `date`/`time` become their ISO
  string. `bytes`/`bytearray` become base64 text. A `Mapping` recurses with
  its keys sorted; a `list`/`tuple` recurses element-wise. Anything else
  becomes `str(v)`.
  """
  if v is None or isinstance(v, (bool, int, str)):
    return v
  if isinstance(v, Decimal):
    if v == 0:
      # Also folds every negative-zero spelling (`-0`, `-0.00`, ...) onto
      # the same "0", since they are numerically equal to positive zero.
      return "0"
    # No `.normalize()`: it applies the *ambient* decimal context precision
    # (28 digits by default) and would silently round a BIGNUMERIC-width
    # value. `format(v, "f")` renders the Decimal's own exact digits,
    # independent of context.
    text = format(v, "f")
    if "." in text:
      text = text.rstrip("0").rstrip(".")
    return text
  if isinstance(v, float):
    if not math.isfinite(v):
      return None
    if v == 0:
      return 0.0  # folds -0.0 onto 0.0 (numerically equal, same digest).
    return float(f"{v:.{_SIG_DIGITS}g}")
  if isinstance(v, datetime):
    aware = v if v.tzinfo is not None else v.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="microseconds")
  if isinstance(v, (date, time)):
    return v.isoformat()
  if isinstance(v, (bytes, bytearray)):
    return base64.b64encode(bytes(v)).decode("ascii")
  if isinstance(v, Mapping):
    return {
        key: canonical_value(value)
        for key, value in sorted(v.items(), key=lambda item: str(item[0]))
    }
  if isinstance(v, (list, tuple)):
    return [canonical_value(item) for item in v]
  return str(v)


def canonical_json(row: Mapping[str, Any], columns: Sequence[str]) -> str:
  """The canonical JSON of `row` restricted to `columns`, in `columns` order.

  Sorted keys and compact separators make this byte-stable for a given
  `(row, columns)` regardless of `row`'s own key order or the caller's
  `columns` order, which is what makes `row_digest` and `hash64` stable
  across read paths.
  """
  payload = {column: canonical_value(row.get(column)) for column in columns}
  return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def row_digest(row: Mapping[str, Any], columns: Sequence[str]) -> str:
  """The blake2b-128 hex digest of `row`'s canonical JSON over `columns`.

  Used for exact row-level dedup/flagging (`evaluation_row_flags`) and for
  the R-side reference digests: two rows with the same values in `columns`
  give the same digest no matter which BigQuery client produced them.
  """
  payload = canonical_json(row, columns).encode("utf-8")
  return hashlib.blake2b(payload, digest_size=16).hexdigest()


def hash64(column: str, value: Any) -> int:
  """A uint64 hash of one `(column, value)` cell.

  `None` always hashes to `NULL_CODE`, independent of `column`, so every
  NULL collapses to one value. Otherwise the hash covers both the column
  name and the canonical JSON of `value`, so the same value under different
  column names hashes differently.
  """
  if value is None:
    return NULL_CODE
  encoded = json.dumps(
      canonical_value(value), sort_keys=True, separators=(",", ":"))
  payload = f"{column}\x1f{encoded}"
  digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
  return int.from_bytes(digest, "big")


def hashed_label(code: int, *, key: bytes) -> str:
  """The D6 hashed label `h:<8 hex>` of a `hash64` code, KEYED (Ruling
  R64): `"h:" + blake2b(code as 8 big-endian bytes, key=key,
  digest_size=4)`.

  What a profile shows instead of a value the literal policy keeps out of
  it. A plain hash of a low-entropy value is not anonymisation — anyone can
  enumerate a small domain (a status list, ages 0..120) and match the
  hashes — so the label is a keyed hash and means nothing without `key`.
  The key has two modes (`beam.label_key.LabelKey` resolves it ON A
  WORKER, Ruling R68). It is never written to BigQuery, a log, a payload
  or the job graph; it lives in worker memory:

      operator    `--label_key_uri` (Secret Manager or GCS, the operator's
                  own secret): stable across runs, so labels line up run to
                  run and the operator can recompute a label to investigate
      ephemeral   a fresh `os.urandom(32)` per evaluation, made on a worker
                  and dropped with it: labels line up source and synthetic
                  within the run only

  The registry records only which mode ran (`label_key_mode`).

  Raises:
    ValueError: `key` is empty or not bytes (an unkeyed label is exactly
      the reversible hash this function exists to avoid).
  """
  if not isinstance(key, bytes) or not key:
    raise ValueError("hashed_label needs a non-empty bytes key (Ruling R64)")
  digest = hashlib.blake2b(
      (int(code) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big"),
      key=key,
      digest_size=4).hexdigest()
  return f"h:{digest}"


def hash_matrix(rows: Sequence[Mapping[str, Any]],
                columns: Sequence[str]) -> np.ndarray:
  """The `(len(rows), len(columns))` uint64 matrix of `hash64` per cell."""
  matrix = np.empty((len(rows), len(columns)), dtype=np.uint64)
  for i, row in enumerate(rows):
    for j, column in enumerate(columns):
      matrix[i, j] = np.uint64(hash64(column, row.get(column)))
  return matrix


def multipliers(columns: Sequence[str], salt: str) -> np.ndarray:
  """One odd uint64 multiplier per column, deterministic in `salt`.

  Odd, so that the multiplier is invertible mod 2**64 (every bit of the
  hashed value can still influence every bit of the product).
  """
  values = []
  for column in columns:
    payload = f"{salt}:{column}".encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    values.append(int.from_bytes(digest, "big") | 1)
  return np.array(values, dtype=np.uint64)


def linear_hash(h: np.ndarray, a: np.ndarray) -> np.ndarray:
  """`sum_j(a_j * h[:, j]) mod 2**64`, per row, as uint64.

  A fast, approximate row-equality test: two rows with the same values
  under `h`'s columns give the same linear hash (collisions are possible,
  as with any hash, but vanishingly unlikely at 64 bits).
  """
  with np.errstate(over="ignore"):
    products = h.astype(np.uint64) * a.astype(np.uint64)
    # `np.sum`'s stubs type a reduction as `ndarray | number` (they cannot see
    # that `axis=1` on a 2-D array always leaves an array); it never is here.
    return cast(np.ndarray, np.sum(products, axis=1, dtype=np.uint64))


def loo_hashes(h: np.ndarray, a: np.ndarray, total: np.ndarray) -> np.ndarray:
  """The `(len(h), h.shape[1])` matrix of `total` with each column dropped.

  Column `j`'s entry is `(total - a[j] * h[:, j])`, XORed with `j * GOLDEN`
  so that dropping different columns cannot coincidentally land on the same
  key. Two rows that agree on every column except `j` therefore share the
  same entry at `j` (their linear hash minus column j's own contribution is
  identical) and differ at every other column index — exactly what a
  leave-one-out near-match search needs.
  """
  with np.errstate(over="ignore"):
    contributions = h.astype(np.uint64) * a.astype(np.uint64)
    without_column = total[:, np.newaxis].astype(np.uint64) - contributions
    column_keys = np.arange(h.shape[1], dtype=np.uint64) * np.uint64(NULL_CODE)
    return without_column ^ column_keys[np.newaxis, :]


def numeric_value(v: Any) -> float | None:
  """`v` as a float for numeric statistics, or `None` if it has no numeric reading.

  `Decimal`/`int`/`float`/`bool` convert directly. Temporal values land on
  the planning scale (Ruling R54), the one unit of every grid, atom, mean
  and encoded value in the evaluator:

      TIMESTAMP, DATETIME   UNIX_MICROS (a naive `datetime` is UTC, as in
                            `canonical_value` and the planning SQL)
      DATE                  UNIX_MICROS of its UTC midnight
      TIME                  microseconds since midnight

  computed in exact integer microseconds, then made a float (exact within
  ±2**53 us, about the years 1685-2255). Everything else (`str`, `bytes`,
  nested structures) is not a scalar numeric value, so it returns `None`.
  """
  if isinstance(v, (bool, int, float, Decimal)):
    return float(v)
  if isinstance(v, datetime):
    aware = v if v.tzinfo is not None else v.replace(tzinfo=UTC)
    return float((aware - _UNIX_EPOCH) // _ONE_MICROSECOND)
  if isinstance(v, date):
    return float((v - _UNIX_EPOCH_DATE).days * _MICROS_PER_DAY)
  if isinstance(v, time):
    seconds = (v.hour * 60 + v.minute) * 60 + v.second
    return float(seconds * _MICROS_PER_SECOND + v.microsecond)
  return None


def json_safe(obj: Any) -> Any:
  """`obj` with every non-finite float replaced by `None`, recursively.

  BigQuery load jobs reject NaN/Infinity; this is the last step before a
  row is handed to `load_table_from_json`. Unlike `canonical_value`, it
  otherwise leaves types alone (no stringification, no key sorting) —
  it is a JSON-safety pass, not a canonicalisation.
  """
  if isinstance(obj, float):
    return obj if math.isfinite(obj) else None
  if isinstance(obj, Mapping):
    return {key: json_safe(value) for key, value in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [json_safe(item) for item in obj]
  return obj
