"""Shape detection for STRING columns that must never reach the LLM pool.

The 2026-07-17 E2E runs showed the free-text LLM pool failing on two whole
classes of *shaped* strings, one per engine:

  - date-shaped strings (B.1 COL_044/COL_034/COL_061): a plausible generated
    date has a high chance of existing somewhere in the dense source
    keyspace, so copy_ratio flags fire without any exemplar memorization —
    and the pool's distinct yield can never cover the marginal anyway;
  - fixed-alphabet identifiers (B.2 COL_001, 24-char upper-hex): the model
    echoes the shown exemplars verbatim at every escalation level
    (prompt_echoes == parsed, novel=0) — an identifier keyspace is exactly
    what an LLM cannot "vary" its way through.

Both are detectable from the reference values alone. Temporal-shaped
columns re-route to the engines' range samplers; identifier-shaped columns
generate format-preserving values from a per-position character template.

Shared between B.1 and B.2 (like `engines/identity.py`). Pure stdlib.
"""

from __future__ import annotations

import re
import string
import zlib
from collections import Counter
from typing import TYPE_CHECKING

from sdfb_core.engines.temporal_parse import parse_temporal_string

if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Callable, Iterable

# Every distinct value must parse with ONE of these for a column to be
# temporal-shaped. Formats are mutually exclusive (a value parses under at
# most one), so first-match is the match.
_TEMPORAL_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)

# One value proves nothing about a shared shape.
_MIN_VALUES = 2
# Mask-stable novelty retries (wave 4, D2): the fill redraws, the mask
# never re-picks. 8 attempts push slip-through on sparse keyspaces below
# ~1e-6 while saturated keyspaces still accept (k-anonymous by pigeonhole).
_FILL_RETRIES = 8
# A shape must carry at least this many varying (class) positions,
# mass-weighted, before expansion can diversify it — an all-literal
# template can only regenerate its own observed values.
_EXPAND_MIN_CLASS_POSITIONS = 2.0
# Below this length "identifier" vs enum-code is ambiguous — short codes
# stay on their existing (categorical / LLM) routes.
_MIN_IDENTIFIER_LENGTH = 8

# Ordered narrowest-first: a position's observed characters bind to the
# first class that covers them, keeping generation as tight as the evidence.
_CHAR_CLASSES: tuple[str, ...] = (
    string.digits,
    string.digits + "abcdef",
    string.digits + "ABCDEF",
    string.ascii_lowercase,
    string.ascii_uppercase,
    string.digits + string.ascii_lowercase,
    string.digits + string.ascii_uppercase,
    string.digits + string.ascii_letters,
)

# (class, has-digit, has-upper, has-lower) — precomputed kind flags for the
# kind-preservation rule in `_class_for`.
_CHAR_CLASS_KINDS: tuple[tuple[str, bool, bool, bool], ...] = tuple((
    cls,
    any(c.isdigit() for c in cls),
    any(c.isupper() for c in cls),
    any(c.islower() for c in cls),
) for cls in _CHAR_CLASSES)


def _class_for(chars: set[str]) -> str | None:
  """Narrowest class covering ``chars`` WITHOUT introducing a character
    KIND (digit / upper / lower) the evidence never showed.

    The hex classes sit before the plain letter classes (narrower), so a
    letter-only position whose observed chars happened to fall inside A-F
    bound to ``digits+ABCDEF`` and started emitting digits 62.5% of the
    time (2026-08-20 B_TABLE R1, COL_038-class: 'CQZWD1   DN…' →
    'CQZWD1   6C…' — measured family split 0.611/0.389, exactly 10/16 vs
    6/16). A class may generalize WITHIN a kind (D,N → any uppercase)
    but never add a kind."""
  has_digit = any(c.isdigit() for c in chars)
  has_upper = any(c.isupper() for c in chars)
  has_lower = any(c.islower() for c in chars)
  for cls, k_digit, k_upper, k_lower in _CHAR_CLASS_KINDS:
    if (chars <= set(cls) and (has_digit or not k_digit) and
        (has_upper or not k_upper) and (has_lower or not k_lower)):
      return cls
  return None


def detect_temporal_format(values: Iterable[str]) -> str | None:
  """The strftime format every value renders in, or None.

    Returns a format only when EVERY non-empty value parses with the same
    one — a single mixed-format value means the column is not uniformly
    date-shaped and stays on its existing route.
    """
  vals = [v for v in values if v]
  if len(vals) < _MIN_VALUES:
    return None
  for fmt in _TEMPORAL_FORMATS:
    try:
      parse_temporal_string(vals[0], fmt)
    except ValueError:
      continue
    try:
      for v in vals[1:]:
        parse_temporal_string(v, fmt)
    except ValueError:
      return None  # formats are mutually exclusive — no other fits
    return fmt
  return None


def detect_identifier_shape(values: Iterable[str]) -> tuple[str, ...] | None:
  """Per-position character template of a fixed-length identifier, or None.

    Each entry is either a literal character (every value agrees at that
    position) or the narrowest character class covering the position's
    observed characters. Any position with unclassifiable variation (spaces,
    mixed punctuation) disqualifies the whole column — prose never matches.
    """
  vals = [v for v in values if v]
  if len(vals) < _MIN_VALUES:
    return None
  length = len(vals[0])
  if length < _MIN_IDENTIFIER_LENGTH:
    return None
  if any(len(v) != length for v in vals):
    return None
  # Whitespace anywhere means prose, however uniform the template looks
  # ("Ticket about issue 0042" is not an identifier).
  if any(" " in v or "\t" in v for v in vals):
    return None
  shape: list[str] = []
  for i in range(length):
    chars = {v[i] for v in vals}
    if len(chars) == 1:
      shape.append(next(iter(chars)))
      continue
    cls = _class_for(chars)
    if cls is None:
      return None
    shape.append(cls)
  return tuple(shape)


def sample_identifier(shape: tuple[str, ...], pick: Callable[[int],
                                                             int]) -> str:
  """One format-preserving value from a template.

    ``pick(k)`` must return an int in ``[0, k)`` — pass the caller's seeded
    RNG (``random.Random.randrange``, or a NumPy-backed lambda) so draws
    stay reproducible in either backend.
    """
  return "".join(a if len(a) == 1 else a[pick(len(a))] for a in shape)


# A (weight, per-position template) pair per observed length bucket.
RelaxedShapes = tuple[tuple[int, tuple[str, ...]], ...]


def build_relaxed_shapes(values: Iterable[str]) -> RelaxedShapes | None:
  """Length-bucketed per-position templates for identifier-ish columns the
    strict detector rejects, or None.

    Last-resort route for LLM-echo-saturated free-text pools (2026-07-22 b2
    E2E: COL_052 — the model returned the shown exemplars verbatim on
    every escalation attempt, killing the whole run). Relaxations vs
    :func:`detect_identifier_shape`: mixed lengths become weighted buckets,
    there is no minimum length, and a position whose characters fit no
    known class falls back to the observed character set at that position
    instead of disqualifying the column. The whitespace (prose) guard and
    the two-value minimum stay — and apply per bucket, because a
    single-value bucket is all-literal and can only regenerate its own
    observed value, which any novelty filter must reject.
    """
  vals = [v for v in values if v]
  if len(vals) < _MIN_VALUES:
    return None
  if any(" " in v or "\t" in v for v in vals):
    return None
  buckets: dict[int, list[str]] = {}
  for v in vals:
    buckets.setdefault(len(v), []).append(v)
  shapes: list[tuple[int, tuple[str, ...]]] = []
  for length, bucket in sorted(buckets.items()):
    if len(bucket) < _MIN_VALUES:
      continue
    shape: list[str] = []
    for i in range(length):
      chars = {v[i] for v in bucket}
      if len(chars) == 1:
        shape.append(next(iter(chars)))
        continue
      cls = _class_for(chars)
      shape.append(cls if cls is not None else "".join(sorted(chars)))
    shapes.append((len(bucket), tuple(shape)))
  return tuple(shapes) or None


def relaxed_shape_lengths(shapes: RelaxedShapes) -> set[int]:
  """Observed length buckets of a relaxed template set."""
  return {len(shape) for _, shape in shapes}


def relaxed_shape_charset(shapes: RelaxedShapes) -> set[str]:
  """Union of every character any template position can emit.

    Literals contribute themselves; class positions contribute the class.
    A candidate value drawn from characters OUTSIDE this union cannot be
    in-format for the column (the templates were built from every observed
    value), which is what makes it a cheap plausibility gate for
    LLM-generated pool candidates."""
  chars: set[str] = set()
  for _, shape in shapes:
    for entry in shape:
      chars.update(entry)
  return chars


def relaxed_shapes_pattern(shapes: RelaxedShapes) -> str:
  """Conservative anchored regex accepting the shapes' length buckets over
    their union charset — for vLLM structured-output ``pattern`` guidance.

    Deliberately looser than the per-position templates (a per-position
    regex would force near-verbatim reproduction and reintroduce the
    memorization pressure the novelty filter exists to stop): any character
    from the union charset, at any observed bucket length. Junk like
    'UUID-…' or column-name echoes is unrepresentable; novel in-charset
    combinations remain free."""
  charset = sorted(relaxed_shape_charset(shapes))
  cls = "".join(re.escape(c) for c in charset)
  lengths = sorted(relaxed_shape_lengths(shapes))
  alternation = "|".join(f"[{cls}]{{{n}}}" for n in lengths)
  return f"^(?:{alternation})$"


def build_shape_mix(values: Iterable[str],
                    top_k: int = 8) -> RelaxedShapes | None:
  """Per-EXACT-SHAPE templates with observed weights, or None.

    The 2026-08-04 crosscheck's core recall finding: the primary free-text
    routes collapse a column's observed *mix* of formats to one or two.
    This builder groups values by their exact character-class mask
    (digit→``9``, upper→``A``, lower→``a``, everything else literal —
    the crosscheck's own masking), keeps the ``top_k`` heaviest masks with
    their observed counts, and emits per-position templates compatible with
    :func:`sample_relaxed_identifier` — a draw reproduces the observed
    shape mass by construction.

    Differences vs :func:`build_relaxed_shapes` (which stays the LLM-pool
    gate): buckets are per-mask, not per-length; single-value buckets are
    KEPT (a mask generalizes, an all-literal length bucket does not);
    spaces are allowed and stay literal (this feeds free-text columns).
    """
  vals = [v for v in values if v]
  if not vals:
    return None
  buckets: dict[str, list[str]] = {}
  for v in vals:
    mask = "".join("9" if ch.isdigit() else "A" if ch.isupper() else "a" if ch
                   .islower() else ch for ch in v)
    buckets.setdefault(mask, []).append(v)
  heaviest = sorted(buckets.values(), key=len, reverse=True)[:top_k]
  shapes: list[tuple[int, tuple[str, ...]]] = []
  for bucket in heaviest:
    length = len(bucket[0])
    shape: list[str] = []
    for i in range(length):
      chars = {v[i] for v in bucket}
      if len(chars) == 1:
        shape.append(next(iter(chars)))
        continue
      cls = _class_for(chars)
      shape.append(cls if cls is not None else "".join(sorted(chars)))
    shapes.append((len(bucket), tuple(shape)))
  return tuple(shapes) or None


def shape_mix_is_identifier_like(shapes: RelaxedShapes | None) -> bool:
  """True when the mix is code-like and expandable: no CLASS position can
    emit whitespace (variable padding is prose), and the mass-weighted
    average shape carries at least two class positions (an all-literal
    template can only regenerate its own observed values — nothing to
    expand). Literal positions do NOT disqualify — not even literal
    whitespace: fixed padding is part of a code's format, and refusing it
    pinned space-padded reference columns at the pool-cap diversity
    ceiling (2026-08-09 B_TABLE R1: 7 columns at distinct ≈ 513 vs source
    45k-146k, COL_038-class). Same whitespace rule as
    :func:`shape_mix_can_template`, to which this now delegates.
    """
  return shape_mix_can_template(shapes)


def shape_mix_can_template(shapes: RelaxedShapes | None) -> bool:
  """True when the mix can drive the *fallback pool* template.

    Looser than :func:`shape_mix_is_identifier_like` in exactly one way:
    whitespace is allowed as a LITERAL position (fixed padding is part of a
    code's format — the 2026-08-05 B_TABLE crosscheck's COL_026 carries 17
    literal leading spaces and the length-bucket relaxation rejected the
    whole column, leaving 0% shape recall). A CLASS position containing
    whitespace still disqualifies — variable padding is prose, not a code —
    and the mass-weighted two-class-position minimum stays, because an
    all-literal template can only regenerate its observed values.
    """
  if not shapes:
    return False
  total_weight = 0.0
  class_weight = 0.0
  for weight, shape in shapes:
    class_count = 0
    for entry in shape:
      if len(entry) > 1:
        if " " in entry or "\t" in entry:
          return False
        class_count += 1
    total_weight += weight
    class_weight += weight * class_count
  return (total_weight > 0 and
          class_weight / total_weight >= _EXPAND_MIN_CLASS_POSITIONS)


def _mask_char(ch: str) -> str:
  if ch.isdigit():
    return "9"
  if ch.isupper():
    return "A"
  if ch.islower():
    return "a"
  return ch


_CLASS_RUN = re.compile(r"9{2,}|A{2,}|a{2,}")


def collapsed_mask(value: str) -> str:
  """Run-collapsed character-class mask: digit/letter runs of ≥2 collapse
    to ``9+``/``A+``/``a+``; whitespace and punctuation stay literal, run
    lengths included.

    The candidate-gate key for shape-rigid whitespace columns (wave-2 §4c):
    letting digit/letter run LENGTHS vary keeps legitimate LLM diversity
    (lexical variation, shorter numbers), while a normalized whitespace run
    (`` ␣␣␣ `` → `` ␣ ``, the 2026-08-09 B_TABLE COL_038 failure) or a
    dropped delimiter changes the mask and is rejected.
    """
  return _CLASS_RUN.sub(lambda m: m.group(0)[0] + "+",
                        "".join(_mask_char(c) for c in value))


# A (weight, exact-mask) table over a column's distinct values.
MaskTable = tuple[tuple[int, str], ...]

_MASK_TABLE_CAP = 1024


def build_mask_table(values: Iterable[str],
                     cap: int = _MASK_TABLE_CAP) -> MaskTable | None:
  """Exact-mask frequency table over observed ROWS, heaviest first.

    The full mask DISTRIBUTION, not the top-8 templates: high-entropy
    identifier columns (2026-08-09 A_TABLE R1, COL_001: top-8 masks cover
    ~20% of 52k distinct) need whole-mask draws to reproduce the source
    mask marginal — the collapsed per-position template scrambles it (0%
    recall). Weights are ROW occurrences, not distinct-value counts: the
    2026-08-11 R1 pair measured distinct weighting inverting row-mass
    marginals wherever a heavy repeated head met a diverse tail
    (COL_054/COL_024/COL_015-class). ``cap`` bounds memory; ties break
    lexicographically for determinism.
    """
  vals = [v for v in values if v]
  if len(set(vals)) < _MIN_VALUES:
    return None
  counts = _mask_counts(vals)
  # Count ties break on a stable hash, NOT the mask string: lexicographic
  # ordering ('-' < '9' < 'a'/'A') kept the most digit-front-loaded masks
  # whenever counts tied — on an all-singleton column (COL_064, UUID v4)
  # the survivors' digit share measured 0.74 vs population 0.63
  # (2026-08-20 A_TABLE R1). crc32 is deterministic across processes.
  heaviest = sorted(
      counts.items(),
      key=lambda kv: (-kv[1], zlib.crc32(kv[0].encode("utf-8")), kv[0]),
  )[:cap]
  return tuple((n, mask) for mask, n in heaviest)


def _mask_counts(vals: list[str]) -> dict[str, int]:
  """Exact-mask row counts over non-empty values."""
  counts: dict[str, int] = {}
  for v in vals:
    mask = "".join(_mask_char(c) for c in v)
    counts[mask] = counts.get(mask, 0) + 1
  return counts


def mask_alphabets(values: Iterable[str]) -> dict[str, str]:
  """Observed characters per mask class, column-wide.

    A mask fill must stay inside the column's real alphabet — hex
    identifiers must not grow ``G-Z`` just because the mask says
    "uppercase" (COL_001 stays hexadecimal). Keys present only for classes
    the column actually exhibits.
    """
  digits: set[str] = set()
  uppers: set[str] = set()
  lowers: set[str] = set()
  for v in values:
    for ch in v:
      if ch.isdigit():
        digits.add(ch)
      elif ch.isupper():
        uppers.add(ch)
      elif ch.islower():
        lowers.add(ch)
  out: dict[str, str] = {}
  if digits:
    out["9"] = "".join(sorted(digits))
  if uppers:
    out["A"] = "".join(sorted(uppers))
  if lowers:
    out["a"] = "".join(sorted(lowers))
  return out


# Residual tail beyond the kept mask table (wave 4): the cap silently
# dropped every mask outside the top 1024 and renormalized the survivors —
# COL_001 (52k distinct 24-hex ids) landed each kept mask at 1/recall =
# 2.2-2.4x its source mass (shape recall 0.46), and COL_064 (UUID v4,
# every mask ~unique) collapsed 210k masks onto 1024 drawn uniformly.
# Tail draws synthesize a value per position from observed character
# FREQUENCIES, so near-unique-mask columns keep their mask entropy by
# construction. The tail's row weight follows Good & Turing's estimator:
# dropped rows plus one per kept singleton mask (a mask seen once is the
# evidence that unseen masks exist).
# (weight, per-length buckets: (bucket_weight, ((char, count), ...) per position))
MaskTail = tuple[int, tuple[tuple[int, tuple[tuple[tuple[str, int], ...], ...]],
                            ...]]


def _build_mask_tail(weight_rows: list[str],
                     extra_evidence: list[str]) -> MaskTail | None:
  """Tail bucket from the rows that carry its weight plus support-only
    evidence (e.g. the ADR 0023 source domain, which informs WHAT tail
    values look like but not HOW MUCH mass they hold)."""
  if not weight_rows:
    return None
  evidence = [v for v in weight_rows if v] + [v for v in extra_evidence if v]
  buckets: dict[int, list[str]] = {}
  for v in evidence:
    buckets.setdefault(len(v), []).append(v)
  per_length: list[tuple[int, tuple[tuple[tuple[str, int], ...], ...]]] = []
  for length, bucket in sorted(buckets.items()):
    cols = tuple(
        tuple(sorted(Counter(v[i]
                             for v in bucket).items()))
        for i in range(length))
    per_length.append((len(bucket), cols))
  return (len(weight_rows), tuple(per_length))


def _sample_mask_tail(tail: MaskTail, pick: Callable[[int], int]) -> str:
  """One tail value: draw a length bucket by evidence weight, then each
    position's character by its observed frequency."""
  _, buckets = tail
  total = sum(w for w, _ in buckets)
  r = pick(total)
  cols = buckets[-1][1]
  for w, cand in buckets:
    if r < w:
      cols = cand
      break
    r -= w
  out: list[str] = []
  for col in cols:
    t = sum(c for _, c in col)
    k = pick(t)
    ch = col[-1][0]
    for cand_ch, c in col:
      if k < c:
        ch = cand_ch
        break
      k -= c
    out.append(ch)
  return "".join(out)


# Per-length, per-position observed charsets: length → (charset@0, charset@1, …).
PositionalAlphabets = dict[int, tuple[str, ...]]


def positional_alphabets(values: Iterable[str]) -> PositionalAlphabets:
  """Observed characters per position, bucketed by value length.

    Column-wide class alphabets scramble fixed positional structure: the
    2026-08-11 A_TABLE R1 lost COL_001's literal ``C2E3`` prefix (every
    source value carries it; synthetic opened with any hex character) and
    COL_064's RFC 4122 v4 version/variant nibbles. A position whose
    observed charset is a singleton is a literal by the same evidence rule
    :func:`detect_identifier_shape` uses — no per-kind special cases.
    Length buckets with fewer than ``_MIN_VALUES`` values are skipped: one
    value proves nothing, and pinning it would regenerate it verbatim.
    """
  buckets: dict[int, list[str]] = {}
  for v in values:
    if v:
      buckets.setdefault(len(v), []).append(v)
  out: PositionalAlphabets = {}
  for length, bucket in buckets.items():
    if len(set(bucket)) < _MIN_VALUES:
      continue
    out[length] = tuple(
        "".join(sorted({v[i] for v in bucket})) for i in range(length))
  return out


def _positional_fill(positional_chars: str, class_alpha: str,
                     pick: Callable[[int], int]) -> str:
  """One char from the position's observed charset restricted to the mask
    class; the column-wide class alphabet is the fallback when the
    intersection is empty (a mask sampled at a length whose positional
    evidence never showed this class)."""
  narrowed = [c for c in positional_chars if c in class_alpha]
  if not narrowed:
    return class_alpha[pick(len(class_alpha))]
  return narrowed[pick(len(narrowed))]


def sample_from_mask(
    mask: str,
    alphabets: dict[str, str],
    pick: Callable[[int], int],
    positional: PositionalAlphabets | None = None,
) -> str:
  """One value from an exact mask: class symbols draw from the column's
    observed alphabets — narrowed to the position's observed charset when
    ``positional`` evidence exists for the mask's length (fixed prefixes,
    version nibbles and other positional literals survive by construction);
    literals pass through."""
  per_position = positional.get(len(mask)) if positional else None
  out: list[str] = []
  for i, ch in enumerate(mask):
    if ch == "9":
      alpha = alphabets.get("9", string.digits)
    elif ch == "A":
      alpha = alphabets.get("A", string.ascii_uppercase)
    elif ch == "a":
      alpha = alphabets.get("a", string.ascii_lowercase)
    else:
      out.append(ch)
      continue
    if per_position is not None:
      out.append(_positional_fill(per_position[i], alpha, pick))
    else:
      out.append(alpha[pick(len(alpha))])
  return "".join(out)


def sample_mask_table(
    table: MaskTable,
    alphabets: dict[str, str],
    pick: Callable[[int], int],
    positional: PositionalAlphabets | None = None,
) -> str:
  """One value from a mask table: draw a mask proportionally to its
    observed row weight, then fill it via :func:`sample_from_mask`."""
  total = sum(w for w, _ in table)
  r = pick(total)
  for w, mask in table:
    if r < w:
      return sample_from_mask(mask, alphabets, pick, positional)
    r -= w
  return sample_from_mask(table[-1][1], alphabets, pick, positional)


# Opaque, cacheable route + tables for one identifier column: everything
# `identifier_sampler_from` needs except the per-batch RNG. Building it
# sorts/masks the whole evidence set — with full source domains attached
# (150k values) that is too heavy to redo per generate_batch call.
IdentifierArtifacts = tuple


def build_identifier_artifacts(
    shape: tuple[str, ...],
    shape_mix: RelaxedShapes | None,
    observed_values: Iterable[object],
    coverage_min: float = 0.5,
    domain: Iterable[str] = (),
) -> IdentifierArtifacts:
  """Resolve the draw route + heavy tables for one identifier column.

    Mask mix when the top masks cover most of the observed mass (rigid
    mask families, the 2026-08-07 fix); otherwise a whole-mask draw from
    the row-weighted mask table filled from the column's observed
    alphabets, narrowed per position (`positional_alphabets`) so fixed
    prefixes and version nibbles survive (2026-08-11 A_TABLE R1: COL_001
    lost its literal C2E3 prefix, COL_064 its v4 nibble), plus a residual
    TAIL bucket for the mass the cap would otherwise renormalize away
    (wave 4 — see `MaskTail`). The collapsed template stays as the last
    resort.

    ``domain`` is the ADR 0023 full source domain, kept SEPARATE from the
    weight-carrying rows (wave 4, D1): concatenating its distinct values
    onto the row multiset let a 146k-value domain out-vote a 10k-row
    sample — the dominant mask fell from 89.8% to 8.2% of draws and the
    coverage pivot always fired. The domain feeds novelty rejection, the
    alphabets/positional evidence and the tail's support; mask WEIGHTS
    and the coverage denominator stay row-mass.
    """
  rows = [str(v) for v in observed_values]
  row_set = frozenset(rows)
  domain_only = [str(v) for v in domain if str(v) and str(v) not in row_set]
  observed = row_set | frozenset(domain_only)
  non_empty = [v for v in rows if v]
  if shape_mix:
    # Coverage counts only GENERATIVE buckets (≥1 class position): an
    # all-literal singleton bucket can only regenerate its observed
    # value verbatim, so it is memorization pressure, not coverage —
    # high-entropy identifiers carry many of them and must pivot to
    # the mask table. Mass is over the same universe the mix was built
    # from — rows for B.1 (row-mass mix), distinct for B.2 (deduped
    # pool) — so both callers stay self-consistent.
    generative = sum(
        w for w, shape in shape_mix if any(len(entry) > 1 for entry in shape))
    coverage = generative / len(non_empty) if non_empty else 0.0
    if coverage >= coverage_min:
      return ("mix", shape_mix, observed)
  table = build_mask_table(non_empty)
  if table is not None:
    counts = _mask_counts(non_empty)
    kept = {mask for _, mask in table}
    tail_rows = [
        v for v in non_empty if (m := "".join(
            _mask_char(c) for c in v)) not in kept or counts[m] == 1
    ]
    alpha_evidence = non_empty + domain_only
    return (
        "table",
        table,
        mask_alphabets(alpha_evidence),
        positional_alphabets(alpha_evidence),
        observed,
        _build_mask_tail(tail_rows, domain_only),
    )
  return ("collapsed", shape)


def identifier_sampler_from(artifacts: IdentifierArtifacts,
                            pick: Callable[[int], int]) -> Callable[[], str]:
  """Per-row generator over prebuilt artifacts.

    Novelty retries are MASK-STABLE (wave 4, D2): the bucket/mask is drawn
    once and only the FILL redraws on a collision. Redrawing the whole
    draw made rejection shape-dependent — mass migrated from saturated
    low-cardinality mask families to high-cardinality ones (2026-08-20
    B_TABLE R1, COL_026: dominant mask 0.890 → 0.838 while a rare variant
    inflated 42x). A keyspace so saturated that three fills all collide
    accepts the collision: its values are k-anonymous by pigeonhole, and
    mask mass beats forced novelty there."""
  route = artifacts[0]
  if route == "mix":
    _, shape_mix, observed = artifacts

    def _from_mix() -> str:
      shape = pick_relaxed_shape(shape_mix, pick)
      v = ""
      for _ in range(_FILL_RETRIES):
        v = sample_identifier(shape, pick)
        if v not in observed:
          break
      return v

    return _from_mix
  if route == "table":
    _, table, alphabets, positional, observed, tail = artifacts
    kept_weight = sum(w for w, _ in table)
    tail_weight = tail[0] if tail is not None else 0

    def _from_table() -> str:
      r = pick(kept_weight + tail_weight)
      if r < kept_weight or tail is None:
        mask = table[-1][1]
        for w, cand in table:
          if r < w:
            mask = cand
            break
          r -= w

        def _fill() -> str:
          return sample_from_mask(mask, alphabets, pick, positional)
      else:

        def _fill() -> str:
          return _sample_mask_tail(tail, pick)

      v = ""
      for _ in range(_FILL_RETRIES):
        v = _fill()
        if v not in observed:
          break
      return v

    return _from_table
  return lambda: sample_identifier(artifacts[1], pick)


def identifier_sampler(
    shape: tuple[str, ...],
    shape_mix: RelaxedShapes | None,
    observed_values: Iterable[object],
    pick: Callable[[int], int],
    coverage_min: float = 0.5,
    domain: Iterable[str] = (),
) -> Callable[[], str]:
  """Per-row identifier generator shared by both engines (wave-2 §4a) —
    the one-shot form of `build_identifier_artifacts` +
    `identifier_sampler_from`; callers on a hot path should build the
    artifacts once and reuse them."""
  return identifier_sampler_from(
      build_identifier_artifacts(
          shape, shape_mix, observed_values, coverage_min, domain=domain),
      pick,
  )


_DIGIT_RUN = re.compile(r"\d{2,}")


def mutate_digit_runs(value: str, pick: Callable[[int], int]) -> str:
  """Replace every maximal run of ≥2 digits with fresh digits of the same
    length. The first digit keeps its zero/nonzero-ness so fixed prefixes
    like ``0001…`` survive; runs of one digit and non-digits are untouched.
    The cardinality lever of `--freetext_expansion=all` (2026-08-05 spec C3).
    """

  def _fresh(m: re.Match[str]) -> str:
    run = m.group(0)
    first = "0" if run[0] == "0" else "123456789"[pick(9)]
    rest = "".join("0123456789"[pick(10)] for _ in run[1:])
    return first + rest

  return _DIGIT_RUN.sub(_fresh, value)


def pick_relaxed_shape(shapes: RelaxedShapes,
                       pick: Callable[[int], int]) -> tuple[str, ...]:
  """Draw ONE bucket proportionally to its observed weight. Callers doing
    novelty retries must keep the bucket and redraw only the fill (wave 4,
    D2): re-picking the bucket per retry makes rejection shape-dependent
    and migrates mass out of saturated mask families."""
  total = sum(w for w, _ in shapes)
  r = pick(total)
  for w, shape in shapes:
    if r < w:
      return shape
    r -= w
  return shapes[-1][1]


def sample_relaxed_identifier(shapes: RelaxedShapes,
                              pick: Callable[[int], int]) -> str:
  """One value from a relaxed template: draw a length bucket proportionally
    to its observed weight, then fill it position-by-position."""
  return sample_identifier(pick_relaxed_shape(shapes, pick), pick)


# C0 controls minus common text whitespace, plus DEL/C1 — the marker set of
# binary payloads mis-stored as STRING. Accented/Unicode TEXT never trips
# this (ord >= 160), so Spanish prose columns stay on their normal routes.
_CONTROL_CHARS = frozenset(
    chr(c) for c in range(32) if chr(c) not in "\t\n\r") | frozenset(
        chr(c) for c in range(127, 160))
_BINARY_MIN_SHARE = 0.5


def is_binary_class(values: Iterable[object],
                    min_share: float = _BINARY_MIN_SHARE) -> bool:
  """True when most substantive values carry control characters.

    COL_048-class (2026-08-21 four-run cycle): a binary/control-char
    column spent the ENTIRE cold pool phase (8.6 min, 41% of wall time)
    in an LLM ladder whose candidates were format-rejected en masse — an
    LLM cannot usefully emit control bytes. Such columns route straight
    to the shape-fallback template pool.
    """
  vals = [str(v) for v in values if v]
  if len(vals) < _MIN_VALUES:
    return False
  hits = sum(1 for v in vals if any(ch in _CONTROL_CHARS for ch in v))
  return hits / len(vals) >= min_share


def length_hint(values: Iterable[object], *, min_samples: int = 8) -> str:
  """Measured length band for free-text pool prompts.

    Steers the LLM's length marginal toward the source's observed p05-p95
    band (the 2026-08-04 crosscheck: synthetic prose ran systematically
    shorter than source). Returns "" below ``min_samples`` or when the band
    is degenerate — fixed-width values already carry their length in the
    shape template. Callers must APPEND this after the shared instruction
    prefix: a per-column constant suffix keeps vLLM automatic prefix caching
    serving the common prefix (ADR 0018).
    """
  lengths = sorted(len(str(v)) for v in values)
  if len(lengths) < min_samples:
    return ""
  last = len(lengths) - 1
  p05 = lengths[int(0.05 * last)]
  p50 = lengths[int(0.50 * last)]
  p95 = lengths[int(0.95 * last)]
  if p05 == p95:
    return ""
  return f"Most values are {p05}-{p95} characters long (median {p50})."


def length_ceiling(values: Iterable[object],
                   *,
                   min_samples: int = 8) -> int | None:
  """The hard length ceiling of a fixed-width prose field, or None.

    A source column stored in a fixed-width field truncates at its width,
    so its length marginal is a free distribution whose upper tail is
    folded onto the maximum: p95 == max over the substantive values, with
    a real spread below it (2026-08-26 R6 A_COL_019: p05 18, p50 34,
    p95 35, max 35 — and the pool's LLM values ran to 62). Free-length
    prose has a lone maximum well above p95; a narrow band (p05 within
    max(4, max/4) of the maximum) is not a wall the distribution runs
    into but a width the shape template already carries. ADR 0033.
    """
  lengths = sorted(
      len(str(v)) for v in values if v is not None and str(v).strip())
  if len(lengths) < min_samples:
    return None
  last = len(lengths) - 1
  p05 = lengths[int(0.05 * last)]
  p95 = lengths[int(0.95 * last)]
  top = lengths[-1]
  if p95 != top or top - p05 < max(4, top // 4):
    return None
  return top


__all__ = [
    "build_identifier_artifacts",
    "build_mask_table",
    "build_relaxed_shapes",
    "build_shape_mix",
    "collapsed_mask",
    "detect_identifier_shape",
    "detect_temporal_format",
    "identifier_sampler",
    "identifier_sampler_from",
    "is_binary_class",
    "length_ceiling",
    "length_hint",
    "mask_alphabets",
    "mutate_digit_runs",
    "pick_relaxed_shape",
    "positional_alphabets",
    "relaxed_shape_charset",
    "relaxed_shape_lengths",
    "relaxed_shapes_pattern",
    "sample_from_mask",
    "sample_identifier",
    "sample_mask_table",
    "sample_relaxed_identifier",
    "shape_mix_can_template",
    "shape_mix_is_identifier_like",
]
