"""MLX-backed `ModelClient` for M4 Apple-Silicon local smoke tests.

This is the M4 counterpart to `VLLMModelClient` (Linux + L4 GPU). The
two satisfy the same `ModelClient` Protocol; engines never know which
one they have. See [ADR 0010](../../../../docs/adr/0010-m4-local-smoke-mlx.md)
for the rationale.

Install: `uv sync --package sdfb-beam --extra mlx` on the M4. The marker
`sys_platform == 'darwin'` in pyproject.toml prevents accidental install
on Linux. The `--package sdfb-beam` selector is required because the
`[mlx]` extra lives on the workspace member, not on the root.

REFs:
  - mlx-lm: https://github.com/ml-explore/mlx-examples/tree/main/llms
  - docs/M4_LOCAL_SMOKE.md
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


class MLXModelClient:
  """`ModelClient` impl backed by `mlx-lm` on Apple Silicon.

    Loads weights from a local directory (HF-layout safetensors). Falls back
    to a JSON-schema instruction in the prompt for structured output — MLX
    does not have native guided-JSON decoding like vLLM does, so we rely on
    Gemma 4's native function-calling training + post-validate-and-retry.

    Lifecycle:
        c = MLXModelClient(model_uri="./models/gemma4/e4b-it/v1/")
        c.setup()                       # downloads / loads model (~30s)
        rows = c.generate_json(prompt, schema, n=5)
    """

  def __init__(self, model_uri: str, max_tokens: int = 4096) -> None:
    # Strip gs:// scheme if present — MLX wants a local path.
    self.model_uri = model_uri.removeprefix("gs://")
    self.default_max_tokens = max_tokens
    self._model = None  # mlx_lm.Model
    self._tokenizer = None
    self._sampler = None

  def setup(self) -> None:
    """Lazy import + load. Idempotent."""
    if self._model is not None:
      return
    try:
      from pathlib import Path

      from mlx_lm import sample_utils
      from mlx_lm.utils import load_model, load_tokenizer
    except ImportError as e:
      raise ImportError(
          "mlx-lm is not installed. Run "
          "`uv sync --package sdfb-beam --extra mlx` on Apple Silicon. "
          "MLXModelClient is not available on Linux/x86.") from e

    logger.info("Loading MLX model from %s", self.model_uri)
    # We deliberately bypass `mlx_lm.load()` (which forces strict=True) and
    # call the lower-level loaders with strict=False. Gemma 4 multimodal
    # checkpoints (Gemma4ForConditionalGeneration) carry redundant
    # k_proj/v_proj/k_norm weights for the shared-KV layers
    # (text_config.num_kv_shared_layers=18 in E4B → layers 24-41). mlx-lm's
    # Gemma 4 implementation correctly omits these from the model graph,
    # so they are never read at inference. The strict load just rejects
    # them on principle; relaxing it is safe.
    model_path = Path(self.model_uri)
    self._model, config = load_model(model_path, lazy=False, strict=False)
    self._tokenizer = load_tokenizer(
        model_path,
        eos_token_ids=config.get("eos_token_id"),
    )
    self._sampler = sample_utils
    logger.info("MLX model loaded.")

  def generate_json(
      self,
      prompt: str,
      json_schema: dict,
      *,
      max_tokens: int | None = None,
      temperature: float = 0.7,
      n: int = 1,
      # ModelClient Protocol knobs this best-effort client does not forward.
      seed: int | None = None,  # pylint: disable=unused-argument
      top_p: float | None = None,  # pylint: disable=unused-argument
      top_k: int | None = None,  # pylint: disable=unused-argument
  ) -> list[dict]:
    """Call the LLM `n` times, parse each output as JSON.

        Schema-conformance is best-effort here (no token-level grammar
        constraint). Use `mlx_lm`'s sampler with low temperature + the
        json_schema injected into the prompt; post-validate via Pydantic
        downstream and reroll on failure (engine's repair loop).
        """
    if self._model is None:
      self.setup()
    from mlx_lm import generate

    max_tokens = max_tokens or self.default_max_tokens
    wrapped_prompt = self._wrap_with_schema(prompt, json_schema)
    # Gemma 4 Instruct expects the chat template (`<start_of_turn>user …
    # <end_of_turn>\n<start_of_turn>model\n`). Without it, the model
    # treats input as a raw completion, never emits EOS, and runs to
    # max_tokens.
    try:
      # enable_thinking=False suppresses Gemma 4's chain-of-thought
      # channel, which otherwise spends ~1-1.5k tokens enumerating the
      # anchor before emitting JSON — starving the token budget and
      # truncating the object. Unknown template kwargs are ignored by
      # Jinja, so this is safe even if the template doesn't use it.
      chat_prompt = self._tokenizer.apply_chat_template(
          [{
              "role": "user",
              "content": wrapped_prompt
          }],
          add_generation_prompt=True,
          tokenize=False,
          enable_thinking=False,
      )
    except ValueError:
      # tokenizer_config.json has no chat_template. This usually means
      # the Kaggle download was the BASE Gemma 4 E4B, not the
      # instruction-tuned variant (`-it`). Base models don't follow
      # instructions, so JSON output quality will be poor even with
      # the hardcoded template. Pull the IT variant before declaring
      # smoke success.
      logger.warning("Tokenizer has no chat_template — falling back to the "
                     "hardcoded Gemma template. This is likely the BASE model; "
                     "pull the `-it` variant from Kaggle for usable output.")
      chat_prompt = ("<start_of_turn>user\n"
                     f"{wrapped_prompt}<end_of_turn>\n"
                     "<start_of_turn>model\n")
    # mlx-lm ≥0.20 routes sampling params through a sampler callable
    # instead of accepting temp= directly on generate().
    sampler = self._sampler.make_sampler(temp=temperature)

    out: list[dict] = []
    for _ in range(n):
      t0 = time.perf_counter()
      text = generate(
          self._model,
          self._tokenizer,
          prompt=chat_prompt,
          max_tokens=max_tokens,
          sampler=sampler,
          verbose=False,
      )
      elapsed = time.perf_counter() - t0
      n_tok = len(self._tokenizer.encode(text))
      hit_cap = n_tok >= max_tokens - 1
      logger.info(
          "MLX generated %d tok in %.1fs (%.1f tok/s)%s",
          n_tok,
          elapsed,
          n_tok / elapsed if elapsed else 0.0,
          " [HIT max_tokens — output likely truncated]" if hit_cap else "",
      )
      payload = self._extract_first_json_object(text)
      if payload is None:
        logger.warning(
            "MLX output not parseable as JSON (chars=%d, hit_cap=%s). "
            "Set log level DEBUG to see the raw text.",
            len(text),
            hit_cap,
        )
        logger.debug("Unparseable MLX output:\n%s", text)
        continue
      out.append(payload)
    return out

  # ------------------------------------------------------------------
  # Helpers
  # ------------------------------------------------------------------

  @staticmethod
  def _wrap_with_schema(prompt: str, json_schema: dict) -> str:
    """Embed the JSON schema in the prompt — MLX has no guided decoding."""
    return (f"{prompt}\n\n"
            "Respond with ONE JSON object conforming to this schema, and "
            "nothing else (no markdown, no commentary):\n"
            f"```json\n{json.dumps(json_schema, indent=2)}\n```\n"
            "JSON:")

  @staticmethod
  def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first valid JSON object out of an LLM response.

        Robust to: markdown code fences, leading/trailing prose, braces that
        appear *inside* string values, and a single retry with trailing commas
        stripped (a common LLM artifact). Scans left-to-right for balanced
        `{…}` spans (string-aware) and returns the first that parses to a dict.
        Returns None if nothing parses (e.g. the output was truncated before
        the object closed).
        """
    n = len(text)
    i = 0
    while i < n:
      if text[i] != "{":
        i += 1
        continue
      close = MLXModelClient._find_balanced_close(text, i)
      if close == -1:
        # No balanced close — truncated. No later '{' can do better.
        return None
      parsed = MLXModelClient._loads_lenient(text[i:close + 1])
      if isinstance(parsed, dict):
        return parsed
      # This span didn't parse to a dict; try the next '{' to the right.
      i += 1
    return None

  @staticmethod
  def _find_balanced_close(text: str, start: int) -> int:
    """Index of the `}` that closes the `{` at `start`, or -1 if unbalanced.

        String-aware: braces inside `"..."` values (and escaped quotes) don't
        affect nesting depth.
        """
    depth = 0
    in_str = False
    escaped = False
    for j in range(start, len(text)):
      ch = text[j]
      if in_str:
        if escaped:
          escaped = False
        elif ch == "\\":
          escaped = True
        elif ch == '"':
          in_str = False
        continue
      if ch == '"':
        in_str = True
      elif ch == "{":
        depth += 1
      elif ch == "}":
        depth -= 1
        if depth == 0:
          return j
    return -1

  @staticmethod
  def _loads_lenient(candidate: str) -> Any | None:
    """`json.loads`, then one retry with trailing commas removed."""
    try:
      return json.loads(candidate)
    except json.JSONDecodeError:
      pass
    # Trailing comma before a closing brace/bracket is the most common
    # LLM artifact. Only applied after a strict parse already failed.
    repaired = re.sub(r",(\s*[}\]])", r"\1", candidate)
    try:
      return json.loads(repaired)
    except json.JSONDecodeError:
      return None
