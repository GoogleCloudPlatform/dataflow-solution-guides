"""Unit tests for `scripts/deployment_prerequisites.py` — step 9 (RAG chunk
store, WS2) and the ddl-optional messaging.

Loaded via importlib (the script is not a package module). Offline-only: the
BigQuery surface is a fake injected through the module's `bq_client`
indirection — no GCP credentials, no network.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,unused-argument

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
