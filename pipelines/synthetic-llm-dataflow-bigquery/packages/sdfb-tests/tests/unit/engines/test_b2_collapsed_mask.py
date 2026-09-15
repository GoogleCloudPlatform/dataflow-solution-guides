"""B.2 parity for the wave-2 shape fixes (design doc §4a/§4c).

The 2026-08-09 R1 evidence is engine-agnostic: B.2's pool ladder has the
same whitespace-normalization hole (no candidate gate at all) and its
identifier branch always used the collapsed template (COL_001-class mask
scrambling). R5 is B.2's acceptance run — these must not regress there.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,protected-access,unused-argument

from __future__ import annotations

import numpy as np
from sdfb_core.engines.b2_library.fidelity import ColumnKind, ColumnProfile
from sdfb_core.engines.b2_library.freetext import FreeTextHook
from sdfb_core.engines.base import GenerationConfig
from sdfb_core.engines.text_shapes import (
    build_shape_mix,
    detect_identifier_shape,
)

_PADDED = tuple(f"CQZWD{i % 10}   CS{i:010d}B" for i in range(60))


def _padded_profile() -> ColumnProfile:
  return ColumnProfile(
      name="REF_CODE",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      text_pool=_PADDED,
      shape_mix=build_shape_mix(list(_PADDED)),
  )


class _SpaceCollapsingClient:

  def generate_json(self, prompt, json_schema, **kw):
    return [{
        "values": [
            f"CQZWD7 CS9{i:09d}B" for i in range(40)  # single space, novel
        ]
    }]


def test_pool_gate_rejects_normalized_whitespace_then_mix_fallback():
  hook = FreeTextHook(_SpaceCollapsingClient(), pool_size=16)
  pool, _ = hook._generate_pool(_padded_profile(), GenerationConfig(seed=3))
  assert pool
  observed = set(_PADDED)
  for v in pool:
    assert "   " in v, v  # triple-space run preserved
    assert v not in observed  # verified novel


def _mask(v: str) -> str:
  return "".join(
      "9" if c.isdigit() else "A" if c.isupper() else "a" if c.islower() else c
      for c in v)


def test_identifier_branch_reproduces_observed_masks():
  import random as _r

  rng = _r.Random(9)
  values = tuple(
      dict.fromkeys("C2E" +
                    "".join(rng.choice("0123456789ABCDEF")
                            for _ in range(21))
                    for _ in range(120)))
  prof = ColumnProfile(
      name="HEX_ID",
      bq_type="STRING",
      kind=ColumnKind.FREE_TEXT,
      nullable=False,
      null_fraction=0.0,
      text_pool=values,
      identifier_shape=detect_identifier_shape(values),
      shape_mix=build_shape_mix(list(values)),
  )
  assert prof.identifier_shape is not None

  class _NeverCalled:

    def generate_json(self, *a, **kw):  # pragma: no cover - guard
      raise AssertionError("identifier route must not call the LLM")

  hook = FreeTextHook(_NeverCalled())
  out = hook.sample(prof, 300, GenerationConfig(seed=11),
                    np.random.default_rng(11))
  drawn = [v for v in out if v]
  assert drawn
  # Wave 4: near-unique-mask columns synthesize tail masks per position
  # (the capped table alone collapsed mask entropy — COL_064/COL_001).
  # Novel masks are correct here; alphabet, prefix and diversity must hold.
  hex_chars = set("0123456789ABCDEF")
  assert all(set(v) <= hex_chars for v in drawn)
  assert all(v.startswith("C2E") for v in drawn)
  assert len({_mask(v) for v in drawn}) > 8
