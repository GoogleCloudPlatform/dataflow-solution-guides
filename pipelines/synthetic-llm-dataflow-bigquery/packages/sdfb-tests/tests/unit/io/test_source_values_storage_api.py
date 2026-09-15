"""`BigQuerySourceValueStore` pulls large domains through the BigQuery
Storage Read API (Arrow), not the REST row iterator (ADR 0034).

2026-08-29 R6 cold run, A_TABLE pool branch: `_fetch_identifier_domains`
paged 944,582 distinct `A_COL_005` values through
`google.cloud.bigquery` `RowIterator.__iter__` — the tabledata.list REST
path at ~2.9k rows/s — and the Beam harness reported the bundle as
"creating for at least 1078 s" (the report then read it as a generation
stall). `RowIterator.to_arrow()` uses the Storage Read API when the
result is large and the `google-cloud-bigquery-storage` client is
installed (both true on the worker image), and falls back to the cached
first page for small results. Nothing else changes: same SQL, same cap
semantics, same process cache.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=import-outside-toplevel,invalid-name,missing-class-docstring,unbalanced-tuple-unpacking,unused-argument

from __future__ import annotations

import pyarrow as pa
from sdfb_beam.io.source_values import (
    BigQuerySourceValueStore,
    clear_source_value_cache,
)


class _ArrowResult:
  """A `RowIterator` stand-in exposing `to_arrow()`; iterating it directly
    is the REST path we want to see avoided."""

  def __init__(self, values: list[str], *, arrow_fails: bool = False) -> None:
    self._values = values
    self.arrow_fails = arrow_fails
    self.arrow_calls = 0
    self.iterated = False

  def to_arrow(self, **kwargs):
    self.arrow_calls += 1
    if self.arrow_fails:
      raise RuntimeError("bqstorage unavailable")
    return pa.table({"v": pa.array(self._values, type=pa.string())})

  def __iter__(self):
    self.iterated = True
    return iter([{"v": v} for v in self._values])


class _Client:

  def __init__(self, result) -> None:
    self._result = result
    self.queries: list[str] = []

  def query(self, sql, job_config=None):
    self.queries.append(sql)
    return self

  def result(self):
    return self._result


def setup_function(_fn) -> None:
  clear_source_value_cache()


def test_fetch_distinct_reads_the_arrow_column_without_iterating_rows():
  result = _ArrowResult(["a", "b", "b", "c"])
  store = BigQuerySourceValueStore("p.d.t", client=_Client(result))
  assert store.fetch_distinct("col") == frozenset({"a", "b", "c"})
  assert result.arrow_calls == 1
  assert result.iterated is False


def test_fetch_distinct_over_cap_is_none_on_the_arrow_path():
  result = _ArrowResult([str(i) for i in range(5)])
  store = BigQuerySourceValueStore("p.d.t", cap=4, client=_Client(result))
  assert store.fetch_distinct("col") is None


def test_arrow_failure_falls_back_to_row_iteration(caplog):
  import logging

  result = _ArrowResult(["x", "y"], arrow_fails=True)
  store = BigQuerySourceValueStore("p.d.t", client=_Client(result))
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert store.fetch_distinct("col") == frozenset({"x", "y"})
  assert result.iterated is True
  assert "name=source_values_arrow_fallback" in caplog.text


def test_fetch_frequent_uses_the_arrow_path_too():
  result = _ArrowResult(["2000", "35"])
  store = BigQuerySourceValueStore("p.d.t", client=_Client(result))
  assert store.fetch_frequent("acct", 10) == frozenset({"2000", "35"})
  assert result.arrow_calls == 1
  assert result.iterated is False


def test_arrow_nulls_are_dropped_like_the_rest_path_would():
  result = _ArrowResult(["a", None, "b"])  # type: ignore[list-item]
  store = BigQuerySourceValueStore("p.d.t", client=_Client(result))
  assert store.fetch_distinct("col") == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# 2026-09-07 R7 pair (single + multi): EVERY source-domain fetch failed —
# 107 + 459 `source_values_arrow_fallback error=PermissionDenied` (the
# worker SA had no BigQuery Read Session permission) followed by
# `*_source_filter_error error=ValueError`: the fallback re-iterated the
# same RowIterator `to_arrow()` had already started
# ("Iterator has already started"). ADR 0023's rejection filters, the
# identifier/numeric domains and the ADR 0033 pool-target sizing were all
# silently inactive for two 10M-row runs. The fallback must take a FRESH
# RowIterator, and a process that has seen PermissionDenied must stop
# paying the doomed Storage attempt on every fetch.
# ---------------------------------------------------------------------------
class PermissionDenied(Exception):  # noqa: N818
  """Same class name as google.api_core.exceptions.PermissionDenied (the
    store detects the denial by class name, never by import)."""


class _RowIter:
  """A `RowIterator` stand-in with google-api-core's one-shot semantics."""

  def __init__(self, values: list[str], *,
               arrow_error: Exception | None) -> None:
    self._values = values
    self._arrow_error = arrow_error
    self._started = False
    self.arrow_calls = 0

  def to_arrow(self, **kwargs):
    self.arrow_calls += 1
    self._started = True  # the Storage attempt consumes the iterator
    if self._arrow_error is not None:
      raise self._arrow_error
    return pa.table({"v": pa.array(self._values, type=pa.string())})

  def __iter__(self):
    if self._started:
      raise ValueError("Iterator has already started", self)
    self._started = True
    return iter([{"v": v} for v in self._values])


class _Job:

  def __init__(self, values: list[str], arrow_error: Exception | None) -> None:
    self._values = values
    self._arrow_error = arrow_error
    self.iterators: list[_RowIter] = []

  def result(self):
    it = _RowIter(self._values, arrow_error=self._arrow_error)
    self.iterators.append(it)
    return it


class _JobClient:

  def __init__(self,
               values: list[str],
               arrow_error: Exception | None = None) -> None:
    self._values = values
    self._arrow_error = arrow_error
    self.jobs: list[_Job] = []

  def query(self, sql, job_config=None):
    job = _Job(self._values, self._arrow_error)
    self.jobs.append(job)
    return job


def test_rest_fallback_uses_a_fresh_row_iterator_after_a_failed_arrow_read(
    caplog):
  import logging

  client = _JobClient(["a", "b"],
                      arrow_error=PermissionDenied("readsessions.create"))
  store = BigQuerySourceValueStore("p.d.t", client=client)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert store.fetch_distinct("col") == frozenset({"a", "b"})
  (job,) = client.jobs
  assert len(job.iterators) == 2, "fallback must call result() again"
  assert job.iterators[0].arrow_calls == 1
  assert job.iterators[1].arrow_calls == 0
  assert "name=source_values_arrow_fallback" in caplog.text
  assert "error=PermissionDenied" in caplog.text


def test_permission_denied_disables_the_storage_api_for_the_process(caplog):
  import logging

  client = _JobClient(["x"],
                      arrow_error=PermissionDenied("readsessions.create"))
  store = BigQuerySourceValueStore("p.d.t", client=client)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert store.fetch_distinct("c1") == frozenset({"x"})
    assert store.fetch_frequent("c1", 10) == frozenset({"x"})
    assert store.fetch_distinct("c2") == frozenset({"x"})
  arrow_attempts = sum(
      it.arrow_calls for job in client.jobs for it in job.iterators)
  assert arrow_attempts == 1, "one doomed Storage attempt per process, not per fetch"
  assert caplog.text.count("name=source_values_storage_api_disabled") == 1


def test_a_transient_arrow_failure_does_not_disable_the_storage_api(caplog):
  import logging

  client = _JobClient(["x"], arrow_error=RuntimeError("stream reset"))
  store = BigQuerySourceValueStore("p.d.t", client=client)
  with caplog.at_level(logging.INFO, logger="sdfb.milestone"):
    assert store.fetch_distinct("c1") == frozenset({"x"})
    assert store.fetch_distinct("c2") == frozenset({"x"})
  arrow_attempts = sum(
      it.arrow_calls for job in client.jobs for it in job.iterators)
  assert arrow_attempts == 2
  assert "name=source_values_storage_api_disabled" not in caplog.text
