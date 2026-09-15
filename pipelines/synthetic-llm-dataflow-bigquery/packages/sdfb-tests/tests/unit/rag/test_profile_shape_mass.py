"""Shape-mix mass comes from ROWS, minus head values (2026-08-11 R1).

Both R1 cold baselines measured the distinct-weighted mix inverting
row-mass marginals:

  - A_TABLE COL_054: `AAAAA` (BATCH) carries 81% of source rows but ONE
    distinct value — the digit-heavy minority masks out-weighed it in the
    mix and synthetic digit presence drifted +0.22;
  - B_TABLE COL_024: `99` codes carry 32% of rows / a handful of distinct
    values vs 49k distinct 16-digit refs — synthetic landed 92% `99`-mask;
  - B_TABLE COL_015: rare-but-diverse alpha values out-weighed the 98%
    digit row mass — 48% of synthetic mass was a hallucinated alpha shape.

Head values are excluded from the mix input: `_with_head_values` already
re-emits them at their exact observed share, so leaving them in the mix
double-counts their mass.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

from sdfb_core.contracts import TableSchema
from sdfb_core.engines.b1_rag.profile import profile_columns


def _profiles(values: list[str], name: str = "col"):
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t"
      },
      "schema": [{
          "name": name,
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  return profile_columns(schema, [{name: v} for v in values])[name]


def test_shape_mix_weights_are_row_mass_not_distinct_counts() -> None:
  # 300 rows of one repeated 5-letter code vs 60 distinct digit codes:
  # row mass 5:1 letters, distinct mass 1:60. 60 distinct forces the
  # free-text route.
  values = ["BATCH"] * 300 + [f"{100 + i}A" for i in range(60)]
  prof = _profiles(values)
  assert prof.shape_mix is not None
  weights = {mask_of(shape): w for w, shape in prof.shape_mix}
  # BATCH is a head value (re-emitted at its share) so it must be OUT of
  # the mix; the digit-mask weight must reflect its 60 rows.
  assert "AAAAA" not in weights
  assert weights["999A"] == 60


def test_head_values_are_excluded_from_the_mix_input() -> None:
  # A dominant literal plus a diverse same-mask family: the family's
  # weight must not include the head's rows.
  values = ["ZZ3000"] * 500 + [f"U{i:05d}" for i in range(100)]
  prof = _profiles(values)
  assert prof.head_values and prof.head_values[0][0] == "ZZ3000"
  assert prof.shape_mix is not None
  weights = {mask_of(shape): w for w, shape in prof.shape_mix}
  assert weights.get("A99999", 0) == 600 - 500


def test_shape_mix_keeps_more_than_eight_masks() -> None:
  # B_TABLE COL_019-class: dozens of source shapes; the top-8 cap left
  # 62% of row mass uncovered (shape recall 0.24). The engine mix now
  # carries up to 32 masks.
  values = []
  for k in range(20):
    # 20 structurally distinct masks (varying digit-run length), each
    # with a few rows.
    values += [f"{'X' * (k + 1)}-{i:03d}" for i in range(3)]
  prof = _profiles(values)
  assert prof.shape_mix is not None
  assert len(prof.shape_mix) == 20


def mask_of(shape: tuple[str, ...]) -> str:
  out = []
  for entry in shape:
    if len(entry) == 1:
      ch = entry
      out.append("9" if ch.isdigit() else "A" if ch.isupper() else "a" if ch
                 .islower() else ch)
    elif all(c.isdigit() for c in entry):
      out.append("9")
    elif all(c.isupper() for c in entry):
      out.append("A")
    elif all(c.islower() for c in entry):
      out.append("a")
    else:
      out.append("?")
  return "".join(out)
