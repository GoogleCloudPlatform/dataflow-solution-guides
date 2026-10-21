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
"""Reading one evaluation back, from where its sink put it.

    read_bq(bq, ...)       the four tables of `--output_dataset`
      registry  WHERE evaluation_id = @evaluation_id          (every event)
      the rest  … AND evaluated_at = @evaluated_at            (the run's own
                                                               partition)
    read_local(directory)  an `--output_local` evaluation directory:
      <directory>/<table>/*.jsonl, every shard there — the pipeline's
      and the driver's own registry events alike

`LocalJsonSinks.read_rows` only knows the shards its own instance wrote,
so a later process (`sdfb-eval report --local`) needs this reader. A
directory is one evaluation's (`<--output_local>/<evaluation_id>`); with
an `evaluation_id` the reader also accepts the `--output_local` parent
and looks inside it.

Rows come back JSON-shaped whatever the store: a BigQuery TIMESTAMP is
RFC 3339 text and a NUMERIC a float, as in the NDJSON files, so the
renderers never see a client type.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import glob
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sdfb_evaluation.context.bq import quote_fqn
from sdfb_evaluation.schemas import TABLES

__all__ = ["Evaluation", "NoSuchEvaluationError", "read_bq", "read_local"]

REGISTRY = "evaluation_data_history"
METRICS = "evaluation_metrics"
PROFILES = "evaluation_profiles"
FLAGS = "evaluation_row_flags"
_FINAL = "FINAL"


class NoSuchEvaluationError(LookupError):
  """The store was read and holds no registry row for the evaluation
  (as opposed to a store that could not be read: BigQuery's own 404 is a
  plain `LookupError`)."""


@dataclass(frozen=True)
class Evaluation:
  """One evaluation's stored rows: its registry `events` (oldest first),
  its metric rows, its profiles (when read) and where they came from."""
  evaluation_id: str
  events: list[dict[str, Any]]
  metrics: list[dict[str, Any]]
  profiles: list[dict[str, Any]] = field(default_factory=list)
  origin: str = ""

  @property
  def latest(self) -> dict[str, Any]:
    """The last registry event (the view `evaluation_latest`'s row)."""
    return self.events[-1]

  @property
  def final(self) -> dict[str, Any] | None:
    """The last FINAL event, or None while only RUNNING is recorded."""
    finals = [e for e in self.events if e.get("event") == _FINAL]
    return finals[-1] if finals else None


def _plain(value: Any) -> Any:
  """A BigQuery client value as the JSON the sinks write."""
  if isinstance(value, datetime):
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
  if isinstance(value, (date, time)):
    return value.isoformat()
  if isinstance(value, Decimal):
    return float(value)
  if isinstance(value, Mapping):
    return {str(key): _plain(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_plain(item) for item in value]
  return value


def _json_column(value: Any) -> Any:
  """A JSON column: the client returns it parsed, or as its text."""
  if isinstance(value, str):
    try:
      return json.loads(value)
    except ValueError:
      return value
  return value


_JSON_COLUMNS = ("detail", "payload", "generation_params", "evaluation_params",
                 "synthetic_key", "source_key")


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
  plain: dict[str, Any] = _plain(dict(row))
  for name in _JSON_COLUMNS:
    if name in plain:
      plain[name] = _json_column(plain[name])
  return plain


def _by_recorded(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  # a RUNNING and a FINAL row never share recorded_at (assemble.finish_time)
  return sorted((dict(e) for e in events),
                key=lambda e:
                (str(e.get("recorded_at") or ""), e.get("event") == _FINAL))


def read_bq(bq: Any,
            *,
            project: str,
            dataset: str,
            evaluation_id: str,
            metrics: bool = True,
            profiles: bool = False) -> Evaluation:
  """The evaluation `evaluation_id` of `project.dataset` (module
  docstring); `metrics` / `profiles` say which of its tables to read
  besides the registry.

  Raises:
    NoSuchEvaluationError: the registry has no row for `evaluation_id`.
    ValueError: `project.dataset` is not a dataset reference.
  """

  def table(name: str) -> str:
    return quote_fqn(f"{project}.{dataset}.{name}")

  events = _by_recorded([
      _row(r) for r in bq.query(
          f"SELECT * FROM {table(REGISTRY)} "
          "WHERE evaluation_id = @evaluation_id",
          {"evaluation_id": evaluation_id})
  ])
  origin = f"{project}.{dataset}"
  if not events:
    raise NoSuchEvaluationError(
        f"no registry row for evaluation {evaluation_id} in {origin}."
        f"{REGISTRY} (check --project / --output_dataset, or pass --local "
        "for a run written with --sink local_json)")
  params = {
      "evaluation_id":
          evaluation_id,
      "evaluated_at":
          datetime.fromisoformat(
              str(events[0]["evaluated_at"]).replace("Z", "+00:00")),
  }
  where = ("WHERE evaluation_id = @evaluation_id "
           "AND evaluated_at = @evaluated_at")

  def rows(name: str) -> list[dict[str, Any]]:
    return [
        _row(r)
        for r in bq.query(f"SELECT * FROM {table(name)} {where}", dict(params))
    ]

  return Evaluation(
      evaluation_id=evaluation_id,
      events=events,
      metrics=rows(METRICS) if metrics else [],
      profiles=rows(PROFILES) if profiles else [],
      origin=origin)


def _shards(directory: str, table: str) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  pattern = os.path.join(glob.escape(os.path.join(directory, table)), "*.jsonl")
  for path in sorted(glob.glob(pattern)):
    with open(path, encoding="utf-8") as shard:
      rows.extend(json.loads(line) for line in shard if line.strip())
  return rows


def _is_evaluation_dir(directory: str) -> bool:
  return any(os.path.isdir(os.path.join(directory, t)) for t in TABLES)


def read_local(directory: str, evaluation_id: str | None = None) -> Evaluation:
  """The evaluation written under `directory` (module docstring).

  Raises:
    NoSuchEvaluationError: no evaluation output there, no registry row,
      or rows of several evaluations and no `evaluation_id` to choose one.
  """
  root = directory
  nested = os.path.join(directory, evaluation_id or "")
  if evaluation_id and _is_evaluation_dir(nested):
    root = nested
  if not _is_evaluation_dir(root):
    wanted = f" for {evaluation_id}" if evaluation_id else ""
    raise NoSuchEvaluationError(
        f"{directory} holds no evaluation output{wanted} (no {REGISTRY}/ "
        "directory): pass the evaluation's own directory, <--output_local>/"
        "<evaluation_id>, or the --output_local directory with "
        "--evaluation_id")
  events = _shards(root, REGISTRY)
  ids = sorted({str(e.get("evaluation_id")) for e in events})
  if evaluation_id is None:
    if len(ids) != 1:
      raise NoSuchEvaluationError(f"{root} holds registry rows of {len(ids)} "
                                  f"evaluations ({ids}); pass --evaluation_id")
    evaluation_id = ids[0]
  own = [e for e in events if e.get("evaluation_id") == evaluation_id]
  if not own:
    raise NoSuchEvaluationError(f"{root} has no registry row for evaluation "
                                f"{evaluation_id} (it holds {ids})")

  def rows(name: str) -> list[dict[str, Any]]:
    return [
        r for r in _shards(root, name)
        if r.get("evaluation_id") == evaluation_id
    ]

  return Evaluation(
      evaluation_id=evaluation_id,
      events=_by_recorded(own),
      metrics=rows(METRICS),
      profiles=rows(PROFILES),
      origin=root)
