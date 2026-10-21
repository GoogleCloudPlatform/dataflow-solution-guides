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
"""`sdfb-eval`: plan, run, report on and compare evaluations.

    sdfb-eval plan       the plan of a target, never run: tables, scopes,
                         methods per column, BigQuery dry-run bytes,
                         predicted shuffle
    sdfb-eval run        one evaluation: RUNNING row → prepare DDL →
                         pipeline → outputs → FINAL row (`cli.driver`)
    sdfb-eval report     a stored evaluation as markdown or JSON
    sdfb-eval compare    two stored evaluations, noise-aware
    sdfb-eval catalogue  the metric catalogue, JSON or markdown
    sdfb-eval schemas    the four tables and two views: print, or create

A target (plan, run) is exactly one of

    --job_id J --region R          a generation Dataflow job
    --run_id B                     a launch by its base run id, from the
                                   validation_runs of --output_dataset
    --tables A,B --landing_dataset L --reference_dataset D
      [--relationships_uri U]      tables named by hand

`--relationships_uri` may accompany any of them; it fills in the model
when the launch's own records name none. An `evaluation_id` is minted
per attempt and never passed in (Ruling R88c); `report` and `compare`
take the ids `run` printed.

`run` accepts Beam's own arguments after its own (`--temp_location`,
`--num_workers`, `--experiments`, …); the other commands take none.

    exit code   run                               the other commands
    ─────────   ────────────────────────────────  ───────────────────
    0           the evaluation finished           done
    1           the --fail_on gate tripped,       —
                and nothing else
    2           a usage error: nothing was        a usage error
                started (no registry row, no
                DDL)
    3           the evaluation failed: its        —
                FINAL row reads FAILED, or the
                driver raised (its FAILED row
                is written first when the
                driver owns the outcome, then
                the traceback goes to stderr)

A usage error is everything that can be told from the command line
alone: the evaluator's own flags, the thresholds file, the runner (the
evaluation does not run on Prism: `--runner PrismRunner` is refused),
and Beam's arguments too (a malformed `--num_workers abc` is refused
here, not after the RUNNING row).

`--runner DirectRunner` (the default) is the operator's word for "run
it here", and what the registry records. The pipeline itself runs on
the runner `pipeline_options_defaults` returns for it — Beam's
in-process `FnApiRunner`, because Beam hands a `DirectRunner` batch
pipeline to Prism, which starts a step before its side input is
complete. Pipeline options are always built from the Beam arguments
given on this command line, as an explicit list: never from `sys.argv`
behind the caller's back. An interrupt (Ctrl-C) keeps its conventional
behaviour and is not mapped to 3. An error in any other command
propagates as it is.

`plan --dry_run`: a plan never runs the prepare DDL and never writes a
registry row, with or without the flag — `--dry_run` says so on the
command line. One thing planning does create (Ruling R57): the
zero-byte, 24-hour start snapshot of each `as_of_diff` scope, which the
scope's own reads go through. `--no_planning_snapshots` plans without
them; those tables are then reported UNPLANNED.

A boolean flag takes an optional value (`--allow_contaminated` or
`--allow_contaminated=false`), and an empty value means "not given" for
every flag of this command line — `--run_id=`, `--mode=`,
`--sample_rows=`, `--output_dataset ""` — so the default applies: a
Dataflow flex template and Composer pass every parameter as
`--name=value`, the unset ones empty. (An empty value of a Beam argument
is Beam's to read.)

Design: docs/designs/2026-07-07-evaluation-framework-design.md

Design: docs/DESIGN.md §11 Evaluation (sdfb-evaluation)
(ADR 0041).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from apache_beam.options.pipeline_options import PipelineOptions

from sdfb_evaluation.beam.label_key import is_label_key_uri
from sdfb_evaluation.beam.pipeline import pipeline_options_defaults
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.cli import driver
from sdfb_evaluation.cli.driver import Env
from sdfb_evaluation.cli.gate import EXIT_FAILED, FAIL_ON
from sdfb_evaluation.cli.thresholds import load_thresholds
from sdfb_evaluation.cli.planview import describe_plan, render_plan_text
from sdfb_evaluation.context.bq import normalize_fqn
from sdfb_evaluation.context.plan import TRIGGERS
from sdfb_evaluation.context.scope import MODES as SCOPE_MODES
from sdfb_evaluation.report.render import (
    compare,
    render_catalogue_markdown,
    render_compare_json,
    render_compare_markdown,
    render_json,
    render_markdown,
)
from sdfb_evaluation.report.store import Evaluation, read_bq, read_local
from sdfb_evaluation.schemas import TABLES, bq_mk_commands, table_ddl, view_sql
from sdfb_evaluation.version import EVALUATOR_VERSION

__all__ = [
    "build_parser", "main", "parse_args", "public_run_flags", "show_progress"
]

_DEFAULT_DATASET = "synthetic_data_quality"
_DEFAULT_RUNNER = "DirectRunner"
_GLOB_CHARS = re.compile(r"[*?\[\]]")
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})
_VIEW_RE = re.compile(r"CREATE OR REPLACE VIEW `([^`]+)`")
_COMPARED = 2

_COUNT_FLAGS = (
    ("--sample_rows", 200_000,
     "sampled mode: rows read per side of a table larger than this"),
    ("--privacy_sample_rows", 50_000,
     "rows of the nearest-neighbour privacy sample"),
    ("--detection_sample_rows", 50_000,
     "rows per side in the detection (C2ST) sample"),
    ("--pair_max_columns", 20, "columns whose pairs are compared, per table"),
    ("--row_flags_top_k", 100, "row flags kept per check and table"),
)

Check = Callable[[argparse.ArgumentParser, argparse.Namespace, list[str]], None]


# --------------------------------------------------------------------------
# argument types
# --------------------------------------------------------------------------
def _text(value: str) -> str | None:
  """A text flag; empty means not given (module docstring)."""
  return value.strip() or None


def _positive(value: str) -> int | None:
  """A count flag: a positive integer; empty means not given."""
  text = value.strip()
  if not text:
    return None
  if not text.isdigit() or int(text) < 1:
    raise argparse.ArgumentTypeError(
        f"expected a positive integer, got {value!r}")
  return int(text)


_TABLE_RE = re.compile(r"([a-z][a-z0-9-]{4,28}[a-z0-9])\.([A-Za-z0-9_]{1,1024})"
                       r"\.([A-Za-z0-9_$-]{1,1024})")


def _bq_table(value: str) -> str | None:
  """A BigQuery table as `project.dataset.table` (`project:dataset.table`
  accepted, normalised); empty means not given."""
  text = value.strip().replace(":", ".", 1)
  if not text:
    return None
  if _TABLE_RE.fullmatch(text) is None:
    raise argparse.ArgumentTypeError(
        f"expected a table as project.dataset.table, got {value!r}")
  return text


def _flag(value: str) -> bool:
  lowered = value.strip().lower()
  if lowered in _TRUE:
    return True
  if lowered in _FALSE or not lowered:
    return False
  raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def _csv(value: str) -> list[str] | None:
  return [part.strip() for part in value.split(",") if part.strip()] or None


class _Arguments(Protocol):
  """A parser or one of its argument groups."""

  def add_argument(self, *name_or_flags: str, **kwargs: Any) -> argparse.Action:
    ...


def _boolean(parser: _Arguments, name: str, help_text: str) -> None:
  parser.add_argument(
      name, nargs="?", const=True, default=False, type=_flag, help=help_text)


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------
def _add_target(parser: argparse.ArgumentParser) -> None:
  group = parser.add_argument_group("target (exactly one)")
  group.add_argument(
      "--project", type=_text, help="the GCP project of the evaluation")
  group.add_argument(
      "--region",
      type=_text,
      help="the Dataflow region: where --job_id ran, and where the "
      "evaluation runs on Dataflow")
  group.add_argument(
      "--generation_job_id",
      "--job_id",
      dest="job_id",
      type=_text,
      help="the generation job's Dataflow id; with --seed_table it is not a "
      "second target but the identity of the launch the seed names: only the "
      "Dataflow job is read (its window; never its log), and the row keeps "
      "the job id. A template launch passes it "
      "as --generation_job_id, because the template launcher does not pass "
      "a parameter named job_id (ADR 0041)")
  group.add_argument(
      "--run_id",
      type=_text,
      help="the generation launch's base run id (validation_runs of "
      "--output_dataset)")
  group.add_argument(
      "--tables",
      type=_csv,
      help="landing table names, comma-separated, parents first (omit them "
      "and pass --relationships_uri alone to evaluate every enabled table "
      "of the relationship model, parents first)")
  group.add_argument(
      "--seed_table",
      type=_text,
      help="the table a launch targeted: evaluate what that launch "
      "generated, which is its enabled component in the relationship model "
      "(--relationships_uri), or the table alone when no model names it or "
      "none is given. Needs --landing_dataset and --reference_dataset")
  group.add_argument(
      "--landing_dataset",
      type=_text,
      help="with --tables: the dataset (or project.dataset) they landed in")
  group.add_argument(
      "--reference_dataset",
      type=_text,
      help="with --tables: the dataset (or project.dataset) of their "
      "sources, same table names")
  group.add_argument(
      "--reference_rows_limit",
      type=_positive,
      help="the generation's reference sample size (its "
      "--reference_rows_limit): it sizes the reference panel behind the "
      "privacy metrics, which a hand-named target cannot read from the "
      "launch's records. Without it those metrics are not evaluated")
  group.add_argument(
      "--validation_runs_table",
      type=_bq_table,
      help="the generator's validation-runs table, project.dataset.table: "
      "with --generation_job_id next to --seed_table, this launch's rows of "
      "it (in the job's window) give the digest that verifies the reference "
      "sample, so the metrics that compare with it are evaluated. Fills what "
      "the launch's own records leave open")
  group.add_argument(
      "--relationships_uri",
      type=_text,
      help="the relationship model file or directory (local or gs://), "
      "when the launch's own records name none; alone (with "
      "--landing_dataset and --reference_dataset) it also names the tables")
  group.add_argument("--fixture_dir", type=_text, help=argparse.SUPPRESS)


def _add_planning(parser: argparse.ArgumentParser) -> None:
  group = parser.add_argument_group("mode, scope, sampling, limits, budgets")
  group.add_argument(
      "--runner",
      default=_DEFAULT_RUNNER,
      help="the Beam runner (default %(default)s)")
  group.add_argument(
      "--mode",
      choices=("exact", "sampled"),
      help="read every row, or a salted sample above --sample_rows "
      "(default: sampled on the DirectRunner, exact on Dataflow)")
  group.add_argument(
      "--scope",
      choices=("auto", *SCOPE_MODES),
      default="auto",
      help="how the job's landing rows are isolated (default %(default)s)")
  _boolean(group, "--allow_contaminated",
           "evaluate a scope another writer touched; it stays `contaminated`")
  for name, default, help_text in _COUNT_FLAGS:
    group.add_argument(
        name,
        type=int,
        default=default,
        help=f"{help_text} (default {default})")
  group.add_argument(
      "--row_flags_source_keys",
      choices=("hashed",),
      default="hashed",
      help="row flags carry the matched source key as a keyed hash only "
      "(raw keys are never written)")
  group.add_argument(
      "--max_bytes_billed",
      type=int,
      default=1_099_511_627_776,
      help="BigQuery bytes the evaluation may process (default 1 TiB)")
  group.add_argument(
      "--max_shuffle_gb",
      type=float,
      default=500.0,
      help="shuffle the value census may use before it is value-sampled")
  group.add_argument(
      "--output_dataset",
      type=_text,
      default=_DEFAULT_DATASET,
      help="the dataset of the four evaluation tables (default %(default)s)")
  group.add_argument(
      "--temp_dataset",
      type=_text,
      help="where the expiring scope, pin and sample tables go "
      "(default: --output_dataset)")


def _add_run(parser: argparse.ArgumentParser) -> None:
  group = parser.add_argument_group("output and control")
  group.add_argument(
      "--sink",
      choices=driver.SINKS,
      help="bq: the pipeline loads BigQuery; bq_client: local files, then "
      "client load jobs; local_json: local files only (default: bq_client "
      "on the DirectRunner, bq on Dataflow)")
  group.add_argument(
      "--output_local",
      type=_text,
      metavar="DIR",
      help="with bq_client / local_json: the evaluation is written under "
      "DIR/<evaluation_id>")
  group.add_argument(
      "--thresholds_uri",
      type=_text,
      help="YAML of warn/fail thresholds that override the catalogue's "
      "for the whole run: stored statuses and scores, roll-ups and the "
      "--fail_on gate (thresholds: {metric id: {warn: W, fail: F}})")
  group.add_argument(
      "--fail_on",
      choices=FAIL_ON,
      default="none",
      help="exit 1 when a metric is at this status or worse "
      "(default %(default)s)")
  group.add_argument(
      "--trigger",
      choices=TRIGGERS,
      default="cli",
      help="what started the evaluation (default %(default)s)")
  group.add_argument(
      "--label_key_uri",
      type=_text,
      help="the operator's label key: a Secret Manager version "
      "(projects/P/secrets/S/versions/V), a gs:// object or an absolute "
      "path; without it the key is ephemeral")


def _add_output(parser: argparse.ArgumentParser, formats: Sequence[str],
                default: str) -> None:
  parser.add_argument(
      "--format",
      choices=tuple(formats),
      default=default,
      help="output format (default %(default)s)")
  parser.add_argument(
      "--out", type=_text, metavar="PATH", help="write there, not to stdout")


def _add_store(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
      "--project", type=_text, help="the project of the evaluation tables")
  parser.add_argument(
      "--output_dataset",
      type=_text,
      default=_DEFAULT_DATASET,
      help="their dataset (default %(default)s)")
  parser.add_argument(
      "--local",
      type=_text,
      metavar="DIR",
      help="read a local evaluation directory (<--output_local>/"
      "<evaluation_id>, or the --output_local directory itself) instead of "
      "BigQuery")


def _subparsers(
) -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
  parser = argparse.ArgumentParser(
      prog="sdfb-eval",
      description="Evaluate landed synthetic BigQuery tables against their "
      "source: plan, run, report, compare.",
      allow_abbrev=False)
  parser.add_argument(
      "--version", action="version", version=f"sdfb-eval {EVALUATOR_VERSION}")
  commands = parser.add_subparsers(
      dest="command", required=True, metavar="COMMAND")

  def add(name: str, help_text: str) -> argparse.ArgumentParser:
    return commands.add_parser(
        name, help=help_text, description=help_text, allow_abbrev=False)

  plan = add("plan", "plan an evaluation without running it")
  _add_target(plan)
  _add_planning(plan)
  _boolean(plan, "--dry_run",
           "a plan never writes; the flag only says so (see `--help`)")
  _boolean(
      plan, "--no_planning_snapshots",
      "do not create as_of_diff start snapshots; those tables are "
      "reported UNPLANNED")
  plan.add_argument(
      "--format", choices=("text", "json"), default="text", help="output")
  run = add("run", "run one evaluation (Beam arguments may follow)")
  _add_target(run)
  _add_planning(run)
  _add_run(run)
  report = add("report", "render a stored evaluation")
  _add_store(report)
  report.add_argument("--evaluation_id", type=_text)
  _add_output(report, ("md", "json"), "md")
  comparison = add("compare", "compare two stored evaluations (A,B)")
  _add_store(comparison)
  comparison.add_argument(
      "--evaluation_ids", type=_csv, required=True, metavar="A,B")
  _add_output(comparison, ("md", "json"), "md")
  catalogue = add("catalogue", "print the metric catalogue")
  _add_output(catalogue, ("json", "md"), "json")
  schemas = add("schemas", "print or create the evaluation tables and views")
  schemas.add_argument("--project", type=_text, required=True)
  schemas.add_argument("--dataset", type=_text, default=_DEFAULT_DATASET)
  _boolean(
      schemas, "--apply",
      "create the tables (kept when they exist) and replace the views "
      "in the dataset, which must exist")
  return parser, {
      "plan": plan,
      "run": run,
      "report": report,
      "compare": comparison,
      "catalogue": catalogue,
      "schemas": schemas,
  }


def build_parser() -> argparse.ArgumentParser:
  """The `sdfb-eval` argument parser."""
  return _subparsers()[0]


def public_run_flags() -> list[str]:
  """Every non-hidden flag of `sdfb-eval run`, sorted (the flex
  template's metadata lists exactly these). A flag with an alias is listed
  under its first name, the template's (`--generation_job_id`, not the
  `--job_id` a direct command line may also use)."""
  run = _subparsers()[1]["run"]
  return sorted(
      action.option_strings[0]
      for action in run._actions  # pylint: disable=protected-access  # argparse has no public list of a parser's options
      if action.help is not argparse.SUPPRESS and action.option_strings and
      action.option_strings[0].startswith("--"))


# --------------------------------------------------------------------------
# usage checks (exit 2)
# --------------------------------------------------------------------------
def _check_target(parser: argparse.ArgumentParser,
                  args: argparse.Namespace) -> None:
  # a job id next to a seed table is not a second target: the seed names
  # the tables, the job id says which generation job the row belongs to
  accompanies = bool(args.job_id and args.seed_table)
  targets = [
      flag for flag, value in (("--job_id", args.job_id and not accompanies),
                               ("--run_id", args.run_id), ("--tables",
                                                           args.tables),
                               ("--seed_table", args.seed_table)) if value
  ]
  if args.fixture_dir:
    if targets:
      parser.error("--fixture_dir is its own target")
    return
  derived = not targets and bool(args.relationships_uri)
  if len(targets) != 1 and not derived:
    given = ", ".join(targets) or "none"
    parser.error("name exactly one of --job_id, --run_id, --tables, "
                 "--seed_table (which --job_id may accompany), or "
                 f"--relationships_uri alone (given: {given})")
  if not args.project:
    parser.error("--project is required")
  if args.job_id and not args.region:
    parser.error("--job_id needs --region: a Dataflow job is only visible "
                 "in the region it ran in")
  datasets = (args.landing_dataset, args.reference_dataset)
  named = bool(args.tables or args.seed_table or derived)
  if named and not all(datasets):
    parser.error("--tables, --seed_table, or --relationships_uri alone, "
                 "needs --landing_dataset and --reference_dataset")
  if not named and any(datasets):
    parser.error("--landing_dataset and --reference_dataset apply to "
                 "--tables, --seed_table, or --relationships_uri alone, only")


def _check_planning(parser: argparse.ArgumentParser,
                    args: argparse.Namespace) -> None:
  _check_target(parser, args)
  try:  # a runner the evaluation refuses (Prism) is refused here
    pipeline_options_defaults(args.runner)
  except ValueError as exc:
    parser.error(f"--runner: {exc}")
  if args.mode is None:
    args.mode = driver.runner_defaults(args.runner)[0]
  try:
    driver.knobs_from_args(args, "eval-usage-check")
  except ValueError as exc:
    parser.error(str(exc))


def _check_plan(parser: argparse.ArgumentParser, args: argparse.Namespace,
                extras: list[str]) -> None:
  del extras
  _check_planning(parser, args)


def _check_sink(parser: argparse.ArgumentParser,
                args: argparse.Namespace) -> None:
  if args.sink is None:
    args.sink = ("local_json" if args.fixture_dir else driver.runner_defaults(
        args.runner)[1])
  local = args.sink != "bq"
  if driver.is_dataflow(args.runner) and local:
    parser.error(f"--sink {args.sink} writes files where the pipeline runs: "
                 "on the DataflowRunner use --sink bq")
  if args.sink == "local_json" and not args.output_local:
    parser.error("--sink local_json needs --output_local DIR")
  if not local and args.output_local:
    parser.error("--output_local applies to --sink bq_client or local_json")
  if args.output_local and _GLOB_CHARS.search(args.output_local):
    parser.error(f"--output_local {args.output_local!r} holds a glob "
                 "character (*?[]): Beam's file sink matches its shards by "
                 "glob; choose another directory")


def _beam_error(extras: Sequence[str]) -> str | None:
  """What Beam's own parser says is wrong with `extras` (a value of the
  wrong type, a missing value), or None. Beam reports it by printing a
  usage text and exiting; here it becomes this command's usage error,
  before anything is started."""
  captured = io.StringIO()
  try:
    with contextlib.redirect_stderr(captured):
      PipelineOptions(list(extras)).get_all_options()
  except SystemExit:
    lines = [line for line in captured.getvalue().splitlines() if line.strip()]
    message = lines[-1] if lines else "not accepted by Beam"
    return message.split("error: ", 1)[-1]
  return None


def _check_run(parser: argparse.ArgumentParser, args: argparse.Namespace,
               extras: list[str]) -> None:
  _check_planning(parser, args)
  _check_sink(parser, args)
  if args.label_key_uri and not is_label_key_uri(args.label_key_uri):
    # the URI is never echoed: it names where the key lives
    parser.error("--label_key_uri must be a Secret Manager version "
                 "(projects/P/secrets/S/versions/V), a gs:// object or an "
                 "absolute path")
  if any(extra.split("=", 1)[0] == "--evaluation_id" for extra in extras):
    parser.error("--evaluation_id is not accepted: every attempt mints its "
                 "own evaluation_id (pass it to report / compare)")
  refused = _beam_error(extras)
  if refused is not None:
    parser.error(f"Beam arguments: {refused}")
  if driver.forbidden_experiments(extras):
    parser.error("the experiment enable_data_sampling is refused: Dataflow "
                 "would sample pipeline elements, the label key among them, "
                 "into its monitoring UI")
  args.thresholds = {}
  if args.thresholds_uri:
    try:
      args.thresholds = load_thresholds(args.thresholds_uri)
    except (ValueError, OSError) as exc:
      parser.error(f"--thresholds_uri: {exc}")


def _check_store(parser: argparse.ArgumentParser,
                 args: argparse.Namespace) -> None:
  if not args.local and not args.project:
    parser.error("name the store: --project (BigQuery) or --local DIR")


def _check_report(parser: argparse.ArgumentParser, args: argparse.Namespace,
                  extras: list[str]) -> None:
  del extras
  _check_store(parser, args)
  if not args.local and not args.evaluation_id:
    parser.error("--evaluation_id is required (or --local DIR for a local "
                 "evaluation directory)")


def _check_compare(parser: argparse.ArgumentParser, args: argparse.Namespace,
                   extras: list[str]) -> None:
  del extras
  _check_store(parser, args)
  if len(args.evaluation_ids or ()) != _COMPARED:
    parser.error("--evaluation_ids takes exactly two ids, A,B")


def _check_schemas(parser: argparse.ArgumentParser, args: argparse.Namespace,
                   extras: list[str]) -> None:
  del extras
  try:  # the names go into DDL between backticks
    normalize_fqn(f"{args.project}.{args.dataset}.evaluation_metrics")
  except ValueError:
    parser.error(f"--project {args.project!r} / --dataset {args.dataset!r} "
                 "do not name a BigQuery dataset")


def _no_check(parser: argparse.ArgumentParser, args: argparse.Namespace,
              extras: list[str]) -> None:
  del parser, args, extras


_CHECKS: dict[str, Check] = {
    "plan": _check_plan,
    "run": _check_run,
    "report": _check_report,
    "compare": _check_compare,
    "catalogue": _no_check,
    "schemas": _check_schemas,
}


def _own_flags(parser: argparse.ArgumentParser) -> frozenset[str]:
  return frozenset(parser._option_string_actions)  # pylint: disable=protected-access  # argparse has no public list of a parser's options


def _without_empty(argv: Sequence[str],
                   commands: dict[str, argparse.ArgumentParser]) -> list[str]:
  """`argv` without the flags of its own command that were given an
  empty value (`--flag=`, or `--flag ""`): "not given", so the default
  applies (module docstring). Beam's arguments are left as they are."""
  command = next((token for token in argv if token in commands), None)
  if command is None:
    return list(argv)
  own = _own_flags(commands[command])
  kept: list[str] = []
  skip = False
  for index, token in enumerate(argv):
    if skip:
      skip = False
      continue
    name, equals, value = token.partition("=")
    following = argv[index + 1] if index + 1 < len(argv) else None
    if name in own and equals and not value.strip():
      continue
    if token in own and following is not None and not following.strip():
      skip = True
      continue
    kept.append(token)
  return kept


def parse_args(
    argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
  """(the parsed arguments, the Beam arguments left for the pipeline),
  runner defaults applied. A usage error exits 2 (argparse's own
  `SystemExit`), before anything is started."""
  parser, commands = _subparsers()
  given = sys.argv[1:] if argv is None else argv
  args, extras = parser.parse_known_args(_without_empty(given, commands))
  command = commands[args.command]
  if extras and args.command != "run":
    joined = " ".join(extras)
    command.error(f"unrecognized arguments: {joined}")
  _CHECKS[args.command](command, args, extras)
  return args, extras


# --------------------------------------------------------------------------
# the commands
# --------------------------------------------------------------------------
def _emit(text: str, out: str | None) -> None:
  if out:
    with open(out, "w", encoding="utf-8") as handle:
      handle.write(text)
    print(f"wrote {out}")
  else:
    sys.stdout.write(text)


def _plan(args: argparse.Namespace, env: Env) -> int:
  planned, unplanned = driver.plan(args, env)
  document = describe_plan(planned, unplanned)
  if args.format == "json":
    sys.stdout.write(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) +
        "\n")
  else:
    sys.stdout.write(render_plan_text(document))
  return 0


def _stored(args: argparse.Namespace, env: Env, evaluation_id: str | None,
            profiles: bool) -> Evaluation:
  if args.local:
    return read_local(args.local, evaluation_id)
  return read_bq(
      env.make_bq(args.project),
      project=args.project,
      dataset=args.output_dataset,
      evaluation_id=str(evaluation_id),
      profiles=profiles)


def _report(args: argparse.Namespace, env: Env) -> int:
  evaluation = _stored(args, env, args.evaluation_id, False)
  render = render_json if args.format == "json" else render_markdown
  _emit(render(evaluation), args.out)
  return 0


def _compare(args: argparse.Namespace, env: Env) -> int:
  first, second = (
      _stored(args, env, evaluation_id, True)
      for evaluation_id in args.evaluation_ids)
  comparison = compare(first, second)
  render = (
      render_compare_json if args.format == "json" else render_compare_markdown)
  _emit(render(comparison), args.out)
  return 0


def _catalogue(args: argparse.Namespace, env: Env) -> int:
  del env
  catalogue = load_catalogue()
  text = (
      catalogue.to_json()
      if args.format == "json" else render_catalogue_markdown(catalogue))
  _emit(text, args.out)
  return 0


def _schemas(args: argparse.Namespace, env: Env) -> int:
  project, dataset = args.project, args.dataset
  tables, views = table_ddl(project, dataset), view_sql(project, dataset)
  if not args.apply:
    print("# the four tables (bq mk), then the two views (bq query); or run "
          "`sdfb-eval schemas --apply`")
    print("\n".join(bq_mk_commands(project, dataset)))
    for statement in views:
      print(f"\n{statement}")
    return 0
  bq = env.make_bq(project)
  for name, statement in zip(TABLES, tables, strict=True):
    bq.execute(statement)
    print(f"{project}.{dataset}.{name}: created or kept (an existing table "
          "is never altered)")
  for statement in views:
    bq.execute(statement)
    match = _VIEW_RE.match(statement)
    name = match.group(1) if match else "view"
    print(f"{name}: replaced")
  return 0


def _run(args: argparse.Namespace, extras: list[str], env: Env) -> int:
  """`run`: the driver's exit code, or 3 when it raised (Ruling R93-5).
  The driver has written the FAILED row by then, when the outcome is its
  to write; the traceback goes to stderr. Exit 1 stays the gate's alone.
  An interrupt is not an error of the evaluation: it propagates."""
  try:
    return driver.run(args, extras, env)
  except Exception:  # pylint: disable=broad-exception-caught  # one exit code for every failure; the traceback is printed
    traceback.print_exc()
    return EXIT_FAILED


_COMMANDS: dict[str, Callable[[argparse.Namespace, Env], int]] = {
    "plan": _plan,
    "report": _report,
    "compare": _compare,
    "catalogue": _catalogue,
    "schemas": _schemas,
}


class _StderrProgress(logging.StreamHandler):
  """A handler on whatever `sys.stderr` is when a line is written."""

  def __init__(self) -> None:
    super().__init__()
    self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

  @property
  def stream(self) -> Any:
    return sys.stderr

  @stream.setter
  def stream(self, value: Any) -> None:
    """The stream is always the current `sys.stderr`."""


def show_progress() -> None:
  """Let this package's INFO lines (planning's steps and seconds, the
  driver's) reach stderr, which is what a launcher's console log and a
  terminal show: Python drops INFO records when no handler is set up."""
  package = logging.getLogger("sdfb_evaluation")
  if not any(isinstance(h, _StderrProgress) for h in package.handlers):
    package.addHandler(_StderrProgress())
  package.setLevel(logging.INFO)


def main(argv: Sequence[str] | None = None, env: Env | None = None) -> int:
  """The `sdfb-eval` entry point; returns the exit code (module
  docstring). `env` replaces the outside world (tests)."""
  show_progress()
  args, extras = parse_args(argv)
  env = env or Env()
  if args.command == "run":
    return _run(args, extras, env)
  return _COMMANDS[args.command](args, env)


if __name__ == "__main__":
  sys.exit(main())
