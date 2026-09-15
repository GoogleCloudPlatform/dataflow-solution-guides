"""Env-scoped Mode-A thresholds — loaded from ``config/thresholds.yml``.

Pure-Python: parses the YAML contract into a resolved :class:`Thresholds`
for one environment. The Beam pipeline reads the file at job start and
passes the resolved object into ``PipelineConfig`` (M1 §12).

REF: .claude/skills/validation-mode-a.md · config/thresholds.yml
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_ENV = "dev"


class Thresholds(BaseModel):
  """Resolved gate values for a single environment.

    ``rules`` keeps the raw per-rule catalog for traceability; the only
    value the M1 BLOCKER gate consumes is ``blocker_failure_ratio``.
    """

  env: str
  blocker_failure_ratio: float = Field(ge=0.0, le=1.0)
  rules: dict[str, dict] = Field(default_factory=dict)

  @classmethod
  def from_mapping(cls, data: dict, env: str = DEFAULT_ENV) -> Thresholds:
    raw = (data.get("defaults") or {}).get("blocker_failure_ratio", 0.0)
    ratio = raw.get(env) if isinstance(raw, dict) else raw
    if ratio is None:
      raise ValueError(f"blocker_failure_ratio has no entry for env={env!r}")
    return cls(
        env=env,
        blocker_failure_ratio=float(ratio),
        rules=data.get("rules") or {},
    )

  @classmethod
  def from_yaml(cls, path: str | Path, env: str = DEFAULT_ENV) -> Thresholds:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return cls.from_mapping(data, env)


def load_thresholds(path: str | Path, env: str = DEFAULT_ENV) -> Thresholds:
  """Load + resolve ``thresholds.yml`` for ``env``."""
  return Thresholds.from_yaml(path, env)
