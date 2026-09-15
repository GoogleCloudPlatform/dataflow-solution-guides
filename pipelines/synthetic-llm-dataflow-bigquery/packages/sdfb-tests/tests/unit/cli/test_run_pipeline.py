"""Unit tests for `sdfb_beam.cli.run_pipeline` — arg parsing and factory."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,redefined-outer-name,reimported,unused-argument,unused-variable,use-implicit-booleaness-not-comparison

from __future__ import annotations

import re

import pytest
from apache_beam.options.pipeline_options import (
    GoogleCloudOptions,
    PipelineOptions,
    SetupOptions,
    WorkerOptions,
)
from sdfb_beam.cli.run_pipeline import (
    _DEFAULT_STATE_CACHE_MB,
    _DEFAULT_WORKER_DISK_GB,
    build_model_client,
    configure_pipeline_options,
    parse_args,
    parse_bool_flag,
    resolve_engine_strictness,
    resolve_landing_dispositions,
)
from sdfb_core.codegen import derive_bq_load_schema
from sdfb_core.contracts import TableSchema


def _common_args() -> list[str]:
  return [
      "--ddl_uri",
      "gs://bucket/ddl.json",
      "--reference_table",
      "p.d.t",
      "--landing_table",
      "p.d.landing",
      "--dlq_table",
      "p.d.dlq",
      "--num_rows",
      "100",
      "--run_id",
      "abc-123",
      "--model_uri",
      "gs://bucket/models/gemma4/e4b-it/v1/",
  ]


def test_parse_args_minimal():
  args, beam_argv = parse_args(_common_args())
  assert args.ddl_uri == "gs://bucket/ddl.json"
  assert args.num_rows == 100
  assert args.engine == "b1_rag"  # default
  assert args.batch_size == 16  # default
  assert args.similarity == 0.5  # default
  assert args.client_type == "vllm"  # default
  assert beam_argv == []


def test_parse_args_overrides():
  argv = [
      *_common_args(),
      "--engine",
      "b2_library",
      "--batch_size",
      "32",
      "--similarity",
      "0.9",
      "--client_type",
      "fake",
      "--reference_rows_limit",
      "500",
  ]
  args, _ = parse_args(argv)
  assert args.engine == "b2_library"
  assert args.batch_size == 32
  assert args.similarity == 0.9
  assert args.client_type == "fake"
  assert args.reference_rows_limit == 500


def test_parse_args_passes_unknown_to_beam():
  argv = [*_common_args(), "--runner", "DirectRunner", "--project", "demo"]
  _, beam_argv = parse_args(argv)
  assert "--runner" in beam_argv
  assert "DirectRunner" in beam_argv


def test_parse_args_identity_cols_uses_underscore_flag():
  """Flex Template launchers pass underscore-named params
    (``--identity_cols=...``, never ``--identity-cols``). Every sibling flag
    on this parser uses underscores; if this one didn't match, argparse's
    `parse_known_args` would silently divert it into `beam_argv` instead of
    populating `args.identity_cols`, and the value would never reach
    `PipelineConfig.identity_columns`."""
  argv = [*_common_args(), "--identity_cols", "customer_id,email"]
  args, beam_argv = parse_args(argv)
  assert args.identity_cols == "customer_id,email"
  assert beam_argv == []


def test_parse_args_identity_cols_defaults_empty():
  args, _ = parse_args(_common_args())
  assert args.identity_cols == ""


def test_parse_args_pk_cols_uses_underscore_flag():
  """Flex Template launchers pass underscore-named params
    (``--pk_cols=...``, never ``--pk-cols``). Every sibling flag
    on this parser uses underscores; if this one didn't match, argparse's
    `parse_known_args` would silently divert it into `beam_argv` instead of
    populating `args.pk_cols`, and the value would never reach
    `PipelineConfig.pk_columns`."""
  argv = [*_common_args(), "--pk_cols", "id,sku"]
  args, beam_argv = parse_args(argv)
  assert args.pk_cols == "id,sku"
  assert beam_argv == []


def test_parse_args_pk_cols_defaults_empty():
  args, _ = parse_args(_common_args())
  assert args.pk_cols == ""


def test_parse_args_rejects_unknown_engine_value():
  """argparse-level rejection of bad client_type; engine is free-form."""
  argv = [*_common_args(), "--client_type", "made-up"]
  with pytest.raises(SystemExit):
    parse_args(argv)


def test_build_model_client_fake():
  """Fake client builds without touching vLLM / MLX imports."""
  c = build_model_client("fake", "ignored")
  assert c is not None


def test_build_model_client_vllm_stub_does_not_fail_on_import():
  """The vllm_client stub must be importable on M4 laptop (no vllm dep).
    Constructor succeeds; generate_json raises NotImplementedError but is
    not exercised here."""
  c = build_model_client("vllm", "gs://bucket/models/foo/")
  assert c.model_uri == "gs://bucket/models/foo/"


def test_build_model_client_rejects_unknown():
  with pytest.raises(ValueError, match="Unknown client_type"):
    build_model_client("openai", "ignored")


def test_configure_options_directrunner_sets_save_main_session():
  """Regression: save_main_session lives on SetupOptions, not GoogleCloudOptions."""
  opts = PipelineOptions(["--runner=DirectRunner"])
  configure_pipeline_options(opts, "DirectRunner", "r1")
  assert opts.view_as(SetupOptions).save_main_session is True


def test_configure_options_dataflow_sets_job_name_not_save_main_session():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(SetupOptions).save_main_session is False
  assert opts.view_as(GoogleCloudOptions).job_name == "sdfb-abc-123"


def test_configure_options_sanitizes_airflow_run_id():
  """Regression: Airflow run_ids carry :/+/__ that Dataflow job names reject."""
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner",
                             "scheduled__2026-05-20T00:00:00+00:00")
  name = opts.view_as(GoogleCloudOptions).job_name
  assert name == "sdfb-scheduled-2026-05-20t00-00-00-00-00"
  assert re.fullmatch(r"[a-z][-a-z0-9]*[a-z0-9]", name)  # Dataflow constraint


def test_configure_options_preserves_launcher_job_name():
  """The flex launcher's valid --job_name must not be overridden."""
  opts = PipelineOptions(
      ["--runner=DataflowRunner", "--job_name=synthetic-sdfb-vlatest-e48544ec"])
  configure_pipeline_options(opts, "DataflowRunner", "scheduled__bad:name")
  assert opts.view_as(
      GoogleCloudOptions).job_name == "synthetic-sdfb-vlatest-e48544ec"


# --- sdk_container_image: pin Runner v2 workers to THIS image -----------------
# Without it, Dataflow boots workers on the stock Beam SDK container (no
# sdfb_core/sdfb_beam) and DoFn unpickling dies with ModuleNotFoundError. The
# image bakes its own pushed coordinate into SDFB_SDK_CONTAINER_IMAGE so the
# sha-free Flex Template / DAG never has to carry it.

_IMG = "artifactory.example/docker-local/ns/sdfb-python:main-abc1234"


def test_configure_options_dataflow_sets_sdk_container_image_from_env(
    monkeypatch):
  monkeypatch.setenv("SDFB_SDK_CONTAINER_IMAGE", _IMG)
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(WorkerOptions).sdk_container_image == _IMG


def test_configure_options_dataflow_respects_explicit_sdk_container_image(
    monkeypatch):
  """An explicit --sdk_container_image (e.g. the probe) must win over the bake."""
  monkeypatch.setenv("SDFB_SDK_CONTAINER_IMAGE", _IMG)
  opts = PipelineOptions(
      ["--runner=DataflowRunner", "--sdk_container_image=other/image:explicit"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(
      WorkerOptions).sdk_container_image == "other/image:explicit"


def test_configure_options_directrunner_ignores_sdk_container_image_env(
    monkeypatch):
  monkeypatch.setenv("SDFB_SDK_CONTAINER_IMAGE", _IMG)
  opts = PipelineOptions(["--runner=DirectRunner"])
  configure_pipeline_options(opts, "DirectRunner", "r1")
  assert opts.view_as(WorkerOptions).sdk_container_image is None


def test_configure_options_dataflow_no_env_leaves_sdk_container_image_unset(
    monkeypatch):
  monkeypatch.delenv("SDFB_SDK_CONTAINER_IMAGE", raising=False)
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(WorkerOptions).sdk_container_image is None


# --- disk_size_gb: pin the worker boot disk in code --------------------------
# The GPU image is multi-GB and overflows Dataflow's 25GB default while the
# kubelet unpacks it. The Flex Template environment.diskSizeGb does NOT reach the
# worker harness, so configure_pipeline_options pins it the same way it pins
# sdk_container_image.


def test_configure_options_dataflow_pins_default_disk_size():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(WorkerOptions).disk_size_gb == _DEFAULT_WORKER_DISK_GB


def test_configure_options_dataflow_respects_explicit_disk_size():
  """An explicit --disk_size_gb must win over the baked default."""
  opts = PipelineOptions(["--runner=DataflowRunner", "--disk_size_gb=500"])
  configure_pipeline_options(opts, "DataflowRunner", "abc-123")
  assert opts.view_as(WorkerOptions).disk_size_gb == 500


def test_configure_options_directrunner_leaves_disk_size_unset():
  opts = PipelineOptions(["--runner=DirectRunner"])
  configure_pipeline_options(opts, "DirectRunner", "r1")
  assert opts.view_as(WorkerOptions).disk_size_gb is None


def test_parse_args_vllm_dtype_defaults_auto():
  args, _ = parse_args(_common_args())
  assert args.vllm_dtype == "auto"


def test_parse_args_vllm_dtype_accepts_float16():
  args, beam_argv = parse_args([*_common_args(), "--vllm_dtype", "float16"])
  assert args.vllm_dtype == "float16"
  assert beam_argv == []


def test_build_model_client_vllm_passes_dtype_override():
  client = build_model_client("vllm", "gs://b/m/v1/", vllm_dtype="float16")
  assert client.vllm_server_kwargs.get("dtype") == "float16"


def test_build_model_client_vllm_auto_dtype_sends_no_flag():
  client = build_model_client("vllm", "gs://b/m/v1/")
  assert "dtype" not in client.vllm_server_kwargs


# --- vllm_max_model_len: cap the KV-cache context allocation ------------------
# Qwen3-4B-Instruct-2507 ships max_position_embeddings=262144; vLLM defaults
# max_model_len to that and needs a 36GiB KV cache — a T4 has ~5GiB free after
# weights, so the EngineCore dies at startup (E2E 2026-07-14, R-run on T4).
# The registry default in config/models.yml (max_model_len: 8192) is
# documentation-only; the cap must be plumbed CLI → Flex Template → DAG like
# vllm_dtype was.


def test_parse_args_vllm_max_model_len_defaults_8192():
  args, _ = parse_args(_common_args())
  assert args.vllm_max_model_len == "8192"


def test_parse_args_vllm_max_model_len_accepts_override():
  args, beam_argv = parse_args(
      [*_common_args(), "--vllm_max_model_len", "16384"])
  assert args.vllm_max_model_len == "16384"
  assert beam_argv == []


def test_build_model_client_vllm_passes_max_model_len():
  client = build_model_client("vllm", "gs://b/m/v1/", vllm_max_model_len="8192")
  assert client.vllm_server_kwargs.get("max-model-len") == "8192"


def test_build_model_client_vllm_empty_max_model_len_sends_no_flag():
  """Empty = escape hatch: let vLLM use the checkpoint's native context."""
  client = build_model_client("vllm", "gs://b/m/v1/", vllm_max_model_len="")
  assert "max-model-len" not in client.vllm_server_kwargs


def test_build_model_client_vllm_combines_dtype_and_max_model_len():
  """The T4+Qwen profile needs BOTH flags on the server command."""
  client = build_model_client(
      "vllm",
      "gs://b/m/v1/",
      vllm_dtype="float16",
      vllm_max_model_len="8192",
  )
  assert client.vllm_server_kwargs.get("dtype") == "float16"
  assert client.vllm_server_kwargs.get("max-model-len") == "8192"


def test_build_model_client_vllm_rejects_non_integer_max_model_len():
  """Fail at launch, not 10 minutes later inside DoFn.setup() on a GPU
    worker — a garbled value would otherwise ride the Flex Template all the
    way to the vLLM server spawn."""
  with pytest.raises(ValueError, match="vllm_max_model_len"):
    build_model_client("vllm", "gs://b/m/v1/", vllm_max_model_len="lots")


def test_parse_args_seed_uses_underscore_flag():
  """Flex Template launchers pass underscore-named params
    (``--seed=...``). Every sibling flag on this parser uses underscores;
    if this one didn't match, argparse's `parse_known_args` would silently
    divert it into `beam_argv` instead of populating `args.seed`, and the
    value would never reach `PipelineConfig.seed`."""
  argv = [*_common_args(), "--seed", "42"]
  args, beam_argv = parse_args(argv)
  assert args.seed == "42"
  assert beam_argv == []


def test_parse_args_seed_defaults_empty():
  args, _ = parse_args(_common_args())
  assert args.seed == ""


@pytest.mark.parametrize(
    "client_type,expected",
    [
        ("vllm", True),
        ("mlx", True),
        ("fake", False),
    ],
)
def test_resolve_engine_strictness(client_type, expected):
  """vllm and mlx are real-LLM paths (Dataflow/L4 and M4 DirectRunner
    respectively) — a failed generation must be loud, not silently
    degrade into memorized reference data. Only the deterministic fake
    client (CPU smoke) stays lenient."""
  assert resolve_engine_strictness(client_type) is expected


def test_parse_args_rag_layer_flags_default_off():
  args, _ = parse_args(_common_args())
  assert parse_bool_flag(args.build_rag_layer) is False
  assert args.rag_chunks_table == ""


def test_parse_args_rag_layer_flags():
  argv = [
      *_common_args(),
      "--build_rag_layer",
      "--rag_chunks_table",
      "proj.synthetic_rag.rag_chunks",
  ]
  args, _ = parse_args(argv)
  assert parse_bool_flag(args.build_rag_layer) is True
  assert args.rag_chunks_table == "proj.synthetic_rag.rag_chunks"


def test_parse_args_build_rag_layer_accepts_flex_template_value():
  """Flex Templates pass every parameter as --name=value — the bare
    store_true form can't receive one, so the flag must accept true/false
    strings too (composer DAG: build_rag_layer param)."""
  argv = [
      *_common_args(),
      "--build_rag_layer=true",
      "--rag_chunks_table=proj.synthetic_rag.rag_chunks",
  ]
  args, _ = parse_args(argv)
  assert parse_bool_flag(args.build_rag_layer) is True
  argv = [
      *_common_args(),
      "--build_rag_layer=false",
      "--rag_chunks_table=proj.synthetic_rag.rag_chunks",
  ]
  args, _ = parse_args(argv)
  assert parse_bool_flag(args.build_rag_layer) is False


def test_parse_args_build_rag_layer_requires_table():
  with pytest.raises(SystemExit):
    parse_args([*_common_args(), "--build_rag_layer"])


def test_parse_args_write_disposition_default_and_choices():
  args, _ = parse_args(_common_args())
  assert args.write_disposition == "append"
  assert args.create_if_not_exists == "false"
  args, _ = parse_args([*_common_args(), "--write_disposition", "overwrite"])
  assert args.write_disposition == "overwrite"
  with pytest.raises(SystemExit):
    parse_args([*_common_args(), "--write_disposition", "truncate"])


def test_landing_sink_schema_uses_load_safe_projection(narrow_ddl_dict):
  """WS4 final-review CRITICAL-1: the landing sink for CREATE_IF_NEEDED
    must be built from `derive_bq_load_schema`, not `derive_bq_schema` —
    the FILE_LOADS runtime path (vendored apitools `TableFieldSchema`)
    rejects `maxLength`/`precision`/`scale`/`defaultValueExpression` at
    load-job time even though Beam accepts them at graph construction.

    `run_pipeline.main()` calls `derive_bq_load_schema(table_schema)`
    directly to build `landing_kwargs["schema"]`; exercise that same call
    here against a parameterized fixture (STRING max_length + NUMERIC
    precision/scale) rather than driving the whole pipeline.
    """
  import sdfb_beam.cli.run_pipeline as run_pipeline_module

  # Regression guard: the module must not have re-imported the unsafe
  # `derive_bq_schema` under the name used to build the landing schema.
  assert run_pipeline_module.derive_bq_load_schema is derive_bq_load_schema
  assert not hasattr(run_pipeline_module, "derive_bq_schema")

  ts = TableSchema.model_validate(narrow_ddl_dict)
  schema = run_pipeline_module.derive_bq_load_schema(ts)

  forbidden = {"maxLength", "precision", "scale", "defaultValueExpression"}
  for field in schema["fields"]:
    assert not forbidden & set(field.keys()), field
  email = next(f for f in schema["fields"] if f["name"] == "email")
  assert email["type"] == "STRING"
  ltv = next(f for f in schema["fields"] if f["name"] == "lifetime_value")
  assert ltv["type"] == "NUMERIC"


def test_parse_bool_flag_truthy_set():
  assert parse_bool_flag("true")
  assert parse_bool_flag("1")
  assert parse_bool_flag(" YES ")
  assert not parse_bool_flag("false")
  assert not parse_bool_flag("0")
  assert not parse_bool_flag("")
  assert not parse_bool_flag("no")


def test_resolve_landing_dispositions_matrix():
  from apache_beam.io.gcp.bigquery import BigQueryDisposition

  assert resolve_landing_dispositions("append", False) == (
      BigQueryDisposition.WRITE_APPEND,
      BigQueryDisposition.CREATE_NEVER,
  )
  assert resolve_landing_dispositions("overwrite", False) == (
      BigQueryDisposition.WRITE_TRUNCATE,
      BigQueryDisposition.CREATE_NEVER,
  )
  assert resolve_landing_dispositions("append", True) == (
      BigQueryDisposition.WRITE_APPEND,
      BigQueryDisposition.CREATE_IF_NEEDED,
  )
  assert resolve_landing_dispositions("overwrite", True) == (
      BigQueryDisposition.WRITE_TRUNCATE,
      BigQueryDisposition.CREATE_IF_NEEDED,
  )


# --- ddl_uri optional with live-extraction precedence (WS4 §6b) ---------


def _args_without_ddl_uri() -> list[str]:
  args = _common_args()
  i = args.index("--ddl_uri")
  return args[:i] + args[i + 2:]


def test_parse_args_ddl_uri_optional_defaults_empty():
  args, beam_argv = parse_args(_args_without_ddl_uri())
  assert args.ddl_uri == ""
  assert beam_argv == []


def test_resolve_table_schema_prefers_explicit_uri(monkeypatch):
  from sdfb_beam.cli import run_pipeline as rp

  sentinel = object()
  monkeypatch.setattr(rp, "load_ddl", lambda uri: sentinel)

  def fake_extract(fqn: str):
    live_calls.append(fqn)

  live_calls: list[str] = []
  monkeypatch.setattr(
      "sdfb_beam.ddl.extract_table_schema",
      fake_extract,
  )
  assert rp.resolve_table_schema("gs://b/d.json", "p.d.t") is sentinel
  assert live_calls == []  # precedence: pin wins, live never touched


def test_resolve_table_schema_live_extracts_when_uri_empty(monkeypatch):
  from sdfb_beam.cli import run_pipeline as rp
  from sdfb_core.contracts import FieldSchema, TableInfo, TableSchema

  live = TableSchema(
      table_info=TableInfo(table_id="p.d.t", description="lake prose"),
      columns=[
          FieldSchema(
              name="COL_001",
              bq_type="STRING",
              mode="NULLABLE",
              description="lake-side hint",
          )
      ],
  )
  # Bound at module scope in run_pipeline since WS5 T1, so patch it
  # there rather than in sdfb_beam.ddl.
  monkeypatch.setattr(rp, "extract_table_schema", lambda fqn: live)
  got = rp.resolve_table_schema("", "p.d.t")
  assert [c.name for c in got.columns] == ["COL_001"]
  # ADR 0027 D2: with no landing_table given, SOURCE descriptions are
  # stripped — they must never steer generation.
  assert got.columns[0].description == ""
  assert got.table_info.description == ""


# ---------------------------------------------------------------------------
# Warm-pool taint preflight (2026-08-07: the 10M warm run replayed the
# memorized 2026-08-05 pools because exists() was the only guard).
# ---------------------------------------------------------------------------


class _PreflightPoolStore:

  def __init__(self, pools):
    self.pools = pools
    self.deleted: list[tuple[str, str]] = []

  def fetch(self, digest, model_uri):
    return list(self.pools)

  def delete(self, digest, model_uri):
    self.deleted.append((digest, model_uri))


class _PreflightValueStore:

  def count_overlap(self, column, values):
    return sum(1 for v in values if v.startswith("real-"))


def _preflight_pool(column, values):
  from sdfb_core.pools import FreeTextPool

  return FreeTextPool(
      reference_digest="d",
      model_uri="m",
      column=column,
      target=len(values),
      values=values,
      stagnated=False,
      attempts=1,
  )


def test_warm_pools_trusted_when_clean():
  from sdfb_beam.cli.run_pipeline import warm_pools_trusted

  store = _PreflightPoolStore([_preflight_pool("c", ("nov-1", "nov-2"))])
  assert warm_pools_trusted(store, _PreflightValueStore(), "d", "m") is True
  assert store.deleted == []


def test_warm_pools_tainted_deletes_and_rebuilds():
  from sdfb_beam.cli.run_pipeline import warm_pools_trusted

  store = _PreflightPoolStore([_preflight_pool("c", ("real-1", "nov-2"))])
  assert warm_pools_trusted(store, _PreflightValueStore(), "d", "m") is False
  assert store.deleted == [("d", "m")]


def test_warm_pools_overlap_check_error_keeps_warm_path():
  from sdfb_beam.cli.run_pipeline import warm_pools_trusted

  class _BoomValueStore:

    def count_overlap(self, column, values):
      raise RuntimeError("bq down")

  store = _PreflightPoolStore([_preflight_pool("c", ("real-1",))])
  assert warm_pools_trusted(store, _BoomValueStore(), "d", "m") is True
  assert store.deleted == []


def test_warm_pools_delete_failure_keeps_warm_path():
  """Append-rebuild without a clean delete would leave stale rows racing
    the rebuilt ones in fetch(); better to keep the (flagged) warm pools."""
  from sdfb_beam.cli.run_pipeline import warm_pools_trusted

  class _NoDeleteStore(_PreflightPoolStore):

    def delete(self, digest, model_uri):
      raise RuntimeError("dml denied")

  store = _NoDeleteStore([_preflight_pool("c", ("real-1",))])
  assert warm_pools_trusted(store, _PreflightValueStore(), "d", "m") is True


# --- num_workers: start a scale run at its worker ceiling (ADR 0034) --------
# The 2026-08-29 R6 pair launched on 2 workers and autoscaled to 4 only ~4
# min into the first generate stage — C_TABLE ran 8 minutes at a quarter
# of its steady-state rate. Like disk_size_gb, the initial count is pinned
# from the launcher (the Flex Template environment field is not the
# channel the DAG controls per trigger).
def test_configure_options_dataflow_sets_num_workers_when_given():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "r1", num_workers=4)
  assert opts.view_as(WorkerOptions).num_workers == 4


def test_configure_options_dataflow_leaves_num_workers_unset_by_default():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "r1")
  assert opts.view_as(WorkerOptions).num_workers is None


def test_configure_options_explicit_beam_flag_wins_over_num_workers():
  opts = PipelineOptions(["--runner=DataflowRunner", "--num_workers=2"])
  configure_pipeline_options(opts, "DataflowRunner", "r1", num_workers=4)
  assert opts.view_as(WorkerOptions).num_workers == 2


_MIN_ARGS = [
    "--reference_table",
    "p.src.t",
    "--landing_table",
    "p.land.t",
    "--dlq_table",
    "p.q.dlq",
    "--num_rows",
    "10",
    "--run_id",
    "r1",
    "--model_uri",
    "gs://b/synthetic/models/m/v1/",
]


def test_parse_args_accepts_initial_workers_as_an_optional_string():
  from sdfb_beam.cli.run_pipeline import parse_args, resolve_num_workers

  args, _beam = parse_args([*_MIN_ARGS, "--initial_workers", "4"])
  assert resolve_num_workers(args.initial_workers) == 4
  args, _beam = parse_args(_MIN_ARGS)
  assert resolve_num_workers(args.initial_workers) is None
  with pytest.raises(ValueError, match="initial_workers"):
    resolve_num_workers("four")


# --- state cache for the ResolveUniqueness side inputs (ADR 0034) ----------
# 2026-09-07 R7: `Retrieving state 62 times costed 60 seconds ... consider
# adding '--max_cache_memory_usage_mb'` on the single-barrier read stage —
# Beam's default cache (100 MB in the harness; the option reads 0 = unset)
# re-fetched the PK/identity group dicts per bundle.
def test_configure_options_dataflow_pins_the_state_cache_size():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "r1")
  assert opts.view_as(
      WorkerOptions).max_cache_memory_usage_mb == _DEFAULT_STATE_CACHE_MB


def test_configure_options_explicit_state_cache_wins():
  opts = PipelineOptions(
      ["--runner=DataflowRunner", "--max_cache_memory_usage_mb=64"])
  configure_pipeline_options(opts, "DataflowRunner", "r1")
  assert opts.view_as(WorkerOptions).max_cache_memory_usage_mb == 64


# --- autoscaling: a pinned fleet stays pinned (ADR 0034 D9) -----------------
# 2026-09-07 warm-rebuild + 2026-09-08 cold (both multi): the autoscaler
# dropped to 1-2 workers during the parent's load / FK-pool phase and
# re-provisioned VMs for the child stage — A_TABLE ran its first 3-8 min on
# 1-2 workers (10.3 / ~11 min vs 6.7 on the run that kept 4).
def test_autoscaling_auto_pins_none_when_initial_workers_is_given():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "r1", num_workers=4)
  w = opts.view_as(WorkerOptions)
  assert w.num_workers == 4
  assert w.autoscaling_algorithm == "NONE"


def test_autoscaling_auto_without_initial_workers_leaves_dataflow_default():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(opts, "DataflowRunner", "r1")
  assert opts.view_as(WorkerOptions).autoscaling_algorithm is None


def test_autoscaling_throughput_keeps_scaling_even_with_initial_workers():
  opts = PipelineOptions(["--runner=DataflowRunner"])
  configure_pipeline_options(
      opts, "DataflowRunner", "r1", num_workers=4, autoscaling="throughput")
  assert opts.view_as(WorkerOptions).autoscaling_algorithm is None


def test_autoscaling_fixed_without_initial_workers_is_a_launch_error():
  from sdfb_beam.cli.run_pipeline import resolve_autoscaling

  assert resolve_autoscaling("fixed", 4) == "NONE"
  assert resolve_autoscaling("auto", 4) == "NONE"
  assert resolve_autoscaling("auto", None) is None
  assert resolve_autoscaling("throughput", 4) is None
  with pytest.raises(ValueError, match="autoscaling"):
    resolve_autoscaling("fixed", None)
  with pytest.raises(ValueError, match="autoscaling"):
    resolve_autoscaling("bogus", 4)


def test_explicit_beam_autoscaling_flag_wins():
  opts = PipelineOptions(
      ["--runner=DataflowRunner", "--autoscaling_algorithm=THROUGHPUT_BASED"])
  configure_pipeline_options(opts, "DataflowRunner", "r1", num_workers=4)
  assert opts.view_as(WorkerOptions).autoscaling_algorithm == "THROUGHPUT_BASED"


def test_parse_args_accepts_autoscaling():
  from sdfb_beam.cli.run_pipeline import parse_args

  args, _beam = parse_args([*_MIN_ARGS, "--autoscaling", "fixed"])
  assert args.autoscaling == "fixed"
  args, _beam = parse_args(_MIN_ARGS)
  assert args.autoscaling == "auto"
