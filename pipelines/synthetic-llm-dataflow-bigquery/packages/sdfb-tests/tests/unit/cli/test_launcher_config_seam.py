"""The launcher → PipelineConfig → GenerationContext wire (ADR 0032).

`_prepare_table_spec` is the one driver-side function no unit test can
call end to end (it reads BigQuery), so a kwarg it passes to
`PipelineConfig` that does not exist is invisible until a real launch —
which is exactly how `relationship_card` reached Dataflow and died with
`TypeError: PipelineConfig.__init__() got an unexpected keyword argument`
after a 7-minute image pull (2026-08-25, job …-11031703962997027597).

These two tests close that hole structurally: every kwarg the launcher
passes must be a real field, and a field that exists must actually reach
the workers. Both would have failed before that launch.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
from pathlib import Path

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from sdfb_beam.io.local_sinks import WriteToJsonLines
from sdfb_beam.pipeline import PipelineConfig, build_pipeline
from sdfb_tests.fakes import FakeModelClient

_RUN_PIPELINE = (
    Path(__file__).parents[5] /
    "packages/sdfb-beam/src/sdfb_beam/cli/run_pipeline.py")


def _pipeline_config_kwargs() -> set[str]:
  """Keyword names the launcher passes to `PipelineConfig(...)`."""
  tree = ast.parse(_RUN_PIPELINE.read_text(encoding="utf-8"))
  names: set[str] = set()
  for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
        node.func.id == "PipelineConfig"):
      names.update(kw.arg for kw in node.keywords if kw.arg)
  return names


def test_every_launcher_kwarg_is_a_real_pipeline_config_field():
  passed = _pipeline_config_kwargs()
  assert passed, "no PipelineConfig(...) call found — did it move?"
  fields = {f.name for f in dataclasses.fields(PipelineConfig)}
  unknown = sorted(passed - fields)
  assert not unknown, (
      f"the launcher passes {unknown} to PipelineConfig, which has no such "
      f"field — this only fails at launch, after the image pull")


def test_the_relationship_card_reaches_the_workers(tmp_path, customers_schema,
                                                   customers_reference, caplog):
  """A field that exists but is never threaded into the
    GenerationContext is worse than a crash: the worker log would just
    be missing the card, silently."""
  card = "RELATIONSHIP MODEL retail | source config/relationships/retail.yaml"
  config = PipelineConfig(
      table_schema=customers_schema,
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=customers_reference),
      num_rows=10,
      batch_size=10,
      run_id="card-thread",
      landing_table="p.land.customers",
      identity_columns=("customer_id",),
      relationship_card=card,
  )
  options = PipelineOptions(["--runner=DirectRunner"])
  with (
      caplog.at_level(logging.INFO, logger="sdfb.milestone"),
      beam.Pipeline(options=options) as p,
  ):
    build_pipeline(
        p,
        reference_rows=customers_reference,
        config=config,
        landing_sink=WriteToJsonLines(str(tmp_path / "land")),
        dlq_sink=WriteToJsonLines(str(tmp_path / "dlq")),
    )
  assert "name=relationship_model" in caplog.text
  assert card in caplog.text
