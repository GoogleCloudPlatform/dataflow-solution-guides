"""Unit tests for `EnforceUniqueness` — the uniqueness gate transform.

Full-row duplicates and repeated identity-column values divert to the
DLQ (first occurrence lands) instead of failing the whole batch.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,redefined-outer-name,reimported

from __future__ import annotations

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from sdfb_beam.dofns.uniqueness import EnforceUniqueness


def test_enforce_uniqueness_diverts_row_and_identity_duplicates():
  # r1/r2 are exact-duplicate rows (same id + value).
  # r3/r4 share identity column "id" but differ in "value" — identity dup,
  # not a row dup.
  r1 = {"id": 1, "value": "a"}
  r2 = {"id": 1, "value": "a"}
  r3 = {"id": 2, "value": "b"}
  r4 = {"id": 2, "value": "c"}
  records = [r1, r2, r3, r4]

  with TestPipeline() as p:
    result = (
        p
        | "Create" >> beam.Create(records)
        | "EnforceUniqueness" >> EnforceUniqueness(identity_columns=["id"]))
    unique = result["unique"]
    duplicates = result["duplicates"]

    assert_that(
        unique | "CountUnique" >> beam.combiners.Count.Globally(),
        equal_to([2]),
        label="unique_count")
    assert_that(
        duplicates | "RuleIds" >> beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate", "identity.unique"]),
        label="duplicate_rule_ids",
    )
    assert_that(
        duplicates
        | "EnvelopeShape" >>
        beam.Map(lambda d: {"error_type", "stage"} <= set(d.keys()) and d[
            "error_type"] == "uniqueness" and d["stage"] == "pre_write"),
        equal_to([True, True]),
        label="duplicate_envelope_shape",
    )


def test_no_identity_columns_still_dedups_rows():
  """PipelineConfig default (identity_columns=()) — row-dedup alone runs.

    Repeated identity values must NOT divert when no identity columns are
    configured; only the exact-duplicate row does.
    """
  records = [
      {
          "id": 1,
          "value": "a"
      },
      {
          "id": 1,
          "value": "a"
      },  # exact row duplicate → diverted
      {
          "id": 1,
          "value": "b"
      },  # same id, different row → kept (no identity stage)
  ]
  with TestPipeline() as p:
    result = (
        p
        | "Create" >> beam.Create(records)
        | "EnforceUniqueness" >> EnforceUniqueness(identity_columns=[]))
    assert_that(
        result["unique"] | "CountUnique" >> beam.combiners.Count.Globally(),
        equal_to([2]),
        label="unique_count",
    )
    assert_that(
        result["duplicates"] | "RuleIds" >> beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate"]),
        label="duplicate_rule_ids",
    )


def test_multi_column_identity_key():
  """Identity is the combined (region, code) tuple — duplicate on the full
    tuple diverts; rows differing on any one identity column are kept."""
  records = [
      {
          "region": "eu",
          "code": 1,
          "value": "a"
      },
      {
          "region": "eu",
          "code": 1,
          "value": "b"
      },  # same (eu, 1) → diverted
      {
          "region": "eu",
          "code": 2,
          "value": "c"
      },  # differs on code → kept
      {
          "region": "us",
          "code": 1,
          "value": "d"
      },  # differs on region → kept
  ]
  with TestPipeline() as p:
    result = (
        p
        | "Create" >> beam.Create(records)
        | "EnforceUniqueness" >>
        EnforceUniqueness(identity_columns=["region", "code"]))
    assert_that(
        result["unique"] | "CountUnique" >> beam.combiners.Count.Globally(),
        equal_to([3]),
        label="unique_count",
    )
    assert_that(
        result["duplicates"] | "RuleIds" >> beam.Map(lambda d: d["rule_id"]),
        equal_to(["identity.unique"]),
        label="duplicate_rule_ids",
    )


def test_row_digest_excludes_identity_columns():
  """Regression test: identity columns are synthesized per row (see
    engines/identity.py) and therefore differ on every row even when the
    REST of the record is an exact engine-batch-replay. If the row digest
    included the identity column, two rows that are otherwise identical
    would never collide on the row digest (masked by the synthesized
    identity value) and the replay would slip past `row.duplicate` entirely.
    With identity columns excluded from the row digest, the second row must
    divert as `row.duplicate` even though its identity value differs.
    """
  records = [
      {
          "id": "identity-1",
          "value": "a"
      },
      {
          "id": "identity-2",
          "value": "a"
      },  # same non-identity content
  ]
  with TestPipeline() as p:
    result = (
        p
        | "Create" >> beam.Create(records)
        | "EnforceUniqueness" >> EnforceUniqueness(identity_columns=["id"]))
    assert_that(
        result["unique"] | "CountUnique" >> beam.combiners.Count.Globally(),
        equal_to([1]),
        label="unique_count",
    )
    assert_that(
        result["duplicates"] | "RuleIds" >> beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate"]),
        label="duplicate_rule_ids",
    )


def test_duplicate_free_input_passes_through_untouched():
  """No duplicates in ⇒ every row lands unchanged and zero envelopes out."""
  records = [
      {
          "id": 1,
          "value": "a"
      },
      {
          "id": 2,
          "value": "b"
      },
      {
          "id": 3,
          "value": "c"
      },
  ]
  with TestPipeline() as p:
    result = (
        p
        | "Create" >> beam.Create(records)
        | "EnforceUniqueness" >> EnforceUniqueness(identity_columns=["id"]))
    assert_that(result["unique"], equal_to(records), label="all_rows_land")
    assert_that(result["duplicates"], equal_to([]), label="no_duplicates")


def test_pk_duplicates_divert_to_dlq():
  """Rows sharing a declared PK tuple: first wins, rest → DLQ with
    rule_id=pk.duplicate. The 2026-07-09 run landed 722/1000 PK duplicates
    with a PASSED gate because no stage ever emitted this rule."""
  rows = [
      {
          "pk_a": "K1",
          "pk_b": 1,
          "val": "x"
      },
      {
          "pk_a": "K1",
          "pk_b": 1,
          "val": "y"
      },  # same PK, different row
      {
          "pk_a": "K2",
          "pk_b": 2,
          "val": "z"
      },
  ]
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(rows)
        | EnforceUniqueness(pk_columns=["pk_a", "pk_b"]))
    assert_that(
        out["unique"] | beam.Map(lambda r: (r["pk_a"], r["pk_b"])),
        equal_to([("K1", 1), ("K2", 2)]),
        label="unique_pks",
    )
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["rule_id"]),
        equal_to(["pk.duplicate"]),
        label="dlq_rule",
    )


# ---------------------------------------------------------------------------
# WS6 W4 — the exact path combines MAP-SIDE before the shuffle.
#
# GroupByKey materializes every value for a key on the reducer side, so the
# 2026-07-26 1M run pushed the whole dataset through shuffle (measured:
# GroupByRowDigest/Read peaked at 12.77 MiB/s). CombinePerKey lets each
# worker collapse its own duplicates first, so the shuffle carries roughly
# the unique set.
#
# The gate consumes dlq_by_rule COUNTS (validation/summary.py), not payloads,
# so exact counts are what must be preserved.
# ---------------------------------------------------------------------------
def test_combining_preserves_exact_duplicate_counts():
  from sdfb_beam.dofns.uniqueness import _FirstWinsCombineFn

  fn = _FirstWinsCombineFn()
  acc = fn.create_accumulator()
  for rec in ({"a": 1}, {"a": 1}, {"a": 1}):
    acc = fn.add_input(acc, rec)
  survivor, seen = fn.extract_output(acc)
  assert survivor == {"a": 1}
  assert seen == 3, "one survivor + two duplicates"


def test_combining_merges_across_workers_without_losing_count():
  """merge_accumulators runs when Dataflow combines partial results from
    different bundles — the count must survive the merge."""
  from sdfb_beam.dofns.uniqueness import _FirstWinsCombineFn

  fn = _FirstWinsCombineFn()
  a = fn.add_input(fn.add_input(fn.create_accumulator(), {"a": 1}), {"a": 1})
  b = fn.add_input(fn.create_accumulator(), {"a": 1})
  empty = fn.create_accumulator()
  survivor, seen = fn.extract_output(fn.merge_accumulators([a, empty, b]))
  assert survivor == {"a": 1}
  assert seen == 3


def test_empty_accumulator_yields_no_survivor():
  from sdfb_beam.dofns.uniqueness import _FirstWinsCombineFn

  fn = _FirstWinsCombineFn()
  assert fn.extract_output(fn.create_accumulator()) == (None, 0)


def test_exact_mode_output_matches_the_pre_ws6_contract():
  """Same unique rows and the same number of DLQ envelopes as the
    GroupByKey implementation produced."""
  import apache_beam as beam
  from apache_beam.testing.test_pipeline import TestPipeline
  from apache_beam.testing.util import assert_that, equal_to
  from sdfb_beam.dofns.uniqueness import EnforceUniqueness

  rows = [
      {
          "id": "1",
          "v": "x"
      },
      {
          "id": "2",
          "v": "x"
      },  # duplicate of row 1 once id is excluded
      {
          "id": "3",
          "v": "y"
      },
  ]
  with TestPipeline() as p:
    out = (p | beam.Create(rows) | EnforceUniqueness(identity_columns=["id"]))
    assert_that(
        out["unique"] | "V" >> beam.Map(lambda r: r["v"]),
        equal_to(["x", "y"]),
        label="unique",
    )
    assert_that(
        out["duplicates"] | "R" >> beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate"]),
        label="dups",
    )
