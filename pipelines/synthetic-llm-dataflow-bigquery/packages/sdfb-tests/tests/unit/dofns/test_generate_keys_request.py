"""ADR 0036: the Generate DoFn drives the engine from parent keys."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,unused-argument,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.testing.util import assert_that
from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_tests.fakes import FakeModelClient

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.src.child_t"
    },
    "schema": [
        {
            "name": "PID",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "CAT",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "AMT",
            "type": "INT64",
            "mode": "REQUIRED"
        },
    ]
})
_ROWS = [{"PID": f"P{i:04d}", "CAT": "abc"[i % 3], "AMT": i} for i in range(60)]


def _ctx() -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_ROWS,
      reference_digest="dofn-fanout",
      pipeline_run_id="dofn-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "2": 1
          },
          "cells": {
              "cols": ["CAT"],
              "rows": [["a"], ["b"], ["c"]],
              "counts": [1, 1, 1]
          },
          "exact_cells": True
      },
  )


def test_keys_request_yields_two_children_per_key(caplog):
  dofn = GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=_ROWS),
      ctx=_ctx(),
  )
  request = {"batch_id": 0, "keys": [("K1",), ("K2",), ("K3",)]}

  def _check(rows):
    rows = list(rows)
    assert len(rows) == 6, rows
    assert {r["PID"] for r in rows} == {"K1", "K2", "K3"}
    for pid in ("K1", "K2", "K3"):
      cats = [r["CAT"] for r in rows if r["PID"] == pid]
      assert len(set(cats)) == 2

  with caplog.at_level(
      logging.INFO, logger="sdfb.milestone"), beam.Pipeline(
          options=PipelineOptions(["--runner=DirectRunner"])) as p:
    out = p | beam.Create([request]) | beam.ParDo(dofn).with_outputs(
        "failed", main="main")
    assert_that(out.main, _check)
  assert "name=batch_done" in caplog.text and "keys=3" in caplog.text


class _RaisingEngine:
  """Engine whose key-batch generation blows up mid-batch."""

  def generate_for_keys(self, keys, cfg):
    yield from ()  # a generator function: raises on first iteration
    raise RuntimeError("cell draw exploded")


def test_a_failed_key_batch_summarizes_its_keys_in_the_dlq():
  """ADR 0036 review I7: `raw_request: request` embedded the WHOLE key
    list — at `--keys_per_batch=10000` that is a multi-MB DLQ row per
    crashed batch, written to BigQuery and read back by the gate. The
    envelope keeps a bounded sample plus the totals, and `n` so
    `_dlq_rule_weight` still weights the batch by its expected rows."""
  dofn = GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=_ROWS),
      ctx=_ctx(),
  )
  dofn._engine = _RaisingEngine()
  keys = [(f"K{i}",) for i in range(25)]
  request = {"batch_id": 7, "keys": keys, "n": 50}

  out = list(dofn.process(request))
  assert len(out) == 1
  envelope = out[0].value
  assert envelope["rule_id"] == "engine_failure"
  raw = envelope["raw_request"]
  assert raw["keys_total"] == 25
  assert len(raw["keys"]) == 10
  assert raw["keys"][0] == ["K0"]
  assert raw["n"] == 50  # the gate's expected-lost-rows weight
  assert raw["batch_id"] == 7


class _RecordingEngine:
  """Spy engine — captures exactly what `GenerateRecordsDoFn` hands to
    `generate_for_keys`, isolating the DoFn's own pre-filtering from the
    engines' own conditional-candidate resolution (covered separately by
    `test_b1_rag.py::TestGenerateForKeysConditional`, Task 3)."""

  def __init__(self) -> None:
    self.calls: list[tuple] = []

  def generate_for_keys(self, keys, cfg, matches=None):
    self.calls.append((list(keys), matches))
    return iter(())


class _CounterSpy:

  def __init__(self) -> None:
    self.value = 0

  def inc(self, n: int = 1) -> None:
    self.value += n


def _ctx_with_conditional(*, nullable: bool) -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_ROWS,
      reference_digest="dofn-fanout-cond",
      pipeline_run_id="dofn-run",
      pk_columns=["PID", "CAT"],
      fanout={
          "driving_cols": ["PID"],
          "histogram": {
              "2": 1
          },
          "conditional": [{
              "id": "(T,R)->right",
              "cols": ["CAT"],
              "nullable": nullable
          }]
      },
  )


class TestConditionalMatchesNullPolicy:
  """ADR 0037 / design 2026-09-11 §4 "NULL policy (ruling B)": a key
    with no candidate on a non-nullable conditional edge never reaches
    the engine — it is dropped, counted, and diverted as `fk.unmatched`
    before `generate_for_keys` is called."""

  def test_unmatched_key_on_non_nullable_edge_is_dropped_and_diverted(self):
    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=False),
    )
    engine = _RecordingEngine()
    dofn._engine = engine
    counter = _CounterSpy()
    dofn._keys_unmatched = counter
    request = {
        "batch_id": 3,
        "keys": [("K1",), ("K2",)],
        "n": 10,
        "matches": {
            "(T,R)->right": [[("r1",)], []]
        },
    }

    out = list(dofn.process(request))

    assert len(out) == 1
    envelope = out[0].value
    assert envelope["rule_id"] == "fk.unmatched"
    assert envelope["error_type"] == "referential_integrity"
    assert envelope["stage"] == "pre_generate"
    assert envelope["raw_request"] == {
        "batch_id": 3,
        "keys": [["K2"]],
        "n": 5,  # max(1, round(10 / 2))
    }
    assert envelope["error_detail"] == (
        "no (T,R)->right candidate for key ('K2',)")

    assert len(engine.calls) == 1
    kept_keys, kept_matches = engine.calls[0]
    assert kept_keys == [("K1",)]
    assert kept_matches == {"(T,R)->right": [[("r1",)]]}

    assert counter.value == 1

  def test_unmatched_key_on_nullable_edge_reaches_the_engine_unfiltered(self):
    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=True),
    )
    engine = _RecordingEngine()
    dofn._engine = engine
    counter = _CounterSpy()
    dofn._keys_unmatched = counter
    request = {
        "batch_id": 4,
        "keys": [("K1",), ("K2",)],
        "n": 10,
        "matches": {
            "(T,R)->right": [[("r1",)], []]
        },
    }

    out = list(dofn.process(request))

    assert out == []  # no DLQ envelope — the engine NULL-fills instead
    assert len(engine.calls) == 1
    kept_keys, kept_matches = engine.calls[0]
    assert kept_keys == [("K1",), ("K2",)]
    assert kept_matches == {"(T,R)->right": [[("r1",)], []]}
    assert counter.value == 0

  def test_request_without_matches_calls_the_engine_positionally(self):
    """No `matches` key on the request ⇒ today's call shape,
        byte-identical (the engine here would TypeError on a stray
        `matches=` kwarg it does not accept)."""

    class _PositionalOnlyEngine:

      def __init__(self) -> None:
        self.calls: list[tuple] = []

      def generate_for_keys(self, keys, cfg):
        self.calls.append(list(keys))
        return iter(())

    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=False),
    )
    engine = _PositionalOnlyEngine()
    dofn._engine = engine
    counter = _CounterSpy()
    dofn._keys_unmatched = counter
    request = {"batch_id": 5, "keys": [("K1",), ("K2",)], "n": 10}

    out = list(dofn.process(request))

    assert out == []
    assert engine.calls == [[("K1",), ("K2",)]]
    assert counter.value == 0

  def test_batch_unmatched_milestone_logs_the_drop_count(self, caplog):
    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=False),
    )
    dofn._engine = _RecordingEngine()
    request = {
        "batch_id": 9,
        "keys": [("K1",), ("K2",)],
        "n": 10,
        "matches": {
            "(T,R)->right": [[("r1",)], []]
        },
    }

    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      list(dofn.process(request))

    assert "name=batch_unmatched" in caplog.text
    assert "batch_id=9" in caplog.text
    assert "keys_dropped=1" in caplog.text

  def test_middle_of_batch_drop_keeps_alignment_across_two_edges(self, caplog):
    """Should-fix (review round 1, finding #1): a 4-key batch drops
        keys 1 AND 2 (the MIDDLE of the batch, not the tail) on a
        non-nullable edge, while a SECOND, nullable edge has candidates
        for every key. Pins down that `keep_idx` re-indexes EVERY edge
        in `matches` — not just the one that caused the drop — and that
        the filtering is truly index-based, not a "drop the tail"
        shortcut that would pass every other test in this file."""
    ctx = GenerationContext(
        table_schema=_SCHEMA,
        reference_rows=_ROWS,
        reference_digest="dofn-fanout-mid",
        pipeline_run_id="dofn-run",
        pk_columns=["PID", "CAT"],
        fanout={
            "driving_cols": ["PID"],
            "histogram": {
                "2": 1
            },
            "conditional": [
                {
                    "id": "(A,X)->px",
                    "cols": ["CAT"],
                    "nullable": False
                },
                {
                    "id": "(B,Y)->py",
                    "cols": ["AMT"],
                    "nullable": True
                },
            ],
        },
    )
    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=ctx,
    )
    engine = _RecordingEngine()
    dofn._engine = engine
    counter = _CounterSpy()
    dofn._keys_unmatched = counter
    keys = [("K0",), ("K1",), ("K2",), ("K3",)]
    request = {
        "batch_id": 11,
        "keys": keys,
        "n": 8,
        "matches": {
            # Non-nullable: no candidate at index 1 or 2 → those keys drop.
            "(A,X)->px": [[("a0",)], [], [], [("a3",)]],
            # Nullable: candidates for ALL four keys — never causes a
            # drop, but MUST still be re-indexed alongside "(A,X)->px".
            "(B,Y)->py": [[("b0",)], [("b1",)], [("b2",)], [("b3",)]],
        },
    }

    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      out = list(dofn.process(request))

    envelopes = [o.value for o in out]
    assert len(envelopes) == 2
    dropped_pids = {e["raw_request"]["keys"][0][0] for e in envelopes}
    assert dropped_pids == {"K1", "K2"}
    for envelope in envelopes:
      assert envelope["rule_id"] == "fk.unmatched"
      assert envelope["error_type"] == "referential_integrity"
      assert envelope["stage"] == "pre_generate"
      assert envelope["raw_request"]["n"] == 2  # max(1, round(8 / 4))
      assert "(A,X)->px" in envelope["error_detail"]  # the offending edge

    assert len(engine.calls) == 1
    kept_keys, kept_matches = engine.calls[0]
    assert kept_keys == [("K0",), ("K3",)]
    assert kept_matches == {
        "(A,X)->px": [[("a0",)], [("a3",)]],
        "(B,Y)->py": [[("b0",)], [("b3",)]],
    }

    assert counter.value == 2
    assert "name=batch_unmatched" in caplog.text
    assert "batch_id=11" in caplog.text
    assert "keys_dropped=2" in caplog.text

  def test_a_crash_after_a_drop_does_not_double_count_the_dropped_rows(
      self, caplog):
    """Fix wave A5: `n` was never recomputed after
        `_filter_unmatched_keys`, so an engine crash AFTER a drop emitted
        an `engine_failure` envelope whose `raw_request["n"]` still
        counted the dropped keys' rows — rows already weighted into their
        own `fk.unmatched` envelopes. `_dlq_rule_weight` then charged the
        BLOCKER ratio twice for them, and `batch_start` / `batch_done`
        reported the PRE-filter key count."""

    class _RaisingConditionalEngine:

      def generate_for_keys(self, keys, cfg, matches=None):
        yield from ()  # a generator function: raises on first iteration
        raise RuntimeError("cell draw exploded")

    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=False),
    )
    dofn._engine = _RaisingConditionalEngine()
    dofn._keys_unmatched = _CounterSpy()
    request = {
        "batch_id": 21,
        "keys": [("K1",), ("K2",), ("K3",), ("K4",)],
        "n": 8,  # 4 keys x mean fan-out 2
        "matches": {
            "(T,R)->right": [[("r1",)], [], [], [("r4",)]]
        },
    }

    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      out = list(dofn.process(request))

    envelopes = [o.value for o in out]
    unmatched = [e for e in envelopes if e["rule_id"] == "fk.unmatched"]
    failures = [e for e in envelopes if e["rule_id"] == "engine_failure"]
    assert len(unmatched) == 2 and len(failures) == 1
    # Each dropped key already carries its own expected rows...
    assert [e["raw_request"]["n"] for e in unmatched] == [2, 2]
    # ...so the crashed batch may only claim what is LEFT.
    assert failures[0]["raw_request"]["n"] == 4
    assert failures[0]["raw_request"]["keys_total"] == 2
    assert [k[0] for k in failures[0]["raw_request"]["keys"]] == ["K1", "K4"]
    # The logs report the SURVIVING batch, with the drop still visible.
    start = [ln for ln in caplog.text.splitlines() if "name=batch_start" in ln]
    assert len(start) == 1
    assert "keys=2" in start[0] and "keys_dropped=2" in start[0]

  def test_a_batch_with_no_drops_keeps_todays_counts(self, caplog):
    """The A5 recompute must not move a batch that dropped nothing."""

    class _RaisingConditionalEngine:

      def generate_for_keys(self, keys, cfg, matches=None):
        yield from ()  # a generator function: raises on first iteration
        raise RuntimeError("boom")

    dofn = GenerateRecordsDoFn(
        engine_name="b1_rag",
        model_client=FakeModelClient(reference_pool=_ROWS),
        ctx=_ctx_with_conditional(nullable=False),
    )
    dofn._engine = _RaisingConditionalEngine()
    dofn._keys_unmatched = _CounterSpy()
    request = {
        "batch_id": 22,
        "keys": [("K1",), ("K2",)],
        "n": 4,
        "matches": {
            "(T,R)->right": [[("r1",)], [("r2",)]]
        },
    }

    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      out = list(dofn.process(request))

    assert [o.value["rule_id"] for o in out] == ["engine_failure"]
    assert out[0].value["raw_request"]["n"] == 4
    start = [ln for ln in caplog.text.splitlines() if "name=batch_start" in ln]
    assert "keys=2" in start[0] and "keys_dropped" not in start[0]
