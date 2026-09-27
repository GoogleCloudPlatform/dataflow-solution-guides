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
"""Property test: hypothesis-generated records pass the derived Pydantic model.

This is the strongest available evidence that the schema-driven generator
(`sdfb_tests.strategies.record_strategy`) and the schema-driven validator
(`sdfb_core.codegen.derive_record_model`) are mutually consistent.

Both derive from the same `TableSchema` source-of-truth, so any divergence
here means one side has a bug the other doesn't catch.
"""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=invalid-name

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sdfb_core.codegen import derive_record_model
from sdfb_core.contracts import TableSchema
from sdfb_tests.fixtures import load_ddl
from sdfb_tests.strategies import record_strategy


def _customers_schema() -> TableSchema:
  return load_ddl("customers")


def _orders_schema() -> TableSchema:
  return load_ddl("orders")


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_customers_records_pass_pydantic(data):
  schema = _customers_schema()
  Record = derive_record_model(schema)
  record_dict = data.draw(record_strategy(schema))
  instance = Record.model_validate(record_dict)
  # Round-trip through model_dump and re-validate.
  Record.model_validate(instance.model_dump())


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_orders_records_pass_pydantic(data):
  """Wide table with STRUCT + REPEATED — exercises recursion."""
  schema = _orders_schema()
  Record = derive_record_model(schema)
  record_dict = data.draw(record_strategy(schema))
  Record.model_validate(record_dict)
