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
"""Unit tests for `scripts/deployment_prerequisites.py` — step 9 (RAG chunk
store, WS2) and the ddl-optional messaging.

Loaded via importlib (the script is not a package module). Offline-only: the
BigQuery surface is a fake injected through the module's `bq_client`
indirection — no GCP credentials, no network.

Design: docs/DESIGN.md §5 Fidelity; §11 Evaluation (sdfb-evaluation)
(ADR 0020, 0041).
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,unused-argument

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[5] / "scripts" / "deployment_prerequisites.py"
_spec = importlib.util.spec_from_file_location("deployment_prerequisites",
                                               _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

_REPO_ROOT = Path(__file__).parents[5]


class _NotFoundError(Exception):
  pass


class _FakeBQ:
  """Just enough of google.cloud.bigquery.Client for step 9."""

  def __init__(self, *, datasets=(), tables=None, index_rows=()):
    self._datasets = set(datasets)
    self._tables = tables or {}
    self._index_rows = list(index_rows)

  def get_dataset(self, ref):
    if ref not in self._datasets:
      raise _NotFoundError(ref)
    return SimpleNamespace(dataset_id=ref)

  def get_table(self, fqn):
    if fqn not in self._tables:
      raise _NotFoundError(fqn)
    return self._tables[fqn]

  def query(self, sql):
    rows = self._index_rows
    return SimpleNamespace(result=lambda: rows)


def _rag_table(columns, partition_field="created_at"):
  return SimpleNamespace(
      schema=[SimpleNamespace(name=c) for c in columns],
      time_partitioning=(SimpleNamespace(
          field=partition_field) if partition_field else None),
  )


def _ctx(fqn, fake):
  args = argparse.Namespace(
      project="p",
      rag_chunks_table=fqn,
      schemas_dir=str(_REPO_ROOT / "config" / "bq_schema"),
  )
  ctx = _mod.Ctx(args=args)
  return ctx


def _patch_bq(monkeypatch, fake):
  monkeypatch.setattr(_mod, "bq_client", lambda project: (fake, None))
  # step9 imports NotFound from google.api_core; alias it to the fake's.
  import google.api_core.exceptions as gexc

  monkeypatch.setattr(gexc, "NotFound", _NotFoundError)


_FULL_COLUMNS = [*_mod.RAG_CHUNKS_MIN_COLUMNS, "source_pk", "metadata"]


def test_step9_all_green_with_index(monkeypatch):
  fqn = "p.synthetic_rag.rag_chunks"
  fake = _FakeBQ(
      datasets={"p.synthetic_rag"},
      tables={fqn: _rag_table(_FULL_COLUMNS)},
      index_rows=[SimpleNamespace(index_name="idx", index_status="ACTIVE")],
  )
  _patch_bq(monkeypatch, fake)
  ctx = _ctx(fqn, fake)
  _mod.step9_rag_layer(ctx)
  statuses = {r.step: r.status for r in ctx.results}
  assert statuses == {"9a": _mod.OK, "9b": _mod.OK, "9c": _mod.OK}
  assert "idx (ACTIVE)" in ctx.results[-1].resource


def test_step9_missing_table_gives_bq_mk_action(monkeypatch):
  fqn = "p.synthetic_rag.rag_chunks"
  fake = _FakeBQ(datasets={"p.synthetic_rag"}, tables={})
  _patch_bq(monkeypatch, fake)
  ctx = _ctx(fqn, fake)
  _mod.step9_rag_layer(ctx)
  by_step = {r.step: r for r in ctx.results}
  assert by_step["9b"].status == _mod.ACTION
  assert "bq mk" in by_step["9b"].action
  assert "--time_partitioning_field created_at" in by_step["9b"].action
  assert by_step["9c"].status == _mod.SKIP  # index can't exist yet


def test_step9_contract_drift_is_deploy_blocking(monkeypatch):
  # A store missing source_fqn (multi-table scoping) or the DAY partition
  # cannot be shared by "any added dataset.table" — must be ACTION.
  fqn = "p.synthetic_rag.rag_chunks"
  cols = [c for c in _FULL_COLUMNS if c != "source_fqn"]
  fake = _FakeBQ(
      datasets={"p.synthetic_rag"},
      tables={fqn: _rag_table(cols, partition_field=None)},
  )
  _patch_bq(monkeypatch, fake)
  ctx = _ctx(fqn, fake)
  _mod.step9_rag_layer(ctx)
  r9b = {r.step: r for r in ctx.results}["9b"]
  assert r9b.status == _mod.ACTION
  assert "source_fqn" in r9b.resource
  assert "created_at" in r9b.resource


def test_step9_no_index_yet_is_informational_not_blocking(monkeypatch):
  fqn = "p.synthetic_rag.rag_chunks"
  fake = _FakeBQ(
      datasets={"p.synthetic_rag"}, tables={fqn: _rag_table(_FULL_COLUMNS)})
  _patch_bq(monkeypatch, fake)
  ctx = _ctx(fqn, fake)
  _mod.step9_rag_layer(ctx)
  r9c = {r.step: r for r in ctx.results}["9c"]
  assert r9c.status == _mod.SKIP
  assert "CREATE VECTOR INDEX" in r9c.resource


def test_step9_empty_fqn_opts_out(monkeypatch):
  ctx = _ctx("", None)
  _mod.step9_rag_layer(ctx)
  assert len(ctx.results) == 1
  assert ctx.results[0].status == _mod.SKIP


def test_parse_args_rag_default_and_opt_out():
  base = ["--project", "p", "--source-table", "p.raw.t"]
  args = _mod.parse_args(base)
  assert args.rag_chunks_table == "p.synthetic_rag.rag_chunks"
  args = _mod.parse_args([*base, "--rag-chunks-table", ""])
  assert args.rag_chunks_table == ""


def test_ddl_uri_skip_message_says_optional():
  args = _mod.parse_args(["--project", "p", "--source-table", "p.raw.t"])
  ctx = _mod.Ctx(args=args)
  _mod.step8_others(ctx)
  r8b = next(r for r in ctx.results if r.step == "8b")
  assert r8b.status == _mod.SKIP
  assert "optional" in r8b.resource
  assert "INFORMATION_SCHEMA" in r8b.resource


# --------------------------------------------------------------------------- #
# step 10 — free-text pool store (WS5 / ADR 0020)
# --------------------------------------------------------------------------- #
def _pool_ctx(fqn):
  args = argparse.Namespace(
      project="p",
      freetext_pools_table=fqn,
      schemas_dir=str(_REPO_ROOT / "config" / "bq_schema"),
  )
  return _mod.Ctx(args=args)


def _pool_table(columns):
  return SimpleNamespace(schema=[SimpleNamespace(name=c) for c in columns])


def test_step10_missing_table_is_skip_not_action(monkeypatch):
  """The pool store is a performance opt-in with tested graceful
    degradation — calling a deployment KO because it is absent would claim
    the deployment is broken when it is merely slower."""
  fqn = "p.synthetic_rag.freetext_pools"
  _patch_bq(monkeypatch, _FakeBQ(tables={}))
  ctx = _pool_ctx(fqn)
  _mod.step10_freetext_pools(ctx)
  (result,) = ctx.results
  assert result.status == _mod.SKIP
  assert "rebuilt per worker" in result.resource
  assert "bq mk" in result.resource


def test_step10_present_and_correct_is_ok(monkeypatch):
  fqn = "p.synthetic_rag.freetext_pools"
  _patch_bq(
      monkeypatch,
      _FakeBQ(tables={fqn: _pool_table(_mod.FREETEXT_POOLS_MIN_COLUMNS)}),
  )
  ctx = _pool_ctx(fqn)
  _mod.step10_freetext_pools(ctx)
  (result,) = ctx.results
  assert result.status == _mod.OK


def test_step10_drifted_table_is_an_action(monkeypatch):
  """A table that EXISTS but lost a column silently degrades every run
    back to rebuilding — invisible without this check."""
  fqn = "p.synthetic_rag.freetext_pools"
  columns = [c for c in _mod.FREETEXT_POOLS_MIN_COLUMNS if c != "stagnated"]
  _patch_bq(monkeypatch, _FakeBQ(tables={fqn: _pool_table(columns)}))
  ctx = _pool_ctx(fqn)
  _mod.step10_freetext_pools(ctx)
  (result,) = ctx.results
  assert result.status == _mod.ACTION
  assert "stagnated" in result.resource


def test_step10_opt_out_is_skip(monkeypatch):
  ctx = _pool_ctx("")
  _mod.step10_freetext_pools(ctx)
  (result,) = ctx.results
  assert result.status == _mod.SKIP


def test_committed_pool_schema_matches_the_contract():
  """The committed schema file is what `bq mk` consumes — it must carry
    exactly the columns the store's fetch() selects."""
  schema = json.loads((_REPO_ROOT / "config" / "bq_schema" / "synthetic_rag" /
                       "freetext_pools.schema.json").read_text())
  assert [f["name"] for f in schema] == _mod.FREETEXT_POOLS_MIN_COLUMNS
  assert all(f.get("description") for f in schema), "every column documented"


# --------------------------------------------------------------------------- #
# step 11 — source stats store (WS8, 2026-08-05 spec WS-B)
# --------------------------------------------------------------------------- #
def _stats_ctx(fqn):
  args = argparse.Namespace(
      project="p",
      source_stats_table=fqn,
      schemas_dir=str(_REPO_ROOT / "config" / "bq_schema"),
  )
  return _mod.Ctx(args=args)


def test_parse_args_source_stats_default_and_opt_out():
  base = ["--project", "p", "--source-table", "p.raw.t"]
  args = _mod.parse_args(base)
  assert args.source_stats_table == "p.synthetic_rag.source_table_stats"
  args = _mod.parse_args([*base, "--source-stats-table", ""])
  assert args.source_stats_table == ""


def test_step11_missing_table_is_skip_not_action(monkeypatch):
  """Stats persistence is optional: absent, the milestone + JSON artifact
    still fire — only the BQ write is skipped."""
  fqn = "p.synthetic_rag.source_table_stats"
  _patch_bq(monkeypatch, _FakeBQ(tables={}))
  ctx = _stats_ctx(fqn)
  _mod.step11_source_stats(ctx)
  (result,) = ctx.results
  assert result.status == _mod.SKIP
  assert "bq mk" in result.resource


def test_step11_present_and_correct_is_ok(monkeypatch):
  fqn = "p.synthetic_rag.source_table_stats"
  _patch_bq(
      monkeypatch,
      _FakeBQ(tables={fqn: _pool_table(_mod.SOURCE_STATS_MIN_COLUMNS)}),
  )
  ctx = _stats_ctx(fqn)
  _mod.step11_source_stats(ctx)
  (result,) = ctx.results
  assert result.status == _mod.OK


def test_step11_drifted_table_is_an_action(monkeypatch):
  """A present-but-drifted stats table fails the driver's write_rows load
    job mid-launch — the one case that must block."""
  fqn = "p.synthetic_rag.source_table_stats"
  columns = [c for c in _mod.SOURCE_STATS_MIN_COLUMNS if c != "empty_fraction"]
  _patch_bq(monkeypatch, _FakeBQ(tables={fqn: _pool_table(columns)}))
  ctx = _stats_ctx(fqn)
  _mod.step11_source_stats(ctx)
  (result,) = ctx.results
  assert result.status == _mod.ACTION
  assert "empty_fraction" in result.resource


def test_step11_opt_out_is_skip():
  ctx = _stats_ctx("")
  _mod.step11_source_stats(ctx)
  (result,) = ctx.results
  assert result.status == _mod.SKIP
  assert "omitted" in result.resource


# --------------------------------------------------------------------------- #
# step 13 — evaluation (sdfb-evaluation, ADR 0041)
# --------------------------------------------------------------------------- #
_EVAL_PKG = _REPO_ROOT / "packages" / "sdfb-evaluation"
# A copy of the repository without the evaluator (the one that ships) has no
# package for step 13 to read: the tests that go through the real package skip.
needs_evaluator = pytest.mark.skipif(
    not _EVAL_PKG.is_dir(),
    reason="packages/sdfb-evaluation is not part of this tree")
_EVAL_SCHEMAS = _EVAL_PKG / "src" / "sdfb_evaluation" / "schemas"
_EVAL_TABLES = ("evaluation_data_history", "evaluation_metrics",
                "evaluation_profiles", "evaluation_row_flags")
_EVAL_VIEWS = ("evaluation_latest", "evaluation_latest_per_job")
_EVAL_DS = "p.synthetic_data_quality"
_BASE_ARGV = ["--project", "p", "--source-table", "p.raw.t"]


class _FakeBlob:

  def __init__(self, present):
    self._present = present

  def exists(self):
    return self._present


class _FakeGCS:
  """Just enough of google.cloud.storage.Client: blob(...).exists() and
    list_blobs(bucket, prefix=..., max_results=...)."""

  def __init__(self, objects=()):
    self._objects = set(objects)
    self.asked = []
    self.listed = []

  def bucket(self, name):
    return SimpleNamespace(blob=lambda path: self._blob(f"gs://{name}/{path}"))

  def _blob(self, uri):
    self.asked.append(uri)
    return _FakeBlob(uri in self._objects)

  def list_blobs(self, bucket, prefix="", max_results=None):
    self.listed.append((bucket, prefix))
    names = sorted(uri[len(f"gs://{bucket}/"):]
                   for uri in self._objects
                   if uri.startswith(f"gs://{bucket}/{prefix}"))
    return [SimpleNamespace(name=name) for name in names[:max_results]]


def _eval_ctx(*extra):
  return _mod.Ctx(args=_mod.parse_args([*_BASE_ARGV, *extra]))


def _live_field(spec):
  return SimpleNamespace(
      name=spec["name"],
      field_type=spec["type"],
      mode=spec.get("mode", "NULLABLE"),
      fields=[_live_field(s) for s in spec.get("fields", [])])


def _schema(name):
  return json.loads((_EVAL_SCHEMAS / f"{name}.schema.json").read_text())


def _live_table(name,
                *,
                drop=(),
                retype=None,
                partitioned=True,
                cluster=True,
                expiration_days="package"):
  """A live table equal to the committed schema file, then bent on request."""
  consts = _mod.eval_constants(_EVAL_PKG)
  specs = [dict(s) for s in _schema(name) if s["name"] not in drop]
  for spec in specs:
    if retype and spec["name"] == retype[0]:
      spec["type"] = retype[1]
  field, kind, package_days = consts["partitioning"][name]
  days = package_days if expiration_days == "package" else expiration_days
  return SimpleNamespace(
      table_type="TABLE",
      schema=[_live_field(s) for s in specs],
      time_partitioning=(SimpleNamespace(
          field=field,
          type_=kind,
          expiration_ms=None if days is None else int(days * 86400000))
                         if partitioned else None),
      clustering_fields=list(consts["clustering"][name]) if cluster else None)


def _provisioned(**bent):
  tables = {
      f"{_EVAL_DS}.{n}": _live_table(n, **bent.get(n, {})) for n in _EVAL_TABLES
  }
  tables.update({
      f"{_EVAL_DS}.{v}": SimpleNamespace(table_type="VIEW", schema=[])
      for v in _EVAL_VIEWS
  })
  return tables


def _run13(monkeypatch, bq, *extra, gcs=None):
  _patch_bq(monkeypatch, bq)
  monkeypatch.setattr(_mod, "gcs_client", lambda: (gcs or _FakeGCS(), None))
  ctx = _eval_ctx(*extra)
  _mod.step13_evaluation(ctx)
  return ctx, {r.step: r for r in ctx.results}


def test_parse_args_evaluation_defaults_and_opt_out():
  args = _mod.parse_args(_BASE_ARGV)
  assert args.evaluation_dataset == _EVAL_DS
  assert args.evaluation_temp_dataset == ""
  assert args.evaluation_label_key_uri == ""
  assert args.require_evaluation is False
  args = _mod.parse_args(
      [*_BASE_ARGV, "--validation-runs-table", "q.dq.validation_runs"])
  assert args.evaluation_dataset == "q.dq"
  args = _mod.parse_args([
      *_BASE_ARGV, "--evaluation-dataset", "", "--evaluation-temp-dataset",
      "tmp", "--evaluation-label-key-uri", "gs://b/k", "--require-evaluation"
  ])
  assert args.evaluation_dataset == ""
  assert args.evaluation_temp_dataset == "p.tmp"
  assert args.evaluation_label_key_uri == "gs://b/k"
  assert args.require_evaluation is True


def test_step13_package_absent_is_one_skip(monkeypatch, tmp_path):
  monkeypatch.setattr(_mod, "EVAL_PACKAGE_DIR", tmp_path / "no-such-package")
  ctx, _ = _run13(monkeypatch, _FakeBQ(), "--require-evaluation")
  (result,) = ctx.results
  assert result.status == _mod.SKIP
  assert result.step == "13"
  assert "not in this tree" in result.resource


def test_step13_empty_dataset_opts_out_with_one_skip(monkeypatch):
  ctx, _ = _run13(monkeypatch, _FakeBQ(), "--evaluation-dataset", "")
  (result,) = ctx.results
  assert result.status == _mod.SKIP
  assert "omitted" in result.resource


@needs_evaluator
def test_step13_nothing_provisioned_skips_with_create_command(monkeypatch):
  ctx, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}))
  assert by["13a"].status == _mod.OK
  assert by["13b"].status == _mod.OK
  assert "temp dataset" in by["13b"].resource  # folded in
  assert "13i" not in by
  for step in ("13c", "13d", "13e", "13f", "13g", "13h"):
    assert by[step].status == _mod.SKIP, step
    assert ("sdfb-eval schemas --project p --dataset synthetic_data_quality "
            "--apply") in by[step].resource
  assert not any(r.status == _mod.ACTION for r in ctx.results)


@needs_evaluator
def test_step13_require_evaluation_turns_absent_into_action(monkeypatch):
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}),
                 "--require-evaluation")
  for step in ("13c", "13d", "13e", "13f", "13g", "13h"):
    assert by[step].status == _mod.ACTION, step
    assert "sdfb-eval schemas" in by[step].action


@needs_evaluator
def test_step13_half_provisioned_is_action_for_the_absent(monkeypatch):
  tables = {f"{_EVAL_DS}.evaluation_metrics": _live_table("evaluation_metrics")}
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13d"].status == _mod.OK  # evaluation_metrics
  for step in ("13c", "13e", "13f", "13g", "13h"):
    assert by[step].status == _mod.ACTION, step
    assert "half-provisioned" in by[step].resource


@needs_evaluator
def test_step13_tables_without_views_is_half_provisioned(monkeypatch):
  tables = {
      k: v
      for k, v in _provisioned().items()
      if k.rsplit(".", 1)[1] in _EVAL_TABLES
  }
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13g"].status == _mod.ACTION
  assert by["13h"].status == _mod.ACTION


@needs_evaluator
def test_step13_all_present_and_correct_is_all_ok(monkeypatch):
  ctx, by = _run13(monkeypatch,
                   _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned()))
  for step in ("13a", "13b", "13c", "13d", "13e", "13f", "13g", "13h"):
    assert by[step].status == _mod.OK, (step, by[step].resource)
  assert not any(r.status == _mod.ACTION for r in ctx.results)


@needs_evaluator
def test_step13_missing_column_is_action_naming_it(monkeypatch):
  tables = _provisioned(evaluation_metrics={"drop": ("table_name",)})
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13d"].status == _mod.ACTION
  assert "table_name" in by["13d"].resource
  assert by["13c"].status == _mod.OK


@needs_evaluator
def test_step13_wrong_type_is_action_naming_it(monkeypatch):
  tables = _provisioned(
      evaluation_profiles={"retype": ("evaluated_at", "DATE")})
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13e"].status == _mod.ACTION
  assert "evaluated_at" in by["13e"].resource
  assert "DATE" in by["13e"].resource


@needs_evaluator
def test_step13_record_subfield_missing_is_action(monkeypatch):
  tables = _provisioned()
  table = tables[f"{_EVAL_DS}.evaluation_data_history"]
  record = next(f for f in table.schema if f.name == "tables")
  record.fields = [f for f in record.fields if f.name != "role"]
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13c"].status == _mod.ACTION
  assert "tables.role" in by["13c"].resource


@needs_evaluator
def test_step13_sql_type_aliases_and_extra_columns_are_fine(monkeypatch):
  tables = _provisioned()
  table = tables[f"{_EVAL_DS}.evaluation_metrics"]
  for f in table.schema:  # the live API reports INTEGER / FLOAT / BOOLEAN
    f.field_type = {
        "INT64": "INTEGER",
        "FLOAT64": "FLOAT",
        "BOOL": "BOOLEAN"
    }.get(f.field_type, f.field_type)
  table.schema.append(_live_field({"name": "extra", "type": "STRING"}))
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13d"].status == _mod.OK


@needs_evaluator
def test_step13_partitioning_and_clustering_mismatch_are_actions(monkeypatch):
  tables = _provisioned(
      evaluation_data_history={"partitioned": False},
      evaluation_metrics={"cluster": False})
  tables[f"{_EVAL_DS}.evaluation_profiles"].time_partitioning = SimpleNamespace(
      field="evaluation_id", type_="DAY")
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13c"].status == _mod.ACTION
  assert "not partitioned" in by["13c"].resource
  assert by["13d"].status == _mod.ACTION
  assert "clustering" in by["13d"].resource
  assert by["13e"].status == _mod.ACTION
  assert "evaluation_id" in by["13e"].resource
  assert by["13f"].status == _mod.OK


@needs_evaluator
def test_step13_unreadable_constants_skip_the_comparison(monkeypatch):
  tables = _provisioned()  # built while the constants are still readable
  # drifted partitioning is not noticed, and the line says so
  tables[f"{_EVAL_DS}.evaluation_metrics"].time_partitioning = None
  monkeypatch.setattr(
      _mod, "eval_constants", lambda pkg: {
          "tables": None,
          "partitioning": None,
          "clustering": None
      })
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13d"].status == _mod.OK
  assert "not compared" in by["13d"].resource


@needs_evaluator
def test_step13_view_that_is_a_table_is_action(monkeypatch):
  tables = _provisioned()
  tables[f"{_EVAL_DS}.evaluation_latest"] = SimpleNamespace(
      table_type="TABLE", schema=[])
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables))
  assert by["13g"].status == _mod.ACTION
  assert "not a view" in by["13g"].resource
  assert by["13h"].status == _mod.OK


@needs_evaluator
def test_step13_dataset_missing_is_action_and_objects_not_checked(monkeypatch):
  _, by = _run13(monkeypatch, _FakeBQ())
  assert by["13b"].status == _mod.ACTION
  assert by["13c"].status == _mod.SKIP
  assert "13b" in by["13c"].resource


@needs_evaluator
def test_step13_temp_dataset_same_is_folded_into_13b(monkeypatch):
  _, by = _run13(monkeypatch,
                 _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned()),
                 "--evaluation-temp-dataset", "synthetic_data_quality")
  assert "13i" not in by
  assert "temp dataset" in by["13b"].resource


@needs_evaluator
def test_step13_temp_dataset_different_is_checked(monkeypatch):
  tables = _provisioned()
  _, by = _run13(monkeypatch,
                 _FakeBQ(datasets={_EVAL_DS, "p.eval_tmp"}, tables=tables),
                 "--evaluation-temp-dataset", "eval_tmp")
  assert by["13i"].status == _mod.OK
  assert "temp dataset" not in by["13b"].resource
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}, tables=tables),
                 "--evaluation-temp-dataset", "eval_tmp")
  assert by["13i"].status == _mod.ACTION
  assert "eval_tmp" in by["13i"].resource


def _run13j(monkeypatch, *names, extra=("--templates-bucket", "b"), gcs=None):
  """Step 13 on a fully provisioned dataset, with `names` as the objects
    under gs://b/synthetic/; returns (the 13j row, the fake GCS)."""
  bq = _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned())
  gcs = gcs or _FakeGCS(objects={f"gs://b/synthetic/{name}" for name in names})
  _, by = _run13(monkeypatch, bq, *extra, gcs=gcs)
  return by["13j"], gcs


def _main_template():
  version = _mod.generator_version(_REPO_ROOT)
  assert version, "packages/sdfb-beam/pyproject.toml names no version"
  return f"sdfb-{version}-template.json"


def test_generator_version_is_the_one_the_template_is_named_after(tmp_path):
  """The template build names the object after the sdfb-beam package's
    version (a release) or `latest` (a development build)."""
  package = tmp_path / "packages" / "sdfb-beam"
  package.mkdir(parents=True)
  (package / "pyproject.toml"
  ).write_text('[project]\nname = "sdfb-beam"\nversion = "1.2.3"\n')
  assert _mod.generator_version(tmp_path) == "1.2.3"
  assert _mod.generator_version(tmp_path / "nowhere") is None
  assert _mod.generator_version(_REPO_ROOT)


@needs_evaluator
def test_step13_template_is_the_one_template_of_the_deployment(monkeypatch):
  """One image and one template serve generation and evaluation (ADR
    0041's amendment): the object 13j wants is the generation template."""
  row, gcs = _run13j(monkeypatch, _main_template())
  assert row.status == _mod.OK
  assert _main_template() in row.resource
  assert "sdfb_job=evaluation" in row.resource
  assert f"gs://b/synthetic/{_main_template()}" in gcs.asked
  assert not gcs.listed  # found by name: the bucket is not listed


@needs_evaluator
def test_step13_template_development_pointer_is_enough(monkeypatch):
  row, _ = _run13j(monkeypatch, "sdfb-latest-template.json")
  assert row.status == _mod.OK
  assert "sdfb-latest-template.json" in row.resource
  assert "sdfb_job=evaluation" in row.resource


@needs_evaluator
def test_step13_template_asks_only_for_the_deployments_one_template(
    monkeypatch):
  """No evaluator-only template exists any more (ADR 0041, note of
    2026-10-15): the names asked for are the deployment's template's
    alone, the release's first."""
  _, gcs = _run13j(monkeypatch, "sdfb-notes.txt")
  assert [uri for uri in gcs.asked if uri.startswith("gs://b/synthetic/")] == [
      f"gs://b/synthetic/{_main_template()}",
      "gs://b/synthetic/sdfb-latest-template.json"
  ]


@needs_evaluator
def test_step13_template_names_the_release_one_when_both_exist(monkeypatch):
  row, _ = _run13j(monkeypatch, "sdfb-latest-template.json", _main_template())
  assert row.status == _mod.OK
  assert _main_template() in row.resource
  assert "development pointer" not in row.resource


@needs_evaluator
def test_step13_template_of_another_version_is_found_and_named(monkeypatch):
  """A development build names its template after the branch and commit,
    which this script cannot know: any sdfb-*-template.json counts, and
    the row says which it found."""
  row, gcs = _run13j(monkeypatch, "sdfb-feature-x-0a1b2c3-template.json",
                     "sdfb-feature-y-4d5e6f7-template.json", "sdfb-notes.txt",
                     "other-template.json")
  assert row.status == _mod.OK
  assert "sdfb-feature-x-0a1b2c3-template.json" in row.resource
  assert "1 more" in row.resource
  assert "sdfb-notes.txt" not in row.resource
  assert "other-template.json" not in row.resource
  assert gcs.listed == [("b", "synthetic/sdfb-")]


@needs_evaluator
def test_step13_template_found_is_existence_only_and_says_so(monkeypatch):
  """An image built before the evaluator was added cannot run it, and a
    template object does not say which image it was built on."""
  row, _ = _run13j(monkeypatch, _main_template())
  assert "existence only" in row.resource


@needs_evaluator
def test_step13_template_absent_and_no_bucket(monkeypatch):
  row, gcs = _run13j(monkeypatch, "sdfb-notes.txt")
  assert row.status == _mod.SKIP
  assert "not built yet" in row.resource
  # what to build: the deployment's one template, and nothing else
  assert "docker/flex_template_metadata.json" in row.resource
  assert "gs://b/synthetic/sdfb-<version>-template.json" in row.resource
  assert "sdfb_job=evaluation" in row.resource
  assert "build_flex_template.sh" not in row.resource
  assert "evaluator-only" not in row.resource
  assert gcs.listed == [("b", "synthetic/sdfb-")]
  row, _ = _run13j(
      monkeypatch, extra=("--templates-bucket", "b", "--require-evaluation"))
  assert row.status == _mod.ACTION
  assert "docker/flex_template_metadata.json" in row.action
  row, _ = _run13j(monkeypatch, extra=())
  assert row.status == _mod.SKIP
  assert "--templates-bucket" in row.resource


@needs_evaluator
def test_step13_template_unreadable_bucket_is_skip(monkeypatch):

  class _Denied(_FakeGCS):

    def _blob(self, uri):
      raise PermissionError("403 on " + uri)

  row, _ = _run13j(monkeypatch, gcs=_Denied())
  assert row.status == _mod.SKIP
  assert "PermissionError" in row.resource


@needs_evaluator
def test_step13_label_key_variants(monkeypatch):
  bq = _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned())
  _, by = _run13(monkeypatch, bq)
  assert by["13k"].status == _mod.SKIP
  assert "optional" in by["13k"].resource
  assert "not stable across runs" in by["13k"].resource
  key = "gs://b/keys/label.key"
  _, by = _run13(
      monkeypatch,
      bq,
      "--evaluation-label-key-uri",
      key,
      gcs=_FakeGCS(objects={key}))
  assert by["13k"].status == _mod.OK
  _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri", key)
  assert by["13k"].status == _mod.ACTION
  secret = "projects/p/secrets/label-key/versions/3"
  gcs = _FakeGCS()
  _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri", secret, gcs=gcs)
  assert by["13k"].status == _mod.SKIP
  assert "not verified here" in by["13k"].resource
  assert "gcloud secrets versions describe 3 --secret=label-key --project=p" \
      in by["13k"].resource
  assert not gcs.asked  # a secret name never touches GCS
  assert "label.key" not in by["13k"].resource


@needs_evaluator
def test_step13_no_bq_client_skips_every_bq_line(monkeypatch):
  monkeypatch.setattr(_mod, "bq_client", lambda project: (None, "no creds"))
  monkeypatch.setattr(_mod, "gcs_client", lambda: (None, "no creds"))
  ctx = _eval_ctx()
  _mod.step13_evaluation(ctx)
  assert not any(r.status == _mod.ACTION for r in ctx.results)
  assert {r.step for r in ctx.results} >= {"13a", "13b", "13c", "13h"}


@needs_evaluator
def test_committed_evaluator_files_match_what_step13_reads():
  """The contract check reads these files: a rename or a reshaped schema
    must fail here, not silently turn the check into a no-op."""
  for name in _EVAL_TABLES:
    fields = _schema(name)
    assert isinstance(fields, list) and fields
    assert all({"name", "type"} <= set(f) for f in fields), name
  record = next(
      f for f in _schema("evaluation_data_history") if f["type"] == "RECORD")
  assert record["fields"] and all("name" in s for s in record["fields"])
  sql = (_EVAL_SCHEMAS / "views.sql").read_text()
  assert all(v in sql for v in _EVAL_VIEWS)
  assert (_EVAL_PKG / "src" / "sdfb_evaluation" / "catalogue" /
          "metrics.yaml").is_file()
  consts = _mod.eval_constants(_EVAL_PKG)
  assert consts["tables"] == _EVAL_TABLES
  assert set(consts["partitioning"]) == set(_EVAL_TABLES)
  assert set(consts["clustering"]) == set(_EVAL_TABLES)
  assert _mod.EVAL_TABLES == _EVAL_TABLES
  assert _mod.EVAL_VIEWS == _EVAL_VIEWS


def test_step13_config_artifacts_missing_or_unparseable_is_action(
    monkeypatch, tmp_path):
  pkg = tmp_path / "sdfb-evaluation"
  schemas = pkg / "src" / "sdfb_evaluation" / "schemas"
  schemas.mkdir(parents=True)
  (schemas / "evaluation_metrics.schema.json").write_text("{not json")
  monkeypatch.setattr(_mod, "EVAL_PACKAGE_DIR", pkg)
  _, by = _run13(monkeypatch, _FakeBQ(datasets={_EVAL_DS}))
  assert by["13a"].status == _mod.ACTION
  assert "views.sql" in by["13a"].resource
  assert "evaluation_metrics.schema.json" in by["13a"].resource
  assert "restore the committed evaluator files" in by["13a"].action


@needs_evaluator
def test_step13_row_flag_retention_compared_with_the_package(monkeypatch):
  """evaluation_row_flags expires after a package-set number of days (a
    privacy retention rule): no expiration or a longer one is an ACTION, a
    shorter one is fine, a table the package gives none is not checked."""
  consts = _mod.eval_constants(_EVAL_PKG)
  days = consts["partitioning"]["evaluation_row_flags"][2]
  assert days, "the package must name a row-flag retention"
  assert consts["partitioning"]["evaluation_metrics"][2] is None

  def bq(**bent):
    return _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned(**bent))

  _, by = _run13(monkeypatch,
                 bq(evaluation_row_flags={"expiration_days": None}))
  assert by["13f"].status == _mod.ACTION
  assert "no partition expiration" in by["13f"].resource
  assert str(days) in by["13f"].resource
  assert "bq update --time_partitioning_expiration" in by["13f"].action
  _, by = _run13(monkeypatch,
                 bq(evaluation_row_flags={"expiration_days": days + 90}))
  assert by["13f"].status == _mod.ACTION
  assert str(days + 90) in by["13f"].resource
  _, by = _run13(monkeypatch,
                 bq(evaluation_row_flags={"expiration_days": days - 30}))
  assert by["13f"].status == _mod.OK
  assert "shorter" in by["13f"].resource
  _, by = _run13(monkeypatch, bq(evaluation_metrics={"expiration_days": 5}))
  assert by["13d"].status == _mod.OK  # package names none: not checked
  assert "shorter" not in by["13d"].resource


def _watch_opens(monkeypatch, target):
  """Record every attempt to open `target` through any Python-level door:
    builtins.open, io.open, Path.open / read_text / read_bytes, os.open."""
  import builtins
  import io

  touched = []

  def spy(real):

    def wrapper(file, *args, **kwargs):
      if str(file) == str(target):
        touched.append(real.__name__)
      return real(file, *args, **kwargs)

    return wrapper

  monkeypatch.setattr(builtins, "open", spy(builtins.open))
  monkeypatch.setattr(io, "open", spy(io.open))
  monkeypatch.setattr(os, "open", spy(os.open))
  for name in ("open", "read_text", "read_bytes"):
    real = getattr(Path, name)

    def method(self, *args, _real=real, _name=name, **kwargs):
      if str(self) == str(target):
        touched.append(f"Path.{_name}")
      return _real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, method)
  return touched


@needs_evaluator
def test_step13_label_key_local_path(monkeypatch, tmp_path):
  bq = _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned())
  key = tmp_path / "label.key"
  key.write_bytes(b"super-secret-bytes")
  touched = _watch_opens(monkeypatch, key)
  _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri", str(key))
  assert by["13k"].status == _mod.OK
  assert "every Dataflow worker" in by["13k"].resource
  assert "super-secret-bytes" not in by["13k"].resource
  assert not touched, f"the key file was opened: {touched}"  # isfile/access only
  _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri",
                 str(tmp_path / "missing.key"))
  assert by["13k"].status == _mod.ACTION


def test_watch_opens_would_catch_a_read(monkeypatch, tmp_path):
  """The spy itself must be able to fail, or the test above proves nothing."""
  key = tmp_path / "label.key"
  key.write_bytes(b"x")
  touched = _watch_opens(monkeypatch, key)
  key.read_text()
  with open(key, "rb"):
    pass
  assert "Path.read_text" in touched
  assert "open" in touched


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a mode-000 file")
@needs_evaluator
def test_step13_label_key_unreadable_local_path_is_action(
    monkeypatch, tmp_path):
  bq = _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned())
  key = tmp_path / "label.key"
  key.write_bytes(b"x")
  key.chmod(0)
  try:
    _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri", str(key))
  finally:
    key.chmod(0o600)
  assert by["13k"].status == _mod.ACTION
  assert "not readable" in by["13k"].resource


def _run_main(monkeypatch, tmp_path, *extra):
  """`main()` end to end with every step but 13 replaced by a no-op: the
    exit code and the report come from main's own code."""
  for name in dir(_mod):
    if name.startswith("step") and name != "step13_evaluation":
      monkeypatch.setattr(_mod, name, lambda ctx: None)
  _patch_bq(monkeypatch, _FakeBQ(datasets={_EVAL_DS}))
  monkeypatch.setattr(_mod, "gcs_client", lambda: (_FakeGCS(), None))
  return _mod.main([*_BASE_ARGV, "--report-dir", str(tmp_path), *extra])


@needs_evaluator
def test_main_exit_code_default_is_unchanged_by_step13(monkeypatch, tmp_path,
                                                       capsys):
  assert _run_main(monkeypatch, tmp_path) == 0
  assert "0 ACTION" in capsys.readouterr().out
  report = next(tmp_path.glob("deployment_prerequisites_*.md")).read_text()
  assert "| 13c |" in report  # step 13 did run, as SKIPs


@needs_evaluator
def test_main_exit_code_require_evaluation_fails_on_unprovisioned(
    monkeypatch, tmp_path):
  assert _run_main(monkeypatch, tmp_path, "--require-evaluation") == 1


@needs_evaluator
def test_step13_label_key_relative_or_other_form_names_the_three_forms(
    monkeypatch):
  bq = _FakeBQ(datasets={_EVAL_DS}, tables=_provisioned())
  for bad in ("keys/label.key", "s3://b/k", "projects/p/secrets/s"):
    _, by = _run13(monkeypatch, bq, "--evaluation-label-key-uri", bad)
    assert by["13k"].status == _mod.ACTION, bad
    for form in ("gs://", "projects/P/secrets/S/versions/V", "absolute"):
      assert form in by["13k"].action, (bad, form)
