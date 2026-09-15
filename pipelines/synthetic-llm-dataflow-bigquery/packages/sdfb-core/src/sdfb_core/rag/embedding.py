"""The `Embedder` seam for the B.1 RAG engine.

The engine never imports `transformers` / `torch` at module scope. It
depends only on the `Embedder` Protocol below. Two implementations:

  - `BgeEmbedder` — production. Loads `bge-small-en-v1.5` (384-dim) from a
    **local directory** (weights mirrored to GCS, pulled once on the M4).
    `transformers` / `torch` / `safetensors` are imported lazily inside
    `__init__`, so importing this module on a bare laptop never drags in
    those deps. NEVER calls `from_pretrained("BAAI/...")` against the Hub —
    only a local path. `HF_HUB_OFFLINE=1` is therefore safe.

  - `HashingEmbedder` — deterministic, dependency-free fallback used by the
    contract tests and as the engine's zero-config default when the real
    embedder's deps are not installed. It maps text → a fixed-dim unit
    vector via a seeded hash, so retrieval is exact and reproducible with
    no model download. NOT a semantic embedder — it exists so the engine is
    importable and testable on the laptop (the spine's fidelity primitives
    do the statistical work; the embedder only conditions which exemplars
    are retrieved).

REFs:
  - bge-small-en-v1.5: BAAI, MIT license, mirrored per config/models.yml
  - GReaT row serialization (what we embed): arXiv 2210.06280
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import hashlib
import logging
import math
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sdfb_core.observability import log_milestone

# bge-small-en-v1.5 is ~130 MB of fp32 weights; activations and CUDA context
# push the real footprint higher. Require this much FREE VRAM before "auto"
# picks CUDA. The 2026-07-26 E2E asked for 2 MiB with 2.81 MiB free and
# OOMed: cuda.is_available() answers "does a GPU exist", never "is there
# room", and on a DoFn.setup() RETRY the module-level vLLM server reuse
# (ADR 0014) means the card is already spoken for.
_MIN_FREE_VRAM_BYTES = 512 * 1024**2


def _resolve_auto_device(torch) -> str:
  """``device="auto"`` = CUDA **if there is room**, else CPU."""
  cuda = getattr(torch, "cuda", None)
  if cuda is None or not cuda.is_available():
    return "cpu"
  mem_get_info = getattr(cuda, "mem_get_info", None)
  if mem_get_info is None:
    # Older/stubbed torch: no way to ask. Keep the historical behaviour
    # rather than refusing the GPU outright.
    return "cuda"
  try:
    free_bytes = mem_get_info()[0]
  except Exception:  # pylint: disable=broad-exception-caught
    return "cuda"
  if free_bytes >= _MIN_FREE_VRAM_BYTES:
    return "cuda"
  log_milestone(
      "embedder_cuda_no_room",
      level=logging.WARNING,
      free_mib=round(free_bytes / 1024**2, 1),
      needed_mib=round(_MIN_FREE_VRAM_BYTES / 1024**2),
  )
  return "cpu"


def _is_cuda_oom(torch, exc: BaseException) -> bool:
  """True for torch's CUDA OOM, however this torch version spells it."""
  oom = getattr(torch, "OutOfMemoryError", None)
  if oom is not None and isinstance(exc, oom):
    return True
  return "out of memory" in str(exc).lower()


if TYPE_CHECKING:  # pragma: no cover - typing only
  from collections.abc import Sequence


@runtime_checkable
class Embedder(Protocol):
  """Maps a batch of strings to fixed-dimension float vectors.

    Returned vectors are plain Python (``list[list[float]]``) so the seam
    never forces a NumPy dependency on callers. They need NOT be
    L2-normalized — the index normalizes on add/query.
    """

  @property
  def dim(self) -> int:
    """Embedding dimensionality (e.g. 384 for bge-small)."""

  def embed(self, texts: Sequence[str]) -> list[list[float]]:
    """Return one vector per input string, in input order."""


class HashingEmbedder:
  """Deterministic, dependency-free `Embedder`.

    Feature-hashing: each whitespace token contributes a signed unit to a
    bucket chosen by a salted SHA-256 of the token. The result is L2
    near-normalized but, more importantly, **identical across processes and
    runs** for the same input — which is exactly what the contract's
    `test_seed_reproducibility` and the "deterministic top-k" acceptance
    criterion require, with zero model download.

    Use `seed` to decorrelate buckets between independent indexes.
    """

  def __init__(self, dim: int = 384, seed: int = 0) -> None:
    if dim <= 0:
      raise ValueError(f"dim must be positive, got {dim}")
    self._dim = dim
    self._seed = seed

  @property
  def dim(self) -> int:
    return self._dim

  def embed(self, texts: Sequence[str]) -> list[list[float]]:
    return [self._embed_one(t) for t in texts]

  def _embed_one(self, text: str) -> list[float]:
    vec = [0.0] * self._dim
    tokens = text.split() or [text]
    for tok in tokens:
      bucket, sign = self._bucket_and_sign(tok)
      vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
      # Degenerate (e.g. empty text): put unit mass in a stable bucket.
      vec[0] = 1.0
      return vec
    return [v / norm for v in vec]

  def _bucket_and_sign(self, token: str) -> tuple[int, float]:
    h = hashlib.sha256()
    h.update(str(self._seed).encode("utf-8"))
    h.update(b"\x00")
    h.update(token.encode("utf-8"))
    digest = h.digest()
    bucket = int.from_bytes(digest[:8], "big") % self._dim
    sign = 1.0 if (digest[8] & 1) == 0 else -1.0
    return bucket, sign


class BgeEmbedder:
  """Production `Embedder` wrapping `bge-small-en-v1.5` on CPU or CUDA.

    Loads from a **local directory** only (weights mirrored to GCS, warm-
    pulled on the M4). `transformers` + `torch` are imported lazily — on
    first use, never at module scope or construction — so this class can be
    referenced (and the module imported) on a laptop without the
    `[embedding]` extra installed. Mean-pooled, L2-normalized CLS-free
    pooling per the bge recipe.

    **Lazy by design (ADR 0034).** Construction records the path and the
    requested device and loads NOTHING; `ensure_loaded()` (called by
    `embed()`) imports the stack, resolves the device, loads the weights and
    moves them — once. A store-warm generate setup (row-doc vectors from
    `rag_chunks`, pools from `freetext_pools`) never embeds a text, and the
    2026-08-29 R6 pair built 32 such embedders per table, each eagerly
    loading 130 MB of weights into a CUDA context beside vLLM.

    ``device="auto"`` resolves to CUDA when available and roomy (the
    2026-07-25 E2E embedded 33,610 chunks on CPU for 25 min while both T4s
    idled) — and callers MUST `demote_to_cpu()` once bulk embedding is done,
    because vLLM's ignition sizes its KV-cache budget from free GPU memory
    (ADR 0019). A demote BEFORE any load pins the eventual load to CPU: the
    caller has handed the card to vLLM, and a later seed-example embed is
    tiny.

    This class is exercised on the M4 (mark such tests `@pytest.mark.gpu`
    or guard on import availability); the contract tests use
    `HashingEmbedder` instead.
    """

  # Loading is serialized per process (2026-07-24 E2E postmortem):
  # transformers v5's lazy `_LazyModule` is not thread-safe — 8 Beam bundle
  # threads hitting the first `from transformers import AutoModel`
  # concurrently raise `ImportError: cannot import name 'AutoModel'` for
  # most of them (reproduced 30/30 locally on 5.8.1). Holding the lock over
  # `from_pretrained` too keeps concurrent mmap weight-loads from stacking
  # up. Instances stay per-caller: HF fast tokenizers are NOT safe to
  # share across threads ("Already borrowed"), so we serialize the load,
  # not the object.
  _construction_lock: threading.Lock = threading.Lock()

  def __init__(
      self,
      model_path: str,
      *,
      dim: int = 384,
      max_length: int = 512,
      device: str = "cpu",
  ) -> None:
    self._model_path = model_path
    self._dim = dim
    self._max_length = max_length
    # The requested device ("auto" | "cuda" | "cpu"); resolved at load.
    self._requested = device
    self._device = device
    self._torch: Any = None
    self._tokenizer: Any = None
    self._model: Any = None
    self._loaded = False

  @property
  def dim(self) -> int:
    return self._dim

  @property
  def device(self) -> str:
    """The resolved device once loaded; the requested one before."""
    return self._device

  @property
  def loaded(self) -> bool:
    return self._loaded

  def ensure_loaded(self) -> None:
    """Import the HF stack, resolve the device and load the weights —
        once, process-serialized. Idempotent."""
    if self._loaded:
      return
    with BgeEmbedder._construction_lock:
      if self._loaded:
        return
      # Lazy heavy imports — never at module scope (keeps sdfb-core
      # pure).
      import torch
      from transformers import AutoModel, AutoTokenizer

      requested = self._requested
      device = requested
      if device == "auto":
        device = _resolve_auto_device(torch)
      # One milestone at the seam covers every embedder user (engine
      # setup AND the population EmbedChunksDoFn): worker logs must
      # show whether bulk embedding actually ran on CUDA — the
      # 2026-07-25 06:18 E2E burned 25 min on CPU with both T4s idle
      # and nothing in the logs said so.
      log_milestone("embedder_device", device=device, requested=requested)
      self._torch = torch
      self._device = device
      # local_files_only=True is belt-and-braces on top of
      # HF_HUB_OFFLINE=1: a local path with this flag can never reach
      # the Hub.
      self._tokenizer = AutoTokenizer.from_pretrained(
          self._model_path, local_files_only=True)
      model = AutoModel.from_pretrained(self._model_path, local_files_only=True)
      # Belt and braces on top of the free-VRAM check above: another
      # process can fill the card between the check and the move. A
      # slower CPU embedder is right; a failed DoFn.setup() is not —
      # it makes Dataflow retry the bundle, which is how the
      # 2026-07-26 run turned one OOM into 11 retries.
      try:
        self._model = model.to(device)
      except Exception as exc:  # pylint: disable=broad-exception-caught
        if device != "cuda" or not _is_cuda_oom(torch, exc):
          raise
        log_milestone(
            "embedder_cuda_oom_fallback",
            level=logging.WARNING,
            error=type(exc).__name__,
        )
        device = "cpu"
        self._device = device
        self._model = model.to(device)
      self._model.eval()
      self._loaded = True

  def demote_to_cpu(self) -> None:
    """Move weights to CPU and release the CUDA cache. Idempotent.

        Callers demote as soon as bulk embedding is done: vLLM's ignition
        sizes its KV-cache budget from free GPU memory, so a resident
        embedder must not still be holding VRAM by then (ADR 0019). Before
        any load there is nothing to release — the eventual load is pinned
        to CPU instead, so a demoted embedder can never take VRAM later."""
    if not self._loaded:
      self._requested = "cpu"
      self._device = "cpu"
      return
    if self._device != "cuda":
      return
    self._model = self._model.to("cpu")
    self._device = "cpu"
    cuda = getattr(self._torch, "cuda", None)
    if cuda is not None:
      cuda.empty_cache()
    # Logged only on a real cuda→cpu transition: its presence in worker
    # logs proves VRAM was released BEFORE vLLM sized its KV cache.
    log_milestone("embedder_demoted")

  def embed(self, texts: Sequence[str]) -> list[list[float]]:
    self.ensure_loaded()
    torch = self._torch
    out: list[list[float]] = []
    # Modest batches keep CPU memory bounded on a 10k-row reference.
    batch = 64
    text_list = list(texts)
    with torch.no_grad():
      for start in range(0, len(text_list), batch):
        chunk = text_list[start:start + batch]
        enc = self._tokenizer(
            chunk,
            padding=True,
            truncation=True,
            max_length=self._max_length,
            return_tensors="pt",
        ).to(self._device)
        model_out = self._model(**enc)
        # bge uses mean pooling over the last hidden state.
        token_emb = model_out.last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).type_as(token_emb)
        summed = (token_emb * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        pooled = summed / counts
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        out.extend(pooled.cpu().tolist())
    return out


_DEFAULT_EMBEDDER_IDENTITY = ("hashing-384", "v1")
_MIN_PARTS_FOR_IDENTITY = 2


def embedder_identity(embedder_uri: str) -> tuple[str, str]:
  """``(embedder_id, embedder_version)`` from a MODEL_LAYOUT embedder URI.

    The layout pins ``.../embedders/{id}/{version}/`` — the last two
    non-empty path segments. Must be derived from the ORIGINAL URI at
    graph-construction time: on the worker the path is already localized
    (``/local-ssd/embedder``) and the identity is gone. Empty URI ⇒ the
    dependency-free HashingEmbedder's fixed identity.
    """
  if not embedder_uri:
    return _DEFAULT_EMBEDDER_IDENTITY
  parts = [s for s in embedder_uri.replace("gs://", "").split("/") if s]
  if len(parts) >= _MIN_PARTS_FOR_IDENTITY:
    return (parts[-2], parts[-1])
  return (parts[0], "v1")


__all__ = ["BgeEmbedder", "Embedder", "HashingEmbedder", "embedder_identity"]
