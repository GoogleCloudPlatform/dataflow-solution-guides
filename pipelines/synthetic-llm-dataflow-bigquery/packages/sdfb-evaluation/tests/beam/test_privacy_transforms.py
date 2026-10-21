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
"""Tests for `sdfb_evaluation.beam.privacy` (Task 24): the sampled Gower
nearest-neighbour privacy metrics (holdout DCR share, DCR/NNDR p5 ratios,
density, coverage), the detection metrics (C2ST AUC with its DeLong CI,
pMSE ratio) and their bounded, keys-only row flags.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import bisect
import bz2
import dataclasses
import itertools
import json
import math
import pickle
import re
import time as clock
import tracemalloc
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.internal import pickler
from apache_beam.portability import common_urns
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.utils.shared import Shared

from sdfb_evaluation.beam import census, dense, membership, privacy
from sdfb_evaluation.beam.encode import EncodeSide, key_hash
from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.beam.label_key import LabelKey
from sdfb_evaluation.beam.membership import RowFlag, flag_row
from sdfb_evaluation.beam.privacy import (
    DETECTABLE,
    DETECTION_METRIC_IDS,
    HOLDOUT_NNDR_P5_ZERO,
    HOLDOUT_P5_ZERO,
    MIN_PANEL_ROWS,
    NEAREST_RECORD,
    OWNED_METRIC_IDS,
    PRIVACY_METRIC_IDS,
    UNVERIFIED_REASON,
    PanelInputs,
    Privacy,
    PrivacyResult,
    PrivacySpec,
    SampleCombineFn,
    detection_columns,
    privacy_outputs,
    sample_parts,
)
from sdfb_evaluation.canonical import hash64, hashed_label
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.scoring import status_for, to_metric_row, to_profile_row
from sdfb_evaluation.stats.privacy import (
    density_coverage,
    effective_chunk,
    gower_knn,
    nn_privacy,
    summarize_nn,
)
from sdfb_evaluation.types import (
    ColumnKind,
    Method,
    MetricValue,
    ProfileValue,
    Status,
)

from .membership_data import (
    PEOPLE_FIELDS,
    PEOPLE_IDENTITY,
    SOURCE_ID_BASE,
    SYNTHETIC_ID_BASE,
    copy_content,
    panel_of,
    people_rows,
    planned,
)
from .privacy_data import (
    DETECTION_ROWS,
    LABEL_KEY,
    PANEL_ROWS,
    SALT,
    SAMPLE_ROWS,
    encode,
    encode_all,
    keyless,
    people,
    with_rates,
)

_CATALOGUE = load_catalogue()
_EVALUATED_AT = "2026-09-30T00:00:00+00:00"
_TOP_K = 25
_COLUMNS = tuple(f["name"] for f in PEOPLE_FIELDS)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _pure(table: Any, rows_by: Mapping[str, Sequence[Mapping[str, Any]]],
          **kwargs: Any) -> PrivacyResult:
  """The in-process path over the batches Beam would see."""
  kwargs.setdefault("label_key", LABEL_KEY)
  kwargs.setdefault("privacy_sample_rows", SAMPLE_ROWS)
  kwargs.setdefault("detection_sample_rows", DETECTION_ROWS)
  kwargs.setdefault("row_flags_top_k", _TOP_K)
  chunk = kwargs.pop("chunk", 997)
  return privacy_outputs(
      table, encode_all(table, rows_by, chunk=chunk), salt=SALT, **kwargs)


def _by_id(metrics: Sequence[MetricValue]) -> dict[str, MetricValue]:
  out: dict[str, MetricValue] = {}
  for mv in metrics:
    assert mv.metric_id not in out, f"duplicate row {mv.metric_id}"
    out[mv.metric_id] = mv
  return out


def _status(mv: MetricValue) -> Status:
  return status_for(_CATALOGUE.get(mv.metric_id), mv)


def _collect(pcoll: beam.PCollection, path: Path, label: str) -> None:

  def dump(actual: Sequence[Any]) -> None:
    path.write_bytes(pickle.dumps(list(actual)))

  assert_that(pcoll, dump, label=label)


def _key_file_reader(uri: str) -> bytes:
  """A fake operator-key reader: the key lives in a local file, so only
  its path is in the graph."""
  return Path(uri).read_bytes()


def _run(tables: Sequence[Any], rows_by: Mapping[str, Mapping[str, list]],
         tmp_path: Path, **kwargs: Any) -> tuple[list, list, list, Any]:
  """The Beam path on the local runner (FnApiRunner): read, encode, privacy. The label
  key is an operator key read on a worker from a local file."""
  kwargs.setdefault("privacy_sample_rows", SAMPLE_ROWS)
  kwargs.setdefault("detection_sample_rows", DETECTION_ROWS)
  kwargs.setdefault("row_flags_top_k", _TOP_K)
  key_file = tmp_path / "label.key"
  key_file.write_bytes(LABEL_KEY)
  by_name = {table.name: table for table in tables}
  sources = InMemorySources({
      (name, side): rows for name, by_side in rows_by.items()
      for side, rows in by_side.items()
  })
  pipeline = BeamTestPipeline()
  with pipeline as p:
    batches = [
        sources.read(p, by_name[name], side)
        | EncodeSide(by_name[name], side, salt=SALT)
        for name, by_side in rows_by.items()
        for side in by_side
    ] | "Flatten" >> beam.Flatten()
    key = p | "LabelKey" >> LabelKey(str(key_file), reader=_key_file_reader)
    out = batches | "Privacy" >> Privacy(
        list(tables), salt=SALT, label_key=key, **kwargs)
    _collect(out["metrics"], tmp_path / "metrics.pkl", "Metrics")
    _collect(out["profiles"], tmp_path / "profiles.pkl", "Profiles")
    _collect(out["flags"], tmp_path / "flags.pkl", "Flags")
  loaded = [
      pickle.loads((tmp_path / f"{name}.pkl").read_bytes())
      for name in ("metrics", "profiles", "flags")
  ]
  return loaded[0], loaded[1], loaded[2], pipeline


def _flag_json(flags: Sequence[RowFlag]) -> str:
  return json.dumps([
      flag_row(f, evaluation_id="e1", evaluated_at=_EVALUATED_AT) for f in flags
  ],
                    default=str)


def _values_absent(text: str, rows_by: Mapping[str, Sequence[Mapping]]) -> None:
  """No attribute value of any row (4 characters or more: shorter numbers
  collide with ranks) appears in `text` as a token of its own — not
  merely as a digit run inside a distance's float digits."""
  for rows in rows_by.values():
    for row in rows:
      for column in _COLUMNS:
        value = row[column]
        if value is None:
          continue
        literal = str(value)
        if len(literal) >= 4 and literal in text:
          token = rf"(?<![0-9.]){re.escape(literal)}(?![0-9])"
          assert re.search(token, text) is None, (column, literal)


def _sorted_profiles(profiles: Sequence[ProfileValue]) -> list[ProfileValue]:
  return sorted(profiles, key=lambda pv: (pv.profile_kind, pv.side))


def _sorted_flags(flags: Sequence[RowFlag]) -> list[RowFlag]:
  return sorted(flags, key=lambda f: (f.table, f.check, f.rank))


# --------------------------------------------------------------------------
# shared runs
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", name="clean_run")
def fixture_clean_run() -> tuple[Any, dict, PrivacyResult]:
  table, rows = people()
  return table, rows, _pure(table, rows)


@pytest.fixture(scope="module", name="copies_run")
def fixture_copies_run() -> tuple[Any, dict, PrivacyResult]:
  table, rows = people(copies=1000)
  return table, rows, _pure(table, rows)


@pytest.fixture(scope="module", name="shifted_run")
def fixture_shifted_run() -> tuple[Any, dict, PrivacyResult]:
  table, rows = people(shift=6000.0)
  return table, rows, _pure(table, rows)


@pytest.fixture(scope="module", name="beam_run")
def fixture_beam_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
  """One local-runner (FnApiRunner) pass over the copies table and a clean twin, with
  the index builds and nearest-neighbour batches counted."""
  copies, copies_rows = people(copies=800, n_synthetic=2200)
  clean, clean_rows = people(n_synthetic=2200, seed=41)
  clean = dataclasses.replace(
      clean, name="people_b", landing_table=f"{clean.landing_table}_b")
  builds: Counter = Counter()
  parts: Counter = Counter()
  real_build, real_part = privacy.build_nn_index, privacy.gower_part

  def counting_build(spec: Any, inputs: Any) -> Any:
    builds[spec.table] += 1
    return real_build(spec, inputs)

  def counting_part(spec: Any, inputs: Any, index: Any, records: Any) -> Any:
    parts[spec.table] += 1
    return real_part(spec, inputs, index, records)

  with pytest.MonkeyPatch.context() as patch:
    patch.setattr(privacy, "build_nn_index", counting_build)
    patch.setattr(privacy, "gower_part", counting_part)
    metrics, profiles, flags, pipeline = _run(
        [copies, clean], {
            "people": copies_rows,
            "people_b": clean_rows
        },
        tmp_path_factory.mktemp("privacy"),
        privacy_sample_rows=2200,
        detection_sample_rows=1000)
  return {
      "tables": {
          "people": (copies, copies_rows),
          "people_b": (clean, clean_rows)
      },
      "metrics": metrics,
      "profiles": profiles,
      "flags": flags,
      "pipeline": pipeline,
      "builds": dict(builds),
      "parts": dict(parts),
  }


# --------------------------------------------------------------------------
# the brief's tests
# --------------------------------------------------------------------------
def test_copies_trip_dcr_share(copies_run):
  _, rows, result = copies_run
  by = _by_id(result.metrics)
  share = by["row.dcr_train_holdout_share"]
  assert share.value is not None and share.ci_low is not None
  assert share.value > 0.65 and share.ci_low >= 0.60
  assert _status(share) is Status.FAIL
  dcr = by["row.dcr_p5_ratio"]
  assert dcr.value == 0.0 and dcr.synthetic_value == 0.0
  assert _status(dcr) is Status.FAIL
  # the nearest-record flags name the copies: distance 0, the donor's key
  nearest = [f for f in result.flags if f.check == NEAREST_RECORD]
  assert len(nearest) == _TOP_K
  assert [f.rank for f in nearest] == list(range(1, _TOP_K + 1))
  donors = {
      hashed_label(key_hash([r["person_id"]]), key=LABEL_KEY)
      for r in rows["source"][:PANEL_ROWS]
  }
  synthetic_ids = {r["person_id"] for r in rows["synthetic"][:1000]}
  for flag in nearest:
    assert flag.distance == 0.0 and flag.score == 1.0
    assert flag.source_set == "R" and flag.source_key_hash in donors
    assert flag.synthetic_key is not None
    assert flag.synthetic_key["person_id"] in synthetic_ids


def test_same_distribution_passes_share(clean_run):
  _, _, result = clean_run
  by = _by_id(result.metrics)
  assert set(by) == set(OWNED_METRIC_IDS)
  share = by["row.dcr_train_holdout_share"]
  assert share.value is not None and 0.44 <= share.value <= 0.56
  assert share.ci_low is not None and share.ci_high is not None
  assert share.ci_low <= 0.5 <= share.ci_high
  for metric_id in PRIVACY_METRIC_IDS:
    mv = by[metric_id]
    assert mv.value is not None, (metric_id, mv.detail)
    assert _status(mv) is Status.PASS, (metric_id, mv.value)
  density, coverage = by["row.density"], by["row.coverage"]
  assert density.value is not None and 0.8 <= density.value <= 1.2
  assert coverage.value is not None and coverage.value >= 0.9
  for mv in (density, coverage):
    assert mv.baseline_value is not None and mv.baseline_value > 0
    assert mv.n_source == mv.n_synthetic == PANEL_ROWS
  auc = by["table.detection_auc"]
  assert auc.value is not None and auc.value < 0.6
  assert _status(auc) is Status.PASS
  # nothing detectable: no detectable flags
  assert not [f for f in result.flags if f.check == DETECTABLE]


def test_detection_separates_shifted_table(shifted_run):
  _, rows, result = shifted_run
  by = _by_id(result.metrics)
  auc = by["table.detection_auc"]
  assert auc.value is not None and auc.value > 0.85
  assert auc.ci_low is not None and auc.ci_high is not None
  assert auc.ci_low <= auc.value <= auc.ci_high and auc.ci_low > 0.8
  assert auc.baseline_value is not None and auc.baseline_value < 0.6
  assert auc.method is Method.SAMPLE
  assert auc.n_source == auc.n_synthetic == DETECTION_ROWS
  assert _status(auc) is Status.FAIL
  pmse = by["table.pmse_ratio"]
  assert pmse.value is not None and pmse.value > 3
  assert pmse.baseline_value is not None and pmse.baseline_value < 3
  assert math.isfinite(pmse.detail["ceiling"]) and pmse.detail["ceiling"] > 10
  assert pmse.detail["k"] >= 2 and pmse.detail["pmse"] > 0
  assert _status(pmse) in (Status.WARN, Status.FAIL)
  detectable = [f for f in result.flags if f.check == DETECTABLE]
  assert 0 < len(detectable) <= _TOP_K
  assert [f.rank for f in detectable] == list(range(1, len(detectable) + 1))
  ids = {r["person_id"] for r in rows["synthetic"]}
  scores = [f.score for f in detectable]
  assert scores == sorted(scores, reverse=True)
  for flag in detectable:
    assert flag.synthetic_key is not None
    assert flag.synthetic_key["person_id"] in ids
    assert flag.source_key_hash is None and flag.distance is None
    assert flag.score is not None and 0.5 < flag.score <= 1.0
  assert len({json.dumps(f.synthetic_key) for f in detectable
             }) == len(detectable)
  roc = [pv for pv in result.profiles if pv.profile_kind == "roc_curve"]
  assert len(roc) == 1 and roc[0].side == "both"
  payload = roc[0].payload
  points = payload["points"]
  assert points[0] == (0.0, 0.0) and points[-1] == (1.0, 1.0)
  assert all(
      a[0] <= b[0] and a[1] <= b[1] for a, b in itertools.pairwise(points))
  assert len(points) <= 101
  assert payload["auc"] == auc.value
  assert (payload["ci_low"], payload["ci_high"]) == (auc.ci_low, auc.ci_high)
  assert payload["n_source"] == payload["n_synthetic"] == DETECTION_ROWS
  assert roc[0].n == 2 * DETECTION_ROWS


@pytest.mark.parametrize("case", ["small", "unverified", "unequal", "none"])
def test_small_or_unverified_not_evaluated(case):
  if case == "small":
    table, rows = people(n_source=1500, n_synthetic=300, n_reference=600)
    why = f"{MIN_PANEL_ROWS:,}"
  elif case == "unverified":
    table, rows = people(n_synthetic=300, verified=False)
    why = UNVERIFIED_REASON
  elif case == "unequal":
    table, rows = people(n_source=4500, n_synthetic=300)
    assert table.panel is not None
    table = dataclasses.replace(
        table,
        panel=dataclasses.replace(
            table.panel, h_rows=table.panel.h_rows[:PANEL_ROWS - 1]))
    why = "|H|"
  else:
    table, rows = people(n_synthetic=300)
    table = dataclasses.replace(table, panel=None)
    why = "panel"
  result = _pure(table, rows)
  by = _by_id(result.metrics)
  assert set(by) == set(OWNED_METRIC_IDS)
  for metric_id in PRIVACY_METRIC_IDS:
    mv = by[metric_id]
    assert mv.value is None and why in mv.detail["reason"], (metric_id,
                                                             mv.detail)
    assert _status(mv) is Status.NOT_EVALUATED
  if case == "unverified":
    assert "digest" in by["row.dcr_p5_ratio"].detail["reason"]
  # detection needs no panel: it still runs on the source and synthetic
  for metric_id in DETECTION_METRIC_IDS:
    assert by[metric_id].value is not None, by[metric_id].detail
  assert not [f for f in result.flags if f.check == NEAREST_RECORD]


def test_shared_index_built_once_per_worker(beam_run):
  """The R/H index (Gower encodings and source codes) is built once per
  table on the local runner's (FnApiRunner) single worker, however many
  nearest-neighbour batches and the emitter use it."""
  assert beam_run["parts"]["people"] >= 2, beam_run["parts"]
  assert beam_run["parts"]["people_b"] >= 2, beam_run["parts"]
  assert beam_run["builds"] == {"people": 1, "people_b": 1}


# --------------------------------------------------------------------------
# the Beam path
# --------------------------------------------------------------------------
def test_beam_equals_the_in_process_path(beam_run):
  for name, (table, rows) in beam_run["tables"].items():
    pure = _pure(
        table, rows, privacy_sample_rows=2200, detection_sample_rows=1000)
    metrics = [mv for mv in beam_run["metrics"] if mv.table == name]
    assert sorted(
        metrics, key=lambda m: m.metric_id) == sorted(
            pure.metrics, key=lambda m: m.metric_id), name
    profiles = [pv for pv in beam_run["profiles"] if pv.table == name]
    assert _sorted_profiles(profiles) == _sorted_profiles(pure.profiles), name
    flags = [f for f in beam_run["flags"] if f.table == name]
    assert _sorted_flags(flags) == _sorted_flags(pure.flags), name
  copies = _by_id([mv for mv in beam_run["metrics"] if mv.table == "people"])
  assert _status(copies["row.dcr_train_holdout_share"]) is Status.FAIL


def test_label_key_never_enters_the_job_graph(beam_run):
  flags = beam_run["flags"]
  labelled = [f for f in flags if f.source_key_hash is not None]
  assert labelled and all(f.source_key_hash.startswith("h:") for f in labelled)
  for blob in _pickled_payloads(beam_run["pipeline"]):
    assert LABEL_KEY not in blob
    assert LABEL_KEY.hex().encode() not in blob
  table, _ = beam_run["tables"]["people"]
  with pytest.raises(TypeError, match="PCollection"):
    Privacy([table], salt=SALT, label_key=LABEL_KEY)  # type: ignore[arg-type]


def _pickled_payloads(pipeline: beam.Pipeline) -> list[bytes]:
  """Every ParDo payload of the job graph, unpickled to raw bytes, plus the
  serialised graph itself."""
  proto = pipeline.to_runner_api()
  out = []
  for transform in proto.components.transforms.values():
    if transform.spec.urn != common_urns.primitives.PAR_DO.urn:
      continue
    payload = beam_runner_api_pb2.ParDoPayload.FromString(
        transform.spec.payload)
    blob = payload.do_fn.payload
    if not blob:
      continue
    pickler.loads(blob)
    raw = base64.b64decode(blob)
    for decompress in (bz2.decompress, zlib.decompress):
      try:
        raw = decompress(raw)
        break
      except (OSError, zlib.error):
        continue
    out.append(raw)
  out.append(proto.SerializeToString())
  return out


def test_catalogue_coverage_every_owned_id_emitted_or_explained(beam_run):
  ids = set(_CATALOGUE.ids())
  assert set(OWNED_METRIC_IDS) <= ids
  assert set(PRIVACY_METRIC_IDS) | set(DETECTION_METRIC_IDS) == set(
      OWNED_METRIC_IDS)
  for name, (table, _) in beam_run["tables"].items():
    metrics = [mv for mv in beam_run["metrics"] if mv.table == name]
    assert Counter(mv.metric_id for mv in metrics) == Counter(OWNED_METRIC_IDS)
    for mv in metrics:
      metric = _CATALOGUE.get(mv.metric_id)
      assert metric.level in ("row", "table")
      assert mv.column is None and mv.edge is None
      assert mv.method is Method.SAMPLE
      assert mv.encoding_plan_digest == table.encoding_plan_digest
      assert mv.feature_set_digest
      if mv.value is None:
        assert mv.detail.get("reason"), mv
      elif metric.noise_floor in ("wilson", "delong"):
        assert mv.ci_low is not None and mv.ci_high is not None, mv  # R41
      if metric.baseline and mv.value is not None:
        assert mv.baseline_value is not None, mv.metric_id
      json.dumps(
          to_metric_row(
              mv,
              evaluation_id="e1",
              evaluated_at=_EVALUATED_AT,
              landing_table=table.landing_table,
              source_table=table.source_table),
          allow_nan=False)
  # with the other passes, every row-level id now has an owner
  owned = (
      set(OWNED_METRIC_IDS) | set(membership.OWNED_METRIC_IDS)
      | set(dense.OWNED_METRIC_IDS) | set(census.OWNED_METRIC_IDS))
  row_ids = {m.id for m in _CATALOGUE.by_level("row")}
  assert row_ids <= owned
  assert {"table.detection_auc", "table.pmse_ratio"} <= owned


def test_a_failing_table_does_not_fail_the_run(tmp_path, monkeypatch):
  """Three tables: one whose nearest-neighbour batches fail on a worker
  (privacy not_evaluated with the reason, detection still evaluated, no
  nearest-record flags), one with a corrupt plan grid (the Gower space
  fails on the driver, detection on its worker: every owned id
  not_evaluated) and a good one that matches the in-process path;
  nothing raises."""
  good, good_rows = people(n_synthetic=400)
  bad, bad_rows = people(n_synthetic=300, seed=43)
  bad = dataclasses.replace(
      bad, name="people_w", landing_table=f"{bad.landing_table}_w")
  broken, broken_rows = people(n_synthetic=300, seed=44)
  columns = tuple(
      dataclasses.replace(c, quantiles_src=tuple(reversed(c.quantiles_src))
                         ) if c.name == "balance" and c.quantiles_src else c
      for c in broken.columns)
  broken = dataclasses.replace(
      broken,
      name="people_d",
      landing_table=f"{broken.landing_table}_d",
      columns=columns)
  real_part = privacy.gower_part

  def failing(spec: Any, inputs: Any, index: Any, records: Any) -> Any:
    if spec.table == "people_w":
      raise ValueError("a malformed batch (test)")
    return real_part(spec, inputs, index, records)

  monkeypatch.setattr(privacy, "gower_part", failing)
  metrics, _, flags, _ = _run([good, bad, broken], {
      "people": good_rows,
      "people_w": bad_rows,
      "people_d": broken_rows
  },
                              tmp_path,
                              privacy_sample_rows=400,
                              detection_sample_rows=400)
  worker = _by_id([mv for mv in metrics if mv.table == "people_w"])
  for metric_id in PRIVACY_METRIC_IDS:
    assert worker[metric_id].value is None
    assert "malformed" in worker[metric_id].detail["reason"]
  assert worker["table.detection_auc"].value is not None
  assert not [
      f for f in flags if f.table == "people_w" and f.check == NEAREST_RECORD
  ]
  driver = _by_id([mv for mv in metrics if mv.table == "people_d"])
  assert set(driver) == set(OWNED_METRIC_IDS)
  assert all(mv.value is None and "non-decreasing" in mv.detail["reason"]
             for mv in driver.values()), driver
  assert not [f for f in flags if f.table == "people_d"]
  monkeypatch.setattr(privacy, "gower_part", real_part)
  pure = _pure(
      good, good_rows, privacy_sample_rows=400, detection_sample_rows=400)
  assert sorted([mv for mv in metrics if mv.table == "people"],
                key=lambda mv: mv.metric_id) == sorted(
                    pure.metrics, key=lambda mv: mv.metric_id)


def _out_of_memory(*_args: Any, **_kwargs: Any) -> None:
  raise MemoryError("Unable to allocate 3.2 GiB for an array")


def _malformed(*_args: Any, **_kwargs: Any) -> None:
  raise ValueError("a malformed batch (test)")


def test_a_worker_memory_error_fails_the_bundle(monkeypatch):
  """The final review's I3 (its probe): a MemoryError in the
  nearest-neighbour DoFn or the sampling DoFn is raised, so the bundle
  fails and is retried; it used to tag the privacy block `failed`, which
  left a run without its privacy verdicts SUCCEEDED. A data error in the
  same place still tags the block."""
  table, rows = people(n_synthetic=300)
  spec = PrivacySpec.from_table(
      table,
      salt=SALT,
      privacy_sample_rows=SAMPLE_ROWS,
      detection_sample_rows=SAMPLE_ROWS)
  inputs = PanelInputs.from_table(table, spec)
  batch = encode(table, "synthetic", rows["synthetic"])[0]
  nearest = privacy.GowerNNFn(spec, Shared())
  sampling = privacy._SamplePartsFn(spec)  # pylint: disable=protected-access  # the DoFn under test
  monkeypatch.setattr(privacy, "gower_part", _malformed)
  monkeypatch.setattr(privacy, "sample_parts", _malformed)
  for tagged in (*nearest.process([], inputs), *sampling.process(batch)):
    assert tagged.tag == "failed" and "malformed" in tagged.value[1]
  monkeypatch.setattr(privacy, "gower_part", _out_of_memory)
  monkeypatch.setattr(privacy, "sample_parts", _out_of_memory)
  with pytest.raises(MemoryError):
    list(nearest.process([], inputs))
  with pytest.raises(MemoryError):
    list(sampling.process(batch))


@pytest.mark.parametrize("step",
                         ["gower_part", "_privacy_rows", "_detection_rows"])
def test_in_process_memory_errors_are_raised_as_on_a_worker(monkeypatch, step):
  """The in-process path fails where the Beam path fails its bundle: the
  nearest-neighbour search, the privacy block's rows and the classifier
  all run on a worker."""
  table, rows = people(n_synthetic=300)
  monkeypatch.setattr(privacy, step, _out_of_memory)
  with pytest.raises(MemoryError):
    _pure(table, rows, privacy_sample_rows=300, detection_sample_rows=300)


# --------------------------------------------------------------------------
# the maths it consumes, and the handoffs
# --------------------------------------------------------------------------
def _space(table: Any, *, salt: str | None = None) -> Any:
  spec = PrivacySpec.from_table(
      table,
      salt=salt or SALT,
      privacy_sample_rows=SAMPLE_ROWS,
      detection_sample_rows=SAMPLE_ROWS)
  inputs = PanelInputs.from_table(table, spec)
  assert inputs.space is not None
  return inputs.space


@pytest.mark.parametrize("duplicates", [0, 700])
def test_whole_sample_equals_the_pure_maths(duplicates):
  """With every synthetic row in the sample (fewer rows than R, so density
  takes them all), the pass reproduces `stats.privacy.nn_privacy` on the
  raw rows exactly — a row held many times weighs its multiplicity
  (Rulings R34/R29), not once."""
  table, rows = people(n_synthetic=1500, copies=1, duplicates=duplicates)
  if duplicates:
    # one R record copied, then held 700 more times as whole rows
    assert Counter(
        r["person_id"]
        for r in rows["synthetic"]).most_common(1)[0][1] == duplicates + 1
  result = _pure(table, rows)
  assert table.panel is not None
  expected = summarize_nn(
      nn_privacy(
          _space(table),
          table.panel.r_rows,
          table.panel.h_rows,
          rows["synthetic"],
          k=privacy.DENSITY_K))
  by = _by_id(result.metrics)
  share = by["row.dcr_train_holdout_share"]
  assert share.value == pytest.approx(
      expected["dcr_train_holdout_share"], abs=1e-12)
  assert share.ci_low == pytest.approx(
      expected["dcr_train_holdout_share_ci_low"], abs=1e-12)
  assert share.ci_high == pytest.approx(
      expected["dcr_train_holdout_share_ci_high"], abs=1e-12)
  assert share.n_synthetic == 1500
  for metric_id, key in (("row.dcr_p5_ratio", "dcr_p5_ratio"),
                         ("row.nndr_p5_ratio", "nndr_p5_ratio"),
                         ("row.density", "density"), ("row.coverage",
                                                      "coverage")):
    assert by[metric_id].value == pytest.approx(
        expected[key], abs=1e-12), metric_id
  if duplicates:
    assert share.value > 0.7  # 701 copies of one R record
    assert by["row.dcr_train_holdout_share"].detail["sample_records"] == (
        1500 - duplicates)


def test_holdout_p5_zero_is_not_evaluated_never_infinite():
  """A holdout whose rows duplicate reference records (a lattice, or a
  source with repeated rows) has a p5 distance of 0: both p5 ratios are
  not_evaluated with the reason — never +inf."""
  table, rows = people(n_synthetic=400)
  assert table.panel is not None
  r_rows = table.panel.r_rows
  h_rows = [
      copy_content(r, h)
      for r, h in zip(r_rows, table.panel.h_rows, strict=True)
  ]
  table = dataclasses.replace(
      table, panel=dataclasses.replace(table.panel, h_rows=h_rows))
  by = _by_id(_pure(table, rows).metrics)
  dcr, nndr = by["row.dcr_p5_ratio"], by["row.nndr_p5_ratio"]
  assert dcr.value is None and dcr.detail["reason"] == HOLDOUT_P5_ZERO
  assert nndr.value is None and nndr.detail["reason"] == HOLDOUT_NNDR_P5_ZERO
  assert dcr.source_value == 0.0 and dcr.synthetic_value is not None
  assert _status(dcr) is Status.NOT_EVALUATED
  # the share itself is still measured
  assert by["row.dcr_train_holdout_share"].value is not None


def test_panel_rows_missing_a_column_are_not_evaluated():
  """The Gower encoder reads a missing column as NULL, so a panel row
  lacking a plan column is refused, naming the column."""
  table, rows = people(n_synthetic=300)
  assert table.panel is not None
  table = dataclasses.replace(
      table,
      panel=dataclasses.replace(
          table.panel,
          r_rows=[{
              k: v for k, v in r.items() if k != "city"
          } for r in table.panel.r_rows]))
  by = _by_id(_pure(table, rows).metrics)
  for metric_id in PRIVACY_METRIC_IDS:
    assert by[metric_id].value is None
    assert "city" in by[metric_id].detail["reason"], by[metric_id].detail


def test_detection_columns_follow_the_plan():
  """Keys never feed detection (identity columns do, as text); the PIT
  grids and dictionaries are the plan's (temporal grids in UNIX micros);
  a text column's head masks come from the SOURCE sample only, so
  reshaping the synthetic side's text changes the verdict, not the
  feature set."""
  table, rows = people(n_synthetic=300)
  spec = PrivacySpec.from_table(
      table, salt=SALT, privacy_sample_rows=300, detection_sample_rows=300)
  inputs = PanelInputs.from_table(table, spec)
  columns, excluded = detection_columns(inputs, rows["source"][:300])
  by = {c.name: c for c in columns}
  plan = {c.name: c for c in table.columns}
  assert "person_id" not in by and not excluded
  assert by["email"].kind is ColumnKind.IDENTIFIER
  assert by["signup_date"].grid is not None
  assert np.array_equal(by["signup_date"].grid,
                        plan["signup_date"].quantiles_src)
  assert by["signup_date"].grid[0] > 1e15  # 2023 in UNIX microseconds
  assert by["city"].dictionary == plan["city"].detection_dictionary
  assert by["surname"].head_masks == ("Aa99999999",)
  reshaped = [{
      **r, "surname": f"x-{i}"
  } for i, r in enumerate(rows["synthetic"])]
  plain = _by_id(_pure(table, rows).metrics)["table.detection_auc"]
  odd = _by_id(
      _pure(table, {
          "source": rows["source"],
          "synthetic": reshaped
      }).metrics)["table.detection_auc"]
  assert plain.feature_set_digest == odd.feature_set_digest
  assert plain.value is not None and odd.value is not None
  assert odd.value > 0.95 > plain.value
  assert plain.detail["folds"] == privacy.DETECTION_FOLDS


def test_pmse_row_always_carries_its_ceiling(clean_run, shifted_run):
  for _, _, result in (clean_run, shifted_run):
    pmse = _by_id(result.metrics)["table.pmse_ratio"]
    assert pmse.value is not None
    ceiling = pmse.detail["ceiling"]
    assert ceiling == pytest.approx(2 * DETECTION_ROWS / (pmse.detail["k"] - 1))
    assert _status(pmse) is not Status.NOT_EVALUATED


def test_sampled_mode_is_method_sample_with_its_rate():
  """R72: in sampled mode the metrics use the sampled synthetic rows, with
  method = sample and the synthetic read rate in detail; none of them
  needs full coverage, so all stay evaluated."""
  table, rows = people(n_synthetic=800)
  sampled = with_rates(table, synthetic=0.5)
  result = _pure(sampled, rows)
  for mv in result.metrics:
    assert mv.method is Method.SAMPLE
    assert mv.detail["sample_rate"] == 0.5, mv.metric_id
    assert mv.sample_rate is not None and mv.sample_rate <= 0.5
    assert mv.value is not None or mv.metric_id in ("row.dcr_p5_ratio",
                                                    "row.nndr_p5_ratio"), mv


def test_flags_never_carry_values_and_are_bounded(copies_run, shifted_run):
  for _, rows, result in (copies_run, shifted_run):
    by_check = Counter(f.check for f in result.flags)
    assert set(by_check) <= {NEAREST_RECORD, DETECTABLE}
    assert all(count <= _TOP_K for count in by_check.values())
    for flag in result.flags:
      assert flag.source_key_hash is None or flag.source_key_hash.startswith(
          "h:")
      assert set(flag.synthetic_key or {}) <= {"person_id"}
    text = _flag_json(result.flags)
    # the synthetic key IS the synthetic table's own key (like membership)
    _values_absent(
        text, {
            "source": rows["source"],
            "synthetic": [{
                **r, "person_id": None
            } for r in rows["synthetic"]]
        })


def test_profiles_are_bounded_and_carry_no_values(copies_run):
  _, rows, result = copies_run
  kinds = Counter((pv.profile_kind, pv.side) for pv in result.profiles)
  assert kinds == Counter({
      ("dcr_hist", "synthetic"): 1,
      ("dcr_hist", "holdout"): 1,
      ("nndr_hist", "synthetic"): 1,
      ("nndr_hist", "holdout"): 1,
      ("roc_curve", "both"): 1,
  })
  out = [
      to_profile_row(pv, evaluation_id="e1", evaluated_at=_EVALUATED_AT)
      for pv in result.profiles
  ]
  text = json.dumps(out, allow_nan=False)
  _values_absent(text, rows)
  for pv in result.profiles:
    if pv.profile_kind in ("dcr_hist", "nndr_hist"):
      assert len(pv.payload["counts"]) == len(pv.payload["edges"]) + 1 == 50
      assert sum(pv.payload["counts"]) == pv.payload["n"] == pv.n
      assert pv.edges_digest


def test_sample_does_not_depend_on_batching(clean_run):
  """The bottom-k sample, its multiplicities and every value are the same
  whatever the batch boundaries (a Beam split or merge order)."""
  table, rows, result = clean_run
  again = _pure(table, rows, chunk=4096)
  assert sorted(
      again.metrics, key=lambda m: m.metric_id) == sorted(
          result.metrics, key=lambda m: m.metric_id)
  assert _sorted_flags(again.flags) == _sorted_flags(result.flags)
  assert _sorted_profiles(again.profiles) == _sorted_profiles(result.profiles)


def test_sample_combine_is_order_free_and_counts_multiplicities():
  table, rows = people(n_synthetic=1200, duplicates=150)
  spec = PrivacySpec.from_table(
      table, salt=SALT, privacy_sample_rows=300, detection_sample_rows=300)
  combine = SampleCombineFn(spec.sample_k)
  batches = encode(table, "synthetic", rows["synthetic"], chunk=97)
  parts = [part for b in batches for _, part in sample_parts(spec, b)]

  def combined(order: Sequence[int]) -> Any:
    accs = []
    for chunk in np.array_split(np.array(order), 5):
      acc = combine.create_accumulator()
      for i in chunk.tolist():
        acc = combine.add_input(acc, parts[i])
      accs.append(acc)
    return combine.extract_output(combine.merge_accumulators(accs))

  forward = combined(range(len(parts)))
  backward = combined(list(reversed(range(len(parts)))))
  assert forward == backward
  assert forward.rows == 1200
  assert len(forward.entries) == spec.sample_k
  counts = [count for _, count in forward.entries]
  # the duplicated row is held once, with its exact multiplicity — when it
  # is sampled at all
  assert max(counts) in (1, 151)
  assert sum(counts) >= spec.sample_k


def test_sampling_prefilter_keeps_the_bottom_k():
  """The per-batch prefilter (priorities below a rate the plan's row count
  sets) only saves work: the kept sample equals an unfiltered bottom-k."""
  table, rows = people(n_synthetic=3000)
  spec = PrivacySpec.from_table(
      table, salt=SALT, privacy_sample_rows=200, detection_sample_rows=200)
  unfiltered = dataclasses.replace(spec, limits=(None, None))
  batches = encode(table, "synthetic", rows["synthetic"], chunk=500)

  def sample(s: PrivacySpec) -> Any:
    combine = SampleCombineFn(s.sample_k)
    acc = combine.create_accumulator()
    kept = 0
    for batch in batches:
      for _, part in sample_parts(s, batch):
        kept += len(part.entries)
        acc = combine.add_input(acc, part)
    return combine.extract_output(acc), kept

  filtered, kept_filtered = sample(spec)
  full, kept_full = sample(unfiltered)
  assert filtered == full
  assert kept_full == 3000 and kept_filtered < 1000


def test_a_stale_row_count_says_why_the_sample_is_empty():
  """A plan counting far more rows and distinct values than are read sets
  a prefilter rate no row passes: both blocks say so instead of "no rows
  were read"."""
  table, rows = people(n_synthetic=300)
  stale = dataclasses.replace(
      table,
      rows_synthetic=10**12,
      columns=tuple(
          dataclasses.replace(c, synthetic_distinct=10**12)
          for c in table.columns))
  by = _by_id(_pure(stale, rows).metrics)
  for metric_id in OWNED_METRIC_IDS:
    assert by[metric_id].value is None
    assert "prefilter" in by[metric_id].detail["reason"], metric_id


def _overstated(table: Any, factor: int) -> Any:
  """`table` with its synthetic row and distinct counts `factor` times
  what is read: a stale plan."""
  return dataclasses.replace(
      table,
      rows_synthetic=table.rows_synthetic * factor,
      columns=tuple(
          dataclasses.replace(
              c,
              synthetic_distinct=(c.synthetic_distinct *
                                  factor if c.synthetic_distinct else None))
          for c in table.columns))


def test_a_short_sample_is_flagged_with_its_counts(clean_run):
  """R85: a sample the prefilter left short (fewer than k keys held
  although rows were left out) is still evaluated, and every row built on
  it says so, with the side's counts; a full sample never does."""
  table, rows, clean = clean_run
  for mv in clean.metrics:
    assert "sample_short" not in mv.detail, mv.metric_id
  result = _pure(_overstated(table, 100), rows)
  k = max(SAMPLE_ROWS, DETECTION_ROWS)
  for mv in result.metrics:
    assert mv.detail["sample_short"] is True, mv.metric_id
    counts = mv.detail["sample_short_counts"]
    assert set(counts) == {"synthetic"}, mv.metric_id  # the source is full
    short = counts["synthetic"]
    assert short["rows_read"] == len(rows["synthetic"]) and short["k"] == k
    assert 20 <= short["held_keys"] == short["held_rows"] < min(
        k, short["rows_read"])
    assert mv.value is not None or mv.metric_id in ("row.dcr_p5_ratio",
                                                    "row.nndr_p5_ratio"), mv
  share = _by_id(result.metrics)["row.dcr_train_holdout_share"]
  assert share.n_synthetic == short["held_rows"]


def _heavy_rows() -> list[dict]:
  """1,000 distinct synthetic people and 5,000 copies of one more row."""
  heavy = people_rows(1, 6, SYNTHETIC_ID_BASE + 900_000)[0]
  return people_rows(1000, 5,
                     SYNTHETIC_ID_BASE) + [dict(heavy) for _ in range(5000)]


def test_one_key_holding_most_rows_does_not_shorten_the_sample():
  """The prefilter's rate is per KEY (a lower bound of the distinct rows
  read), so a key holding 5/6 of a side neither shortens the sample nor
  changes it: the filtered bottom-k equals the unfiltered one."""
  source = people_rows(2000, 31, SOURCE_ID_BASE)
  synthetic = _heavy_rows()
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY)
  for s in range(8):
    salt = f"heavy-{s}"
    batches = encode(table, "synthetic", synthetic, chunk=500, salt=salt)
    for k in (50, 200):
      spec = PrivacySpec.from_table(
          table, salt=salt, privacy_sample_rows=k, detection_sample_rows=k)
      assert spec.limits[1] is not None  # the prefilter is on
      filtered = _combined(spec, batches)
      assert filtered == _combined(
          dataclasses.replace(spec, limits=(None, None)), batches)
      assert len(filtered.entries) == k, (salt, k)


def _combined(spec: PrivacySpec, batches: Sequence[Any]) -> Any:
  combine = SampleCombineFn(spec.sample_k)
  acc = combine.create_accumulator()
  for batch in batches:
    for _, part in sample_parts(spec, batch):
      acc = combine.add_input(acc, part)
  return combine.extract_output(acc)


# --------------------------------------------------------------------------
# row samples (Ruling R85)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", name="keyless_run")
def fixture_keyless_run() -> tuple[Any, dict, PrivacyResult]:
  table, rows = keyless()
  return table, rows, _pure(table, rows)


def test_keyless_repeated_rows_faithful_generator_passes(keyless_run):
  """A key-less table whose whole rows repeat (200 patterns over 20,000
  rows a side): the privacy sample's priority prefix is a handful of
  heavy keys, but density and coverage read a systematic PPS sample over
  every held key, and detection simple random rows — so a faithful
  generator passes both and the detection baseline sits at chance."""
  _, _, result = keyless_run
  by = _by_id(result.metrics)
  share, coverage = by["row.dcr_train_holdout_share"], by["row.coverage"]
  assert share.detail["sample_records"] < 60  # the cluster prefix
  assert coverage.detail["density_records"] >= 150  # most patterns drawn
  assert coverage.value is not None and coverage.value >= 0.95
  assert _status(coverage) is Status.PASS
  density = by["row.density"]
  assert density.value is not None and abs(density.value - 1.0) < 0.1
  assert _status(density) is Status.PASS
  auc = by["table.detection_auc"]
  assert auc.value is not None and 0.4 <= auc.value <= 0.6
  assert auc.baseline_value is not None and 0.4 <= auc.baseline_value <= 0.6
  assert _status(auc) is Status.PASS
  assert _status(share) is not Status.FAIL
  # DeLong reads repeated rows as independent, so its ci_low can clear 0.5
  # here by chance; detectable flags wait for a WARN-level AUC
  assert not [f for f in result.flags if f.check == DETECTABLE]


def test_keyless_misspecified_generator_still_separates():
  """The same table with a generator drawing the patterns uniformly
  instead of Zipf(1.2): detection separates it as a simple random sample
  of rows would, the baseline stays at chance, and density sees the
  synthetic rows fall away from the source's dense patterns."""
  table, rows = keyless(source_s=1.2, synthetic_s=None, seed=5)
  by = _by_id(_pure(table, rows).metrics)
  auc = by["table.detection_auc"]
  assert auc.value is not None and auc.value >= 0.85
  assert auc.ci_low is not None and auc.ci_low > 0.8
  assert _status(auc) is Status.FAIL
  assert auc.baseline_value is not None and 0.4 <= auc.baseline_value <= 0.6
  assert by["table.pmse_ratio"].value is not None
  assert _status(by["table.pmse_ratio"]) is Status.FAIL
  density = by["row.density"]
  assert density.value is not None and density.value < 0.6
  assert _status(density) is Status.FAIL


@pytest.mark.parametrize("salt", ["ref-a", "ref-b", "ref-c"])
def test_density_matches_an_independent_reconstruction(salt):
  """n_syn > |R| (2,600 synthetic rows, R = 2,000) with one key held
  1,000 times: rebuilt independently — the bottom-k by priority, then
  2,000 systematic points walked row by row from the salt's start —
  density and coverage match `density_coverage` on those rows, and the
  share still matches `nn_privacy` on the multiplicity prefix, to 1e-12."""
  source = people_rows(4200, 31, SOURCE_ID_BASE)
  fresh = people_rows(2500, 77, SYNTHETIC_ID_BASE)
  donor = copy_content(source[5],
                       people_rows(1, 78, SYNTHETIC_ID_BASE + 900_000)[0])
  synthetic = fresh + [dict(donor) for _ in range(1000)]
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, PANEL_ROWS))
  privacy_rows = 2600
  result = privacy_outputs(
      table,
      encode_all(
          table, {
              "source": source,
              "synthetic": synthetic
          },
          chunk=613,
          salt=salt),
      salt=salt,
      label_key=LABEL_KEY,
      privacy_sample_rows=privacy_rows,
      detection_sample_rows=40,
      row_flags_top_k=5)
  by = _by_id(result.metrics)
  # the bottom-k, rebuilt: one entry per (priority, row hash), in order
  batch = encode(
      table, "synthetic", synthetic, chunk=len(synthetic), salt=salt)[0]
  prio = privacy.sample_priority(batch.row_hash, salt)
  held: dict[tuple[int, int], list[int]] = {}
  for i in range(len(synthetic)):
    held.setdefault((int(prio[i]), int(batch.row_hash[i])), [i, 0])[1] += 1
  order = sorted(held)[:privacy_rows]
  firsts = [held[key][0] for key in order]
  counts = [held[key][1] for key in order]
  assert max(counts) == 1000
  # the privacy (share) sample: the multiplicity prefix
  prefix: list[dict] = []
  for first, count in zip(firsts, counts, strict=True):
    prefix.extend([synthetic[first]] * min(count, privacy_rows - len(prefix)))
  assert len(prefix) == privacy_rows
  # density: n_eq = |R| systematic points over the held rows
  total, n_eq = sum(counts), PANEL_ROWS
  start = hash64(f"{privacy.PPS_LABEL}:density", salt) % total
  bounds = np.cumsum(counts)
  fake = [
      synthetic[firsts[bisect.bisect_right(bounds,
                                           (start + j * total) // n_eq)]]
      for j in range(n_eq)
  ]
  space = _space(table, salt=salt)
  assert table.panel is not None
  density, coverage = density_coverage(
      *space.encode(table.panel.r_rows[:n_eq]),
      *space.encode(fake),
      k=privacy.DENSITY_K)
  assert by["row.density"].value == pytest.approx(density, abs=1e-12)
  assert by["row.coverage"].value == pytest.approx(coverage, abs=1e-12)
  assert by["row.density"].n_synthetic == n_eq
  expected = summarize_nn(
      nn_privacy(
          space,
          table.panel.r_rows,
          table.panel.h_rows,
          prefix,
          k=privacy.DENSITY_K))
  share = by["row.dcr_train_holdout_share"]
  assert share.value == pytest.approx(
      expected["dcr_train_holdout_share"], abs=1e-12)
  assert share.ci_low == pytest.approx(
      expected["dcr_train_holdout_share_ci_low"], abs=1e-12)
  assert share.n_synthetic == privacy_rows


def test_detection_rows_are_bounded_by_their_knob():
  """R85: each side reaches detection as a `detection_sample_rows`-row
  sample, whatever the privacy sample's size."""
  table, rows = people(n_synthetic=2500)
  spec = PrivacySpec.from_table(
      table, salt=SALT, privacy_sample_rows=2500, detection_sample_rows=100)
  for side in ("source", "synthetic"):
    sample = _combined(spec, encode(table, side, rows[side]))
    assert len(sample.entries) == 2500
    view = privacy.detection_view(spec, side, sample)
    assert view.size == 100 and len(view.entries) <= 100
    assert (view.rows, view.held, view.keys) == (len(rows[side]), 2500, 2500)


@pytest.mark.parametrize("simple", [False, True])
def test_row_draws_are_equal_probability(simple):
  """Systematic PPS and the simple random draw: exactly min(n, T) rows,
  never more of a key than it holds (systematic: the floor or ceiling of
  c·n/T), all rows when n >= T, and over salts each key drawn about
  c·n/T times — every row equally likely."""
  counts = [1, 1, 7, 30, 2, 1, 59]
  total, n = sum(counts), 25
  assert privacy.pps_hits(
      counts, 500, salt="s", purpose="t", simple=simple) == counts
  sums = np.zeros(len(counts))
  salts = 600
  for s in range(salts):
    hits = privacy.pps_hits(counts, n, salt=f"s{s}", purpose="t", simple=simple)
    assert sum(hits) == n
    assert all(0 <= h <= c for h, c in zip(hits, counts, strict=True))
    if not simple:
      assert all(
          math.floor(c * n / total) <= h <= math.ceil(c * n / total)
          for h, c in zip(hits, counts, strict=True))
    sums += hits
  expected = np.array(counts) * n / total
  assert np.allclose(sums / salts, expected, atol=0.35)


def test_throughput_gower_knn_chunked(record_property):
  """A scaled-down privacy batch: 4,096 synthetic rows against R and H of
  2,000 rows each; the full-scale figure (50,000 against 20,000) is in
  the Task 24 report."""
  table, rows = people(n_synthetic=4096)
  space = _space(table)
  assert table.panel is not None
  r = space.encode(table.panel.r_rows)
  h = space.encode(table.panel.h_rows)
  q = space.encode(rows["synthetic"])
  tracemalloc.start()
  start = clock.perf_counter()
  gower_knn(*q, *r, k=2)
  gower_knn(*q, *h, k=1)
  elapsed = clock.perf_counter() - start
  _, peak = tracemalloc.get_traced_memory()
  tracemalloc.stop()
  record_property("gower_rows_per_s", round(4096 / elapsed))
  record_property("gower_peak_mb", round(peak / 1e6, 1))
  assert peak < 2 * 64e6 + 50e6  # two chunk working sets plus the blocks
  assert effective_chunk(PANEL_ROWS, space.n_features) >= 1
  assert elapsed < 60


def test_driver_side_spec_is_slim(beam_run):
  """Only the slim spec is pickled into the DoFns: the panel rows and the
  Gower grids travel as a side input."""
  table, rows = beam_run["tables"]["people"]
  spec = PrivacySpec.from_table(
      table, salt=SALT, privacy_sample_rows=2200, detection_sample_rows=1000)
  assert len(pickle.dumps(spec)) < 20_000
  surname = rows["source"][0]["surname"]
  assert surname.encode() not in pickle.dumps(spec)


def test_source_side_is_never_value_published(beam_run):
  """Detection and distance profiles are computed on source rows, but no
  source value reaches a metric detail or a payload."""
  for name, (table, rows) in beam_run["tables"].items():
    metrics = [mv for mv in beam_run["metrics"] if mv.table == name]
    text = json.dumps([
        to_metric_row(
            mv,
            evaluation_id="e1",
            evaluated_at=_EVALUATED_AT,
            landing_table=table.landing_table,
            source_table=table.source_table) for mv in metrics
    ],
                      allow_nan=False)
    _values_absent(text, rows)
