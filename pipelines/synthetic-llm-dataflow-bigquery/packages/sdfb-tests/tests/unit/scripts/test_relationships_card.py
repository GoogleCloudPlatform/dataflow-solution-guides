"""Unit tests for `scripts/relationships/card.py`'s local directory scan.

Loaded via importlib the same way `test_deployment_prerequisites.py` loads
its script (see that file's docstring/idiom).

The Beam loader (`sdfb_beam.io.relationships.load_relationship_registry`)
already skips documentation samples (`example_*.yaml`, `*.example.yaml`)
on a directory scan and still loads one named directly — this script's own
local-path branch (it never touches Beam's `FileSystems` for a plain
directory) must apply the exact same rule, via the same public predicate
(`is_sample_model`), so `--all` never renders a committed sample next to a
real model that reuses the same anonymised aliases.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[5] / "scripts" / "relationships" / "card.py"
_spec = importlib.util.spec_from_file_location("relationships_card", _SCRIPT)
card = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = card
_spec.loader.exec_module(card)

_REAL = "model: real\ntables:\n  A_TABLE:\n    pk: [A_COL_001]\n"
_EXAMPLE = "model: example_x\ntables:\n  A_TABLE:\n    pk: [A_COL_001]\n"


def _write(tmp_path: Path, name: str, text: str) -> Path:
  path = tmp_path / name
  path.write_text(text)
  return path


def test_a_directory_scan_skips_committed_samples(tmp_path, capsys):
  _write(tmp_path, "example_x.yaml", _EXAMPLE)
  _write(tmp_path, "real.yaml", _REAL)

  rc = card.main(["--relationships-uri", str(tmp_path), "--all"])

  assert rc == 0
  out = capsys.readouterr()
  assert "real" in out.out
  assert "example_x" not in out.out
  assert "skipping sample model" in out.err
  assert "example_x.yaml" in out.err


def test_a_sample_named_directly_still_renders(tmp_path, capsys):
  path = _write(tmp_path, "example_x.yaml", _EXAMPLE)

  rc = card.main(["--relationships-uri", str(path), "--all"])

  assert rc == 0
  out = capsys.readouterr()
  assert "example_x" in out.out


def test_a_directory_with_only_samples_says_so_instead_of_crashing(
    tmp_path, capsys):
  _write(tmp_path, "example_x.yaml", _EXAMPLE)

  rc = card.main(["--relationships-uri", str(tmp_path), "--all"])

  assert rc == 0
  out = capsys.readouterr()
  assert "no model files under" in out.out
  assert "skipping sample model" in out.err
