"""Unit tests for `scripts/derive_landing_schema.py`.

The script is a thin CLI over `derive_bq_schema` (already tested in
`codegen/test_derive_bq_ddl.py`). These tests cover the wrapper's own
behaviour: writing the **bare array** (not `{"fields": …}`), and the
`--print-bq` command assembly with/without partitioning + clustering.

Pure sdfb-core — no Beam, no GCP; runs on the laptop.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=redefined-outer-name

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Load the repo-root script (not a package, so not importable by name).
_SCRIPT = Path(__file__).parents[5] / "scripts" / "derive_landing_schema.py"
_spec = importlib.util.spec_from_file_location("derive_landing_schema", _SCRIPT)
derive_landing_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(derive_landing_schema)


def _write_ddl(tmp_path: Path, ddl: dict) -> Path:
  p = tmp_path / "src_ddl.json"
  p.write_text(json.dumps(ddl))
  return p


@pytest.fixture
def partitioned_ddl_dict(narrow_ddl_dict: dict) -> dict:
  """`narrow_ddl_dict` plus DAY partitioning and clustering."""
  return {
      **narrow_ddl_dict,
      "partitioning": {
          "type": "DAY",
          "field": "signup_at"
      },
      "clustering": {
          "fields": ["email", "customer_id"]
      },
  }


def test_writes_bare_array_not_fields_wrapper(tmp_path, narrow_ddl_dict):
  src = _write_ddl(tmp_path, narrow_ddl_dict)
  out = tmp_path / "landing_ddl.json"

  rc = derive_landing_schema.main([str(src), "-o", str(out)])

  assert rc == 0
  schema = json.loads(out.read_text())
  assert isinstance(schema, list)  # bare array — bq / Terraform shape
  assert {f["name"] for f in schema} == {
      "customer_id",
      "email",
      "signup_at",
      "lifetime_value",
  }


def test_preserves_type_constraints(tmp_path, narrow_ddl_dict):
  src = _write_ddl(tmp_path, narrow_ddl_dict)
  out = tmp_path / "landing_ddl.json"

  derive_landing_schema.main([str(src), "-o", str(out)])

  schema = {f["name"]: f for f in json.loads(out.read_text())}
  assert schema["email"]["maxLength"] == 255
  assert schema["lifetime_value"]["precision"] == 18
  assert schema["lifetime_value"]["scale"] == 2


def test_print_bq_folds_in_partitioning_and_clustering(tmp_path, capsys,
                                                       partitioned_ddl_dict):
  src = _write_ddl(tmp_path, partitioned_ddl_dict)
  out = tmp_path / "landing_ddl.json"

  rc = derive_landing_schema.main(
      [str(src), "-o",
       str(out), "--print-bq", "myproj:synthetic_data.landing"])

  assert rc == 0
  stdout = capsys.readouterr().out
  assert "partitioning: DAY signup_at" in stdout
  assert "clustering: email,customer_id" in stdout
  assert "bq mk --table" in stdout
  assert f"--schema {out}" in stdout
  assert "--time_partitioning_type DAY --time_partitioning_field signup_at" in stdout
  assert "--clustering_fields email,customer_id" in stdout
  assert "myproj:synthetic_data.landing" in stdout


def test_print_bq_omits_flags_when_ddl_has_none(tmp_path, capsys,
                                                narrow_ddl_dict):
  src = _write_ddl(tmp_path, narrow_ddl_dict)  # no partitioning / clustering
  out = tmp_path / "landing_ddl.json"

  derive_landing_schema.main(
      [str(src), "-o",
       str(out), "--print-bq", "myproj:synthetic_data.landing"])

  stdout = capsys.readouterr().out
  assert "bq mk --table" in stdout
  assert "--time_partitioning" not in stdout
  assert "--clustering_fields" not in stdout
  assert "partitioning:" not in stdout
  assert "clustering:" not in stdout
