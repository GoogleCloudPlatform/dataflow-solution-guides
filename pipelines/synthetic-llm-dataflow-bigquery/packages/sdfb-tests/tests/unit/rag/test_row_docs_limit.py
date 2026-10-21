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
"""`--rag_row_docs_limit`: the B.1 row-document prefix as a launch knob.

One value, resolved once by the launcher, slices the same prefix in every
place that used to assume 1024::

    CLI / flex / DAG ──> resolve_launch_row_docs_limit (clamp to sample)
        ├─> plan_rag_population   (store covers the prefix? full|topup|skip)
        ├─> _population_branch    (row_doc chunks for rows[:N])
        └─> GenerationContext ──> B1RagEngine (embed | read rows[:N])

The default (`MAX_ROW_DOC_ROWS`, 1024) must reproduce today's behaviour.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import argparse
import dataclasses
import logging

import apache_beam as beam
import pytest
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.testing.test_pipeline import TestPipeline
from pydantic import ValidationError
from sdfb_beam.cli.run_pipeline import (
    parse_args,
    parse_rag_row_docs_limit,
    plan_rag_population,
    resolve_launch_row_docs_limit,
)
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import (
    PipelineConfig,
    _population_branch,
    build_pipeline,
)
from sdfb_core.contracts import TableSchema
from sdfb_core.engines import GenerationContext
from sdfb_core.engines.b1_rag import B1RagEngine
from sdfb_core.engines.generation_plan import clear_generation_plan_log
from sdfb_core.rag import InMemoryChunkStore
from sdfb_core.rag.chunking import (
    CHUNK_KIND_FREE_TEXT_COL,
    MAX_ROW_DOC_ROWS,
    chunk_free_text_value,
    chunk_row,
    compute_row_digest,
    resolve_row_docs_limit,
)
from sdfb_core.rag.embedding import HashingEmbedder
from sdfb_core.rag.serialize import serialize_rows
from sdfb_tests.fakes import FakeModelClient

_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.t"
    },
    "schema": [
        {
            "name": "id",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "status",
            "type": "STRING",
            "mode": "REQUIRED"
        },
    ],
    "primary_keys": ["id"],
})
_ROWS = [{"id": i, "status": ["OPEN", "DONE"][i % 2]} for i in range(40)]
_EID, _EVER = "hashing-384", "v1"
_COLUMNS = ["id", "status"]


class _CountingEmbedder(HashingEmbedder):

  def __init__(self, dim: int = 384) -> None:
    super().__init__(dim=dim)
    self.counts: list[int] = []

  def embed(self, texts):
    self.counts.append(len(texts))
    return super().embed(texts)


class _NoLLMClient:

  def generate_json(self, prompt, json_schema, **kw):
    return [{"values": []}]


def _store_for(rows, *, values: bool = False) -> InMemoryChunkStore:
  """What a population run with ``len(rows)`` as the limit wrote."""
  chunks = [
      c for row in rows for c in chunk_row(
          row,
          column_order=_COLUMNS,
          free_text_columns=[],
          source_fqn="p.d.t",
          reference_digest="dig",
          embedder_id=_EID,
          embedder_version=_EVER,
      )
  ]
  vectors = HashingEmbedder(dim=384).embed(serialize_rows(rows, _COLUMNS))
  store = InMemoryChunkStore([
      dataclasses.replace(c, embedding=vectors[i]) for i, c in enumerate(chunks)
  ])
  if values:
    store.add([
        chunk_free_text_value(
            "status",
            "OPEN",
            source_fqn="p.d.t",
            reference_digest="dig",
            embedder_id=_EID,
            embedder_version=_EVER,
        )
    ])
  return store


def _ctx(**kw) -> GenerationContext:
  return GenerationContext(
      table_schema=_SCHEMA,
      reference_rows=_ROWS,
      reference_digest="dig",
      pipeline_run_id="row-docs-limit",
      embedder_id=_EID,
      embedder_version=_EVER,
      **kw,
  )


# --- the value: validation, clamping, default --------------------------------


def test_the_default_is_the_old_constant():
  assert MAX_ROW_DOC_ROWS == 1024
  # pylint: disable-next=unsubscriptable-object  # pydantic's model_fields is a dict
  assert GenerationContext.model_fields[
      "rag_row_docs_limit"].default == MAX_ROW_DOC_ROWS
  assert PipelineConfig.__dataclass_fields__[
      "rag_row_docs_limit"].default == MAX_ROW_DOC_ROWS


@pytest.mark.parametrize("bad", [0, -1, 1.5, "8", True, None])
def test_resolve_refuses_anything_but_a_positive_integer(bad):
  with pytest.raises(ValueError, match="rag_row_docs_limit"):
    resolve_row_docs_limit(bad, 100)


@pytest.mark.parametrize(("requested", "sample", "expected"), [
    (1024, 10_000, 1024),
    (4096, 10_000, 4096),
    (4096, 500, 500),
    (1, 500, 1),
    (2048, 0, 2048),
])
def test_resolve_clamps_to_the_sample(requested, sample, expected):
  assert resolve_row_docs_limit(requested, sample) == expected


def test_the_context_refuses_a_limit_below_one():
  with pytest.raises(ValidationError):
    _ctx(rag_row_docs_limit=0)


# --- the CLI flag --------------------------------------------------------------


def _argv(*extra: str) -> list[str]:
  return [
      "--reference_table", "p.d.t", "--landing_table", "p.d.landing",
      "--dlq_table", "p.d.dlq", "--num_rows", "10", "--run_id", "r",
      "--model_uri", "gs://b/synthetic/models/f/m/v1/", *extra
  ]


def test_the_flag_defaults_to_the_constant():
  args, _ = parse_args(_argv())
  assert args.rag_row_docs_limit == MAX_ROW_DOC_ROWS


def test_the_flag_takes_an_integer_and_an_empty_string_means_default():
  args, _ = parse_args(_argv("--rag_row_docs_limit", "2048"))
  assert args.rag_row_docs_limit == 2048
  args, _ = parse_args(_argv("--rag_row_docs_limit="))
  assert args.rag_row_docs_limit == MAX_ROW_DOC_ROWS


@pytest.mark.parametrize("bad", ["0", "-5", "many", "1.5"])
def test_the_flag_refuses_a_non_positive_or_non_integer(bad, capsys):
  with pytest.raises(SystemExit):
    parse_args(_argv(f"--rag_row_docs_limit={bad}"))
  assert "--rag_row_docs_limit must be an integer >= 1" in capsys.readouterr(
  ).err
  with pytest.raises(argparse.ArgumentTypeError):
    parse_rag_row_docs_limit(bad)


def test_the_launcher_clamps_and_logs_once(caplog):
  args = argparse.Namespace(rag_row_docs_limit=4096, landing_table="p.d.l")
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert resolve_launch_row_docs_limit(args, _ROWS) == len(_ROWS)
  clamped = [
      r for r in caplog.records if "name=rag_row_docs_clamped" in r.message
  ]
  assert len(clamped) == 1
  assert "requested=4096" in clamped[0].message


def test_a_default_launch_on_a_small_sample_logs_nothing_new(caplog):
  args = argparse.Namespace(landing_table="p.d.l")  # no attribute: default
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert resolve_launch_row_docs_limit(args, _ROWS) == len(_ROWS)
  assert "rag_row_docs_clamped" not in caplog.text


# --- the engine: embed and read the same prefix ------------------------------


def test_the_engine_embeds_exactly_the_limit():
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NoLLMClient(), _ctx(rag_row_docs_limit=7))
  assert embedder.counts[0] == 7
  assert len(engine._ref_vectors) == 7
  engine.teardown()


def test_the_engine_default_embeds_the_whole_small_sample():
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NoLLMClient(), _ctx())
  assert embedder.counts[0] == len(_ROWS)
  engine.teardown()


def test_a_store_written_with_a_larger_limit_serves_a_smaller_one():
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NoLLMClient(),
               _ctx(rag_row_docs_limit=10, chunk_store=_store_for(_ROWS[:30])))
  assert not embedder.counts, "the store covered the prefix: no embed"
  assert len(engine._ref_vectors) == 10
  engine.teardown()


def test_a_store_written_with_a_smaller_limit_is_never_mixed_in():
  embedder = _CountingEmbedder()
  engine = B1RagEngine(embedder=embedder)
  engine.setup(_NoLLMClient(),
               _ctx(rag_row_docs_limit=30, chunk_store=_store_for(_ROWS[:10])))
  assert embedder.counts[0] == 30, "a partial store re-embeds the prefix"
  engine.teardown()


# --- the store reuse rule ----------------------------------------------------


def test_population_plan_full_on_a_cold_digest():
  plan = plan_rag_population(InMemoryChunkStore(), "dig", _ROWS[:16], _EID,
                             _EVER)
  assert (plan.mode, plan.present, plan.missing) == ("full", None, 16)


def test_population_plan_skips_a_store_that_covers_the_prefix():
  plan = plan_rag_population(
      _store_for(_ROWS[:20]), "dig", _ROWS[:16], _EID, _EVER)
  assert plan.mode == "skip"


def test_population_plan_tops_up_a_store_written_with_a_smaller_limit():
  plan = plan_rag_population(
      _store_for(_ROWS[:10]), "dig", _ROWS[:16], _EID, _EVER)
  assert plan.mode == "topup"
  assert plan.missing == 6
  assert plan.present == {compute_row_digest(r) for r in _ROWS[:10]}


def test_another_embedder_does_not_count_as_coverage():
  plan = plan_rag_population(
      _store_for(_ROWS[:20]), "dig", _ROWS[:16], _EID, "v2")
  assert plan.mode == "full"


# --- the population branch ---------------------------------------------------


def _config(**kw) -> PipelineConfig:
  return PipelineConfig(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=_ROWS),
      num_rows=1,
      embedder_id=_EID,
      embedder_version=_EVER,
      **kw,
  )


def _populate(config: PipelineConfig, rows: list[dict], check) -> None:
  with TestPipeline() as p:
    chunks = _population_branch(p, config, rows, "dig", label_prefix="")
    _ = chunks | beam.combiners.ToList() | beam.Map(check)


_PROSE = [{
    "id": i,
    "status": "OPEN",
    "note": f"customer {i} wrote a long note about delivery {i % 3}"
} for i in range(12)]
_PROSE_SCHEMA = TableSchema.model_validate({
    "table_info": {
        "table_id": "p.d.t"
    },
    "schema": [
        {
            "name": "id",
            "type": "INT64",
            "mode": "REQUIRED"
        },
        {
            "name": "status",
            "type": "STRING",
            "mode": "REQUIRED"
        },
        {
            "name": "note",
            "type": "STRING",
            "mode": "NULLABLE"
        },
    ],
    "primary_keys": ["id"],
})


def test_the_population_branch_writes_the_limit_and_the_values():

  def check(out):
    kinds = [c["chunk_kind"] for c in out]
    assert kinds.count("row_doc") == 5
    assert kinds.count(CHUNK_KIND_FREE_TEXT_COL) > 0

  _populate(
      _config(table_schema=_PROSE_SCHEMA, rag_row_docs_limit=5), _PROSE, check)


def test_a_topup_writes_only_the_missing_rows_and_no_values():
  present = frozenset(compute_row_digest(r) for r in _PROSE[:4])

  def check(out):
    assert [c["chunk_kind"] for c in out] == ["row_doc"] * 6
    assert {c["row_digest"] for c in out
           } == {compute_row_digest(r) for r in _PROSE[4:10]}

  _populate(
      _config(
          table_schema=_PROSE_SCHEMA,
          rag_row_docs_limit=10,
          rag_present_row_digests=present), _PROSE, check)


# --- end to end: the knob reaches the worker ----------------------------------


def test_the_knob_reaches_the_worker_engine(tmp_path, customers_schema,
                                            customers_reference, caplog):
  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=4,
      batch_size=4,
      run_id="row-docs-thread",
      identity_columns=("customer_id",),
      rag_row_docs_limit=3,
  )
  with (
      caplog.at_level(logging.INFO, logger="sdfb.milestone"),
      beam.Pipeline(options=PipelineOptions(["--runner=DirectRunner"])) as p,
  ):
    build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "land")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  # The once-per-process plan log would otherwise stay "already logged"
  # for this fixture's digest and silence the next test that reads it.
  clear_generation_plan_log()
  embeds = [r.message for r in caplog.records if "b1_embed_done" in r.message]
  assert embeds and all(" rows=3 " in m for m in embeds), embeds
