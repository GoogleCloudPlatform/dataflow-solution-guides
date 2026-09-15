"""Unit tests for `GenerateRecordsDoFn.setup()` — specifically that a gs://
`embedder_uri` is warm-pulled to a local path before the engine builds its
embedder.

Regression test: previously the DoFn passed the raw gs:// URI straight into
the engine, which handed it to `transformers.from_pretrained` — which cannot
read gs:// and raised `OSError: Repo id must be in the form ...`.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,redefined-outer-name,unused-argument

from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest
from sdfb_beam.dofns import generate as generate_mod
from sdfb_beam.dofns.generate import GenerateRecordsDoFn
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.engines import GenerationContext


@pytest.fixture
def fake_gcs(monkeypatch):
  """Inject a fake `google.cloud.storage` module + return its recorder."""

  class _FakeBlob:

    def __init__(self, name):
      self.name = name
      self.downloaded_to = None

    def download_to_filename(self, dest):
      self.downloaded_to = dest
      with open(dest, "w", encoding="utf-8") as f:
        f.write(self.name)

  recorder: dict = {"list_calls": [], "blobs": []}

  class _FakeStorageClient:

    def list_blobs(self, bucket, prefix=""):
      recorder["list_calls"].append((bucket, prefix))
      return list(recorder["blobs"])

  storage_mod = types.ModuleType("google.cloud.storage")
  storage_mod.Client = lambda *_a, **_k: _FakeStorageClient()
  cloud_mod = types.ModuleType("google.cloud")
  cloud_mod.storage = storage_mod
  google_mod = sys.modules.get("google") or types.ModuleType("google")

  monkeypatch.setitem(sys.modules, "google", google_mod)
  monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
  monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
  recorder["blob_cls"] = _FakeBlob
  return recorder


class _FakeGeneratedRecord:
  """Minimal stand-in for a `GeneratedRecord` — only needs `model_dump()`."""

  def __init__(self, i: int, seed: int | None = None) -> None:
    self._i = i
    self._seed = seed

  def model_dump(self, mode: str = "python") -> dict:
    # "country" mirrors `customers_schema`'s STRING(max_length=2) column
    # so identity-overwrite tests can exercise the max_length-truncation
    # path without disturbing the "i"/"seed"-only assertions elsewhere.
    return {"i": self._i, "seed": self._seed, "country": "ZZ"}


class _RecordingEngine:
  """Stand-in engine that captures the ctx it was set up with."""

  last_ctx: ClassVar[GenerationContext | None] = None
  last_cfgs: ClassVar[list] = []

  def setup(self, model_client, ctx):
    type(self).last_ctx = ctx

  def generate_batch(self, n, cfg):
    # Embed cfg.seed so tests can assert generation output actually
    # varies with the derived per-batch seed (not just that a seed value
    # was computed somewhere).
    type(self).last_cfgs.append(cfg)
    for i in range(n):
      yield _FakeGeneratedRecord(i, seed=cfg.seed)

  def teardown(self):
    pass


@pytest.fixture
def recording_engine(monkeypatch):
  _RecordingEngine.last_ctx = None
  _RecordingEngine.last_cfgs = []
  monkeypatch.setattr(generate_mod, "get_engine",
                      lambda _name: _RecordingEngine)
  return _RecordingEngine


def _dofn(ctx, seed=None):
  return GenerateRecordsDoFn(
      engine_name="b1_rag",
      model_client=FakeModelClient(reference_pool=[{
          "x": 1
      }]),
      ctx=ctx,
      seed=seed,
  )


def test_setup_localizes_gs_embedder_uri(fake_gcs, recording_engine,
                                         monkeypatch, tmp_path,
                                         customers_schema):
  # The localization moved to the shared dofns/localize module (2026-07-28
  # R1 fix: BuildFreeTextPoolsDoFn skipped it), so patch it THERE.
  from sdfb_beam.dofns import localize as localize_mod

  monkeypatch.setattr(localize_mod, "EMBEDDER_LOCAL_DIR", str(tmp_path / "emb"))
  prefix = "synthetic/models/embedders/bge-small-en-v1.5/v1/"
  blob_cls = fake_gcs["blob_cls"]
  fake_gcs["blobs"] = [blob_cls(prefix + "config.json")]

  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri=f"gs://env-bucket/{prefix}",
  )
  dofn = _dofn(ctx)
  dofn.setup()

  # The pull ran against the right prefix...
  assert fake_gcs["list_calls"] == [("env-bucket", prefix)]
  # ...and the engine got a LOCAL path, never the gs:// URI.
  got = recording_engine.last_ctx.embedder_uri
  assert not got.startswith("gs://")
  assert got == str(tmp_path / "emb")


def test_setup_leaves_empty_embedder_uri_untouched(recording_engine,
                                                   customers_schema):
  """Empty URI ⇒ engine falls back to HashingEmbedder; no pull attempted."""
  ctx = GenerationContext(table_schema=customers_schema, embedder_uri="")
  _dofn(ctx).setup()
  assert recording_engine.last_ctx.embedder_uri == ""


def test_setup_passes_local_embedder_uri_through(recording_engine, tmp_path,
                                                 customers_schema):
  """A bare local path (M4 with staged weights) skips the pull, used as-is."""
  local = str(tmp_path / "staged-embedder")
  ctx = GenerationContext(table_schema=customers_schema, embedder_uri=local)
  _dofn(ctx).setup()
  assert recording_engine.last_ctx.embedder_uri == local


def test_setup_and_process_emit_milestones(caplog, recording_engine,
                                           customers_schema):
  import logging

  ctx = GenerationContext(table_schema=customers_schema, embedder_uri="")
  dofn = _dofn(ctx)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    dofn.setup()
    list(dofn.process({"n": 4, "batch_id": 0}))
  text = "\n".join(r.message for r in caplog.records)
  assert "SDFB_MILESTONE name=dofn_setup_start" in text
  assert "SDFB_MILESTONE name=dofn_setup_done" in text
  assert "SDFB_MILESTONE name=batch_start" in text and "batch_id=0" in text
  assert "SDFB_MILESTONE name=batch_done" in text and "rows=4" in text


def test_no_explicit_seed_derives_distinct_seed_per_batch(
    recording_engine, customers_schema):
  """Regression test for the batch-replay defect: with no `--seed`, the
    DoFn previously passed `seed=None` for every batch, and the engines'
    `_mix_seed(None) -> 0` fallback made every batch replay an identical
    draw. Batch 0 and batch 1 must now derive different seeds (and thus
    different output) from `(ctx.pipeline_run_id, batch_id)`.
    """
  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri="",
      pipeline_run_id="run-xyz",
  )
  dofn = _dofn(ctx)
  dofn.setup()

  batch0 = list(dofn.process({"n": 4, "batch_id": 0}))
  batch1 = list(dofn.process({"n": 4, "batch_id": 1}))

  assert batch0 != batch1


def test_identity_columns_overwrite_engine_output(recording_engine,
                                                  customers_schema):
  """Regression test for the 2026-07 E2E privacy leak: identity columns
    (declared via `ctx.identity_columns`) must never surface the engine's
    own value for that field — `process()` overwrites them per-row from
    `(run_id, batch_id, row_index, column)`, unique across rows and batches.
    """
  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri="",
      pipeline_run_id="run-privacy",
      identity_columns=["i"],
  )
  dofn = _dofn(ctx)
  dofn.setup()

  batch0 = list(dofn.process({"n": 4, "batch_id": 0}))
  batch1 = list(dofn.process({"n": 4, "batch_id": 1}))

  # The engine-produced value for "i" (0, 1, 2, 3) is never what comes out.
  engine_values = {0, 1, 2, 3}
  for row in batch0 + batch1:
    assert row["i"] not in engine_values

  # Deterministic per (batch_id, row_index) and unique across all rows.
  all_ids = [row["i"] for row in batch0 + batch1]
  assert len(set(all_ids)) == len(all_ids) == 8

  # Non-identity fields ("seed") are untouched by the overwrite.
  assert all("seed" in row for row in batch0 + batch1)


def test_identity_column_max_length_truncates_string_value(
    recording_engine, customers_schema):
  """Regression test: a STRING identity column with a schema `max_length`
    narrower than the 36-char UUID (e.g. `customers_schema`'s `country`,
    max_length=2) must come out truncated to that width, not overflow it.
    `_column_max_lengths` is built once in `setup()` from the table schema's
    `FieldSchema.max_length` and forwarded into `apply_identity_columns`.
    """
  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri="",
      pipeline_run_id="run-maxlen",
      identity_columns=["country"],
  )
  dofn = _dofn(ctx)
  dofn.setup()

  assert dofn._column_max_lengths["country"] == 2

  batch = list(dofn.process({"n": 4, "batch_id": 0}))
  for row in batch:
    assert len(row["country"]) == 2
    assert row["country"] != "ZZ"

  # Still unique per row.
  assert len({row["country"] for row in batch}) == len(batch)


def test_explicit_seed_pool_seed_equals_base_seed_across_batches(
    recording_engine, customers_schema):
  """Explicit ``--seed`` mode: every batch's cfg must carry the SAME
    ``engine_specific["pool_seed"]`` (the base seed), decoupled from that
    batch's own `cfg.seed` (base_seed + batch_id) — the P6 fix. Before this
    fix the pool build used `cfg.seed` directly, so it varied per batch."""
  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri="",
      pipeline_run_id="run-explicit-seed",
  )
  dofn = _dofn(ctx, seed=100)
  dofn.setup()

  list(dofn.process({"n": 2, "batch_id": 0}))
  list(dofn.process({"n": 2, "batch_id": 1}))

  cfgs = recording_engine.last_cfgs
  assert len(cfgs) == 2
  assert cfgs[0].seed == 100 and cfgs[
      1].seed == 101, "per-batch seed still varies"
  assert cfgs[0].engine_specific["pool_seed"] == 100
  assert cfgs[1].engine_specific["pool_seed"] == 100
  assert cfgs[0].engine_specific["pool_seed"] == cfgs[1].engine_specific[
      "pool_seed"]


def test_derived_seed_pool_seed_stable_and_differs_from_batch_seed(
    recording_engine, customers_schema):
  """Derived-mode (no explicit ``--seed``): `pool_seed` must be identical
    across batches (derived from `(run_id, -1)`, a reserved namespace outside
    any real batch_id) and must differ from each batch's own derived seed."""
  ctx = GenerationContext(
      table_schema=customers_schema,
      embedder_uri="",
      pipeline_run_id="run-derived-seed",
  )
  dofn = _dofn(ctx)
  dofn.setup()

  list(dofn.process({"n": 2, "batch_id": 0}))
  list(dofn.process({"n": 2, "batch_id": 1}))

  cfgs = recording_engine.last_cfgs
  assert len(cfgs) == 2
  pool_seed_0 = cfgs[0].engine_specific["pool_seed"]
  pool_seed_1 = cfgs[1].engine_specific["pool_seed"]
  assert pool_seed_0 == pool_seed_1, "pool_seed must be stable across batches"
  assert pool_seed_0 != cfgs[0].seed
  assert pool_seed_1 != cfgs[1].seed
