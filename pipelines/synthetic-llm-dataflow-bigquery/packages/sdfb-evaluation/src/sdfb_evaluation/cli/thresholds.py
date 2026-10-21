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
"""`--thresholds_uri`: a run's own warn and fail thresholds (Ruling
R93-6).

The catalogue owns the default thresholds; a run may override them for
the metrics a YAML file names (a local path or `gs://`):

    thresholds:
      column.ks: {warn: 0.05, fail: 0.10}
      row.coverage: {warn: 0.90, fail: 0.80}

The override holds for the whole run, and nothing grades twice:

    this module            the file, validated before anything starts
          │                (`cli.main`: a bad file is a usage error)
          ▼
    driver.run ─► build_evaluation_pipeline(thresholds=)
          │              ▼
          │       assemble.RowContext ─► scoring.to_metric_row(thresholds=)
          │              ▼
          │       every `evaluation_metrics` row of a named metric: its
          │       status, score, threshold_warn and threshold_fail
          │              ▼
          │       the roll-ups and the registry's counts and scores (they
          │       read the rows), then the --fail_on gate (it reads the
          │       registry's counts)
          ▼
    the registry's `evaluation_params`: `thresholds_uri` and
    `thresholds_digest` (`thresholds_digest` of what the file held), both
    NULL for a run graded by the catalogue alone

An override moves no measured value, so it is not part of the
`evaluation_key`: two runs that differ only in their thresholds are the
same evaluation, told apart by the digest.

What a file may say (anything else is refused, naming the file):

    a metric id            one the packaged catalogue has, and grades: a
                           metric the catalogue only reports (no
                           thresholds of its own) cannot be given a gate
    warn and fail          both, each a finite number, 0 or more
    their order            lower_better and target (thresholds on
                           |value - target|): warn <= fail;
                           higher_better: warn >= fail, and not both 0 —
                           the scorer reads 0 / 0 as the zero-tolerance
                           rule "any value above 0 fails"

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

import yaml

from sdfb_evaluation.catalogue import Metric, load_catalogue
from sdfb_evaluation.scoring import Thresholds

__all__ = ["load_thresholds", "thresholds_digest"]

_TOP = "thresholds"
_BOUNDS = ("warn", "fail")
_HIGHER = "higher_better"
_SHOWN_CHARS = 40  # of a refused value, in the error


def _read_text(uri: str) -> str:
  if uri.startswith("gs://"):
    from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel  # only a gs:// URI needs Beam's filesystems

    with FileSystems.open(uri) as handle:
      content: bytes = handle.read()
    return content.decode("utf-8")
  with open(uri, encoding="utf-8") as local:
    return local.read()


def _bound(metric_id: str, name: str, value: Any) -> float:
  """`value` as a float bound, or a `ValueError`: also for an integer
  no float can hold (YAML integers have no size limit)."""
  bound = math.nan
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    try:
      bound = float(value)
    except OverflowError:
      bound = math.inf
  if not math.isfinite(bound) or bound < 0:
    shown = repr(value)
    if len(shown) > _SHOWN_CHARS:
      shown = f"{shown[:_SHOWN_CHARS]}… ({len(shown)} characters)"
    raise ValueError(f"{metric_id}.{name}: expected a number, finite and 0 "
                     f"or more, got {shown}")
  return bound


def _pair(metric: Metric, entry: Any) -> tuple[float, float]:
  """The (warn, fail) `entry` gives `metric` (module docstring)."""
  metric_id, direction = metric.id, metric.direction
  if not isinstance(entry, Mapping):
    raise ValueError(f"{metric_id}: expected a mapping with warn and fail, "
                     f"got {entry!r}")
  for name in _BOUNDS:
    if name not in entry:
      raise ValueError(f"{metric_id}: {name} is missing (an override names "
                       "both warn and fail)")
  unknown = sorted(str(key) for key in set(entry) - set(_BOUNDS))
  if unknown:
    raise ValueError(f"{metric_id}: unknown key(s) {unknown}; expected warn "
                     "and fail")
  if metric.warn is None or metric.fail is None:
    raise ValueError(f"{metric_id} is reported, never graded: the catalogue "
                     "gives it no thresholds, and an override cannot add a "
                     "gate")
  warn, fail = (_bound(metric_id, name, entry[name]) for name in _BOUNDS)
  if direction == _HIGHER:
    if warn < fail:
      raise ValueError(f"{metric_id} is {direction}: expected warn >= fail, "
                       f"got warn {warn}, fail {fail}")
    if warn == 0 and fail == 0:
      raise ValueError(f"{metric_id} is {direction}: warn and fail cannot "
                       "both be 0 (the scorer reads 0 / 0 as: any value "
                       "above 0 fails)")
  elif warn > fail:
    raise ValueError(f"{metric_id} is {direction}: expected warn <= fail, "
                     f"got warn {warn}, fail {fail}")
  return warn, fail


def _parse(document: Any) -> dict[str, tuple[float, float]]:
  entries = document.get(_TOP) if isinstance(document, Mapping) else None
  if not isinstance(entries, Mapping):
    raise ValueError("expected a top-level `thresholds` mapping of metric id "
                     "to {warn, fail}")
  extra = sorted(str(key) for key in set(document) - {_TOP})
  if extra:
    raise ValueError(f"unknown top-level key(s) {extra}; expected only "
                     "`thresholds`")
  catalogue = load_catalogue()
  known = set(catalogue.ids())
  overrides = {}
  for metric_id, entry in entries.items():
    if metric_id not in known:
      raise ValueError(f"{metric_id!r} is not a catalogue metric id "
                       "(`sdfb-eval catalogue --format md` lists them)")
    overrides[metric_id] = _pair(catalogue.get(metric_id), entry)
  return dict(sorted(overrides.items()))


def load_thresholds(uri: str) -> dict[str, tuple[float, float]]:
  """`{metric id: (warn, fail)}` from the YAML at `uri` (a local path or
  `gs://`), validated against the packaged catalogue (module docstring),
  as floats and ordered by metric id.

  Raises:
    ValueError: the file is not YAML, or does not say what an override
      may say; the message names `uri`.
    OSError: the file cannot be read.
  """
  text = _read_text(uri)
  try:
    return _parse(yaml.safe_load(text))
  except yaml.YAMLError as exc:
    problem = " ".join(str(exc).split())
    raise ValueError(f"{uri}: not valid YAML ({problem})") from exc
  except ValueError as exc:
    raise ValueError(f"{uri}: {exc}") from exc


def thresholds_digest(overrides: Thresholds) -> str:
  """The digest the registry records for `overrides` (blake2b-128, hex):
  of the metric ids and their two bounds, whatever the file's order,
  spelling or comments."""
  normalised = {
      metric_id: [float(warn), float(fail)]
      for metric_id, (warn, fail) in overrides.items()
  }
  text = json.dumps({_TOP: normalised}, sort_keys=True, separators=(",", ":"))
  return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()
