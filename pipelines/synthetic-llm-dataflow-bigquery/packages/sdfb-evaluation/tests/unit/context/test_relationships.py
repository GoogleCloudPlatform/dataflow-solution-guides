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
"""Unit tests for `sdfb_evaluation.context.relationships`.

These exercise the mirror directly, on small hand-written models, beyond
what the golden-file parity test (`test_parity_goldens.py`) covers off
the three committed example models and the goldens script's own invented
cases: the widening tests here (`test_enforced_edges_widens_...`) are a
second proof of the path that script's own invented widening model pins,
written independently of it; the `parse_model` strictness tests duplicate
the goldens script's `_PARSE_CASES` as direct, readable unit tests, plus
the non-string-key cases (int/bool/null/float YAML keys) the original
rejects as a validation error; and the `load_models` gs://-seam, dotfile
and lazy-import tests have no golden-file counterpart at all (they are
about THIS module's own IO plumbing, not about parity with the original).

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from sdfb_evaluation.context import relationships
from sdfb_evaluation.context.relationships import (
    Edge,
    RelationshipError,
    component,
    derived_widenings,
    enforced_edges,
    from_sources,
    generation_order,
    is_sample_model,
    load_models,
    model_for,
    parse_model,
    sha12,
)

# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


def test_edge_external_is_true_only_for_a_dataset_qualified_ref():
  internal = Edge(cols=("C1",), ref="A", ref_cols=("A1",))
  external = Edge(cols=("C1",), ref="ds.EXT", ref_cols=("E1",))
  assert internal.external is False
  assert external.external is True


def test_edge_ref_name_strips_the_dataset_prefix():
  assert Edge(cols=("C1",), ref="ds.EXT", ref_cols=("E1",)).ref_name == "EXT"
  assert Edge(cols=("C1",), ref="A", ref_cols=("A1",)).ref_name == "A"


def test_edge_label():
  edge = Edge(cols=("C1", "C2"), ref="A", ref_cols=("A1", "A2"))
  assert edge.label("C") == "C(C1,C2) -> A(A1,A2)"


# ---------------------------------------------------------------------------
# parse_model — happy path
# ---------------------------------------------------------------------------


def test_parse_model_reads_pk_identity_and_fk():
  text = ("model: retail\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1, A2]\n"
          "    identity: [A9]\n"
          "  B:\n"
          "    pk: [B1]\n"
          "    fk:\n"
          "      - cols: [B6, B7]\n"
          "        ref: A\n"
          "        ref_cols: [A1, A2]\n")
  model = parse_model(text, source="x.yaml")
  assert model.model == "retail"
  assert model.source == "x.yaml"
  assert model.tables["A"].pk == ("A1", "A2")
  assert model.tables["A"].identity == ("A9",)
  assert model.tables["A"].enabled is True
  edge = model.tables["B"].fk[0]
  assert edge == Edge(cols=("B6", "B7"), ref="A", ref_cols=("A1", "A2"))


def test_parse_model_reads_enabled_enforced_and_drives_flags():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    enabled: false\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n"
          "        enforced: false\n"
          "        drives: true\n")
  model = parse_model(text, source="x")
  assert model.tables["B"].enabled is False
  edge = model.tables["B"].fk[0]
  assert edge.enforced is False
  assert edge.drives is True


def test_parse_model_accepts_an_external_ref():
  text = ("model: m\n"
          "tables:\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: warehouse_ds.EXT\n"
          "        ref_cols: [E1]\n")
  model = parse_model(text, source="x")
  assert model.tables["B"].fk[0].external is True


# ---------------------------------------------------------------------------
# parse_model — error paths
# ---------------------------------------------------------------------------


def test_parse_model_rejects_invalid_yaml():
  with pytest.raises(RelationshipError):
    parse_model(": not [ valid yaml", source="x")


def test_parse_model_rejects_a_non_mapping_top_level():
  with pytest.raises(RelationshipError):
    parse_model("- a\n- list\n", source="x")


def test_parse_model_requires_a_model_name():
  with pytest.raises(RelationshipError):
    parse_model("tables: {}\n", source="x")


def test_parse_model_rejects_self_reference():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "    fk:\n"
          "      - cols: [A1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


def test_parse_model_rejects_arity_mismatch():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [B1, B2]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


def test_parse_model_rejects_an_unknown_internal_ref():
  text = ("model: m\n"
          "tables:\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: GHOST\n"
          "        ref_cols: [G1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


def test_parse_model_rejects_a_duplicate_edge():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n"
          "      - cols: [B1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


# ---------------------------------------------------------------------------
# parse_model — strictness, pinned against the originals' pydantic models
# empirically (see scripts/evaluation/make_parity_goldens.py's
# `_PARSE_CASES`, which runs the SAME shapes through the real
# `parse_relationship_model`; this file duplicates the interesting ones
# as direct, readable unit tests rather than only via the golden).
# ---------------------------------------------------------------------------


def test_parse_model_accepts_an_empty_string_model_id():
  # `model: str` has no length constraint upstream — only ABSENCE of the
  # key is rejected ("Field required"), not an empty value.
  model = parse_model("model: ''\ntables: {}\n", source="x")
  assert model.model == ""


def test_parse_model_rejects_a_non_string_model_id():
  # Pydantic's `str` fields do not coerce int/float in this configuration
  # (confirmed empirically: `model: 7` -> "Input should be a valid
  # string", not silently str()-ed).
  with pytest.raises(RelationshipError):
    parse_model("model: 7\ntables: {}\n", source="x")


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("'false'", False),
        ("'FALSE'", False),  # case-insensitive, no whitespace stripping
        ("0", False),
        ("1", True),
        ("'yes'", True),
        ("'on'", True),
        ("'no'", False),
        ("'off'", False),
        ("'t'", True),
        ("'f'", False),
    ],
)
def test_parse_model_reads_booleans_the_way_pydantic_lax_mode_does(
    literal: str, expected: bool):
  model = parse_model(
      f"model: m\ntables:\n  A:\n    enabled: {literal}\n", source="x")
  assert model.tables["A"].enabled is expected


def test_parse_model_rejects_an_out_of_vocabulary_int_boolean():
  # Only literal 0/1 coerce; pydantic rejects `enabled: 2` outright
  # ("unable to interpret input").
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    enabled: 2\n", source="x")


def test_parse_model_rejects_a_whitespace_padded_boolean_string():
  # No stripping: ' true ' is rejected even though 'true' alone accepts.
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    enabled: ' true '\n", source="x")


def test_parse_model_rejects_a_scalar_pk():
  # Pydantic never treats `str` as an implicit sequence of its own
  # characters for a `tuple[str, ...]` field.
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    pk: A1\n", source="x")


def test_parse_model_rejects_a_pk_list_of_non_strings():
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    pk: [1, 2]\n", source="x")


def test_parse_model_accepts_an_empty_pk_list():
  model = parse_model("model: m\ntables:\n  A:\n    pk: []\n", source="x")
  assert not model.tables["A"].pk


def test_parse_model_rejects_an_explicit_null_pk():
  # Absence uses the default `()`; an explicit `null` overrides the
  # default and fails the (non-Optional) tuple validation.
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    pk: null\n", source="x")


def test_parse_model_accepts_a_pk_element_that_is_an_empty_string():
  # No non-empty-string validator on TableRelations.pk/identity (that
  # validator lives only on FkEdge.cols/ref_cols) — confirmed empirically.
  model = parse_model(
      "model: m\ntables:\n  A:\n    pk: ['A1', '']\n", source="x")
  assert model.tables["A"].pk == ("A1", "")


def test_parse_model_rejects_an_unknown_table_key():
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A:\n    unknown_key: 1\n", source="x")


def test_parse_model_rejects_an_unknown_top_level_key():
  with pytest.raises(RelationshipError):
    parse_model("model: m\nextra_top: 1\ntables: {}\n", source="x")


def test_parse_model_rejects_an_unknown_fk_key():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    fk:\n"
          "      - cols: [A1]\n"
          "        ref: B\n"
          "        ref_cols: [B1]\n"
          "        unknown: 1\n"
          "  B:\n"
          "    pk: [B1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


@pytest.mark.parametrize(
    "text",
    [
        # top level, table level and edge level: an int key next to a
        # string key cannot be ordered against it
        "model: m\n1: x\nzzz: y\ntables: {}\n",
        "model: m\ntables:\n  A:\n    1: x\n    zzz: y\n",
        ("model: m\ntables:\n  B:\n    pk: [B1]\n  A:\n    fk:\n"
         "      - cols: [A1]\n        ref: B\n        ref_cols: [B1]\n"
         "        1: x\n        zz: y\n"),
    ],
    ids=["model", "table", "edge"])
def test_parse_model_rejects_mixed_type_unknown_keys_as_relationship_error(
    text: str):
  # The original's pydantic models reject these as a validation error
  # (`RelationshipError`); a bare `sorted()` over mixed key types would
  # escape as a `TypeError` instead.
  with pytest.raises(RelationshipError, match="unexpected field"):
    parse_model(text, source="x")


@pytest.mark.parametrize("key", ["1", "true", "~", "1.5"])
def test_parse_model_rejects_a_non_string_table_name(key: str):
  # `tables: dict[str, ...]` in the original: YAML's int, bool, null and
  # float keys are not strings (pydantic does not coerce them), so they
  # are rejected up front — not later as an AttributeError on `.rsplit`.
  with pytest.raises(RelationshipError, match="table name"):
    parse_model(f"model: m\ntables:\n  {key}:\n    pk: [A1]\n", source="x")


def test_parse_model_rejects_an_empty_fk_cols_list():
  text = ("model: m\n"
          "tables:\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: []\n"
          "        ref: A\n"
          "        ref_cols: []\n"
          "  A: {}\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


def test_parse_model_rejects_fk_declared_as_a_mapping():
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  B:\n    fk: {}\n", source="x")


def test_parse_model_rejects_an_explicit_null_table_value():
  with pytest.raises(RelationshipError):
    parse_model("model: m\ntables:\n  A: null\n", source="x")


def test_parse_model_accepts_tables_omitted_entirely():
  model = parse_model("model: m\n", source="x")
  assert model.tables == {}


def test_parse_model_rejects_fk_missing_the_ref_key():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    fk:\n"
          "      - cols: [A1]\n"
          "        ref_cols: [A1]\n")
  with pytest.raises(RelationshipError):
    parse_model(text, source="x")


# ---------------------------------------------------------------------------
# from_sources — the from_sources-time checks (duplicate table across
# models, an FK cycle caught at load rather than lazily)
# ---------------------------------------------------------------------------


def test_from_sources_rejects_a_table_declared_in_two_models():
  sources = [
      ("a.yaml", "model: m1\ntables:\n  A:\n    pk: [A1]\n"),
      ("b.yaml", "model: m2\ntables:\n  A:\n    pk: [A1]\n"),
  ]
  with pytest.raises(RelationshipError):
    from_sources(sources)


def test_from_sources_accepts_the_same_table_across_two_calls_independently():
  # Sanity: it is specifically COMBINING them in one from_sources call
  # that is rejected, not the table name itself.
  one = from_sources([("a.yaml", "model: m1\ntables:\n  A:\n    pk: [A1]\n")])
  two = from_sources([("b.yaml", "model: m2\ntables:\n  A:\n    pk: [A1]\n")])
  assert [m.model for m in one] == ["m1"]
  assert [m.model for m in two] == ["m2"]


def test_from_sources_rejects_an_enforced_edge_cycle_at_load_time():
  # `parse_model` alone never sees the cycle (each table parses fine on
  # its own); it is `from_sources` that proactively calls
  # `generation_order(component(t))` for every table, so this raises
  # here, not lazily the first time a caller happens to ask for the
  # generation order.
  cyclic = [("a.yaml", "model: m\ntables:\n"
             "  A:\n    fk:\n      - cols: [X]\n        ref: B\n"
             "        ref_cols: [X]\n"
             "  B:\n    fk:\n      - cols: [X]\n        ref: A\n"
             "        ref_cols: [X]\n")]
  with pytest.raises(RelationshipError):
    from_sources(cyclic)


# ---------------------------------------------------------------------------
# is_sample_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("config/relationships/example_retail.yaml", True),
        ("config/relationships/foo.example.yaml", True),
        ("config/relationships/bar_example.yml", True),
        ("config/relationships/retail_private.yaml", False),
        ("config/relationships/gcp_public_fk_example.yaml", True),
        ("gs://bucket/models/retail.yaml", False),
    ],
)
def test_is_sample_model(path: str, expected: bool):
  assert is_sample_model(path) is expected


# ---------------------------------------------------------------------------
# load_models — injectable IO, no real filesystem or GCS
# ---------------------------------------------------------------------------


def test_load_models_empty_uri_means_relationships_are_off():
  assert not load_models("")


def test_load_models_local_directory_skips_samples():
  files = {
      "config/relationships/real.yaml":
          "model: real\ntables:\n  A:\n    pk: [A1]\n",
      "config/relationships/example_demo.yaml": ("model: demo\ntables:\n"
                                                 "  B:\n    pk: [B1]\n"),
  }

  def lister(pattern: str) -> list[str]:
    base, suffix = pattern.rsplit("*", 1)
    return [p for p in files if p.startswith(base) and p.endswith(suffix)]

  models = load_models(
      "config/relationships",
      reader=files.__getitem__,
      lister=lister,
  )
  assert [m.model for m in models] == ["real"]


def test_load_models_direct_file_loads_a_sample():
  files = {
      "config/relationships/example_demo.yaml": ("model: demo\ntables:\n"
                                                 "  B:\n    pk: [B1]\n"),
  }
  models = load_models(
      "config/relationships/example_demo.yaml",
      reader=files.__getitem__,
      lister=lambda pattern: [pattern],
  )
  assert [m.model for m in models] == ["demo"]


def test_load_models_gcs_uri_uses_injected_io_only():
  # No real GCS access here: `load_models` only reaches for
  # `apache_beam.io.filesystems` (lazily) when `reader`/`lister` are left
  # `None`; passing both means that branch is never entered.
  def lister(pattern: str) -> list[str]:
    assert pattern.startswith("gs://bucket/models/")
    return (["gs://bucket/models/real.yaml"]
            if pattern.endswith(".yaml") else [])

  def reader(path: str) -> str:
    assert path == "gs://bucket/models/real.yaml"
    return "model: real\ntables:\n  A:\n    pk: [A1]\n"

  models = load_models("gs://bucket/models", reader=reader, lister=lister)
  assert [m.model for m in models] == ["real"]


def test_load_models_default_io_reads_real_local_files(tmp_path):
  (tmp_path / "real.yaml").write_text(
      "model: real\ntables:\n  A:\n    pk: [A1]\n", encoding="utf-8")
  (tmp_path / "example_demo.yaml").write_text(
      "model: demo\ntables:\n  B:\n    pk: [B1]\n", encoding="utf-8")
  models = load_models(str(tmp_path))
  assert [m.model for m in models] == ["real"]


def test_load_models_default_io_includes_dotfiles_like_filesystems_match(
    tmp_path):
  # `glob.glob`'s bare `*` hides dotfiles by default; Beam's
  # `FileSystems.match` does not — the local branch must agree with the
  # gs:// branch on what `*.yaml` means.
  (tmp_path / ".hidden.yaml").write_text(
      "model: hidden\ntables:\n  A:\n    pk: [A1]\n", encoding="utf-8")
  models = load_models(str(tmp_path))
  assert [m.model for m in models] == ["hidden"]


def test_load_models_explicit_directory_uri_with_no_files_is_an_error(tmp_path):
  # Unlike the ORIGINAL's loader (which special-cases its packaged
  # default directory), this mirror has no such default: ANY non-empty
  # `uri` that resolves to zero files is an error, never a silent `()`.
  with pytest.raises(RelationshipError):
    load_models(str(tmp_path))


def test_load_models_missing_direct_file_is_an_error(tmp_path):
  missing = tmp_path / "does_not_exist.yaml"
  with pytest.raises(RelationshipError):
    load_models(str(missing))


def test_load_models_explicit_uri_with_no_files_is_an_error_via_injected_io():
  with pytest.raises(RelationshipError):
    load_models(
        "config/relationships",
        reader=lambda _p: "",
        lister=lambda _pattern: [],
    )


def test_default_lister_uses_filesystems_match_for_gcs(
    monkeypatch: pytest.MonkeyPatch):
  from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel

  class _Metadata:

    def __init__(self, path: str):
      self.path = path

  class _MatchResult:

    def __init__(self, paths: list[str]):
      self.metadata_list = [_Metadata(p) for p in paths]

  def fake_match(patterns: list[str]) -> list[_MatchResult]:
    assert patterns == ["gs://bucket/models/*.yaml"]
    return [
        _MatchResult(["gs://bucket/models/b.yaml", "gs://bucket/models/a.yaml"])
    ]

  monkeypatch.setattr(FileSystems, "match", staticmethod(fake_match))
  paths = relationships._default_lister(  # pylint: disable=protected-access
      "gs://bucket/models/*.yaml")
  assert paths == ["gs://bucket/models/a.yaml", "gs://bucket/models/b.yaml"]


def test_default_reader_uses_filesystems_open_for_gcs(
    monkeypatch: pytest.MonkeyPatch):
  import io  # pylint: disable=import-outside-toplevel

  from apache_beam.io.filesystems import FileSystems  # pylint: disable=import-outside-toplevel

  def fake_open(path: str) -> io.BytesIO:
    assert path == "gs://bucket/models/a.yaml"
    return io.BytesIO(b"model: real\ntables: {}\n")

  monkeypatch.setattr(FileSystems, "open", staticmethod(fake_open))
  text = relationships._default_reader(  # pylint: disable=protected-access
      "gs://bucket/models/a.yaml")
  assert text == "model: real\ntables: {}\n"


def test_importing_the_module_does_not_import_apache_beam():
  # The lazy-import discipline (`_default_lister`/`_default_reader` only
  # reach for `apache_beam.io.filesystems` inside their gs:// branch) is
  # only meaningful if merely IMPORTING this module leaves `apache_beam`
  # untouched — checked in a subprocess so this test's own import of
  # `sdfb_evaluation.context.relationships` (already done at module
  # level, above) cannot have pre-polluted `sys.modules`.
  script = ("import sys\n"
            "import sdfb_evaluation.context.relationships\n"
            "print('apache_beam' in sys.modules)\n")
  result = subprocess.run(
      [sys.executable, "-c", script],
      capture_output=True,
      text=True,
      check=True,
  )
  assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# model_for
# ---------------------------------------------------------------------------


def test_model_for_resolves_bare_and_fqn_table_names():
  model = parse_model("model: m\ntables:\n  A:\n    pk: [A1]\n", source="x")
  assert model_for([model], "A") is model
  assert model_for([model], "ds.A") is model
  assert model_for([model], "GHOST") is None


# ---------------------------------------------------------------------------
# component / generation_order — graph traversal
# ---------------------------------------------------------------------------


def _chain_model():
  text = ("model: chain\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    pk: [B1]\n"
          "    fk:\n"
          "      - cols: [B2]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n"
          "  C:\n"
          "    pk: [C1]\n"
          "    fk:\n"
          "      - cols: [C2]\n"
          "        ref: B\n"
          "        ref_cols: [B1]\n")
  return parse_model(text, source="x")


def test_component_and_generation_order_over_a_parent_child_chain():
  model = _chain_model()
  assert component([model], "A") == ("A", "B", "C")
  assert component([model], "C") == ("A", "B", "C")
  assert generation_order([model], component([model], "A")) == ("A", "B", "C")


def test_component_detaches_through_a_disabled_parent():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    enabled: false\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n"
          "  C:\n"
          "    fk:\n"
          "      - cols: [C1]\n"
          "        ref: B\n"
          "        ref_cols: [B1]\n")
  model = parse_model(text, source="x")
  # C's only edge targets a DISABLED parent, so it is never drawn as an
  # adjacency, and C ends up isolated too — matching
  # `RelationshipRegistry._adjacency`'s `self.enabled(edge.ref)` check.
  assert component([model], "A") == ("A",)
  assert component([model], "C") == ("C",)


def test_component_isolates_a_table_with_only_an_external_ref():
  text = ("model: m\n"
          "tables:\n"
          "  E:\n"
          "    pk: [E1]\n"
          "    fk:\n"
          "      - cols: [E2]\n"
          "        ref: warehouse.EXT\n"
          "        ref_cols: [X1]\n")
  model = parse_model(text, source="x")
  assert component([model], "E") == ("E",)


def test_generation_order_detects_an_enforced_edge_cycle():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    fk:\n"
          "      - cols: [X]\n"
          "        ref: B\n"
          "        ref_cols: [X]\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [X]\n"
          "        ref: A\n"
          "        ref_cols: [X]\n")
  model = parse_model(text, source="x")
  with pytest.raises(RelationshipError):
    generation_order([model], component([model], "A"))


# ---------------------------------------------------------------------------
# enforced_edges / derived_widenings
# ---------------------------------------------------------------------------


def test_enforced_edges_drops_documented_only_edges():
  text = ("model: m\n"
          "tables:\n"
          "  A:\n"
          "    pk: [A1]\n"
          "  B:\n"
          "    fk:\n"
          "      - cols: [B1]\n"
          "        ref: A\n"
          "        ref_cols: [A1]\n"
          "        enforced: false\n")
  model = parse_model(text, source="x")
  assert not enforced_edges([model], "B")


def _widening_model():
  # G has two key columns; P's own edge to G only covers G1. Child C
  # references the SAME column (CG) on both P (via P's other column PG2)
  # and G (via G2) — the model asserts CG == P.PG2 == G.G2, so P's edge to
  # G must be widened to carry (PG2 -> G2) too.
  text = ("model: widen\n"
          "tables:\n"
          "  G:\n"
          "    pk: [G1, G2]\n"
          "  P:\n"
          "    pk: [PG1]\n"
          "    fk:\n"
          "      - cols: [PG1]\n"
          "        ref: G\n"
          "        ref_cols: [G1]\n"
          "  C:\n"
          "    pk: [CG]\n"
          "    fk:\n"
          "      - cols: [CG]\n"
          "        ref: P\n"
          "        ref_cols: [PG2]\n"
          "        drives: true\n"
          "      - cols: [CG]\n"
          "        ref: G\n"
          "        ref_cols: [G2]\n")
  return parse_model(text, source="x")


def test_enforced_edges_widens_the_parents_edge_to_the_grandparent():
  model = _widening_model()
  edges = enforced_edges([model], "P")
  assert edges == (Edge(cols=("PG1", "PG2"), ref="G", ref_cols=("G1", "G2")),)


def test_derived_widenings_reports_the_same_widening():
  model = _widening_model()
  assert derived_widenings([model]) == [{
      "table": "P",
      "ref": "G",
      "via": "C",
      "added": [("PG2", "G2")],
  }]


def test_enforced_edges_of_the_child_itself_are_unaffected_by_widening():
  model = _widening_model()
  edges = enforced_edges([model], "C")
  assert edges == (
      Edge(cols=("CG",), ref="P", ref_cols=("PG2",), drives=True),
      Edge(cols=("CG",), ref="G", ref_cols=("G2",)),
  )


def test_generation_order_respects_a_widened_edge():
  model = _widening_model()
  component_tables = component([model], "C")
  assert component_tables == ("G", "P", "C")
  assert generation_order([model], component_tables) == ("G", "P", "C")


# ---------------------------------------------------------------------------
# sha12
# ---------------------------------------------------------------------------


def test_sha12_is_a_twelve_char_hex_digest():
  model = _chain_model()
  digest = sha12([model])
  assert len(digest) == 12
  assert all(c in "0123456789abcdef" for c in digest)


def test_sha12_is_stable_for_identical_content():
  first = _chain_model()
  second = _chain_model()
  assert sha12([first]) == sha12([second])


def test_sha12_changes_when_content_changes():
  base = sha12([_chain_model()])
  text = (
      "model: chain\n"
      "tables:\n"
      "  A:\n"
      "    pk: [A1, A9]\n"  # widened PK
      "  B:\n"
      "    pk: [B1]\n"
      "    fk:\n"
      "      - cols: [B2]\n"
      "        ref: A\n"
      "        ref_cols: [A1]\n"
      "  C:\n"
      "    pk: [C1]\n"
      "    fk:\n"
      "      - cols: [C2]\n"
      "        ref: B\n"
      "        ref_cols: [B1]\n")
  changed = sha12([parse_model(text, source="x")])
  assert changed != base
