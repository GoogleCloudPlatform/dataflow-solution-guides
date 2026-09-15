"""--uniqueness_mode=streaming: no barrier on the landing path (WS6 W3).

2026-07-26_17_10_37: EnforceUniqueness chains up to three shuffles of the
FULL dataset, each a materialization barrier, so no row reached BigQuery
until generation had finished — measured as ~1.65 MiB/s during generation
then a narrow 12.77-13.97 MiB/s spike at the very end.

Streaming mode passes rows straight through to the sink and MEASURES the
duplicate rate on a parallel branch that shuffles 32-byte digests instead
of rows. The gate keeps working because the measured counts are fed into
dlq_by_rule exactly as diverted envelopes were.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import apache_beam as beam
import pytest
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that, equal_to
from sdfb_beam.dofns.uniqueness import EnforceUniqueness

_ROWS = [
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
        "v": "x"
    },  # and again
    {
        "id": "4",
        "v": "y"
    },
]


def test_streaming_lands_every_row_including_duplicates():
  """Nothing is diverted — that is the point. Rows reach the sink as they
    are generated instead of after a global barrier."""
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(_ROWS)
        | EnforceUniqueness(identity_columns=["id"], mode="streaming"))
    assert_that(out["unique"] | beam.Map(lambda r: r["id"]),
                equal_to(["1", "2", "3", "4"]))


def test_streaming_writes_no_duplicate_envelopes_to_the_dlq():
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(_ROWS)
        | EnforceUniqueness(identity_columns=["id"], mode="streaming"))
    assert_that(out["duplicates"], equal_to([]))


def test_streaming_still_reports_the_duplicate_count_to_the_gate():
  """The BLOCKER gate folds dlq_by_rule counts. If streaming reported
    nothing, memorization would silently stop being detectable."""
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(_ROWS)
        | EnforceUniqueness(identity_columns=["id"], mode="streaming"))
    assert_that(out["rule_counts"], equal_to([("row.duplicate", 2)]))


def test_streaming_distinct_count_keeps_the_gate_denominator_honest():
  """total = valid + dlq must still equal the rows generated. Feeding the
    duplicate count in WITHOUT correcting valid_count would inflate the
    denominator and silently weaken the blocker ratio."""
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(_ROWS)
        | EnforceUniqueness(identity_columns=["id"], mode="streaming"))
    assert_that(out["distinct_count"], equal_to([2]))  # {x, y}


def test_exact_mode_is_unchanged_and_is_the_default():
  with TestPipeline() as p:
    out = p | beam.Create(_ROWS) | EnforceUniqueness(identity_columns=["id"])
    assert_that(
        out["unique"] | beam.Map(lambda r: r["v"]),
        equal_to(["x", "y"]),
        label="unique")
    assert_that(
        out["duplicates"] | beam.Map(lambda d: d["rule_id"]),
        equal_to(["row.duplicate", "row.duplicate"]),
        label="dups")
    assert_that(out["rule_counts"], equal_to([]), label="counts")


def test_unknown_mode_fails_loudly():
  with pytest.raises(ValueError, match="uniqueness_mode"):
    EnforceUniqueness(mode="stremaing")


def _all_labels(node) -> list[str]:
  """Every transform label in the graph, recursing into composites."""
  out: list[str] = []
  for part in getattr(node, "parts", []):
    out.append(str(part.full_label))
    out.extend(_all_labels(part))
  return out


def test_streaming_dag_has_no_groupbykey_between_generate_and_write():
  """The structural claim, asserted on the built graph rather than argued.

    A GroupByKey (or CombinePerKey, which contains one) anywhere on the
    landing path is a materialization barrier — that is what kept rows out
    of BigQuery until the very end of 2026-07-26_17_10_37.
    """
  from sdfb_beam.pipeline import PipelineConfig, build_pipeline
  from sdfb_core.contracts import TableSchema

  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "demo.t"
      },
      "schema": [{
          "name": "v",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })

  def _labels(mode):
    cfg = PipelineConfig(
        table_schema=schema,
        engine_name="fake",
        model_client=None,
        num_rows=4,
        batch_size=4,
        run_id="r1",
        uniqueness_mode=mode,
    )
    # Construct the graph only — never execute it. We are asserting
    # DAG SHAPE, and running would need a real engine and client.
    p = beam.Pipeline()
    build_pipeline(
        p,
        reference_rows=[{
            "v": "a"
        }],
        config=cfg,
        landing_sink=beam.Map(lambda x: x),
        dlq_sink=beam.Map(lambda x: x),
    )
    return _all_labels(p.transforms_stack[0])

  exact = " ".join(_labels("exact"))
  streaming = " ".join(_labels("streaming"))
  assert "CombineByRowDigest" in exact
  assert "CombineByRowDigest" not in streaming


_PK_ROWS = [
    {
        "pk": "1",
        "v": "x"
    },
    {
        "pk": "1",
        "v": "y"
    },  # same PK, different free column — a PK duplicate only
    {
        "pk": "2",
        "v": "z"
    },
]


def test_streaming_measures_pk_duplicates():
  """A driven child defaults to streaming (ADR 0036 D6) and its PK is the
    thing under test. Measuring only the whole-row digest would read PASSED
    on a run that landed duplicate PKs with different free columns."""
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(_PK_ROWS)
        | EnforceUniqueness(pk_columns=["pk"], mode="streaming"))
    assert_that(
        out["rule_counts"],
        equal_to([("row.duplicate", 0), ("pk.duplicate", 1)]),
    )


def test_streaming_pk_duplicates_are_an_upper_bound_on_full_row_repeats():
  """A byte-identical row is counted under BOTH rules — the two branches
    are independent measurements, and the gate treats the sum as an upper
    bound (documented in `_expand_streaming`)."""
  rows = [{"pk": "1", "v": "x"}, {"pk": "1", "v": "x"}]
  with TestPipeline() as p:
    out = (
        p
        | beam.Create(rows)
        | EnforceUniqueness(pk_columns=["pk"], mode="streaming"))
    assert_that(
        out["rule_counts"],
        equal_to([("row.duplicate", 1), ("pk.duplicate", 1)]),
    )


def test_streaming_without_pk_columns_publishes_no_pk_rule():
  with TestPipeline() as p:
    out = (p | beam.Create(_PK_ROWS) | EnforceUniqueness(mode="streaming"))
    assert_that(out["rule_counts"], equal_to([("row.duplicate", 0)]))
