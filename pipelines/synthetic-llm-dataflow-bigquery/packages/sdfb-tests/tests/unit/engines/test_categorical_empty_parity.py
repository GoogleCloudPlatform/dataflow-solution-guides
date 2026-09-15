"""Categorical sparsity categories are pinned at empirical share (§4 wave-2).

2026-08-09 B_TABLE R1: COL_033 (Δ0.28) and COL_035 (Δ0.24) failed
`freetext.empty_parity` because the categorical similarity blend flattened
the empty/whitespace category toward uniform along with everything else —
at similarity 0.5, a 95%-empty category emits at ~72%. FREE_TEXT columns
pin sparsity at observed rates (`_sparsity_or`); CATEGORICAL must too:
sparsity categories keep their empirical mass, the blend applies only
within the substantive remainder.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import random

import numpy as np
from sdfb_core.engines.b1_rag._fidelity import ColumnSampler
from sdfb_core.engines.b1_rag.profile import ColumnKind, ColumnProfile


def _profile() -> ColumnProfile:
  # 95% single-space (trimmed-empty), 2.5% 'S', 2.5% 'N' — COL_035-class.
  return ColumnProfile(
      name="FLAG",
      bq_type="STRING",
      kind=ColumnKind.CATEGORICAL,
      nullable=False,
      null_fraction=0.0,
      categories={
          " ": 950,
          "S": 25,
          "N": 25
      },
  )


def _empty_share(values: list) -> float:
  return sum(
      1 for v in values if isinstance(v, str) and not v.strip()) / len(values)


def test_numpy_backend_pins_empty_share_at_similarity_half() -> None:
  sampler = ColumnSampler(_profile())
  values = sampler.sample_numpy(np.random.default_rng(7), 20000, 0.5)
  assert abs(_empty_share(values) - 0.95) < 0.02


def test_python_backend_pins_empty_share_at_similarity_half() -> None:
  sampler = ColumnSampler(_profile())
  values = sampler.sample_python(random.Random(7), 20000, 0.5)
  assert abs(_empty_share(values) - 0.95) < 0.02


def test_b2_temperature_reweighting_pins_sparsity_mass() -> None:
  # B.2 parity: temperature reweighting must not shift the empty
  # category's mass either (same evidence, same rule).
  from sdfb_core.engines.b2_library.backends import _sample_categorical
  from sdfb_core.engines.b2_library.fidelity import (
      ColumnKind as B2Kind,)
  from sdfb_core.engines.b2_library.fidelity import (
      ColumnProfile as B2Profile,)

  prof = B2Profile(
      name="FLAG",
      bq_type="STRING",
      kind=B2Kind.CATEGORICAL,
      nullable=False,
      null_fraction=0.0,
      categories=(" ", "S", "N"),
      weights=(950.0, 25.0, 25.0),
  )
  values = _sample_categorical(
      prof, 20000, np.random.default_rng(7), temperature=2.0)
  assert abs(_empty_share(values) - 0.95) < 0.02


def test_substantive_categories_follow_empirical_frequencies() -> None:
  # 2026-08-11 R1 (both tables): the substantive similarity blend
  # flattened every skewed enum toward uniform at the default 0.5 —
  # ~25 categorical columns with entropy gaps -0.2..-0.99 (A_TABLE
  # COL_004-class: source ~all EUR, synthetic near-uniform over 37
  # currencies). Categorical marginals now follow the empirical
  # frequency table at ANY similarity — ADR 0013's original contract;
  # `similarity` stays an LLM/retrieval dial, not a fidelity dial.
  prof = ColumnProfile(
      name="FLAG",
      bq_type="STRING",
      kind=ColumnKind.CATEGORICAL,
      nullable=False,
      null_fraction=0.0,
      categories={
          " ": 800,
          "S": 190,
          "N": 10
      },
  )
  for similarity in (0.0, 0.5, 1.0):
    sampler = ColumnSampler(prof)
    values = sampler.sample_numpy(np.random.default_rng(7), 20000, similarity)
    s = sum(1 for v in values if v == "S")
    n = sum(1 for v in values if v == "N")
    assert abs(_empty_share(values) - 0.8) < 0.02  # sparsity pinned
    assert abs(s / 20000 - 0.19) < 0.02
    assert n / max(s, 1) < 0.12, "19:1 skew must survive sampling"
