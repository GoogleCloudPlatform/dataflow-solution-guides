"""`--uniqueness_mode=exact` runs ONE full-row shuffle barrier (ADR 0034).

The 2026-08-29 R6 pair (10M rows/table, cold + warm) spent ~26 of 94
minutes in `EnforceUniqueness`: three chained `CombinePerKey` barriers —
row digest → PK → identity — each pushing every full row through Dataflow
Shuffle (123 GB shuffled per job, `resource exhausted` retry storms,
harness restarts mid-barrier). The PK and identity rules only need the
KEYS: a `(pk_key → sorted digests)` group is a few bytes per row, and the
digest-unique row stream is the only thing that must cross the shuffle
whole. The old chain stays available as `exact_chained` for A/B runs.

Semantics preserved exactly (the gate folds `dlq_by_rule` counts):
  - row.duplicate: one survivor per digest, `seen - 1` envelopes;
  - pk.duplicate: among digest-unique rows, one survivor per PK tuple
    (the MIN digest — deterministic, where the chain was arbitrary);
  - identity.unique: among PK survivors, one survivor per identity tuple.
"""

from __future__ import annotations

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from sdfb_beam.dofns.uniqueness import (
    UNIQUENESS_MODES,
    EnforceUniqueness,
    _pack_row,
    _unpack_row,
)
from sdfb_core.validation.uniqueness import row_digest


def _all_labels(node) -> list[str]:
  out: list[str] = []
  for part in getattr(node, "parts", []):
    out.append(str(part.full_label))
    out.extend(_all_labels(part))
  return out


def _labels_for(mode: str) -> str:
  p = beam.Pipeline()
  _ = (
      p
      | beam.Create([{
          "pk": "k",
          "id": "i",
          "v": "x"
      }])
      | EnforceUniqueness(
          identity_columns=["id"], pk_columns=["pk"], mode=mode))
  return " ".join(_all_labels(p.transforms_stack[0]))


def test_exact_is_the_default_and_the_chain_is_still_selectable():
  assert "exact" in UNIQUENESS_MODES
  assert "exact_chained" in UNIQUENESS_MODES
  assert EnforceUniqueness().mode == "exact"


def test_exact_dag_has_one_full_row_barrier_and_key_only_groups():
  labels = _labels_for("exact")
  # The single full-row barrier stays.
  assert "CombineByRowDigest" in labels
  # PK and identity are resolved from KEY-ONLY groups, never a second
  # full-row combine.
  assert "CombineByPk" not in labels
  assert "CombineByIdentity" not in labels
  assert "PkDigestGroups" in labels
  assert "IdentityDigestGroups" in labels
  assert "ResolveUniqueness" in labels


def test_exact_chained_keeps_the_three_barrier_chain():
  labels = _labels_for("exact_chained")
  assert "CombineByRowDigest" in labels
  assert "CombineByPk" in labels
  assert "CombineByIdentity" in labels
  assert "ResolveUniqueness" not in labels


def test_pk_survivor_is_the_min_digest_row_and_losers_carry_their_own_payload():
  rows = [
      {
          "pk": "K1",
          "v": "x"
      },
      {
          "pk": "K1",
          "v": "y"
      },
      {
          "pk": "K2",
          "v": "z"
      },
  ]
  d_x, d_y = row_digest(rows[0]), row_digest(rows[1])
  survivor, loser = (rows[0], rows[1]) if d_x < d_y else (rows[1], rows[0])
  with TestPipeline() as p:
    out = p | beam.Create(rows) | EnforceUniqueness(pk_columns=["pk"])
    assert_that(out["unique"], equal_to([survivor, rows[2]]), label="unique")
    assert_that(
        out["duplicates"]
        | beam.Map(lambda d: (d["rule_id"], d["raw_request"])),
        equal_to([("pk.duplicate", loser)]),
        label="loser_payload",
    )


def test_pk_and_identity_collision_on_the_same_pair_drops_exactly_one_row():
  """Two rows sharing BOTH the PK tuple and the identity tuple: the chain
    dropped one as pk.duplicate and then saw a single PK survivor per
    identity — zero identity envelopes. The single-barrier path must agree
    (identity survivorship is resolved among PK survivors, not among all
    digest-unique rows, or the pair would lose both rows)."""
  rows = [
      {
          "pk": "K",
          "id": "I",
          "v": "a"
      },
      {
          "pk": "K",
          "id": "I",
          "v": "b"
      },
  ]
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(rows)
        | EnforceUniqueness(identity_columns=["id"], pk_columns=["pk"]))
    assert_that(
        out["unique"] | beam.combiners.Count.Globally(),
        equal_to([1]),
        label="one_survivor",
    )
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["rule_id"]),
        equal_to(["pk.duplicate"]),
        label="only_pk_rule",
    )


def test_identity_survivor_ignores_pk_losers_when_choosing():
  """r1 loses on PK to r0; r1 also shares its identity with r2. The
    identity survivor must be chosen among PK survivors — r2 stays."""
  r0 = {"pk": "K", "id": "A", "v": "0"}
  r2 = {"pk": "L", "id": "B", "v": "2"}
  # Pick r1's content so that r1 is the PK loser (larger digest than r0):
  # the identity column is excluded from the digest, so only `v` moves it.
  r1 = next({
      "pk": "K",
      "id": "B",
      "v": f"loser-{i}"
  } for i in range(64) if row_digest({
      "pk": "K",
      "v": f"loser-{i}"
  }) > row_digest({
      "pk": "K",
      "v": "0"
  }))
  rows = [r0, r1, r2]
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(rows)
        | EnforceUniqueness(identity_columns=["id"], pk_columns=["pk"]))
    assert_that(
        out["unique"] | beam.Map(lambda r: r["v"]),
        equal_to(["0", "2"]),
        label="pk_and_identity_survivors",
    )
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["rule_id"]),
        equal_to(["pk.duplicate"]),
        label="no_identity_envelope",
    )


def test_row_duplicates_still_count_exactly_with_three_copies():
  rows = [{"v": "x"}, {"v": "x"}, {"v": "x"}, {"v": "y"}]
  with TestPipeline() as p:
    out = p | beam.Create(rows) | EnforceUniqueness()
    assert_that(
        out["unique"] | beam.Map(lambda r: r["v"]),
        equal_to(["x", "y"]),
        label="unique",
    )
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate", "row.duplicate"]),
        label="two_envelopes",
    )


def test_pack_row_round_trips_through_value_tuples():
  columns = ["a", "b", "c"]
  row = {"a": 1, "b": None, "c": "z"}
  packed = _pack_row(row, columns)
  assert isinstance(packed, tuple)
  assert packed == (1, None, "z")
  assert _unpack_row(packed, columns) == row


def test_pack_row_leaves_rows_with_other_keys_untouched():
  columns = ["a", "b"]
  row = {"a": 1, "extra": 2}
  assert _pack_row(row, columns) is row
  assert _unpack_row(row, columns) is row
  assert _pack_row(row, None) is row


def test_packed_barrier_lands_rows_as_the_original_dicts():
  rows = [
      {
          "pk": "K1",
          "id": "I1",
          "v": "x",
          "n": None
      },
      {
          "pk": "K1",
          "id": "I1",
          "v": "x",
          "n": None
      },  # row dup
      {
          "pk": "K2",
          "id": "I2",
          "v": "y",
          "n": 3
      },
  ]
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(rows)
        | EnforceUniqueness(
            identity_columns=["id"],
            pk_columns=["pk"],
            columns=["pk", "id", "v", "n"],
        ))
    assert_that(out["unique"], equal_to([rows[0], rows[2]]), label="dicts")
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["raw_request"]),
        equal_to([rows[0]]),
        label="envelope_payload_is_a_dict",
    )


def test_exact_publishes_no_rule_counts_and_no_distinct_count():
  with TestPipeline() as p:
    out = p | beam.Create([{"v": "x"}]) | EnforceUniqueness(pk_columns=["v"])
    assert_that(out["rule_counts"], equal_to([]), label="counts")
    assert_that(out["distinct_count"], equal_to([]), label="distinct")
