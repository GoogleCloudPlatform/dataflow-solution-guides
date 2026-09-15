"""Regression tests for `docker/entrypoint.sh` — the dispatch entrypoint (ADR 0009).

Dataflow runs the image ENTRYPOINT for BOTH roles and *appends* args (it does NOT
override the entrypoint). The script must exec the Beam worker `boot` when it sees
the FnAPI `*_endpoint`/`--id` flags, and the Flex Template launcher otherwise.
Getting this wrong sent FnAPI flags to the launcher binary and crash-looped the
`sdk-0-0` worker (`flag provided but not defined: -logging_endpoint`).

These run the REAL script under `sh`, swapping ONLY the two absolute exec targets
for stubs that print a marker — so the dispatch logic (the for-loop + case
discriminator) is exercised as committed, not a reimplementation of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Ascend from this test file to the repo root (the dir that has docker/entrypoint.sh).
_HERE = Path(__file__).resolve()
_REPO_ROOT = next(
    p for p in _HERE.parents if (p / "docker" / "entrypoint.sh").is_file())
_ENTRYPOINT = _REPO_ROOT / "docker" / "entrypoint.sh"

# The two absolute exec targets in the script. The test rewrites these to stubs.
_BOOT_PATH = "/opt/apache/beam/boot"
_LAUNCHER_PATH = "/opt/google/dataflow/python_template_launcher"


def _run_entrypoint(tmp_path: Path, args: list[str]) -> tuple[str, list[str]]:
  """Run the real entrypoint with stubbed exec targets.

    Returns (role, forwarded_args) where role is "WORKER" or "LAUNCHER" — read
    from the stub that actually got exec'd — and forwarded_args are the args the
    script passed through (proving ``"$@"`` forwarding survives).
    """
  boot = tmp_path / "boot"
  launcher = tmp_path / "launcher"
  # Each stub prints its role on line 1, then echoes the args it received,
  # one per line — so the caller can read back both the dispatch decision and
  # that "$@" forwarding survived.
  for stub, role in ((boot, "WORKER"), (launcher, "LAUNCHER")):
    stub.write_text(f'#!/bin/sh\necho {role}\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)

  script = _ENTRYPOINT.read_text()
  assert _BOOT_PATH in script and _LAUNCHER_PATH in script, (
      "entrypoint.sh no longer references the expected absolute exec targets; "
      "update this test's stub-substitution paths.")
  patched = script.replace(_BOOT_PATH,
                           str(boot)).replace(_LAUNCHER_PATH, str(launcher))
  patched_script = tmp_path / "entrypoint.sh"
  patched_script.write_text(patched)

  proc = subprocess.run(
      ["sh", str(patched_script), *args],
      capture_output=True,
      text=True,
      check=True,
  )
  lines = proc.stdout.splitlines()
  return lines[0], lines[1:]


def test_entrypoint_is_valid_shell():
  """The committed script must parse — guards the unterminated-quote regression."""
  proc = subprocess.run(["sh", "-n", str(_ENTRYPOINT)],
                        capture_output=True,
                        text=True,
                        check=False)
  assert proc.returncode == 0, f"entrypoint.sh failed `sh -n`:\n{proc.stderr}"


def test_worker_flags_dispatch_to_boot(tmp_path):
  """FnAPI boot flags → exec the Beam worker `boot`."""
  role, forwarded = _run_entrypoint(
      tmp_path,
      [
          "--id=1", "--logging_endpoint=localhost:1",
          "--control_endpoint=localhost:2"
      ],
  )
  assert role == "WORKER"
  assert "--logging_endpoint=localhost:1" in forwarded


@pytest.mark.parametrize(
    "flag",
    [
        "--id=w1",
        "--logging_endpoint=host:1",
        "--control_endpoint=host:2",
        "--artifact_endpoint=host:3",
        "--provision_endpoint=host:4",
    ],
)
def test_any_single_fnapi_flag_dispatches_to_boot(tmp_path, flag):
  """Each FnAPI flag ALONE is enough to select the worker path."""
  role, _ = _run_entrypoint(tmp_path, [flag])
  assert role == "WORKER"


def test_no_args_dispatches_to_launcher(tmp_path):
  """A bare launch (no args) → the Flex Template launcher."""
  role, _ = _run_entrypoint(tmp_path, [])
  assert role == "LAUNCHER"


def test_launcher_flags_dispatch_to_launcher(tmp_path):
  """Template-style flags (no FnAPI endpoints) → the launcher, args forwarded."""
  role, forwarded = _run_entrypoint(tmp_path,
                                    ["--template-container-args", "--foo=bar"])
  assert role == "LAUNCHER"
  assert "--foo=bar" in forwarded


def test_endpoint_substring_does_not_false_positive(tmp_path):
  """A flag that merely CONTAINS 'endpoint' but isn't an FnAPI flag → launcher.

    The discriminator matches specific ``--*_endpoint=`` / ``--id=`` prefixes, so a
    lookalike like ``--my_endpoint_url=`` must NOT be mistaken for a worker boot.
    """
  role, _ = _run_entrypoint(tmp_path, ["--my_endpoint_url=host:9"])
  assert role == "LAUNCHER"
