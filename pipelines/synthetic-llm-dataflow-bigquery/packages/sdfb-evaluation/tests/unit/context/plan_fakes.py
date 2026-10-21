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
"""An in-memory BigQuery for the planning tests, and the invented thelook
launch it serves.

`PlanBq` answers the queries `context.plan.build_plan` sends by reading
the SQL text itself: the planning SELECT's `<expr> AS c<i>_<stat>` lines
name the column (`t.\\`name\\``) and the statistic, which it computes from
invented Python rows with BigQuery's semantics (NULLs ignored by the
aggregates, counted by `APPROX_TOP_COUNT`; temporal values on the
`UNIX_MICROS` scale). The R/H panel is ranked by a stand-in fingerprint.
Nothing here is real data: thelook table names, `demo-project`, e-mails
at `example.com`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sdfb_evaluation.context.jobs import JobWrite
from sdfb_evaluation.context.launch import LaunchContext, RunRecord
from sdfb_evaluation.context.reference import reference_digest
from sdfb_evaluation.context.relationships import from_sources

from .bq_rules import check_one_point_in_time_per_table

PROJECT = "demo-project"
DS = f"{PROJECT}.thelook_synthetic"
SRC = "bigquery-public-data.thelook_ecommerce"
EXTERNAL = f"{PROJECT}.synthetic_data.products"
EXTERNAL_SOURCE = f"{SRC}.products"
QDS = f"{PROJECT}.synthetic_data_quality"
JOB_ID = "2026-09-13_06_10_16-9000000000000000017"
BASE = "thelook-0913-a1b2c3"
CREATED = "2026-09-13T13:10:16Z"
FINISHED = "2026-09-13T14:02:00Z"
NOW = "2026-09-14T08:00:00Z"
MODEL_URI = "gs://demo-bucket/synthetic/relationships/"
NAMES = ("users", "orders", "order_items")
TABLES = tuple(f"{DS}.{name}" for name in NAMES)
RUN_IDS = tuple(f"{BASE}-{i:02d}-{name}" for i, name in enumerate(NAMES))
REFERENCE_N = 40

MODEL_YAML = """
model: thelook
tables:
  users:
    pk: [id]
    identity: [email]
  orders:
    pk: [order_id]
    fk:
      - cols: [user_id]
        ref: users
        ref_cols: [id]
  order_items:
    pk: [id]
    fk:
      - cols: [order_id]
        ref: orders
        ref_cols: [order_id]
      - cols: [product_id]
        ref: synthetic_data.products
        ref_cols: [id]
"""

SCHEMAS: dict[str, list[dict]] = {
    "users": [
        {
            "name": "id",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "email",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "age",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "gender",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "country",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "signup_date",
            "type": "DATE",
            "mode": "NULLABLE"
        },
        {
            "name": "created_at",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
    ],
    "orders": [
        {
            "name": "order_id",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "user_id",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "status",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "num_of_item",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "created_at",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
    ],
    "order_items": [
        {
            "name": "id",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "order_id",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "product_id",
            "type": "INTEGER",
            "mode": "NULLABLE"
        },
        {
            "name": "sale_price",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
        {
            "name": "status",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "created_at",
            "type": "TIMESTAMP",
            "mode": "NULLABLE"
        },
    ],
    "products": [
        {
            "name": "id",
            "type": "INTEGER",
            "mode": "REQUIRED"
        },
        {
            "name": "category",
            "type": "STRING",
            "mode": "NULLABLE"
        },
        {
            "name": "retail_price",
            "type": "FLOAT",
            "mode": "NULLABLE"
        },
    ],
}

_COUNTRIES = ("Spain", "France", "Brazil", "Japan", "Kenya")
_STATUSES = ("Complete", "Shipped", "Processing", "Returned", "Cancelled")
_CATEGORIES = ("Jeans", "Tops", "Socks", "Outerwear")
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def thelook_rows(seed: int) -> dict[str, list[dict]]:
  """Invented thelook-shaped rows: 120 users, 240 orders, 480 items and 30
  products, referentially intact."""
  rng = random.Random(seed)
  users = [{
      "id": i,
      "email": f"user{seed}x{i}@example.com",
      "age": rng.randint(18, 70),
      "gender": rng.choice(("F", "M")),
      "country": rng.choice(_COUNTRIES) if rng.random() > 0.05 else None,
      "signup_date": date(2025, 1, 1) + timedelta(days=rng.randint(0, 300)),
      "created_at": _EPOCH + timedelta(minutes=rng.randint(0, 90_000)),
  } for i in range(1, 121)]
  orders = [{
      "order_id": i,
      "user_id": rng.randint(1, 120),
      "status": rng.choice(_STATUSES),
      "num_of_item": rng.randint(1, 4),
      "created_at": _EPOCH + timedelta(minutes=rng.randint(0, 90_000)),
  } for i in range(1, 241)]
  items = [{
      "id": i,
      "order_id": rng.randint(1, 240),
      "product_id": rng.randint(1, 30),
      "sale_price": round(rng.uniform(5.0, 120.0), 2),
      "status": rng.choice(_STATUSES),
      "created_at": _EPOCH + timedelta(minutes=rng.randint(0, 90_000)),
  } for i in range(1, 481)]
  products = [{
      "id": i,
      "category": rng.choice(_CATEGORIES),
      "retail_price": round(rng.uniform(5.0, 150.0), 2),
  } for i in range(1, 31)]
  return {
      "users": users,
      "orders": orders,
      "order_items": items,
      "products": products
  }


def thelook_models():
  return from_sources([(f"{MODEL_URI}thelook.yaml", MODEL_YAML)])


def _fingerprint(row: Mapping[str, Any]) -> str:
  """A stand-in for FARM_FINGERPRINT(TO_JSON_STRING(ref))."""
  text = json.dumps(row, sort_keys=True, default=str)
  return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def _micros(value: Any) -> Any:
  """BigQuery's planning scale: temporal values as epoch microseconds."""
  if isinstance(value, datetime):
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return (aware -
            datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
  if isinstance(value, date):
    return _micros(datetime(value.year, value.month, value.day, tzinfo=UTC))
  if isinstance(value, time):
    return ((value.hour * 60 + value.minute) * 60 +
            value.second) * 1_000_000 + value.microsecond
  if isinstance(value, float) and not math.isfinite(value):
    return None
  if isinstance(value, Decimal):
    return float(value)
  return value


_LINE_RE = re.compile(
    r"^\s*(?P<sql>.+) AS (?P<alias>c\d+_(?P<stat>[a-z_]+)),?$")
_COLUMN_RE = re.compile(r"t\.`([^`]+)`")
_TOP_RE = re.compile(r"APPROX_TOP_COUNT\(.*, (\d+)\)")


def _top(values: Sequence[Any], k: int) -> list[dict]:
  counts = Counter(values)
  ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
  return [{"value": v, "count": c} for v, c in ranked[:k]]


def _quantiles(values: Sequence[float]) -> list[float] | None:
  if not values:
    return None
  ordered = sorted(values)
  last = len(ordered) - 1
  return [ordered[round(i * last / 1000)] for i in range(1001)]


def _mean(values: Sequence[float]) -> float | None:
  return sum(values) / len(values) if values else None


def _std(values: Sequence[float]) -> float | None:
  mean = _mean(values)
  if mean is None:
    return None
  return math.sqrt(sum((v - mean)**2 for v in values) / len(values))


def _is_midnight(value: Any) -> bool:
  return isinstance(value,
                    datetime) and (value.hour, value.minute, value.second,
                                   value.microsecond) == (0, 0, 0, 0)


def _stat(stat: str, sql: str, values: list[Any]) -> Any:
  """One planning statistic, with BigQuery's NULL semantics."""
  present = [v for v in values if v is not None]
  scaled = [
      v if isinstance(v, (bool, str, bytes)) else _micros(v) for v in values
  ]
  numeric = [
      v for v, raw in zip(scaled, values, strict=True)
      if v is not None and not isinstance(raw, (bool, str, bytes))
  ]
  handlers = {
      "null":
          lambda: (sum(1 for v in values if not v)
                   if "ARRAY_LENGTH" in sql else len(values) - len(present)),
      "empty":
          lambda: sum(1 for v in present
                      if isinstance(v, str) and not v.strip()),
      "distinct":
          lambda: len({json.dumps(v, default=str) for v in present}),
      "quantiles":
          lambda: _quantiles(numeric),
      "mean":
          lambda: _mean(numeric),
      "std":
          lambda: _std(numeric),
      "min":
          lambda: min(numeric) if numeric else None,
      "max":
          lambda: max(numeric) if numeric else None,
      # APPROX_TOP_COUNT counts NULL as a value.
      "top":
          lambda: _top(scaled, int(_TOP_RE.search(sql).group(1))
                      ),  # type: ignore[union-attr]
      "avg_len":
          lambda: _mean([len(v) for v in present]),
      "midnight":
          lambda: sum(1 for v in present if _is_midnight(v)),
  }
  if stat not in handlers:
    raise AssertionError(f"unknown planning statistic {stat!r}")
  return handlers[stat]()


class PlanBq:
  """`context.bq.Bq` over invented rows (see the module docstring)."""

  def __init__(self,
               *,
               source: Mapping[str, list[dict]],
               landing: Mapping[str, list[dict]],
               schemas: Mapping[str, list[dict]] | None = None,
               dry_bytes: int = 1_000_000,
               location: str | None = "EU"):
    self.project = PROJECT
    self.location = location
    self.dry_bytes = dry_bytes
    schemas = schemas or SCHEMAS
    self.rows: dict[str, list[dict]] = {}
    self.tables: dict[str, dict] = {}
    for name, rows in landing.items():
      fqn = EXTERNAL if name == "products" else f"{DS}.{name}"
      self._add(fqn, schemas[name], rows)
    for name, rows in source.items():
      self._add(f"{SRC}.{name}", schemas[name], rows)
    self.foreign: list[dict] = []
    self.queries: list[tuple[str, dict]] = []
    self.dry_runs: list[tuple[str, dict]] = []
    self.executed: list[tuple[str, dict]] = []
    self.execute_caps: list[int | None] = []
    # SQL substring → the error a dry run / an execute raises instead.
    self.dry_failures: dict[str, BaseException] = {}
    self.execute_failures: dict[str, BaseException] = {}
    self.max_bytes: list[int | None] = []
    self.events: list[tuple[str, str]] = []  # (dry | query | execute, sql)

  def _add(self, fqn: str, schema: list[dict], rows: list[dict]) -> None:
    self.rows[fqn] = [dict(r) for r in rows]
    self.tables[fqn] = {
        "schema": copy.deepcopy(schema),
        "numRows": len(rows),
        "location": "EU",
        "timePartitioning": None,
        "lastModified": "2026-09-01T00:00:00.000000Z",
        "timeTravelHours": 168,
    }

  # --- the Bq surface ------------------------------------------------------
  def table(self, fqn: str) -> dict:
    if fqn not in self.tables:
      raise LookupError(f"table {fqn}: Not found")
    return copy.deepcopy(self.tables[fqn])

  def dry_run_bytes(self,
                    sql: str,
                    params: Mapping[str, Any] | None = None) -> int:
    check_one_point_in_time_per_table(sql)
    self.dry_runs.append((sql, dict(params or {})))
    self.events.append(("dry", sql))
    for needle, exc in self.dry_failures.items():
      if needle in sql:
        raise exc
    return self.dry_bytes

  def execute(self,
              sql: str,
              params: Mapping[str, Any] | None = None,
              *,
              max_bytes: int | None = None) -> None:
    check_one_point_in_time_per_table(sql)
    for needle, exc in self.execute_failures.items():
      if needle in sql:
        raise exc
    self.executed.append((sql, dict(params or {})))
    self.execute_caps.append(max_bytes)
    self.events.append(("execute", sql))

  def job_stats(self, job_id: str, location: str) -> dict:
    raise AssertionError(f"unexpected jobs.get {job_id} {location}")

  def query(self,
            sql: str,
            params: Mapping[str, Any] | None = None,
            *,
            max_bytes: int | None = None) -> list[dict]:
    check_one_point_in_time_per_table(sql)
    self.queries.append((sql, dict(params or {})))
    self.max_bytes.append(max_bytes)
    self.events.append(("query", sql))
    if "JOBS_BY_PROJECT" in sql:
      return copy.deepcopy(self.foreign)
    fqn = self.reads(sql)
    if "__sdfb_rk" in sql:
      size = int(re.search(r"LIMIT (\d+)",
                           sql).group(1))  # type: ignore[union-attr]
      return [
          dict(row, __sdfb_rk=rank)
          for rank, row in enumerate(self.ordered(fqn)[:size], start=1)
      ]
    if "n_rows" in sql:
      return [self._planning(sql, self.rows[fqn])]
    raise AssertionError(f"PlanBq: unexpected SQL {sql!r}")

  # --- helpers ---------------------------------------------------------------
  def reads(self, sql: str) -> str:
    """The one known table the SQL reads (longest name first, so orders
    never shadows order_items)."""
    found = [
        fqn for fqn in sorted(self.rows, key=len, reverse=True)
        if f"`{fqn}`" in sql
    ]
    assert found, f"PlanBq: no known table in {sql!r}"
    return found[0]

  def ordered(self, fqn: str) -> list[dict]:
    """The rows in the generator's reference order."""
    return sorted((dict(r) for r in self.rows[fqn]), key=_fingerprint)

  def digest(self, name: str, n: int = REFERENCE_N) -> str:
    """What the generator recorded for its LIMIT-n sample of `name`."""
    return reference_digest(self.ordered(f"{SRC}.{name}")[:n])

  def planning_queries(self) -> list[tuple[str, dict]]:
    return [(sql, p) for sql, p in self.queries if "n_rows" in sql]

  @staticmethod
  def _planning(sql: str, rows: list[dict]) -> dict:
    out: dict[str, Any] = {"n_rows": len(rows)}
    for line in sql.splitlines():
      match = _LINE_RE.match(line)
      if match is None:
        continue
      column = _COLUMN_RE.search(match.group("sql"))
      assert column, line
      values = [row.get(column.group(1)) for row in rows]
      out[match.group("alias")] = _stat(
          match.group("stat"), match.group("sql"), values)
    return out


def thelook_bq(**kwargs: Any) -> PlanBq:
  """Source = generator seed 1, landing (synthetic) = seed 2."""
  source = thelook_rows(1)
  landing = thelook_rows(2)
  return PlanBq(source=source, landing=landing, **kwargs)


def thelook_launch(bq: PlanBq,
                   *,
                   write_disposition: str | None = "overwrite",
                   tables: Sequence[str] = TABLES,
                   run_ids: Sequence[str] = RUN_IDS,
                   params: Mapping[str, Any] | None = None,
                   relationships_uri: str | None = MODEL_URI,
                   **fields: Any) -> LaunchContext:
  """A resolved relational launch of the thelook closure."""
  writes, runs = [], []
  for i, table in enumerate(tables):
    name = table.rsplit(".", 1)[1]
    start = datetime(2026, 9, 13, 13, 40, tzinfo=UTC) + timedelta(minutes=5 * i)
    rows = len(bq.rows.get(table, []))
    writes.append(
        JobWrite(
            table=table,
            job_type="LOAD",
            start=start.isoformat(),
            end=(start + timedelta(seconds=20)).isoformat(),
            output_rows=rows,
            job_id=f"beam_bq_job_LOAD_{name}_{i}"))
    runs.append(
        RunRecord(
            landing_table=table,
            run_id=run_ids[i] if i < len(run_ids) else f"{BASE}-{i:02d}-{name}",
            reference_table=f"{SRC}.{name}",
            reference_digest=bq.digest(name)
            if f"{SRC}.{name}" in bq.rows else None,
            valid_count=rows,
            num_rows_requested=rows,
            status="PASSED",
            created_at=(start + timedelta(minutes=1)).isoformat()))
  launch_params: dict[str, Any] = {
      "engine": "b1_rag",
      "model_uri": "gs://demo-bucket/synthetic/models/qwen/qwen2.5-3b/v1/",
      "embedder_uri": "",
      "seed": "",
      "reference_rows_limit": REFERENCE_N,
      "num_rows": 5000,
      "write_disposition": write_disposition,
      "generate_fk_relationships": "true",
      "relationships_uri": relationships_uri or "",
      "run_id": BASE,
      "env": "dev",
      "pk_cols": "",
      "identity_cols": "",
  }
  launch_params.update(params or {})
  values: dict[str, Any] = {
      "generation_job_id": JOB_ID,
      "job_name": "sdfb-thelook-0913",
      "region": "europe-west1",
      "started_at": CREATED,
      "finished_at": FINISHED,
      "base_run_id": BASE,
      "run_ids": tuple(run_ids),
      "tables_in_order": tuple(tables),
      "reference_table": None,
      "write_disposition": write_disposition,
      "relationships_uri": relationships_uri,
      "params": launch_params,
      "model_sha": None,
      "model_name": "thelook",
      "adjusted_model_uri": None,
      "params_source": "jobs_labels+logs",
      "writes": tuple(writes),
      "runs": tuple(runs),
      "model_adjusted": False,
      "warnings": (),
  }
  values.update(fields)
  return LaunchContext(**values)
