"""Loading `config/relationships/` from disk, a file, or GCS (ADR 0032).

The launcher must find the models with zero ceremony: the packaged
directory is the default, a single file works, and a `gs://` override
needs no image rebuild. Absence is legitimate; a broken file is not.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,missing-class-docstring,use-implicit-booleaness-not-comparison

from __future__ import annotations

import logging

import pytest
from sdfb_beam.io.relationships import is_sample_model, load_relationship_registry
from sdfb_core.contracts.relationships import RelationshipError

_MODEL_A = """
model: retail
tables:
  A_TABLE:
    pk: [A_COL_001]
  B_TABLE:
    pk: [B_COL_001]
    fk:
      - cols: [B_COL_002]
        ref: A_TABLE
        ref_cols: [A_COL_001]
"""

_MODEL_B = """
model: risk
tables:
  R_TABLE:
    pk: [R_COL_001]
"""


def _write(tmp_path, name: str, text: str):
  path = tmp_path / name
  path.write_text(text)
  return path


class TestDirectoryLoading:

  def test_every_model_in_the_folder_is_loaded(self, tmp_path):
    _write(tmp_path, "retail.yaml", _MODEL_A)
    _write(tmp_path, "risk.yml", _MODEL_B)
    registry = load_relationship_registry(str(tmp_path))
    assert {m.model for m in registry.models} == {"retail", "risk"}
    assert set(registry.component("A_TABLE")) == {"A_TABLE", "B_TABLE"}
    assert registry.component("R_TABLE") == ("R_TABLE",)

  def test_non_model_files_are_ignored(self, tmp_path):
    _write(tmp_path, "retail.yaml", _MODEL_A)
    _write(tmp_path, "README.md", "# not a model")
    assert len(load_relationship_registry(str(tmp_path)).models) == 1

  def test_a_single_file_uri_works(self, tmp_path):
    path = _write(tmp_path, "retail.yaml", _MODEL_A)
    registry = load_relationship_registry(str(path))
    assert [m.model for m in registry.models] == ["retail"]

  def test_the_source_path_is_carried_for_the_log_card(self, tmp_path):
    path = _write(tmp_path, "retail.yaml", _MODEL_A)
    registry = load_relationship_registry(str(tmp_path))
    assert registry.models[0].source.endswith("retail.yaml")
    assert str(path) in registry.card("A_TABLE")


class TestAbsenceAndFailure:
  """Absence is legitimate ONLY where nobody pointed: the packaged
    default. A URI someone typed and got wrong must never degrade into
    "this run has no relationships" — that silently generates every
    table alone and loses the whole model."""

  def test_the_default_location_may_hold_no_models(self, tmp_path, caplog,
                                                   monkeypatch):
    monkeypatch.setattr(
        "sdfb_beam.io.relationships.DEFAULT_RELATIONSHIPS_URI",
        str(tmp_path),
    )
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      registry = load_relationship_registry(str(tmp_path))
    assert registry.models == ()
    assert "name=relationships_absent" in caplog.text

  def test_an_explicit_location_with_no_models_stops_the_launch(self, tmp_path):
    with pytest.raises(RelationshipError, match="no model files"):
      load_relationship_registry(str(tmp_path))

  def test_an_unreachable_bucket_stops_the_launch(self, monkeypatch):
    """A typo'd or unauthorized gs:// path is the likeliest first
        mistake; it must read as a stop, not as "no relationships"."""
    from apache_beam.io.filesystem import BeamIOError

    def _boom(_patterns):
      raise BeamIOError("bucket does not exist")

    monkeypatch.setattr("sdfb_beam.io.relationships.FileSystems.match", _boom)
    with pytest.raises(RelationshipError) as exc:
      load_relationship_registry("gs://bucket/typo_level")
    assert "gs://bucket/typo_level" in str(exc.value)
    assert "BeamIOError" in str(exc.value)

  def test_an_empty_uri_disables_relationships_on_purpose(self):
    assert load_relationship_registry("").models == ()

  def test_a_broken_model_stops_the_launch(self, tmp_path):
    _write(tmp_path, "broken.yaml",
           "model: x\ntables:\n  T:\n    pk: oops: [\n")
    with pytest.raises(RelationshipError):
      load_relationship_registry(str(tmp_path))

  def test_an_unknown_ref_stops_the_launch(self, tmp_path):
    _write(
        tmp_path,
        "bad.yaml",
        "model: x\ntables:\n  T:\n    fk:\n      - cols: [A]\n"
        "        ref: NOPE\n        ref_cols: [A]\n",
    )
    with pytest.raises(RelationshipError, match="does not name a table"):
      load_relationship_registry(str(tmp_path))


def test_loaded_milestone_reports_what_the_run_will_use(tmp_path, caplog):
  _write(tmp_path, "retail.yaml", _MODEL_A)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    registry = load_relationship_registry(str(tmp_path))
  assert "name=relationships_loaded" in caplog.text
  assert "models=retail" in caplog.text
  assert "tables=2" in caplog.text
  assert f"sha={registry.sha12()}" in caplog.text


class TestDocumentationSamplesAreSkipped:
  """Launch 2026-09-10_12_09_34-5531344118137403488 died at registry
    load: the committed `example_retail.yaml` sample declares the same
    anonymised aliases (A_TABLE…) as the operator's real model, and the
    one-source-of-truth rule refused both. A directory scan now skips
    `example_*.yaml` / `*.example.yaml` samples (announced once); a URI
    that names a sample file directly still loads it."""

  _KW = (
      "model: kw\ntables:\n  A_TABLE:\n    pk: [K]\n  B_TABLE:\n    pk: [K]\n"
      "    fk:\n      - cols: [K]\n        ref: A_TABLE\n        ref_cols: [K]\n"
  )
  _EXAMPLE = "model: example_retail\ntables:\n  A_TABLE:\n    pk: [X]\n"

  def test_a_directory_scan_skips_the_samples(self, tmp_path, caplog):
    _write(tmp_path, "example_retail.yaml", self._EXAMPLE)
    _write(tmp_path, "retail.example.yaml", self._EXAMPLE)
    _write(tmp_path, "kw.yaml", self._KW)
    with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
      registry = load_relationship_registry(str(tmp_path))
    assert [m.model for m in registry.models] == ["kw"]
    assert "name=relationships_example_skipped" in caplog.text
    assert "example_retail.yaml" in caplog.text

  def test_a_sample_named_directly_still_loads(self, tmp_path):
    path = _write(tmp_path, "example_retail.yaml", self._EXAMPLE)
    registry = load_relationship_registry(str(path))
    assert [m.model for m in registry.models] == ["example_retail"]

  def test_only_samples_in_an_explicit_directory_still_stops(self, tmp_path):
    _write(tmp_path, "example_retail.yaml", self._EXAMPLE)
    with pytest.raises(RelationshipError, match=r"no model files"):
      load_relationship_registry(str(tmp_path))

  def test_is_sample_model_is_public_for_other_callers(self):
    """`scripts/relationships/card.py` reuses this exact predicate for
        its own local-directory scan (same rule, one definition)."""
    assert is_sample_model("config/relationships/example_retail.yaml")
    assert is_sample_model("config/relationships/retail.example.yaml")
    assert is_sample_model("config/relationships/gcp_public_fk_example.yaml")
    assert not is_sample_model("config/relationships/examples_catalog.yaml")
    assert not is_sample_model("config/relationships/retail.yaml")
