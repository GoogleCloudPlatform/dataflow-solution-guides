"""Structured worker-log milestones — the log-mining contract.

One stable, greppable line per milestone:

    SDFB_MILESTONE name=<milestone> key=value key='quoted value' ...

``scripts/e2e/e2e_gcp_probe.py`` mines Dataflow worker logs for this prefix to
derive engine execution timings. The format is therefore an API: fields are
emitted in sorted order, values containing whitespace are single-quoted via
``shlex.quote``, and the line never contains a newline. Change nothing here
without updating the probe and the contract test together.

This module is pure stdlib (``logging``/``shlex``/``re``) — sdfb-core must
stay Beam-free. Beam metric counterparts live in ``sdfb_beam``.
"""

# Heavy or optional dependencies are imported lazily, where they are used.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
from contextvars import ContextVar

MILESTONE_PREFIX = "SDFB_MILESTONE"

# name= then sorted key=value tokens; values may be shlex-quoted.
_MILESTONE_RE = re.compile(
    rf"{MILESTONE_PREFIX} name=(?P<name>[a-z0-9_]+)(?P<fields>.*)$")

_logger = logging.getLogger("sdfb.milestone")


def format_milestone(name: str, **fields) -> str:
  if not re.fullmatch(r"[a-z0-9_]+", name):
    raise ValueError(f"milestone name {name!r} must fully match [a-z0-9_]+ "
                     "(lowercase_underscore only) — the parser anchors on this "
                     "pattern, so a non-conforming name would produce a line "
                     "`parse_milestone` can't mine back out")
  parts = [f"{MILESTONE_PREFIX} name={name}"]
  for key in sorted(fields):
    value = str(fields[key]).replace("\n", " ")
    parts.append(f"{key}={shlex.quote(value)}")
  return " ".join(parts)


def log_prompt_debug(mode: str, column: str, prompt: str,
                     redacted_prompt: str) -> None:
  """Log one built pool prompt per the `--prompt_debug` contract
    (ADR 0024 §3c).

    "off" logs nothing (production default — reference values are banned
    from logs). "redacted" logs the instruction + rendered constraint with
    seed exemplars elided, at INFO. "full" logs the verbatim prompt —
    reference exemplars INCLUDED — at WARNING, so a debug run's leak
    surface is loud in Dataflow logs. The sha12 of the FULL prompt is
    logged in both modes, so prompt drift across runs is comparable even
    when only redacted text was captured.
    """
  if mode not in ("redacted", "full"):
    return
  log_milestone(
      "freetext_pool_prompt",
      level=logging.WARNING if mode == "full" else logging.INFO,
      column=column,
      mode=mode,
      prompt_chars=len(prompt),
      sha12=sha12(prompt),
      text=prompt if mode == "full" else redacted_prompt,
  )


def sha12(text: str) -> str:
  """First 12 hex chars of the text's sha256 — the drift-comparison key
    used for pool prompts and rendered constraint clauses alike."""
  import hashlib

  return hashlib.sha256(text.encode()).hexdigest()[:12]


# Multi-table runs interleave N engines' milestones in one worker log
# (ADR 0030): the active table rides a ContextVar so EVERY milestone in
# scope carries `table=<LANDING_NAME>` without touching call sites.
# Thread-correct: set per DoFn bundle / engine entry; pool worker threads
# inherit via the executor initializer (engine-side).
_MILESTONE_TABLE: ContextVar[str] = ContextVar(
    "sdfb_milestone_table", default="")


@contextlib.contextmanager
def milestone_scope(table: str):
  """Tag every milestone logged inside with ``table=`` (explicit
    ``table=`` kwargs win)."""
  token = _MILESTONE_TABLE.set(table)
  try:
    yield
  finally:
    _MILESTONE_TABLE.reset(token)


def milestone_scope_value() -> str:
  """The active scope (for propagating into worker threads)."""
  return _MILESTONE_TABLE.get()


def set_milestone_scope_for_thread(table: str) -> None:
  """Executor-initializer helper: contextvars do NOT propagate into
    ThreadPoolExecutor workers, so pool ladders set the scope per thread
    (ADR 0030)."""
  _MILESTONE_TABLE.set(table)


def log_milestone(name: str, *, level: int = logging.INFO, **fields) -> str:
  scope = _MILESTONE_TABLE.get()
  if scope and "table" not in fields:
    fields["table"] = scope
  line = format_milestone(name, **fields)
  _logger.log(level, line)
  return line


def log_milestone_pretty(name: str,
                         payload: dict,
                         *,
                         level: int = logging.INFO,
                         **fields) -> str:
  """A milestone whose body is human-readable indent-2 JSON.

    The header line keeps the single-line ``SDFB_MILESTONE name=…``
    grep contract; the payload follows as one multi-line JSON block in
    the SAME log record, so Cloud Logging shows it as a single expandable
    entry (operator ask, 2026-08-22: the plan blob was an 8k-char single
    line). Machine consumers keep parsing the compact sibling milestone —
    pretty entries are for eyes, never for tooling."""
  line = format_milestone(name, **fields)
  body = json.dumps(
      payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)
  _logger.log(level, "%s\n%s", line, body)
  return line


def log_milestone_text(name: str,
                       body: str,
                       *,
                       level: int = logging.INFO,
                       **fields) -> str:
  """A milestone whose body is raw multi-line text (e.g. mermaid).

    Same contract as :func:`log_milestone_pretty` — greppable header,
    one Cloud Logging entry — for bodies that are not JSON: the
    `fk_model_pretty` diagram source is pasted straight into a mermaid
    renderer, so it must not be JSON-escaped."""
  line = format_milestone(name, **fields)
  _logger.log(level, "%s\n%s", line, body)
  return line


# Baked into the image at build time (docker/Dockerfile GIT_COMMIT_ARG →
# this env var); "unknown" means a build whose invocation never passed it.
BUILD_COMMIT_ENV = "SDFB_BUILD_COMMIT"


def log_build_info(component: str) -> str:
  """One `build_info` milestone naming the image's git commit.

    The 2026-08-21 four-run cycle could not tell which build each job ran
    (two same-day runs on the "same" label differed only by image content,
    and the interpreter mis-filed a deterministic behavior change as
    run-to-run non-determinism). Stamped from the launcher AND each worker
    so every mined log surface carries it.
    """
  import os

  return log_milestone(
      "build_info",
      commit=os.environ.get(BUILD_COMMIT_ENV, "unknown"),
      component=component,
  )


def parse_milestone(line: str) -> dict | None:
  m = _MILESTONE_RE.search(line)
  if not m:
    return None
  out = {"name": m.group("name")}
  for token in shlex.split(m.group("fields")):
    if "=" in token:
      k, v = token.split("=", 1)
      out[k] = v
  return out
