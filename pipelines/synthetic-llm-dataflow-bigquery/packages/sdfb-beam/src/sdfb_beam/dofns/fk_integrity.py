"""Referential-integrity gate — `fk.orphan`, line 4 of defense (ADR 0031).

The engine draws whole parent key tuples, so an orphan should be
impossible. This DoFn is the INDEPENDENT check that the run actually
achieved it: it holds the same parent key set the child sampled from and
diverts any row whose FK tuple is not in it.

Why it exists at all, given the generator is correct by construction:
the 2026-08-23 run landed 817,627 orphan rows and still reported
``status=PASSED``, because no rule anywhere scored referential
integrity. A rule that can only fire on a generator regression is
exactly what catches the next one — and it makes "0 orphans" a MEASURED
per-run fact in `validation_runs` instead of an argument about the code.

NULL FK tuples pass: SQL MATCH SIMPLE treats a NULL reference as "no
parent", not as a broken one.
"""

# f-string fields keep single quotes while Python 3.11 is supported;
# pylint on Python >= 3.12 reads those quotes as inconsistent.
# pylint: disable=inconsistent-quotes

from __future__ import annotations

import apache_beam as beam
from apache_beam.metrics import Metrics


class EnforceFkIntegrityDoFn(beam.DoFn):
  """Divert rows whose FK tuple is not a landed parent key."""

  def __init__(self, fk_key_pools: list[dict] | None = None) -> None:
    super().__init__()
    # Driver-side pools (parent landed in an earlier job). Same-job
    # parents arrive per-bundle through the `fk_side` side input.
    self.fk_key_pools = list(fk_key_pools or [])
    self._edges: list[tuple[tuple[str, ...], frozenset]] = []
    self._bound_side = False
    self._orphans = Metrics.counter("validation", "fk_orphans")

  def setup(self):
    # Reset BOTH: a re-entered setup() must not leave `_bound_side`
    # true against freshly-rebuilt driver edges, which would drop the
    # side input's pools and silently stop checking.
    self._edges = _compile(self.fk_key_pools)
    self._bound_side = False

  def _bind(self, fk_side: list | None) -> None:
    if fk_side and not self._bound_side:
      self._edges = self._edges + _compile(list(fk_side))
      self._bound_side = True

  # pylint: disable-next=arguments-renamed  # Beam passes the element positionally
  def process(self, record, fk_side: list | None = None):
    self._bind(fk_side)
    for cols, keys in self._edges:
      key = tuple(record.get(c) for c in cols)
      if all(v is None for v in key) or key in keys:
        continue
      self._orphans.inc()
      yield beam.pvalue.TaggedOutput(
          "invalid",
          {
              "raw_record": record,
              "error_type": "referential_integrity",
              "error_detail":
                  (f"{','.join(cols)}={key!r} is not a landed parent "
                   f"key — the row references a parent that does not "
                   f"exist"),
              "rule_id": "fk.orphan",
              "stage": "pre_write",
          },
      )
      return
    yield record


def _compile(payloads: list[dict]) -> list[tuple[tuple[str, ...], frozenset]]:
  """``[{"cols", "keys"}]`` → membership sets, built once per bundle
    worker. Hashing 100k tuples costs milliseconds; the per-row check is
    then a single set lookup."""
  return [(
      tuple(p["cols"]),
      frozenset(tuple(k) for k in (p.get("keys") or ())),
  ) for p in payloads if p.get("cols") and p.get("keys")]


__all__ = ["EnforceFkIntegrityDoFn"]
