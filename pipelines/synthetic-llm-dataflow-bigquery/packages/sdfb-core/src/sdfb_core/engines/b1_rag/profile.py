"""Column profiling for the distribution-estimator spine.

Profiles each schema column from the reference rows into one of five kinds
(see `ColumnKind`). Profiling is the cheap O(N_ref) pass that decides which
fidelity primitive samples the column at `generate_batch` time:

  - CONSTANT     → literal copy (``nunique() == 1``); never sent to the LLM.
  - NUMERIC      → in-observed-range empirical sampling (low-cardinality
                   numeric enums are demoted to CATEGORICAL).
  - CATEGORICAL  → empirical-frequency sampling from the observed value set.
  - FREE_TEXT    → bounded LLM-generated pool, sampled with replacement.
  - TEMPORAL     → novel values sampled within the observed time range
                   (high-cardinality DATE/DATETIME/TIME/TIMESTAMP; verbatim
                   copies are linkage quasi-identifiers).

Pure-Python (stdlib only) so it lives in `sdfb-core`. Numeric stats are
computed without NumPy here; the vectorized *sampling* (in the engine) is
where NumPy is used and deferred-imported.

REF: spec §2 fidelity primitives; ADR 0013 distribution-estimator spine.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING

from sdfb_core.contracts.prompt_constraint import (
    PromptConstraint,
    parse_prompt_constraint,
    render_prompt_clause,
)
from sdfb_core.engines.temporal_parse import parse_temporal_string
from sdfb_core.engines.text_shapes import (
    RelaxedShapes,
    build_shape_mix,
    detect_identifier_shape,
    detect_temporal_format,
)
from sdfb_core.observability import log_milestone

if TYPE_CHECKING:  # pragma: no cover - typing only
  from sdfb_core.contracts import FieldSchema, TableSchema

# BQ types that are inherently numeric (sampled by range, not by category).
_NUMERIC_BQ_TYPES = frozenset(
    {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"})
# BQ types whose Python value is a string we might treat as free text.
_STRINGY_BQ_TYPES = frozenset({"STRING", "JSON", "GEOGRAPHY", "BYTES"})

# A string column with more unique non-null values than this *fraction* of
# its non-null count, AND a mean rendered length above the threshold, is
# treated as FREE_TEXT rather than CATEGORICAL. Tuned so low-cardinality
# enums (country, tier) stay categorical while names / descriptions /
# free-form notes go to the LLM pool.
_FREE_TEXT_UNIQUE_RATIO = 0.9
_FREE_TEXT_MIN_MEAN_LEN = 20
# Above this absolute distinct count a string column is high-cardinality and
# treated as free text even if short (e.g. emails, ids-as-strings).
_FREE_TEXT_MAX_CATEGORIES = 50
# Head-value (dominant literal) capture for FREE_TEXT columns: a value must
# hold at least this share of the substantive (non-null, non-empty) rows AND
# at least this many observations before it is re-emitted verbatim — the
# count floor is the k-anonymity guard (a value shared by many rows is an
# enum member, not an identifier).
_FREE_TEXT_HEAD_MIN_SHARE = 0.05
_FREE_TEXT_HEAD_MIN_COUNT = 10
_FREE_TEXT_HEAD_MAX = 8
# Shape-mix cap for the engines (the stats module keeps 8 for display):
# 2026-08-11 B_TABLE R1, COL_019 — dozens of source shapes, the top-8 mix
# left ~62% of row mass uncovered (shape recall 0.24).
_SHAPE_MIX_TOP_K = 32
# BQ temporal types: high-cardinality columns get range-sampled novel values
# (verbatim copies of real event timestamps are linkage quasi-identifiers —
# 2026-07-15 E2E report: COL_052 landed 935 real microsecond timestamps).
_TEMPORAL_BQ_TYPES = frozenset({"DATE", "DATETIME", "TIME", "TIMESTAMP"})
# At or below this distinct count a temporal / numeric column is an enum in
# disguise (load dates, status codes): sample the observed values instead of
# inventing in-between ones (COL_002: 2 source codes -> 5 landing codes).
_TEMPORAL_MAX_CATEGORIES = 20
_NUMERIC_MAX_CATEGORIES = 20
# Sentinel calendar years excluded from a TEMPORAL column's jitter range and
# re-injected at their observed frequency (0001-01-01 null-substitute,
# 9999-12-31 open-end). Leaving them in [min, max] landed b1 dates like
# "72-08-01" in the 2026-07-23 E2E run. Mirrors b2_library/fidelity.py.
_TEMPORAL_SENTINEL_YEARS = frozenset({1, 9999})
# Interim temporal-age policy (2026-07-23): the jitter floor is clamped to
# now - 10y; fully-historical columns keep their observed range. Subject to
# per-column DDL-JSON functional descriptions later (audit fields, FK
# referential integrity, M2+) — see b2_library/fidelity.py for the full
# rationale.
_MAX_TEMPORAL_AGE_YEARS = 10


class ColumnKind(StrEnum):
  CONSTANT = "constant"
  NUMERIC = "numeric"
  CATEGORICAL = "categorical"
  FREE_TEXT = "free_text"
  TEMPORAL = "temporal"


@dataclass(frozen=True)
class ColumnProfile:
  """Per-column statistics derived from the reference sample.

    Engine-local (kept under `b1_rag/` per the spec's "fidelity helpers
    local during parallel dev" guidance).
    """

  name: str
  bq_type: str
  kind: ColumnKind
  nullable: bool
  null_fraction: float
  # Fraction of ALL sampled rows whose value is a string that is empty
  # after .strip() (2026-08-04 crosscheck: 11/13 columns were mostly
  # empty in source, ~0% empty synthetic). Empties are EXCLUDED from
  # pools/examples and re-emitted at this rate — except on CATEGORICAL
  # routes, where the frequency table carries them and this stays 0.0.
  empty_fraction: float = 0.0
  # CONSTANT
  constant_value: object | None = None
  # NUMERIC — observed bounds + whether values are integral.
  numeric_min: float | None = None
  numeric_max: float | None = None
  is_integral: bool = False
  is_decimal: bool = False
  # NUMERIC/BIGNUMERIC decimal scale from the DDL (places to round to).
  decimal_scale: int | None = None
  # CATEGORICAL — value → count (insertion order = first-seen, for determinism).
  categories: dict[object, int] = field(default_factory=dict)
  # FREE_TEXT — a deduped sample of observed values (the retrieval seed pool).
  text_examples: tuple[str, ...] = ()
  # FREE_TEXT — nearly every reference value is distinct (unique ratio >=
  # the free-text threshold): identifier-like or personal prose. Folding
  # observed values into the generated pool would memorize them.
  is_unique_valued: bool = False
  # FREE_TEXT — fixed per-position character template (engines/text_shapes):
  # identifier-shaped columns generate format-preserving values per row and
  # never touch the LLM (2026-07-17 E2E: the pool route collapses them).
  identifier_shape: tuple[str, ...] | None = None
  # FREE_TEXT — observed exact-shape mix (weight, template) for the
  # shape-preserving expander + pattern guidance (2026-08-05 spec C2/C3).
  shape_mix: RelaxedShapes | None = None
  # FREE_TEXT — dominant literal values (value, share-of-substantive-rows).
  # An enum-like literal hiding in a free-text column (2026-08-07 A_TABLE
  # R1: `ZZ3000` at 77% share) can never come out of the pool — ADR 0023
  # rightly rejects every source value — so the head mass is re-emitted
  # at its observed frequency, like temporal sentinels. K-anonymous by
  # construction: only values above the share/count floors qualify.
  head_values: tuple[tuple[str, float], ...] = ()
  # FREE_TEXT — per-column prompt steering parsed from the column's DDL
  # description JSON (spec C5); attached to pool prompts when
  # ctx.prompt_constraints is on. Empty = no constraint.
  llm_prompt_constraint: str = ""
  # FREE_TEXT — structured-constraint extras (ADR 0024): user regex for
  # guided decoding, whether the constraint pins length (suppresses the
  # derived length hint), fictitious examples (joined to the pool
  # rejection set so a verbatim echo never lands as data).
  constraint_pattern: str = ""
  constraint_sets_length: bool = False
  constraint_examples: tuple[str, ...] = ()
  # FREE_TEXT — Tier-P/B routing inputs (ADR 0028): the clause's literal
  # prefix, its pinned fixed width (None when unpinned or a band), and
  # the structured prefix-family shares for weighted pattern sampling.
  constraint_prefix: str = ""
  constraint_length: int | None = None
  constraint_families: tuple[tuple[str, float], ...] = ()
  # TEMPORAL — strftime format when the column is a date-shaped STRING;
  # range-sampled floats render back to strings in the observed format.
  temporal_format: str | None = None
  # TEMPORAL — sentinel values (year 1 / 9999) excluded from [min, max] and
  # re-injected at their observed fraction: ((value, fraction), ...).
  temporal_sentinels: tuple[tuple[object, float], ...] = ()
  # All non-null observed values, original order — used for sampling fallbacks.
  observed_values: tuple[object, ...] = ()


def profile_columns(table_schema: TableSchema,
                    reference_rows: list[dict]) -> dict[str, ColumnProfile]:
  """Profile every top-level column. Returns name → `ColumnProfile`.

    STRUCT/RECORD and REPEATED columns are profiled as CATEGORICAL over
    their JSON-rendered values (M1 is single-table, nested handling stays
    coarse; deep nested synthesis is M2+).
    """
  profiles: dict[str, ColumnProfile] = {}
  for col in table_schema.columns:
    values = [r.get(col.name, None) for r in reference_rows]
    profiles[col.name] = _profile_one(col, values)
  return profiles


def _profile_one(col: FieldSchema, values: list[object]) -> ColumnProfile:
  n = len(values)
  non_null = [v for v in values if v is not None]
  null_fraction = (n - len(non_null)) / n if n else 0.0
  nullable = col.is_nullable
  # Trimmed-empty strings are sparsity, not content: they leave the value
  # stream here and re-enter at sampling time via `empty_fraction`.
  empties = sum(1 for v in non_null if isinstance(v, str) and not v.strip())
  empty_fraction = empties / n if n else 0.0
  substantive = [
      v for v in non_null if not (isinstance(v, str) and not v.strip())
  ]

  distinct = _ordered_distinct(non_null)

  # `route: "llm"` (ADR 0024): a STRING column overrides its typed
  # classification — constant/categorical/temporal/identifier — into the
  # LLM free-text route, carrying its rendered constraint. Non-STRING
  # types keep their route: numeric fidelity is owned by the B.2
  # inverse-CDF acceptance path (ADR 0022), so LLM-generating them would
  # regress a documented ceiling.
  pc = parse_prompt_constraint(col.description, column=col.name)
  force_llm = pc is not None and pc.route == "llm"
  if force_llm and (col.bq_type not in _STRINGY_BQ_TYPES or col.is_struct or
                    col.is_repeated):
    log_milestone(
        "prompt_constraint_route_unsupported",
        level=logging.WARNING,
        column=col.name,
        bq_type=col.bq_type,
    )
    force_llm = False

  # CONSTANT: exactly one distinct non-null value and no nulls observed.
  if len(distinct) == 1 and not (n - len(non_null)) and not force_llm:
    return ColumnProfile(
        name=col.name,
        bq_type=col.bq_type,
        kind=ColumnKind.CONSTANT,
        nullable=nullable,
        null_fraction=0.0,
        constant_value=distinct[0],
        observed_values=tuple(non_null),
    )

  if col.bq_type in _NUMERIC_BQ_TYPES:
    return _profile_numeric(col, non_null, nullable, null_fraction)

  if col.is_struct or col.is_repeated:
    return _profile_categorical(
        col, non_null, nullable, null_fraction, render=_json_render)

  if col.bq_type in _STRINGY_BQ_TYPES:
    return _profile_string(
        col,
        substantive,
        nullable,
        null_fraction,
        empty_fraction=empty_fraction,
        with_empties=non_null,
        pc=pc,
        force_llm=force_llm,
    )

  if col.bq_type in _TEMPORAL_BQ_TYPES:
    return _profile_temporal(col, non_null, nullable, null_fraction)

  # BOOL (and anything unrecognized) → categorical over observed values.
  return _profile_categorical(col, non_null, nullable, null_fraction)


def _profile_numeric(
    col: FieldSchema,
    non_null: list[object],
    nullable: bool,
    null_fraction: float,
) -> ColumnProfile:
  numbers: list[float] = []
  integral = True
  is_decimal = col.bq_type in {"NUMERIC", "BIGNUMERIC"}
  for v in non_null:
    f = _to_float(v)
    if f is None:
      continue
    numbers.append(f)
    if f != int(f):
      integral = False
  if not numbers:
    # No parseable numbers (all null / unparseable) — treat as categorical.
    return _profile_categorical(col, non_null, nullable, null_fraction)
  # A numeric column whose observed support is a small, repeating value set
  # is an enum in disguise (status codes, flags): interpolating over its
  # range invents category codes that do not exist in the source
  # (2026-07-15 E2E: COL_002 had 2 source values, landed 5). `distinct <
  # len` keeps all-distinct columns (PKs, tiny fixtures) numeric.
  n_distinct = len(set(numbers))
  if n_distinct <= _NUMERIC_MAX_CATEGORIES and n_distinct < len(numbers):
    return _profile_categorical(col, non_null, nullable, null_fraction)
  # Use the DDL scale when present; default to 2 places for NUMERIC currency.
  decimal_scale = col.scale if (is_decimal and col.scale is not None) else (
      2 if is_decimal else None)
  return ColumnProfile(
      name=col.name,
      bq_type=col.bq_type,
      kind=ColumnKind.NUMERIC,
      nullable=nullable,
      null_fraction=null_fraction,
      numeric_min=min(numbers),
      numeric_max=max(numbers),
      is_integral=integral and col.bq_type in {"INTEGER", "INT64"},
      is_decimal=is_decimal,
      decimal_scale=decimal_scale,
      observed_values=tuple(non_null),
  )


def _profile_temporal(
    col: FieldSchema,
    non_null: list[object],
    nullable: bool,
    null_fraction: float,
) -> ColumnProfile:
  """DATE/DATETIME/TIME/TIMESTAMP → TEMPORAL (range-sampled novel values)
    when high-cardinality, CATEGORICAL when the column is an enum in disguise
    (load dates, partition stamps).

    Verbatim copies of high-cardinality timestamps are linkage
    quasi-identifiers (2026-07-15 E2E: COL_052 landed 935 real microsecond
    event timestamps); range sampling keeps the marginal support without
    reproducing real instants.
    """
  distinct = _ordered_distinct(non_null)
  pairs = [(v, f, v.year if isinstance(v, date) else None)
           for v, f in ((v, temporal_to_float(v)) for v in non_null)
           if f is not None]
  if len(distinct) <= _TEMPORAL_MAX_CATEGORIES or not pairs:
    return _profile_categorical(col, non_null, nullable, null_fraction)
  lo, hi, sentinels = _temporal_range_and_sentinels(pairs, col.name)
  return ColumnProfile(
      name=col.name,
      bq_type=col.bq_type,
      kind=ColumnKind.TEMPORAL,
      nullable=nullable,
      null_fraction=null_fraction,
      numeric_min=lo,
      numeric_max=hi,
      temporal_sentinels=sentinels,
      observed_values=tuple(non_null),
  )


def _temporal_range_and_sentinels(
    pairs: list[tuple[object, float, int | None]],
    column_name: str,
) -> tuple[float, float, tuple[tuple[object, float], ...]]:
  """(lo, hi, sentinels) for a TEMPORAL column's jitter range.

    ``pairs`` are ``(value, float_axis, year)`` — year is None for TIME.
    Sentinel-year values leave the range and come back as ``(value,
    fraction)`` pairs the sampler re-injects; an all-sentinel column keeps
    its observed range (nothing to trim toward). The floor is then clamped
    to now - `_MAX_TEMPORAL_AGE_YEARS` unless the whole range is older
    (fully-historical columns keep old truth; per-column DDL-JSON
    descriptions will govern those later). TIME columns are untouched by
    the clamp: their axis is seconds-of-day, far below any epoch floor.
    """
  regular = [(v, f) for v, f, y in pairs if y not in _TEMPORAL_SENTINEL_YEARS]
  if regular and len(regular) < len(pairs):
    counts = Counter(v for v, _, y in pairs if y in _TEMPORAL_SENTINEL_YEARS)
    total = len(pairs)
    sentinels = tuple((v, c / total) for v, c in counts.items())
    floats = [f for _, f in regular]
  else:
    sentinels = ()
    floats = [f for _, f, _ in pairs]
  lo, hi = min(floats), max(floats)
  floor = (datetime.now(UTC) -
           timedelta(days=round(_MAX_TEMPORAL_AGE_YEARS * 365.25))).timestamp()
  if lo < floor <= hi:
    lo = floor
    log_milestone(
        "temporal_range_clamped",
        column=column_name,
        max_age_years=_MAX_TEMPORAL_AGE_YEARS,
    )
  return lo, hi, sentinels


def temporal_to_float(v: object) -> float | None:
  """Temporal value → float on a per-type axis (epoch seconds; TIME uses
    seconds-since-midnight). Naive datetimes are pinned to UTC so the
    round-trip through `temporal_from_float` is exact. Non-temporal values
    (e.g. ISO strings from odd sources) return None — the caller falls back
    to categorical profiling."""
  if isinstance(v, datetime):  # before `date`: datetime IS a date subclass
    dt = v if v.tzinfo is not None else v.replace(tzinfo=UTC)
    return dt.timestamp()
  if isinstance(v, date):
    return datetime(v.year, v.month, v.day, tzinfo=UTC).timestamp()
  if isinstance(v, time):
    return v.hour * 3600 + v.minute * 60 + v.second + v.microsecond / 1e6
  return None


def temporal_string_to_float(s: str, fmt: str) -> float:
  """Date-shaped STRING value → epoch seconds (naive parses pin to UTC,
    matching `temporal_to_float`). Lock-free parse: the 2026-08-06 10M run
    stalled generate bundles up to 908 s on `strptime`'s global cache lock."""
  return parse_temporal_string(s, fmt).replace(tzinfo=UTC).timestamp()


def temporal_string_from_float(v: float, fmt: str) -> str:
  """Inverse of `temporal_string_to_float`: render an epoch-seconds float
    back into the column's observed string format."""
  return datetime.fromtimestamp(v, tz=UTC).strftime(fmt)


def temporal_from_float(v: float, bq_type: str) -> object:
  """Inverse of `temporal_to_float`, coercing to the BQ type's Python shape
    (TIMESTAMP → aware datetime, DATETIME → naive, DATE → date, TIME → time)."""
  if bq_type == "TIMESTAMP":
    return datetime.fromtimestamp(v, tz=UTC)
  if bq_type == "DATETIME":
    return datetime.fromtimestamp(v, tz=UTC).replace(tzinfo=None)
  if bq_type == "DATE":
    return datetime.fromtimestamp(v, tz=UTC).date()
  # TIME — seconds since midnight, clamped to one day.
  total = max(0.0, min(float(v), 86_399.999999))
  seconds = int(total)
  micros = min(round((total - seconds) * 1e6), 999_999)
  return time(seconds // 3600, seconds % 3600 // 60, seconds % 60, micros)


def _profile_string(
    col: FieldSchema,
    non_null: list[object],
    nullable: bool,
    null_fraction: float,
    empty_fraction: float = 0.0,
    with_empties: list[object] | None = None,
    pc: PromptConstraint | None = None,
    force_llm: bool = False,
) -> ColumnProfile:
  """`non_null` arrives with trimmed-empty strings already removed;
    `with_empties` keeps them for the CATEGORICAL fallthrough, where the
    frequency table (not `empty_fraction`) owns the parity. ``force_llm``
    (`route: "llm"`, ADR 0024) skips the typed shape routes and the
    categorical fallthrough — the column generates via the LLM pool with
    its rendered constraint."""
  if pc is None:
    pc = parse_prompt_constraint(col.description, column=col.name)
  constraint = render_prompt_clause(pc) if pc is not None else ""
  c_pattern = pc.pattern if pc is not None else ""
  c_sets_length = pc is not None and pc.length is not None
  c_examples = pc.examples if pc is not None else ()
  c_prefix = pc.prefix if pc is not None else ""
  c_length = (
      pc.length[0] if pc is not None and pc.length is not None and
      pc.length[0] == pc.length[1] else None)
  c_families = pc.families if pc is not None else ()
  strings = [str(v) for v in non_null]
  distinct = _ordered_distinct(strings)
  n = len(strings)
  unique_ratio = (len(distinct) / n) if n else 0.0
  mean_len = (sum(len(s) for s in strings) / n) if n else 0.0

  is_free_text = force_llm or (len(distinct) > _FREE_TEXT_MAX_CATEGORIES or
                               (unique_ratio >= _FREE_TEXT_UNIQUE_RATIO and
                                mean_len >= _FREE_TEXT_MIN_MEAN_LEN))
  head_values = _free_text_head_values(strings) if is_free_text else ()
  # Shape-mix input is ROWS minus head values (2026-08-11 R1 pair): the
  # distinct-set input weighted every mask by its distinct-value count and
  # inverted row-mass marginals (A_TABLE COL_054: `BATCH` = 81% of rows
  # but ONE distinct value; B_TABLE COL_024/COL_015 inverted the same
  # way). Heads leave the input because `_with_head_values` re-emits them
  # at their exact share — keeping them would double-count their mass.
  head_set = {v for v, _ in head_values}
  mix_rows = [s for s in strings if s not in head_set] or strings
  if is_free_text:
    # Shaped strings leave the LLM route before it can fail on them
    # (2026-07-17 E2E): date-shaped columns range-sample as TEMPORAL,
    # fixed-alphabet identifiers generate from a per-position template.
    # `force_llm` skips both shape routes by explicit user intent.
    fmt = None if force_llm else detect_temporal_format(distinct)
    if fmt is not None:
      pairs: list[tuple[object, float, int | None]] = []
      for s in strings:
        parsed = parse_temporal_string(s, fmt)
        pairs.append((s, parsed.replace(tzinfo=UTC).timestamp(), parsed.year))
      lo, hi, sentinels = _temporal_range_and_sentinels(pairs, col.name)
      return ColumnProfile(
          name=col.name,
          bq_type=col.bq_type,
          kind=ColumnKind.TEMPORAL,
          nullable=nullable,
          null_fraction=null_fraction,
          empty_fraction=empty_fraction,
          numeric_min=lo,
          numeric_max=hi,
          temporal_format=fmt,
          temporal_sentinels=sentinels,
          observed_values=tuple(strings),
      )
    shape = None if force_llm else detect_identifier_shape(distinct)
    if shape is not None:
      # text_examples stays empty on purpose: there is no LLM call to
      # seed and no fallback that may ever fold reference identifiers.
      return ColumnProfile(
          name=col.name,
          bq_type=col.bq_type,
          kind=ColumnKind.FREE_TEXT,
          nullable=nullable,
          null_fraction=null_fraction,
          empty_fraction=empty_fraction,
          identifier_shape=shape,
          is_unique_valued=unique_ratio >= _FREE_TEXT_UNIQUE_RATIO,
          observed_values=tuple(strings),
          # The mask MIX, not just the collapsed template: variant
          # masks merge into digit+upper classes and lose fixed
          # prefixes (2026-08-07 A_TABLE R1: COL_001-class columns
          # reproduced 0% of source masks).
          shape_mix=build_shape_mix(mix_rows, top_k=_SHAPE_MIX_TOP_K),
          head_values=head_values,
          llm_prompt_constraint=constraint,
          constraint_pattern=c_pattern,
          constraint_sets_length=c_sets_length,
          constraint_examples=c_examples,
          constraint_prefix=c_prefix,
          constraint_length=c_length,
          constraint_families=c_families,
      )
    # Cap the seed pool — exemplars condition the LLM, they aren't the bulk.
    examples = tuple(distinct[:64])
    return ColumnProfile(
        name=col.name,
        bq_type=col.bq_type,
        kind=ColumnKind.FREE_TEXT,
        nullable=nullable,
        null_fraction=null_fraction,
        empty_fraction=empty_fraction,
        text_examples=examples,
        # Nearly-all-distinct reference values (ids, unique prose): the
        # engine must not fold observed values into the generated pool.
        is_unique_valued=unique_ratio >= _FREE_TEXT_UNIQUE_RATIO,
        observed_values=tuple(strings),
        shape_mix=build_shape_mix(mix_rows, top_k=_SHAPE_MIX_TOP_K),
        head_values=head_values,
        llm_prompt_constraint=constraint,
        constraint_pattern=c_pattern,
        constraint_sets_length=c_sets_length,
        constraint_examples=c_examples,
        constraint_prefix=c_prefix,
        constraint_length=c_length,
        constraint_families=c_families,
    )
  return _profile_categorical(
      col,
      with_empties if with_empties is not None else non_null,
      nullable,
      null_fraction,
  )


def _free_text_head_values(
    strings: list[str],) -> tuple[tuple[str, float], ...]:
  """Dominant literals of a FREE_TEXT column, with substantive-row shares.

    Share floor keeps this to genuine enum-like heads; the count floor is
    the k-anonymity guard. Ordered heaviest-first; bounded so a flat
    distribution can never smuggle the whole sample back in.
    """
  n = len(strings)
  if n == 0:
    return ()
  counts = Counter(strings)
  heads = [(value, count / n)
           for value, count in counts.most_common(_FREE_TEXT_HEAD_MAX)
           if count >= _FREE_TEXT_HEAD_MIN_COUNT and count /
           n >= _FREE_TEXT_HEAD_MIN_SHARE]
  return tuple(heads)


def _profile_categorical(
    col: FieldSchema,
    non_null: list[object],
    nullable: bool,
    null_fraction: float,
    render=None,
) -> ColumnProfile:
  counter: Counter = Counter()
  # Preserve first-seen order for determinism (Counter keeps insertion order).
  keyed: list[object] = []
  for v in non_null:
    key = render(v) if render is not None else v
    keyed.append(key)
    counter[_hashable(key)] += 1
  # Rebuild an ordered dict keyed by the original (hashable) values.
  categories: dict[object, int] = {}
  for key in keyed:
    h = _hashable(key)
    if h not in categories:
      categories[h] = counter[h]
  return ColumnProfile(
      name=col.name,
      bq_type=col.bq_type,
      kind=ColumnKind.CATEGORICAL,
      nullable=nullable,
      null_fraction=null_fraction,
      categories=categories,
      observed_values=tuple(_hashable(k) for k in keyed),
  )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _ordered_distinct(values: list) -> list:
  seen: dict = {}
  for v in values:
    h = _hashable(v)
    if h not in seen:
      seen[h] = None
  return list(seen.keys())


def _hashable(v: object) -> object:
  """Coerce unhashable JSON values (lists/dicts) to a stable string key."""
  if isinstance(v, (list, dict)):
    return _json_render(v)
  return v


def _json_render(v: object) -> str:
  import json

  return json.dumps(v, sort_keys=True, default=str)


def _to_float(v: object) -> float | None:
  if isinstance(v, bool):
    return None
  if isinstance(v, (int, float)):
    return float(v)
  if isinstance(v, Decimal):
    return float(v)
  if isinstance(v, str):
    try:
      return float(Decimal(v))
    except (InvalidOperation, ValueError):
      return None
  return None


__all__ = ["ColumnKind", "ColumnProfile", "profile_columns"]
