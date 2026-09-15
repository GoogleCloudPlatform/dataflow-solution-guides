"""Uniqueness enforcement — duplicates divert to the DLQ instead of landing.

Full-row duplicates and repeated identity values are the two block-replay /
memorization signatures the 2026-07 E2E report found unguarded. Keying by
digest + ``CombinePerKey`` keeps memory flat regardless of run size; one
occurrence lands, the rest become DLQ rows whose ``rule_id`` feeds
``build_run_summary`` → the BLOCKER gate.

Three modes (``--uniqueness_mode``):

``exact`` (default, ADR 0034) — ONE full-row shuffle barrier. Rows cross
the shuffle once, keyed by their digest (``CombineByRowDigest``); the PK
and identity rules are resolved from KEY-ONLY groups
(``(pk tuple → sorted digests)``, a few bytes per row, computed map-side
in the same fused stage) delivered as side inputs to the barrier's read
stage. The 2026-08-29 R6 pair spent ~26 of 94 minutes in the previous
three-barrier chain — 123 GB through Dataflow Shuffle per job, with
``resource exhausted`` retry storms and harness restarts mid-barrier.

``exact_chained`` — the pre-ADR-0034 chain (row digest → PK → identity,
three full-row ``CombinePerKey`` barriers). Kept for A/B runs; identical
envelopes and counts.

``streaming`` — no barrier on the landing path; duplicates are MEASURED
on digest-only branches (WS6 W3): the whole-row digest always, and the PK
tuple's digest too when ``pk_columns`` is set (ADR 0036 D6). A
byte-identical row therefore counts under both rules — the pair is an
upper bound, which is the safe direction for a gate.

Which record survives a collision: ``exact`` keeps the row with the
smallest digest (deterministic); ``exact_chained`` keeps an arbitrary one
(Beam orders neither GroupByKey values nor combiner inputs). Duplicate
COUNTS are exact in every mode, which is what the gate folds. Row-duplicate
envelopes carry the survivor's payload (identical non-identity content by
construction); PK/identity envelopes in ``exact`` carry the dropped row's
own payload.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence

import apache_beam as beam
from sdfb_core.validation.uniqueness import row_digest

RULE_ROW_DUPLICATE = "row.duplicate"
RULE_IDENTITY_UNIQUE = "identity.unique"
RULE_PK_DUPLICATE = "pk.duplicate"

# WS6 W3 / ADR 0034. `exact` is the default: every duplicate is diverted
# to the DLQ behind a single full-row barrier. `exact_chained` is the
# three-barrier chain it replaced. `streaming` lands rows as they are
# generated and MEASURES the duplicate rate instead of removing it.
MODE_EXACT = "exact"
MODE_EXACT_CHAINED = "exact_chained"
MODE_STREAMING = "streaming"
UNIQUENESS_MODES = (MODE_EXACT, MODE_EXACT_CHAINED, MODE_STREAMING)


def _envelope(record: dict, rule_id: str) -> dict:
  return {
      "raw_request": record,
      "error_type": "uniqueness",
      "error_detail": f"{rule_id}: duplicate of an earlier record in this run",
      "rule_id": rule_id,
      "stage": "pre_write",
  }


@functools.lru_cache(maxsize=64)
def _column_set(columns: tuple[str, ...]) -> frozenset[str]:
  return frozenset(columns)


def _pack_row(row: dict, columns: Sequence[str] | None):
  """Row dict → value tuple in schema column order, for the shuffle.

    Beam's default coder spells every key name into every element; a
    67-column row shuffled as a dict is ~2x the bytes of its values. Rows
    whose key set is not exactly ``columns`` pass through untouched (an
    unknown shape is never truncated silently).
    """
  if not columns:
    return row
  cols = tuple(columns)
  if row.keys() != _column_set(cols):
    return row
  return tuple(row[c] for c in cols)


def _unpack_row(value, columns: Sequence[str] | None):
  """Inverse of `_pack_row`; dicts (unpacked rows) pass through."""
  if not columns or not isinstance(value, tuple):
    return value
  return dict(zip(tuple(columns), value, strict=True))


def _key_of(row: dict, columns: tuple[str, ...]) -> tuple[str, ...]:
  return tuple(str(row.get(c)) for c in columns)


def _pk_digest(row: dict, columns: tuple[str, ...]) -> str:
  """Digest of a row's PK tuple, keyed exactly as the exact modes key it.

    The members are `_key_of`'s stringified values, so a PK collision is the
    same event in every mode; hashing them keeps the streaming branch's
    shuffle at a fixed 32 bytes per row regardless of how wide the PK is.
    """
  return row_digest(dict(zip(columns, _key_of(row, columns), strict=True)))


class _FirstWinsCombineFn(beam.CombineFn):
  """Keep ONE survivor per key and count everything else.

    `GroupByKey` materializes every value for a key on the reducer side, so
    the whole dataset crosses the shuffle (2026-07-26 1M run:
    `GroupByRowDigest/Read` peaked at 12.77 MiB/s). `CombinePerKey` combines
    **map-side** first, so each worker collapses its own duplicates and the
    shuffle carries roughly the unique set.

    The accumulator is `(survivor, seen)`. `seen` is the EXACT number of
    records for the key — the BLOCKER gate folds `dlq_by_rule` counts
    (`validation/summary.py`), so the count is the part that must be
    preserved bit-for-bit.

    Which record survives is arbitrary, exactly as it was under
    `GroupByKey` (Beam does not order values within a group).
    """

  def create_accumulator(self) -> tuple[object | None, int]:
    return (None, 0)

  # pylint: disable-next=arguments-renamed  # Beam passes these positionally
  def add_input(self, accumulator: tuple[object | None, int],
                element) -> tuple[object | None, int]:
    survivor, seen = accumulator
    return (element if survivor is None else survivor, seen + 1)

  def merge_accumulators(self, accumulators) -> tuple[object | None, int]:
    survivor: object | None = None
    seen = 0
    for acc_survivor, acc_seen in accumulators:
      if survivor is None and acc_survivor is not None:
        survivor = acc_survivor
      seen += acc_seen
    return (survivor, seen)

  def extract_output(
      self, accumulator: tuple[object | None,
                               int]) -> tuple[object | None, int]:
    return accumulator


class _DistinctDigestsFn(beam.CombineFn):
  """`(key → sorted tuple of distinct row digests)` — the key-only group
    the single-barrier path resolves PK/identity collisions from. A key
    seen by one digest only never leaves the map side as a collision."""

  def create_accumulator(self) -> set:
    return set()

  # pylint: disable-next=arguments-renamed  # Beam passes these positionally
  def add_input(self, accumulator: set, digest: str) -> set:
    accumulator.add(digest)
    return accumulator

  def merge_accumulators(self, accumulators) -> set:
    out: set = set()
    for acc in accumulators:
      out |= acc
    return out

  def extract_output(self, accumulator: set) -> tuple[str, ...]:
    return tuple(sorted(accumulator))


class _ExpandCombined(beam.DoFn):
  """Turn `(key, (survivor, seen))` back into one survivor + `seen - 1`
    DLQ envelopes (the `exact_chained` path).

    The envelopes carry the SURVIVOR's payload rather than each dropped
    record's. For `row.duplicate` that is the same content by construction —
    equal `row_digest` means the non-identity fields are identical — the
    only loss being the dropped rows' freshly-synthesized identity values,
    which are meaningless by definition. Counts, which is what the gate
    folds, are exact.
    """

  def __init__(self, rule_id: str) -> None:
    super().__init__()
    self.rule_id = rule_id

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, kv):
    _, (survivor, seen) = kv
    if survivor is None:  # pragma: no cover - defensive
      return
    yield survivor
    envelope = _envelope(survivor, self.rule_id)
    for _ in range(seen - 1):
      yield beam.pvalue.TaggedOutput("duplicates", envelope)


class _ResolveUniquenessDoFn(beam.DoFn):
  """The single barrier's read side: one survivor per digest, then the
    PK and identity rules from the key-only collision groups.

    ``pk_groups`` / ``identity_groups`` map a key tuple to the SORTED
    digests that share it (collisions only). The PK survivor is the
    smallest digest of its group; the identity survivor is the smallest
    digest of its group that is not a PK loser — the same "among PK
    survivors" order the chain applied, made deterministic.
    """

  def __init__(
      self,
      pk_columns: tuple[str, ...],
      identity_columns: tuple[str, ...],
      columns: tuple[str, ...] | None,
  ) -> None:
    super().__init__()
    self.pk_columns = pk_columns
    self.identity_columns = identity_columns
    self.columns = columns
    self._losers_source: object | None = None
    self._pk_losers: frozenset[str] = frozenset()

  def _pk_loser_set(self, pk_groups) -> frozenset[str]:
    # Derived once per side-input value (Dataflow hands the same cached
    # dict to every bundle on a worker; a fresh dict recomputes).
    if pk_groups is None:
      return frozenset()
    if self._losers_source is not pk_groups:
      self._pk_losers = frozenset(
          d for group in pk_groups.values() for d in group[1:])
      self._losers_source = pk_groups
    return self._pk_losers

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, kv, pk_groups=None, identity_groups=None):
    digest, (survivor, seen) = kv
    if survivor is None:  # pragma: no cover - defensive
      return
    row = _unpack_row(survivor, self.columns)
    if seen > 1:
      envelope = _envelope(row, RULE_ROW_DUPLICATE)
      for _ in range(seen - 1):
        yield beam.pvalue.TaggedOutput("duplicates", envelope)
    if self.pk_columns and pk_groups:
      group = pk_groups.get(_key_of(row, self.pk_columns))
      if group and digest != group[0]:
        yield beam.pvalue.TaggedOutput("duplicates",
                                       _envelope(row, RULE_PK_DUPLICATE))
        return
    if self.identity_columns and identity_groups:
      group = identity_groups.get(_key_of(row, self.identity_columns))
      if group:
        losers = self._pk_loser_set(pk_groups)
        survivor_digest = min(d for d in group if d not in losers)
        if digest != survivor_digest:
          yield beam.pvalue.TaggedOutput("duplicates",
                                         _envelope(row, RULE_IDENTITY_UNIQUE))
          return
    yield row


class EnforceUniqueness(beam.PTransform):
  """Diverts full-row, PK and identity-column duplicates to the DLQ.

    Returns a ``dict`` with ``"unique"`` (main, deduplicated records) and
    ``"duplicates"`` (DLQ-envelope dicts) PCollections, plus the
    ``"rule_counts"`` / ``"distinct_count"`` streams the streaming mode
    publishes for the gate (empty in the exact modes).

    The ROW digest is computed with identity columns excluded
    (``{k: v for k, v in r.items() if k not in identity_set}``). Identity
    columns (see ``sdfb_core.engines.identity``) are synthesized fresh per
    row from ``(run_id, batch_id, row_index, column)``, so two rows that are
    an exact engine-batch-replay in every OTHER field would still carry
    distinct identity values — including them in the row digest would mask
    the replay from ``row.duplicate`` entirely. Excluding them keeps the two
    rules covering disjoint failure modes: ``row.duplicate`` catches
    non-identity-field replay, ``identity.unique`` catches identity-value
    collisions, keyed on the final (post-identity-synthesis) rows.

    ``columns`` (the landing schema's column names, in order) lets the
    exact barrier shuffle rows as value tuples instead of dicts — about
    half the bytes; rows of any other shape pass through as dicts.
    """

  def __init__(
      self,
      identity_columns: list[str] | None = None,
      pk_columns: list[str] | None = None,
      mode: str = MODE_EXACT,
      columns: Sequence[str] | None = None,
  ) -> None:
    super().__init__()
    if mode not in UNIQUENESS_MODES:
      raise ValueError(
          f"uniqueness_mode must be one of {UNIQUENESS_MODES}, got {mode!r}")
    self.identity_columns = list(identity_columns or [])
    self.pk_columns = list(pk_columns or [])
    self.mode = mode
    self.columns = list(columns) if columns else None

  # pylint: disable-next=arguments-renamed  # Beam passes the PCollection positionally
  def expand(self, records):
    identity_set = frozenset(self.identity_columns)

    def _row_key(r, ids=identity_set):
      return row_digest({k: v for k, v in r.items() if k not in ids})

    if self.mode == MODE_STREAMING:
      return self._expand_streaming(records, _row_key)
    if self.mode == MODE_EXACT_CHAINED:
      return self._expand_chained(records, _row_key)
    return self._expand_single_barrier(records, _row_key)

  # -- exact (ADR 0034): one full-row barrier ------------------------------

  def _expand_single_barrier(self, records, row_key):
    columns = tuple(self.columns) if self.columns else None
    pk_cols = tuple(self.pk_columns)
    id_cols = tuple(self.identity_columns)

    # The digest is computed ONCE per row; the barrier keying and the
    # key-only groups all read it from this fused stream.
    digested = records | "DigestRows" >> beam.Map(lambda r, rk=row_key:
                                                  (rk(r), r))
    combined = (
        digested
        | "KeyByRowDigest" >> beam.Map(lambda kv, cols=columns:
                                       (kv[0], _pack_row(kv[1], cols)))
        | "CombineByRowDigest" >> beam.CombinePerKey(_FirstWinsCombineFn()))
    side_inputs: dict[str, object] = {}
    if pk_cols:
      side_inputs["pk_groups"] = beam.pvalue.AsDict(
          self._collision_groups(digested, pk_cols, "Pk"))
    if id_cols:
      side_inputs["identity_groups"] = beam.pvalue.AsDict(
          self._collision_groups(digested, id_cols, "Identity"))
    resolved = combined | "ResolveUniqueness" >> beam.ParDo(
        _ResolveUniquenessDoFn(pk_cols, id_cols, columns), **
        side_inputs).with_outputs(
            "duplicates", main="unique")
    return self._exact_outputs(resolved.unique, resolved.duplicates)

  @staticmethod
  def _collision_groups(digested, cols: tuple[str, ...], label: str):
    """`(key tuple → sorted digests)` for keys shared by ≥ 2 distinct
        rows. Map-side combined; the shuffle carries key + digest only."""
    return (digested
            | f"{label}DigestPairs" >> beam.Map(lambda kv, c=cols:
                                                (_key_of(kv[1], c), kv[0]))
            | f"{label}DigestGroups" >> beam.CombinePerKey(_DistinctDigestsFn())
            | f"{label}Collisions" >> beam.Filter(lambda kv: len(kv[1]) > 1))

  @staticmethod
  def _exact_outputs(unique, duplicates):
    return {
        "unique":
            unique,
        "duplicates":
            duplicates,
        # Exact modes report through diverted envelopes, so they have
        # no separate counts to contribute.
        "rule_counts":
            duplicates | "NoRuleCounts" >> beam.FlatMap(lambda _: []),
        "distinct_count":
            unique | "NoDistinctCount" >> beam.FlatMap(lambda _: []),
    }

  # -- exact_chained (pre-ADR-0034): three full-row barriers ---------------

  def _expand_chained(self, records, row_key):
    by_row = (
        records
        | "KeyByRowDigest" >> beam.Map(lambda r: (row_key(r), r))
        | "CombineByRowDigest" >> beam.CombinePerKey(_FirstWinsCombineFn())
        | "FirstRowWins" >> beam.ParDo(_ExpandCombined(
            RULE_ROW_DUPLICATE)).with_outputs("duplicates", main="unique"))
    row_unique = by_row.unique
    dup_streams = [by_row.duplicates]
    if self.pk_columns:
      pk_cols = self.pk_columns
      by_pk = (
          row_unique
          | "KeyByPk" >> beam.Map(lambda r, c=pk_cols:
                                  (tuple(str(r.get(x)) for x in c), r))
          | "CombineByPk" >> beam.CombinePerKey(_FirstWinsCombineFn())
          | "FirstPkWins" >> beam.ParDo(_ExpandCombined(
              RULE_PK_DUPLICATE)).with_outputs("duplicates", main="unique"))
      row_unique = by_pk.unique
      dup_streams.append(by_pk.duplicates)
    if self.identity_columns:
      cols = self.identity_columns
      by_id = (
          row_unique
          | "KeyByIdentity" >> beam.Map(lambda r, c=cols:
                                        (tuple(str(r.get(x)) for x in c), r))
          | "CombineByIdentity" >> beam.CombinePerKey(_FirstWinsCombineFn())
          | "FirstIdentityWins" >> beam.ParDo(
              _ExpandCombined(RULE_IDENTITY_UNIQUE)).with_outputs(
                  "duplicates", main="unique"))
      row_unique = by_id.unique
      dup_streams.append(by_id.duplicates)
    duplicates = dup_streams | "FlattenDuplicates" >> beam.Flatten()
    return self._exact_outputs(row_unique, duplicates)

  # -- streaming (WS6 W3): no barrier --------------------------------------

  def _expand_streaming(self, records, row_key):
    """No barrier on the landing path.

        Rows pass straight through, so BigQuery sees them as they are
        generated. Duplicates are MEASURED on a parallel branch that
        shuffles 32-byte digests rather than whole rows, and the measurement
        never gates the write.

        The gate's arithmetic stays honest: `build_run_summary` computes
        ``total = valid_count + dlq_count``. Feeding the duplicate count in
        while `valid_count` still counted every landed row would inflate the
        denominator and quietly weaken the blocker ratio, so streaming also
        publishes `distinct_count` for the caller to use as `valid_count` —
        distinct + excess is exactly the number of rows generated.

        PK duplicates are measured on their OWN branch when ``pk_columns``
        is set (ADR 0036 D6: a driven child defaults to this mode, and its
        PK is precisely the claim under test — measuring only the whole-row
        digest would read PASSED on a run that landed duplicate PKs whose
        free columns differ). The two branches are independent, so a
        byte-identical row is counted under BOTH ``row.duplicate`` and
        ``pk.duplicate``: the gate reads the pair as an UPPER BOUND on
        distinct defective rows, which is the correct direction for a
        safety gate.
        """
    per_digest = (
        records
        | "DigestOnly" >> beam.Map(row_key)
        # Count.PerElement combines map-side, and the values crossing
        # the shuffle are digests, not rows.
        | "CountPerDigest" >> beam.combiners.Count.PerElement())
    excess = (
        per_digest
        | "ExcessPerDigest" >> beam.Map(lambda kv: kv[1] - 1)
        | "SumExcess" >> beam.CombineGlobally(sum))
    rule_counts = excess | "AsRuleCount" >> beam.Map(lambda n:
                                                     (RULE_ROW_DUPLICATE, n))
    if self.pk_columns:
      pk_cols = tuple(self.pk_columns)
      pk_counts = (
          records
          |
          "StreamingPkDigest" >> beam.Map(lambda r, c=pk_cols: _pk_digest(r, c))
          | "StreamingPkCount" >> beam.combiners.Count.PerElement()
          | "StreamingPkExcess" >> beam.Map(lambda kv: max(0, kv[1] - 1))
          | "StreamingSumPkExcess" >> beam.CombineGlobally(sum)
          | "AsPkRuleCount" >> beam.Map(lambda n: (RULE_PK_DUPLICATE, n)))
      rule_counts = (rule_counts,
                     pk_counts) | "FlattenRuleCounts" >> beam.Flatten()
    return {
        "unique":
            records,
        "duplicates":
            records | "NoDuplicates" >> beam.FlatMap(lambda _: []),
        "rule_counts":
            rule_counts,
        "distinct_count":
            per_digest | "CountDistinct" >> beam.combiners.Count.Globally(),
    }
