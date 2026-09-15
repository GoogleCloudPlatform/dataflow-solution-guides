"""Tests for `sdfb_beam.handlers.fake_client.FakeModelClient`."""

# Test module: pytest fixtures and white-box access are intentional.
# pylint: disable=unused-argument

from __future__ import annotations

import pytest
from sdfb_beam.handlers.fake_client import FakeModelClient
from sdfb_core.engines import ModelClient


def test_satisfies_model_client_protocol():
  fake = FakeModelClient(responses=[{"x": 1}])
  assert isinstance(fake, ModelClient)


def test_rejects_no_args():
  with pytest.raises(ValueError, match="exactly one"):
    FakeModelClient()


def test_rejects_both_args():
  with pytest.raises(ValueError, match="exactly one"):
    FakeModelClient(responses=[{"a": 1}], reference_pool=[{"b": 2}])


def test_mode_property():
  assert FakeModelClient(responses=[{"a": 1}]).mode == "canned"
  assert FakeModelClient(reference_pool=[{"b": 2}]).mode == "echo"


def test_canned_mode_cycles_in_order():
  responses = [{"a": 1}, {"a": 2}, {"a": 3}]
  fake = FakeModelClient(responses=responses)
  out = fake.generate_json("p", {}, n=5)
  assert out == [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 1}, {"a": 2}]


def test_canned_mode_cursor_persists_across_calls():
  responses = [{"a": 1}, {"a": 2}]
  fake = FakeModelClient(responses=responses)
  first = fake.generate_json("p", {}, n=1)
  second = fake.generate_json("p", {}, n=1)
  assert first == [{"a": 1}]
  assert second == [{"a": 2}]


def test_echo_mode_deterministic_by_prompt():
  pool = [{"id": i} for i in range(8)]
  a = FakeModelClient(reference_pool=pool)
  b = FakeModelClient(reference_pool=pool)
  out_a = a.generate_json("same prompt", {"k": "v"}, n=3)
  out_b = b.generate_json("same prompt", {"k": "v"}, n=3)
  assert out_a == out_b


def test_echo_mode_different_prompts_diverge():
  pool = [{"id": i} for i in range(64)]  # large pool — collisions unlikely
  fake_a = FakeModelClient(reference_pool=pool)
  fake_b = FakeModelClient(reference_pool=pool)
  out_a = fake_a.generate_json("prompt A", {}, n=4)
  out_b = fake_b.generate_json("prompt B", {}, n=4)
  assert out_a != out_b


def test_echo_mode_seed_changes_output():
  pool = [{"id": i} for i in range(64)]
  fake_a = FakeModelClient(reference_pool=pool)
  fake_b = FakeModelClient(reference_pool=pool)
  out_a = fake_a.generate_json("p", {}, n=4, seed=1)
  out_b = fake_b.generate_json("p", {}, n=4, seed=2)
  assert out_a != out_b


def test_echo_mode_same_seed_same_output():
  pool = [{"id": i} for i in range(64)]
  fake_a = FakeModelClient(reference_pool=pool)
  fake_b = FakeModelClient(reference_pool=pool)
  out_a = fake_a.generate_json("p", {}, n=4, seed=42)
  out_b = fake_b.generate_json("p", {}, n=4, seed=42)
  assert out_a == out_b


def test_call_count_increments():
  fake = FakeModelClient(responses=[{"a": 1}])
  fake.generate_json("p", {}, n=1)
  fake.generate_json("p", {}, n=5)
  assert fake.call_count == 2


def test_echo_mode_loads_real_fixture(customers_schema, customers_reference):
  """Sanity: fixture data round-trips through echo mode."""
  fake = FakeModelClient(reference_pool=customers_reference)
  out = fake.generate_json("prompt", {}, n=3)
  assert len(out) == 3
  for record in out:
    assert record in customers_reference
