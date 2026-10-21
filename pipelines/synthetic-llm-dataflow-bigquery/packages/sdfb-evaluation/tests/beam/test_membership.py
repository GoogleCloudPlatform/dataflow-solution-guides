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
"""Tests for `sdfb_evaluation.beam.membership` (Task 23): map-side row
membership against the R/E/H panel and the full source, the
memorization / exposure / near-match lifts, internal and key duplicates,
and the bounded exact/near-copy row flags (keys only, never values).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import base64
import bz2
import dataclasses
import functools
import json
import math
import pickle
import time as clock
import tracemalloc
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import apache_beam as beam
import numpy as np
import pytest
from apache_beam.internal import pickler
from apache_beam.portability import common_urns
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.coders import coders
from apache_beam.pvalue import TaggedOutput
from apache_beam.testing.util import assert_that
from scipy.stats import hypergeom

from sdfb_evaluation.beam import census, dense, membership
from sdfb_evaluation.beam.encode import BatchEncoder, EncodeSide, key_hash
from sdfb_evaluation.beam.io import InMemorySources
from sdfb_evaluation.beam.label_key import LabelKey
from sdfb_evaluation.beam.membership import (
    EXACT_COPY,
    NEAR_COPY,
    OWNED_METRIC_IDS,
    UNVERIFIED_REASON,
    KeyCountsCombineFn,
    Membership,
    MembershipSpec,
    PanelIndex,
    PanelRefs,
    RowFlag,
    RowKeysFn,
    batch_membership,
    duplicate_excess,
    duplicate_null_variance,
    flag_row,
    membership_outputs,
    rarefied_duplicates,
)
from sdfb_evaluation.canonical import hashed_label
from sdfb_evaluation.catalogue import load_catalogue
from sdfb_evaluation.context.budget import (
    MEMBERSHIP_CODE_BYTES,
    fixed_shuffle_bytes,
    membership_code_bytes,
    source_sets_fit,
)
from sdfb_evaluation.context.reference import Panel
from sdfb_evaluation.schemas import load_schema
from sdfb_evaluation.scoring import status_for, to_metric_row
from sdfb_evaluation.stats import noise
from sdfb_evaluation.types import Method, MetricValue, Side, Status

from .membership_data import (
    CATALOG_PATTERNS,
    LABEL_KEY,
    PEOPLE_FIELDS,
    PEOPLE_IDENTITY,
    PEOPLE_NONKEY,
    SALT,
    SOURCE_ID_BASE,
    SYNTHETIC_ID_BASE,
    catalog_table,
    copy_content,
    encode,
    encode_all,
    panel_of,
    people_rows,
    people_table,
    planned,
    zipf_weights,
)

_CATALOGUE = load_catalogue()
PEOPLE_FIELDS_NAMES = tuple(f["name"] for f in PEOPLE_FIELDS)
_EVALUATED_AT = "2026-09-29T00:00:00+00:00"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _pure(table: Any, rows_by: Mapping[str, Sequence[Mapping[str, Any]]],
          **kwargs: Any) -> membership.MembershipResult:
  """The in-process path over the same encoded batches Beam sees."""
  kwargs.setdefault("label_key", LABEL_KEY)
  return membership_outputs(
      table, encode_all(table, rows_by), salt=SALT, **kwargs)


def _collect(pcoll: beam.PCollection, path: Path, label: str) -> None:

  def dump(actual: Sequence[Any]) -> None:
    path.write_bytes(pickle.dumps(list(actual)))

  assert_that(pcoll, dump, label=label)


def _run(table: Any, rows_by: Mapping[str, list], tmp_path: Path,
         **kwargs: Any) -> tuple[list[MetricValue], list[RowFlag]]:
  """The Beam path on the local runner (FnApiRunner): read, encode, membership."""
  sources = InMemorySources({
      (table.name, side): rows for side, rows in rows_by.items()
  })
  with BeamTestPipeline() as p:
    batches = [
        sources.read(p, table, side) | EncodeSide(table, side, salt=SALT)
        for side in rows_by
    ] | "Flatten" >> beam.Flatten()
    key = p | "Key" >> beam.Create([LABEL_KEY])
    out = batches | "Membership" >> Membership(
        [table], salt=SALT, label_key=key, **kwargs)
    _collect(out["metrics"], tmp_path / "metrics.pkl", "Metrics")
    _collect(out["flags"], tmp_path / "flags.pkl", "Flags")
  metrics = pickle.loads((tmp_path / "metrics.pkl").read_bytes())
  flags = pickle.loads((tmp_path / "flags.pkl").read_bytes())
  return metrics, flags


def _by_id(metrics: Sequence[MetricValue]) -> dict[str, MetricValue]:
  out: dict[str, MetricValue] = {}
  for mv in metrics:
    assert mv.metric_id not in out, f"duplicate row {mv.metric_id}"
    out[mv.metric_id] = mv
  return out


def _status(mv: MetricValue) -> Status:
  return status_for(_CATALOGUE.get(mv.metric_id), mv)


def _comparable(mv: MetricValue) -> tuple:
  """A metric row minus the path note (side input vs keyed count)."""
  detail = {
      k: v for k, v in mv.detail.items() if k not in ("path", "path_note")
  }
  return (dataclasses.replace(mv,
                              detail={}), json.dumps(detail, sort_keys=True))


def _people_with(copies: int = 0,
                 full_copies: int = 0,
                 near_copies: int = 0,
                 *,
                 e_n: int | None = None,
                 verified: bool = True,
                 n_source: int = 3000,
                 n_synthetic: int = 2000,
                 n_reference: int = 600) -> tuple[Any, dict, dict]:
  """The people table with planted copies: `copies` synthetic rows carry
  an R row's content under their own key, `full_copies` the whole R row
  (its key included), `near_copies` an R row's content with the city
  changed. Returns (table, rows by side, the donor of each copied row by
  synthetic person_id)."""
  source = people_rows(n_source, 31, SOURCE_ID_BASE)
  synthetic = people_rows(n_synthetic, 32, SYNTHETIC_ID_BASE)
  donors: dict[int, dict] = {}
  at = 0
  for k in range(copies):
    donor = source[(7 * k) % n_reference]
    synthetic[at] = copy_content(donor, synthetic[at])
    donors[synthetic[at]["person_id"]] = donor
    at += 1
  for k in range(full_copies):
    donor = source[(11 * k + 301) % n_reference]
    synthetic[at] = dict(donor)  # its key and identity too
    donors[synthetic[at]["person_id"]] = donor
    at += 1
  for k in range(near_copies):
    donor = source[(13 * k + 5) % n_reference]
    changed = copy_content(donor, synthetic[at])
    changed["city"] = f"Elsewhere{k:03d}"
    synthetic[at] = changed
    donors[synthetic[at]["person_id"]] = donor
    at += 1
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, n_reference, e_n=e_n, verified=verified))
  return table, {"source": source, "synthetic": synthetic}, donors


def _flag_values_absent(flags: Sequence[RowFlag], rows_by: Mapping[str,
                                                                   Sequence],
                        columns: Sequence[str]) -> None:
  """No attribute value of any row appears in any flag row's JSON."""
  text = json.dumps([
      flag_row(f, evaluation_id="e1", evaluated_at=_EVALUATED_AT) for f in flags
  ],
                    default=str)
  for rows in rows_by.values():
    for row in rows:
      for column in columns:
        value = row[column]
        if value is None:
          continue
        literal = str(value)
        if len(literal) >= 4:  # short numbers collide with ranks/digests
          assert literal not in text, (column, literal)


# --------------------------------------------------------------------------
# the brief's tests
# --------------------------------------------------------------------------
def test_verbatim_reference_copies_lift_and_flags():
  table, rows, donors = _people_with(copies=40, full_copies=5, e_n=200)
  result = _pure(table, rows)
  by_id = _by_id(result.metrics)
  n_syn = len(rows["synthetic"])
  # 45 synthetic rows reproduce an R record's content
  lift = by_id["row.memorization_lift"]
  assert lift.detail["events_r"] == 45 and lift.detail["events_h"] == 0
  assert lift.ci_low is not None and lift.ci_low >= 5
  assert _status(lift) is Status.FAIL
  nonkey = by_id["row.exact_match_rate_nonkey"]
  assert nonkey.value == pytest.approx(45 / n_syn)
  assert nonkey.ci_low <= nonkey.value <= nonkey.ci_high
  assert _status(nonkey) is Status.FAIL
  # only the 5 full copies match a source row key included
  full = by_id["row.exact_match_rate"]
  assert full.value == pytest.approx(5 / n_syn)
  assert _status(full) is Status.FAIL
  # flags: every copy, E before R, the donor's key as a keyed hash
  flags = [f for f in result.flags if f.check == EXACT_COPY]
  assert len(flags) == 45
  assert [f.rank for f in flags] == list(range(1, 46))
  sets = [f.source_set for f in flags]
  assert set(sets) == {"E", "R"}
  assert sets == sorted(sets, key=["E", "R", "H", "source"].index)
  for flag in flags:
    assert flag.synthetic_key is not None
    donor = donors[flag.synthetic_key["person_id"]]
    assert flag.source_key_hash == hashed_label(
        key_hash([donor["person_id"]]), key=LABEL_KEY)
    assert flag.distance == 0.0
    assert flag.detail["full_row"] is (
        donor["person_id"] == flag.synthetic_key["person_id"])
  assert not [f for f in result.flags if f.check == NEAR_COPY]


def test_chance_matches_do_not_trip_lift():
  """A low-entropy table: synthetic rows drawn from the source's own
  (skewed) pattern distribution hit R and H alike by chance. Many
  synthetic rows per pattern (n_syn >> n) make row counts overdispersed;
  the lift counts distinct records, so its interval covers 1."""
  for seed in range(4):
    rng = np.random.default_rng(100 + seed)
    weights = zipf_weights(CATALOG_PATTERNS, 1.1)
    source = rng.choice(CATALOG_PATTERNS, size=2400, p=weights)
    synthetic = rng.choice(CATALOG_PATTERNS, size=8000, p=weights)
    table, rows = catalog_table(source, synthetic, n_reference=800)
    by_id = _by_id(_pure(table, rows).metrics)
    lift = by_id["row.memorization_lift"]
    assert lift.detail["events_r"] > 20 and lift.detail["events_h"] > 20
    assert lift.detail["rows_r"] > lift.detail["events_r"]  # clustered rows
    assert lift.ci_low <= 1.0 <= lift.ci_high, (seed, lift)
    assert _status(lift) is Status.PASS
    near = by_id["row.near_match_lift"]
    assert near.ci_low <= 1.0 <= near.ci_high, (seed, near)
    # the chance hits are real exact matches of the source
    assert by_id["row.exact_match_rate_nonkey"].value > 0.3


def test_exposure_lift_attributes_prompt_rows():
  """Copies drawn only from E (the prompt-exposed ranks) raise the
  exposure lift; R\\E vs H\\H_E (`detail.unexposed`) stays at chance."""
  rng = np.random.default_rng(7)
  source = rng.choice(CATALOG_PATTERNS, size=3000)  # uniform patterns
  r_patterns = set(source[:1000].tolist())
  h_patterns = set(source[1000:2000].tolist())
  e_exclusive = [
      p for p in dict.fromkeys(source[:250].tolist()) if p not in h_patterns
  ]
  copies = e_exclusive[:100]
  synthetic = np.concatenate([rng.choice(CATALOG_PATTERNS, size=500), copies])
  table, rows = catalog_table(source, synthetic, n_reference=1000, e_n=250)
  by_id = _by_id(_pure(table, rows).metrics)
  exposure = by_id["row.exposure_lift"]
  assert exposure.value > 5 and exposure.ci_low >= 2
  assert _status(exposure) in (Status.WARN, Status.FAIL)
  unexposed = exposure.detail["unexposed"]
  assert unexposed["ci_low"] <= 1.0 <= unexposed["ci_high"]
  assert unexposed["events_r"] > 10 and unexposed["events_h"] > 10
  # the whole-R lift carries the E copies too, diluted by R\E
  lift = by_id["row.memorization_lift"]
  assert 1.0 < lift.value < exposure.value
  assert r_patterns  # (the fixture really has an R\E part)


def test_near_match_one_field_changed():
  table, rows, donors = _people_with(copies=10, near_copies=30, e_n=300)
  result = _pure(table, rows)
  by_id = _by_id(result.metrics)
  n_syn = len(rows["synthetic"])
  rate = by_id["row.near_match_rate"]
  # the 10 exact copies are exact, not near
  assert rate.value == pytest.approx(30 / n_syn)
  assert rate.ci_low <= rate.value <= rate.ci_high
  assert _status(rate) is Status.FAIL
  lift = by_id["row.near_match_lift"]
  assert lift.detail["events_r"] == 30 and lift.detail["events_h"] == 0
  assert lift.ci_low >= 5
  near = [f for f in result.flags if f.check == NEAR_COPY]
  assert len(near) == 30
  for flag in near:
    assert flag.detail == {"differs_in": "city"}
    donor = donors[flag.synthetic_key["person_id"]]
    assert flag.source_key_hash == hashed_label(
        key_hash([donor["person_id"]]), key=LABEL_KEY)
    assert flag.distance == pytest.approx(1 / len(PEOPLE_NONKEY))
    assert flag.source_set in ("E", "R")
  assert {f.source_set for f in near} == {"E", "R"}
  exact = [f for f in result.flags if f.check == EXACT_COPY]
  assert len(exact) == 10


def test_near_match_is_verified_against_the_row_not_the_hash():
  """A leave-one-out hash hit whose record differs in a second column (a
  hash collision, simulated by editing the R row's cell codes but not its
  row hash) is rejected by the exact per-column check."""
  table, rows, _ = _people_with(near_copies=1)
  spec = MembershipSpec.from_table(table, salt=SALT)
  refs = PanelRefs.from_table(table, spec)
  batches = encode(table, "synthetic", rows["synthetic"], chunk=4096)
  index = PanelIndex.build(spec, refs)
  hits = sum(
      batch_membership(spec, index, None, b, top_k=10)[0].near_rows_r
      for b in batches)
  assert hits == 1
  donor_rank = 5  # `_people_with`'s first near-copy donor
  tampered = refs.r_cells.copy()
  surname = spec.nonkey_columns.index("surname")
  tampered[donor_rank, surname] ^= np.uint64(1)  # same row hash, new cell
  collided = PanelIndex.build(spec, dataclasses.replace(refs, r_cells=tampered))
  hits = sum(
      batch_membership(spec, collided, None, b, top_k=10)[0].near_rows_r
      for b in batches)
  assert hits == 0


def test_reference_unverified_marks_lifts_not_evaluated():
  table, rows, _ = _people_with(copies=40, near_copies=10, verified=False)
  result = _pure(table, rows)
  by_id = _by_id(result.metrics)
  for metric_id in ("row.memorization_lift", "row.exposure_lift",
                    "row.near_match_rate", "row.near_match_lift"):
    mv = by_id[metric_id]
    assert mv.value is None and mv.ci_low is None, metric_id
    assert mv.detail["reason"] == (f"{UNVERIFIED_REASON}: reference digest "
                                   "mismatch (test)"), metric_id
    assert _status(mv) is Status.NOT_EVALUATED
  # the full-source metrics are still computed
  assert by_id["row.exact_match_rate_nonkey"].value == pytest.approx(
      40 / len(rows["synthetic"]))
  assert by_id["row.internal_duplicate_excess"].value is not None
  # no flag claims R, E or H membership
  assert result.flags
  assert {f.source_set for f in result.flags} == {"source"}


def test_pk_duplicates_fail_integrity():
  source = people_rows(1500, 31, SOURCE_ID_BASE)
  synthetic = people_rows(1200, 32, SYNTHETIC_ID_BASE)
  for k in range(5):  # five duplicated keys: ten rows
    synthetic[100 + k]["person_id"] = synthetic[k]["person_id"]
  for k in range(3):  # NULL keys: a separate integrity signal
    synthetic[200 + k]["person_id"] = None
  for k in range(4):  # an identity value reused by a second row: 8 rows
    synthetic[300 + k]["email"] = synthetic[400 + k]["email"]
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=("email",),
      panel=panel_of(source, 300))
  by_id = _by_id(
      _pure(table, {
          "source": source,
          "synthetic": synthetic
      }).metrics)
  pk = by_id["table.pk_duplicate_rate"]
  assert pk.value == pytest.approx(10 / 1200)
  assert pk.detail["duplicate_rows"] == 10
  assert pk.detail["null_key_rows"] == 3
  assert _status(pk) is Status.FAIL
  ident = by_id["table.identity_duplicate_rate"]
  assert ident.value == pytest.approx(8 / 1200)
  assert _status(ident) is Status.FAIL
  # a table without a declared key is NOT_EVALUATED, never clean
  keyless = planned("people", PEOPLE_FIELDS, source, synthetic)
  by_id = _by_id(
      _pure(keyless, {
          "source": source,
          "synthetic": synthetic
      }).metrics)
  for metric_id in ("table.pk_duplicate_rate", "table.identity_duplicate_rate"):
    assert by_id[metric_id].value is None
    assert "declared" in by_id[metric_id].detail["reason"]


def test_flags_never_carry_values(tmp_path):
  table, rows, _ = _people_with(copies=12, full_copies=3, near_copies=8)
  metrics, flags = _run(table, rows, tmp_path, row_flags_top_k=100)
  assert {f.check for f in flags} == {EXACT_COPY, NEAR_COPY}
  schema = [f["name"] for f in load_schema("evaluation_row_flags")]
  for flag in flags:
    row = flag_row(flag, evaluation_id="e1", evaluated_at=_EVALUATED_AT)
    assert list(row) == schema
    json.dumps(row, allow_nan=False)
    assert row["source_key"] is None
    assert set(row["synthetic_key"]) == {"person_id"}  # the PK only
    assert row["source_key_hash"].startswith("h:")
    assert row["source_set"] in ("R", "E", "H", "source")
  # no attribute value, no source key, no identity value
  _flag_values_absent(flags, rows, (*PEOPLE_NONKEY, "email"))
  text = json.dumps([
      flag_row(f, evaluation_id="e1", evaluated_at=_EVALUATED_AT) for f in flags
  ])
  for row in rows["source"]:
    assert str(row["person_id"]) not in text or any(
        f.synthetic_key["person_id"] == row["person_id"] for f in flags)
  assert metrics


# --------------------------------------------------------------------------
# more behaviour
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", name="copies_run")
def fixture_copies_run(tmp_path_factory: pytest.TempPathFactory) -> tuple:
  """One local-runner (FnApiRunner) pass (side-input mode) over a people table with
  every planted defect, shared by the tests that only read its output."""
  table, rows, donors = _people_with(
      copies=15,
      full_copies=4,
      near_copies=9,
      e_n=300,
      n_source=1500,
      n_synthetic=1200,
      n_reference=400)
  metrics, flags = _run(table, rows, tmp_path_factory.mktemp("copies"))
  return table, rows, donors, metrics, flags


def test_beam_equals_the_in_process_path(copies_run):
  table, rows, _, metrics, flags = copies_run
  pure = _pure(table, rows)
  assert sorted(
      metrics, key=lambda m: m.metric_id) == sorted(
          pure.metrics, key=lambda m: m.metric_id)
  assert sorted(
      flags, key=lambda f: (f.check, f.rank)) == sorted(
          pure.flags, key=lambda f: (f.check, f.rank))


def test_keyed_count_path_matches_the_side_input(copies_run, tmp_path):
  """Above the side-input budget (forced here with 0 bytes) the full-source
  matches come from the keyed count: the same metric values; only the
  full-source ('source') flags need the map-side set."""
  table, rows, _, metrics, flags = copies_run
  keyed_metrics, keyed_flags = _run(
      table, rows, tmp_path, source_set_max_bytes=0)
  assert sorted(
      map(_comparable, keyed_metrics), key=lambda c: c[0].metric_id) == sorted(
          map(_comparable, metrics), key=lambda c: c[0].metric_id)
  paths = {
      mv.detail.get("path")
      for mv in keyed_metrics
      if mv.metric_id.startswith("row.exact_match")
  }
  assert paths == {"keyed_count"}
  # every copy here comes from the panel, so no flag needs the source set
  assert not [f for f in flags if f.source_set == "source"]
  assert sorted(
      keyed_flags, key=lambda f: (f.check, f.rank)) == sorted(
          flags, key=lambda f: (f.check, f.rank))


def test_catalogue_coverage_every_owned_id_emitted_or_explained(copies_run):
  table, _, _, metrics, _ = copies_run
  ids = set(_CATALOGUE.ids())
  assert set(OWNED_METRIC_IDS) <= ids
  assert Counter(mv.metric_id for mv in metrics) == Counter(OWNED_METRIC_IDS)
  for mv in metrics:
    metric = _CATALOGUE.get(mv.metric_id)
    assert metric.level in ("row", "table")
    assert mv.column is None and mv.table == table.name
    if mv.value is None and not metric.uses_ci_bound:
      assert mv.detail.get("reason"), mv
    if metric.noise_floor in ("wilson", "rate_ratio") and mv.value is not None:
      assert mv.ci_low is not None, mv  # R41: intervals carry their CI
    assert mv.encoding_plan_digest == table.encoding_plan_digest
    json.dumps(
        to_metric_row(
            mv,
            evaluation_id="e1",
            evaluated_at=_EVALUATED_AT,
            landing_table=table.landing_table,
            source_table=table.source_table),
        allow_nan=False)
  # with the dense pass, every row-level id has an owner except Task 24's
  # nearest-neighbour metrics
  row_ids = {m.id for m in _CATALOGUE.by_level("row")}
  owned = set(OWNED_METRIC_IDS) | set(dense.OWNED_METRIC_IDS) | set(
      census.OWNED_METRIC_IDS)
  assert row_ids - owned == {
      "row.dcr_train_holdout_share", "row.dcr_p5_ratio", "row.nndr_p5_ratio",
      "row.density", "row.coverage"
  }


def test_degenerate_tables_are_not_evaluated_with_reasons():
  table, rows, _ = _people_with()
  empty = _by_id(
      _pure(table, {
          "source": rows["source"],
          "synthetic": []
      }).metrics)
  assert set(empty) == set(OWNED_METRIC_IDS)
  for metric_id, mv in empty.items():
    assert mv.value is None and mv.detail.get("reason"), metric_id
  no_panel = dataclasses.replace(table, panel=None)
  by_id = _by_id(_pure(no_panel, rows).metrics)
  for metric_id in ("row.memorization_lift", "row.exposure_lift",
                    "row.near_match_rate", "row.near_match_lift"):
    assert "panel" in by_id[metric_id].detail["reason"], metric_id
  assert by_id["row.exact_match_rate"].value is not None
  # every column a key: nothing to match on but the key tuple itself
  keys_only = planned(
      "links",
      PEOPLE_FIELDS[:2], [{
          "person_id": r["person_id"],
          "email": r["email"]
      } for r in rows["source"]], [{
          "person_id": r["person_id"],
          "email": r["email"]
      } for r in rows["synthetic"]],
      pk=("person_id",),
      identity=("email",),
      panel=None)
  by_id = _by_id(
      _pure(
          keys_only, {
              "source": [{
                  "person_id": r["person_id"],
                  "email": r["email"]
              } for r in rows["source"]],
              "synthetic": [{
                  "person_id": r["person_id"],
                  "email": r["email"]
              } for r in rows["synthetic"]]
          }).metrics)
  for metric_id in ("row.exact_match_rate_nonkey",
                    "row.internal_duplicate_excess", "row.near_match_rate"):
    assert "non-key" in by_id[metric_id].detail["reason"], metric_id
  assert by_id["row.exact_match_rate"].value == 0.0


def test_clean_run_lift_is_null_with_a_finite_bound():
  """R38: no match on either side leaves the lift undefined (value NULL)
  with ci_low 0 — PASS on the bound, never NOT_EVALUATED."""
  table, rows = people_table()
  by_id = _by_id(_pure(table, rows).metrics)
  for metric_id in ("row.memorization_lift", "row.exposure_lift",
                    "row.near_match_lift"):
    mv = by_id[metric_id]
    assert mv.value is None and mv.ci_low == 0.0, metric_id
    assert _status(mv) is Status.PASS, metric_id
  assert by_id["row.exact_match_rate_nonkey"].value == 0.0
  assert _status(by_id["row.exact_match_rate_nonkey"]) is Status.PASS


def test_all_null_nonkey_rows_never_match_or_duplicate():
  """R60: a row whose every non-key column is NULL carries no content —
  never an exact or near match, never an internal duplicate."""
  source = people_rows(900, 31, SOURCE_ID_BASE)
  synthetic = people_rows(700, 32, SYNTHETIC_ID_BASE)
  blank = dict.fromkeys(PEOPLE_NONKEY)
  for k in range(60):
    source[k] = {**source[k], **blank}  # inside R
    source[300 + k] = {**source[300 + k], **blank}  # inside H
    synthetic[k] = {**synthetic[k], **blank}
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, 300))
  result = _pure(table, {"source": source, "synthetic": synthetic})
  by_id = _by_id(result.metrics)
  assert by_id["row.exact_match_rate_nonkey"].value == 0.0
  assert by_id["row.memorization_lift"].value is None
  assert by_id["row.near_match_rate"].value == 0.0
  dup = by_id["row.internal_duplicate_excess"]
  assert dup.value == 0.0
  assert dup.detail["null_content_rows_synthetic"] == 60
  assert not result.flags


def _brute_rarefied(counts: Sequence[int], m: int) -> float:
  """E[D_m] summed over records by the full hypergeometric pmf: an
  independent formulation of `rarefied_duplicates`."""
  total = sum(counts)
  d = 0.0
  for c in counts:
    xs = np.arange(2, min(c, m) + 1)
    d += float((xs * hypergeom.pmf(xs, total, c, m)).sum())
  return d


def _subsample_duplicates(rows: np.ndarray, m: int, draws: int,
                          seed: int) -> np.ndarray:
  """Rows in duplicate groups of `draws` uniform m-subsets of `rows`."""
  rng = np.random.default_rng(seed)
  out = []
  for _ in range(draws):
    _, held = np.unique(
        rng.choice(rows, size=m, replace=False), return_counts=True)
    out.append(int(held[held >= 2].sum()))
  return np.array(out)


def test_rarefied_duplicates_match_the_hypergeometric_and_simulation():
  counts = [1] * 400 + [2] * 60 + [3] * 15 + [7] * 3 + [40]
  freqs = tuple(sorted(Counter(counts).items()))
  total = sum(counts)
  for m in (1, 2, 50, 300, total - 1, total, total + 5):
    assert rarefied_duplicates(freqs, m) == pytest.approx(
        _brute_rarefied(counts, min(m, total)), rel=1e-9, abs=1e-9), m
  assert rarefied_duplicates(freqs, 0) == 0.0
  assert rarefied_duplicates((), 10) == 0.0
  rows = np.repeat(np.arange(len(counts)), counts)
  simulated = _subsample_duplicates(rows, 250, 2000, seed=3)
  assert float(simulated.mean()) == pytest.approx(
      rarefied_duplicates(freqs, 250), rel=0.02)


def test_duplicate_null_variance_matches_subsampling():
  """The null variance of the duplicate count of an m-subset of the
  pooled rows (exact, via the multivariate hypergeometric) against a
  simulation, on a skewed pool, a paired pool and an all-unique pool."""
  rng = np.random.default_rng(0)
  skewed = rng.choice(
      CATALOG_PATTERNS, size=8800, p=zipf_weights(CATALOG_PATTERNS, 1.1))
  paired = np.concatenate(
      [np.arange(4000), np.repeat(np.arange(4000, 4500), 2)])
  for pool, m in ((skewed, 800), (skewed, 4400), (paired, 1000)):
    freqs = tuple(sorted(Counter(Counter(pool.tolist()).values()).items()))
    simulated = _subsample_duplicates(pool, m, 3000, seed=m)
    assert duplicate_null_variance(freqs, m) == pytest.approx(
        float(simulated.var()), rel=0.12), m
  assert duplicate_null_variance(((1, 5000),), 1000) <= 1e-9  # exactly 0
  assert duplicate_null_variance(((2, 10),), 20) == 0.0  # m = N


def test_internal_duplicates_rarefy_the_larger_side():
  """R73: both sides at matched n = min(n_src, n_syn) content rows — the
  smaller side at its observed share, the larger by exact rarefaction —
  with a design-effect Newcombe interval and the reference sample's own
  rarefied rate as the baseline (D4)."""
  source = people_rows(2000, 31, SOURCE_ID_BASE)
  synthetic = people_rows(5000, 32, SYNTHETIC_ID_BASE)
  for k in range(30):  # 30 source records repeated once: 60 rows
    source[1000 + k] = copy_content(source[1500 + k], source[1000 + k])
  for k in range(100):  # 100 synthetic records held three times: 300 rows
    for extra in (1, 2):
      at = 1000 + 3 * k + extra
      synthetic[at] = copy_content(synthetic[1000 + 3 * k], synthetic[at])
  for k in range(5):  # five duplicated pairs inside R
    source[10 + k] = copy_content(source[20 + k], source[10 + k])
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, 500))
  mv = _by_id(_pure(table, {
      "source": source,
      "synthetic": synthetic
  }).metrics)["row.internal_duplicate_excess"]
  d_syn = _brute_rarefied([3] * 100 + [1] * 4700, 2000) / 2000
  d_src = 70 / 2000  # the smaller side: its observed share
  assert mv.detail["matched_n"] == 2000
  assert mv.detail["duplicate_rows_synthetic"] == 300  # full n, observed
  assert mv.source_value == pytest.approx(d_src)
  assert mv.synthetic_value == pytest.approx(d_syn)
  assert mv.value == pytest.approx(d_syn - d_src)
  assert mv.ci_low < mv.value < mv.ci_high
  # the pooled null's effective n never exceeds m: never narrower than an
  # independent-rows interval
  naive = noise.newcombe_diff_interval(
      round(d_syn * 2000), 2000, round(d_src * 2000), 2000)
  assert mv.ci_high - mv.ci_low >= naive[1] - naive[0] - 1e-12
  assert mv.detail["null_sd_share"] > 0.0
  # D4: R (500 rows, five pairs) against the source rarefied to 500
  src_counts = [2] * 35 + [1] * 1930
  assert mv.baseline_value == pytest.approx(10 / 500 -
                                            _brute_rarefied(src_counts, 500) /
                                            500)
  assert mv.n_source == 2000 and mv.n_synthetic == 5000


def test_faithful_generator_with_ten_times_the_rows_passes_duplicates():
  """A generator drawing from the source's own skewed distribution, with
  10x the rows, holds far more duplicates at full n (the old full-n excess
  FAILed at ~0.37); at matched n it passes."""
  weights = zipf_weights(CATALOG_PATTERNS, 1.1)
  for seed in range(4):
    rng = np.random.default_rng(40 + seed)
    source = rng.choice(CATALOG_PATTERNS, size=800, p=weights)
    synthetic = rng.choice(CATALOG_PATTERNS, size=8000, p=weights)
    table, rows = catalog_table(source, synthetic, n_reference=300)
    mv = _by_id(_pure(table, rows).metrics)["row.internal_duplicate_excess"]
    full_n = (
        mv.detail["duplicate_rows_synthetic"] / 8000 -
        mv.detail["duplicate_rows_source"] / 800)
    assert full_n >= 0.05, seed  # the old full-n excess FAILed
    assert mv.detail["matched_n"] == 800
    assert mv.ci_low <= 0.0 <= mv.ci_high, (seed, mv)
    assert _status(mv) is Status.PASS, (seed, mv)


def test_planted_duplicates_fail_at_matched_n():
  weights = zipf_weights(CATALOG_PATTERNS, 1.1)
  rng = np.random.default_rng(9)
  source = rng.choice(CATALOG_PATTERNS, size=800, p=weights)
  synthetic = rng.choice(CATALOG_PATTERNS, size=8000, p=weights)
  synthetic[:3200] = np.repeat(synthetic[3200:3400], 16)  # a small pool
  table, rows = catalog_table(source, synthetic, n_reference=300)
  mv = _by_id(_pure(table, rows).metrics)["row.internal_duplicate_excess"]
  assert mv.value >= 0.05 and mv.ci_low > 0.0
  assert _status(mv) is Status.FAIL


def test_panel_size_guard_skips_near_matching_with_a_reason():
  table, rows, _ = _people_with(near_copies=5)
  by_id = _by_id(_pure(table, rows, panel_max_bytes=8192).metrics)
  for metric_id in ("row.near_match_rate", "row.near_match_lift"):
    assert "MB" in by_id[metric_id].detail["reason"], metric_id
  assert by_id["row.memorization_lift"].ci_low is not None
  tiny = _by_id(_pure(table, rows, panel_max_bytes=64).metrics)
  assert "MB" in tiny["row.memorization_lift"].detail["reason"]
  assert tiny["row.exact_match_rate"].value is not None


def test_flags_are_bounded_ranked_and_deterministic():
  table, rows, _ = _people_with(copies=40, near_copies=20, e_n=100)
  first = _pure(table, rows, row_flags_top_k=7).flags
  again = _pure(table, rows, row_flags_top_k=7).flags
  assert first == again
  for check in (EXACT_COPY, NEAR_COPY):
    ranked = [f for f in first if f.check == check]
    assert [f.rank for f in ranked] == list(range(1, 8))
    assert ranked[0].source_set == "E"  # the prompt-exposed copies first
  # batch boundaries do not change the chosen flags
  rechunked = membership_outputs(
      table,
      encode_all(table, rows, chunk=311),
      salt=SALT,
      label_key=LABEL_KEY,
      row_flags_top_k=7).flags
  assert rechunked == first


def _row_key_outputs(fn: RowKeysFn, batches: Sequence[Any]) -> list[tuple]:
  """RowKeysFn's main outputs over one bundle (tagged outputs dropped)."""
  fn.start_bundle()
  out = [
      o for b in batches for o in fn.process(b)
      if not isinstance(o, TaggedOutput)
  ]
  out += [wv.value for wv in fn.finish_bundle()]
  return out


def test_keyed_counts_do_not_depend_on_buckets_or_compaction(monkeypatch):
  table, rows, _ = _people_with(copies=20, near_copies=5)
  baseline = _pure(table, rows)
  monkeypatch.setattr(membership, "_bucket_bits", lambda _table: 5)
  monkeypatch.setattr(membership, "_COMPACT_CODES", 64)
  assert _pure(table, rows) == baseline
  # the keyed-count combine over packed parts: split and merged in any
  # order, the same exact counts; every code in its own bucket
  spec = MembershipSpec.from_table(table, salt=SALT)
  assert spec.bucket_bits == 5
  parts: dict[tuple, list] = {}
  for key, part in _row_key_outputs(
      RowKeysFn({table.name: spec}, flush_codes=64), encode_all(table, rows)):
    parts.setdefault(key, []).append(part)
  assert len({key[2] for key in parts}) > 1
  fn = KeyCountsCombineFn()
  content_rows = 0
  for (_, kind, bucket), items in parts.items():
    whole = fn.extract_output(
        functools.reduce(fn.add_input, items, fn.create_accumulator()))
    halves = [
        fn.compact(
            functools.reduce(fn.add_input, items[k::2],
                             fn.create_accumulator())) for k in (1, 0)
    ]
    merged = fn.extract_output(fn.merge_accumulators(halves))
    for name in ("src_codes", "src_counts", "syn_codes", "syn_counts"):
      assert np.array_equal(getattr(whole, name), getattr(merged, name))
    for codes in (whole.src_codes, whole.syn_codes):
      assert set((codes >> np.uint64(59)).tolist()) <= {bucket}
    if kind == "nk":
      content_rows += int(whole.src_counts.sum() + whole.syn_counts.sum())
  assert content_rows == len(rows["source"]) + len(rows["synthetic"])


def test_keyed_count_shuffle_stays_within_the_budgeted_bytes_per_code(
    record_property):
  """Packed sparse per-side parts, merged per bundle: the bytes a code
  costs on the shuffle (FastPrimitivesCoder, as Beam encodes them) stay
  within `budget.MEMBERSHIP_CODE_BYTES` at every bucket count."""
  table, rows = people_table(n_source=3000, n_synthetic=16384, n_reference=600)
  spec = MembershipSpec.from_table(table, salt=SALT)
  batches = encode(table, "synthetic", rows["synthetic"], chunk=4096)
  codes_in = sum(3 * b.n for b in batches)  # nk, pk and identity per row
  coder = coders.FastPrimitivesCoder()
  combine = KeyCountsCombineFn()
  for bits in (0, 4, 6):  # >= 256 codes a bucket in this bundle
    fn = RowKeysFn({table.name: dataclasses.replace(spec, bucket_bits=bits)})
    out = _row_key_outputs(fn, batches)
    emitted = sum(len(coder.encode(element)) for element in out)
    by_key: dict[tuple, list] = {}
    for key, part in out:
      by_key.setdefault(key, []).append(part)
    compacted = sum(
        len(coder.encode((key, combine.compact(list(items)))))
        for key, items in by_key.items())
    record_property(f"bytes_per_code_bits_{bits}", round(emitted / codes_in, 2))
    assert emitted / codes_in <= MEMBERSHIP_CODE_BYTES, bits
    assert compacted / codes_in <= MEMBERSHIP_CODE_BYTES, bits
  # the corners (B5): a lone batch at the 10-bit maximum, and a long table
  # name, stay within the budgeted MEMBERSHIP_CODE_BYTES + len(name)
  for name in (table.name, "project_dataset_" + "x" * 40):
    corner = dataclasses.replace(spec, table=name, bucket_bits=10)
    lone = [dataclasses.replace(batches[0], table=name)]
    out = _row_key_outputs(RowKeysFn({name: corner}), lone)
    per_code = sum(len(coder.encode(e)) for e in out) / (3 * lone[0].n)
    record_property(f"bytes_per_code_corner_{len(name)}", round(per_code, 2))
    assert per_code <= membership_code_bytes(name), name


def test_budget_counts_the_keyed_mode_row_hashes():
  both = 3e7 + 3e7
  args: dict[str, Any] = {
      "rows_source": 3e7,
      "rows_synthetic": 3e7,
      "edges": 0,
      "keyed_counts": 1,
      "table": "orders",
  }
  per_code = membership_code_bytes("orders")
  side = fixed_shuffle_bytes(**args, nonkey=True, keyed=True, side_input=True)
  keyed = fixed_shuffle_bytes(**args, nonkey=True, keyed=True, side_input=False)
  assert keyed - side == pytest.approx(both * per_code)
  keyless = fixed_shuffle_bytes(
      **args, nonkey=True, keyed=False, side_input=False)
  assert keyless == pytest.approx(side)  # the row IS the content
  # B5/R79: every element repeats the key's table name, in UTF-8 bytes
  assert per_code == MEMBERSHIP_CODE_BYTES + 6
  assert membership_code_bytes("orders_ñ") == MEMBERSHIP_CODE_BYTES + 9
  with pytest.raises(TypeError, match="table"):
    fixed_shuffle_bytes(  # type: ignore[call-arg]  # table is required
        rows_source=1.0,
        rows_synthetic=1.0,
        edges=0,
        keyed_counts=0)
  # the switch: 8 B a source row and array within 160 MB
  assert source_sets_fit(2e7, nonkey=True, keyed=False)
  assert not source_sets_fit(2e7, nonkey=True, keyed=True)
  assert source_sets_fit(1e7, nonkey=True, keyed=True)
  assert not source_sets_fit(None, nonkey=True, keyed=True)


def test_keyless_table_flags_name_the_record_by_its_content():
  """No key at all: the full row IS the content, and a flag names the
  copied record by a keyed label of its row hash (no synthetic key)."""
  source = people_rows(900, 31, SOURCE_ID_BASE)
  synthetic = people_rows(700, 32, SYNTHETIC_ID_BASE)
  for k in range(12):
    synthetic[k] = dict(source[k])
  table = planned(
      "people", PEOPLE_FIELDS, source, synthetic, panel=panel_of(source, 300))
  result = _pure(table, {"source": source, "synthetic": synthetic})
  by_id = _by_id(result.metrics)
  assert by_id["row.exact_match_rate"].value == pytest.approx(12 / 700)
  assert by_id["row.exact_match_rate_nonkey"].value == pytest.approx(12 / 700)
  assert by_id["table.pk_duplicate_rate"].value is None
  flags = [f for f in result.flags if f.check == EXACT_COPY]
  assert len(flags) == 12
  donors = BatchEncoder.from_table(
      table, "reference", salt=SALT).encode(source[:12]).row_hash
  assert {f.source_key_hash for f in flags} == {
      hashed_label(int(code), key=LABEL_KEY) for code in donors.tolist()
  }
  for flag in flags:
    assert flag.synthetic_key is None and flag.detail["full_row"] is True
  _flag_values_absent(flags, {"source": source}, PEOPLE_FIELDS_NAMES)


def test_keyless_source_copies_are_labelled_with_the_record_hash():
  """Copies of source rows OUTSIDE the panel in a key-less table: the flag
  names the copied record by the keyed label of its row hash."""
  source = people_rows(900, 31, SOURCE_ID_BASE)
  synthetic = people_rows(700, 32, SYNTHETIC_ID_BASE)
  for k in range(5):
    synthetic[k] = dict(source[800 + k])  # outside R (0..299), H (300..599)
  table = planned(
      "people", PEOPLE_FIELDS, source, synthetic, panel=panel_of(source, 300))
  flags = _pure(table, {"source": source, "synthetic": synthetic}).flags
  donors = BatchEncoder.from_table(
      table, "source", salt=SALT).encode(source[800:805]).row_hash
  assert [f.source_set for f in flags] == ["source"] * 5
  assert {f.source_key_hash for f in flags} == {
      hashed_label(int(code), key=LABEL_KEY) for code in donors.tolist()
  }


@pytest.mark.parametrize("heavy_donor", range(6))
def test_keyless_repeated_copies_take_one_flag_slot(heavy_donor):
  """One R record copied 200 times plus 20 other copies: the top-10 flags
  name 10 distinct records (candidates are distinct by synthetic row hash
  and source code)."""
  source = people_rows(900, 31, SOURCE_ID_BASE)
  synthetic = people_rows(700, 32, SYNTHETIC_ID_BASE)
  for k in range(200):
    synthetic[k] = dict(source[heavy_donor])
  for k in range(20):
    synthetic[200 + k] = dict(source[10 + k])
  table = planned(
      "people", PEOPLE_FIELDS, source, synthetic, panel=panel_of(source, 300))
  flags = _pure(
      table, {
          "source": source,
          "synthetic": synthetic
      }, row_flags_top_k=10).flags
  assert len(flags) == 10
  assert len({f.source_key_hash for f in flags}) == 10


def test_identity_only_table_flags_carry_the_identity_handle():
  source = people_rows(900, 31, SOURCE_ID_BASE)
  synthetic = people_rows(700, 32, SYNTHETIC_ID_BASE)
  for k in range(6):
    synthetic[k] = copy_content(source[k], synthetic[k])
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, 300))
  flags = _pure(table, {"source": source, "synthetic": synthetic}).flags
  assert len(flags) == 6
  by_handle = {f.synthetic_key["email"]: f for f in flags}
  for k in range(6):
    flag = by_handle[synthetic[k]["email"]]
    assert flag.source_key_hash == hashed_label(
        key_hash([source[k]["email"]]), key=LABEL_KEY)


def test_all_key_table_copies_are_flagged():
  """Every column a key: the key tuple is the record, so its copies are
  flagged (the content metrics are not evaluated)."""
  fields = PEOPLE_FIELDS[:2]

  def keys_of(rows: Sequence[dict]) -> list[dict]:
    return [{"person_id": r["person_id"], "email": r["email"]} for r in rows]

  source = keys_of(people_rows(900, 31, SOURCE_ID_BASE))
  synthetic = keys_of(people_rows(700, 32, SYNTHETIC_ID_BASE))
  for k in range(5):
    synthetic[k] = dict(source[k])  # inside R
  for k in range(3):
    synthetic[10 + k] = dict(source[800 + k])  # outside the panel
  table = planned(
      "links",
      fields,
      source,
      synthetic,
      pk=("person_id",),
      identity=("email",),
      panel=panel_of(source, 300))
  result = _pure(table, {"source": source, "synthetic": synthetic})
  by_id = _by_id(result.metrics)
  assert by_id["row.exact_match_rate"].value == pytest.approx(8 / 700)
  assert "non-key" in by_id["row.memorization_lift"].detail["reason"]
  sets = Counter(f.source_set for f in result.flags)
  assert sets["source"] == 3 and sets["E"] + sets["R"] == 5
  # a full-row copy's synthetic key IS the copied source key (documented)
  assert {f.synthetic_key["person_id"] for f in result.flags
         } == {r["person_id"] for r in [*source[:5], *source[800:803]]}


def test_exact_matches_are_never_near_and_common_records_are_not_e():
  """A row copying an H record exactly is not a near match of the R
  record one field away; a record R and H both hold is flagged R, not E."""
  source = people_rows(1500, 31, SOURCE_ID_BASE)
  synthetic = people_rows(1200, 32, SYNTHETIC_ID_BASE)
  # H row 400 = R row 3 with the city changed; synthetic 0 copies it
  source[400] = {**copy_content(source[3], source[400]), "city": "Twin001"}
  synthetic[0] = copy_content(source[400], synthetic[0])
  # R row 7's content is also an H row: common, so its copy is R
  source[450] = copy_content(source[7], source[450])
  synthetic[1] = copy_content(source[7], synthetic[1])
  table = planned(
      "people",
      PEOPLE_FIELDS,
      source,
      synthetic,
      pk=("person_id",),
      identity=PEOPLE_IDENTITY,
      panel=panel_of(source, 300, e_n=100))
  result = _pure(table, {"source": source, "synthetic": synthetic})
  by_id = _by_id(result.metrics)
  assert by_id["row.near_match_rate"].value == 0.0
  assert by_id["row.near_match_rate"].detail["near_rows_h"] == 0
  sets = {f.synthetic_key["person_id"]: f.source_set for f in result.flags}
  assert sets[synthetic[0]["person_id"]] == "H"
  assert sets[synthetic[1]["person_id"]] == "R"
  assert not [f for f in result.flags if f.check == NEAR_COPY]


def test_unexposed_lift_with_an_empty_set_is_explained_not_a_crash():
  """(R minus H) minus E empty while (H minus R) minus H_E is hit: the
  secondary lift in `detail.unexposed` says why instead of dividing by
  zero (the review's probe)."""
  n = 1200
  r = [k % 100 for k in range(1024)] + [k % 100 for k in range(n - 1024)]
  h = [100 + k % 100 for k in range(1024)
      ] + [200 + k % 10 for k in range(n - 1024)]
  source = r + h + [5000 + k for k in range(300)]
  synthetic = [200, 201, 7000, 7001]
  table, rows = catalog_table(source, synthetic, n_reference=n)
  by_id = _by_id(_pure(table, rows).metrics)
  unexposed = by_id["row.exposure_lift"].detail["unexposed"]
  assert unexposed["exposed_r"] == 0 and unexposed["events_h"] == 2
  assert unexposed["lift"] is None and "empty" in unexposed["reason"]
  assert by_id["row.memorization_lift"].ci_low is not None


def test_sampled_mode_withholds_verdicts_that_need_every_row():
  """R72: a row-sampled side cannot support full-coverage verdicts (not
  evaluated, the observed lower bound in detail); panel-based metrics stay
  evaluated, marked as sampled."""
  table, rows, _ = _people_with(copies=20)
  src = _by_id(
      _pure(dataclasses.replace(table, sample_rate_source=0.5), rows).metrics)
  for metric_id in ("row.exact_match_rate", "row.exact_match_rate_nonkey",
                    "row.internal_duplicate_excess"):
    reason = src[metric_id].detail["reason"]
    assert src[metric_id].value is None, metric_id
    assert reason.startswith("sampled mode cannot measure"), metric_id
    assert reason.endswith("run exact mode"), metric_id
  assert src["row.exact_match_rate_nonkey"].detail["matches_lower_bound"] == 20
  assert src["table.pk_duplicate_rate"].value == 0.0
  assert src["row.memorization_lift"].method is Method.EXACT
  syn = _by_id(
      _pure(dataclasses.replace(table, sample_rate_synthetic=0.25),
            rows).metrics)
  for metric_id in ("table.pk_duplicate_rate", "table.identity_duplicate_rate",
                    "row.internal_duplicate_excess"):
    assert syn[metric_id].value is None, metric_id
    assert "run exact mode" in syn[metric_id].detail["reason"], metric_id
  assert syn["table.pk_duplicate_rate"].detail[
      "duplicate_rows_lower_bound"] == 0
  for metric_id in ("row.memorization_lift", "row.near_match_rate",
                    "row.exact_match_rate"):
    mv = syn[metric_id]
    assert mv.method is Method.SAMPLE and mv.sample_rate == 0.25, metric_id
    assert mv.detail["sample_rate"] == 0.25, metric_id


def test_a_table_failing_on_a_worker_is_not_evaluated(monkeypatch):
  """A data error while a table's metrics are computed makes that table
  not_evaluated with the reason; nothing raises."""
  table, rows, _ = _people_with(copies=6)

  def broken(*_args: Any, **_kwargs: Any) -> None:
    raise ZeroDivisionError("division by zero")

  monkeypatch.setattr(membership, "_lifts", broken)
  result = _pure(table, rows)
  assert not result.flags
  assert {mv.metric_id for mv in result.metrics} == set(OWNED_METRIC_IDS)
  for mv in result.metrics:
    assert mv.value is None
    assert "ZeroDivisionError" in mv.detail["reason"]


def _out_of_memory(*_args: Any, **_kwargs: Any) -> None:
  raise MemoryError("Unable to allocate 3.2 GiB for an array")


def _malformed(*_args: Any, **_kwargs: Any) -> None:
  raise ValueError("a malformed batch (test)")


@pytest.mark.parametrize("step",
                         ["key_counts", "batch_membership", "source_hashes"])
def test_a_worker_memory_error_fails_the_bundle(monkeypatch, step):
  """The final review's I3: running out of memory is the worker's state,
  not the table's. On a worker it is raised — the bundle fails and the
  runner retries it — where it used to become a permanent not_evaluated
  block that left the run SUCCEEDED. A data error in the same place
  still makes the table not_evaluated."""
  table, rows, _ = _people_with(copies=6, n_source=400, n_synthetic=300)
  spec = MembershipSpec.from_table(table, salt=SALT)
  batches = encode_all(table, rows)
  synthetic = next(b for b in batches if b.side is Side.SYNTHETIC)
  source = next(b for b in batches if b.side is Side.SOURCE)
  refs = PanelRefs.from_table(table, spec)

  def run() -> list[Any]:
    if step == "key_counts":
      fn = membership.RowKeysFn({table.name: spec})
      fn.start_bundle()
      return list(fn.process(synthetic))
    if step == "batch_membership":
      return list(membership.MembershipFn(spec, 10).process(synthetic, refs))
    return list(membership._SourceHashesFn(spec).process(source))  # pylint: disable=protected-access  # the DoFn under test

  monkeypatch.setattr(membership, step, _malformed)
  [tagged] = run()
  assert tagged.tag == "failed" and "malformed" in tagged.value[1]
  monkeypatch.setattr(membership, step, _out_of_memory)
  with pytest.raises(MemoryError):
    run()


def test_memory_errors_split_by_where_they_happen(monkeypatch):
  """In process, as in Beam: a memory error while the emit step computes
  a table's rows is raised (a worker's), and one while the driver builds
  the table's panel index makes the table not_evaluated (no retry can
  change what the driver holds)."""
  table, rows, _ = _people_with(copies=6, n_source=400, n_synthetic=300)
  assert MemoryError in membership.TABLE_ERRORS
  assert set(
      membership.WORKER_ERRORS) == set(membership.TABLE_ERRORS) - {MemoryError}
  with monkeypatch.context() as patch:
    patch.setattr(membership, "_lifts", _out_of_memory)
    with pytest.raises(MemoryError):
      _pure(table, rows)
  monkeypatch.setattr(membership.PanelIndex, "build",
                      staticmethod(_out_of_memory))
  result = _pure(table, rows)
  assert {mv.metric_id for mv in result.metrics} == set(OWNED_METRIC_IDS)
  assert all(mv.value is None and "MemoryError" in mv.detail["reason"]
             for mv in result.metrics)


def test_a_failing_table_does_not_fail_the_run(tmp_path, monkeypatch):
  """Three tables in one pipeline: one fails on the driver (a panel row
  the encoder rejects), one while its batches are counted on a worker;
  both become not_evaluated rows with a reason and write no flags, while
  the third matches the in-process path."""
  good, good_rows, _ = _people_with(copies=6)
  rng = np.random.default_rng(1)
  bad, bad_rows = catalog_table(
      rng.choice(CATALOG_PATTERNS, 600), rng.choice(CATALOG_PATTERNS, 400), 200)
  assert bad.panel is not None
  bad = dataclasses.replace(
      bad,
      panel=Panel(
          r_rows=[{
              k: v for k, v in row.items() if k != "brand"
          } for row in bad.panel.r_rows],
          h_rows=bad.panel.h_rows,
          e_n=bad.panel.e_n,
          he_n=bad.panel.he_n,
          digest="d" * 64,
          verified=True,
          expected_digest="d" * 64))
  worker, worker_rows = catalog_table(
      rng.choice(CATALOG_PATTERNS, 600), rng.choice(CATALOG_PATTERNS, 400), 200)
  worker = dataclasses.replace(
      worker, name="catalog_w", landing_table=f"{worker.landing_table}_w")
  real_key_counts = membership.key_counts

  def failing(spec: Any, batch: Any) -> Any:
    if spec.table == "catalog_w":
      raise ValueError("a malformed batch (test)")
    return real_key_counts(spec, batch)

  monkeypatch.setattr(membership, "key_counts", failing)
  tables = {"people": good, "catalog": bad, "catalog_w": worker}
  rows_by = {"people": good_rows, "catalog": bad_rows, "catalog_w": worker_rows}
  sources = InMemorySources({
      (name, side): r for name, by in rows_by.items() for side, r in by.items()
  })
  with BeamTestPipeline() as p:
    batches = [
        sources.read(p, tables[name], side)
        | EncodeSide(tables[name], side, salt=SALT)
        for name, by_side in rows_by.items()
        for side in by_side
    ] | "Flatten" >> beam.Flatten()
    key = p | "Key" >> beam.Create([LABEL_KEY])
    out = batches | "Membership" >> Membership(
        list(tables.values()), salt=SALT, label_key=key)
    _collect(out["metrics"], tmp_path / "metrics.pkl", "Metrics")
    _collect(out["flags"], tmp_path / "flags.pkl", "Flags")
  metrics = pickle.loads((tmp_path / "metrics.pkl").read_bytes())
  flags = pickle.loads((tmp_path / "flags.pkl").read_bytes())
  for name, cause in (("catalog", "ValueError"), ("catalog_w", "malformed")):
    rows_of = [mv for mv in metrics if mv.table == name]
    assert {mv.metric_id for mv in rows_of} == set(OWNED_METRIC_IDS), name
    for mv in rows_of:
      assert mv.value is None and cause in mv.detail["reason"], (name, mv)
      assert mv.encoding_plan_digest == tables[name].encoding_plan_digest
  assert not [f for f in flags if f.table != "people"]
  monkeypatch.setattr(membership, "key_counts", real_key_counts)
  pure = _pure(good, good_rows)
  assert sorted([mv for mv in metrics if mv.table == "people"],
                key=lambda mv: mv.metric_id) == sorted(
                    pure.metrics, key=lambda mv: mv.metric_id)
  assert sorted([f for f in flags if f.table == "people"],
                key=lambda f: (f.check, f.rank)) == sorted(
                    pure.flags, key=lambda f: (f.check, f.rank))


def _freqs_of(values: np.ndarray) -> tuple[tuple[int, int], ...]:
  """The frequency of frequencies of `values`' distinct items."""
  return tuple(sorted(Counter(Counter(values.tolist()).values()).items()))


def _pooled(*freqs: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
  total: Counter = Counter()
  for f in freqs:
    total.update(dict(f))
  return tuple(sorted(total.items()))


def _excess_status(x: Any) -> Status:
  return _status(
      MetricValue(
          metric_id="row.internal_duplicate_excess",
          table="t",
          value=x.value,
          ci_low=x.ci_low,
          ci_high=x.ci_high))


def _unique_but_pairs(n: int, pairs: int) -> tuple[tuple[int, int], ...]:
  return tuple(x for x in ((1, n - 2 * pairs), (2, pairs)) if x[1])


def test_duplicate_excess_verdicts_are_monotone_in_source_pairs():
  """R76: a 10n-row synthetic side repeating every record 2 or 3 times
  FAILs against a near-unique source, whatever its handful of duplicate
  pairs (one design effect from the pooled null; a per-side one read a
  source with 1-2 pairs as a tiny sample and passed the excess)."""
  for n in (400, 800):
    for repeats in (2, 3):
      values = []
      for pairs in (0, 1, 2, 5):
        freqs_src = _unique_but_pairs(n, pairs)
        freqs_syn = ((repeats, 10 * n // repeats),)
        x = duplicate_excess(freqs_src, freqs_syn,
                             _pooled(freqs_src, freqs_syn))
        assert x.ci_low <= x.value <= x.ci_high  # B2: inside its own CI
        assert x.ci_low > 0.0 and _excess_status(x) is Status.FAIL, (n, repeats,
                                                                     pairs)
        values.append(x.value)
      assert values == sorted(values, reverse=True), (n, repeats)
  # the same through the pipeline's keyed counts
  for pairs in (0, 2):
    rng = np.random.default_rng(3)
    perm = rng.permutation(CATALOG_PATTERNS)
    source = perm[:400].copy()
    source[400 - pairs:] = source[:pairs]
    synthetic = np.repeat(perm[400:2400], 2)
    table, rows = catalog_table(source, synthetic, n_reference=200)
    mv = _by_id(_pure(table, rows).metrics)["row.internal_duplicate_excess"]
    assert mv.ci_low <= mv.value <= mv.ci_high
    assert _status(mv) is Status.FAIL, pairs


@pytest.mark.slow
def test_duplicate_excess_null_miss_rate_stays_nominal():
  """Both sides drawn from one Zipf (200 seeds a setting): the interval
  misses 0 at most at its nominal 5 %, and always holds the value."""
  for k, n_src, n_syn in ((1000, 800, 8000), (10000, 800, 800), (100000, 8000,
                                                                 800)):
    weights = zipf_weights(k, 1.1)
    misses = 0
    for seed in range(200):
      rng = np.random.default_rng(seed)
      src = rng.choice(k, n_src, p=weights)
      syn = rng.choice(k, n_syn, p=weights)
      x = duplicate_excess(
          _freqs_of(src), _freqs_of(syn), _freqs_of(np.concatenate([src, syn])))
      assert x.ci_low <= x.value <= x.ci_high
      misses += not x.ci_low <= 0.0 <= x.ci_high
    assert misses / 200 <= 0.05, (k, n_src, n_syn, misses)


def test_duplicate_null_variance_stays_exact_at_millions_of_rows():
  """Exact log-ratio sums, not log-gamma differences: an all-distinct pool
  has variance 0 at any size, and a half-paired pool scales exactly."""
  for total in (20_000, 20_000_000):
    m = total // 2
    assert duplicate_null_variance(((1, total),), m) <= 1e-6 * m
  small = duplicate_null_variance(((1, 10_000), (2, 5_000)), 10_000)
  large = duplicate_null_variance(((1, 10_000_000), (2, 5_000_000)), 10_000_000)
  assert large / 20_000_000 == pytest.approx(small / 20_000, rel=1e-3)


def _exact_null_variance(freqs: Sequence[tuple[int, int]], m: int) -> Fraction:
  """Var(F1) of an m-subset of the pool, in exact rational arithmetic
  (math.comb): the reference `duplicate_null_variance` is held to."""
  total = sum(c * f for c, f in freqs)
  every = math.comb(total, m)

  def p_one(c: int) -> Fraction:
    rest = total - c
    return Fraction(c * math.comb(rest, m - 1),
                    every) if rest >= m - 1 else (Fraction(0))

  def pair(s: int) -> Fraction:
    rest = total - s
    return Fraction(math.comb(rest, m -
                              2), every) if rest >= m - 2 else (Fraction(0))

  a = [p_one(c) for c, _ in freqs]
  variance = sum(
      (f * a_i * (1 - a_i) for (_, f), a_i in zip(freqs, a, strict=True)),
      Fraction(0))
  for i, ((c_i, f_i), a_i) in enumerate(zip(freqs, a, strict=True)):
    for j, ((c_j, f_j), a_j) in enumerate(zip(freqs, a, strict=True)):
      pairs = f_i * (f_j - (i == j))
      if pairs:
        variance += pairs * (c_i * c_j * pair(c_i + c_j) - a_i * a_j)
  return variance


# (pool, m, Var(F1)) with records held 2.1-3 million times at N ≈ 1e9: the
# reviewer's R79 cases. The references are `_exact_null_variance`'s values
# (Fraction/comb, 1-21 s each), at 13 significant digits; the fastest is
# recomputed live below.
_PAST_THE_CAP = (
    (((1, 900_000_000), (2, 45_000_000), (2_200_000, 1)), 10_000,
     22.14176348560),
    (((1, 900_000_000), (2, 45_000_000), (2_100_000, 1)), 10_000,
     21.14041211859),
    (((1, 1_900_000_000), (2, 40_000_000), (3, 1_000_000), (3_000_000, 3)),
     20_000, 89.96946402206),
    (((1, 900_000_000), (5, 1_000_000), (2_500_000, 4)), 3_000, 32.56568956400),
)


@pytest.mark.parametrize("freqs, m, exact", _PAST_THE_CAP)
def test_duplicate_null_variance_is_exact_past_the_table_cap(freqs, m, exact):
  """R79: pair sums past the dense table are walked on from the same exact
  running sum (never a whole-matrix switch to log-gamma values, which was
  6.9-13x off here)."""
  assert 2 * max(c for c, _ in freqs) > membership.PAIR_TABLE_CAP
  assert duplicate_null_variance(freqs, m) == pytest.approx(exact, rel=1e-9)


def test_exact_null_variance_reference_is_recomputed_live():
  freqs, m, exact = _PAST_THE_CAP[3]
  assert float(_exact_null_variance(freqs, m)) == pytest.approx(
      exact, rel=1e-11)
  small = ((1, 400), (2, 60), (3, 15), (7, 3), (40, 1))
  assert duplicate_null_variance(small, 250) == pytest.approx(
      float(_exact_null_variance(small, 250)), rel=1e-12)


@pytest.mark.slow
def test_duplicate_null_variance_memory_is_bounded():
  """12,000 count classes (B3): the pair sum runs in row blocks over a
  tabulated scale, so memory stays bounded; the value matches the
  rare-duplication closed form Var(D) ≈ 4·E[pairs]."""
  pool = tuple((c, 50) for c in range(1, 12_001))
  total = sum(c * f for c, f in pool)
  tracemalloc.start()
  start = clock.perf_counter()
  variance = duplicate_null_variance(pool, 1000)
  elapsed = clock.perf_counter() - start
  _, peak = tracemalloc.get_traced_memory()
  tracemalloc.stop()
  assert peak < 128 * 2**20 and elapsed < 60
  pairs = 1000 * 999 / 2 * sum(f * c * (c - 1) for c, f in pool) / (
      total * (total - 1))
  assert variance == pytest.approx(4 * pairs, rel=0.1)


def test_an_emit_failure_drops_the_tables_flags_in_beam(tmp_path, monkeypatch):
  """B4: a table whose metrics fail at emit time writes not_evaluated
  rows and no flags on the Beam path, as in process."""
  table, rows, _ = _people_with(copies=6)

  def broken(*_args: Any, **_kwargs: Any) -> None:
    raise ZeroDivisionError("division by zero")

  monkeypatch.setattr(membership, "_lifts", broken)
  metrics, flags = _run(table, rows, tmp_path)
  assert not flags
  assert {mv.metric_id for mv in metrics} == set(OWNED_METRIC_IDS)
  assert all("ZeroDivisionError" in mv.detail["reason"] for mv in metrics)


def test_label_key_never_enters_the_job_graph(tmp_path):
  table, rows, _ = _people_with(copies=6)
  operator_key = b"operator-secret-key-material-0042"
  key_file = tmp_path / "label.key"
  key_file.write_bytes(operator_key)
  sources = InMemorySources({
      ("people", side): rows[side] for side in ("source", "synthetic")
  })
  pipeline = BeamTestPipeline()
  with pipeline as p:
    batches = [
        sources.read(p, table, side) | EncodeSide(table, side, salt=SALT)
        for side in rows
    ] | "Flatten" >> beam.Flatten()
    key = p | "LabelKey" >> LabelKey(str(key_file), reader=_key_file_reader)
    out = batches | "Membership" >> Membership(
        [table], salt=SALT, label_key=key)
    _collect(out["flags"], tmp_path / "flags.pkl", "Flags")
  flags = pickle.loads((tmp_path / "flags.pkl").read_bytes())
  assert flags and all(
      f.source_key_hash is None or f.source_key_hash.startswith("h:")
      for f in flags)
  labelled = next(f for f in flags if f.source_set != "source")
  assert labelled.source_key_hash in {
      hashed_label(key_hash([r["person_id"]]), key=operator_key)
      for r in rows["source"]
  }
  for blob in _pickled_payloads(pipeline):
    assert operator_key not in blob
    assert operator_key.hex().encode() not in blob
  with pytest.raises(TypeError, match="PCollection"):
    Membership([table], salt=SALT,
               label_key=LABEL_KEY)  # type: ignore[arg-type]


def _key_file_reader(uri: str) -> bytes:
  """A fake operator-key reader: the key lives in a local file, so only
  its path is in the graph."""
  return Path(uri).read_bytes()


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
    if not blob:  # CombineGlobally's KeyWithVoid: a URN, no pickled payload
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


def test_throughput_batch_membership(record_property):
  table, rows, _ = _people_with(
      copies=50,
      near_copies=50,
      n_source=9000,
      n_synthetic=8192,
      n_reference=2000)
  spec = MembershipSpec.from_table(table, salt=SALT)
  index = PanelIndex.build(spec, PanelRefs.from_table(table, spec))
  batch = encode(table, "synthetic", rows["synthetic"], chunk=8192)[0]
  batch_membership(spec, index, None, batch, top_k=100)  # warm-up
  start = clock.perf_counter()
  part, _ = batch_membership(spec, index, None, batch, top_k=100)
  elapsed = clock.perf_counter() - start
  record_property("membership_rows_per_s", round(batch.n / elapsed))
  assert part.near_rows_r == 50 and part.rows_r_only == 50
  assert elapsed < 30
  assert math.isfinite(elapsed)
