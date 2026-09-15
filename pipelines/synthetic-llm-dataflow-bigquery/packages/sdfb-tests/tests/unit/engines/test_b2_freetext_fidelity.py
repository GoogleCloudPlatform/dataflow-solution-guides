"""B.2 FreeTextHook: empty mask + shape-mix expansion (Task 7)."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unused-argument

import re

import numpy as np
import pytest
from sdfb_core.engines.b2_library.fidelity import ColumnKind, ColumnProfile
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import GenerationConfig
from sdfb_core.engines.text_shapes import build_shape_mix

_OBSERVED = tuple(
    sorted({f"U{(123456 + i * 17041) % 1000000:06d}" for i in range(55)}))


class _PoolClient:
  """Returns a fixed novel pool for the LLM route."""

  def __init__(self):
    self.calls = 0

  def generate_json(self, prompt, json_schema, **kwargs):
    self.calls += 1
    return [{"values": [f"U{900000 + i:06d}" for i in range(32)]}]


def _profile(expansion_pool: tuple = _OBSERVED) -> ColumnProfile:
  return ColumnProfile(
      name="REF_CODE",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=True,
      null_fraction=0.1,
      empty_fraction=0.45,
      text_pool=expansion_pool,
      shape_mix=build_shape_mix(list(expansion_pool)),
  )


def _sample(expansion: str, n: int = 4000, client=None) -> list:
  hook = FreeTextHook(client or _PoolClient())
  cfg = GenerationConfig(
      seed=7, engine_specific={"freetext_expansion": expansion})
  rng = np.random.default_rng(7)
  return hook.sample(_profile(), n, cfg, rng)


def test_empty_and_null_fractions_reproduced():
  values = _sample("off")
  n = len(values)
  assert sum(1 for v in values if v is None) / n == pytest.approx(0.1, abs=0.03)
  assert sum(1 for v in values if v == "") / n == pytest.approx(0.45, abs=0.03)


def test_expansion_breaks_pool_ceiling_and_stays_in_shape():
  client = _PoolClient()
  values = [v for v in _sample("identifiers", client=client) if v]
  distinct = set(values)
  assert len(distinct) > 1200
  assert all(re.fullmatch(r"U\d{6}", v) for v in distinct)
  assert client.calls == 0  # expander route never pays the LLM


def test_expansion_novelty_guard():
  observed = set(_OBSERVED)
  values = [v for v in _sample("identifiers") if v]
  assert sum(1 for v in values if v in observed) / len(values) < 0.02


def test_expansion_off_uses_pool_only():
  on = {v for v in _sample("identifiers") if v}
  off = {v for v in _sample("off") if v}
  assert len(off) < len(on) / 2
