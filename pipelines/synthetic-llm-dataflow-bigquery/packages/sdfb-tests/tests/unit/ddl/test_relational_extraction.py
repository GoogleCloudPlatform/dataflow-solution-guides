"""The extractor reads BigQuery's own metadata — never a relational
contract from the table description (ADR 0032).

PK/FK/identity of RECORD live in `config/relationships/`. A description
that still carries a legacy `{"sdfb": 1, …}` object is inert: it must not
be parsed, mirrored into `_ddl.json`, or silently applied to a run.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=protected-access

from unittest.mock import MagicMock

from sdfb_beam.ddl.extractor import extract_ddl_metadata
from sdfb_core.contracts import TableSchema

_LEGACY_DESC = (
    "Ops table. "
    '{"sdfb": 1, "pk": ["ID", "SEQ"], '
    '"fk": [{"cols": ["CUST_ID"], "ref": "ds.customers", "ref_cols": ["ID"]}], '
    '"identity": ["ID"]}')


def _make_field(name: str, ftype: str) -> MagicMock:
  f = MagicMock()
  f.name = name
  f.field_type = ftype
  f.mode = "REQUIRED"
  f.description = ""
  f.fields = None
  f.max_length = None
  f.precision = None
  f.scale = None
  return f


def _make_table(description: str, constraints=None) -> MagicMock:
  t = MagicMock()
  t.schema = [_make_field(n, "STRING") for n in ("ID", "SEQ", "CUST_ID")]
  t.created = None
  t.modified = None
  t.expires = None
  t.location = "EU"
  t.description = description
  t.labels = {}
  t.table_type = "TABLE"
  t.encryption_configuration = None
  t.time_partitioning = None
  t.range_partitioning = None
  t.clustering_fields = None
  t.num_rows = 100
  t.require_partition_filter = False
  t._properties = {}
  t.table_constraints = constraints
  return t


def _extract(description: str, constraints=None) -> dict:
  client = MagicMock()
  client.get_table.return_value = _make_table(description, constraints)
  client.query.return_value.result.return_value = iter([])
  return extract_ddl_metadata(
      project="p", dataset="d", table="t", client=client)


def test_a_legacy_description_contract_is_inert():
  """The old marker must not come back through the side door."""
  result = _extract(_LEGACY_DESC)
  assert "relational" not in result["table_info"]
  assert result["primary_keys"] is None


def test_bigquery_declared_primary_key_is_still_read():
  constraints = MagicMock()
  constraints.primary_key.columns = ["ID", "SEQ"]
  assert _extract("Ops table.", constraints)["primary_keys"] == ["ID", "SEQ"]


def test_legacy_description_line_stays_the_last_fallback():
  result = _extract("Test table.\nPRIMARY KEY: ID\nOther notes.")
  assert result["primary_keys"] == ["ID"]


def test_table_schema_has_no_relational_accessor():
  """TableSchema describes COLUMNS; relations are not its business."""
  schema = TableSchema.model_validate({
      "table_info": {
          "table_id": "p.d.t",
          "description": _LEGACY_DESC
      },
      "schema": [{
          "name": "ID",
          "type": "STRING",
          "mode": "REQUIRED"
      }],
  })
  assert not hasattr(schema, "relational_contract")
