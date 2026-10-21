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
"""Static tests for the one image: `docker/Dockerfile` also carries the
evaluator (ADR 0041, amendment of 2026-10-06).

Nothing is built here. The Dockerfile is read as text and its claims are
checked one by one:

    claim                                  why it matters
    ─────────────────────────────────────  ─────────────────────────────────
    the evaluator's source arrives         the same file must build in a
    through a stage that copies            tree that has no
    `packages/` and tests for the          packages/sdfb-evaluation (the
    directory in shell                     image then runs generation only)
    no BuildKit-only syntax                any builder builds it
    the entry is docker/flex_entry.py      one template, two jobs
    the evaluator's src is on PYTHONPATH   launcher and worker both import
    and in the bridge .pth                 it; the generator's entries keep
                                           their order, ahead of it
    both worker-image variables read       no new build argument
    the one build argument
    a build-time import of the             an image that lost a dependency
    evaluator's two entry modules          the evaluator needs fails at
                                           BUILD, not at launch

The evaluator has no environment of its own in the image: it runs on what
`uv sync` installed for the generator. That is true only while the root
lock holds every dependency the evaluator declares, at a version inside its
range, and while the extras the image installs still reach each of them
(scipy and scikit-learn arrive through an extra, not through a requirement
of the generator). The last tests here turn that into a checked fact; they
skip in a tree without the evaluator.

Design: docs/DESIGN.md §11 Evaluation (sdfb-evaluation)
(ADR 0041).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents
    if (p / "docker" / "entrypoint.sh").is_file())
_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"
_EVALUATOR = _REPO_ROOT / "packages" / "sdfb-evaluation"
_EVALUATOR_SRC = "/workspace/packages/sdfb-evaluation/src"
_VENV_SITE = "/workspace/.venv/lib/python3.14/site-packages"
_ENTRY_MODULES = ("sdfb_evaluation.cli.run_evaluation",
                  "sdfb_evaluation.beam.pipeline")

# The image is linux/amd64 on the Python the lock is resolved for.
_IMAGE_ENVIRONMENT = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "python_full_version": "3.14.0",
}


# --------------------------------------------------------------------------
# reading the Dockerfile
# --------------------------------------------------------------------------
def _instructions(text: str) -> list[str]:
  """The Dockerfile's instructions in order: comment lines dropped,
    continuation lines joined, inner whitespace collapsed."""
  joined: list[str] = []
  pending = ""
  for line in text.splitlines():
    if line.lstrip().startswith("#") or not line.strip():
      continue
    pending += line.rstrip()
    if pending.endswith("\\"):
      pending = pending[:-1] + " "
      continue
    joined.append(" ".join(pending.split()))
    pending = ""
  assert not pending, "the Dockerfile ends inside a continued instruction"
  return joined


def _stages(instructions: list[str]) -> list[tuple[str, list[str]]]:
  """(the stage's name or "", its instructions) per `FROM`, in order."""
  stages: list[tuple[str, list[str]]] = []
  for instruction in instructions:
    if instruction.startswith("FROM "):
      named = re.search(r" AS (\S+)$", instruction)
      stages.append((named.group(1) if named else "", []))
    elif stages:
      stages[-1][1].append(instruction)
  return stages


def _dockerfile() -> list[str]:
  return _instructions(_DOCKERFILE.read_text(encoding="utf-8"))


def _final() -> list[str]:
  name, body = _stages(_dockerfile())[-1]
  assert name == "", "the image is the last, unnamed stage"
  return body


def _env(name: str) -> str:
  values = [
      match.group(1)
      for instruction in _final()
      for match in re.finditer(rf'(?:^ENV | ){name}="?([^"\s]+)"?', instruction)
      if instruction.startswith("ENV ")
  ]
  assert len(values) == 1, f"ENV {name} is set {len(values)} times"
  return values[0]


def _index(instructions: list[str], pattern: str) -> int:
  (found,) = [
      i for i, text in enumerate(instructions) if re.search(pattern, text)
  ] or [None]
  assert found is not None, f"no instruction matches {pattern!r}"
  return found


# --------------------------------------------------------------------------
# the evaluator's source, in a tree that may not have it
# --------------------------------------------------------------------------
def test_the_evaluator_source_comes_from_a_stage_that_tolerates_its_absence():
  stages = dict(_stages(_dockerfile()))
  assert "evaluator_source" in stages
  stage = stages["evaluator_source"]
  # `packages/` exists in every tree; the evaluator's directory may not.
  (copied,) = [i for i in stage if i.startswith("COPY ")]
  assert re.fullmatch(r"COPY packages (/\S+)", copied)
  (run,) = [i for i in stage if i.startswith("RUN ")]
  tested = re.search(r"if \[ -d (\S+/sdfb-evaluation/src) \]; then cp -R", run)
  assert tested, run
  # the output directory exists whether or not the test passes
  made = re.search(r"mkdir -p (/\S+)", run)
  assert made and run.index("mkdir -p") < run.index("if [ -d")
  final = _final()
  taken = final[_index(final, r"^COPY --from=evaluator_source ")]
  assert taken == (f"COPY --from=evaluator_source {made.group(1)}/ "
                   "packages/sdfb-evaluation/")
  assert "WORKDIR /workspace" in final


def _stage_run(tmp_path: Path) -> subprocess.CompletedProcess:
  """The throwaway stage's RUN, as committed, on a tree under `tmp_path`
    (`/stage` and `/out` moved there)."""
  (run,) = [
      i for i in dict(_stages(_dockerfile()))["evaluator_source"]
      if i.startswith("RUN ")
  ]
  command = run[len("RUN "):].replace("/stage/", f"{tmp_path}/stage/").replace(
      "/out", f"{tmp_path}/out")
  return subprocess.run(["sh", "-c", command],
                        capture_output=True,
                        text=True,
                        check=False)


def test_the_stage_copies_the_source_when_the_tree_has_it(tmp_path):
  package = tmp_path / "stage" / "packages" / "sdfb-evaluation"
  (package / "src" / "sdfb_evaluation").mkdir(parents=True)
  (package / "src" / "sdfb_evaluation" / "__init__.py").write_text("")
  (package / "tests").mkdir()
  (package / "uv.lock").write_text("")
  done = _stage_run(tmp_path)
  assert done.returncode == 0, done.stderr
  out = tmp_path / "out"
  # the source and nothing else: no lock, no tests
  assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == [
      "src", "src/sdfb_evaluation", "src/sdfb_evaluation/__init__.py"
  ]


def test_the_stage_leaves_an_empty_directory_when_the_tree_has_none(tmp_path):
  (tmp_path / "stage" / "packages" / "sdfb-core").mkdir(parents=True)
  done = _stage_run(tmp_path)
  assert done.returncode == 0, done.stderr
  assert (tmp_path / "out").is_dir() and not list((tmp_path / "out").iterdir())


def test_no_instruction_names_the_evaluator_directory_in_the_build_context():
  """A `COPY packages/sdfb-evaluation/…` fails the build where the directory
    is absent; only the stage's shell test may look for it."""
  for instruction in _dockerfile():
    if instruction.startswith(
        ("COPY ", "ADD ")) and "--from=" not in instruction:
      sources = instruction.split()[1:-1]
      assert not [s for s in sources if "sdfb-evaluation" in s], instruction


def test_the_stage_starts_from_an_image_the_build_already_pulls():
  froms = [i for i in _dockerfile() if i.startswith("FROM ")]
  assert froms == [
      "FROM ${LAUNCHER_IMAGE} AS flex_launcher",
      "FROM ${BEAM_SDK_IMAGE} AS evaluator_source",
      "FROM ${BEAM_SDK_IMAGE}",
  ]


def test_sources_stay_after_the_dependency_layer():
  final = _final()
  sync = _index(final, r"^RUN uv sync ")
  assert sync < _index(final, r"^COPY packages/sdfb-core/src ")
  assert sync < _index(final, r"^COPY packages/sdfb-beam/src ")
  assert sync < _index(final, r"^COPY --from=evaluator_source ")


def test_any_builder_can_build_it():
  text = _DOCKERFILE.read_text(encoding="utf-8")
  assert not re.search(r"^#\s*syntax\s*=", text, re.M)
  for instruction in _dockerfile():
    assert "--mount" not in instruction, instruction
    assert "--link" not in instruction, instruction
    assert "<<" not in instruction, instruction  # here-documents


def test_the_evaluator_has_no_environment_of_its_own():
  final = _final()
  (sync,) = [i for i in final if "uv sync" in i]
  assert "--project" not in sync and "sdfb-evaluation" not in sync
  assert not [i for i in final if "uv venv" in i or "uv pip" in i]
  assert not [i for i in final if "sdfb-evaluation/uv.lock" in i]


# --------------------------------------------------------------------------
# one entry
# --------------------------------------------------------------------------
def test_the_flex_entry_is_the_dispatcher_and_is_copied_where_it_is_named():
  entry = _env("FLEX_TEMPLATE_PYTHON_PY_FILE")
  assert Path(entry).name == "flex_entry.py"
  assert f"COPY docker/flex_entry.py {entry}" in _final()
  assert (_REPO_ROOT / "docker" / "flex_entry.py").is_file()
  assert "run_pipeline.py" not in " ".join(
      i for i in _final() if "FLEX_TEMPLATE_PYTHON_PY_FILE" in i)


def test_the_entrypoint_and_the_launcher_contract_are_untouched():
  final = _final()
  assert final[-1] == 'ENTRYPOINT ["/opt/sdfb/entrypoint.sh"]'
  assert "COPY docker/entrypoint.sh /opt/sdfb/entrypoint.sh" in final
  assert not [i for i in final if "FLEX_TEMPLATE_PYTHON_REQUIREMENTS_FILE" in i]


# --------------------------------------------------------------------------
# both execution contexts import the evaluator
# --------------------------------------------------------------------------
def test_pythonpath_keeps_the_generators_entries_first():
  assert _env("PYTHONPATH").split(":") == [
      _VENV_SITE,
      "/workspace/packages/sdfb-core/src",
      "/workspace/packages/sdfb-beam/src",
      _EVALUATOR_SRC,
  ]


def _bridge() -> str:
  """The text the Dockerfile's `printf` writes into the bridge .pth."""
  final = _final()
  run = final[_index(final, r"zz_sdfb_bridge\.pth")]
  written = re.search(
      r'printf "((?:[^"\\]|\\.)*)" > "\$\{SITE\}/zz_sdfb_bridge', run)
  assert written, run
  return written.group(1).replace("\\n", "\n")


def test_the_bridge_adds_the_venv_then_the_evaluator_source():
  assert _bridge().splitlines() == [
      f"import site; site.addsitedir('{_VENV_SITE}')",
      _EVALUATOR_SRC,
  ]
  assert _bridge().endswith("\n")


# Runtime noise on a child interpreter's stderr that is not ours: gRPC prints
# these when the process forks while its threads are alive (`fork_posix.cc`,
# `ev_poll_posix.cc`; seen in this repository's full-suite logs). A bad `.pth`
# line is a Python traceback or message and carries neither.
_STDERR_NOISE = ("fork_posix.cc", "ev_poll_posix.cc")


def _own_stderr(text: str) -> str:
  """`text` without the lines of known runtime noise (`_STDERR_NOISE`)."""
  return "\n".join(line for line in text.splitlines()
                   if not any(noise in line for noise in _STDERR_NOISE))


def test_known_grpc_fork_noise_is_dropped_and_anything_else_is_kept():
  noise = ("I1006 21:11:31.586576 2233243 ev_poll_posix.cc:593] FD from fork "
           "parent still in poll list: fd(27, generation: 1)\n"
           "E1006 21:11:31.587 1 fork_posix.cc:70] Other threads are "
           "currently calling into gRPC, skipping fork() handlers\n")
  assert not _own_stderr(noise)
  assert not _own_stderr("")
  assert _own_stderr(noise + "Error processing line 1 of x.pth\n") == (
      "Error processing line 1 of x.pth")
  assert _own_stderr("Traceback (most recent call last):\n") != ""


def _path_after_bridge(site_dir: Path, bridge: str) -> list[str]:
  (site_dir / "zz_sdfb_bridge.pth").write_text(bridge, encoding="utf-8")
  script = ("import json, site, sys; "
            f"site.addsitedir({str(site_dir)!r}); print(json.dumps(sys.path))")
  process = subprocess.run([sys.executable, "-I", "-c", script],
                           capture_output=True,
                           text=True,
                           check=True)
  # a bad .pth line prints there
  assert not _own_stderr(process.stderr), process.stderr
  return json.loads(process.stdout)


def test_the_bridge_file_is_harmless_without_the_evaluator(tmp_path):
  """`site` adds a path line only when the path exists: in an image built
    from a tree without the evaluator the line does nothing."""
  site_dir = tmp_path / "site"
  site_dir.mkdir()
  path = _path_after_bridge(site_dir, _bridge())
  assert _EVALUATOR_SRC not in path


def test_the_bridge_file_adds_the_evaluator_source_when_it_exists(tmp_path):
  site_dir = tmp_path / "site"
  site_dir.mkdir()
  venv, source = tmp_path / "venv-site", tmp_path / "evaluator" / "src"
  venv.mkdir()
  source.mkdir(parents=True)
  bridge = _bridge().replace(_VENV_SITE,
                             str(venv)).replace(_EVALUATOR_SRC, str(source))
  path = _path_after_bridge(site_dir, bridge)
  assert str(venv) in path and str(source) in path
  assert path.index(str(venv)) < path.index(str(source))


# --------------------------------------------------------------------------
# the evaluator brings no build argument
# --------------------------------------------------------------------------
def test_both_worker_image_variables_read_the_one_build_argument():
  """A deployment's Dockerfile may declare arguments of its own (a registry
    mirror, the build's environment); none of them is the evaluator's. Its
    worker-image variable reads the argument the generator's already reads,
    so a build passes nothing new."""
  arguments = {
      re.match(r"ARG (\w+)", i).group(1)
      for i in _dockerfile()
      if i.startswith("ARG ")
  }
  assert "SDFB_SDK_CONTAINER_IMAGE_ARG" in arguments
  assert not [a for a in arguments if "EVAL" in a.upper()]
  assert _env("SDFB_SDK_CONTAINER_IMAGE") == "$SDFB_SDK_CONTAINER_IMAGE_ARG"
  assert _env(
      "SDFB_EVAL_SDK_CONTAINER_IMAGE") == "$SDFB_SDK_CONTAINER_IMAGE_ARG"
  final = _final()
  # declared late: a per-commit value never invalidates the `uv sync` layer
  assert _index(final, r"^RUN uv sync ") < _index(
      final, r"^ARG SDFB_SDK_CONTAINER_IMAGE_ARG")


# --------------------------------------------------------------------------
# the build fails, not the launch
# --------------------------------------------------------------------------
def test_the_build_imports_the_evaluators_entry_modules_in_both_contexts():
  final = _final()
  check = final[_index(final, r"python -c .*sdfb_evaluation")]
  assert check.startswith("RUN if [ -d ")
  assert re.search(
      r"if \[ -d \S*packages/sdfb-evaluation/src/sdfb_evaluation \]", check)
  for module in _ENTRY_MODULES:
    assert module in check, module
  # the launcher's context (ENV PYTHONPATH) and the worker's (the bridge
  # alone: Beam's worker venv drops PYTHONPATH), then the image's own venv
  # interpreter, which no base-image package can satisfy
  assert len(re.findall(r"python -c ", check)) == 3
  assert "PYTHONPATH= python -c " in check
  assert "PYTHONPATH= /workspace/.venv/bin/python -c " in check
  for package in _SCIENCE:
    assert package in check, package
  # each context prints where it found them
  assert "__version__" in check and "__file__" in check
  assert " else " in check and check.rstrip().endswith("fi")
  # after everything it depends on is in place
  position = final.index(check)
  assert position > _index(final, r"^COPY --from=evaluator_source ")
  assert position > _index(final, r"^ENV PYTHONPATH=")
  assert position > _index(final, r"zz_sdfb_bridge\.pth")


_SCIENCE = ("numpy", "scipy", "sklearn")

_PYTHON_STUB = """#!/bin/sh
# Stands in for an interpreter of the image (the global `python`, or the
# venv's): records which one and how it was called, then succeeds or fails
# as the test asked for this call.
calls="$STUB_DIR/calls"
count=$(( $(wc -l < "$calls") + 1 ))
echo "$0 PYTHONPATH=${PYTHONPATH-<unset>} $*" >> "$calls"
[ "$count" != "$(cat "$STUB_DIR/fail_at")" ]
"""


def _check_run(tmp_path: Path,
               *,
               evaluator: bool,
               fail_at: int = 0) -> tuple[subprocess.CompletedProcess, list]:
  """The build-time check's RUN, as committed, with the evaluator's
    directory under `tmp_path` and `python` replaced by a stub that fails
    on its `fail_at`-th call (0: never)."""
  final = _final()
  run = final[_index(final, r"python -c .*sdfb_evaluation")]
  package = tmp_path / "workspace" / "packages" / "sdfb-evaluation" / "src"
  if evaluator:
    (package / "sdfb_evaluation").mkdir(parents=True)
  command = run[len("RUN "):].replace("/workspace/", f"{tmp_path}/workspace/")
  stubs = tmp_path / "bin"
  stubs.mkdir()
  (stubs / "calls").write_text("")
  (stubs / "fail_at").write_text(str(fail_at))
  venv = tmp_path / "workspace" / ".venv" / "bin"
  venv.mkdir(parents=True)
  for stub in (stubs / "python", venv / "python"):
    stub.write_text(_PYTHON_STUB)
    stub.chmod(0o755)
  done = subprocess.run(
      ["sh", "-c", command],
      capture_output=True,
      text=True,
      check=False,
      env={
          "PATH": f"{stubs}:/usr/bin:/bin",
          "PYTHONPATH": "/the/image/pythonpath",
          "STUB_DIR": str(stubs)
      })
  return done, (stubs / "calls").read_text().splitlines()


def test_the_check_imports_once_per_context_when_the_evaluator_is_there(
    tmp_path):
  done, calls = _check_run(tmp_path, evaluator=True)
  assert done.returncode == 0, done.stderr
  imports = "import " + ", ".join(_ENTRY_MODULES)
  stubs, venv = f"{tmp_path}/bin/python", f"{tmp_path}/workspace/.venv/bin/python"
  assert [c.split(" -c ")[0] for c in calls] == [
      f"{stubs} PYTHONPATH=/the/image/pythonpath",  # the launcher's
      f"{stubs} PYTHONPATH=",  # the worker's: the bridge alone
      f"{venv} PYTHONPATH=",  # the image's own interpreter
  ]
  launcher, worker, own = (c.split(" -c ", 1)[1] for c in calls)
  assert launcher.startswith(imports) and worker.startswith(imports)
  assert not own.startswith(imports)  # the entry modules are not on its path
  for call in (launcher, worker, own):
    for package in _SCIENCE:  # each context imports them ...
      assert package in call
  for call in (launcher, worker):  # ... and says which version and file
    assert "__version__" in call and "__file__" in call


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_a_failed_import_in_any_context_fails_the_build(tmp_path, fail_at):
  done, calls = _check_run(tmp_path, evaluator=True, fail_at=fail_at)
  assert done.returncode != 0
  assert len(calls) == fail_at
  assert "importable in both contexts" not in done.stdout


def test_the_check_does_nothing_without_the_evaluator(tmp_path):
  done, calls = _check_run(tmp_path, evaluator=False, fail_at=1)
  assert done.returncode == 0, done.stderr
  assert not calls
  assert "generation only" in done.stdout


# --------------------------------------------------------------------------
# one environment: the root lock serves the evaluator
# --------------------------------------------------------------------------
Node = tuple[str, str]  # (canonical package name, extra or "")


def _edges(dependencies: Iterable[Mapping],
           environment: Mapping[str, str]) -> list[Node]:
  """The lock dependencies that apply in `environment`, each as the package
    itself plus one node per extra it is asked with."""
  nodes: list[Node] = []
  for dependency in dependencies:
    marker = dependency.get("marker")
    if marker and not Marker(marker).evaluate({**environment, "extra": ""}):
      continue
    name = canonicalize_name(dependency["name"])
    nodes.append((name, ""))
    nodes.extend((name, extra) for extra in dependency.get("extra", ()))
  return nodes


def installed_by_sync(lock: Mapping, extras: Iterable[str],
                      environment: Mapping[str, str]) -> set[Node]:
  """What `uv sync --no-dev --all-packages --extra E…` installs from `lock`
    in `environment`: every package reachable from the workspace members
    through requirements and through the named extras (dev groups are not
    followed)."""
  packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
  members = [canonicalize_name(m) for m in lock["manifest"]["members"]]
  reached: set[Node] = set()
  queue: list[Node] = [(m, "") for m in members]
  queue += [(m, extra) for m in members for extra in sorted(set(extras))]
  while queue:
    node = queue.pop()
    if node in reached or node[0] not in packages:
      continue
    reached.add(node)
    package, extra = packages[node[0]], node[1]
    dependencies = (
        package.get("optional-dependencies", {}).get(extra, [])
        if extra else package.get("dependencies", []))
    queue.extend(_edges(dependencies, environment))
  return reached


def _sync_extras() -> list[str]:
  (sync,) = [i for i in _final() if i.startswith("RUN uv sync ")]
  assert "--frozen" in sync and "--no-dev" in sync and "--all-packages" in sync
  return re.findall(r"--extra (\S+)", sync)


def _synthetic_lock() -> dict:
  return {
      "manifest": {
          "members": ["app"]
      },
      "package": [
          {
              "name": "app",
              "dependencies": [{
                  "name": "base",
                  "extra": ["net"]
              }, {
                  "name": "mac-only",
                  "marker": "sys_platform == 'darwin'"
              }],
              "optional-dependencies": {
                  "science": [{
                      "name": "wrapper"
                  }]
              },
              "dev-dependencies": {
                  "dev": [{
                      "name": "linter"
                  }]
              },
          },
          {
              "name": "base",
              "optional-dependencies": {
                  "net": [{
                      "name": "http"
                  }],
                  "unused": [{
                      "name": "never"
                  }]
              }
          },
          {
              "name": "wrapper",
              "dependencies": [{
                  "name": "Sci_Py"
              }]
          },
          {
              "name": "sci-py"
          },
          {
              "name": "http"
          },
          {
              "name": "never"
          },
          {
              "name": "mac-only"
          },
          {
              "name": "linter"
          },
      ],
  }


def test_reachability_follows_requirements_extras_and_markers():
  lock = _synthetic_lock()
  without = installed_by_sync(lock, [], _IMAGE_ENVIRONMENT)
  assert {n for n, _ in without} == {"app", "base", "http"}
  assert ("base", "net") in without
  # the extra is what brings the wrapper and, through it, its dependency
  science = installed_by_sync(lock, ["science"], _IMAGE_ENVIRONMENT)
  assert {n for n, _ in science} == {"app", "base", "http", "wrapper", "sci-py"}
  # a marker that is false on the image, an extra nobody asks for and a
  # dev group are never installed
  assert not {"mac-only", "never", "linter"} & {n for n, _ in science}
  darwin = installed_by_sync(lock, [], {
      **_IMAGE_ENVIRONMENT, "sys_platform": "darwin"
  })
  assert ("mac-only", "") in darwin


def _evaluator_requirements() -> list[Requirement]:
  if not (_EVALUATOR / "pyproject.toml").is_file():
    pytest.skip("packages/sdfb-evaluation is not part of this tree")
  project = tomllib.loads(
      (_EVALUATOR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
  requirements = [Requirement(text) for text in project["dependencies"]]
  assert requirements, "the evaluator declares no dependency: wrong file?"
  return requirements


def _root_lock() -> dict:
  return tomllib.loads((_REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))


def test_the_root_lock_holds_every_evaluator_dependency_inside_its_range():
  requirements = _evaluator_requirements()
  locked: dict[str, list[str]] = {}
  for package in _root_lock()["package"]:
    locked.setdefault(canonicalize_name(package["name"]),
                      []).append(package.get("version", ""))
  for requirement in requirements:
    versions = locked.get(canonicalize_name(requirement.name), [])
    assert len(versions) == 1, (
        f"{requirement.name}: the root uv.lock holds {len(versions)} "
        "versions of it; the shared image's environment (the generator's) "
        "must hold exactly one")
    assert requirement.specifier.contains(Version(versions[0])), (
        f"{requirement.name}: the root uv.lock pins {versions[0]}, outside "
        f"the evaluator's range {requirement.specifier}. The shared image "
        "runs the evaluator on the root lock's versions")


def test_both_projects_run_on_the_same_python():
  if not (_EVALUATOR / "pyproject.toml").is_file():
    pytest.skip("packages/sdfb-evaluation is not part of this tree")
  minor = (_REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
  assert (_EVALUATOR /
          ".python-version").read_text(encoding="utf-8").strip() == minor
  assert f"python{minor}/site-packages" in _env("PYTHONPATH")


def test_the_images_extras_install_every_evaluator_dependency():
  requirements = _evaluator_requirements()
  extras = _sync_extras()
  installed = installed_by_sync(_root_lock(), extras, _IMAGE_ENVIRONMENT)
  flags = " ".join(f"--extra {extra}" for extra in extras)
  for requirement in requirements:
    name = canonicalize_name(requirement.name)
    for extra in ("", *sorted(requirement.extras)):
      shown = f"{requirement.name}[{extra}]" if extra else requirement.name
      assert (name, extra) in installed, (
          f"{shown} is a dependency of packages/sdfb-evaluation and is no "
          "longer installed by the image's extras (docker/Dockerfile: "
          f"`uv sync … {flags}`): the evaluator would fail to import in the "
          "shared image")


def test_the_guard_sees_an_extra_that_stops_reaching_a_dependency():
  """The check above is not vacuous: with no extra at all, at least one of
    the evaluator's dependencies is not installed."""
  requirements = _evaluator_requirements()
  bare = {n for n, _ in installed_by_sync(_root_lock(), [], _IMAGE_ENVIRONMENT)}
  missing = [
      r.name for r in requirements if canonicalize_name(r.name) not in bare
  ]
  assert missing, (
      "every evaluator dependency is now a requirement of the generator "
      "itself: the image's extras no longer matter for it, and this test "
      "and the sentence about extras in the module docstring can go")
