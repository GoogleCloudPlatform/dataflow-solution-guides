"""Contract tests for the SDFB_MILESTONE worker-log line format."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel
import logging

import pytest
from sdfb_core.observability import (
    MILESTONE_PREFIX,
    format_milestone,
    log_milestone,
    parse_milestone,
)


def test_format_is_single_stable_line():
  line = format_milestone("embedder_pull_done", seconds=1.6, files=5)
  assert line == "SDFB_MILESTONE name=embedder_pull_done files=5 seconds=1.6"
  assert "\n" not in line


def test_fields_are_sorted_for_determinism():
  a = format_milestone("x", b=2, a=1)
  b = format_milestone("x", a=1, b=2)
  assert a == b == "SDFB_MILESTONE name=x a=1 b=2"


def test_values_with_spaces_are_quoted():
  line = format_milestone(
      "freetext_llm_fallback", error="RuntimeError: boom now")
  assert line == "SDFB_MILESTONE name=freetext_llm_fallback error='RuntimeError: boom now'"


def test_parse_roundtrip():
  line = format_milestone("vllm_ready", seconds=337.2, model="gemma4")
  parsed = parse_milestone(line)
  assert parsed == {"name": "vllm_ready", "model": "gemma4", "seconds": "337.2"}


def test_parse_rejects_non_milestone_lines():
  assert parse_milestone("INFO something else") is None


def test_log_milestone_emits_via_std_logging(caplog):
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    line = log_milestone("dofn_setup_start", engine="b1_rag")
  assert line.startswith(MILESTONE_PREFIX)
  assert any(line in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "name",
    ["Bad-Name", "has space", "UPPERCASE", "trailing-dash-", "dotted.name", ""],
)
def test_format_milestone_rejects_non_conforming_names(name):
  """`name` is the parser's own anchor (`_MILESTONE_RE`'s
    `[a-z0-9_]+`) — every in-repo emitter already only ever passes
    lowercase_underscore names, so this is pure guard-rail: a non-conforming
    name would silently produce a log line the probe's miner can't parse
    back out (`parse_milestone` re-derives `name` via the same pattern)."""
  with pytest.raises(ValueError):
    format_milestone(name)


def test_format_milestone_accepts_conforming_name():
  assert format_milestone(
      "dofn_setup_start") == "SDFB_MILESTONE name=dofn_setup_start"


def test_warning_level_supported(caplog):
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    log_milestone("freetext_llm_fallback", level=logging.WARNING, column="c1")
  assert caplog.records[0].levelno == logging.WARNING


def test_build_info_stamps_the_baked_commit(caplog, monkeypatch):
  # 2026-08-21 four-run cycle: two same-day runs could not be told apart
  # by build — the commit is baked into the image env and stamped from
  # launcher + workers so the probe mines it from any log surface.
  from sdfb_core.observability import BUILD_COMMIT_ENV, log_build_info

  monkeypatch.setenv(BUILD_COMMIT_ENV, "abc1234")
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    line = log_build_info("launcher")
  assert "name=build_info" in line
  assert "commit=abc1234" in line
  assert "component=launcher" in line
  monkeypatch.delenv(BUILD_COMMIT_ENV)
  assert "commit=unknown" in log_build_info("worker")
