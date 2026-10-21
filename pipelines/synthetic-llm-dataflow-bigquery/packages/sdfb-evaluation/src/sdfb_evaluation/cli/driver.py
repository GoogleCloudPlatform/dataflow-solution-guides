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
"""One evaluation, driven end to end: the registry events around the
pipeline (D7), and who writes the one terminal row.

    mint evaluation_id  eval-<UTC yyyymmddThhmmssZ>-<8 hex>, fresh per
          │             attempt (temp tables carry it and collide for 24 h)
          ▼
    resolve the launch ─► load the models ─► build_plan
          │      any exception: no plan exists, so a FAILED row is built
          │      from the arguments (`planning_failed_row`), then re-raised
          ▼
    RUNNING row         the driver's own events are a BigQuery load job,
          ▼             or — `--sink local_json` — a shard in the
    prepare_evaluation  evaluation's own directory
          ▼
    read path           DIRECT_READ, or the paged read when BigQuery
          ▼             refuses a read session ("The read path")
    pipeline(options from the PREPARED plan) ─► submit ─► wait
          │             on Dataflow the panel is staged beside the graph
          │             while the graph is built ("The panel")
          │  bq         the pipeline writes its rows and the FINAL row
          │  bq_client  local NDJSON, then one load job per table, the
          │             registry's FINAL row last
          │  local_json local NDJSON only
          ▼
    read the FINAL row back ─► exit code (`cli.gate`)

Never two terminal rows: the one there is, is written by whoever owns
the outcome, and by nobody while that is not known (Rulings R93-3,
R98-1):

    what happened                                 terminal row
    ────────────────────────────────────────────  ─────────────────────────
    the run completed                             the pipeline's FINAL
    planning, RUNNING, prepare, graph             the driver's FAILED: no
      construction or submission raised           job is running
    the job ended in a state other than DONE      the driver's FAILED: the
      (it failed, it was cancelled) and the       job will write nothing
      registry, read back, holds no FINAL row
    the job ended in a state other than DONE      the pipeline's FINAL: it
      after it loaded its FINAL row (cancelled    is there already, so the
      late)                                       driver appends nothing
    the runner built the job and did not submit   NOBODY's yet: the
      it (`--template_location`, a flex-          launcher submits the job,
      template launch)                            which writes its own
    the job ended DONE and the registry, read     the driver's FAILED ("the
      back, holds no FINAL row                    job finished without a
                                                  FINAL row")
    an interrupt or a polling error while a       NOBODY's yet: the job
      submitted job is still running              goes on and writes its
                                                  own; it is never
                                                  cancelled
    the wait raised and the run, looked at        the pipeline's FINAL
      again, is DONE (Ruling R98-1)               (the run completed): a
                                                  polling error is a
                                                  warning and the driver
                                                  reads the row back as
                                                  usual; an interrupt
                                                  writes nothing and says
                                                  how to read the result
    the read-back itself failed                   none: a FINAL row may
                                                  well exist

A submitted job is one that outlives the driver: its result names a job
id (Dataflow). While it is not known to be terminal — an unreadable
state counts as running — the driver writes no terminal row: it prints
the job id and how to read the result later, and re-raises. A local run
(no job id) dies with the driver, so the driver closes it. A submitted
job that ended in a state other than DONE may have loaded its FINAL row
first, so the driver reads the registry back before it appends FAILED
and appends nothing when a FINAL row is there, or when the read-back
itself fails (Ruling R113).

A template launch hands the job over instead of submitting it: with
`--template_location` — which is how a flex-template launcher runs the
entry — Beam's DataflowRunner returns a result with no job (`has_job`
false, and a `job_id()` that raises). There is no job id and nothing to
wait for: the driver leaves its RUNNING row for the job to close and
ends 0, whether or not it was asked to wait (Ruling R113). Two
contradicting terminal rows are worse than a RUNNING row that stays open
(an orchestrator's failure callback closes that one). For the same
reason a run found DONE is never closed by the driver on the strength of
a failed wait: FAILED is appended to a DONE run only after a read-back
that worked and found no FINAL row.

Every exception is re-raised after its row, never replaced; a FAILED row
that cannot itself be written is reported on stderr and the original
error still surfaces. `sdfb-eval run` waits, so its exit code can carry
the `--fail_on` gate; the flex-template entry submits the Dataflow job
and returns.

Not every FAILED comes with an exception: when no launch table could be
evaluated the pipeline itself writes a FINAL row reading FAILED and ends
normally. The driver reads the FINAL row back anyway — the gate needs
its counts — and a FAILED one exits 3 (`cli.gate.final_exit_code`), with
no second row.

Runner defaults: the DirectRunner evaluates samples and loads through
the client (`--mode sampled --sink bq_client`); Dataflow reads every row
and writes from the pipeline (`--mode exact --sink bq`). The pipeline
options come from `pipeline_options_defaults(runner, prepared plan)` —
the side-input cache is sized from the plan that will actually run, and
the RUNNER is the one it returns: `--runner DirectRunner` is what the
operator says and the registry records, while the pipeline runs on
Beam's in-process `FnApiRunner` (Beam's own `DirectRunner` hands a batch
pipeline to Prism, which `build_evaluation_pipeline` refuses). Every
`PipelineOptions` here is built over the explicit list of Beam arguments
the operator gave, never over `sys.argv` — with those arguments
underneath the defaults: their experiments are
kept, `upload_graph` is always among them on Dataflow, and
`enable_data_sampling` is refused (it would sample the label key into
the monitoring UI, Ruling R68).

The label key is never read here: `--label_key_uri` travels into the
plan, a worker resolves it, the registry records only `operator` or
`ephemeral`, and nothing this module prints or writes names the URI (an
error message that quotes it is redacted before the registry's
1,000-character cut). A reason that is cut keeps, after its beginning,
the line on which Beam names the failing step (else its last line).

`--thresholds_uri` (`cli.thresholds`, Ruling R93-6): the overrides the
file held arrive validated in `args.thresholds`. They go to the
pipeline, which grades and stores every metric row under them, and the
plan — so every registry row, a FAILED one included — records the URI
(a `gs://` one as given, any other only as its file's base name) and the
overrides' digest in `evaluation_params`. The gate is unchanged:
it reads the registry's counts, which are counts of the stored rows.

The free-text pools (`field.pool_memorization_lift`) are read here, per
table, from the launch's `freetext_pools_table`. A read BigQuery REFUSES
(`context.bq.is_refusal`) degrades with a warning; a transient error
fails the run, as in `prepare_evaluation` (Ruling R89, M8).

The read path. The pipeline reads the source and synthetic rows by
DIRECT_READ (the BigQuery Storage Read API), which needs
`bigquery.readsessions.create` on the project of every table it reads.
A deployment may not grant it, and a worker that is refused fails the
job four attempts later. So, once the prepare statements have built the
read tables and before the graph is built, the driver asks the API for
one read session per project of the planned read tables
(`beam.io.probe_direct_read`: the call a worker makes, with no row
read):

    accepted by a project       its read tables keep DIRECT_READ
    refused (a 403) by a        its read tables, and only they, are read
      project                   through the BigQuery client's
                                `tabledata.list`
                                (`beam.io.RoutedBigQuerySources`: a read
                                table is paged if and only if its
                                project refused), and one WARNING says
                                so: per refusing project, why, the table
                                sides it pages with their rows and
                                bytes, the quota that bounds them and
                                the role that restores the fast read;
                                the same text joins the plan's warnings,
                                so the registry's FINAL (or FAILED) row
                                has it
    anything else               raised: the driver's FAILED row. A
                                transient error never silently changes
                                how the evaluation reads

A run that reads nothing from BigQuery (a fixture, sources a test put
in) is not probed. The probe's answer is the workers' answer when the
driver runs as the account they run as: a Flex Template launcher does,
and so does a local runner.

The panel. What the driver built from the reference panel (the R and H
rows, the census's views of them, each table's panel hashes and privacy
inputs) has to reach the workers. A local run embeds it in the job
graph. A Dataflow run does not (Ruling R154): a graph with the panel in
it weighed 44 MB for five invented tables at 10,000 reference rows
(a laptop measurement), and `pipeline.run()` serialises the graph,
rebuilds the pipeline from it and serialises it again inside a
launcher's twelve minutes. So for a
Dataflow run (`is_dataflow`) the driver hands the graph a staging root,
`<temp_location>/sdfb-evaluation/<evaluation_id>/panel/`
(`beam.staged.panel_root`), and each payload is written there as one
object while the graph is built; the graph carries the object's name,
digest and element count. The temp location is the one Beam settled on,
read from the PIPELINE's options, never from the options the driver
built: `beam.Pipeline` deep-copies what it is given and validates its
copy, and for a Dataflow pipeline that is where a missing or invalid
`--temp_location` becomes the staging location (or a default bucket).
The driver's own options would say None for a launch that passes
`--staging_location` alone. A write that fails raises before anything is
submitted: the driver's FAILED row. One line says what was staged,
`evaluation <id>: panel staging: <n> objects, <size>, <s>s (<w>s in the
writes)`: `<s>` is everything staging took (pickling, hashing, writing)
and `<w>` the part spent writing to storage. Both are inside the `graph`
step's seconds. No flag turns it on or off.

The launch is timed step by step (`_timed`): the submission's own line
is the sum of the four before it (the free-text pools read, the pipeline
options, the graph, `pipeline.run()`), and while the pipeline is handed
to its runner Beam's own lines about its uploads to Cloud Storage are
relayed, because `run()` cannot be timed from outside beyond its total.

`--sink bq_client` without `--output_local` writes under a temporary
directory: removed when the run ends without an error, kept (its path
printed once on stderr) when it failed after writing something.

`plan` builds the same plan and stops. Planning itself creates one kind
of table (Ruling R57): the zero-byte, 24-hour start snapshot of an
`as_of_diff` scope, because the scope's reads go through it. With
`--no_planning_snapshots` the planner is handed a BigQuery that refuses
every DDL statement, so nothing is created and each such table is
planned as scope `unknown` with that reason — unplanned.

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §4 Relational generation
(ADR 0038).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import apache_beam as beam
from apache_beam.options.pipeline_options import (
    DebugOptions,
    GoogleCloudOptions,
    PipelineOptions,
    WorkerOptions,
)
from apache_beam.runners.runner import PipelineState

from sdfb_evaluation.beam.assemble import failed_row, running_row
from sdfb_evaluation.beam.census import FreeTextPool, read_pools
from sdfb_evaluation.beam.io import (
    BigQuerySinks,
    BigQuerySources,
    ClientLoadSinks,
    LocalJsonSinks,
    RoutedBigQuerySources,
    Sinks,
    Sources,
    probe_direct_read,
)
from sdfb_evaluation.beam.label_key import label_key_mode
from sdfb_evaluation.beam.pipeline import (
    build_evaluation_pipeline,
    pipeline_options_defaults,
    prepare_evaluation,
)
from sdfb_evaluation.beam.staged import Staging, panel_root
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.cli.fixture import Fixture, load_fixture
from sdfb_evaluation.cli.gate import EXIT_FAILED, final_exit_code
from sdfb_evaluation.cli.thresholds import thresholds_digest
from sdfb_evaluation.context.bq import (
    Bq,
    BqApiError,
    is_refusal,
    normalize_fqn,
)
from sdfb_evaluation.context.gcp import make_session
from sdfb_evaluation.context.launch import LaunchContext, resolve_launch
from sdfb_evaluation.context.plan import (
    EvaluationPlan,
    Knobs,
    TablePlan,
    build_plan,
    evaluation_key,
)
from sdfb_evaluation.context.relationships import (
    RelationshipError,
    RelModel,
    component,
    generation_order,
    load_models,
)
from sdfb_evaluation.context.runs import runs_for
from sdfb_evaluation.report.store import (
    REGISTRY,
    NoSuchEvaluationError,
    read_bq,
)
from sdfb_evaluation.schemas import load_schema
from sdfb_evaluation.types import Side
from sdfb_evaluation.version import EVALUATOR_VERSION

__all__ = [
    "EVALUATION_ID_RE",
    "SINKS",
    "Env",
    "forbidden_experiments",
    "knobs_from_args",
    "launch_request",
    "mint_evaluation_id",
    "pipeline_options",
    "plan",
    "planning_failed_row",
    "read_sides",
    "run",
    "runner_defaults",
    "submit_pipeline",
]

EVALUATION_ID_RE = re.compile(r"eval-\d{8}T\d{6}Z-[0-9a-f]{8}")
SINKS = ("bq", "bq_client", "local_json")
_TOKEN_RE = re.compile(r"[0-9a-f]{8}")
_TOKEN_BYTES = 4
_FORBIDDEN_EXPERIMENT = "enable_data_sampling"
_WORKER_IMAGE_ENV = "SDFB_EVAL_SDK_CONTAINER_IMAGE"
_LOGGER = logging.getLogger(__name__)


@contextlib.contextmanager
def _timed(evaluation_id: str, step: str) -> Iterator[None]:
  """Log one finished step of the launch around planning (the lookup
  before it, the registry row, the prepare statements, the read path and
  the submission's steps after it) and its seconds, in the planner's
  line shape. The `submission` line is the sum of the four before it."""
  began = time.monotonic()
  try:
    yield
  finally:
    _LOGGER.info("evaluation %s: %s %.2fs", evaluation_id, step,
                 time.monotonic() - began)


# Beam's Dataflow client, and the lines it logs while `pipeline.run()`
# stages the job: the only view from outside of where that call's time
# goes (module docstring). Nothing else it logs is relayed — its "Create
# job" line prints the whole job.
_BEAM_SUBMIT_LOGGER = "apache_beam.runners.dataflow.internal.apiclient"
_BEAM_SUBMIT_LINES = ("Starting GCS upload to ", "Completed GCS upload to ",
                      "A template was just created at location ")


class _RelayUploads(logging.Filter):
  """On Beam's Dataflow client logger while a pipeline is handed over:
  relays `_BEAM_SUBMIT_LINES` as this module's own INFO lines and lets
  through only what the logger let through before (`threshold`, its
  effective level then), so no other handler sees a line it did not see
  before."""

  def __init__(self, evaluation_id: str, threshold: int):
    super().__init__()
    self._evaluation_id = evaluation_id
    self._threshold = threshold

  def filter(self, record: logging.LogRecord) -> bool:
    try:
      message = record.getMessage()
    except Exception:  # pylint: disable=broad-exception-caught  # a line Beam formatted badly must not stop the launch
      message = ""
    if record.levelno == logging.INFO and message.startswith(
        _BEAM_SUBMIT_LINES):
      _LOGGER.info("evaluation %s: pipeline run: %s", self._evaluation_id,
                   message)
    return record.levelno >= self._threshold


@contextlib.contextmanager
def _relayed_uploads(evaluation_id: str) -> Iterator[None]:
  """While the pipeline is handed to its runner, relay Beam's upload
  lines. Beam's logger is opened to INFO for that time, with a filter
  that keeps every other line where it was; both are undone on the way
  out, whatever happened."""
  beam_logger = logging.getLogger(_BEAM_SUBMIT_LOGGER)
  level = beam_logger.level
  relay = _RelayUploads(evaluation_id, beam_logger.getEffectiveLevel())
  beam_logger.addFilter(relay)
  if not beam_logger.isEnabledFor(logging.INFO):
    beam_logger.setLevel(logging.INFO)
  try:
    yield
  finally:
    beam_logger.setLevel(level)
    beam_logger.removeFilter(relay)


_DATAFLOW = "dataflow"
_LOCAL = "local_json"
_FINAL = "FINAL"
_PARTIAL = "PARTIAL"
_SKIPPED = "SKIPPED"
_REDACTED = "<label key uri>"
_CUT = " […] "
_WHILE_RUNNING = "[while running "
_COUNT_KEYS = ("total", "pass", "warn", "fail", "info", "not_evaluated")
_SCORE_KEYS = ("overall", "fidelity", "privacy", "integrity", "diversity")
_BQ_ERRORS = (BqApiError, PermissionError, LookupError)
_NO_FINAL = "the job finished without a FINAL row"
_HTTP_FORBIDDEN = 403
_READ_SESSION_ROLE = "roles/bigquery.readSessionUser"
_ERROR_CHARS = 300
_SIZE_UNITS = (("GB", 10**9), ("MB", 10**6), ("kB", 10**3))
_NO_SNAPSHOTS = (
    "--no_planning_snapshots: planning creates no table, so this as_of_diff "
    "scope has no start snapshot to read; plan or run without the flag to "
    "evaluate it")


def _utc_now() -> datetime:
  return datetime.now(UTC)


def _token() -> str:
  return secrets.token_hex(_TOKEN_BYTES)


def _default_bq(project: str) -> Any:
  return Bq(project)


def _pipeline(options: PipelineOptions) -> beam.Pipeline:
  return beam.Pipeline(options=options)


def submit_pipeline(pipeline: beam.Pipeline) -> Any:
  """Hand `pipeline` to its runner and return the runner's result. A
  local runner has run it to its end by then (an error in it is raised
  here); Dataflow has only accepted the job."""
  return pipeline.run()


def _handed_over(result: Any) -> bool:
  """Whether the runner built the job without submitting it: Beam's
  DataflowRunner returns a result with no job (`has_job` false) when
  `--template_location` is set, which is how a flex-template launcher
  runs the entry. The launcher submits the job; nothing ran here and
  there is nothing to wait for. A local result has no `has_job`."""
  return getattr(result, "has_job", True) is False


def _job_id(result: Any) -> str | None:
  """The id of the job `result` stands for, when the runner submitted
  one that goes on without the driver (Dataflow); None for a local run
  and for a job handed over as a template (`_handed_over`: its
  `job_id()` raises)."""
  if _handed_over(result):
    return None
  job_id = getattr(result, "job_id", None)
  value = job_id() if callable(job_id) else job_id
  return str(value) if value else None


def _last_state(result: Any) -> Any:
  """The state the runner reports for `result` now, or None when it
  cannot say (which is not "terminal")."""
  try:
    return result.state
  except Exception:  # pylint: disable=broad-exception-caught  # an unreachable runner must read as "unknown", whatever it raised
    return None


def _still_running(job: str | None, state: Any) -> bool:
  """Whether a submitted job may still be running: it has a job id and
  its last known state is not terminal."""
  return job is not None and not PipelineState.is_terminal(state)


@dataclass
class Env:
  """What the CLI reaches the outside world through (tests replace it):
  the BigQuery client of a project, the REST session factory (None:
  Application Default Credentials, made when a job id is resolved), the
  clock and the random half of an evaluation id, the pipeline's sources,
  how a pipeline is made from its options (on Dataflow, Beam validates
  them against Cloud Storage right there) and how a built one is handed
  to its runner (the result: `wait_until_finish()`, `state` and, for a
  submitted job, `job_id()`). `probe_read(read table, columns)` asks the
  Storage Read API for one read session and raises what it raised
  (module docstring, The read path); it is called only when
  `make_sources` gives the DIRECT_READ sources."""
  make_bq: Callable[[str], Any] = _default_bq
  session_factory: Callable[[str], Any] | None = None
  now: Callable[[], datetime] = _utc_now
  token: Callable[[], str] = _token
  make_sources: Callable[[], Sources] = BigQuerySources
  probe_read: Callable[[str, Sequence[str]], None] = probe_direct_read
  make_pipeline: Callable[[PipelineOptions], beam.Pipeline] = _pipeline
  submit: Callable[[beam.Pipeline], Any] = submit_pipeline


def mint_evaluation_id(now: datetime, token: str) -> str:
  """`eval-<UTC yyyymmddThhmmssZ>-<8 hex>` (Ruling R88c): the time makes
  ids sort, the random half keeps two attempts of one second apart.

  Raises:
    ValueError: `token` is not 8 lowercase hex digits.
  """
  if _TOKEN_RE.fullmatch(token) is None:
    raise ValueError(f"evaluation id token {token!r}: expected 8 hex digits")
  stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
  return f"eval-{stamp}-{token}"


def is_dataflow(runner: str) -> bool:
  return _DATAFLOW in runner.lower()


def runner_defaults(runner: str) -> tuple[str, str]:
  """(`--mode`, `--sink`) when the flags are not given (module
  docstring)."""
  return ("exact", "bq") if is_dataflow(runner) else ("sampled", "bq_client")


def qualified_dataset(dataset: str, project: str | None) -> str:
  """`project.dataset` from `dataset` or `project.dataset` (also
  `project:dataset`)."""
  text = dataset.replace(":", ".", 1)
  return text if "." in text else f"{project}.{text}"


def knobs_from_args(args: argparse.Namespace, evaluation_id: str) -> Knobs:
  """The plan's knobs from the planning flags.

  Raises:
    ValueError: a value `Knobs` refuses (the message names the knob).
  """
  temp = args.temp_dataset
  return Knobs(
      sample_rows=args.sample_rows,
      privacy_sample_rows=args.privacy_sample_rows,
      detection_sample_rows=args.detection_sample_rows,
      pair_max_columns=args.pair_max_columns,
      row_flags_top_k=args.row_flags_top_k,
      row_flags_source_keys=args.row_flags_source_keys,
      max_bytes_billed=args.max_bytes_billed,
      max_shuffle_gb=args.max_shuffle_gb,
      scope=args.scope,
      allow_contaminated=args.allow_contaminated,
      output_dataset=args.output_dataset,
      temp_dataset=qualified_dataset(temp, args.project) if temp else None,
      evaluation_id=evaluation_id)


# --------------------------------------------------------------------------
# the launch and the plan
# --------------------------------------------------------------------------
def launch_request(
    args: argparse.Namespace) -> tuple[str | None, dict[str, Any] | None]:
  """(`job_id`, `manual`) for `context.launch.resolve_launch` from the
  target flags: `--job_id`, or the tables of `--tables` in
  `--landing_dataset` with their sources in `--reference_dataset`
  (`reference_table` names the first one: the planner takes its dataset
  for every table), plus `--relationships_uri`, `--run_id` and
  `--reference_rows_limit` (the generation's reference sample size, which
  decides the privacy panel) and `--validation_runs_table` (whose rows verify
  it) wherever they are given. Manual values only
  fill what the launch's own records leave open."""
  manual: dict[str, Any] = {}
  params: dict[str, Any] = {}
  if args.relationships_uri:
    manual["relationships_uri"] = args.relationships_uri
    params["relationships_uri"] = args.relationships_uri
  if args.run_id:
    manual["base_run_id"] = args.run_id
    params["run_id"] = args.run_id
  if getattr(args, "reference_rows_limit", None):
    params["reference_rows_limit"] = args.reference_rows_limit
  if getattr(args, "validation_runs_table", None):
    params["validation_runs_table"] = args.validation_runs_table
  if args.tables:
    landing = qualified_dataset(args.landing_dataset, args.project)
    manual["tables_in_order"] = [f"{landing}.{name}" for name in args.tables]
    reference = qualified_dataset(args.reference_dataset, args.project)
    manual["reference_table"] = f"{reference}.{args.tables[0]}"
  if params:
    manual["params"] = params
  return args.job_id, (manual or None)


class TargetCheckError(ValueError):
  """The tables a target names cannot be evaluated: a name is not a table
  name, or none of the tables can be read. The message lists every
  problem, so one run shows them all."""


def derived_tables(models: Sequence[RelModel]) -> list[str]:
  """The ENABLED tables of `models`, parents first (the order the
  generation ran them): what `--relationships_uri` alone evaluates."""
  enabled = tuple(name for model in models
                  for name, rel in model.tables.items() if rel.enabled)
  return list(generation_order(models, enabled))


def _table_problems(bq: Any, name: str, landing: str, source: str) -> list[str]:
  """Why one landing/source pair cannot be read, as sentences. An empty
  table or a column one side lacks is not a problem here: planning notes
  the first and compares the columns both sides have."""
  problems: list[str] = []
  for role, fqn in (("landing", landing), ("source", source)):
    try:
      bq.table(fqn)
    except (PermissionError, LookupError, BqApiError) as exc:
      problems.append(f"{name}: the {role} table {fqn} could not be read "
                      f"({exc})")
  return problems


def _checked_names(args: argparse.Namespace, names: Sequence[str], landing: str,
                   reference: str) -> None:
  """Every name is a table name (it is about to go into SQL).

  Raises:
    TargetCheckError: one is not, naming it."""
  flag = "--seed_table" if getattr(args, "seed_table", None) else "the model"
  for name in names:
    try:
      if "." in name:
        raise ValueError("a bare table name, not a qualified one")
      normalize_fqn(f"{landing}.{name}")
      normalize_fqn(f"{reference}.{name}")
    except ValueError as exc:
      raise TargetCheckError(f"{flag} {name!r} is not a table name: "
                             f"{exc}") from exc


def _seed_tables(args: argparse.Namespace) -> tuple[list[str], list[str]]:
  """(tables, notes) of `--seed_table`: what a launch of that table
  generated (the generator's rule, `relationships.component`) — its
  enabled component, parents first, or the table alone when no model names
  it, the models are off, or the URI holds no model file. That last case
  clears `args.relationships_uri` (the plan is made without one) and says
  so in a note; any other model error raises."""
  seed = args.seed_table
  if not args.relationships_uri:
    return [seed], []
  try:
    models = load_models(args.relationships_uri)
  except RelationshipError as exc:
    if _NO_MODEL_FILES not in str(exc):
      raise
    note = (f"relationships_uri {args.relationships_uri} holds no model "
            f"file: {seed} was evaluated alone, as a launch generates a "
            "table no model names")
    args.relationships_uri = None
    return [seed], [note]
  return list(generation_order(models, component(models, seed))), []


def _derive_targets(args: argparse.Namespace, bq: Any) -> list[str]:
  """`--seed_table`, or `--relationships_uri` alone, names the tables
  (module docstring): sets `args.tables` — the seed's component
  (`_seed_tables`), or all the model's enabled tables, parents first —
  after reading each landing table and its source twin in
  `--reference_dataset`. Any other target is left alone. Returns the notes
  the plan should carry: the seed's, and a sentence for every table that
  could not be read, which planning then skips on its own (R61) while the
  others are evaluated.

  Raises:
    RelationshipError: the URI holds no usable model (for `--seed_table`
      only a model that is malformed).
    TargetCheckError: a name is not a table name, the model enables no
      table, or no table can be read (every problem is listed).
  """
  seed = getattr(args, "seed_table", None)
  # a job id is a target of its own, unless it accompanies the seed
  if (args.tables or (args.job_id and not seed) or args.run_id or
      args.fixture_dir or not (seed or args.relationships_uri)):
    return []
  if seed:
    names, notes = _seed_tables(args)
  else:
    names, notes = derived_tables(load_models(args.relationships_uri)), []
  if not names:
    raise TargetCheckError(
        f"{args.relationships_uri}: the relationship model enables no table")
  landing = qualified_dataset(args.landing_dataset, args.project)
  reference = qualified_dataset(args.reference_dataset, args.project)
  _checked_names(args, names, landing, reference)
  found = {
      name:
          _table_problems(bq, name, f"{landing}.{name}", f"{reference}.{name}")
      for name in names
  }
  problems = [problem for listed in found.values() for problem in listed]
  if all(found.values()):
    raise TargetCheckError("none of the tables of the target can be read: " +
                           "; ".join(problems))
  args.tables = names
  return [*notes, *problems]


def _resolve(args: argparse.Namespace, env: Env, bq: Any) -> LaunchContext:
  job_id, manual = launch_request(args)
  if args.run_id and not args.job_id and not args.tables:
    # no job to ask: the launch is what validation_runs recorded under the
    # run id, in the generator's quality dataset (--output_dataset)
    runs = runs_for(
        bq,
        quality_dataset=qualified_dataset(args.output_dataset, args.project),
        landing_tables=[],
        window=(None, None),
        base_run_id=args.run_id)
    return LaunchContext.from_sources(
        job=None, launch_config=None, writes=(), manual=manual, runs=runs)
  factory = env.session_factory
  if job_id is not None and factory is None:
    factory = make_session
  return resolve_launch(
      bq=bq,
      session_factory=factory,
      project=args.project,
      region=args.region or "",
      job_id=job_id,
      manual=manual,
      # the job is the identity of a launch the seed names: read nothing
      # else about it (no log, no BigQuery labels, no validation_runs)
      job_only=bool(job_id and getattr(args, "seed_table", None)))


# `context.relationships.load_models` raises this sentence when a folder holds
# no model file (samples excluded); the test of the driver pins it.
_NO_MODEL_FILES = "no model files there"


def _models(
    launch: LaunchContext,
    *,
    explicit_uri: str | None = None
) -> tuple[tuple[RelModel, ...], LaunchContext]:
  """The model the launch applied, and the launch to plan with: the
  adjusted copy when it adjusted one (ADR 0038), else the one it loaded;
  none when relationships were off.

  The evaluator follows what the generation did, and what decides is
  where the URI came from and whether the launch's log was read (R121):

  - the URI is the operator's own `--relationships_uri` (`explicit_uri`)
    and it resolves to no model file: raises, always;
  - the URI is the generation job's record and the log was read and shows
    no model loaded (the generator logged `relationships_absent`): the
    launch is evaluated without relationships, with a note;
  - the same with the log unread (`model_adjusted` is None exactly when no
    milestones were read): evaluated without relationships, the note says
    that whether a model was loaded is unknown;
  - the record shows a model WAS loaded and its files cannot be found:
    raises, naming the model, its sha and the URI.

  An adjusted model's errors are never softened."""
  adjusted = launch.adjusted_model_uri if launch.model_adjusted else None
  uri = adjusted or launch.relationships_uri
  if not uri:
    return (), launch
  try:
    return tuple(load_models(uri)), launch
  except RelationshipError as exc:
    if adjusted or _NO_MODEL_FILES not in str(exc) or (explicit_uri and
                                                       explicit_uri == uri):
      raise
    if launch.model_name or launch.model_sha:
      name, sha = launch.model_name or "?", launch.model_sha or "?"
      raise RelationshipError(
          f"{exc}. The launch's log shows it loaded the relationship model "
          f"{name} (sha {sha}), which is not readable from where the "
          "evaluator runs") from exc
  if launch.model_adjusted is None:
    note = (f"the launch's log could not be read, so whether it loaded a "
            f"relationship model is unknown, and the recorded "
            f"relationships_uri {uri} holds no model file where the "
            "evaluator runs: evaluated without relationships")
  else:
    note = (f"the launch recorded relationships_uri {uri} but its log shows "
            "it loaded no relationship model (there was no model file "
            "there): evaluated without relationships, as the generation ran")
  return (), dataclasses.replace(
      launch, relationships_uri=None, warnings=(*launch.warnings, note))


class _ReadOnlyPlanning:
  """A `Bq` for a planning that may create nothing
  (`--no_planning_snapshots`): every read goes to the real client, every
  DDL statement is refused and recorded in `refused`. The planner treats
  the refusal as it treats BigQuery's own: that table's scope is
  `unknown` with the reason, the other tables are still planned."""

  def __init__(self, bq: Any):
    self._bq = bq
    self.refused: list[str] = []

  def __getattr__(self, name: str) -> Any:
    return getattr(self._bq, name)

  def execute(self,
              sql: str,
              params: Mapping[str, Any] | None = None,
              *,
              max_bytes: int | None = None) -> None:
    del params, max_bytes
    self.refused.append(sql)
    raise PermissionError(_NO_SNAPSHOTS)


@dataclass
class _Attempt:
  """One attempt's identity, and what planning learned before it failed
  or finished."""
  evaluation_id: str
  now: datetime
  launch: LaunchContext | None = None
  refused: list[str] = field(default_factory=list)


def _make_plan(args: argparse.Namespace, env: Env, bq: Any,
               fixture: Fixture | None, attempt: _Attempt) -> EvaluationPlan:
  knobs = knobs_from_args(args, attempt.evaluation_id)
  trigger = getattr(args, "trigger", "cli")
  if fixture is not None:
    return fixture.plan(
        knobs=knobs,
        mode=args.mode,
        trigger=trigger,
        runner=args.runner,
        now=attempt.now,
        evaluation_id=attempt.evaluation_id)
  with _timed(attempt.evaluation_id, "target check"):
    notes = _derive_targets(args, bq)
  with _timed(attempt.evaluation_id, "launch lookup"):
    attempt.launch = _resolve(args, env, bq)
  if notes:
    attempt.launch = dataclasses.replace(
        attempt.launch, warnings=(*attempt.launch.warnings, *notes))
  planning_bq = bq
  if getattr(args, "no_planning_snapshots", False):
    planning_bq = _ReadOnlyPlanning(bq)
    attempt.refused = planning_bq.refused
  with _timed(attempt.evaluation_id, "relationship models"):
    models, attempt.launch = _models(
        attempt.launch, explicit_uri=getattr(args, "relationships_uri", None))
  return build_plan(
      launch=attempt.launch,
      models=models,
      bq=planning_bq,
      knobs=knobs,
      mode=args.mode,
      trigger=trigger,
      runner=args.runner,
      now=attempt.now)


def plan(args: argparse.Namespace,
         env: Env | None = None) -> tuple[EvaluationPlan, frozenset[str]]:
  """`sdfb-eval plan`: the plan of the target, never run (module
  docstring). Returns it with the landing tables left unplanned by
  `--no_planning_snapshots`. Nothing is written to the registry; a
  planning error naming DDL it had already run says so on stderr before
  it propagates."""
  env = env or Env()
  now = env.now()
  attempt = _Attempt(mint_evaluation_id(now, env.token()), now)
  fixture = load_fixture(args.fixture_dir) if args.fixture_dir else None
  bq = None if fixture is not None else env.make_bq(args.project)
  try:
    planned = _make_plan(args, env, bq, fixture, attempt)
  except Exception as exc:
    for statement in getattr(exc, "planning_ddl", ()) or ():
      print(
          f"sdfb-eval: planning had already run: {statement.sql}",
          file=sys.stderr)
    raise
  refused = set(attempt.refused)
  unplanned = frozenset(
      t.landing_table
      for t in planned.tables
      if not t.evaluated and refused & set(t.scope.planning_sql))
  return planned, unplanned


def _request_key(args: argparse.Namespace, knobs: Knobs) -> str:
  """The key of a request whose launch never resolved: the same recipe
  over the target as it was asked for."""
  request = {
      "job_id": args.job_id,
      "run_id": args.run_id,
      "tables": list(args.tables or ()),
      "landing_dataset": args.landing_dataset,
      "reference_dataset": args.reference_dataset,
      "relationships_uri": args.relationships_uri,
      "fixture_dir": args.fixture_dir,
  }
  # absent unless given: the key of every earlier request is unchanged
  if getattr(args, "seed_table", None):
    request["seed_table"] = args.seed_table
  payload = {
      "unresolved_request": request,
      "catalogue_version": load_catalogue().version,
      "evaluator_version": EVALUATOR_VERSION,
      "mode": args.mode,
      "knobs": knobs.key_dict(),
  }
  text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
  return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def _rfc3339(moment: datetime) -> str:
  return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _override(args: argparse.Namespace) -> tuple[str | None, str | None]:
  """What a plan records of `--thresholds_uri` (module docstring): the
  URI and the digest of the overrides it held, (None, None) without it.
  A `gs://` URI is recorded as given; any other value only as its file's
  base name, so a home-directory path never reaches BigQuery (the digest
  identifies the content)."""
  uri = getattr(args, "thresholds_uri", None)
  if not uri:
    return None, None
  held = getattr(args, "thresholds", None) or {}
  uri = str(uri)
  recorded = uri if uri.startswith("gs://") else os.path.basename(uri)
  return recorded, thresholds_digest(held)


def planning_failed_row(args: argparse.Namespace,
                        *,
                        evaluation_id: str,
                        evaluated_at: datetime,
                        exc: BaseException,
                        launch: LaunchContext | None = None,
                        project: str | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
  """The FINAL FAILED registry row of an attempt that never got a plan
  (Ruling R88b), in schema order: `assemble.failed_row` over a plan with
  no tables, built from the arguments — the mode, trigger, runner and
  knobs asked for, the launch when it had resolved (else only what the
  target flags name), the error as the reason and, in the warnings, any
  DDL planning had already run. What no plan measured stays NULL. A
  resolved launch gets the key its plan would have had
  (`context.plan.evaluation_key`); an unresolved one a key of the request
  as it was asked."""
  knobs = knobs_from_args(args, evaluation_id)
  thresholds_uri, digest = _override(args)
  resolved = launch is not None
  if launch is None:
    launch = LaunchContext(
        generation_job_id=args.job_id,
        job_name=None,
        region=args.region if args.job_id else None,
        started_at=None,
        finished_at=None,
        base_run_id=args.run_id,
        run_ids=(),
        tables_in_order=(),
        reference_table=None,
        write_disposition=None,
        relationships_uri=args.relationships_uri,
        params={},
        model_sha=None,
        model_name=None,
        adjusted_model_uri=None,
        params_source="manual",
        writes=())
  try:
    key = (
        evaluation_key(launch, args.mode, knobs) if resolved else _request_key(
            args, knobs))
  except ValueError:  # the malformed table name may be the failure itself
    key = _request_key(args, knobs)
  stub = EvaluationPlan(
      evaluation_id=evaluation_id,
      evaluation_key=key,
      evaluated_at=_rfc3339(evaluated_at),
      salt="",
      mode=args.mode,
      trigger=args.trigger,
      runner=args.runner,
      launch=launch,
      models=(),
      model_sha=None,
      tables=(),
      knobs=knobs,
      prepare_sql=(),
      bq_bytes_estimate=0,
      predicted_shuffle_gb=0.0,
      warnings=tuple(launch.warnings),
      catalogue_version=load_catalogue().version,
      temp_dataset=knobs.temp_dataset or
      f"{project or args.project}.{knobs.output_dataset}",
      label_key_uri=args.label_key_uri,
      thresholds_uri=thresholds_uri,
      thresholds_digest=digest)
  row = _failed(stub, exc, now)
  row.update(bq_bytes_processed=None, predicted_shuffle_gb=None)
  if not resolved:
    row["params_source"] = None
  return row


def _redacted(exc: BaseException, uri: str | None) -> BaseException:
  """`exc`, or — its message quotes the label key's URI — a stand-in of
  the same class name whose message does not (and that still carries
  `planning_ddl`). The registry's reason is cut at a fixed length, so
  the URI has to go BEFORE the cut: afterwards a piece of it could be
  left at the end."""
  if not uri or uri not in str(exc):
    return exc
  stand_in: BaseException = type(
      type(exc).__name__, (Exception,),
      {"planning_ddl": getattr(exc, "planning_ddl", ())})(
          str(exc).replace(uri, _REDACTED))
  return stand_in


def _telling_line(message: str) -> str:
  """The line of a long error message worth keeping when it is cut: the
  last one on which Beam names the failing step (it appends `[while
  running '<step>']` to the error itself), else the last line."""
  lines = [line.strip() for line in message.splitlines() if line.strip()]
  named = [line for line in lines if _WHILE_RUNNING in line]
  return (named or lines)[-1]


def _failed(planned: EvaluationPlan, exc: BaseException,
            now: datetime | None) -> dict[str, Any]:
  """`assemble.failed_row`, with two things a reader of the reason needs:
  the label key's URI is out of it and out of the warnings (an error, or
  a warning made of one, may quote the secret's resource name), and a
  reason the registry had to cut keeps, after its beginning, the line
  that says what failed (`_telling_line`): a runner reports a worker's
  failure as a traceback far longer than the cut."""
  uri = planned.label_key_uri
  shaped = _redacted(exc, uri)
  row = failed_row(planned, shaped, now=now)
  if uri:  # a warning can quote it as an error can
    row["warnings"] = [str(w).replace(uri, _REDACTED) for w in row["warnings"]]
  whole = f"{type(shaped).__name__}: {shaped}".rstrip()
  reason = str(row["status_reason"])
  if len(reason) < len(whole):
    tail = _telling_line(whole)[-(len(reason) // 2):]
    head = reason[:len(reason) - len(tail) - len(_CUT)]
    row["status_reason"] = f"{head}{_CUT}{tail}"
  return row


# --------------------------------------------------------------------------
# pipeline options
# --------------------------------------------------------------------------
def forbidden_experiments(beam_args: Sequence[str]) -> list[str]:
  """The experiments among `beam_args` this evaluator refuses
  (`enable_data_sampling`, however it is spelled: alone, with a value,
  or inside a comma-separated list)."""
  experiments = PipelineOptions(
      list(beam_args)).view_as(DebugOptions).experiments or []
  return [
      e for e in experiments
      if any(part.strip().split("=", 1)[0] == _FORBIDDEN_EXPERIMENT
             for part in str(e).split(","))
  ]


def pipeline_options(args: argparse.Namespace, beam_args: Sequence[str],
                     planned: EvaluationPlan,
                     evaluation_id: str) -> PipelineOptions:
  """The pipeline's options: the operator's Beam arguments, overridden
  by the evaluator's defaults for `planned` (module docstring) — the
  PREPARED plan, so the side-input cache fits what will run. A larger
  cache the operator asked for is kept.

  Raises:
    ValueError: `enable_data_sampling` is among the experiments.
  """
  flags = list(beam_args)
  refused = forbidden_experiments(flags)
  if refused:
    raise ValueError(f"experiment {_FORBIDDEN_EXPERIMENT} is refused "
                     f"({refused}): Dataflow would sample pipeline elements, "
                     "the label key among them, into its monitoring UI "
                     "(Ruling R68)")
  user = PipelineOptions(flags)
  overrides = pipeline_options_defaults(args.runner, planned)
  experiments = list(
      dict.fromkeys([
          *(user.view_as(DebugOptions).experiments or []),
          *overrides.pop("experiments", []),
      ]))
  if experiments:
    overrides["experiments"] = experiments
  asked = user.view_as(WorkerOptions).max_cache_memory_usage_mb or 0
  overrides["max_cache_memory_usage_mb"] = max(
      int(asked), overrides["max_cache_memory_usage_mb"])
  if args.project:
    overrides["project"] = args.project
  if args.region:
    overrides["region"] = args.region
  if is_dataflow(args.runner) and not user.view_as(GoogleCloudOptions).job_name:
    overrides["job_name"] = f"sdfb-{evaluation_id.lower()}"
  if is_dataflow(
      args.runner) and not user.view_as(WorkerOptions).sdk_container_image:
    # The image bakes its own pushed coordinate (docker/Dockerfile); an
    # explicit --sdk_container_image wins.
    baked = os.environ.get(_WORKER_IMAGE_ENV)
    if baked:
      overrides["sdk_container_image"] = baked
    else:
      _LOGGER.warning(
          "Neither --sdk_container_image nor %s is set: Dataflow workers will "
          "boot the stock Beam SDK image and fail to import sdfb_evaluation.",
          _WORKER_IMAGE_ENV)
  return PipelineOptions(flags, **overrides)


# --------------------------------------------------------------------------
# the read path
# --------------------------------------------------------------------------
def read_sides(planned: EvaluationPlan) -> list[tuple[TablePlan, Side, str]]:
  """(table, side, read table) of every source or synthetic side the
  pipeline reads from BigQuery: both sides of an evaluated table, and
  the sides of a read-only parent that still have a read table. A read
  table that is no table name is left to the pipeline's own set-up,
  which names it."""
  sides: list[tuple[TablePlan, Side, str]] = []
  for table in planned.tables:
    if not table.columns or not (table.evaluated or table.role == "external"):
      continue
    reads = ((Side.SOURCE, table.source_read_table),
             (Side.SYNTHETIC, table.synthetic_read_table or
              table.scope.read_table))
    for side, read in reads:
      if not read:
        continue
      try:
        sides.append((table, side, normalize_fqn(read)))
      except ValueError:
        continue
  return sides


def _refused(exc: BaseException) -> bool:
  """Whether the Storage Read API REFUSED the probe — a 403: the
  missing permission, the API disabled in the project, a perimeter. Any
  other error may pass on a retry and is not a refusal (module
  docstring, The read path)."""
  return isinstance(exc, PermissionError) or getattr(exc, "code",
                                                     None) == _HTTP_FORBIDDEN


def _first_line(exc: BaseException) -> str:
  lines = str(exc).strip().splitlines()
  return (lines[0] if lines else type(exc).__name__)[:_ERROR_CHARS]


def _size(count: int) -> str:
  for unit, scale in _SIZE_UNITS:
    if count >= scale:
      return f"{count / scale:.2f} {unit}"
  return f"{count} B"


def _side_size(bq: Any, table: TablePlan, side: Side,
               read: str) -> tuple[int, int | None]:
  """(rows, logical bytes or None) of one read table, from the table
  itself (the prepare statements built it after planning); the rows the
  plan counted, and no size, when BigQuery refuses the lookup.

  Raises:
    BqApiError: the lookup failed for a reason a retry may remove.
  """
  planned = table.rows_read[0 if side is Side.SOURCE else 1]
  try:
    info = bq.table(read)
  except _BQ_ERRORS as exc:
    if not is_refusal(exc):
      raise
    return round(planned), None
  rows, size = info.get("numRows"), info.get("numBytes")
  return (int(rows) if rows is not None else round(planned),
          int(size) if size is not None else None)


def _paged_sides(sides: Sequence[tuple[TablePlan, Side, str]], bq: Any) -> str:
  """The table sides one refusing project pages, each with its rows and
  the table's logical bytes, and their total. The bytes are an upper
  bound on what is paged: a table's size counts every column, and only
  the plan's columns are read (a read-only parent's key columns)."""
  entries, rows_all, bytes_all, sized = [], 0, 0, 0
  for table, side, read in sides:
    rows, size = _side_size(bq, table, side, read)
    rows_all += rows
    if size is not None:
      bytes_all += size
      sized += 1
    measured = _size(size) if size is not None else "size not read"
    entries.append(f"{table.name} {side.value}: {rows:,} rows, {measured}")
  noun = "table side" if len(sides) == 1 else "table sides"
  total = f"{len(sides)} {noun}, {rows_all:,} rows"
  if sized == len(sides):
    total += f", at most {_size(bytes_all)} in all"
  elif sized:
    total += (f" in all, at most {_size(bytes_all)} in the {sized} whose "
              "size was read")
  else:
    total += " in all"
  return "; ".join([*entries, total])


def _paged_note(refusals: Mapping[str, BaseException],
                paged: Sequence[tuple[TablePlan, Side, str]], bq: Any) -> str:
  """The one line a launch says when it pages (module docstring, The
  read path), on the console and in the plan's warnings: per refusing
  project, why it refused and the table sides read from it. `paged` are
  the sides whose read table is in a refusing project; a side that keeps
  DIRECT_READ is not named. It says what brings the fast read back
  without telling the reader to grant a role: a refusing project may be
  one nobody in the deployment administers (a public one)."""
  noun = "project" if len(refusals) == 1 else "projects"
  where = noun + " " + ", ".join(sorted(refusals))
  parts = []
  for project, exc in sorted(refusals.items()):
    sides = [s for s in paged if s[2].split(".", 1)[0] == project]
    parts.append(f"Project {project} ({type(exc).__name__}: "
                 f"{_first_line(exc)}) pages {_paged_sides(sides, bq)}.")
  paging = " ".join(parts)
  return (
      f"the BigQuery Storage Read API refused a read session on {where}: "
      "the table sides read from there are paged through tabledata.list by "
      "the BigQuery client instead, which is slower, and every other side "
      "keeps the fast read. BigQuery limits tabledata.list to 3.7 GB of row "
      "data per minute (7.5 GB in the US and EU multi-regions) for the "
      "project that contains the table, shared with every other reader of "
      "that project. The sizes below are the tables' logical bytes, so at "
      "most that is paged: only the plan's columns are read. "
      f"{paging} The fast read comes back for a project once the account "
      f"the job runs as holds {_READ_SESSION_ROLE} on it, a grant only that "
      "project's administrators can make; for tables too large to page, "
      "--mode sampled reads a sample of each")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
class _Registry:
  """Where the driver's own registry events go (module docstring)."""

  def __init__(self, sinks: Sinks, bq: Any, table: str, local: bool):
    self._sinks = sinks
    self._bq = bq
    self._table = table
    self._local = local

  def append(self, row: Mapping[str, Any], label: str) -> None:
    if self._local:
      assert isinstance(self._sinks, LocalJsonSinks)
      self._sinks.write_rows([row], REGISTRY, label=label)
    else:
      self._bq.load_json(self._table, [dict(row)], load_schema(REGISTRY))

  def record_failure(self, make_row: Callable[..., Mapping[str, Any]], *args:
                     Any, **kwargs: Any) -> None:
    """Append the FAILED row `make_row(*args, **kwargs)` builds; if
    building or writing it fails too, say so on stderr and return, so the
    evaluation's own error is the one that surfaces."""
    try:
      self.append(make_row(*args, **kwargs), "driver-failed")
    except Exception as exc:  # pylint: disable=broad-exception-caught  # whatever stops this write must not replace the evaluation's own error
      print(
          "sdfb-eval: the FAILED registry row could not be written "
          f"({type(exc).__name__}: {exc}); the evaluation's own error "
          "follows",
          file=sys.stderr)


def _free_text_pools(
    planned: EvaluationPlan,
    bq: Any) -> tuple[dict[str, dict[str, FreeTextPool]] | None, list[str]]:
  """The launch's free-text pools per table (`census.read_pools`) and a
  note for each table whose pools BigQuery refused to give; None when
  the launch names no pools table (`field.pool_memorization_lift` then
  says so itself).

  Raises:
    BqApiError: a read failed for a reason a retry may remove
      (`context.bq.is_refusal` is False): the run fails instead of
      publishing a pool metric that is missing by accident.
  """
  params = planned.launch.params
  table = str(params.get("freetext_pools_table") or "").strip()
  if not table:
    return None, []
  pools: dict[str, dict[str, FreeTextPool]] = {}
  notes = []
  for entry in planned.tables:
    if not entry.evaluated or not entry.reference_digest:
      continue
    try:
      found = read_pools(
          bq,
          pools_table=table,
          reference_digest=entry.reference_digest,
          model_uri=str(params.get("model_uri") or ""),
          max_bytes=planned.budget.max_bytes_billed)
    except (*_BQ_ERRORS, ValueError) as exc:
      # ValueError: the launch names a table that is no table reference
      if not isinstance(exc, ValueError) and not is_refusal(exc):
        raise
      notes.append(f"{entry.name}: the free-text pools could not be read "
                   f"from {table} ({type(exc).__name__}: {exc}); "
                   "field.pool_memorization_lift is not evaluated")
      continue
    if found:
      pools[entry.name] = found
  return (pools or None), notes


def _local_final(sinks: Sinks) -> dict[str, Any]:
  """The FINAL row the pipeline wrote under a local sink.

  Raises:
    RuntimeError: there is none (the pipeline did not run to its end).
  """
  assert isinstance(sinks, LocalJsonSinks)
  finals = [r for r in sinks.read_rows(REGISTRY) if r.get("event") == _FINAL]
  if not finals:
    raise RuntimeError(
        "the pipeline ended without writing a FINAL registry row: its "
        "outputs are incomplete and nothing was loaded")
  return finals[-1]


def _score(value: Any) -> str:
  return "—" if value is None else f"{float(value):.3f}"


def _summary(final: Mapping[str, Any], counts: Mapping[str, Any], where: str,
             key_mode: str) -> list[str]:
  evaluation_id, status = final.get("evaluation_id"), final.get("status")
  lines = [f"evaluation {evaluation_id}: {status}"]
  reason = final.get("status_reason")
  if reason:
    lines.append(f"  {reason}")
  scores = ", ".join(
      name + " " + _score(final.get(name + "_score")) for name in _SCORE_KEYS)
  tally = ", ".join(
      f"{int(counts.get(key) or 0):,} {key}" for key in _COUNT_KEYS)
  lines += [
      f"  scores: {scores}", f"  metrics: {tally}", f"  label key: {key_mode}",
      f"  written to: {where}"
  ]
  return lines


def _gate_line(final: Mapping[str, Any], counts: Mapping[str, Any],
               fail_on: str, code: int) -> str:
  """The summary's gate line of an active gate."""
  failing, warning = counts.get("fail") or 0, counts.get("warn") or 0
  outcome = "TRIPPED" if code else "passed"
  line = (f"  gate (--fail_on {fail_on}): {outcome} — {failing} fail, "
          f"{warning} warn")
  if code and final.get("status") == _PARTIAL:
    reason = final.get("status_reason") or "no reason recorded"
    line += (f"; the run is PARTIAL ({reason}): the gate cannot vouch for a "
             "launch table that was not evaluated")
  return line


@dataclass
class _Run:
  """One `run`'s wiring: the attempt, where it writes and how it reads
  its own result back."""
  args: argparse.Namespace
  env: Env
  attempt: _Attempt
  project: str
  fixture: Fixture | None
  sinks: Sinks
  bq: Any
  registry: _Registry
  directory: str | None
  temporary: str | None

  @property
  def where(self) -> str:
    if self.args.sink == _LOCAL:
      return str(self.directory)
    return f"{self.project}.{self.args.output_dataset}"

  @property
  def how_to_read(self) -> str:
    """The command that reads this evaluation's result later."""
    evaluation_id = self.attempt.evaluation_id
    if self.args.sink == _LOCAL:
      return f"sdfb-eval report --local {self.directory}"
    dataset = self.args.output_dataset
    return (f"sdfb-eval report --project {self.project} --evaluation_id "
            f"{evaluation_id} --output_dataset {dataset}")

  def read_path(self,
                planned: EvaluationPlan) -> tuple[Sources, EvaluationPlan]:
    """The sources the pipeline of the PREPARED plan reads through, and
    the plan — with the paged read's warning when a project refused a
    read session (module docstring, The read path).

    Raises:
      Exception: a probe failed without being refused (`_refused`), or
        a read table's size could not be read for a reason a retry may
        remove; the driver then writes the FAILED row.
    """
    sources = self.env.make_sources()
    if not isinstance(sources, BigQuerySources):
      return sources, planned
    sides = read_sides(planned)
    refusals: dict[str, BaseException] = {}
    probed: set[str] = set()
    for table, _, read in sides:
      project = read.split(".", 1)[0]
      if project in probed:
        continue
      probed.add(project)
      try:
        self.env.probe_read(read, [c.name for c in table.columns])
      except Exception as exc:  # pylint: disable=broad-exception-caught  # the API's own error types; whatever is not a refusal is re-raised
        if not _refused(exc):
          raise
        refusals[project] = exc
    if not refusals:
      return sources, planned
    routed = RoutedBigQuerySources(refusals, self.project)
    note = _paged_note(refusals, [s for s in sides if routed.paged(s[2])],
                       self.bq)
    _LOGGER.warning("%s", note)
    return routed, dataclasses.replace(
        planned, warnings=(*planned.warnings, note))

  def submit(self,
             planned: EvaluationPlan,
             beam_args: Sequence[str],
             sources: Sources | None = None) -> Any:
    """Build the pipeline of the prepared plan over `sources` (None: a
    fixture's own) and hand it to its runner; returns the runner's
    result. Each step is timed, and a Dataflow run's panel is staged
    under its temp location while the graph is built (module docstring,
    The panel).

    Raises:
      ValueError: a Dataflow run whose pipeline carries no temp
        location (one Beam did not validate as a Dataflow service
        pipeline: `beam.staged.panel_root`).
      StagedObjectError: a panel object could not be written.
    """
    args, fixture = self.args, self.fixture
    evaluation_id = self.attempt.evaluation_id
    pools: dict[str, dict[str, FreeTextPool]] | None = None
    notes: list[str] = []
    with _timed(evaluation_id, "free text pools"):
      if fixture is None:
        pools, notes = _free_text_pools(planned, self.bq)
    if notes:
      planned = dataclasses.replace(
          planned, warnings=(*planned.warnings, *notes))
    with _timed(evaluation_id, "pipeline options"):
      options = pipeline_options(args, beam_args, planned, evaluation_id)
      pipeline = self.env.make_pipeline(options)
    if sources is None:
      sources = (
          self.env.make_sources() if fixture is None else fixture.sources())
    staging: Staging | None = None
    if is_dataflow(args.runner):
      # The pipeline's options, not `options`: Beam deep-copied them and
      # settled the temp location in ITS copy (the staging location, or a
      # default bucket, when no valid --temp_location was given).
      settled = pipeline.options.view_as(GoogleCloudOptions)
      staging = Staging(panel_root(settled.temp_location, evaluation_id))
    with _timed(evaluation_id, "graph"):
      build_evaluation_pipeline(
          pipeline,
          planned,
          sources=sources,
          sinks=self.sinks,
          stats_query=self.bq.query if fixture is None else None,
          pools=pools,
          thresholds=getattr(args, "thresholds", None) or None,
          staging=staging)
    if staging is not None:
      _LOGGER.info(
          "evaluation %s: panel staging: %d objects, %s, %.2fs (%.2fs in the "
          "writes)", evaluation_id, len(staging.objects), _size(staging.size),
          staging.seconds, staging.write_seconds)
    with _timed(evaluation_id, "pipeline run"), _relayed_uploads(evaluation_id):
      return self.env.submit(pipeline)

  def _close(self, planned: EvaluationPlan, exc: BaseException) -> None:
    """The driver owns the outcome: append its FAILED row."""
    self.registry.record_failure(_failed, planned, exc, self.env.now())

  def _read_final(self) -> dict[str, Any] | None:
    """This evaluation's FINAL row, read back from the registry table
    (None: it holds none).

    Raises:
      Exception: the registry could not be read.
    """
    try:
      stored = read_bq(
          self.bq,
          project=self.project,
          dataset=self.args.output_dataset,
          evaluation_id=self.attempt.evaluation_id,
          metrics=False)
    except NoSuchEvaluationError:
      return None
    return stored.final

  def _close_ended(self, planned: EvaluationPlan, job: str | None,
                   exc: BaseException) -> None:
    """The run ended in a state other than DONE. A local run is the
    driver's to close. A submitted job that writes to BigQuery may have
    loaded its FINAL row before it failed or was cancelled, so the
    registry is read back first: FAILED is appended only when it holds
    no FINAL row, and nothing when the read-back fails (a FINAL row may
    well exist) — never two terminal rows (Ruling R113). What is printed
    of an error goes through the label key's redaction, as a failed
    row's reason does."""
    if job is None or self.args.sink != "bq":
      self._close(planned, exc)
      return
    evaluation_id = self.attempt.evaluation_id
    uri = planned.label_key_uri
    ended = (f"job {job} ended without completing "
             f"({type(exc).__name__}: {_redacted(exc, uri)})")
    try:
      found = self._read_final()
    except Exception as read_exc:  # pylint: disable=broad-exception-caught  # any read failure means "unknown": write nothing
      print(
          f"sdfb-eval: evaluation {evaluation_id}: {ended}, and the registry "
          f"could not be read ({type(read_exc).__name__}: "
          f"{_redacted(read_exc, uri)}). No registry row was written: its "
          "FINAL row may be there. Read the result later with: "
          f"{self.how_to_read}",
          file=sys.stderr)
      return
    if found is None:
      self._close(planned, exc)
      return
    status = found.get("status")
    print(
        f"sdfb-eval: evaluation {evaluation_id}: {ended}, but the registry "
        f"already holds a FINAL row ({status}): no further row was written. "
        f"Read it with: {self.how_to_read}",
        file=sys.stderr)

  def _leave_running(self, job: str) -> None:
    """A submitted job is still running: write nothing, say so."""
    evaluation_id = self.attempt.evaluation_id
    print(
        f"sdfb-eval: evaluation {evaluation_id} continues on "
        f"{self.args.runner} as job {job}: it was not cancelled and no "
        "terminal registry row was written (the job writes its own FINAL "
        f"row). Read its result later with: {self.how_to_read}",
        file=sys.stderr)

  def _leave_finished(self, job: str | None) -> None:
    """The run ended DONE and the wait was interrupted: its FINAL row
    is the pipeline's. Write nothing, say where the result is."""
    evaluation_id = self.attempt.evaluation_id
    what = f"job {job}" if job else "the pipeline"
    then = f"Read its result with: {self.how_to_read}"
    if isinstance(self.sinks, ClientLoadSinks):
      then = (f"Its outputs are in {self.directory} and were not loaded "
              "into BigQuery")
    print(
        f"sdfb-eval: evaluation {evaluation_id}: {what} finished (DONE) "
        "before the wait was interrupted: no registry row was written here "
        f"(the pipeline writes its own FINAL row). {then}",
        file=sys.stderr)

  def wait(self, result: Any, planned: EvaluationPlan) -> None:
    """Wait for `result` to end DONE (module docstring: who owns the
    terminal row when it does not). A wait that raises on a run which,
    looked at again, is DONE returns as if it had not raised when the
    error is an ordinary one (a warning says so: the evaluation
    completed); an interrupt is re-raised, with no row written.

    A run that ended in another state is closed through `_close_ended`
    (a submitted job's registry is read back first).

    Raises:
      RuntimeError: the run ended in a state other than DONE.
      BaseException: whatever interrupted the wait, unchanged.
    """
    job = _job_id(result)
    try:
      state = result.wait_until_finish()
    except BaseException as exc:
      state = _last_state(result)  # one look: it decides who owns the row
      if state == PipelineState.DONE:
        if not isinstance(exc, Exception):
          self._leave_finished(job)
          raise
        what = f"job {job}" if job else "the pipeline"
        shown = _redacted(exc, planned.label_key_uri)  # as a failed row's
        print(
            f"sdfb-eval: warning: the wait failed ({type(shown).__name__}: "
            f"{shown}), but {what} finished (DONE): reading its result",
            file=sys.stderr)
        return
      if _still_running(job, state):
        self._leave_running(str(job))
      else:
        self._close_ended(planned, job, exc)
      raise
    if state is None or state == PipelineState.DONE:
      return
    named = f" (job {job})" if job else ""
    error = RuntimeError(f"the evaluation pipeline{named} ended in state "
                         f"{state}, not DONE")
    if _still_running(job, state):
      self._leave_running(str(job))
    else:
      self._close_ended(planned, job, error)
    raise error

  def collect(self, planned: EvaluationPlan) -> dict[str, Any]:
    """The FINAL row of a run that ended DONE: a local sink's own file
    (then, `bq_client`, the load jobs), or the registry read back.

    Raises:
      RuntimeError: there is no FINAL row (the driver appended FAILED),
        or the registry could not be read (nothing was appended).
    """
    if self.args.sink != "bq":
      try:
        final = _local_final(self.sinks)
        if isinstance(self.sinks, ClientLoadSinks):
          self.sinks.load(self.bq)
      except BaseException as exc:
        self._close(planned, exc)
        raise
      return final
    evaluation_id = self.attempt.evaluation_id
    try:
      found = self._read_final()
    except Exception as exc:
      raise RuntimeError(
          f"evaluation {evaluation_id}: the job finished, but the registry "
          f"could not be read ({type(exc).__name__}: {exc}). No registry row "
          "was written: its FINAL row may be there. Read the result later "
          f"with: {self.how_to_read}") from exc
    if found is None:
      error = RuntimeError(f"evaluation {evaluation_id}: {_NO_FINAL} in "
                           f"{self.where}.{REGISTRY}")
      self._close(planned, error)
      raise error
    return found

  def discard_temporary(self, *, failed: bool) -> None:
    """Remove the temporary directory — unless the run failed after
    writing into it: then it holds the only copy of the outputs, and its
    path is printed once on stderr."""
    if not self.temporary:
      return
    wrote = any(files for _, _, files in os.walk(self.temporary))
    if failed and wrote:
      print("sdfb-eval: the local outputs were kept in "
            f"{self.temporary}",
            file=sys.stderr)
      return
    shutil.rmtree(self.temporary, ignore_errors=True)


def _wiring(args: argparse.Namespace, env: Env) -> _Run:
  """The run's wiring. A temporary output directory made here does not
  outlive a failure of the rest (the BigQuery client that cannot be
  made, for one): it is still empty, so it is removed before the error
  goes on."""
  now = env.now()
  attempt = _Attempt(mint_evaluation_id(now, env.token()), now)
  fixture = load_fixture(args.fixture_dir) if args.fixture_dir else None
  project = args.project or (fixture.project if fixture else "")
  directory: str | None = None
  temporary: str | None = None
  if args.sink != "bq":
    base = args.output_local
    if not base:
      base = temporary = tempfile.mkdtemp(prefix="sdfb-eval-")
    directory = os.path.join(base, attempt.evaluation_id)
  try:
    sinks: Sinks
    if args.sink == "bq":
      sinks = BigQuerySinks(project, args.output_dataset)
    elif args.sink == "bq_client":
      sinks = ClientLoadSinks(
          str(directory), project=project, dataset=args.output_dataset)
    else:
      sinks = LocalJsonSinks(str(directory))
    offline = fixture is not None and args.sink == _LOCAL
    bq = None if offline else env.make_bq(project)
    registry = _Registry(
        sinks,
        bq,
        f"{project}.{args.output_dataset}.{REGISTRY}",
        local=args.sink == _LOCAL)
  except BaseException:
    if temporary:
      shutil.rmtree(temporary, ignore_errors=True)
    raise
  return _Run(
      args=args,
      env=env,
      attempt=attempt,
      project=project,
      fixture=fixture,
      sinks=sinks,
      bq=bq,
      registry=registry,
      directory=directory,
      temporary=temporary)


def _drive(this: _Run, beam_args: Sequence[str], wait: bool,
           gated: bool) -> int:
  args, attempt, registry = this.args, this.attempt, this.registry
  print(f"evaluation {attempt.evaluation_id}: planning "
        f"(mode {args.mode}, {args.runner}, sink {args.sink})")
  try:
    planned = _make_plan(args, this.env, this.bq, this.fixture, attempt)
  except Exception as exc:
    registry.record_failure(
        planning_failed_row,
        args,
        evaluation_id=attempt.evaluation_id,
        evaluated_at=attempt.now,
        exc=exc,
        launch=attempt.launch,
        project=this.project,
        now=this.env.now())
    raise
  thresholds_uri, digest = _override(args)
  planned = dataclasses.replace(
      planned,
      label_key_uri=args.label_key_uri,
      thresholds_uri=thresholds_uri,
      thresholds_digest=digest)
  try:
    with _timed(planned.evaluation_id, "RUNNING row"):
      registry.append(running_row(planned), "driver-running")
    sources: Sources | None = None
    if this.fixture is None:
      with _timed(planned.evaluation_id, "prepare statements"):
        planned = prepare_evaluation(planned, this.bq)
      with _timed(planned.evaluation_id, "read path"):
        sources, planned = this.read_path(planned)
    with _timed(planned.evaluation_id, "submission"):
      result = this.submit(planned, beam_args, sources)
  except BaseException as exc:  # no job is running: the driver's to close
    registry.record_failure(_failed, planned, exc, this.env.now())
    raise
  key_mode = label_key_mode(args.label_key_uri)
  if _handed_over(result):  # a template launch: nothing ran, nothing to wait
    print(f"evaluation {attempt.evaluation_id}: handed to {args.runner} as a "
          "template (the launcher submits the job; no job id is known "
          "here), not waited for\n"
          f"  label key: {key_mode}\n"
          f"  written to: {this.where} (the RUNNING row stays open until "
          "the job writes the FINAL row; --fail_on is not applied here)")
    return 0
  if not wait:
    job = _job_id(result)
    named = f" as job {job}" if job else ""
    print(f"evaluation {attempt.evaluation_id}: submitted on {args.runner}"
          f"{named}, not waited for\n"
          f"  label key: {key_mode}\n"
          f"  written to: {this.where} (the pipeline writes the FINAL row; "
          "--fail_on is not applied here)")
    return 0
  this.wait(result, planned)
  final = this.collect(planned)
  counts = {key: final.get(f"metrics_{key}") or 0 for key in _COUNT_KEYS}
  lines = _summary(final, counts, this.where, key_mode)
  if args.sink == "bq_client" and args.output_local:
    lines.append(f"  local copy: {this.directory}")
  code = final_exit_code(final, args.fail_on, counts, gated=gated)
  if code == EXIT_FAILED:
    lines.append(f"  the evaluation finished FAILED: exit {EXIT_FAILED} "
                 "(--fail_on does not apply; no further registry row is "
                 "written)")
  elif not gated:
    lines.append("  gate: --fail_on is not applied here")
  elif args.fail_on != "none":
    lines.append(_gate_line(final, counts, args.fail_on, code))
  print("\n".join(lines))
  if final.get("status") == _SKIPPED:
    reason = final.get("status_reason") or "no reason recorded"
    print(f"sdfb-eval: nothing was evaluated: {reason}", file=sys.stderr)
  return code


def run(args: argparse.Namespace,
        beam_args: Sequence[str],
        env: Env | None = None,
        *,
        wait: bool = True,
        gated: bool = True) -> int:
  """`sdfb-eval run`: one evaluation, start to registry (module
  docstring). `wait=False` submits the pipeline and returns (the flex
  entry on Dataflow); `gated=False` never applies `--fail_on`.

  Returns:
    0; 1 when the `--fail_on` gate trips; 3 when the run ended without
    an error but its FINAL row reads FAILED (no launch table could be
    evaluated: the pipeline wrote that row itself, so none is added).

  Raises:
    Whatever stopped the evaluation, after its FAILED row was appended
    (when the driver owns the outcome: module docstring).
  """
  this = _wiring(args, env or Env())
  try:
    code = _drive(this, beam_args, wait, gated)
  except BaseException:
    this.discard_temporary(failed=True)
    raise
  this.discard_temporary(failed=False)
  return code
