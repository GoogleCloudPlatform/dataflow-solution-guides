"""ADR 0036: the SOURCE fan-out histogram and PK cells, measured once,
cached by (source child, edge cols, model sha). ADR 0038 fix J adds the
DECLARED PK's own measurement, cached in the same payload."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,redefined-outer-name,reimported,unbalanced-tuple-unpacking,unused-argument

from __future__ import annotations

import logging

import pytest
from sdfb_beam.io.fanout_stats import (
    BigQueryFanoutStatsStore,
    fanout_payload,
    measure_fanout,
)


class _Result:

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return self._rows


class _Client:
  """Answers the three queries by shape of SQL."""

  def __init__(self):
    self.sql: list[str] = []
    self.loaded: list[list[dict]] = []

  def query(self, sql, job_config=None):
    self.sql.append(sql)
    if "AS k" in sql:
      return _Result([{"k": 1, "parents": 30}, {"k": 2, "parents": 20}])
    if "COUNT(DISTINCT" in sql or "APPROX_COUNT_DISTINCT" in sql:
      return _Result([{"parents": 100}])
    if "AS n FROM" in sql and "GROUP BY" in sql:  # cells
      return _Result([{
          "C2": "C0",
          "D18": "K0",
          "n": 40
      }, {
          "C2": "C1",
          "D18": "K1",
          "n": 20
      }])
    if "SELECT payload" in sql:
      return _Result([])
    raise AssertionError(sql)

  def load_table_from_json(self, rows, table, job_config=None):
    self.loaded.append(rows)
    return _Result([])


def test_measure_fanout_builds_histogram_with_zero_bucket_and_cells():
  client = _Client()
  out = measure_fanout(
      source_child="p.src.C_TABLE",
      child_cols=("D_COL_001",),
      source_parent="p.src.B_TABLE",
      ref_cols=("D_COL_001",),
      cell_cols=("C2", "D18"),
      client=client,
  )
  # 100 parents, 50 with children (30 x1 + 20 x2) -> 50 in the zero bucket
  assert out["histogram"] == {"0": 50, "1": 30, "2": 20}
  assert out["parents"] == 100 and out["children"] == 70
  assert out["cells"] == {
      "cols": ["C2", "D18"],
      "rows": [["C0", "K0"], ["C1", "K1"]],
      "counts": [40.0, 20.0]
  }


def test_measure_fanout_builds_a_client_when_none_is_given(monkeypatch):
  fake = _Client()
  monkeypatch.setattr("google.cloud.bigquery.Client", lambda: fake)
  out = measure_fanout(
      source_child="p.src.C_TABLE",
      child_cols=("D_COL_001",),
      source_parent="p.src.B_TABLE",
      ref_cols=("D_COL_001",),
      cell_cols=(),
  )
  assert out["histogram"] == {"0": 50, "1": 30, "2": 20}


def test_measure_fanout_without_cell_columns():
  out = measure_fanout(
      source_child="p.src.C",
      child_cols=("K",),
      source_parent="p.src.P",
      ref_cols=("K",),
      cell_cols=(),
      client=_Client(),
  )
  assert out["cells"] is None


def test_store_round_trip_uses_a_load_job():
  client = _Client()
  store = BigQueryFanoutStatsStore(
      "p.synthetic_data_quality.fk_fanout_stats", client=client)
  assert store.get("p.src.C", ("K",), "abc") is None
  store.put("p.src.C", ("K",), "abc", {
      "histogram": {
          "0": 1
      },
      "cells": None,
      "parents": 1,
      "children": 0
  })
  (rows,) = client.loaded
  assert rows[0]["source_table"] == "p.src.C" and rows[0]["model_sha"] == "abc"
  assert '"histogram"' in rows[0]["payload"]


def test_fanout_payload_shape():
  payload = fanout_payload(
      {
          "histogram": {
              "0": 1,
              "2": 1
          },
          "cells": None,
          "parents": 2,
          "children": 2
      },
      driving_cols=("K", "INH"),
      exact_cells=False,
  )
  # `parents` rides along for ADR 0039's projection warnings — an
  # orphan-heavy source clamps the zero bucket to 0, so the source
  # parent count cannot be read back off the histogram alone.
  assert payload == {
      "driving_cols": ["K", "INH"],
      "histogram": {
          "0": 1,
          "2": 1
      },
      "cells": None,
      "exact_cells": False,
      "pk_source": None,
      "parents": 2
  }


class _OrphanClient(_Client):
  """Same child histogram (50 parent tuples with children) but only 40
    parent tuples in the source parent — 10 child tuples are orphans."""

  def query(self, sql, job_config=None):
    if "COUNT(DISTINCT" in sql or "APPROX_COUNT_DISTINCT" in sql:
      self.sql.append(sql)
      return _Result([{"parents": 40}])
    return super().query(sql, job_config=job_config)


def test_source_orphans_are_announced_not_silently_clamped(caplog):
  """ADR 0036 review I6: `max(0, parents - with_children)` hid the case
    where the SOURCE child holds tuples its parent does not — the zero
    bucket is then unmeasurable and children/parents OVERSTATES the mean,
    which sizes the whole driven child."""
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    out = measure_fanout(
        source_child="p.src.C_TABLE",
        child_cols=("D_COL_001",),
        source_parent="p.src.B_TABLE",
        ref_cols=("D_COL_001",),
        cell_cols=(),
        client=_OrphanClient(),
        edge="C_TABLE(D_COL_001)->B_TABLE",
    )
  assert out["histogram"]["0"] == 0
  assert out["parents"] == 40 and out["children"] == 70
  assert "name=fk_fanout_source_orphans" in caplog.text
  assert "child_tuples=50" in caplog.text
  assert "parent_tuples=40" in caplog.text


def test_no_orphan_milestone_when_every_child_tuple_has_a_parent(caplog):
  with caplog.at_level(logging.WARNING, logger="sdfb.milestone"):
    measure_fanout(
        source_child="p.src.C_TABLE",
        child_cols=("D_COL_001",),
        source_parent="p.src.B_TABLE",
        ref_cols=("D_COL_001",),
        cell_cols=(),
        client=_Client(),
    )
  assert "fk_fanout_source_orphans" not in caplog.text


# Launch 2026-09-12_14_50_30 logged, for one edge in a single milestone:
#   children=1172025 max=13 mean=22.3052 p50=2 p95=2 parents=52545
# A mean cannot exceed the maximum. The histogram counts rows per CHILD
# KEY VALUE, but the mean divided the total child rows by the PARENT key
# count — and 583,134 of that child's key values have no parent in the
# source, so the figure an operator reads was 11x the measured one.
def _measured(histogram: dict, parents: int) -> dict:
  children = sum(k * n for k, n in histogram.items())
  return {
      "histogram": histogram,
      "parents": parents,
      "children": children,
      "cells": None
  }


def test_the_logged_mean_never_exceeds_the_histogram_max(caplog):
  import logging

  from sdfb_beam.io.fanout_stats import log_fanout_measured

  # Five key values of two rows each, but only two parent keys exist:
  # the source child holds tuples its parent does not.
  caplog.clear()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    log_fanout_measured(
        "(K)->P", _measured({2: 5}, parents=2), source="measured")
  line = next(
      ln for ln in caplog.text.splitlines() if "fk_fanout_measured" in ln)
  assert "mean=2.0" in line, line
  assert "max=2" in line and "mean=5.0" not in line


def test_the_orphan_key_values_reach_the_log(caplog):
  """The operator must see WHY the mean is per key value and not per
    parent: 3 of the 5 key values have no parent in the source."""
  import logging

  from sdfb_beam.io.fanout_stats import log_fanout_measured

  caplog.clear()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    log_fanout_measured(
        "(K)->P", _measured({2: 5}, parents=2), source="measured")
  line = next(
      ln for ln in caplog.text.splitlines() if "fk_fanout_measured" in ln)
  assert "key_values=5" in line and "orphan_keys=3" in line


def test_a_clean_source_keeps_todays_numbers(caplog):
  """No orphans: the zero bucket is real, the histogram total IS the
    parent count, and the mean is unchanged from before this fix."""
  import logging

  from sdfb_beam.io.fanout_stats import log_fanout_measured

  caplog.clear()
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    log_fanout_measured(
        "(K)->P", _measured({
            0: 4,
            1: 3,
            2: 3
        }, parents=10), source="measured")
  line = next(
      ln for ln in caplog.text.splitlines() if "fk_fanout_measured" in ln)
  assert "mean=0.9" in line and "orphan_keys=0" in line and "key_values=6" in line


# --- fix J: the DECLARED PK, measured on the source child -------------
#
# 2026-09-13 (…-12600311608685394436): F_TABLE declares
# `pk: [D_COL_001, CONTINUOUS_NR]` and is driven by `(D_COL_001)`, so its
# key has a member OUTSIDE the driving edge. The fan-out histogram
# describes the EDGE, never that key, so nothing measured it and the run
# died at the BLOCKER gate with 179,853 pk.duplicate (0.2908 > 0.2).


class _PkClient:
  """Answers the declared-PK measurement's single GROUP BY."""

  def __init__(self, rows):
    self.sql: list[str] = []
    self._rows = rows

  def query(self, sql, job_config=None):
    self.sql.append(sql)
    return _Result(self._rows)


def test_measure_pk_uniqueness_counts_a_composite_key_as_one_tuple():
  """A two-column PK is ONE key, not two columns: the GROUP BY is over
    the tuple, so 12 rows over 9 tuples is a 25% repeat share — not the
    per-column distinct counts, which would each read far higher."""
  from sdfb_beam.io.fanout_stats import measure_pk_uniqueness

  client = _PkClient([{
      "key_tuples": 9,
      "row_count": 12,
      "max_rows_per_key": 3
  }])
  out = measure_pk_uniqueness(
      source_child="p.src.F_TABLE",
      pk_cols=("D_COL_001", "CONTINUOUS_NR"),
      client=client,
  )
  assert out == {
      "cols": ["D_COL_001", "CONTINUOUS_NR"],
      "rows": 12,
      "key_tuples": 9,
      "max_rows_per_key": 3
  }
  (sql,) = client.sql
  assert "GROUP BY `D_COL_001`, `CONTINUOUS_NR`" in sql
  assert "`p.src.F_TABLE`" in sql


def test_the_pk_measurement_handles_nulls_the_way_the_histogram_does():
  """NULL handling matches `measure_fanout`'s child GROUP BY: a NULL
    member is a VALUE of the key, grouped with its equals. A
    `COUNT(DISTINCT CONCAT(...))` would drop every NULL-bearing tuple
    from the key count while the row count kept it, understating the
    repeat share on exactly the sparse columns that motivate the check."""
  from sdfb_beam.io.fanout_stats import measure_pk_uniqueness

  client = _PkClient([{"key_tuples": 2, "row_count": 5, "max_rows_per_key": 4}])
  measure_pk_uniqueness(
      source_child="p.src.F_TABLE",
      pk_cols=("A", "B"),
      client=client,
  )
  (sql,) = client.sql
  assert "IS NOT NULL" not in sql
  assert "COUNT(DISTINCT" not in sql


def test_the_pk_measurement_rejects_a_column_that_is_not_an_identifier():
  from sdfb_beam.io.fanout_stats import measure_pk_uniqueness

  with pytest.raises(ValueError, match="not a BigQuery column name"):
    measure_pk_uniqueness(
        source_child="p.src.F",
        pk_cols=("A`; DROP",),
        client=_PkClient([]),
    )


def test_the_pk_measurement_rides_in_the_cached_payload():
  """Cached beside the fan-out under the same key, so a re-launch pays
    for neither scan."""
  from sdfb_beam.io.fanout_stats import fanout_payload

  payload = fanout_payload(
      {
          "histogram": {
              "0": 1,
              "2": 1
          },
          "cells": None,
          "parents": 2,
          "children": 2,
          "pk": {
              "cols": ["K", "NR"],
              "rows": 12,
              "key_tuples": 9,
              "max_rows_per_key": 3
          }
      },
      driving_cols=("K",),
      exact_cells=False,
  )
  assert payload["pk_source"] == {
      "cols": ["K", "NR"],
      "rows": 12,
      "key_tuples": 9,
      "max_rows_per_key": 3
  }
  assert fanout_payload(
      {
          "histogram": {},
          "cells": None,
          "parents": 0,
          "children": 0
      },
      driving_cols=("K",),
      exact_cells=False,
  )["pk_source"] is None
