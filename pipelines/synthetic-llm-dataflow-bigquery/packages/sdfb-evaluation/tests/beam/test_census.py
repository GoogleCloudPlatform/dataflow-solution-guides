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
"""Tests for `sdfb_evaluation.beam.census` (Task 22): the keyed value
census, value-hash sampling, the value/pool memorization lifts and the
top-k / shape_mix profiles under the D6 literal policy (R56, R64).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pickle
import random
import time as clock
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.utils.windowed_value import WindowedValue

from sdfb_evaluation.beam import census, dense
from sdfb_evaluation.beam.census import (
    MAX_PREAGG_KEYS,
    OWNED_METRIC_IDS,
    POOL_CAP,
    RARE_COUNT,
    TOPK_ITEMS,
    CensusMetrics,
    CensusPreAggregateFn,
    CensusSpec,
    CountsCombineFn,
    FreeTextPool,
    batch_counts,
    census_outputs,
    census_refs,
    pools_from_rows,
    pools_sql,
    read_pools,
    shape_masks,
)
from sdfb_evaluation.beam.dense import DenseMetrics
from sdfb_evaluation.beam.encode import BatchEncoder, EncodeSide
from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.canonical import hash64
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context import budget as budget_module
from sdfb_evaluation.context import plan as plan_module
from sdfb_evaluation.scoring import status_for, to_metric_row, to_profile_row
from sdfb_evaluation.stats import distances, noise, shapes
from sdfb_evaluation.stats.diversity import CensusAccumulator, summarize
from sdfb_evaluation.types import MetricValue, ProfileValue, Status

from .census_data import (
    LABEL_KEY,
    SALT,
    encode,
    identity_table,
    totals_for,
    users_table,
    with_census,
)
from .dense_data import planned_table

_CATALOGUE = load_catalogue()
_TOL = 1e-9
_SENTINEL_PREFIXES = ("0001-", "9999-")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _pure(table: Any,
          rows_by: Mapping[str, Sequence[Mapping[str, Any]]],
          *,
          pools: Mapping[str, FreeTextPool] | None = None,
          label_key: bytes = LABEL_KEY) -> census.CensusResult:
  """The in-process path: encode, dense totals, census."""
  batches = {side: encode(table, side, rows) for side, rows in rows_by.items()}
  totals = totals_for(table, batches)
  spec = CensusSpec.from_table(table)
  refs = census_refs(table, pools=pools)
  return census_outputs(
      spec, [b for side in batches.values() for b in side],
      totals,
      refs,
      label_key=label_key)


def _collect(pcoll: beam.PCollection, path: Path, label: str) -> None:

  def dump(actual: Sequence[Any]) -> None:
    path.write_bytes(pickle.dumps(list(actual)))

  assert_that(pcoll, dump, label=label)


def _run(
    table: Any,
    rows_by: Mapping[str, list],
    tmp_path: Path,
    *,
    pools: Mapping[str, Mapping[str, FreeTextPool]] | None = None
) -> tuple[list[MetricValue], list[ProfileValue], dict]:
  """The Beam path on the local runner (FnApiRunner): read, encode, dense, census."""
  sources = InMemorySources({
      (table.name, side): rows for side, rows in rows_by.items()
  })
  with BeamTestPipeline() as p:
    encoded = [
        sources.read(p, table, side) | EncodeSide(table, side, salt=SALT)
        for side in rows_by
    ]
    batches = encoded | "Flatten" >> beam.Flatten()
    key = p | "Key" >> beam.Create([LABEL_KEY])
    dense_out = batches | "Dense" >> DenseMetrics([table], label_key=key)
    out = ({
        "batches": batches,
        "accumulators": dense_out["accumulators"]
    }
           | "Census" >> CensusMetrics([table], label_key=key, pools=pools))
    _collect(out["metrics"], tmp_path / "metrics.pkl", "Metrics")
    _collect(out["profiles"], tmp_path / "profiles.pkl", "Profiles")
    _collect(out["summaries"], tmp_path / "summaries.pkl", "Summaries")
  metrics = pickle.loads((tmp_path / "metrics.pkl").read_bytes())
  profiles = pickle.loads((tmp_path / "profiles.pkl").read_bytes())
  summaries = dict(pickle.loads((tmp_path / "summaries.pkl").read_bytes()))
  return metrics, profiles, summaries


def _by_key(metrics: Sequence[MetricValue]) -> dict[tuple, MetricValue]:
  out: dict[tuple, MetricValue] = {}
  for mv in metrics:
    key = (mv.metric_id, mv.column)
    assert key not in out, f"duplicate row {key}"
    out[key] = mv
  return out


def _substantive(value: Any) -> bool:
  if isinstance(value, str):
    return value.strip() != "" and not value.startswith(_SENTINEL_PREFIXES)
  if isinstance(value, date):
    return value.year not in (1, 9999)
  return True


def _pure_accumulator(
    table: Any, j: int,
    rows_by: Mapping[str, Sequence[Mapping[str, Any]]]) -> CensusAccumulator:
  """The Task 8 accumulator of plan column j, counted with a Counter over
  `hash64` codes (the matched-n flags are the encoder's own); a
  value-sampled column counts only the head and the hash range."""
  name = table.columns[j].name
  column = CensusSpec.from_table(table).columns[j]
  full: dict[str, Counter] = {}
  matched: dict[str, Counter] = {}
  substantive: dict[int, bool] = {}
  for side, rows in rows_by.items():
    flags = BatchEncoder.from_table(table, side, salt=SALT).encode(rows)
    full[side], matched[side] = Counter(), Counter()
    for row, flag in zip(rows, flags.subsample_m.tolist(), strict=True):
      value = row[name]
      if value is None:
        continue
      code = hash64(name, value)
      if not column.kept(code):
        continue
      substantive[code] = _substantive(value)
      full[side][code] += 1
      if flag:
        matched[side][code] += 1
  acc = CensusAccumulator()
  for code in sorted(set(full["source"]) | set(full["synthetic"])):
    acc.add(
        code,
        full["source"][code],
        full["synthetic"][code],
        matched["source"][code],
        matched["synthetic"][code],
        substantive=substantive[code])
  return acc


def _floats(obj: Any) -> list[float]:
  if isinstance(obj, float):
    return [obj] if math.isfinite(obj) else []
  if isinstance(obj, Mapping):
    return [x for value in obj.values() for x in _floats(value)]
  if isinstance(obj, (list, tuple)):
    return [x for value in obj for x in _floats(value)]
  return []


def _column_index(table: Any, name: str) -> int:
  return [c.name for c in table.columns].index(name)


def _counts(rows: Sequence[Mapping[str, Any]], name: str) -> Counter:
  return Counter(r[name] for r in rows if r[name] is not None)


@pytest.fixture(scope="module", name="users_run")
def fixture_users_run(tmp_path_factory: pytest.TempPathFactory) -> tuple:
  """One local-runner (FnApiRunner) pass over the users table, shared by the tests that
  only read its output; `sku` is value-sampled (R67's head/tail design)
  so the Beam path is held to the in-process one there too."""
  table, rows = users_table()
  table = with_census(table, sku=("value_sampled", 0.25))
  metrics, profiles, summaries = _run(table, rows,
                                      tmp_path_factory.mktemp("users"))
  return table, rows, metrics, profiles, summaries


# --------------------------------------------------------------------------
# the brief's tests
# --------------------------------------------------------------------------
def test_census_matches_pure_summary(users_run):
  table, rows, metrics, profiles, summaries = users_run
  censused = [j for j, c in enumerate(table.columns) if c.census != "none"]
  assert censused, "the users plan censuses its non-key columns"
  assert set(summaries) == {(table.name, j) for j in censused}
  for j in censused:
    expected = summarize(_pure_accumulator(table, j, rows))
    got = summarize(summaries[(table.name, j)])
    assert got.keys() == expected.keys()
    for key, value in expected.items():
      if isinstance(value, float):  # 12 digits (R21); merge-order ulps
        assert got[key] == pytest.approx(value, rel=1e-10), (j, key)
      else:
        assert got[key] == value, (j, key)
  # the Beam path equals the in-process path, metric by metric
  pure = _pure(table, rows)
  beam_rows, pure_rows = _by_key(metrics), _by_key(pure.metrics)
  assert beam_rows.keys() == pure_rows.keys()
  for key, mv in beam_rows.items():
    other = pure_rows[key]
    for name in ("value", "baseline_value", "noise_floor", "ci_low", "ci_high",
                 "source_value", "synthetic_value"):
      a, b = getattr(mv, name), getattr(other, name)
      if a is None or b is None:
        assert a is b, (key, name, a, b)
      elif math.isinf(a) or math.isinf(b):
        assert a == b, (key, name)
      else:
        # 12 significant digits (R21): merge-order ulps may straddle one
        assert a == pytest.approx(b, rel=1e-10, abs=1e-12), (key, name)

  def payloads(values: Sequence[ProfileValue]) -> list[tuple]:
    return sorted((pv.profile_kind, pv.side, str(pv.column), pv.n, pv.truncated,
                   json.dumps(pv.payload, sort_keys=True)) for pv in values)

  assert payloads(profiles) == payloads(pure.profiles)
  # value distances equal the pure stats on the aligned count vectors
  for name in ("status", "city", "is_member"):
    src = _counts(rows["source"], name)
    syn = _counts(rows["synthetic"], name)
    _, p, q = distances.align(src, syn)
    assert beam_rows[("column.tvd", name)].value == pytest.approx(
        distances.tvd(p, q), abs=_TOL)
    assert beam_rows[("column.jsd", name)].value == pytest.approx(
        distances.jsd_bits(p, q), abs=_TOL)
    w = distances.cohens_w(p, q)
    assert w is not None
    cohens = beam_rows[("column.cohens_w", name)]
    assert cohens.value == pytest.approx(w.w, abs=_TOL)
    assert cohens.detail["q_mass_on_p0"] == pytest.approx(
        w.q_mass_on_p0, abs=_TOL)
    ref = _counts(rows["source"][:600], name)
    _, p_src, r = distances.align(src, ref)
    assert beam_rows[("column.tvd", name)].baseline_value == pytest.approx(
        distances.tvd(p_src, r), abs=_TOL)


def test_census_high_cardinality_profile_truncates_metrics_exact(users_run):
  table, rows, metrics, profiles, summaries = users_run
  by_key = _by_key(metrics)
  name = "full_name"
  j = _column_index(table, name)
  src, syn = _counts(rows["source"], name), _counts(rows["synthetic"], name)
  assert len(src) > 1000  # past the accumulator's top-1000 list
  acc = summaries[(table.name, j)]
  assert len(acc.top_src) == 1000
  assert acc.k_src == len(src) and acc.k_syn == len(syn)
  # metrics read every value, not the truncated lists
  novelty = by_key[("column.novelty_mass", name)]
  novel = sum(c for v, c in syn.items() if v not in src)
  assert novelty.value == pytest.approx(novel / sum(syn.values()), abs=_TOL)
  singletons = sum(1 for c in src.values() if c == 1)
  assert novelty.source_value == pytest.approx(
      singletons / sum(src.values()), abs=_TOL)
  distinct = by_key[("column.distinct_ratio", name)]
  assert distinct.detail["distinct_src"] == len(src)
  assert distinct.detail["distinct_syn"] == len(syn)
  # the profile truncates: at most TOPK_ITEMS labelled items plus "other"
  topk = [
      pv for pv in profiles if pv.profile_kind == "topk" and pv.column == name
  ]
  assert {pv.side for pv in topk} == {"source", "synthetic"}
  for pv in topk:
    payload = pv.payload
    assert len(payload["items"]) == TOPK_ITEMS
    assert pv.truncated is True
    total = sum(item["count"] for item in payload["items"])
    assert total + payload["other_count"] == payload["total"]
    counts = src if pv.side == "source" else syn
    assert payload["distinct"] == len(counts)
    assert payload["total"] == sum(counts.values())
    assert all(not item["literal"] for item in payload["items"])


def test_value_sampling_above_budget_is_unbiased():
  rng = np.random.default_rng(5)
  n_src, n_syn = 60_000, 40_000
  fields = ({
      "name": "row_id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "handle",
      "type": "STRING",
      "mode": "NULLABLE"
  })
  # source: 45k distinct handles, most of them rare (a long tail)
  handles = [f"h{int(k):06d}" for k in rng.integers(0, 45_000, n_src)]
  source = [{"row_id": i, "handle": h} for i, h in enumerate(handles)]
  # synthetic: 30 % copy a source handle, the rest are invented
  synthetic = []
  for i in range(n_syn):
    if rng.random() < 0.3:
      handle = handles[int(rng.integers(0, n_src))]
    else:
      handle = f"n{int(rng.integers(0, 10**7)):07d}"
    synthetic.append({"row_id": 10**6 + i, "handle": handle})
  rows = {"source": source, "synthetic": synthetic}
  planned = planned_table(
      "handles", fields, source, synthetic, pk=("row_id",), pair_max_columns=0)
  exact = _by_key(
      _pure(with_census(planned, handle=("exact", None)), rows).metrics)
  sampled_table = with_census(planned, handle=("value_sampled", 0.02))
  sampled = _by_key(_pure(sampled_table, rows).metrics)

  distinct = sampled[("column.distinct_ratio", "handle")]
  assert distinct.method.value == "value_sampled"
  assert distinct.sample_rate == 0.02
  truth = exact[("column.distinct_ratio", "handle")]
  for key in ("distinct_src", "distinct_syn"):
    assert distinct.detail[key] == pytest.approx(
        truth.detail[key], rel=0.10), key
  for field in ("source_value", "synthetic_value"):  # matched-n distinct
    assert getattr(distinct, field) == pytest.approx(
        getattr(truth, field), rel=0.10), field

  copy = sampled[("field.substantive_copy_rate", "handle")]
  exact_copy = exact[("field.substantive_copy_rate", "handle")].value
  assert copy.ci_low is not None and copy.ci_high is not None
  assert copy.ci_low <= exact_copy <= copy.ci_high
  assert copy.method.value == "value_sampled"
  # the Horvitz-Thompson entropy lands near the exact plug-in entropy
  entropy = sampled[("column.entropy_ratio", "handle")]
  truth_entropy = exact[("column.entropy_ratio", "handle")]
  assert entropy.source_value == pytest.approx(
      truth_entropy.source_value, rel=0.05)
  # only the head (with certainty) and the tail's hash range enter
  spec = CensusSpec.from_table(sampled_table)
  head = spec.columns[1].head
  assert head, "the planner's census head (source + synthetic top values)"
  counts = batch_counts(spec, encode(sampled_table, "source", source)[0])
  keep = round(0.02 * budget_module.VALUE_SAMPLE_MODULUS)
  assert counts.values
  assert all(code in head or code % budget_module.VALUE_SAMPLE_MODULUS < keep
             for (_, _, code) in counts.values)
  assert any(code % budget_module.VALUE_SAMPLE_MODULUS >= keep
             for (_, _, code) in counts.values)
  # the top values sit in the certainty stratum: top-1 is exact (R67)
  top1 = sampled[("column.top1_share_delta", "handle")]
  assert top1.value == exact[("column.top1_share_delta", "handle")].value


def test_value_lift_detects_planted_rare_copies(tmp_path):
  table, rows = users_table(copies_from_reference=300)
  metrics, _, _ = _run(table, rows, tmp_path)
  lift = _by_key(metrics)[("field.value_memorization_lift", "full_name")]
  assert lift.ci_low is not None and lift.ci_low >= 5
  assert lift.detail["copies_r"] >= 250 and lift.detail["copies_h"] == 0
  family = lift.detail["bonferroni_family"]
  assert family >= 2 and lift.detail["alpha"] == pytest.approx(0.05 / family)
  assert status_for(_CATALOGUE.get(lift.metric_id), lift) is Status.FAIL
  # the untouched run: no copies on either side, the bound passes
  clean = _by_key(
      _pure(*users_table()).metrics)[("field.value_memorization_lift",
                                      "full_name")]
  assert clean.value is None and clean.ci_low == 0.0
  assert status_for(_CATALOGUE.get(clean.metric_id), clean) is Status.PASS


def test_literal_policy_hashes_rare_values(users_run):
  table, rows, _, profiles, _ = users_run
  payloads = [pv for pv in profiles if pv.profile_kind in ("topk", "shape_mix")]
  assert payloads
  labels: set[str] = set()
  for pv in payloads:
    for item in pv.payload["items"]:
      labels.add(item.get("label", item.get("mask")))
  text = json.dumps([pv.payload for pv in profiles], ensure_ascii=False)
  source_values: Counter = Counter()
  synthetic_values: set[str] = set()
  for column in table.columns:
    if column.bq_type != "STRING":
      continue
    source_values.update((column.name, r[column.name])
                         for r in rows["source"]
                         if r[column.name] is not None)
    synthetic_values.update(
        r[column.name] for r in rows["synthetic"] if r[column.name])
  rare = {v for (_, v), c in source_values.items() if c < RARE_COUNT and v}
  assert rare, "the users table holds rare source values"
  assert not rare & labels
  assert not [v for v in rare if len(v) >= 5 and v in text]
  synthetic_only = synthetic_values - {v for _, v in source_values}
  assert synthetic_only and not synthetic_only & labels
  # a frequent value of a literal-ok column is shown as itself
  status = [pv for pv in payloads if pv.column == "status"]
  shown = {item["label"] for pv in status for item in pv.payload["items"]}
  assert {"active", "dormant"} <= shown
  # labels are keyed (R64): enumerating the source domain with an unkeyed
  # formula recovers none of them
  codes = {hash64(name, value) for (name, value) in source_values}
  unkeyed = {f"h:{(code >> 32) & 0xFFFFFFFF:08x}" for code in codes} | {
      "h:" +
      hashlib.blake2b(code.to_bytes(8, "big"), digest_size=4).hexdigest()
      for code in codes
  }
  hashed = {label for label in labels if label.startswith("h:")}
  assert hashed and not hashed & unkeyed


def test_labels_are_keyed_and_stable_within_a_key():
  table, rows = users_table(n_source=1500, n_synthetic=1000, n_reference=300)

  def city_labels(key: bytes) -> dict[tuple[str, int], str]:
    result = _pure(table, rows, label_key=key)
    return {
        (pv.side, rank): item["label"] for pv in result.profiles
        if pv.profile_kind == "topk" and pv.column == "city"
        for rank, item in enumerate(pv.payload["items"])
    }

  first, again, other = (city_labels(LABEL_KEY), city_labels(LABEL_KEY),
                         city_labels(b"another-32-byte-label-key-000000"))
  assert first == again
  hashed = [k for k, label in first.items() if label.startswith("h:")]
  assert hashed and all(first[k] != other[k] for k in hashed)
  # one value carries one label on both sides within a run
  by_count = {}
  for pv in _pure(table, rows).profiles:
    if pv.profile_kind == "topk" and pv.column == "city":
      for item in pv.payload["items"]:
        by_count.setdefault(item["label"], set()).add(pv.side)
  assert any(sides == {"source", "synthetic"} for sides in by_count.values())


def test_numeric_and_day_temporal_copy_rate_is_info(users_run):
  _, _, metrics, _, _ = users_run
  by_key = _by_key(metrics)
  metric = _CATALOGUE.get("field.substantive_copy_rate")
  for name in ("age", "signup_date"):
    mv = by_key[(metric.id, name)]
    assert mv.value is not None
    assert status_for(metric, mv) is Status.INFO, name
    row = to_metric_row(
        mv,
        evaluation_id="e1",
        evaluated_at="2026-09-29T00:00:00+00:00",
        landing_table=None,
        source_table=None)
    assert row["status"] == "info" and row["score"] is None
    assert "domain" in row["detail"]["reason"]
  assert by_key[(metric.id, "signup_date")].detail["day_granularity"] is True
  # R66: gated only on free text; every other kind reports INFO
  for name in ("last_login", "full_name", "city"):
    mv = by_key[(metric.id, name)]
    assert status_for(metric, mv) is Status.INFO, name
  bio = by_key[(metric.id, "bio")]
  assert bio.column_kind == "text"
  assert status_for(metric, bio) is not Status.INFO


# --------------------------------------------------------------------------
# coverage, lifts, pools
# --------------------------------------------------------------------------
def test_catalogue_coverage_every_owned_id_emitted_or_explained(users_run):
  table, _, metrics, profiles, _ = users_run
  ids = set(_CATALOGUE.ids())
  assert set(OWNED_METRIC_IDS) <= ids
  emitted = {mv.metric_id for mv in metrics}
  assert emitted == set(OWNED_METRIC_IDS), sorted(
      set(OWNED_METRIC_IDS) ^ emitted)
  rows_per_id: Counter = Counter()
  for mv in metrics:
    metric = _CATALOGUE.get(mv.metric_id)
    assert mv.column is not None and mv.column_kind in metric.kinds, mv
    assert mv.column_kind in {str(k) for k in census.APPLIES_TO[mv.metric_id]}
    if mv.value is None and not metric.uses_ci_bound:
      assert mv.detail.get("reason"), mv
    # M3: every float in detail at 12 significant digits (R21)
    for x in _floats(mv.detail):
      assert float(f"{x:.12g}") == x, (mv.metric_id, x)
    assert mv.encoding_plan_digest == table.encoding_plan_digest
    json.dumps(
        to_metric_row(
            mv,
            evaluation_id="e1",
            evaluated_at="2026-09-29T00:00:00+00:00",
            landing_table=table.landing_table,
            source_table=table.source_table),
        allow_nan=False)
    rows_per_id[mv.metric_id] += 1
  # one row per applicable (id, column): the reach, within the catalogue's
  # kinds (column.jsd's numeric/temporal side is the dense pass's)
  assert set(census.APPLIES_TO) == set(OWNED_METRIC_IDS)
  for metric_id in OWNED_METRIC_IDS:
    kinds = set(_CATALOGUE.get(metric_id).kinds)
    reach = {str(k) for k in census.APPLIES_TO[metric_id]}
    assert reach <= kinds, metric_id
    if metric_id != "column.jsd":
      assert reach == kinds, metric_id
    applicable = [c for c in table.columns if str(c.kind) in reach]
    assert rows_per_id[metric_id] == len(applicable), metric_id
  # a key column (census none) is explained, never silently skipped
  keyed = [
      mv for mv in metrics
      if mv.column == "user_id" and mv.metric_id == "column.distinct_ratio"
  ]
  assert all("census" in mv.detail["reason"] for mv in keyed)
  # with the dense pass, every field/column id has an owner (R62: Task 26
  # owns column.source_stats_drift)
  field_column = {
      m.id for m in _CATALOGUE.metrics if m.level in ("field", "column")
  }
  owned = set(OWNED_METRIC_IDS) | set(dense.OWNED_METRIC_IDS)
  assert field_column - owned == {"column.source_stats_drift"}
  for pv in profiles:
    json.dumps(
        to_profile_row(
            pv, evaluation_id="e1", evaluated_at="2026-09-29T00:00:00+00:00"),
        allow_nan=False)


def test_reference_unverified_marks_lifts_not_evaluated():
  table, rows = users_table(copies_from_reference=300, verified=False)
  by_key = _by_key(_pure(table, rows).metrics)
  for name in ("full_name", "city", "age"):
    mv = by_key[("field.value_memorization_lift", name)]
    assert mv.value is None and mv.ci_low is None
    assert "reference" in mv.detail["reason"]
  # fidelity is still computed
  assert by_key[("column.tvd", "city")].value is not None


def test_pool_lift_needs_the_pool_side_input_and_detects_reference_values():
  table, rows = users_table()
  bios = [r["bio"] for r in rows["source"]]
  pure = _by_key(_pure(table, rows).metrics)
  missing = pure[("field.pool_memorization_lift", "bio")]
  assert missing.value is None and "side input" in missing.detail["reason"]
  pool = FreeTextPool(
      column="bio",
      values=tuple(bios[:120] + bios[600:605]),
      target=POOL_CAP,
      reference_digest="d" * 64,
      model_uri="gs://bucket/synthetic/models/m/v1/")
  lifted = _by_key(_pure(table, rows, pools={"bio": pool}).metrics)
  lift = lifted[("field.pool_memorization_lift", "bio")]
  assert lift.detail["copies_r"] == 120 and lift.detail["copies_h"] == 5
  assert lift.ci_low is not None and lift.ci_low > 5
  assert status_for(_CATALOGUE.get(lift.metric_id), lift) is Status.FAIL
  other = _by_key(_pure(table, rows, pools={
      "other": pool
  }).metrics)[("field.pool_memorization_lift", "bio")]
  assert "no free-text pool" in other.detail["reason"]


def test_distinct_ceiling_hit_flags_the_pool_cap():
  n = 2000
  fields = ({
      "name": "row_id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "note",
      "type": "STRING",
      "mode": "NULLABLE"
  })
  words = " was delivered on time and in perfect condition"
  source = [{"row_id": i, "note": f"order {i}{words}"} for i in range(n)]
  synthetic = [{
      "row_id": 10**6 + i,
      "note": f"pool {i % POOL_CAP}{words}"
  } for i in range(n)]
  table = planned_table(
      "reviews", fields, source, synthetic, pk=("row_id",), pair_max_columns=0)
  assert table.columns[1].kind.value == "text"
  hit = _by_key(
      _pure(table, {
          "source": source,
          "synthetic": synthetic
      }).metrics)[("column.distinct_ceiling_hit", "note")]
  assert hit.value == 1.0 and hit.detail["distinct_syn"] == POOL_CAP
  miss = _by_key(_pure(table, {
      "source": source,
      "synthetic": source
  }).metrics)[("column.distinct_ceiling_hit", "note")]
  assert miss.value == 0.0


def test_read_pools_seam_binds_the_pool_identity():

  class FakeBq:

    def __init__(self):
      self.calls = []

    def query(self, sql, params=None, *, max_bytes=None):
      self.calls.append((sql, dict(params or {}), max_bytes))
      return [{
          "reference_digest": "d" * 64,
          "model_uri": "gs://bucket/m",
          "column": "bio",
          "target": 40,
          "values": ["a note", "another note"],
      }]

  bq = FakeBq()
  pools = read_pools(
      bq,
      pools_table="demo-project.synthetic_rag.freetext_pools",
      reference_digest="d" * 64,
      model_uri="gs://bucket/m",
      max_bytes=1 << 30)
  assert pools["bio"].values == ("a note", "another note")
  assert pools["bio"].target == 40
  sql, params, max_bytes = bq.calls[0]
  assert sql == pools_sql("demo-project.synthetic_rag.freetext_pools")
  assert params == {"reference_digest": "d" * 64, "model_uri": "gs://bucket/m"}
  assert max_bytes == 1 << 30
  assert "`values`" in sql
  assert not pools_from_rows([])
  with pytest.raises(ValueError):
    pools_sql("not a table")


# --------------------------------------------------------------------------
# building blocks
# --------------------------------------------------------------------------
def test_shape_masks_match_the_verbatim_port():
  values = [
      "AB-1234", "sk042", "", "東京 3丁目", "ǅemo", "½ cup", "٣ items", "a\tb",
      "😀x", "\ud800", "\x1c", "UPPER lower 99", "x" * 300
  ]
  assert shape_masks(values) == [shapes.shape_of(v) for v in values]


def test_shape_head_tv_follows_the_pure_head_pooling(users_run):
  table, rows, metrics, profiles, _ = users_run
  name = "sku"
  assert str(table.columns[_column_index(table, name)].kind) == "identifier"

  def mass(side_rows):
    masks = Counter(
        shapes.shape_of(r[name])
        for r in side_rows
        if r[name] not in (None, ""))
    total = sum(masks.values())
    return {m: c / total for m, c in masks.items()}

  src, syn = mass(rows["source"]), mass(rows["synthetic"])
  raw, head, heads = shapes.shape_head_tv(src, syn)
  mv = _by_key(metrics)[("column.shape_head_tv", name)]
  assert mv.value == pytest.approx(head, abs=_TOL)
  assert mv.detail["raw_tv"] == pytest.approx(raw, abs=_TOL)
  assert mv.noise_floor is not None and mv.noise_floor > 0
  mixes = [
      pv for pv in profiles
      if pv.profile_kind == "shape_mix" and pv.column == name
  ]
  assert {pv.side for pv in mixes} == {"source", "synthetic"}
  for pv in mixes:
    assert {item["mask"] for item in pv.payload["items"]} == heads
    assert pv.payload["head_floor"] == 0.02
    shares = sum(item["share"] for item in pv.payload["items"])
    assert shares + pv.payload["tail_share"] == pytest.approx(1.0)
  adherence = _by_key(metrics)[("field.shape_adherence", name)]
  inside = sum(m for s, m in syn.items() if s in src)
  assert adherence.value == pytest.approx(inside, abs=_TOL)
  assert adherence.ci_low <= adherence.value <= adherence.ci_high
  # long prose has no head mask: not evaluated, with the reason
  bio = _by_key(metrics)[("column.shape_head_tv", "bio")]
  assert bio.value is None and "2 %" in bio.detail["reason"]


def _signed_key(key: tuple) -> tuple:
  table, j, code = key
  return (table, j, code - 2**64 if code >= 2**63 else code)


def _drain(outputs: Sequence[Any]) -> tuple[dict, dict]:
  """Sum a DoFn's main and `masks` outputs by key."""
  totals: dict[tuple, list[int]] = {}
  masks: dict[tuple, list[Any]] = {}
  combine = CountsCombineFn()
  mask_combine = census.MaskCountsCombineFn()
  for output in outputs:
    tag, element = None, output
    if isinstance(element, beam.pvalue.TaggedOutput):
      tag, element = element.tag, element.value
    if isinstance(element, WindowedValue):
      element = element.value
    key, value = element
    if tag == "masks":
      masks[key] = mask_combine.add_input(
          masks.get(key) or mask_combine.create_accumulator(), value)
    elif tag is None:
      totals[key] = combine.add_input(
          totals.get(key) or combine.create_accumulator(), value)
  return totals, masks


@pytest.mark.parametrize("max_keys", [50, 3000])
def test_preaggregation_flushes_early_and_never_drops(max_keys):
  table, rows = users_table(n_source=800, n_synthetic=600, n_reference=200)
  table = with_census(table, sku=("value_sampled", 0.3))
  spec = CensusSpec.from_table(table)
  batches = encode(table, "source", rows["source"], chunk=200)
  batches += encode(table, "synthetic", rows["synthetic"], chunk=200)
  fn = CensusPreAggregateFn({table.name: spec}, max_keys=max_keys)
  fn.setup()
  fn.start_bundle()
  early = []
  for batch in batches:
    early.extend(fn.process(batch))
    # M1: never more than max_keys held (or one column-batch's keys)
    assert fn.held <= max(max_keys, 2 * batch.n)
  late = list(fn.finish_bundle())
  assert early, "a full dict flushes before the bundle ends"
  totals, masks = _drain(early + late)
  expected = batch_counts(spec, batches[0])
  for batch in batches[1:]:
    census.accumulate(spec, batch, expected)
  # a Beam key carries each code as the int64 with the same 64 bits
  assert {
      k: tuple(v) for k, v in totals.items()
  } == {
      _signed_key(k): tuple(v) for k, v in expected.values.items()
  }
  assert {
      k: tuple(v) for k, v in masks.items()
  } == {
      _signed_key(k): tuple(v) for k, v in expected.masks.items()
  }
  assert MAX_PREAGG_KEYS == 100_000


def test_constants_match_the_planner():
  assert RARE_COUNT == plan_module.LITERAL_MIN_COUNT
  assert census.VALUE_SAMPLE_MODULUS == budget_module.VALUE_SAMPLE_MODULUS
  assert not hasattr(census, "HOT_KEY_FANOUT")  # M2: no value-level fanout


def test_throughput_census_batch(record_property):
  table, rows = users_table(n_source=8192, n_synthetic=100, n_reference=100)
  spec = CensusSpec.from_table(table)
  batch = encode(table, "source", rows["source"], chunk=8192)[0]
  batch_counts(spec, batch)  # warm-up
  start = clock.perf_counter()
  counts = batch_counts(spec, batch)
  elapsed = clock.perf_counter() - start
  record_property("census_rows_per_s", round(batch.n / elapsed))
  assert counts.values
  assert elapsed < 30


def test_identity_columns_are_censused_by_their_text():
  table, rows = identity_table(copies=150)
  spec = CensusSpec.from_table(table)
  by_name = {c.name: c for c in spec.columns}
  assert by_name["email"].code_block == "text"
  assert by_name["member_no"].code_block == "text"
  assert by_name["tier"].code_block == "nonkey"
  assert not by_name["id"].censused
  result = _pure(table, rows)
  for name in ("email", "member_no"):
    j = _column_index(table, name)
    expected: Counter = Counter()
    for side, side_rows in rows.items():
      for r in side_rows:
        expected[(side, hash64(name, str(r[name])))] += 1
    acc = result.summaries[j]
    assert acc.k_src == len(rows["source"]) and acc.k_both == 150
    assert acc.n_syn == sum(
        c for (s, _), c in expected.items() if s == "synthetic")
    top = {code for _, code in acc.top_src}
    assert top <= {code for (s, code) in expected if s == "source"}
  by_key = _by_key(result.metrics)
  for name in ("email", "member_no"):
    lift = by_key[("field.value_memorization_lift", name)]
    assert lift.detail["copies_r"] == 150 and lift.detail["copies_h"] == 0
    assert lift.ci_low is not None and lift.ci_low > 5


def test_empty_synthetic_side_is_not_evaluated_with_reasons():
  table, rows = users_table(n_source=900, n_synthetic=600, n_reference=200)
  result = _pure(table, {"source": rows["source"]})
  assert result.metrics
  for mv in result.metrics:
    assert mv.value is None and mv.ci_low is None, mv  # lifts too (M4)
    assert mv.detail["reason"], mv
  assert all(pv.side == "source" or pv.payload["total"] == 0
             for pv in result.profiles
             if pv.profile_kind == "topk")


def test_interval_metrics_carry_ci_and_scalar_metrics_a_noise_floor(users_run):
  _, _, metrics, _, _ = users_run
  for mv in metrics:
    if mv.value is None and mv.ci_low is None:
      continue
    method = _CATALOGUE.get(mv.metric_id).noise_floor
    if method in ("wilson", "newcombe", "rate_ratio"):
      assert mv.ci_low is not None and mv.ci_high is not None, mv
      assert mv.ci_low <= mv.ci_high, mv
      assert mv.noise_floor is None, mv
    elif method in ("tvd_null", "jsd_null"):
      assert mv.noise_floor is not None and mv.noise_floor >= 0, mv
      assert mv.ci_low is None and mv.ci_high is None, mv
    else:
      assert method in (None, "none"), mv
      assert mv.noise_floor is None and mv.ci_low is None, mv


# --------------------------------------------------------------------------
# value sampling (Ruling R67): stratified head/tail, guards, exact masks
# --------------------------------------------------------------------------
def _cities(n: int, seed: int, name: str) -> tuple[Any, dict[str, list]]:
  """A zipf city column whose top value holds about a quarter of the
  rows; the synthetic side halves it into invented values (a heavy-hitter
  distortion value sampling must not miss)."""
  rng = np.random.default_rng(seed)
  src = [f"Town{int(rng.zipf(1.3)) % 20000:05d}" for _ in range(n)]
  syn = [f"Town{int(rng.zipf(1.3)) % 20000:05d}" for _ in range(n)]
  top = Counter(src).most_common(1)[0][0]
  syn = [
      f"New{i:06d}" if v == top and i % 2 == 0 else v for i, v in enumerate(syn)
  ]
  fields = ({
      "name": "row_id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": name,
      "type": "STRING",
      "mode": "NULLABLE"
  })
  rows = {
      "source": [{
          "row_id": i,
          name: v
      } for i, v in enumerate(src)],
      "synthetic": [{
          "row_id": 10**6 + i,
          name: v
      } for i, v in enumerate(syn)],
  }
  table = planned_table(
      "cities",
      fields,
      rows["source"],
      rows["synthetic"],
      pk=("row_id",),
      pair_max_columns=0)
  return table, rows


def _without_head(table: Any, name: str) -> Any:
  columns = tuple(
      dataclasses.replace(c, census_head=None) if c.name == name else c
      for c in table.columns)
  return dataclasses.replace(table, columns=columns)


def test_value_sampled_heavy_hitter_is_exact_in_the_head_and_never_crashes():
  """The review probes (probe_heavy2/3): a heavy value that lands in the
  hash range used to push Σ c ln c past n ln n (ZeroDivisionError). The
  head stratum now counts it with certainty; without a head the HT view
  is validated and a metric out of range is not_evaluated, never a crash."""
  for name in ("city_c", "city_3", "city_8"):
    table, rows = _cities(20_000, 7, name)
    column = next(c for c in table.columns if c.name == name)
    assert column.census_head, "the planner fills the census head"
    exact = _by_key(
        _pure(with_census(table, **{name: ("exact", None)}), rows).metrics)
    sampled_table = with_census(table, **{name: ("value_sampled", 0.02)})
    sampled = _by_key(_pure(sampled_table, rows).metrics)
    for metric_id in ("column.tvd", "column.coverage_mass",
                      "column.top1_share_delta", "field.category_adherence"):
      e, s = exact[(metric_id, name)], sampled[(metric_id, name)]
      assert s.value == pytest.approx(e.value, abs=0.03), (name, metric_id)
    # the distorted head value is counted exactly: top-1 matches
    assert sampled[("column.top1_share_delta",
                    name)].value == exact[("column.top1_share_delta",
                                           name)].value
    # no head: the HT view is guarded, never a crash
    headless = _by_key(_pure(_without_head(sampled_table, name), rows).metrics)
    for (metric_id, column_name), mv in headless.items():
      if column_name != name:
        continue
      if mv.value is None and mv.ci_low is None:
        assert mv.detail["reason"], metric_id
      elif metric_id in ("column.tvd", "column.jsd", "column.coverage_mass",
                         "column.novelty_mass", "field.category_adherence"):
        assert 0.0 <= mv.value <= 1.0, (metric_id, mv.value)


def test_heavy_value_in_the_hash_range_is_guarded_without_a_head():
  """A heavy value forced into the tail's hash range (the head removed):
  its weight 1/rate pushes Σ c ln c past n ln n, so entropy is not
  evaluated with the reason instead of dividing by zero."""
  name = "city_c"
  table, rows = _cities(20_000, 7, name)
  top = Counter(r[name] for r in rows["source"]).most_common(1)[0][0]
  rate = (hash64(name, top) % budget_module.VALUE_SAMPLE_MODULUS + 1) / (
      budget_module.VALUE_SAMPLE_MODULUS)
  forced = _without_head(
      with_census(table, **{name: ("value_sampled", rate)}), name)
  spec = CensusSpec.from_table(forced)
  assert spec.columns[1].in_hash_range(hash64(name, top))
  by_key = _by_key(_pure(forced, rows).metrics)
  entropy = by_key[("column.entropy_ratio", name)]
  assert entropy.value is None
  assert "Horvitz-Thompson" in entropy.detail["reason"]
  for metric_id in ("column.tvd", "column.jsd"):
    mv = by_key[(metric_id, name)]
    assert mv.value is None or 0.0 <= mv.value <= 1.0


def test_empty_placeholder_in_the_sampled_range_is_counted_exactly():
  """probe_empty: users.sku's "" placeholder (about 5 % of rows) inside
  the sampled hash range — it is in the head, so it is exact; and with
  no head nothing crashes."""
  table, rows = users_table(
      n_source=12_000, n_synthetic=12_000, n_reference=600)
  r = hash64("sku", "") % budget_module.VALUE_SAMPLE_MODULUS
  rate = (r + 1) / budget_module.VALUE_SAMPLE_MODULUS
  sampled = with_census(table, sku=("value_sampled", rate))
  spec = CensusSpec.from_table(sampled)
  sku = next(c for c in spec.columns if c.name == "sku")
  assert hash64("sku", "") in sku.head and sku.in_hash_range(hash64("sku", ""))
  exact_table = with_census(table, sku=("exact", None))
  exact_run = _pure(exact_table, rows)
  exact = _by_key(exact_run.metrics)
  got_run = _pure(sampled, rows)
  got = _by_key(got_run.metrics)
  j = _column_index(table, "sku")
  # the placeholder is counted exactly: its row counts equal the exact ones
  top = {code: count for count, code in exact_run.summaries[j].top_src}
  sampled_top = {code: count for count, code in got_run.summaries[j].top_src}
  assert sampled_top[hash64("sku", "")] == top[hash64("sku", "")]
  assert got[("column.top1_share_delta",
              "sku")].value == exact[("column.top1_share_delta", "sku")].value
  # the rest carries the tail's Horvitz-Thompson noise (rate ~0.02 on a
  # near-unique column: about 0.05 standard error here)
  for metric_id in ("column.tvd", "column.coverage_mass",
                    "field.category_adherence"):
    e, s = exact[(metric_id, "sku")], got[(metric_id, "sku")]
    assert s.value == pytest.approx(e.value, abs=0.15), metric_id
  # R70: the copy rate's tail total is exact — the census counts every
  # substantive synthetic row before the value sample
  copy_key = ("field.substantive_copy_rate", "sku")
  assert got[copy_key].detail["substantive"] == exact[copy_key].detail[
      "substantive"]
  # M5: the baseline's missing-R term uses the driver's R mass on the same
  # draw, so the sampled baseline tracks the exact one
  e, s = exact[("column.tvd", "sku")], got[("column.tvd", "sku")]
  assert e.baseline_value is not None and s.baseline_value is not None
  assert s.baseline_value == pytest.approx(e.baseline_value, abs=0.15)
  refs = census_refs(sampled)
  assert 0.5 < refs[j].r_mass_ht < 1.5
  assert census_refs(exact_table)[j].r_mass_ht == 1.0
  headless = _pure(_without_head(sampled, "sku"), rows)
  assert any(mv.column == "sku" for mv in headless.metrics)


def _addresses(n_src: int, n_syn: int) -> tuple[Any, dict[str, list]]:
  rng = np.random.default_rng(3)
  words = ("Oak", "Elm", "Maplewood", "Pine", "Cedarbrook", "Birch", "Ash",
           "Willowdale", "Spruce", "Laurel")
  suffixes = ("St", "Ave", "Blvd", "Rd", "Ln", "Terrace", "Way")

  def addr() -> str:
    parts = [str(int(rng.integers(1, 99999)))]
    parts += [
        words[int(rng.integers(0, len(words)))]
        for _ in range(int(rng.integers(1, 4)))
    ]
    parts.append(suffixes[int(rng.integers(0, len(suffixes)))])
    if rng.random() < 0.3:
      parts.append(f"Apt {int(rng.integers(1, 999))}")
    return " ".join(parts)

  fields = ({
      "name": "row_id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "addr",
      "type": "STRING",
      "mode": "NULLABLE"
  })
  source = [{"row_id": i, "addr": addr()} for i in range(n_src)]
  synthetic = [{"row_id": 10**6 + i, "addr": addr()} for i in range(n_syn)]
  rows = {"source": source, "synthetic": synthetic}
  return planned_table(
      "addrs", fields, source, synthetic, pk=("row_id",),
      pair_max_columns=0), rows


def test_mask_pass_is_never_value_sampled():
  """probe_shape_sampling: shape metrics were biased low (0.639 FAIL vs
  0.978) when masks came from sampled values only; the mask pass now
  counts every value, so value-sampled equals exact."""
  table, rows = _addresses(20_000, 15_000)
  exact = _by_key(_pure(with_census(table, addr=("exact", None)), rows).metrics)
  sampled = _by_key(
      _pure(with_census(table, addr=("value_sampled", 0.02)), rows).metrics)
  for metric_id in ("field.shape_adherence", "column.shape_head_tv"):
    e, s = exact[(metric_id, "addr")], sampled[(metric_id, "addr")]
    assert s.value == e.value and s.ci_low == e.ci_low, metric_id
    assert s.method.value == "exact" and s.sample_rate is None
  assert exact[("field.shape_adherence", "addr")].value > 0.9
  # the copy rate's interval still bounds the exact rate with no sampled copy
  e = exact[("field.substantive_copy_rate", "addr")]
  s = sampled[("field.substantive_copy_rate", "addr")]
  assert s.ci_low <= e.value <= s.ci_high
  assert s.detail["interval"].startswith("cluster-robust")


def _pool_population(seed: int,
                     copies: int = 100,
                     pool: int = 512,
                     reps: int = 40) -> list[tuple[int, tuple[int, ...]]]:
  """A pool-driven text column: `pool` synthetic values, each repeated
  `reps` times; `copies` of them (at random) reproduce a rare source value
  (c_src = 1). Codes are fresh random hashes per seed (a new value
  sample)."""
  rng = np.random.default_rng(seed)
  codes = rng.integers(0, 2**63, size=pool, dtype=np.int64).tolist()
  copied = set(rng.choice(pool, size=copies, replace=False).tolist())
  return [(int(code), (1 if k in copied else 0, reps, 0, reps, 0, 0))
          for k, code in enumerate(codes)]


def _sampled_copy_coverage(copies: int, rate: float, head_size: int,
                           seeds: int) -> tuple[float, float, float, float]:
  """(coverage, row-level Wilson coverage, median width, median Wilson
  width) of the census's copy-rate interval over value samples of a
  512-value pool column, through the census's own per-value sums
  (`value_contribution`) and its tail total (every substantive row)."""
  table, _ = users_table(n_source=300, n_synthetic=300, n_reference=60)
  base = next(
      c for c in CensusSpec.from_table(table).columns if c.name == "bio")
  keep = round(rate * budget_module.VALUE_SAMPLE_MODULUS)
  # the column totals a dense pass would give
  sizes = census.ColumnSizes(20_000, 20_480, 20_000, 20_480, 20_000, 20_480)
  combine = census.ContributionCombineFn()
  covered = wilson_covered = 0
  widths, wilson_widths = [], []
  for seed in range(seeds):
    values = _pool_population(seed, copies=copies)
    head = frozenset(code for code, _ in values[:head_size])
    col = dataclasses.replace(
        base, census="value_sampled", rate=rate, keep=keep, head=head)
    total = combine.create_accumulator()
    k = n = 0
    for code, counts in values:
      if not col.kept(code):
        continue
      total = combine.add_input(
          total,
          census.value_contribution(col, code, counts, sizes, None,
                                    (None, None)))
      k += counts[1] if counts[0] else 0
      n += counts[1]
    substantive = sum(counts[1] for _, counts in values)
    ratio = noise.stratified_ratio_interval(
        census.stratified_share(total, "copy"),
        rate,
        tail_total=substantive - total.cr_hx)
    assert ratio is not None
    truth = sum(c[1] for _, c in values if c[0]) / substantive
    _, lo, hi = ratio
    covered += lo <= truth <= hi
    widths.append(hi - lo)
    w_lo, w_hi = noise.wilson_interval(k, n)
    wilson_covered += w_lo <= truth <= w_hi
    wilson_widths.append(w_hi - w_lo)
  return (covered / seeds, wilson_covered / seeds, float(np.median(widths)),
          float(np.median(wilson_widths)))


def test_sampled_copy_rate_interval_is_cluster_robust():
  """Rows of one value are a cluster: on a pool-driven column (512
  values, n/512 rows each) a row-level Wilson interval on the sampled
  rows is far too narrow. The census's interval (the head exact, K&G on
  the tail's ratio) covers the exact rate across value samples."""
  coverage, wilson, width, wilson_width = _sampled_copy_coverage(
      copies=100, rate=0.2, head_size=128, seeds=300)
  assert coverage >= 0.90, coverage
  assert wilson < 0.75, wilson  # the row-level interval fails
  assert width > 2 * wilson_width


@pytest.mark.parametrize(("copies", "rate", "head_size"), [
    (2, 0.2, 254),
    (2, 0.02, 254),
    (5, 0.02, 254),
    (5, 0.2, 128),
    (20, 0.05, 128),
])
def test_sampled_copy_rate_covers_when_few_copies_reach_the_tail(
    copies, rate, head_size):
  """R70 (the review's few-copied-pool-values scenarios): 2-20 copied
  values out of 512, a head of 128-254 — the interval must not collapse
  when few copies reach the sampled tail: coverage >= 0.85 over 300
  value samples."""
  coverage, *_ = _sampled_copy_coverage(copies, rate, head_size, seeds=300)
  assert coverage >= 0.85, (copies, rate, head_size, coverage)


def _share_coverage(pool_sizes: Callable[[np.random.Generator],
                                         np.ndarray], copies: int, rate: float,
                    head_size: int, seeds: int) -> float:
  """Coverage of `noise.stratified_ratio_interval` on a larger column,
  its sums built with numpy as the census builds them (head = the
  largest values, the planner's top list; tail Poisson-sampled)."""
  hits = 0
  for seed in range(seeds):
    rng = np.random.default_rng(seed)
    x = pool_sizes(rng).astype(float)
    y = np.zeros_like(x)
    copied = rng.choice(x.size, size=copies, replace=False)
    y[copied] = x[copied]
    head = np.zeros(x.size, dtype=bool)
    head[np.argsort(-x, kind="stable")[:head_size]] = True
    sampled = ~head & (rng.random(x.size) < rate)
    ys, xs = y[sampled], x[sampled]
    share = noise.StratifiedShare(y[head].sum(), x[head].sum(), ys.sum(),
                                  xs.sum(), (ys * ys).sum(), (ys * xs).sum(),
                                  (xs * xs).sum(), float((xs > 0).sum()))
    ratio = noise.stratified_ratio_interval(
        share, rate, tail_total=x[~head].sum())
    assert ratio is not None
    hits += ratio[1] <= y.sum() / x.sum() <= ratio[2]
  return hits / seeds


@pytest.mark.parametrize(("label", "copies", "rate"), [
    ("pool5000x40", 10, 0.02),
    ("pool5000x40", 50, 0.02),
    ("zipf20000", 200, 0.05),
    ("singletons40000", 23, 0.02),
])
def test_stratified_interval_covers_on_large_columns(label, copies, rate):
  """The review's larger scenarios (a 5000-value pool, a zipf-sized
  20000-value column, 40000 singletons), head 508, 300 value samples."""

  def sizes(rng: np.random.Generator) -> np.ndarray:
    if label == "pool5000x40":
      return np.full(5000, 40)
    if label == "zipf20000":
      return np.minimum(rng.zipf(1.6, size=20_000), 5000)
    return np.ones(40_000, dtype=int)

  assert _share_coverage(sizes, copies, rate, 508, seeds=300) >= 0.85


def test_value_lift_null_calibration_covers_one():
  """M9: a synthetic side that reproduces rare values of R and of H alike
  (no memorization of what the generator read) gives a lift whose CI
  covers 1, so it passes."""
  table, rows = users_table(copies_from_reference=150, copies_from_holdout=150)
  lift = _by_key(_pure(table, rows).metrics)[("field.value_memorization_lift",
                                              "full_name")]
  assert lift.detail["copies_r"] >= 140 and lift.detail["copies_h"] >= 140
  assert lift.ci_low <= 1.0 <= lift.ci_high
  assert status_for(_CATALOGUE.get(lift.metric_id), lift) is Status.PASS


def test_lifts_read_the_hash_range_only_on_a_sampled_column():
  """A value-sampled column's lift counts only hash-range values, so the
  head's certainty never tilts R against H."""
  table, rows = users_table(copies_from_reference=300)
  sampled = with_census(table, full_name=("value_sampled", 0.5))
  lift = _by_key(_pure(sampled, rows).metrics)[("field.value_memorization_lift",
                                                "full_name")]
  assert lift.method.value == "value_sampled"
  assert 100 < lift.detail["copies_r"] < 200  # about half of the copies
  assert lift.ci_low is not None and lift.ci_low > 5


def test_unobserved_sampled_column_is_not_evaluated_never_fabricated():
  """R75 (the review's p_novelty_empty): a headless (INT64 identity) value-
  sampled column whose synthetic values — copies of 3 source values — all
  fall outside the hash range. Nothing synthetic was observed, so novelty,
  the copy rate and the distinct ratio are not evaluated with the reason,
  never a fabricated 0 (novelty 1.0 used to PASS where the exact census
  FAILs)."""
  table, rows = identity_table()
  sampled = with_census(table, member_no=("value_sampled", 0.02))
  col = next(
      c for c in CensusSpec.from_table(sampled).columns
      if c.name == "member_no")
  assert not col.head  # an INT64 column has no planned top list
  outside = [
      r["member_no"]
      for r in rows["source"]
      if not col.kept(hash64("member_no", str(r["member_no"])))
  ][:3]
  synthetic = [
      dict(r, member_no=outside[i % 3]) for i, r in enumerate(rows["synthetic"])
  ]
  rows_by = {"source": rows["source"], "synthetic": synthetic}
  exact = _by_key(_pure(table, rows_by).metrics)
  got = _by_key(_pure(sampled, rows_by).metrics)
  novelty = _CATALOGUE.get("column.novelty_mass")
  assert status_for(novelty, exact[(novelty.id, "member_no")]) is Status.FAIL
  for metric_id, words in (
      ("column.novelty_mass", "no counted synthetic value"),
      ("field.substantive_copy_rate", "no substantive synthetic values"),
      ("column.distinct_ratio", "no counted synthetic value"),
  ):
    mv = got[(metric_id, "member_no")]
    assert mv.value is None and mv.ci_low is None, metric_id
    assert words in mv.detail["reason"], (metric_id, mv.detail["reason"])
    assert "hash range" in mv.detail["reason"]
    assert status_for(_CATALOGUE.get(metric_id),
                      mv) is Status.NOT_EVALUATED, metric_id
  # the sibling columns are untouched
  assert got[("column.novelty_mass", "email")].value is not None


# --------------------------------------------------------------------------
# sampled mode (Ruling R72; the final review's C1)
# --------------------------------------------------------------------------
_NEED_BOTH = ("column.distinct_ratio", "column.entropy_ratio")
_NEED_THE_SOURCE = ("field.category_adherence", "column.novelty_mass",
                    "field.substantive_copy_rate", "field.shape_adherence",
                    *_NEED_BOTH)
_NEED_THE_SYNTHETIC = ("column.coverage_mass", "column.distinct_ceiling_hit",
                       *_NEED_BOTH)


def _row_sampled(table: Any,
                 rows: Mapping[str, list],
                 *,
                 source: int = 1,
                 synthetic: int = 1) -> tuple[Any, dict[str, list]]:
  """What `--mode sampled` hands the census: the plan made from every row
  (the planning scan precedes the sample) and carrying the row rates, the
  pipeline reading every `source`-th / `synthetic`-th row of a side."""
  plan = dataclasses.replace(
      table,
      sample_rate_source=1.0 / source if source > 1 else None,
      sample_rate_synthetic=1.0 / synthetic if synthetic > 1 else None)
  return plan, {
      "source": rows["source"][::source],
      "synthetic": rows["synthetic"][::synthetic]
  }


def _copied_notes(n: int = 1200) -> tuple[Any, dict[str, list]]:
  """Invented free text: every other synthetic note is a verbatim source
  note (the final review's probe; half the column copied)."""
  words = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
           "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
           "oscar", "papa", "quebec", "romeo", "sierra", "tango")
  fields = ({
      "name": "id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "note",
      "type": "STRING",
      "mode": "NULLABLE"
  })

  def notes(seed: int, base: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    return [{
        "id": base + i,
        "note": " ".join(words[int(w)] for w in rng.integers(0, 20, size=7))
    } for i in range(n)]

  source, synthetic = notes(1, 1), notes(2, 10**6)
  rng = np.random.default_rng(9)
  for i in range(0, n, 2):
    synthetic[i]["note"] = source[int(rng.integers(0, n))]["note"]
  table = planned_table(
      "people", fields, source, synthetic, pk=("id",), pair_max_columns=0)
  assert table.columns[1].kind.value == "text"
  return table, {"source": source, "synthetic": synthetic}


def test_a_collapsed_value_pool_fails_the_distinct_ratio():
  """Ruling R111: a free-text column the generator filled from a pool of
  40 values, against 3,000 distinct source values, has a distinct ratio
  of 40 / 3,000 = 0.013 at matched n — FAIL (the diversity collapse the
  metric exists for), with the entropy ratio falling with it."""
  n, pool_size = 3000, 40
  fields = ({
      "name": "id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "motto",
      "type": "STRING",
      "mode": "NULLABLE"
  })
  words = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
           "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
           "oscar", "papa", "quebec", "romeo", "sierra", "tango")

  def motto(rng: np.random.Generator) -> str:
    return " ".join(words[int(w)] for w in rng.integers(0, 20, size=7))

  rng = np.random.default_rng(1)
  source = [{"id": i, "motto": motto(rng)} for i in range(n)]
  pool = [motto(np.random.default_rng(100 + k)) for k in range(pool_size)]
  synthetic = [{
      "id": 10**6 + i,
      "motto": pool[i % pool_size]
  } for i in range(n)]
  distinct_src = len({r["motto"] for r in source})
  assert distinct_src == n and len(set(pool)) == pool_size
  table = planned_table(
      "people", fields, source, synthetic, pk=("id",), pair_max_columns=0)
  assert table.columns[1].kind.value == "text"
  by_key = _by_key(
      _pure(table, {
          "source": source,
          "synthetic": synthetic
      }).metrics)
  ratio = by_key[("column.distinct_ratio", "motto")]
  assert ratio.value == pytest.approx(pool_size / n) == pytest.approx(
      0.0133, abs=1e-4)
  assert (ratio.source_value, ratio.synthetic_value) == (n, pool_size)
  assert (ratio.n_source, ratio.n_synthetic) == (n, n)  # matched n: all rows
  assert ratio.detail["distinct_syn"] == pool_size
  assert status_for(_CATALOGUE.get(ratio.metric_id), ratio) is Status.FAIL
  entropy = by_key[("column.entropy_ratio", "motto")]
  assert entropy.value == pytest.approx(
      math.log2(pool_size) / math.log2(n), abs=0.01)
  assert status_for(_CATALOGUE.get(entropy.metric_id), entropy) is Status.FAIL


def test_a_row_sample_never_answers_the_diversity_ratios():
  """The re-review's probe (R116 item 1): a 6,000-row text column filled
  from a 6,000-value pool scores 0.633 FAIL in an exact run; read as a
  1/10 row sample of either side it scored 0.945 / 0.978 / 0.926 PASS
  (thinned repeats pull the ratio toward 1). Defined at m = min(n_src,
  n_syn), both ratios need both sides in full: not_evaluated, with the
  sample's own ratio and counts under names that are not the table's."""
  words = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
           "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
           "oscar", "papa", "quebec", "romeo", "sierra", "tango")

  def motto(rng: Any) -> str:
    return " ".join(words[rng.randrange(20)] for _ in range(8))

  n = 6000
  rng = random.Random(1)
  source = [{"id": i, "motto": motto(rng)} for i in range(n)]
  pool = [motto(random.Random(10_000 + k)) for k in range(n)]
  rng = random.Random(2)
  synthetic = [{"id": 10**6 + i, "motto": rng.choice(pool)} for i in range(n)]
  fields = ({
      "name": "id",
      "type": "INT64",
      "mode": "REQUIRED"
  }, {
      "name": "motto",
      "type": "STRING",
      "mode": "NULLABLE"
  })
  table = planned_table(
      "people", fields, source, synthetic, pk=("id",), pair_max_columns=0)
  rows = {"source": source, "synthetic": synthetic}
  exact = _by_key(_pure(table, rows).metrics)
  for metric_id in ("column.distinct_ratio", "column.entropy_ratio"):
    mv = exact[(metric_id, "motto")]
    assert mv.method.value == "exact" and mv.value is not None
  assert exact[("column.distinct_ratio", "motto")].value == pytest.approx(
      0.633, abs=0.01)
  assert status_for(
      _CATALOGUE.get("column.distinct_ratio"),
      exact[("column.distinct_ratio", "motto")]) is Status.FAIL
  for kinds, tenth in (("both", (10, 10)), ("source", (10, 1)), ("synthetic",
                                                                 (1, 10))):
    plan, read = _row_sampled(table, rows, source=tenth[0], synthetic=tenth[1])
    got = _by_key(_pure(plan, read).metrics)
    for metric_id in ("column.distinct_ratio", "column.entropy_ratio"):
      mv = got[(metric_id, "motto")]
      reason = mv.detail["reason"]
      assert mv.value is None, (kinds, metric_id)
      assert status_for(_CATALOGUE.get(metric_id),
                        mv) is Status.NOT_EVALUATED, (kinds, metric_id)
      assert reason.startswith("sampled mode cannot measure"), reason
      assert reason.endswith("run exact mode"), reason
      assert "row sample" in reason
      assert "sample_ratio" in mv.detail, (kinds, metric_id)
    seen = got[("column.distinct_ratio", "motto")].detail
    assert 0 < seen["distinct_src_in_sample"] <= 600 or kinds == "synthetic"
    assert 0 < seen["distinct_syn_in_sample"] <= 600 or kinds == "source"
    assert "distinct_src" not in seen and "distinct_syn" not in seen
  assert set(census.NEEDS_FULL_SIDE) >= {
      "column.distinct_ratio", "column.entropy_ratio"
  }


def test_a_copied_free_text_column_fails_the_copy_rate_when_read_in_full():
  """Ruling R111: half of a free-text column copied verbatim from rare
  source values is a copy rate of 0.5, FAIL, in an exact run."""
  table, rows = _copied_notes()
  copy = _by_key(_pure(table,
                       rows).metrics)[("field.substantive_copy_rate", "note")]
  assert copy.value == 0.5 and copy.method.value == "exact"
  assert copy.detail["copies"] == 600 and copy.detail["substantive"] == 1200
  assert status_for(_CATALOGUE.get(copy.metric_id), copy) is Status.FAIL


def test_a_sampled_source_never_stores_the_copy_rate_as_exact():
  """The final review's probe (C1): with the source read as a 10 % row
  sample, nine copies in ten have no sampled twin — the rate read 0.046
  `exact` where it is 0.5, and novelty turned from FAIL to PASS. Both need
  every source row, so they are not evaluated (R72)."""
  table, rows = _copied_notes()
  exact = _by_key(_pure(table, rows).metrics)
  novelty = _CATALOGUE.get("column.novelty_mass")
  assert exact[(novelty.id, "note")].value == 0.5
  assert status_for(novelty, exact[(novelty.id, "note")]) is Status.FAIL
  plan, read = _row_sampled(table, rows, source=10)
  got = _by_key(_pure(plan, read).metrics)
  for metric_id in ("field.substantive_copy_rate", "column.novelty_mass"):
    mv = got[(metric_id, "note")]
    reason = mv.detail["reason"]
    assert mv.value is None and mv.ci_low is None, metric_id
    assert reason.startswith("sampled mode cannot measure"), reason
    assert "the source side is a 0.1 row sample" in reason, reason
    assert reason.endswith("run exact mode"), reason
    assert mv.detail["sample_rates"] == {"source": 0.1}, metric_id
    assert status_for(_CATALOGUE.get(metric_id),
                      mv) is Status.NOT_EVALUATED, metric_id
  # what the sample did see, never as a rate: fewer than the 600 copies
  seen = got[("field.substantive_copy_rate", "note")].detail
  assert 0 < seen["copies_in_sample"] < 200 and seen["substantive"] == 1200
  assert got[("column.novelty_mass",
              "note")].detail["matched_rows_lower_bound"] < 200


def test_sampled_source_withholds_what_needs_every_source_row():
  """R72 on the census: the metrics whose definition is the source's value
  (or shape) SET are not evaluated on a source row sample; every other
  row is a sample estimate over the rows read, marked as one."""
  table, rows = users_table()
  plan, read = _row_sampled(table, rows, source=4)
  got = [  # the key column has no census: its rows say so, whatever the mode
      mv for mv in _pure(plan, read).metrics if mv.column != "user_id"
  ]
  withheld = [mv for mv in got if mv.metric_id in _NEED_THE_SOURCE]
  assert {mv.metric_id for mv in withheld} == set(_NEED_THE_SOURCE)
  for mv in withheld:
    reason = mv.detail["reason"]
    assert mv.value is None, (mv.metric_id, mv.column)
    assert reason.startswith("sampled mode cannot measure"), reason
    assert reason.endswith("run exact mode"), reason
    assert mv.detail["sample_rates"] == {"source": 0.25}
  by_key = _by_key(got)
  status = by_key[("field.category_adherence", "status")]
  assert status.detail["matched_rows_lower_bound"] == sum(
      r["status"] is not None for r in read["synthetic"])
  assert by_key[("field.shape_adherence",
                 "sku")].detail["adherent_lower_bound"] > 0
  # the rest: evaluated on the rows read, never as exact
  evaluated = [
      mv for mv in got
      if mv.metric_id not in _NEED_THE_SOURCE and mv.value is not None
  ]
  assert {mv.metric_id for mv in evaluated} >= {
      "column.tvd", "column.jsd", "column.cohens_w", "column.top1_share_delta",
      "column.coverage_mass", "column.shape_head_tv"
  }
  for mv in got:
    if mv.metric_id == "column.distinct_ceiling_hit":  # reads the synthetic
      assert mv.method.value == "exact" and mv.sample_rate is None
      continue
    assert mv.method.value == "sample", (mv.metric_id, mv.column)
    assert mv.sample_rate == 0.25, (mv.metric_id, mv.column)
    assert mv.detail["sample_rates"] == {"source": 0.25}
  # the noise reference is over the rows read, not the table's
  tvd = by_key[("column.tvd", "status")]
  n_read = sum(r["status"] is not None for r in read["source"])
  assert tvd.n_source == n_read < 1100
  full = _by_key(_pure(table, rows).metrics)[("column.tvd", "status")]
  assert tvd.noise_floor > full.noise_floor


def test_sampled_synthetic_withholds_coverage_and_the_pool_cap():
  """R72, the other side: coverage needs the synthetic's value set and the
  pool-cap hit its exact distinct count; the shares of the synthetic rows
  read (adherence, novelty, the copy rate) are sample estimates."""
  table, rows = users_table()
  plan, read = _row_sampled(table, rows, synthetic=5)
  got = [mv for mv in _pure(plan, read).metrics if mv.column != "user_id"]
  withheld = [mv for mv in got if mv.metric_id in _NEED_THE_SYNTHETIC]
  assert {mv.metric_id for mv in withheld} == set(_NEED_THE_SYNTHETIC)
  for mv in withheld:
    reason = mv.detail["reason"]
    assert mv.value is None, (mv.metric_id, mv.column)
    assert reason.startswith("sampled mode cannot measure"), reason
    assert "the synthetic side is a 0.2 row sample" in reason, reason
    assert reason.endswith("run exact mode"), reason
  by_key = _by_key(got)
  assert by_key[("column.coverage_mass",
                 "status")].detail["covered_rows_lower_bound"] > 0
  assert by_key[("column.distinct_ceiling_hit",
                 "bio")].detail["distinct_syn_lower_bound"] == len(
                     read["synthetic"])
  for metric_id, column in (("field.category_adherence",
                             "status"), ("column.novelty_mass", "city"),
                            ("field.substantive_copy_rate",
                             "bio"), ("field.shape_adherence", "sku")):
    mv = by_key[(metric_id, column)]
    assert mv.value is not None, metric_id
    assert mv.method.value == "sample" and mv.sample_rate == 0.2, metric_id
    assert mv.detail["sample_rates"] == {"synthetic": 0.2}, metric_id
  adherence = by_key[("field.category_adherence", "status")]
  assert adherence.n_synthetic == sum(
      r["status"] is not None for r in read["synthetic"])
  pool = [mv for mv in got if mv.metric_id == "field.pool_memorization_lift"]
  assert pool and all(mv.method.value == "exact" for mv in pool)


def test_both_sides_sampled_take_the_lower_rate_and_name_both():
  table, rows = users_table()
  plan, read = _row_sampled(table, rows, source=4, synthetic=2)
  by_key = _by_key(_pure(plan, read).metrics)
  tvd = by_key[("column.tvd", "status")]
  assert tvd.method.value == "sample" and tvd.sample_rate == 0.25
  assert tvd.detail["sample_rates"] == {"source": 0.25, "synthetic": 0.5}
  # each withheld metric names the side it needs
  assert "the source side" in by_key[("field.category_adherence",
                                      "status")].detail["reason"]
  assert "the synthetic side" in by_key[("column.coverage_mass",
                                         "status")].detail["reason"]


def test_a_row_sample_outranks_a_value_sample_and_keeps_its_rate():
  """A value-sampled column of a row-sampled side: the row is a row
  sample first (R72); the value-sample rate moves to `detail`."""
  table, rows = users_table()
  plan, read = _row_sampled(
      with_census(table, sku=("value_sampled", 0.25)), rows, synthetic=2)
  by_key = _by_key(_pure(plan, read).metrics)
  distinct = by_key[("column.distinct_ratio", "sku")]
  assert distinct.method.value == "sample" and distinct.sample_rate == 0.5
  assert distinct.detail["value_sample_rate"] == 0.25
  assert distinct.detail["estimator"] == "value_sampled"


def test_profiles_of_a_sampled_side_say_so():
  table, rows = users_table()
  plan, read = _row_sampled(table, rows, source=4)
  profiles = _pure(plan, read).profiles
  assert {p.profile_kind for p in profiles} == {"topk", "shape_mix"}
  for p in profiles:
    if p.side == "source":
      assert p.payload["sample_rate"] == 0.25, (p.profile_kind, p.column)
    else:
      assert "sample_rate" not in p.payload, (p.profile_kind, p.column)
  exact = _pure(table, rows).profiles
  assert all("sample_rate" not in p.payload for p in exact)


def test_exact_mode_rows_are_unchanged_by_the_sampled_mode_rule():
  table, rows = users_table()
  for mv in _pure(table, rows).metrics:
    assert mv.method.value == "exact" and mv.sample_rate is None
    assert "sample_rates" not in mv.detail


def test_baseline_diversity_sums_as_python_3_11_did(monkeypatch):
  """The reference side's `sum(c * ln(c))` behind the entropy and distinct
  baselines. CPython 3.12 made the builtin `sum()` of floats compensated,
  which moved its last bit; the counts are added left to right, as 3.11
  added them. `summarize` rounds what it returns, so the accumulator it is
  handed is what is pinned."""
  handed = []
  monkeypatch.setattr(census, "summarize", handed.append)

  class _Of:

    def __init__(self, **fields):
      self.__dict__.update(fields)

  r_counts = {"v0": 9, "v1": 10, "v2": 6, "v3": 2}
  view = _Of(
      ref=_Of(r_nonnull=27, r_counts=r_counts),
      col=_Of(sampled=False),
      c=_Of(n_src_r=27.0, clc_src_r=67.5, k_src_r=4.0))
  census._baseline_diversity(view)  # pylint: disable=protected-access
  assert len(handed) == 1
  accumulator = handed[0]
  assert accumulator.n_syn_m == 27 and accumulator.k_syn_m == 4
  assert accumulator.clc_syn_m.hex() == "0x1.b780751325eb3p+5"
