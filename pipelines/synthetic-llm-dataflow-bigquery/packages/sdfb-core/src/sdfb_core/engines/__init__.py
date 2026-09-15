"""Synthetic-data generation engines.

The `GenerationEngine` ABC is the single seam between the Beam pipeline
and the synthesis logic. Concrete implementations live in their own
worktrees and are merged into `main` via PRs:

  - B.1 RAG engine          → `engines/b1_rag/`        (worktrees/b1-rag)
  - B.2 library-wrapper     → `engines/b2_library/`    (worktrees/b2-library)

Both must pass the contract tests in
`packages/sdfb-tests/tests/unit/engines/test_abc_contract.py`.
"""

from sdfb_core.engines.base import (
    FreeTextEmptyYieldError,
    GenerationConfig,
    GenerationContext,
    GenerationEngine,
    ModelClient,
    ModelClientTransientError,
)

# ---------------------------------------------------------------------------
# Engine registry — name → class.
#
# The Beam DAG looks up engines by string name from the CLI flag rather
# than via direct class references; that lets DoFns survive pickling
# across the package boundary without needing every package to import
# every engine implementation. Each engine's module calls
# `register_engine()` at module load time:
#
#     # in packages/sdfb-core/src/sdfb_core/engines/b2_library/__init__.py
#     from sdfb_core.engines import register_engine
#     register_engine("b2_library", B2LibraryEngine)
#
# The Beam DoFn imports `sdfb_core.engines.b2_library` in its `setup()`
# (or relies on the pipeline driver to have imported it) — which fires
# the registration as a side effect.
# ---------------------------------------------------------------------------

ENGINE_REGISTRY: dict[str, type[GenerationEngine]] = {}


def register_engine(name: str, engine_class: type[GenerationEngine]) -> None:
  """Register an engine class under a string name."""
  ENGINE_REGISTRY[name] = engine_class


def get_engine(name: str) -> type[GenerationEngine]:
  """Look up an engine class by name. Raises if not registered."""
  if name not in ENGINE_REGISTRY:
    available = sorted(ENGINE_REGISTRY)
    raise ValueError(f"Unknown engine {name!r}. Available: {available}. "
                     f"Ensure the engine's module has been imported (which "
                     f"triggers `register_engine` at module load time).")
  return ENGINE_REGISTRY[name]


__all__ = [
    "ENGINE_REGISTRY",
    "FreeTextEmptyYieldError",
    "GenerationConfig",
    "GenerationContext",
    "GenerationEngine",
    "ModelClient",
    "ModelClientTransientError",
    "get_engine",
    "register_engine",
]

# ---------------------------------------------------------------------------
# Auto-register the bundled engines. Importing each subpackage fires its
# `register_engine(...)`, so `get_engine("b1_rag" | "b2_library")` resolves
# wherever `sdfb_core.engines` is imported (pipeline driver, CLI, tests) — not
# only when a test imports the subpackage directly. Imports live at the BOTTOM
# (after `register_engine` is defined) to avoid a circular import; the engines'
# heavy deps (faiss / sdgx) stay deferred inside them.
# ---------------------------------------------------------------------------
from sdfb_core.engines import b1_rag as _b1_rag  # noqa: E402, F401  # pylint: disable=wrong-import-position
from sdfb_core.engines import b2_library as _b2_library  # noqa: E402, F401  # pylint: disable=wrong-import-position
