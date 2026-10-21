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
"""The metric catalogue v1: the evaluation contract (E0, Task 3).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import copy
import json
import re
from importlib import resources

import pytest
import yaml

from sdfb_evaluation import catalogue
from sdfb_evaluation import schemas

_REQUIRED_V1_IDS = (
    "field.category_adherence",
    "field.range_adherence",
    "field.shape_adherence",
    "field.substantive_copy_rate",
    "field.value_memorization_lift",
    "field.pool_memorization_lift",
    "field.type_validity",
    "column.null_rate_delta",
    "column.empty_rate_delta",
    "column.ks",
    "column.pit_w1",
    "column.wasserstein",
    "column.decile_ks_legacy",
    "column.jsd",
    "column.tvd",
    "column.psi",
    "column.smd",
    "column.std_ratio",
    "column.zero_rate_delta",
    "column.range_coverage",
    "column.dow_tvd",
    "column.month_tvd",
    "column.hour_tvd",
    "column.cohens_w",
    "column.top1_share_delta",
    "column.coverage_mass",
    "column.novelty_mass",
    "column.entropy_ratio",
    "column.distinct_ratio",
    "column.distinct_ceiling_hit",
    "column.length_ks",
    "column.shape_head_tv",
    "column.char_class_l1",
    "column.source_stats_drift",
    "pair.pearson_delta",
    "pair.spearman_delta",
    "pair.cramers_v_delta",
    "pair.nmi_delta",
    "pair.contingency_tvd",
    "row.exact_match_rate",
    "row.exact_match_rate_nonkey",
    "row.memorization_lift",
    "row.exposure_lift",
    "row.near_match_rate",
    "row.near_match_lift",
    "row.internal_duplicate_excess",
    "row.dcr_train_holdout_share",
    "row.dcr_p5_ratio",
    "row.nndr_p5_ratio",
    "row.density",
    "row.coverage",
    "row.null_pattern_tvd",
    "table.row_count_ratio",
    "table.pk_duplicate_rate",
    "table.identity_duplicate_rate",
    "table.detection_auc",
    "table.pmse_ratio",
    "table.corr_rms_delta",
    "table.corr_max_delta",
    "table.column_shape_score",
    "table.pair_trend_score",
    "table.fidelity_score",
    "table.privacy_score",
    "table.integrity_score",
    "table.diversity_score",
    "table.overall_score",
    "relationship.orphan_rate",
    "relationship.orphan_rate_source",
    "relationship.fanout_tvd",
    "relationship.fanout_w1",
    "relationship.fanout_mean_ratio",
    "relationship.zero_child_share_delta",
    "relationship.cardinality_adherence",
    "relationship.parent_coverage",
    "model.overall_score",
    "model.fidelity_score",
    "model.privacy_score",
    "model.integrity_score",
    "model.diversity_score",
)

_LEVELS = ("field", "column", "pair", "row", "table", "relationship", "model")
_MEASURED_FAMILIES = {"fidelity", "privacy", "integrity", "diversity"}
_COLUMN_SCOPED_LEVELS = {"field", "column", "pair"}

# Ruling R1: the lifts and the holdout share gate on their CI lower bound.
_CI_BOUND_TARGETS = {
    "field.value_memorization_lift": 1.0,
    "field.pool_memorization_lift": 1.0,
    "row.memorization_lift": 1.0,
    "row.exposure_lift": 1.0,
    "row.near_match_lift": 1.0,
    "row.dcr_train_holdout_share": 0.5,
}

# D5: computed at matched n (the brief's bold `n_dep` column).
_N_DEPENDENT = {
    "column.entropy_ratio",
    "column.distinct_ratio",
    "row.dcr_train_holdout_share",
    "row.dcr_p5_ratio",
    "row.nndr_p5_ratio",
    "row.density",
    "row.coverage",
    "table.detection_auc",
    "table.pmse_ratio",
    # Ruling R13: duplicate shares grow with n, so both sides use matched m.
    "row.internal_duplicate_excess",
}

# Integrity by construction: FAIL iff value > 0.
_ZERO_TOLERANCE = {
    "table.pk_duplicate_rate",
    "table.identity_duplicate_rate",
    "relationship.orphan_rate",
}

# "-/-" rows: INFO only, no thresholds, no score.
_INFO_ONLY = {
    "column.wasserstein",
    "relationship.orphan_rate_source",
    "relationship.fanout_w1",
}

# A `target` metric whose target is data-dependent (the source's Good-Turing
# unseen mass), carried per row as `source_value` instead of in the YAML.
_RUNTIME_TARGET = {"column.novelty_mass"}

# Ruling R12: informational targets on non-target metrics whose perfect value
# is not 0; they are the D5 noise reference.
_INFORMATIONAL_TARGETS = {
    "table.detection_auc": 0.5,
    "field.category_adherence": 1.0,
    "field.range_adherence": 1.0,
    "field.shape_adherence": 1.0,
    "relationship.cardinality_adherence": 1.0,
}

# Standard KaTeX macros the formulas may use (the GUI renders them with
# stock KaTeX: no custom macros). Extend it only with macros from the KaTeX
# support table: https://katex.org/docs/supported.html
_KATEX_MACROS = {
    "\\#", "\\,", "\\{", "\\}", "\\Delta", "\\Pr", "\\alpha", "\\bar", "\\cap",
    "\\cup", "\\delta", "\\dots", "\\ell", "\\chi", "\\exists", "\\frac",
    "\\ge", "\\hat", "\\in", "\\infty", "\\int", "\\le", "\\left", "\\ln",
    "\\log", "\\lvert", "\\mathbf", "\\mathcal", "\\max", "\\min", "\\mu",
    "\\notin", "\\nu", "\\operatorname", "\\pi", "\\quad", "\\rho", "\\right",
    "\\rvert", "\\sigma", "\\sqrt", "\\sum", "\\text", "\\tilde", "\\to",
    "\\varphi"
}
# A control word (\frac) or a control symbol (\{, \, or \#).
_MACRO = re.compile(r"\\(?:[A-Za-z]+|[^A-Za-z])")


def _raw_catalogue() -> dict:
  text = resources.files("sdfb_evaluation.catalogue").joinpath(
      "metrics.yaml").read_text(encoding="utf-8")
  data: dict = yaml.safe_load(text)
  return data


@pytest.fixture(name="cat", scope="module")
def _cat() -> catalogue.Catalogue:
  return catalogue.load_catalogue()


def test_catalogue_is_versioned_and_leveled(cat):
  assert cat.version == "1.0.0"
  assert cat.levels == _LEVELS
  assert set(cat.families) >= _MEASURED_FAMILIES
  # `overall` is the roll-up family: only the *.overall_score rows carry it.
  assert set(cat.families) - _MEASURED_FAMILIES == {"overall"}
  for m in cat.metrics:
    assert m.family in _MEASURED_FAMILIES or m.id.endswith(".overall_score")


def test_ids_unique_and_prefixed_by_level(cat):
  ids = cat.ids()
  assert len(ids) == len(set(ids))
  for m in cat.metrics:
    level, _, name = m.id.partition(".")
    assert level == m.level and m.level in cat.levels, m.id
    assert re.fullmatch(r"[a-z][a-z0-9_]*", name), m.id


def test_required_v1_ids_present(cat):
  missing = sorted(set(_REQUIRED_V1_IDS) - set(cat.ids()))
  assert not missing
  assert len(cat.ids()) == len(_REQUIRED_V1_IDS)


def test_every_entry_documented(cat):
  for m in cat.metrics:
    for text in (m.title, m.version, m.formula, m.purpose,
                 m.interpretation.good, m.interpretation.bad, m.pitfalls):
      assert isinstance(text, str) and text.strip(), m.id
    # A popover line, not an essay: at most two sentences.
    assert len(re.findall(r"[.!?](?:\s|$)", m.purpose.strip())) <= 2, m.id


def test_references_are_https_and_labelled(cat):
  for m in cat.metrics:
    assert isinstance(m.references, tuple), m.id
    for ref in m.references:
      assert ref.label.strip(), m.id
      assert ref.url.startswith("https://") and " " not in ref.url, (m.id,
                                                                     ref.url)


def test_vocabularies_are_known(cat):
  for m in cat.metrics:
    assert m.value_kind in catalogue.VALUE_KINDS, m.id
    assert set(m.estimator.split("/")) <= set(catalogue.ESTIMATORS), m.id
    assert m.direction in catalogue.DIRECTIONS, m.id
    assert m.score_fn in catalogue.SCORE_FNS, m.id
    assert m.noise_floor is None or m.noise_floor in catalogue.NOISE_METHODS
    assert m.noise_floor != "none", m.id
    assert m.threshold_source in catalogue.THRESHOLD_SOURCES, m.id
    assert isinstance(m.kinds, tuple), m.id
    assert set(m.kinds) <= set(catalogue.KINDS), m.id


def test_noise_method_none_loads_as_python_none(cat):
  raw = {e["id"]: e for e in _raw_catalogue()["metrics"]}
  for m in cat.metrics:
    assert raw[m.id]["noise_floor"] in catalogue.NOISE_METHODS, m.id
    assert (m.noise_floor is None) == (raw[m.id]["noise_floor"] == "none")


def test_kinds_scope_by_level(cat):
  for m in cat.metrics:
    if m.level in _COLUMN_SCOPED_LEVELS:
      assert m.kinds, m.id
    else:
      assert m.kinds == (), m.id


def test_thresholds_are_numbers_and_ordered_by_direction(cat):
  for m in cat.metrics:
    for bound in (m.warn, m.fail):
      # float, never int/bool: the loader normalises every bound.
      assert bound is None or isinstance(bound, float), m.id
    assert (m.warn is None) == (m.fail is None), m.id
    assert (m.warn is None) == (m.threshold_source == "none"), m.id
    if m.warn is None:
      continue
    assert m.warn is not None and m.fail is not None  # narrowing for mypy
    if m.direction == "higher_better":
      assert m.warn >= m.fail, m.id
    else:
      # lower_better on the value; target on |value - target|.
      assert 0.0 <= m.warn <= m.fail, m.id


def test_score_fn_matches_direction(cat):
  for m in cat.metrics:
    if m.score_fn == "complement":
      assert m.direction == "lower_better" and m.range == (0.0, 1.0), m.id
    elif m.score_fn == "auc":
      assert m.direction == "lower_better" and m.value_kind == "auc", m.id
    elif m.score_fn == "ratio_to_one":
      assert m.direction == "target", m.id
    elif m.score_fn == "linear":
      assert m.direction in {"lower_better", "higher_better"}, m.id
      assert m.warn is not None and m.fail is not None, m.id
    if m.direction == "target":
      assert m.score_fn == "ratio_to_one", m.id
      assert m.target is not None or m.id in _RUNTIME_TARGET, m.id


def test_ranges_well_formed(cat):
  for m in cat.metrics:
    lo, hi = m.range
    assert lo is None or isinstance(lo, float), m.id
    assert hi is None or isinstance(hi, float), m.id
    if lo is not None and hi is not None:
      assert lo < hi, m.id
    if m.target is not None:
      assert lo is None or m.target >= lo, m.id
      assert hi is None or m.target <= hi, m.id


def test_flags_are_booleans(cat):
  for m in cat.metrics:
    for flag in (m.n_dependent, m.baseline, m.uses_ci_bound):
      assert isinstance(flag, bool), m.id


def test_n_dependent_set_is_the_matched_n_metrics(cat):
  assert {m.id for m in cat.metrics if m.n_dependent} == _N_DEPENDENT


def test_baseline_only_on_fidelity_and_diversity(cat):
  for m in cat.metrics:
    if m.baseline:
      assert m.family in {"fidelity", "diversity"}, m.id


def test_ci_bound_metrics_follow_ruling_r1(cat):
  assert {m.id for m in cat.metrics if m.uses_ci_bound
         } == set(_CI_BOUND_TARGETS)
  for metric_id, target in _CI_BOUND_TARGETS.items():
    m = cat.get(metric_id)
    assert m.direction == "lower_better" and m.score_fn == "linear", metric_id
    assert m.target == target, metric_id
    assert m.warn is not None and m.fail is not None
    assert target < m.warn < m.fail, metric_id


def test_ratio_to_one_scores_the_distance_band(cat):
  # Ruling R10: ratio_to_one scores d = |value - target|, 1 at or under warn
  # and 0 at or over fail, so every such metric needs both thresholds.
  for m in cat.metrics:
    if m.score_fn == "ratio_to_one":
      assert m.direction == "target", m.id
      assert m.warn is not None and m.fail is not None, m.id
  assert {
      m.id for m in cat.metrics if m.direction == "target" and m.target is None
  } == _RUNTIME_TARGET


def test_novelty_mass_takes_its_target_from_the_row(cat):
  # Ruling R9: the target is the source's Good-Turing unseen mass, carried
  # as the row's source_value; t = 0 is legal (d is then the value).
  m = cat.get("column.novelty_mass")
  assert (m.direction, m.target, m.score_fn) == ("target", None, "ratio_to_one")
  assert (m.warn, m.fail) == (0.10, 0.25)
  assert m.range == (0.0, 1.0)


def test_informational_targets_follow_ruling_r12(cat):
  for metric_id, target in _INFORMATIONAL_TARGETS.items():
    m = cat.get(metric_id)
    assert m.direction != "target" and m.target == target, metric_id


def test_noise_reference_defined_for_every_noise_gated_metric(cat):
  # D5 gates a FAIL on the distance from a reference: the target when set,
  # else 0 (lower_better) or the top of the range (higher_better).
  for m in cat.metrics:
    if m.noise_floor is None or m.target is not None:
      continue
    assert m.direction == "lower_better", m.id
  for m in cat.metrics:
    if m.direction == "higher_better" and m.noise_floor is not None:
      assert m.target is not None and m.range[1] == m.target, m.id


def test_metrics_schema_descriptions_list_catalogue_vocabularies(cat):
  described = {
      f["name"]: f["description"]
      for f in schemas.load_schema("evaluation_metrics")
  }
  for field, vocabulary in (("level", cat.levels), ("family", cat.families),
                            ("column_kind", catalogue.KINDS),
                            ("value_kind", catalogue.VALUE_KINDS),
                            ("method", catalogue.ESTIMATORS)):
    for word in vocabulary:
      assert re.search(rf"\b{word}\b", described[field]), (field, word)
  for method in catalogue.NOISE_METHODS:
    if method != "none":
      assert method in described["noise_floor_method"], method


def test_integrity_zero_tolerance_and_info_rows(cat):
  for metric_id in _ZERO_TOLERANCE:
    m = cat.get(metric_id)
    assert (m.family, m.warn, m.fail) == ("integrity", 0.0, 0.0), metric_id
  for metric_id in _INFO_ONLY:
    m = cat.get(metric_id)
    assert (m.warn, m.fail, m.score_fn) == (None, None, "none"), metric_id


def test_formulas_use_standard_katex_only(cat):
  for m in cat.metrics:
    depth = 0
    for char in m.formula.replace("\\{", "").replace("\\}", ""):
      depth += {"{": 1, "}": -1}.get(char, 0)
      assert depth >= 0, m.id
    assert depth == 0, m.id
    unknown = set(_MACRO.findall(m.formula)) - _KATEX_MACROS
    assert not unknown, (m.id, unknown)


def test_get_and_by_level(cat):
  ks = cat.get("column.ks")
  assert (ks.direction, ks.warn, ks.fail) == ("lower_better", 0.10, 0.20)
  assert ks.score_fn == "complement" and ks.noise_floor == "ks_two_sample"
  with pytest.raises(KeyError):
    cat.get("column.no_such_metric")
  for level in cat.levels:
    members = cat.by_level(level)
    assert members and all(m.level == level for m in members), level
  assert sum(len(cat.by_level(level)) for level in cat.levels) == len(
      cat.metrics)
  assert cat.ids() == tuple(m.id for m in cat.metrics)


def test_json_export_is_byte_stable(cat):
  first = cat.to_json()
  assert first == catalogue.load_catalogue().to_json()
  parsed = json.loads(first)
  assert json.dumps(
      parsed, sort_keys=True, indent=2, ensure_ascii=False) + "\n" == first
  assert parsed["catalogue_version"] == "1.0.0"
  assert [m["id"] for m in parsed["metrics"]] == list(cat.ids())
  assert set(parsed["metrics"][0]) == {
      "id", "title", "version", "level", "family", "kinds", "value_kind",
      "estimator", "direction", "target", "range", "warn", "fail", "score_fn",
      "noise_floor", "n_dependent", "baseline", "uses_ci_bound", "formula",
      "purpose", "interpretation", "pitfalls", "references", "threshold_source"
  }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("n_dependent", "true"),  # a string, not a YAML boolean
        ("score", "sigmoid"),
        ("noise_floor", "bootstrap"),
        ("direction", "closer_better"),
        ("id", "tabel.ks"),  # prefix is not a level
        (
            "thresholds",
            {
                "warn": "1e-4",  # PyYAML reads an undotted exponent as a string
                "fail": 1.0e-3,
                "source": "heuristic"
            }),
        ("references", [{
            "label": "plain http",
            "url": "http://example.com"
        }]),
    ],
)
def test_parser_rejects_malformed_entry(key, value):
  data = _raw_catalogue()
  data["metrics"][0][key] = value
  with pytest.raises(ValueError):
    catalogue.parse_catalogue(data)


def test_parser_rejects_missing_unknown_and_duplicate_entries():
  base = _raw_catalogue()
  missing_key = copy.deepcopy(base)
  del missing_key["metrics"][0]["pitfalls"]
  unknown_key = copy.deepcopy(base)
  unknown_key["metrics"][0]["weight"] = 2
  duplicate = copy.deepcopy(base)
  duplicate["metrics"].append(copy.deepcopy(base["metrics"][0]))
  for data in (missing_key, unknown_key, duplicate):
    with pytest.raises(ValueError):
      catalogue.parse_catalogue(data)
