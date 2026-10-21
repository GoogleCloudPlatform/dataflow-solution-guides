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
"""The metric catalogue: one versioned YAML, three readers (the E0 contract).

`metrics.yaml` is the single source of truth for every metric this package
computes. Three readers consume it:

- the scoring code, which reads `direction`, `warn`/`fail`, `score_fn`,
  `noise_floor`, `uses_ci_bound`, `baseline`, `n_dependent`, `value_kind`
  and `version` to label, score and assign a status to each metric row;
- the design doc, whose catalogue table is rendered from the YAML;
- the GUI, whose info popovers show `title`, `purpose`, `formula` (KaTeX),
  `interpretation`, `pitfalls` and `references`. The GUI build has no
  Python: it reads the raw YAML (keys as written there, e.g. `score` and
  `thresholds.warn`), never this module.

`to_json()` serves the Python side: the `sdfb-eval catalogue` CLI and the
docs tooling. It flattens each entry into `Metric`'s field names.

The YAML's header comment documents each key's semantics. This module
parses it strictly: a missing or unknown key, an unknown vocabulary value,
a non-boolean flag or a non-numeric threshold is a `ValueError`, so a bad
edit fails at load time instead of mis-scoring a run.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

import yaml

DIRECTIONS: tuple[str, ...] = ("lower_better", "higher_better", "target")
SCORE_FNS: tuple[str, ...] = (
    "complement",
    "linear",
    "ratio_to_one",
    "auc",
    "none",
)
NOISE_METHODS: tuple[str, ...] = ("ks_two_sample", "wilson", "newcombe",
                                  "tvd_null", "jsd_null", "fisher_z", "mi_bias",
                                  "rate_ratio", "delong", "none")
ESTIMATORS: tuple[str, ...] = ("exact", "binned", "sketch", "sample",
                               "value_sampled")
VALUE_KINDS: tuple[str, ...] = ("distance", "share", "ratio", "bits",
                                "correlation_delta", "auc", "score", "count")
KINDS: tuple[str, ...] = ("numeric", "temporal", "categorical", "boolean",
                          "text", "identifier", "nested")
# `none` only when a metric has no thresholds at all (INFO rows).
THRESHOLD_SOURCES: tuple[str, ...] = ("literature", "repo-continuity",
                                      "heuristic", "none")

_PACKAGE = "sdfb_evaluation.catalogue"
_TOP_KEYS = frozenset({"catalogue_version", "levels", "families", "metrics"})
_ENTRY_KEYS = frozenset({
    "id", "version", "title", "level", "family", "kinds", "value_kind",
    "estimator", "direction", "target", "range", "thresholds", "score",
    "noise_floor", "n_dependent", "baseline", "uses_ci_bound", "formula",
    "purpose", "interpretation", "pitfalls", "references"
})
_THRESHOLD_KEYS = frozenset({"warn", "fail", "source"})
_RANGE_LEN = 2  # [lo, hi]


@dataclasses.dataclass(frozen=True)
class Reference:
  """A primary source: a label and an https URL (DOI, arXiv or docs)."""
  label: str
  url: str


@dataclasses.dataclass(frozen=True)
class Interpretation:
  """How to read a value: what a good and a bad value mean."""
  good: str
  bad: str


@dataclasses.dataclass(frozen=True)
class Metric:  # pylint: disable=too-many-instance-attributes  # a catalogue row is wide by design
  """One catalogue entry, flattened for the scoring code.

  `warn`, `fail` and `threshold_source` come from the YAML's `thresholds`
  mapping and `score_fn` from its `score` key. `noise_floor` is the noise
  method's name, or None when the YAML says `none`. `range` is `(lo, hi)`,
  either bound None when unbounded.
  """
  id: str
  title: str
  version: str
  level: str
  family: str
  kinds: tuple[str, ...]
  value_kind: str
  estimator: str
  direction: str
  target: float | None
  range: tuple[float | None, float | None]
  warn: float | None
  fail: float | None
  score_fn: str
  noise_floor: str | None
  n_dependent: bool
  baseline: bool
  uses_ci_bound: bool
  formula: str
  purpose: str
  interpretation: Interpretation
  pitfalls: str
  references: tuple[Reference, ...]
  threshold_source: str


@dataclasses.dataclass(frozen=True)
class Catalogue:
  """The whole catalogue: its version, vocabularies and metrics in file order."""
  version: str
  levels: tuple[str, ...]
  families: tuple[str, ...]
  metrics: tuple[Metric, ...]
  _index: Mapping[str, Metric] = dataclasses.field(
      init=False, repr=False, compare=False)

  def __post_init__(self) -> None:
    index: dict[str, Metric] = {}
    for metric in self.metrics:
      if metric.id in index:
        raise ValueError(f"duplicate metric id {metric.id!r}")
      index[metric.id] = metric
    object.__setattr__(self, "_index", index)

  def ids(self) -> tuple[str, ...]:
    """Every metric id, in file order."""
    return tuple(metric.id for metric in self.metrics)

  def get(self, metric_id: str) -> Metric:
    """The metric with this id; `KeyError` if the catalogue has none."""
    return self._index[metric_id]

  def by_level(self, level: str) -> tuple[Metric, ...]:
    """The metrics of one level, in file order."""
    return tuple(metric for metric in self.metrics if metric.level == level)

  def to_json(self) -> str:
    """The catalogue as canonical JSON: sorted keys, 2-space indent, `\\n`-terminated.

    For the Python CLI and docs tooling (the GUI reads the raw YAML).
    Byte-stable for a given YAML, so a saved copy can be diffed against it
    to detect drift.
    """
    payload = {
        "catalogue_version": self.version,
        "levels": list(self.levels),
        "families": list(self.families),
        "metrics": [dataclasses.asdict(metric) for metric in self.metrics],
    }
    return json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _fail(where: str, message: str) -> ValueError:
  return ValueError(f"metrics.yaml {where}: {message}")


def _require_keys(where: str, entry: Mapping[str, Any],
                  expected: frozenset[str]) -> None:
  if not isinstance(entry, Mapping):
    raise _fail(where, "expected a mapping")
  missing = sorted(expected - set(entry))
  unknown = sorted(set(entry) - expected)
  if missing or unknown:
    raise _fail(where, f"missing keys {missing}, unknown keys {unknown}")


def _text(where: str, value: Any) -> str:
  if not isinstance(value, str) or not value.strip():
    raise _fail(where, f"expected non-empty text, got {value!r}")
  return value


def _one_of(where: str, value: Any, allowed: tuple[str, ...]) -> str:
  if value not in allowed:
    raise _fail(where, f"{value!r} is not one of {list(allowed)}")
  return str(value)


def _flag(where: str, value: Any) -> bool:
  if not isinstance(value, bool):
    raise _fail(where, f"expected a boolean, got {value!r}")
  return value


def _number(where: str, value: Any) -> float | None:
  """A float or None. Rejects bools and strings (e.g. an undotted `1e-4`)."""
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise _fail(where, f"expected a number or null, got {value!r}")
  return float(value)


def _references(where: str, value: Any) -> tuple[Reference, ...]:
  if not isinstance(value, list):
    raise _fail(where, "references must be a list")
  refs = []
  for item in value:
    _require_keys(where, item, frozenset({"label", "url"}))
    url = _text(where, item["url"])
    if not url.startswith("https://"):
      raise _fail(where, f"reference url must be https: {url!r}")
    refs.append(Reference(label=_text(where, item["label"]), url=url))
  return tuple(refs)


def _metric(entry: Mapping[str, Any], levels: tuple[str, ...],
            families: tuple[str, ...]) -> Metric:
  """One validated `Metric` from its YAML entry."""
  claimed_id = entry.get("id") if isinstance(entry, Mapping) else None
  where = f"entry {claimed_id!r}"
  _require_keys(where, entry, _ENTRY_KEYS)
  metric_id = _text(where, entry["id"])
  level = _one_of(where, entry["level"], levels)
  if not metric_id.startswith(f"{level}."):
    raise _fail(where, f"id must start with its level {level!r}")
  thresholds = entry["thresholds"]
  _require_keys(where, thresholds, _THRESHOLD_KEYS)
  kinds = entry["kinds"]
  if not isinstance(kinds, list):
    raise _fail(where, "kinds must be a list")
  estimator = _text(where, entry["estimator"])
  for part in estimator.split("/"):
    _one_of(where, part, ESTIMATORS)
  value_range = entry["range"]
  if not isinstance(value_range, list) or len(value_range) != _RANGE_LEN:
    raise _fail(where, "range must be a [lo, hi] pair")
  interpretation = entry["interpretation"]
  _require_keys(where, interpretation, frozenset({"good", "bad"}))
  noise = _one_of(where, entry["noise_floor"], NOISE_METHODS)
  return Metric(
      id=metric_id,
      title=_text(where, entry["title"]),
      version=_text(where, entry["version"]),
      level=level,
      family=_one_of(where, entry["family"], families),
      kinds=tuple(_one_of(where, kind, KINDS) for kind in kinds),
      value_kind=_one_of(where, entry["value_kind"], VALUE_KINDS),
      estimator=estimator,
      direction=_one_of(where, entry["direction"], DIRECTIONS),
      target=_number(where, entry["target"]),
      range=(_number(where, value_range[0]), _number(where, value_range[1])),
      warn=_number(where, thresholds["warn"]),
      fail=_number(where, thresholds["fail"]),
      score_fn=_one_of(where, entry["score"], SCORE_FNS),
      noise_floor=None if noise == "none" else noise,
      n_dependent=_flag(where, entry["n_dependent"]),
      baseline=_flag(where, entry["baseline"]),
      uses_ci_bound=_flag(where, entry["uses_ci_bound"]),
      formula=_text(where, entry["formula"]),
      purpose=_text(where, entry["purpose"]),
      interpretation=Interpretation(
          good=_text(where, interpretation["good"]),
          bad=_text(where, interpretation["bad"]),
      ),
      pitfalls=_text(where, entry["pitfalls"]),
      references=_references(where, entry["references"]),
      threshold_source=_one_of(where, thresholds["source"], THRESHOLD_SOURCES),
  )


def parse_catalogue(data: Mapping[str, Any]) -> Catalogue:
  """A validated `Catalogue` from the parsed YAML document.

  Raises:
    ValueError: on a missing or unknown key, an unknown vocabulary value,
      a non-boolean flag, a non-numeric threshold, a non-https reference or
      a duplicate id.
  """
  _require_keys("top level", data, _TOP_KEYS)
  levels = tuple(str(level) for level in data["levels"])
  families = tuple(str(family) for family in data["families"])
  metrics = data["metrics"]
  if not isinstance(metrics, list) or not metrics:
    raise _fail("top level", "metrics must be a non-empty list")
  return Catalogue(
      version=_text("top level", data["catalogue_version"]),
      levels=levels,
      families=families,
      metrics=tuple(_metric(entry, levels, families) for entry in metrics),
  )


def load_catalogue() -> Catalogue:
  """The packaged `metrics.yaml`, parsed and validated."""
  text = resources.files(_PACKAGE).joinpath("metrics.yaml").read_text(
      encoding="utf-8")
  return parse_catalogue(yaml.safe_load(text))
