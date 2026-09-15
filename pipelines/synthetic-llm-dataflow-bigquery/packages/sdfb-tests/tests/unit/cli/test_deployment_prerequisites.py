"""Unit tests for `scripts/deployment_prerequisites.py` (the predeployment preflight).

Offline-only: the GCP client factories are monkeypatched to the unavailable
branch so steps 4-8 land on SKIP without touching the network. The local /
derive steps (1, 2, 3, 8c) and the report/exit-code contract run for real.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=redefined-outer-name,unused-argument

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[5] / "scripts" / "deployment_prerequisites.py"
_spec = importlib.util.spec_from_file_location("deployment_prerequisites",
                                               _SCRIPT)
dp = importlib.util.module_from_spec(_spec)
sys.modules[
    _spec
    .name] = dp  # register before exec so @dataclass can resolve annotations
_spec.loader.exec_module(dp)


@pytest.fixture
def ddl_file(tmp_path, narrow_ddl_dict) -> Path:
  p = tmp_path / "src_ddl.json"
  p.write_text(json.dumps(narrow_ddl_dict))
  return p


def _args(tmp_path, ddl: Path, **extra) -> object:
  argv = [
      "--project",
      "demo",
      "--source-table",
      "demo.raw.customers",
      "--no-extract",
      "--ddl-json",
      str(ddl),
      "--schemas-dir",
      str(tmp_path / "schemas"),
      "--models-dir",
      str(tmp_path / "models"),
      "--report-dir",
      str(tmp_path / "out"),
  ]
  for k, v in extra.items():
    argv += [f"--{k.replace('_', '-')}", v]
  return dp.parse_args(argv)


@pytest.fixture
def offline(monkeypatch):
  """Force both GCP client factories to the 'unavailable' branch."""
  monkeypatch.setattr(dp, "bq_client", lambda project: (None, "offline"))
  monkeypatch.setattr(dp, "gcs_client", lambda: (None, "offline"))


# --- links -------------------------------------------------------------------
def test_links_carry_project_and_console_host():
  tl = dp.bq_table_link("proj.ds.tbl")
  assert "console.cloud.google.com/bigquery" in tl and "d=ds" in tl and "t=tbl" in tl
  gl = dp.gcs_link("gs://buck/pre/fix/", "proj")
  assert "storage/browser/buck/pre/fix/" in gl and "project=proj" in gl


# --- steps 1 & 2: derive + drop schema files ---------------------------------
def test_steps_1_2_write_bare_array_schemas(tmp_path, ddl_file):
  ctx = dp.Ctx(args=_args(tmp_path, ddl_file))
  # landing defaults to the source table name in synthetic_data
  assert ctx.args.landing_table == "demo.synthetic_data.customers"
  dp.step1_source_ddl(ctx)
  dp.step2_landing_schema(ctx)

  # dataset-nested: source under its own dataset, landing under synthetic_data
  src = json.loads(
      (tmp_path / "schemas" / "raw" / "customers.schema.json").read_text())
  landing = json.loads((tmp_path / "schemas" / "synthetic_data" /
                        "customers.schema.json").read_text())
  assert isinstance(src, list) and isinstance(landing, list)  # bare arrays
  assert src == landing  # landing mirrors source
  assert {r.status for r in ctx.results} == {dp.OK}
  assert ctx.table_schema is not None


# --- step 3: local weights ---------------------------------------------------
def test_step3_missing_dir_is_action(tmp_path, ddl_file):
  ctx = dp.Ctx(
      args=_args(
          tmp_path,
          ddl_file,
          model_uri="gs://b/synthetic/models/gemma4/e4b-it/v1/"))
  dp.step3_local_weights(ctx)
  llm = next(r for r in ctx.results if r.step == "3a")
  assert llm.status == dp.ACTION


def test_step3_complete_dir_is_ok(tmp_path, ddl_file):
  mdir = tmp_path / "models" / "gemma4" / "e4b-it" / "v1"
  mdir.mkdir(parents=True)
  for f in dp.LLM_REQUIRED:
    (mdir / f).write_text("{}")
  (mdir / "tokenizer.json").write_text("{}")  # gemma-style tokenizer asset
  (mdir / "model.safetensors").write_bytes(
      b"\x00")  # single shard → no index needed
  ctx = dp.Ctx(
      args=_args(
          tmp_path,
          ddl_file,
          model_uri="gs://b/synthetic/models/gemma4/e4b-it/v1/"))
  dp.step3_local_weights(ctx)
  llm = next(r for r in ctx.results if r.step == "3a")
  assert llm.status == dp.OK


# --- steps 4-8a: GCP checks skip cleanly offline -----------------------------
def test_gcp_steps_skip_when_client_unavailable(tmp_path, ddl_file, offline):
  ctx = dp.Ctx(
      args=_args(
          tmp_path,
          ddl_file,
          staging_bucket="b-staging",
          templates_bucket="b-templates",
          model_uri="gs://b/synthetic/models/gemma4/e4b-it/v1/"))
  for step in (dp.step4_bq_tables, dp.step5_staging_bucket,
               dp.step6_templates_bucket, dp.step7_bq_datasets,
               dp.step8_others):
    step(ctx)
  gcp = [
      r for r in ctx.results
      if r.step[0] in {"4", "5", "6", "7"} or r.step in {"8a", "8b"}
  ]
  assert gcp and all(r.status == dp.SKIP for r in gcp)


# --- step 8c: committed config artifacts -------------------------------------
def test_step8c_action_when_config_missing(tmp_path, ddl_file):
  ctx = dp.Ctx(args=_args(tmp_path,
                          ddl_file))  # schemas-dir is an empty tmp dir
  dp.step8_others(ctx)
  cfg = next(r for r in ctx.results if r.step == "8c")
  assert cfg.status == dp.ACTION


# --- end-to-end: report written, KO exit on ACTION ---------------------------
def test_main_writes_report_and_returns_ko(tmp_path, ddl_file, offline, capsys):
  argv = [
      "--project",
      "demo",
      "--source-table",
      "demo.raw.customers",
      "--no-extract",
      "--ddl-json",
      str(ddl_file),
      "--schemas-dir",
      str(tmp_path / "schemas"),
      "--models-dir",
      str(tmp_path / "models"),
      "--report-dir",
      str(tmp_path / "out"),
      "--model-uri",
      "gs://b/synthetic/models/gemma4/e4b-it/v1/",  # → missing local → ACTION
  ]
  rc = dp.main(argv)
  assert rc == 1  # KO: at least one ACTION
  reports = list((tmp_path / "out").glob("deployment_prerequisites_*.md"))
  assert len(reports) == 1
  body = reports[0].read_text()
  assert "KO — actions required" in body
  assert "## Actions needed" in body
  assert "report:" in capsys.readouterr().out


def test_step3_qwen_modelscope_layout_without_special_tokens_map_is_ok(
    tmp_path, ddl_file):
  """Qwen checkpoints (HF + ModelScope) ship NO special_tokens_map.json —
    special tokens live in tokenizer_config.json. The preflight must not
    demand it (it is gemma/bge that ship one, not every family)."""
  mdir = tmp_path / "models" / "qwen3" / "4b-instruct-2507" / "v1"
  mdir.mkdir(parents=True)
  for f in ("config.json", "tokenizer_config.json", "generation_config.json",
            "tokenizer.json", "vocab.json", "merges.txt", "configuration.json"):
    (mdir / f).write_text("{}")
  for shard in ("model-00001-of-00002.safetensors",
                "model-00002-of-00002.safetensors"):
    (mdir / shard).write_bytes(b"\x00")
  (mdir / "model.safetensors.index.json").write_text("{}")
  ctx = dp.Ctx(
      args=_args(
          tmp_path,
          ddl_file,
          model_uri="gs://b/synthetic/models/qwen3/4b-instruct-2507/v1/"))
  dp.step3_local_weights(ctx)
  llm = next(r for r in ctx.results if r.step == "3a")
  assert llm.status == dp.OK


def test_step3_missing_tokenizer_assets_is_action(tmp_path, ddl_file):
  """config+tokenizer_config alone are not a loadable tokenizer: at least
    tokenizer.json / tokenizer.model, or the BPE pair vocab.json+merges.txt,
    must be present."""
  mdir = tmp_path / "models" / "qwen3" / "4b-instruct-2507" / "v1"
  mdir.mkdir(parents=True)
  for f in dp.LLM_REQUIRED:
    (mdir / f).write_text("{}")
  (mdir / "model.safetensors").write_bytes(b"\x00")
  ctx = dp.Ctx(
      args=_args(
          tmp_path,
          ddl_file,
          model_uri="gs://b/synthetic/models/qwen3/4b-instruct-2507/v1/"))
  dp.step3_local_weights(ctx)
  llm = next(r for r in ctx.results if r.step == "3a")
  assert llm.status == dp.ACTION
  assert "tokenizer" in llm.resource
