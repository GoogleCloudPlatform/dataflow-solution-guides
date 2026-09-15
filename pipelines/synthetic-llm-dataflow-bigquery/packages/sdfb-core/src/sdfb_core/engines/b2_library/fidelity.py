"""Column profiling + post-hoc fidelity enforcement, LOCAL to B.2.

This is the FASTGEN-spine fidelity layer (ADR 0013 / design §2, §4): we
classify each column once over ``reference_rows`` and then clamp sampled
values back into the observed support. ``sdgx``'s constraint/metadata API
is thinner than SDV's, so range/enum/constant enforcement lives here as
defense-in-depth on top of the Mode-A Pandera contract.

Kept local to ``engines/b2_library/`` during parallel development to avoid
a shared-file merge conflict with B.1; consolidate to a shared
``engines/_fidelity.py`` post-merge if duplication warrants (design §2).

Pure Python + NumPy only — no Beam, no GCP, no torch, no sdgx. Importing
this module must succeed with only ``sdfb-core``'s base deps present.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, cast

from sdfb_core.contracts.prompt_constraint import (
    parse_prompt_constraint,
    render_prompt_clause,
)
from sdfb_core.contracts.schema import FieldSchema, TableSchema
from sdfb_core.engines.b2_library.temporal import (
    age_floor_epoch,
    classify_temporal_values,
    from_epoch,
    to_epoch,
    value_year,
)
from sdfb_core.engines.text_shapes import (
    RelaxedShapes,
    build_shape_mix,
    detect_identifier_shape,
)
from sdfb_core.observability import log_milestone

# Free-text heuristics. A STRING column is routed to the LLM free-text hook
# when the LLM can plausibly do better than empirical resampling: either the
# values look like prose/JSON, or the column is so high-cardinality that
# resampling the reference pool would leak/duplicate near-unique values.
_HIGH_CARDINALITY_RATIO = 0.9  # distinct / non-null count above this ⇒ free-text
_FREE_TEXT_MIN_LEN = 40  # mean string length above this ⇒ likely prose

# Cardinality caps. At or below the cap a discrete column is an
# enum-in-disguise and verbatim empirical resampling is the intended
# fidelity primitive. Above it, resampling IS memorization (2026-07-20 E2E:
# 11 columns at copy_ratio=1.0) — temporal columns jitter within the
# observed range, genuinely high-cardinality strings go to the LLM hook.
#
# The temporal cap mirrors b1_rag/profile.py's `_TEMPORAL_MAX_CATEGORIES`
# (20). The STRING cap mirrors b1's `_FREE_TEXT_MAX_CATEGORIES` (50), NOT
# the numeric/temporal 20: WS1 originally set 20 here, which routed
# mid-cardinality enums (COL_901, 41 distinct) to the LLM — a
# saturated domain the model cannot generate novel values for, so under
# strict_freetext every batch died on FreeTextEmptyYieldError and the
# 2026-07-22 b2 E2E run failed with blocker_ratio=1.0. 21-50-distinct
# enums resample empirically, exactly as b1 does on the same table (its
# memorization gate only scores columns with source_distinct > 100).
_TEMPORAL_MAX_CATEGORIES = 20
_CATEGORICAL_MAX_CATEGORIES = 50
_TEMPORAL_BQ_TYPES = frozenset({"DATE", "DATETIME", "TIME", "TIMESTAMP"})

# Sentinel calendar years excluded from a TEMPORAL column's jitter range and
# re-injected at their observed frequency instead. 0001-01-01 (null-substitute)
# and 9999-12-31 (open-end) are the classic warehouse sentinels; leaving them
# in [min, max] made the 2026-07-23 b2 E2E land uniform dates across ~2000
# years ("50-08-23", "955-10-29") on five date-STRING columns.
_TEMPORAL_SENTINEL_YEARS = frozenset({1, 9999})

# Interim temporal-age policy (2026-07-23): generated dates/datetimes/
# timestamps must not be older than this. The jitter floor becomes
# max(observed_min, now - 10y); columns whose ENTIRE observed range is older
# keep it unchanged (fabricated recent dates would be worse than old truth).
# EXPLICITLY interim: per-column behavior will later come from the DDL-JSON
# description (audit fields that must keep fixed/consistent values, FK
# referential integrity across linked tables, M2+) and must override this
# blanket constant — keep the policy a single constant + clamp, not a
# config surface, until that metadata exists.
_MAX_TEMPORAL_AGE_YEARS = 10

# Control-character guard (WS1 §3c): values containing C0/C1 control bytes
# in a STRING column usually mean binary data mis-declared upstream
# (2026-07-20 E2E: COL_048 landed garbled bytes verbatim). Accented /
# non-ASCII text is NOT flagged — only control ranges.
_NONPRINTABLE_RATIO_THRESHOLD = 0.05


class ColumnKind(StrEnum):
  """How a column is synthesized in B.2.

    - ``CONSTANT``: a single observed value across the reference → copied.
    - ``NUMERIC``: int/float/decimal → sampled then clipped to observed range.
    - ``CATEGORICAL``: low-cardinality discrete → empirical-frequency sample.
    - ``TEMPORAL``: high-cardinality date/time → novel-range jitter within [min, max].
    - ``FREE_TEXT``: prose / JSON / very-high-cardinality string → LLM hook.
    """

  CONSTANT = "constant"
  NUMERIC = "numeric"
  CATEGORICAL = "categorical"
  TEMPORAL = "temporal"
  FREE_TEXT = "free_text"


_NUMERIC_BQ_TYPES = frozenset(
    {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"})
_STRINGY_BQ_TYPES = frozenset({"STRING", "JSON", "GEOGRAPHY", "BYTES"})


@dataclass(frozen=True)
class ColumnProfile:
  """The fitted-once distribution metadata for one column.

    Carries everything ``generate_batch`` needs to enforce fidelity
    without re-reading the reference rows. Frozen + plain-data so it
    pickles cleanly across the Beam worker boundary alongside the engine.
    """

  name: str
  bq_type: str
  kind: ColumnKind
  nullable: bool
  null_fraction: float = 0.0
  # Fraction of ALL sampled rows whose value is a trimmed-empty string.
  # Empties leave `text_pool` and re-emit at this rate on the FREE_TEXT
  # route; CATEGORICAL keeps them as categories and leaves this 0.0
  # (2026-08-05 spec C1 — never double-count the parity).
  empty_fraction: float = 0.0
  # CONSTANT
  constant_value: object | None = None
  # NUMERIC observed bounds (inclusive); None when no non-null samples.
  minimum: float | None = None
  maximum: float | None = None
  is_integer: bool = False
  # NUMERIC / BIGNUMERIC decimal scale (places after the point) — sampled
  # values are rounded to this so they satisfy the record model's
  # `decimal_places` constraint. None ⇒ no rounding (FLOAT/INT).
  decimal_scale: int | None = None
  # CATEGORICAL empirical distribution: parallel value/weight lists.
  categories: tuple[object, ...] = ()
  weights: tuple[float, ...] = ()
  # FREE_TEXT: the observed pool (deduped, order-stable) the LLM hook
  # conditions on / falls back to.
  text_pool: tuple[str, ...] = ()
  # FREE_TEXT: fixed per-position character template (engines/text_shapes).
  # Identifier-shaped columns generate format-preserving values instead of
  # calling the LLM — qwen3-4b echoed COL_001's exemplars verbatim on every
  # escalation attempt in the 2026-07-17 E2E run (novel=0, 872 rows dead).
  identifier_shape: tuple[str, ...] | None = None
  # FREE_TEXT: observed exact-shape mix for the shape-preserving expander
  # (2026-08-05 spec C2/C3).
  shape_mix: RelaxedShapes | None = None
  # FREE_TEXT: per-column prompt steering from the column's DDL
  # description JSON (spec C5). Empty = no constraint.
  llm_prompt_constraint: str = ""
  # FREE_TEXT: structured-constraint extras (ADR 0024) — user regex for
  # guided decoding, length-pin flag (suppresses the derived length
  # hint), fictitious examples (joined to the pool rejection set).
  constraint_pattern: str = ""
  constraint_sets_length: bool = False
  constraint_examples: tuple[str, ...] = ()
  # TEMPORAL: how to render sampled epoch floats back into values.
  # minimum/maximum hold epoch floats (units per temporal.py) for this kind.
  temporal_value_type: str | None = None
  temporal_format: str | None = None
  # TEMPORAL: sentinel values (year 1 / 9999) excluded from [min, max] and
  # re-injected at their observed fraction: ((value, fraction), ...).
  temporal_sentinels: tuple[tuple[object, float], ...] = ()
  # NUMERIC/TEMPORAL: 11-point empirical quantile vector (p0..p100; epoch
  # floats for TEMPORAL, clamped like [minimum, maximum]). Samplers
  # inverse-transform uniform draws through it so skewed source marginals
  # land skewed (uniform-in-range flattened them — ADR 0022); () falls
  # back to uniform.
  quantiles: tuple[float, ...] = ()


def _decile_points(ordered: list[float]) -> tuple[float, ...]:
  """11 evenly-spaced empirical quantiles (p0..p100) from an ALREADY
    sorted list — one sort serves min/max and the inverse-CDF vector."""
  if not ordered:
    return ()
  last = len(ordered) - 1
  return tuple(ordered[round(i * last / 10)] for i in range(11))


def _non_null(values: list[object]) -> list[object]:
  return [v for v in values if v is not None]


def _column_values(reference_rows: list[dict], name: str) -> list[object]:
  return [row.get(name) for row in reference_rows]


def _has_control_chars(s: str) -> bool:
  # Unicode category Cc is exactly the C0 controls, DEL, and C1 controls.
  return any(
      unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in s)


def _warn_if_nonprintable(field: FieldSchema, non_null: list[object]) -> None:
  if field.bq_type not in {"STRING", "BYTES"} or not non_null:
    return
  strs = [str(v) for v in non_null]
  ratio = sum(1 for s in strs if _has_control_chars(s)) / len(strs)
  if ratio >= _NONPRINTABLE_RATIO_THRESHOLD:
    log_milestone(
        "column_nonprintable",
        level=logging.WARNING,
        column=field.name,
        ratio=round(ratio, 3),
    )


def _classify(  # noqa: PLR0911 — type classifier; sequential returns read clearer than nesting
    field: FieldSchema,
    non_null_values: list[object],
) -> ColumnKind:
  """Assign a :class:`ColumnKind` from the schema type + observed values."""
  distinct = len({_hashable(v) for v in non_null_values})

  if distinct <= 1:
    return ColumnKind.CONSTANT

  if field.bq_type in _NUMERIC_BQ_TYPES:
    return ColumnKind.NUMERIC

  if field.bq_type in {"BOOLEAN", "BOOL"}:
    return ColumnKind.CATEGORICAL

  if field.bq_type in _TEMPORAL_BQ_TYPES:
    if distinct <= _TEMPORAL_MAX_CATEGORIES:
      return ColumnKind.CATEGORICAL  # enum-in-disguise (load-date partitions)
    return ColumnKind.TEMPORAL

  if field.bq_type in _STRINGY_BQ_TYPES:
    # JSON columns always go to the free-text hook — empirical resampling
    # of structured blobs is meaningless.
    if field.bq_type == "JSON":
      return ColumnKind.FREE_TEXT
    strs = [str(v) for v in non_null_values]
    cardinality_ratio = distinct / max(len(strs), 1)
    mean_len = sum(len(s) for s in strs) / max(len(strs), 1)
    if cardinality_ratio >= _HIGH_CARDINALITY_RATIO or mean_len >= _FREE_TEXT_MIN_LEN:
      return ColumnKind.FREE_TEXT
    # Date-shaped strings follow the (stricter) temporal cap: ≤20
    # distinct is an enum-in-disguise (load-date partitions); above it
    # they jitter as TEMPORAL — the 2026-07-20 memorization defect was
    # resampling them verbatim.
    if classify_temporal_values(strs) is not None:
      if distinct <= _TEMPORAL_MAX_CATEGORIES:
        return ColumnKind.CATEGORICAL
      return ColumnKind.TEMPORAL
    if distinct <= _CATEGORICAL_MAX_CATEGORIES:
      return ColumnKind.CATEGORICAL
    # Above the string cap: high-cardinality discrete text the LLM must
    # synthesize — resampling it verbatim is the memorization defect.
    return ColumnKind.FREE_TEXT

  # Everything else (BOOL handled above): categorical over the observed pool.
  return ColumnKind.CATEGORICAL


def _hashable(value: object) -> object:
  """Coerce to a hashable key for distinct-counting (dicts/lists → repr)."""
  try:
    hash(value)
    return value
  except TypeError:
    return repr(value)


def _temporal_profile(
    field: FieldSchema,
    non_null: list[object],
    nullable: bool,
    null_fraction: float,
) -> ColumnProfile | None:
  """The TEMPORAL profile for a column, or None when the observed values
    are mixed/unparseable (caller demotes to CATEGORICAL).

    Sentinel-year values are split out of the jitter range (re-injected at
    sampling time at their observed fraction); a column that is ALL
    sentinel years has nothing to trim toward and keeps the observed range.
    The floor is then clamped to now - `_MAX_TEMPORAL_AGE_YEARS` — unless
    the whole range is older (fully-historical columns keep old truth;
    per-column DDL-JSON descriptions govern later).
    """
  spec = classify_temporal_values(non_null)
  if spec is None:
    return None
  value_type, fmt = spec
  regular: list[object] = []
  sentinel_counts: Counter = Counter()
  for v in non_null:
    year = value_year(v, value_type, fmt)
    if year in _TEMPORAL_SENTINEL_YEARS:
      sentinel_counts[v] += 1
    else:
      regular.append(v)
  if not regular:
    regular = list(non_null)
    sentinel_counts = Counter()
  epochs = [to_epoch(v, value_type, fmt) for v in regular]
  total_non_null = len(non_null)
  temporal_sentinels = tuple(
      (v, count / total_non_null) for v, count in sentinel_counts.items())
  ordered = sorted(epochs)
  lo, hi = ordered[0], ordered[-1]
  floor = age_floor_epoch(value_type, fmt, _MAX_TEMPORAL_AGE_YEARS)
  if floor is not None and lo < floor <= hi:
    lo = floor
    log_milestone(
        "temporal_range_clamped",
        column=field.name,
        max_age_years=_MAX_TEMPORAL_AGE_YEARS,
    )
  # Quantile points below the clamp floor collapse onto it — same
  # semantics as the [minimum, maximum] clamp, applied to the CDF vector.
  quantiles = tuple(max(q, lo) for q in _decile_points(ordered))
  return ColumnProfile(
      name=field.name,
      bq_type=field.bq_type,
      kind=ColumnKind.TEMPORAL,
      nullable=nullable,
      null_fraction=null_fraction,
      minimum=lo,
      maximum=hi,
      temporal_value_type=value_type,
      temporal_format=fmt,
      temporal_sentinels=temporal_sentinels,
      quantiles=quantiles,
  )


def profile_column(field: FieldSchema,
                   reference_rows: list[dict]) -> ColumnProfile:
  """Profile a single column over the reference rows (the O(1) fit step)."""
  raw = _column_values(reference_rows, field.name)
  non_null = _non_null(raw)
  _warn_if_nonprintable(field, non_null)
  total = len(raw)
  null_fraction = (total - len(non_null)) / total if total else 0.0
  nullable = field.is_nullable
  # Trimmed-empty strings are sparsity, not content: classification and
  # pools see `substantive`; the CATEGORICAL route keeps the original
  # stream so its frequency table carries the empties itself.
  substantive = [
      v for v in non_null if not (isinstance(v, str) and not v.strip())
  ]
  empty_fraction = (len(non_null) - len(substantive)) / total if total else 0.0

  # No non-null observations: emit a CONSTANT-None / empty profile. The
  # record model supplies the schema default (None for NULLABLE).
  if not non_null:
    return ColumnProfile(
        name=field.name,
        bq_type=field.bq_type,
        kind=ColumnKind.CONSTANT,
        nullable=nullable,
        null_fraction=null_fraction,
        constant_value=None,
    )

  kind = _classify(field, substantive if substantive else non_null)

  # `route: "llm"` (ADR 0024): STRING columns override their typed
  # classification into the LLM free-text route (B.1 parity). Non-STRING
  # types keep their route — B.2's own inverse-CDF path owns numeric
  # fidelity (ADR 0022) — with the same WARNING milestone as B.1.
  pc = parse_prompt_constraint(field.description, column=field.name)
  force_llm = pc is not None and pc.route == "llm"
  if force_llm:
    if field.bq_type in _STRINGY_BQ_TYPES:
      kind = ColumnKind.FREE_TEXT
    else:
      log_milestone(
          "prompt_constraint_route_unsupported",
          level=logging.WARNING,
          column=field.name,
          bq_type=field.bq_type,
      )
      force_llm = False

  if kind is ColumnKind.CONSTANT:
    return ColumnProfile(
        name=field.name,
        bq_type=field.bq_type,
        kind=kind,
        nullable=nullable,
        null_fraction=null_fraction,
        constant_value=(substantive[0] if substantive else non_null[0]),
    )

  if kind is ColumnKind.NUMERIC:
    # NUMERIC verdicts come from _classify, which already proved every
    # value parses; float() re-raising here would be a classifier bug.
    nums = sorted(float(cast(Any, v)) for v in non_null)
    is_int = field.bq_type in {"INTEGER", "INT64"}
    # NUMERIC/BIGNUMERIC: respect the declared scale (default 2 places
    # for fixed-point money-like columns) so sampled values pass the
    # record model's `decimal_places` constraint. FLOAT has no scale.
    decimal_scale: int | None = None
    if field.bq_type in {"NUMERIC", "BIGNUMERIC"}:
      decimal_scale = field.scale if field.scale is not None else 2
    return ColumnProfile(
        name=field.name,
        bq_type=field.bq_type,
        kind=kind,
        nullable=nullable,
        null_fraction=null_fraction,
        minimum=nums[0],
        maximum=nums[-1],
        is_integer=is_int,
        decimal_scale=decimal_scale,
        quantiles=_decile_points(nums),
    )

  if kind is ColumnKind.TEMPORAL:
    profile = _temporal_profile(field, substantive, nullable, null_fraction)
    if profile is not None:
      return profile
    # BQ-typed temporal whose observed values are mixed/unparseable:
    # stay in-support rather than guessing an epoch mapping.
    kind = ColumnKind.CATEGORICAL

  if kind is ColumnKind.FREE_TEXT:
    pool = _dedupe_stable([str(v) for v in substantive])
    return ColumnProfile(
        name=field.name,
        bq_type=field.bq_type,
        kind=kind,
        nullable=nullable,
        null_fraction=null_fraction,
        empty_fraction=empty_fraction,
        text_pool=tuple(pool),
        # `force_llm` skips the template route by explicit user intent.
        identifier_shape=(None if force_llm else detect_identifier_shape(pool)),
        # Deduped pool on purpose: B.2's identifier coverage pivot
        # divides mix mass by len(text_pool), so mass and denominator
        # must share the distinct universe. The row-mass weighting fix
        # (2026-08-11 R1, COL_054/COL_024/COL_015-class) needs rows
        # plumbed through this profile — R5/B.2 scope (ADR 0025).
        shape_mix=build_shape_mix(pool),
        llm_prompt_constraint=(render_prompt_clause(pc)
                               if pc is not None else ""),
        constraint_pattern=pc.pattern if pc is not None else "",
        constraint_sets_length=pc is not None and pc.length is not None,
        constraint_examples=pc.examples if pc is not None else (),
    )

  # CATEGORICAL — empirical frequency table, order-stable for determinism.
  counts = Counter(_hashable(v) for v in non_null)
  values = _dedupe_stable([_hashable(v) for v in non_null])
  total_n = sum(counts.values())
  weights = tuple(counts[v] / total_n for v in values)
  return ColumnProfile(
      name=field.name,
      bq_type=field.bq_type,
      kind=kind,
      nullable=nullable,
      null_fraction=null_fraction,
      categories=tuple(values),
      weights=weights,
  )


def _dedupe_stable(items: list) -> list:
  """Order-preserving de-duplication (determinism for sampling pools)."""
  seen: set = set()
  out: list = []
  for item in items:
    key = _hashable(item)
    if key not in seen:
      seen.add(key)
      out.append(item)
  return out


def profile_table(ctx_schema: TableSchema,
                  reference_rows: list[dict]) -> dict[str, ColumnProfile]:
  """Profile every top-level column. STRUCT/REPEATED columns are profiled
    as a single free-text/categorical unit over their serialized form;
    nested decomposition is out of M1 scope (single-table, §4)."""
  return {
      col.name: profile_column(col, reference_rows) for col in ctx_schema.columns
  }


# ---------------------------------------------------------------------------
# Post-hoc enforcement — clamp a sampled value back into observed support.
# ---------------------------------------------------------------------------


def enforce_value(profile: ColumnProfile, value: object) -> object:
  """Clamp one sampled value to the column's observed support.

    Defense-in-depth on top of the Pandera Mode-A contract: constants are
    copied verbatim, numerics are clipped to ``[min, max]``, categoricals
    are snapped to a known category if a backend produced something unseen.
    Free-text and temporal values are passed through (their samplers own support).
    """
  if profile.kind is ColumnKind.CONSTANT:
    return profile.constant_value

  if value is None:
    # Permit None only where the schema allows it; otherwise fall back to
    # a representative in-support value so the row stays schema-valid.
    return None if profile.nullable else _representative(profile)

  if profile.kind is ColumnKind.NUMERIC:
    return _enforce_numeric(profile, value)

  if profile.kind is ColumnKind.CATEGORICAL:
    known = {_hashable(c) for c in profile.categories}
    return value if _hashable(value) in known else _representative(profile)

  # TEMPORAL and FREE_TEXT — pass through, but coerce a JSON-string back to a dict
  # so it validates against the JSON column's `dict` record-model type (the LLM
  # hook and the text_pool both carry JSON as a string). TEMPORAL values are
  # rendered from in-range epoch draws by construction; re-parsing them here
  # would just repeat temporal.py.
  if profile.bq_type == "JSON":
    return _coerce_json(value, profile)
  return value


def _enforce_numeric(profile: ColumnProfile, value: object) -> object:
  """Clip a sampled numeric to ``[min, max]`` and pin its type/scale."""
  try:
    num = float(cast(Any, value))
  except (TypeError, ValueError):
    return _representative(profile)
  if profile.minimum is not None:
    num = max(num, profile.minimum)
  if profile.maximum is not None:
    num = min(num, profile.maximum)
  if profile.is_integer:
    return round(num)
  if profile.decimal_scale is not None:
    # Produce a Decimal quantized to the column's scale so it passes the
    # record model's NUMERIC `decimal_places` constraint exactly.
    return _quantize(num, profile.decimal_scale)
  return num


def _quantize(num: float, scale: int) -> Decimal:
  """Round a float to ``scale`` decimal places as an exact ``Decimal``.

    Going through ``str(num)`` avoids binary float-representation artifacts
    (``Decimal(15.37)`` would be ``15.3699...``); the quantize then pins it
    to exactly ``scale`` places so Pydantic's ``decimal_places`` check holds.
    """
  exponent = Decimal(1).scaleb(-scale) if scale > 0 else Decimal(1)
  return Decimal(str(num)).quantize(exponent, rounding=ROUND_HALF_UP)


def _coerce_json(value: object, profile: ColumnProfile) -> object:
  """Parse a JSON-string into a dict for a JSON column; tolerate failure."""
  if isinstance(value, dict | list):
    return value
  if isinstance(value, str):
    try:
      return json.loads(value)
    except (ValueError, TypeError):
      pass
  # Unparseable → fall back to a representative observed value (also parsed).
  rep = _representative(profile)
  if isinstance(rep, str):
    try:
      return json.loads(rep)
    except (ValueError, TypeError):
      return None
  return rep


def _representative(profile: ColumnProfile) -> object:
  """A guaranteed in-support fallback value for a column."""
  if profile.kind is ColumnKind.CONSTANT:
    return profile.constant_value
  if profile.kind is ColumnKind.NUMERIC:
    lo = profile.minimum if profile.minimum is not None else 0.0
    return round(lo) if profile.is_integer else lo
  if profile.kind is ColumnKind.TEMPORAL and profile.minimum is not None:
    # TEMPORAL profiles always carry a value type (set beside kind).
    return from_epoch(
        profile.minimum,
        cast("str", profile.temporal_value_type),
        profile.temporal_format,
    )
  if profile.kind is ColumnKind.CATEGORICAL and profile.categories:
    return profile.categories[0]
  if profile.kind is ColumnKind.FREE_TEXT and profile.text_pool:
    return profile.text_pool[0]
  return None
