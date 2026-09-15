"""vLLM-backed `ModelClient` — owns a vLLM OpenAI-compatible server.

Per [ADR 0014](../../../../docs/adr/0014-vllm-model-client-owns-server.md)
(which amends [ADR 0011](../../../../docs/adr/0011-adopt-beam-vllm-model-handler.md)),
this client manages the vLLM server subprocess directly instead of going
through Beam's `RunInference` handler. The engines call
`generate_json(prompt, json_schema, ...)` synchronously and O(1) times
(free-text pools / distribution inference, NOT per row — see ADR 0013),
so a `RunInference` PTransform is the wrong shape for the seam.

Lifecycle (per Dataflow worker, driven by the engine's `DoFn`):

    c = VLLMModelClient(model_uri="gs://.../gemma4/e4b-it/v1/",
                        vllm_server_kwargs={"max-model-len": "8192", ...})
    c.setup()        # GCS warm-pull → /local-ssd/model; spawn server; poll ready
    rows = c.generate_json(prompt, schema, n=5)
    c.teardown()     # terminate the server subprocess

CUDA-only. This CANNOT run on the M4 (vLLM ships no macOS wheels and needs
an NVIDIA GPU). The laptop only ever imports the class and exercises the
mock-based unit tests — every heavy dependency (`vllm`, `openai`,
`google.cloud.storage`) is imported INSIDE `setup()` / `generate_json()`,
never at module load. Real-vLLM behavior is validated at M1 §11 on an L4.

Constraints honoured here:
  - Weights pulled via the `google-cloud-storage` Python client, NOT gsutil
    (ADR 0012 — the CLI drags in a `packages.cloud.google.com` apt dep the
    enterprise build can't reach; ADC authenticates the client on-worker).
  - The **chat** endpoint (not completions) is used so vLLM applies Gemma 4's
    chat template — required to suppress the chain-of-thought channel via
    `chat_template_kwargs={"enable_thinking": False}` (ADR 0014; the
    completions endpoint does not apply the chat template).
  - Guided JSON via the OpenAI-standard `response_format={"type":
    "json_schema", ...}` — vLLM >= 0.10 structured outputs. The legacy
    `extra_body={"guided_json": ...}` spelling is silently ignored by
    vLLM 0.24 (2026-07-15 E2E run: free-form output, 100 % parse-drop).

REFs:
  - docs/adr/0014-vllm-model-client-owns-server.md (THE design)
  - docs/adr/0011-adopt-beam-vllm-model-handler.md (amended)
  - docs/adr/0012-enterprise-image-build.md (GCS-client pull, version pins)
  - docs/adr/0013-distribution-estimator-spine.md (LLM is O(1))
  - config/models.yml (`vllm_server_kwargs` per model)
  - .claude/skills/model-handler.md (recipe)
  - https://docs.vllm.ai/en/latest/usage/structured_outputs.html
  - https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

from sdfb_core.engines.base import ModelClientTransientError
from sdfb_core.observability import log_milestone

from sdfb_beam.gcs import localize_gcs_prefix, split_gs_uri

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
  import subprocess

logger = logging.getLogger(__name__)

# Where the GCS warm-pull lands and where the vLLM server reads weights from.
# Dataflow GPU workers mount fast local SSD here (see gpu-dockerfile recipe).
DEFAULT_LOCAL_MODEL_DIR = "/local-ssd/model"
DEFAULT_PORT = 8000
# vLLM cold start on an L4 (CUDA graph capture + weight load) is well under
# this; the GCS pull happens before the poll loop starts. Generous but bounded.
DEFAULT_STARTUP_TIMEOUT_S = 600.0
DEFAULT_POLL_INTERVAL_S = 2.0
_HTTP_OK = 200

# One vLLM server per (host, port) per SDK worker PROCESS. Beam hands bundles
# to many DoFn threads at once, and each DoFn owns its own VLLMModelClient —
# without process-wide serialization every thread fails the reuse probe in
# the same instant and spawns its own server onto the one GPU (2026-07-16
# b2_library run: 3 concurrent spawns x ~4.8 GiB weights on a 14.56 GiB T4 →
# mutual CUDA OOM, exit code 1, 4 bundle-retry strikes, job FAILED).
# `_SETUP_LOCK` serializes the probe → pull → spawn → ready window;
# `_SERVER_REFS` counts live clients bound to each server so teardown() only
# kills a server the LAST client releases (`_PARKED_SERVERS` holds a spawner's
# subprocess handle when its owner tears down while siblings are still bound).
_SETUP_LOCK = threading.Lock()
_SERVER_REFS: dict[str, int] = {}

# WS6 F2 (2026-07-27_11_32_51 E2E): a doomed server was re-pulled and
# re-spawned on EVERY generate_json() call — 52 cycles over 50 minutes with
# zero vllm_ready, because each ladder attempt re-entered setup() fresh.
# After this many consecutive startup failures per (host, port) the process
# raises immediately with the remembered error instead of thrashing the GPU;
# the bundle then fails in seconds and Dataflow surfaces the real cause.
_MAX_CONSECUTIVE_SPAWN_FAILURES = 3
_SPAWN_FAILURES: dict[str, int] = {}

# WS6 F1 (same run): vLLM sizes its allocation as gpu_memory_utilization x
# TOTAL device memory (default 0.9). When sibling processes / the population
# branch's embedder contexts hold part of the card, free < 0.9 x total and
# EngineCore init fails before serving a byte. Unless the caller pinned the
# flag, we derive it from what is actually FREE, minus this margin.
#
# WS6 F3 (2026-07-29_09_30_47 E2E): the margin must exceed the Beam harness's
# residual CUDA context — 622 MiB survives embedder demotion (a context can't
# be released without killing the process), so 512 MiB was not enough. And a
# fully-free card must never get vLLM's 0.90 default: spawn #3 at 0.90
# overflowed by ~150 MiB during warmup once CUDA graphs + non-torch overhead
# landed on top of the budget. 0.85 caps the derived fraction with real
# headroom for both.
_VLLM_VRAM_MARGIN_BYTES = 1024 * 1024**2
_VLLM_MAX_DYNAMIC_UTILIZATION = 0.85
# vLLM's own non-KV usage beyond the checkpoint bytes: fp16 load overhead,
# activation peak during profiling, CUDA-graph private pools, non-torch
# allocations. Measured ~0.94 GiB on the 2026-07-29 T4 run (Qwen3-4B);
# 1.25 GiB keeps the fitted length ~2k tokens inside vLLM's own estimate.
_VLLM_NON_KV_OVERHEAD_BYTES = int(1.25 * 1024**3)
# Floor for a clamped --max-model-len: pool builds complete with
# max_tokens=2048, so anything shorter leaves no room for the prompt.
_VLLM_MIN_MODEL_LEN = 4096
# In-process wait-and-re-measure window for a transiently unfittable card.
# Sibling embedders demote within ~80 s of setup on a single-table run
# (2026-08-05 B_TABLE R1); a two-table relational job (ADR 0030) doubles
# the DoFn instances churning the card during setup and the 2026-08-25 R6
# run needed 161 s from the first unfittable measure to a fittable card —
# the old window (6 attempts = 5 x 20 s between first and last measure)
# expired 60 s short and failed the bundle after its sibling ladders had
# finished (ADR 0033). 12 attempts (11 x 20 s = 220 s) cover that with
# margin for a third table's churn, still at a fraction of a bundle
# retry's cost (fresh DoFn.setup() + re-embed + store fetches).
_UNFITTABLE_RETRY_ATTEMPTS = 12
_UNFITTABLE_RETRY_WAIT_S = 20.0
# vLLM rounds KV capacity to 16-token blocks; keep the clamp aligned.
_VLLM_LEN_ALIGN = 16
_PARKED_SERVERS: dict[str, Any] = {}


class ModelLenUnfittableError(ModelClientTransientError):
  """The measured VRAM budget cannot host even `_VLLM_MIN_MODEL_LEN`.

    Raised BEFORE the server spawn (2026-07-29_09_30_47: three doomed spawns
    burned the whole `_MAX_CONSECUTIVE_SPAWN_FAILURES` budget on a card that
    was only transiently contended). Deliberately NOT counted as a spawn
    failure — each bundle retry re-measures the card, and an embedder that
    has since demoted frees the budget the next attempt needs. A
    `ModelClientTransientError` (ADR 0033): the engine's ladder retries the
    losing thread in-process instead of failing the bundle."""


def _kv_bytes_per_token(cfg: dict, dtype_bytes: int = 2) -> int | None:
  """KV-cache bytes one token costs, from a HF `config.json` dict.

    2 (K and V) x layers x kv-heads x head-dim x dtype bytes. Qwen3-4B
    (36 x 8 x 128, fp16) -> 144 KiB/token — exactly the "1.12 GiB KV cache
    is needed" vLLM reported for max_model_len=8192 on the 2026-07-29 run.
    Returns None when the config lacks the geometry (nothing to size by).
    """
  layers = cfg.get("num_hidden_layers")
  kv_heads = cfg.get("num_key_value_heads") or cfg.get("num_attention_heads")
  head_dim = cfg.get("head_dim")
  if head_dim is None:
    hidden = cfg.get("hidden_size")
    heads = cfg.get("num_attention_heads")
    if hidden and heads:
      head_dim = hidden // heads
  if not (layers and kv_heads and head_dim):
    return None
  return 2 * int(layers) * int(kv_heads) * int(head_dim) * dtype_bytes


def _fit_max_model_len(
    requested: int,
    *,
    budget_bytes: int,
    weights_bytes: int,
    kv_bytes_per_token: int,
    overhead_bytes: int = _VLLM_NON_KV_OVERHEAD_BYTES,
) -> int:
  """The longest --max-model-len the VRAM budget can host, <= `requested`.

    Pure arithmetic mirror of vLLM's `_check_enough_kv_cache_memory`: what is
    left of the budget after weights and non-KV overhead, divided by the KV
    cost per token, floored to a block multiple. 0 means not even one block
    fits. Never exceeds `requested` (a clamp, not a promotion).
    """
  kv_budget = budget_bytes - weights_bytes - overhead_bytes
  if kv_budget <= 0:
    return 0
  fitted = (kv_budget //
            kv_bytes_per_token) // _VLLM_LEN_ALIGN * _VLLM_LEN_ALIGN
  return min(int(requested), int(fitted))


class ModelGpuIncompatibleError(RuntimeError):
  """The pulled model's dtype cannot run on this worker's GPU.

    Raised BEFORE the vLLM server spawn so the Dataflow job fails fast with an
    actionable message instead of stalling for ~24 min and letting the engines
    silently fall back to copying reference exemplars (E2E report §4.2)."""


_MIN_BF16_CAPABILITY = (8, 0)


def _assert_dtype_supported(
    torch_dtype: str,
    capability: tuple[int, int],
    *,
    dtype_override: str = "",
    model_type: str = "",
) -> None:
  """Raise `ModelGpuIncompatibleError` if `torch_dtype` cannot run on a GPU
    reporting `capability` (major, minor). Pure function — no torch import
    required, unit-testable on the laptop.

    ``dtype_override`` is the explicit ``--dtype`` the server will be launched
    with (from ``vllm_server_kwargs``). An explicit fp16 downcast makes a bf16
    checkpoint runnable on Turing for fp16-safe families (Qwen ships bf16
    checkpoints but is numerically stable in fp16) — EXCEPT gemma, whose fp16
    activations overflow and silently emit empty/pad output, so the override
    never bypasses the guard for gemma-family ``model_type``s.
    """
  if dtype_override in {"float16", "half"}:
    if model_type.startswith("gemma"):
      raise ModelGpuIncompatibleError(
          f"refusing --dtype={dtype_override} for a gemma-family "
          f"checkpoint (model_type={model_type!r}): fp16 Gemma silently "
          "emits empty output (HF gemma-3-4b-it #33, vLLM #40290). Run "
          "Gemma on gpu=l4, or point SDFB_MODEL_URI at an fp16-safe "
          "model (config/models.yml: qwen3_4b_instruct_2507).")
    return  # explicit fp16 serve dtype — checkpoint bf16 no longer applies
  if torch_dtype == "bfloat16" and capability < _MIN_BF16_CAPABILITY:
    raise ModelGpuIncompatibleError(
        f"model dtype bfloat16 needs GPU compute capability >= 8.0 "
        f"(Ampere/L4+); this worker reports {capability[0]}.{capability[1]} "
        "(e.g. T4/Turing). Run with gpu=l4, or point SDFB_MODEL_URI at a "
        "T4-safe fp16 model (see config/models.yml: qwen3_4b_instruct_2507). "
        "Do NOT force --dtype=half for Gemma: it silently emits empty output.")


class _PortMutex:
  """Cross-PROCESS mutex for the spawn window: a bound loopback port.

    Dataflow's default topology runs one SDK harness process per vCPU
    (ADR 0034); `_SETUP_LOCK` only serializes the threads of ONE of them.
    Every SDK container on a worker shares the host network — the same
    fact the reuse probe on `127.0.0.1:8000` already relies on — so a
    port only one process can bind is a lock every process can see, with
    no shared filesystem assumption. Released by closing the socket, and
    by the kernel if the holder dies.
    """

  def __init__(self, host: str, port: int) -> None:
    self.host = host
    self.port = port
    self._sock: socket.socket | None = None

  def try_acquire(self) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
      sock.bind((self.host, self.port))
      sock.listen(1)
    except OSError:
      sock.close()
      return False
    self._sock = sock
    return True

  def release(self) -> None:
    sock, self._sock = self._sock, None
    if sock is not None:
      sock.close()


class VLLMModelClient:
  """`ModelClient` impl that owns a vLLM OpenAI-compatible server.

    Structural `ModelClient` (the Protocol in `sdfb_core.engines.base`) —
    does not subclass it; engines test interchangeability via
    `isinstance(client, ModelClient)`.
    """

  def __init__(
      self,
      model_uri: str,
      *,
      vllm_server_kwargs: dict[str, Any] | None = None,
      local_model_dir: str = DEFAULT_LOCAL_MODEL_DIR,
      port: int = DEFAULT_PORT,
      host: str = "127.0.0.1",
      startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
      poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
      guided_decoding_backend: str = "outlines",
      cross_process: bool = False,
      spawn_lock_port: int | None = None,
  ) -> None:
    """Configure the client. No heavy work happens here.

        Args:
            model_uri: `gs://{bucket}/.../{family}/{model}/{version}/` prefix
                whose contents are pulled to `local_model_dir`. A bare local
                path (no `gs://` scheme) is used as-is (skips the pull) — handy
                for an L4 box that already has the weights staged.
            vllm_server_kwargs: extra CLI flags for
                `vllm.entrypoints.openai.api_server`, from the matching entry
                in `config/models.yml` (e.g. `{"quantization": "awq",
                "max-model-len": "8192", "gpu-memory-utilization": "0.85"}`).
                Keys map to `--key value` (or a bare `--key` flag when the
                value is `True`).
            local_model_dir: where weights land / where vLLM reads them.
            port / host: where the spawned server listens.
            startup_timeout_s / poll_interval_s: readiness-poll budget.
            guided_decoding_backend: retained for flex-template parameter
                compatibility only. vLLM ≥ 0.10 selects the structured-output
                backend server-side (`structured_outputs_config`, default
                `auto`); per-request backend selection no longer exists, so
                this value is not sent with requests.
            cross_process: sibling SDK PROCESSES on this worker race for the
                one GPU (Dataflow's default multi-container topology, ADR
                0034): the spawn window is guarded by `_PortMutex` on
                ``spawn_lock_port`` and teardown never terminates the server
                (another process may still be bound to it; the worker VM
                reaps it at job end).
            spawn_lock_port: the mutex port; default ``port + 1``.
        """
    self.model_uri = model_uri
    self.cross_process = cross_process
    self.spawn_lock_port = (
        spawn_lock_port if spawn_lock_port is not None else port + 1)
    self.vllm_server_kwargs: dict[str, Any] = dict(vllm_server_kwargs or {})
    self.local_model_dir = local_model_dir
    self.port = port
    self.host = host
    self.startup_timeout_s = startup_timeout_s
    self.poll_interval_s = poll_interval_s
    self.guided_decoding_backend = guided_decoding_backend

    # Populated by setup(); reset by teardown().
    self._server: subprocess.Popen[bytes] | None = None
    self._client: Any = None  # openai.OpenAI
    # True once this client is counted in `_SERVER_REFS` (spawner or
    # reuser alike); teardown() decrements exactly once.
    self._bound = False
    # The model identifier the OpenAI client must send. vLLM registers the
    # served model under the path/name it was launched with, so it equals
    # the local model dir after a GCS pull.
    self._served_model_name: str = local_model_dir

  @property
  def base_url(self) -> str:
    return f"http://{self.host}:{self.port}/v1"

  # ------------------------------------------------------------------
  # Lifecycle
  # ------------------------------------------------------------------

  def setup(self) -> None:
    """Per-worker init. Idempotent (a second call is a no-op).

        Process-wide serialized (`_SETUP_LOCK`): concurrent DoFn threads on a
        fresh worker must not each pull weights and spawn a server — one
        thread does the pull → spawn → ready sequence while the rest block,
        then bind to the now-healthy server via the reuse probe (2026-07-16
        b2_library run: 3 unserialized spawns OOMed each other off one T4).
        Under ``cross_process`` the same window is also serialized across
        the worker's SDK processes by `_PortMutex` (ADR 0034).

        1. Pull weights GCS → `local_model_dir` (skipped for a local path).
        2. Spawn the vLLM OpenAI server subprocess.
        3. Poll `/v1/models` until ready (or time out).
        4. Build the `openai.OpenAI` client pointed at the local server.
        """
    if self._client is not None:
      return
    with _SETUP_LOCK:
      if self._client is not None:  # pragma: no cover - defensive
        return

      t0 = time.monotonic()
      log_milestone("model_client_setup_start", client=type(self).__name__)

      # A previous bundle attempt in this container may have left a
      # healthy server behind, and while this thread waited on
      # _SETUP_LOCK a sibling thread may have finished spawning one.
      # Spawning a second server into the GPU it still owns fails with
      # CUDA OOM — the 2026-07-16 corp run logged exactly that on every
      # retry, while each retry also re-pulled 7.5 GB of weights. Reuse
      # the survivor instead.
      expected_model = (
          self.local_model_dir
          if self.model_uri.startswith("gs://") else self.model_uri)
      if self._try_reuse(expected_model, t0):
        return
      if self.cross_process:
        self._setup_cross_process(expected_model, t0)
        return
      self._pull_spawn_bind(t0)

  def _try_reuse(self, expected_model: str, t0: float) -> bool:
    """Bind to a healthy server already serving `expected_model`."""
    if not self._probe_reusable_server(expected_model):
      return False
    self._served_model_name = expected_model
    self._client = self._build_openai_client()
    self._bind_server_locked()
    log_milestone("vllm_reuse", seconds=round(time.monotonic() - t0, 1))
    log_milestone(
        "model_client_setup_done",
        seconds=round(time.monotonic() - t0, 1),
    )
    logger.info(
        "Reusing healthy vLLM server already serving %r at %s "
        "(skipping weight pull and spawn).",
        expected_model,
        self.base_url,
    )
    return True

  def _setup_cross_process(self, expected_model: str, t0: float) -> None:
    """Spawn only while holding the cross-process mutex; otherwise wait
        for the holder's server to answer the reuse probe (ADR 0034)."""
    deadline = time.monotonic() + self.startup_timeout_s
    mutex = _PortMutex(self.host, self.spawn_lock_port)
    announced = False
    while True:
      if mutex.try_acquire():
        try:
          log_milestone("vllm_spawn_lock_acquired", port=self.spawn_lock_port)
          # The previous holder may have brought the server up
          # between our last probe and the bind.
          if self._try_reuse(expected_model, t0):
            return
          self._pull_spawn_bind(t0)
          return
        finally:
          mutex.release()
      if not announced:
        log_milestone(
            "vllm_spawn_lock_wait",
            port=self.spawn_lock_port,
            url=self.base_url,
        )
        announced = True
      if self._try_reuse(expected_model, t0):
        return
      if time.monotonic() >= deadline:
        raise TimeoutError(
            f"vLLM spawn lock on port {self.spawn_lock_port} was held "
            f"by another process for {self.startup_timeout_s}s and no "
            f"server serving {expected_model!r} appeared at "
            f"{self.base_url}.")
      time.sleep(self.poll_interval_s)

  def _pull_spawn_bind(self, t0: float) -> None:
    """The pull → dtype guard → spawn → ready → client sequence.
        Caller holds `_SETUP_LOCK` (and the cross-process mutex)."""
    if self.model_uri.startswith("gs://"):
      log_milestone("model_pull_start", uri=self.model_uri)
      t_pull = time.monotonic()
      self._pull_weights()
      log_milestone(
          "model_pull_done",
          seconds=round(time.monotonic() - t_pull, 1),
      )
      self._served_model_name = self.local_model_dir
    else:
      # Already-local weights; serve them in place.
      logger.info(
          "model_uri %r is not a gs:// URI — serving it as a local "
          "path (skipping GCS pull).",
          self.model_uri,
      )
      self._served_model_name = self.model_uri

    # Fail fast on T4+bf16 instead of letting vLLM stall and the
    # engines fall back to memorizing reference data.
    self._assert_gpu_dtype_compatible()

    failures = _SPAWN_FAILURES.get(self.base_url, 0)
    if failures >= _MAX_CONSECUTIVE_SPAWN_FAILURES:
      log_milestone(
          "vllm_spawn_suppressed",
          level=logging.ERROR,
          failures=failures,
          url=self.base_url,
      )
      raise RuntimeError(
          f"vLLM startup failed {failures} consecutive times in "
          f"this process for {self.base_url}; suppressing further "
          "spawn attempts. See the first failure's log for the "
          "root cause (2026-07-27 run: 52 doomed spawn cycles "
          "burned 50 minutes before the job failed).")

    log_milestone("vllm_spawn")
    self._spawn_until_ready(failures)
    _SPAWN_FAILURES[self.base_url] = 0
    self._client = self._build_openai_client()
    self._bind_server_locked()
    log_milestone("vllm_ready", seconds=round(time.monotonic() - t0, 1))
    log_milestone(
        "model_client_setup_done", seconds=round(time.monotonic() - t0, 1))
    logger.info("vLLM server ready at %s", self.base_url)

  def _spawn_until_ready(self, failures: int) -> None:
    """Spawn + readiness poll, waiting out a transiently unfittable card.

        Pre-flight `ModelLenUnfittableError` means nothing was spawned: the
        card is (likely transiently) contended — the 2026-08-05 B_TABLE R1
        abort recovered 79 s later once sibling embedders demoted. Wait and
        RE-MEASURE in-process (the clamp re-queries VRAM on every spawn
        attempt); only when the window is exhausted does the bundle-retry
        path take over. Never a spawn-failure strike either way.
        """
    for attempt in range(1, _UNFITTABLE_RETRY_ATTEMPTS + 1):
      try:
        self._spawn_server()
        self._wait_until_ready()
        return
      except ModelLenUnfittableError:
        if attempt >= _UNFITTABLE_RETRY_ATTEMPTS:
          raise
        log_milestone(
            "vllm_unfittable_wait",
            level=logging.WARNING,
            attempt=attempt,
            wait_s=_UNFITTABLE_RETRY_WAIT_S,
        )
        time.sleep(_UNFITTABLE_RETRY_WAIT_S)
      except Exception:
        _SPAWN_FAILURES[self.base_url] = failures + 1
        raise

  def _bind_server_locked(self) -> None:
    """Register this client against the process-wide server refcount.

        Caller must hold `_SETUP_LOCK`."""
    if not self._bound:
      self._bound = True
      _SERVER_REFS[self.base_url] = _SERVER_REFS.get(self.base_url, 0) + 1

  def teardown(self) -> None:
    """Release this client's hold on the shared server; terminate the
        server subprocess only when this client is the LAST one bound to it
        (the 2026-07-16 b2 run killed pid=109 out from under 7 sibling
        threads that were still generating against it).

        Safe to call when `setup()` never ran or already torn down.

        Under ``cross_process`` the server is never terminated here: a
        sibling SDK process may still be bound to it, and its refcount is
        not ours to see. The subprocess handle is parked; the worker VM
        reaps it at job end (ADR 0034).
        """
    if self.cross_process:
      self._teardown_cross_process()
      return
    with _SETUP_LOCK:
      self._client = None
      if self._bound:
        self._bound = False
        _SERVER_REFS[self.base_url] = _SERVER_REFS.get(self.base_url, 1) - 1
      still_bound = _SERVER_REFS.get(self.base_url, 0) > 0
      server, self._server = self._server, None
      if server is None:
        # Last client out also reaps a server parked by its spawner.
        if not still_bound:
          server = _PARKED_SERVERS.pop(self.base_url, None)
      elif still_bound:
        # This client spawned the server but siblings still use it —
        # park the handle for the last one out instead of killing it.
        _PARKED_SERVERS[self.base_url] = server
        logger.info(
            "Parking vLLM server subprocess (pid=%s): %d sibling "
            "client(s) still bound.",
            server.pid,
            _SERVER_REFS.get(self.base_url, 0),
        )
        return
      if server is None:
        return
    logger.info("Terminating vLLM server subprocess (pid=%s)", server.pid)
    server.terminate()
    try:
      server.wait(timeout=30)
    except Exception:  # best-effort cleanup — terminate may hang  # pylint: disable=broad-exception-caught
      logger.warning("vLLM server did not exit on SIGTERM; killing.")
      server.kill()
      try:
        server.wait(timeout=10)
      except Exception:  # pylint: disable=broad-exception-caught
        logger.error("vLLM server did not exit on SIGKILL.")

  def _teardown_cross_process(self) -> None:
    with _SETUP_LOCK:
      self._client = None
      was_bound = self._bound
      if self._bound:
        self._bound = False
        _SERVER_REFS[self.base_url] = _SERVER_REFS.get(self.base_url, 1) - 1
      server, self._server = self._server, None
      if server is not None:
        _PARKED_SERVERS[self.base_url] = server
    if not was_bound and server is None:
      # A client that never ignited left nothing running (2026-09-07
      # R7m: 40 kept-alive lines, 38 of them from such clients).
      return
    fields: dict[str, Any] = {"url": self.base_url}
    if server is not None:
      fields["pid"] = getattr(server, "pid", None)
    log_milestone("vllm_server_kept_alive", **fields)
    logger.info(
        "Keeping vLLM server at %s alive (cross-process mode): sibling "
        "SDK processes may still be bound to it.",
        self.base_url,
    )

  # ------------------------------------------------------------------
  # Generation
  # ------------------------------------------------------------------

  def generate_json(
      self,
      prompt: str,
      json_schema: dict,
      *,
      max_tokens: int = 2048,
      temperature: float = 0.7,
      n: int = 1,
      seed: int | None = None,
      top_p: float | None = None,
      top_k: int | None = None,
  ) -> list[dict]:
    """Return up to `n` JSON dicts conforming to `json_schema`.

        Calls the vLLM OpenAI-compatible **chat** endpoint once with `n=`,
        so the server applies Gemma 4's chat template (which lets us suppress
        the thinking channel) and batches the `n` candidates server-side.
        Each `choices[*].message.content` is parsed as JSON; entries that fail
        to parse to a dict are dropped (the engine's repair loop handles
        shortfalls — yielding fewer than `n` is allowed by the contract).

        CAUTION: do NOT rely on `n>1` for output diversity. Under structured
        outputs the V1 engine (which runs seeded, `seed=0`) returned n
        IDENTICAL choices at every temperature/top_p/top_k in the 2026-07-16
        runs — each choice is blind to its siblings, so a "distinct values"
        instruction is unsatisfiable per choice. Callers that need a diverse
        set should request ONE completion carrying an array of values.
        """
    if self._client is None:
      # Lazy ignition (WS1 §3b): the first real LLM call brings the
      # server up. setup() is idempotent and _SETUP_LOCK-serialized,
      # so concurrent DoFn threads still share one server.
      self.setup()

    request: dict = {
        "model": self._served_model_name,
        "messages": [{
            "role": "user",
            "content": prompt
        }],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "n": n,
        # vLLM ≥ 0.10 structured outputs: the schema constraint travels
        # in the OpenAI-standard `response_format`. The legacy
        # `guided_json` / `guided_decoding_backend` extra_body fields are
        # silently ignored by vLLM 0.24 — sending them yields free-form
        # text that fails the strict parse below (2026-07-15 E2E run:
        # 256/256 choices dropped, exemplar-only pools).
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "sdfb_record",
                "schema": json_schema
            },
        },
        "extra_body": {
            # vLLM applies the chat template on this endpoint; pass
            # template kwargs through to suppress the thinking channel on
            # models that have one (ADR 0014). Unknown kwargs are ignored
            # by Jinja.
            "chat_template_kwargs": {
                "enable_thinking": False
            },
        },
    }
    # A per-request seed with n>1 makes vLLM emit n identical
    # completions — only send one when a caller explicitly asks.
    if seed is not None:
      request["seed"] = seed
    # Truncation overrides. A served model can pin top_k/top_p through its
    # generation_config.json (Qwen3-4B ships top_k=20, top_p=0.8), which
    # collapses the nucleus onto exemplar echoes and makes temperature
    # escalation inert (2026-07-16 run: 96/96 verbatim copies at 0.7-1.3).
    # top_p is OpenAI-standard; top_k is vLLM-specific and travels in
    # extra_body (0 = consider all tokens).
    if top_p is not None:
      request["top_p"] = top_p
    if top_k is not None:
      request["extra_body"]["top_k"] = top_k
    response = self._client.chat.completions.create(**request)

    out: list[dict] = []
    for choice in response.choices:
      content = choice.message.content
      parsed = self._parse_json(content)
      if parsed is None:
        logger.warning(
            "vLLM choice content did not parse to a JSON object "
            "(len=%d); dropping. Guided decoding should make this "
            "rare — investigate the schema if it recurs.",
            len(content or ""),
        )
        continue
      out.append(parsed)
    return out

  # ------------------------------------------------------------------
  # Internals — heavy imports stay inside these (laptop-importable class).
  # ------------------------------------------------------------------

  def _pull_weights(self) -> None:
    """Warm-pull `model_uri` (gs://) → `local_model_dir` (ADR 0012)."""
    localize_gcs_prefix(self.model_uri, self.local_model_dir)

  def _probe_reusable_server(self, expected_model: str) -> bool:
    """True when a healthy vLLM server on `host:port` already serves
        `expected_model`. Any failure (nothing listening, non-JSON body,
        different model) means "not reusable" — setup falls through to the
        normal pull → spawn path."""
    from urllib.request import urlopen

    try:
      with urlopen(
          f"{self.base_url}/models", timeout=self.poll_interval_s) as resp:
        if resp.status != _HTTP_OK:
          return False
        payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # pylint: disable=broad-exception-caught
      return False
    models = payload.get("data", []) if isinstance(payload, dict) else []
    return any(
        isinstance(m, dict) and m.get("id") == expected_model for m in models)

  def _assert_gpu_dtype_compatible(self) -> None:
    """Fatal init guard: bf16 weights on a sub-Ampere GPU (e.g. T4) must
        raise `ModelGpuIncompatibleError` here — BEFORE `_spawn_server()` —
        rather than let vLLM stall for ~24 min and the engines silently fall
        back to copying reference exemplars (E2E report §4.2).

        No-ops when torch is absent (laptop), no CUDA device is visible, or
        the pulled model has no `config.json` to inspect — those cases have
        no dtype signal to act on, so we let `_spawn_server()` surface the
        real failure instead of guessing.
        """
    try:
      import torch  # GPU-only path; absent on the laptop
    except ImportError:
      torch = None
    if torch is not None and torch.cuda.is_available():
      from pathlib import Path as _Path

      # Inspect the directory vLLM will actually serve: local_model_dir
      # after a gs:// pull, but the raw model_uri in the already-local
      # weights branch — reading local_model_dir there would silently
      # skip the guard.
      cfg_path = _Path(self._served_model_name) / "config.json"
      if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        _assert_dtype_supported(
            cfg.get("torch_dtype", ""),
            torch.cuda.get_device_capability(),
            dtype_override=str(self.vllm_server_kwargs.get("dtype",
                                                           "")).lower(),
            model_type=cfg.get("model_type", ""),
        )

  def _server_command(self) -> list[str]:
    """Build the `python -m vllm.entrypoints.openai.api_server ...` argv."""
    import sys

    cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        self._served_model_name,
        "--host",
        self.host,
        "--port",
        str(self.port),
    ]
    for key, value in self.vllm_server_kwargs.items():
      flag = f"--{key}"
      if value is True:
        cmd.append(flag)
      elif value is False or value is None:
        continue
      else:
        cmd.extend([flag, str(value)])
    pinned = any(
        k in self.vllm_server_kwargs
        for k in ("gpu-memory-utilization", "gpu_memory_utilization"))
    if not pinned:
      frac = self._dynamic_gpu_memory_utilization()
      if frac is not None:
        cmd.extend(["--gpu-memory-utilization", f"{frac:.3f}"])
        self._clamp_max_model_len(cmd)
    return cmd

  def _model_sizing(self) -> tuple[dict, int] | None:
    """(config dict, checkpoint bytes on disk) for the served model dir,
        or None when the dir cannot be sized (missing/unreadable config.json,
        no *.safetensors / *.bin checkpoint files)."""
    from pathlib import Path as _Path

    model_dir = _Path(self._served_model_name)
    cfg_path = model_dir / "config.json"
    if not cfg_path.exists():
      return None
    try:
      cfg = json.loads(cfg_path.read_text())
    except (OSError, ValueError):
      return None
    weights_bytes = sum(
        f.stat().st_size for f in model_dir.glob("*.safetensors")) or sum(
            f.stat().st_size for f in model_dir.glob("*.bin"))
    if not weights_bytes:
      return None
    return cfg, weights_bytes

  def _query_vram(self) -> tuple[int, int] | None:
    """(free_bytes, total_bytes) for the visible CUDA device, else None
        (no torch on the laptop / no CUDA / query failed)."""
    try:
      import torch

      if not torch.cuda.is_available():
        return None
      return torch.cuda.mem_get_info()
    except Exception:  # pylint: disable=broad-exception-caught
      return None

  def _dynamic_gpu_memory_utilization(self) -> float | None:
    """Utilization fraction derived from FREE VRAM (WS6 F1 + F3).

        vLLM interprets the flag as a fraction of TOTAL device memory, so on
        a card where sibling CUDA contexts (population-branch embedders, a
        prior bundle's residue) hold memory, the 0.9 default over-asks and
        EngineCore init fails. `(free - margin) / total` keeps ignition
        honest about what it can get; the cap stays below vLLM's default
        because CUDA graphs + non-torch overhead land ON TOP of the budget
        (2026-07-29 spawn #3 overflowed a "free" T4 at 0.90 by ~150 MiB).
        None (no torch / no CUDA / query failed) omits the flag entirely —
        today's behaviour.
        """
    vram = self._query_vram()
    if vram is None:
      return None
    free_bytes, total_bytes = vram
    frac = (free_bytes - _VLLM_VRAM_MARGIN_BYTES) / total_bytes
    frac = min(_VLLM_MAX_DYNAMIC_UTILIZATION, frac)
    log_milestone(
        "vllm_gpu_memory_utilization",
        fraction=round(frac, 3),
        free_mib=round(free_bytes / 1024**2),
    )
    return max(0.05, frac)

  def _clamp_max_model_len(self, cmd: list[str]) -> None:
    """Shrink --max-model-len to what the VRAM budget can host (WS6 F3).

        2026-07-29_09_30_47: with embedders holding the T4, the derived
        budget left a 1.11 GiB KV cache — 10 MB short of the 1.12 GiB that
        max_model_len=8192 needs, and vLLM's error even printed the fix
        ("the estimated maximum model length is 8048"). This computes that
        estimate BEFORE the spawn from the served config.json + checkpoint
        bytes on disk, lowers the flag in place (never raises it), and
        raises `ModelLenUnfittableError` when even `_VLLM_MIN_MODEL_LEN`
        cannot fit — a doomed spawn must not burn a spawn-failure strike.

        Best-effort: missing config/weights/geometry leaves `cmd` untouched
        (vLLM then reports whatever is really wrong). Only called on the
        dynamic-utilization path — a pinned utilization means the operator
        took manual control of memory, so we keep our hands off the length.
        """
    vram = self._query_vram()
    sizing = self._model_sizing()
    if vram is None or sizing is None:
      return
    cfg, weights_bytes = sizing
    free_bytes, total_bytes = vram
    budget_bytes = int(
        min(
            free_bytes - _VLLM_VRAM_MARGIN_BYTES,
            _VLLM_MAX_DYNAMIC_UTILIZATION * total_bytes,
        ))
    dtype = str(
        self.vllm_server_kwargs.get("dtype", "") or
        self.vllm_server_kwargs.get("--dtype", "") or
        cfg.get("torch_dtype", "")).lower()
    kv_bpt = _kv_bytes_per_token(cfg, dtype_bytes=4 if "32" in dtype else 2)
    if kv_bpt is None:
      return

    flag_idx = next(
        (i for i, arg in enumerate(cmd)
         if arg in ("--max-model-len", "--max_model_len")),
        None,
    )
    try:
      requested = int(cmd[flag_idx + 1] if flag_idx is not None else cfg
                      .get("max_position_embeddings"))
    except (TypeError, ValueError):
      return

    fitted = _fit_max_model_len(
        requested,
        budget_bytes=budget_bytes,
        weights_bytes=weights_bytes,
        kv_bytes_per_token=kv_bpt,
    )
    if fitted >= requested:
      return
    if fitted < _VLLM_MIN_MODEL_LEN:
      log_milestone(
          "vllm_max_model_len_unfittable",
          level=logging.ERROR,
          requested=requested,
          fitted=fitted,
          free_mib=round(free_bytes / 1024**2),
      )
      raise ModelLenUnfittableError(
          f"The measured VRAM budget ({budget_bytes / 1024**3:.2f} GiB) "
          f"fits a max_model_len of {fitted}, below the minimum viable "
          f"{_VLLM_MIN_MODEL_LEN} (pool builds need max_tokens=2048 "
          "plus the prompt). Not spawning a doomed server; the next "
          "bundle attempt re-measures the card — sibling embedders "
          "may have released it by then.")
    log_milestone(
        "vllm_max_model_len_clamped",
        level=logging.WARNING,
        requested=requested,
        fitted=fitted,
        free_mib=round(free_bytes / 1024**2),
    )
    if flag_idx is not None:
      cmd[flag_idx + 1] = str(fitted)
    else:
      cmd.extend(["--max-model-len", str(fitted)])

  def _spawn_server(self) -> None:
    import os
    import subprocess

    cmd = self._server_command()
    # Expandable segments avoid the allocator fragmentation both OOM
    # spawns of 2026-07-29 pointed at ("If reserved but unallocated
    # memory is large try setting PYTORCH_CUDA_ALLOC_CONF...").
    env = os.environ.copy()
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    logger.info("Spawning vLLM server: %s", " ".join(cmd))
    self._server = subprocess.Popen(cmd, env=env)

  def _wait_until_ready(self) -> None:
    """Poll `/v1/models` until the server answers 200, or time out.

        Fails fast if the subprocess dies during startup (so a bad weight
        path / OOM surfaces as an error instead of a silent timeout).
        """
    from urllib.error import URLError
    from urllib.request import urlopen

    models_url = f"{self.base_url}/models"
    deadline = time.monotonic() + self.startup_timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
      if self._server is not None and self._server.poll() is not None:
        # Our subprocess died. Before calling that fatal, check
        # whether we simply LOST A RACE for the fixed port: a sibling
        # attempt's server may already be healthy and serving, in
        # which case ours exited "address already in use" and the
        # right move is to adopt the winner (2026-07-26_17_10_37 E2E:
        # 3 spawns on --port 8000 in 72s; the losers raised here,
        # crashing DoFn.setup(), and the retry then OOMed the
        # embedder against the winner's VRAM — 11 retries, 12 OOMs,
        # while a healthy server was serving 4 requests throughout).
        returncode = self._server.returncode
        if self._probe_reusable_server(self._served_model_name):
          log_milestone(
              "vllm_spawn_lost_race",
              returncode=returncode,
              url=self.base_url,
          )
          logger.warning(
              "Our vLLM subprocess exited (code %s) but a healthy "
              "server is already serving %r at %s — adopting it.",
              returncode,
              self._served_model_name,
              self.base_url,
          )
          # Not ours to terminate: teardown() must never kill a
          # server another client spawned and still depends on.
          self._server = None
          return
        raise RuntimeError(
            "vLLM server subprocess exited during startup with code "
            f"{returncode}. Check the model path "
            f"({self._served_model_name!r}) and vllm_server_kwargs.")
      try:
        with urlopen(models_url, timeout=self.poll_interval_s) as resp:
          if resp.status == _HTTP_OK:
            return
      except URLError as e:  # not up yet — keep polling
        last_err = e
      except Exception as e:  # connection refused / reset during boot  # pylint: disable=broad-exception-caught
        last_err = e
      time.sleep(self.poll_interval_s)
    raise TimeoutError(
        f"vLLM server did not become ready at {models_url} within "
        f"{self.startup_timeout_s}s. Last error: {last_err!r}")

  def _build_openai_client(self) -> Any:
    from openai import OpenAI

    # The local vLLM server ignores the key, but the client requires a
    # non-empty value. This is NOT an external API call (ADR 0001 / hard
    # constraint #4) — base_url points at localhost.
    return OpenAI(base_url=self.base_url, api_key="EMPTY")

  @staticmethod
  def _parse_json(content: str | None) -> dict | None:
    """Parse guided-decoding output into a dict; None if not a JSON object.

        Guided JSON makes the content a bare JSON object, so a strict
        `json.loads` is enough — no lenient brace-scanning like the MLX path
        (which has no token-level grammar constraint).
        """
    if not content:
      return None
    try:
      parsed = json.loads(content)
    except json.JSONDecodeError:
      return None
    return parsed if isinstance(parsed, dict) else None


# Re-exported for backwards compatibility; the canonical home is `sdfb_beam.gcs`.
_split_gs_uri = split_gs_uri  # pylint: disable=invalid-name
