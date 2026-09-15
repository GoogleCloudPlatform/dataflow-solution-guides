"""Structured prompt-constraint templates carried in column descriptions.

ADR 0024: the ``"llm_prompt_constraint"`` marker (ADR 0021) accepts an
object as well as the legacy string. This module is the single parse site
and the single renderer — both engines' profilers consume it, so they can
never disagree about what a description means.

Compatibility contract:
  * legacy string form ⇒ ``notes`` only, and the rendered clause is the
    normalized string itself, byte-identical to pre-wave-2 prompts;
  * unknown object keys are skipped with a WARNING milestone (a newer DDL
    never breaks an older engine);
  * a marked object with invalid known keys raises loudly — a half-parsed
    constraint silently steering a prompt is worse than a stop (ADR 0021
    parsing rule).
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from sdfb_core.contracts.description_json import (
    DescriptionJsonError,
    extract_embedded_json,
)
from sdfb_core.observability import log_milestone

# Prompt steering, not schema enforcement: cap what a description can inject
# into an LLM prompt (2026-08-05 spec, C5 guardrails).
_MAX_CONSTRAINT_CHARS = 500
_MAX_PATTERN_CHARS = 200
_MAX_EXAMPLES = 8
_MAX_EXAMPLE_CHARS = 64
_MAX_VALUES = 64

_CONSTRAINT_MARKER = "llm_prompt_constraint"


def _one_line(s: str) -> str:
  return " ".join(s.split())


class PromptConstraint(BaseModel):
  """Typed per-column generation hints (wave-2 design doc §3).

    Every field optional; empty strings/tuples mean "not given". ``length``
    normalizes to an inclusive ``(min, max)`` band; a bare int is a fixed
    width.
    """

  model_config = ConfigDict(frozen=True)

  format: str = ""
  pattern: str = ""
  examples: tuple[str, ...] = ()
  values: tuple[str, ...] = ()
  prefix: str = ""
  suffix: str = ""
  charset: str = ""
  length: tuple[int, int] | None = None
  units: str = ""
  locale: str = ""
  route: Literal["auto", "llm"] = "auto"
  notes: str = ""
  # Prefix-family shares for Tier-P pattern sampling (ADR 0028 D5):
  # the structured replacement for prose percentages. Accepts
  # {"C2E3": 0.57, ...} or [["C2E3", 0.57], ...]; shares are
  # normalized by the sampler. Not rendered into the prompt clause —
  # its consumer is the sampler, the prose `format` keeps steering
  # the LLM when one is involved.
  families: tuple[tuple[str, float], ...] = ()

  @field_validator("format", "prefix", "suffix", "charset", "units", "locale",
                   "notes")
  @classmethod
  def _normalized(cls, v: str) -> str:
    return _one_line(v)[:_MAX_CONSTRAINT_CHARS]

  @field_validator("pattern")
  @classmethod
  def _compilable(cls, v: str) -> str:
    v = v.strip()[:_MAX_PATTERN_CHARS]
    if v:
      try:
        re.compile(v)
      except re.error as exc:
        raise ValueError(f"pattern does not compile: {exc}") from exc
    return v

  @field_validator("examples", mode="after")
  @classmethod
  def _cap_examples(cls, v: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_one_line(e)[:_MAX_EXAMPLE_CHARS] for e in v[:_MAX_EXAMPLES])

  @field_validator("values", mode="after")
  @classmethod
  def _cap_values(cls, v: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_one_line(e)[:_MAX_EXAMPLE_CHARS] for e in v[:_MAX_VALUES])

  @field_validator("length", mode="before")
  @classmethod
  def _band(cls, v: object) -> object:
    if isinstance(v, int) and not isinstance(v, bool):
      return (v, v)
    return v

  @field_validator("families", mode="before")
  @classmethod
  def _family_pairs(cls, v: object) -> object:
    if isinstance(v, dict):
      return tuple(v.items())
    return v


def parse_prompt_constraint(description: str | None,
                            column: str = "") -> PromptConstraint | None:
  """The column's constraint, or None when the description is unmarked.

    String value ⇒ legacy prose (``notes``). Object value ⇒ typed keys;
    unknown keys are skipped with a ``prompt_constraint_unknown_keys``
    WARNING (forward compatibility), invalid known keys raise
    :class:`DescriptionJsonError` (loud stop). ``column`` names the owner
    in both — a keys warning or a validation error without the column was
    undebuggable against a 67-column DDL (2026-08-20 follow-up).
    """
  obj = extract_embedded_json(description, _CONSTRAINT_MARKER)
  if obj is None:
    return None
  value = obj.get(_CONSTRAINT_MARKER)
  if isinstance(value, str):
    return PromptConstraint(notes=value)
  if not isinstance(value, dict):
    return None
  known = set(PromptConstraint.model_fields)
  unknown = sorted(set(value) - known)
  if unknown:
    log_milestone(
        "prompt_constraint_unknown_keys",
        level=logging.WARNING,
        column=column,
        keys=",".join(unknown),
    )
  try:
    return PromptConstraint.model_validate({
        k: v for k, v in value.items() if k in known
    })
  except ValidationError as exc:
    owner = f" on column {column!r}" if column else ""
    raise DescriptionJsonError(
        f"llm_prompt_constraint object{owner} failed validation: {exc}"
    ) from exc


def render_prompt_clause(pc: PromptConstraint) -> str:
  """Compact, deterministic single-line clause for the pool prompt.

    Key order is fixed so the clause is a per-column CONSTANT — the prompt
    keeps its byte-identical shared prefix for vLLM automatic prefix
    caching (ADR 0018). A notes-only constraint renders as the notes text
    alone (legacy byte-compat pin).
    """
  parts: list[str] = []
  if pc.format:
    parts.append(f"format={pc.format}")
  if pc.length is not None:
    lo, hi = pc.length
    parts.append(f"length={lo}" if lo == hi else f"length={lo}-{hi}")
  if pc.charset:
    parts.append(f"charset={pc.charset}")
  if pc.prefix:
    parts.append(f"prefix={pc.prefix}")
  if pc.suffix:
    parts.append(f"suffix={pc.suffix}")
  if pc.pattern:
    parts.append(f"pattern={pc.pattern}")
  if pc.values:
    parts.append(f"allowed values={list(pc.values)}")
  if pc.units:
    parts.append(f"units={pc.units}")
  if pc.locale:
    parts.append(f"locale={pc.locale}")
  if pc.examples:
    parts.append(f"fictitious examples={list(pc.examples)}")
  if pc.notes:
    parts.append(pc.notes)
  return _one_line("; ".join(parts))[:_MAX_CONSTRAINT_CHARS]


__all__ = [
    "PromptConstraint",
    "parse_llm_prompt_constraint",
    "parse_prompt_constraint",
    "render_prompt_clause",
]


def parse_llm_prompt_constraint(description: str | None,
                                column: str = "") -> str:
  """Rendered per-column prompt clause from a COLUMN description, or "".

    Column-level steering stays in the column description (ADR 0024): it
    describes how to generate one field's values, so it belongs next to
    that field. Only the RELATIONAL contract moved out to
    `config/relationships/` (ADR 0032). The legacy string form renders
    byte-identically to its own normalized text; a malformed marked
    object still raises via the extractor/validator.
    """
  pc = parse_prompt_constraint(description, column=column)
  return "" if pc is None else render_prompt_clause(pc)
