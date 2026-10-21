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
"""Tests for `sdfb_evaluation.beam.staged` (Ruling R154): driver-built
panel data embedded in the graph (`beam.Create`, no staging) or written
as one object under a staging root and read back by a worker, digest
first. The root here is a local directory: the same `FileSystems` calls
write to Cloud Storage on Dataflow. No network.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import dataclasses
import hashlib
import pickle
import time
from pathlib import Path
from typing import Any

import apache_beam as beam
import pytest
from apache_beam.testing.test_pipeline import TestPipeline as BeamTestPipeline
from apache_beam.testing.util import assert_that, equal_to

from sdfb_evaluation.beam import staged
from sdfb_evaluation.beam.staged import StagedObjectError, Staging

from sdfb_evaluation.beam.io import BigQuerySources, InMemorySources
from sdfb_evaluation.types import Side

from .tables import all_types_plan, client_rows, make_panel

READ = "Read[demo-project.thelook_synthetic.order_items/reference]"


@dataclasses.dataclass(frozen=True)
class _Payload:
  """One driver-built object of a class of its own, as a table's panel
  refs or privacy inputs are."""
  n: int
  cells: tuple[float, ...]


def _described(value: Any) -> str:
  """A value with the type of every cell: `equal_to` then compares
  types too, and NaN with itself."""
  if isinstance(value, dict):
    cells = sorted((k, type(v).__name__, repr(v)) for k, v in value.items())
    return repr(cells)
  return f"{type(value).__name__}:{value!r}"


def _wide_rows(n: int) -> list[dict]:
  """`n` rows no compression makes small: 64 hex characters each."""
  return [{
      "id": i,
      "token": hashlib.sha256(str(i).encode()).hexdigest()
  } for i in range(n)]


def _labels(p: beam.Pipeline) -> set[str]:
  return {
      t.unique_name for t in p.to_runner_api().components.transforms.values()
  }


def _staging(tmp_path: Path) -> Staging:
  return Staging(str(tmp_path / "panel"))


# --------------------------------------------------------------------------
# no staging root: beam.Create, as before
# --------------------------------------------------------------------------
def test_without_a_root_it_is_beam_create_itself(tmp_path):
  rows = client_rows()
  plain, helped = beam.Pipeline(), beam.Pipeline()
  wanted = plain | READ >> beam.Create(rows)
  found = staged.create(helped, READ, rows)
  assert isinstance(found.producer.parent.transform, beam.Create)
  assert found.element_type == wanted.element_type
  assert _labels(helped) == _labels(plain)  # the same graph, label for label
  assert not list(tmp_path.iterdir())  # and nothing written anywhere


def test_without_a_root_the_elements_are_the_values():
  rows = client_rows()
  with BeamTestPipeline() as p:
    found = staged.create(p, READ, rows) | beam.Map(_described)
    assert_that(found, equal_to([_described(row) for row in rows]))


# --------------------------------------------------------------------------
# a staging root: one object, a reference in the graph
# --------------------------------------------------------------------------
def test_with_a_root_the_values_are_one_object(tmp_path):
  rows = client_rows()
  staging = _staging(tmp_path)
  staged.create(beam.Pipeline(), READ, rows, staging)
  assert len(staging.objects) == 1
  written = staging.objects[0]
  (path,) = (tmp_path / "panel").iterdir()
  assert written.uri == str(path)
  payload = path.read_bytes()
  # the object IS the list of values (NaN and all: compared described)
  assert [_described(v) for v in pickle.loads(payload)
         ] == [_described(row) for row in rows]
  assert written.sha256 == hashlib.sha256(payload).hexdigest()
  assert (written.elements, written.size) == (len(rows), len(payload))
  assert (staging.size, staging.seconds >= 0) == (len(payload), True)
  # the object says what it holds and cannot collide with another payload
  assert path.name.startswith("Read_demo-project.thelook_synthetic.order_items")
  assert path.name.endswith(f"-{written.sha256[:16]}.pickle")


def test_two_payloads_are_two_objects_and_the_tally_adds_up(tmp_path):
  staging = _staging(tmp_path)
  p = beam.Pipeline()
  staged.create(p, "Panel[users]", [{"a": 1}], staging)
  staged.create(p, "Panel[orders]", [{"a": 2}, {"a": 3}], staging)
  assert [o.elements for o in staging.objects] == [1, 2]
  assert staging.size == sum(o.size for o in staging.objects)
  assert len(list((tmp_path / "panel").iterdir())) == 2


@pytest.mark.parametrize("spread", [False, True])
def test_a_worker_gets_the_values_back_cell_types_included(tmp_path, spread):
  rows = client_rows()  # every BigQuery type, as the client types it
  with BeamTestPipeline() as p:
    found = staged.create(p, READ, rows, _staging(tmp_path), spread=spread)
    assert_that(found | beam.Map(_described),
                equal_to([_described(row) for row in rows]))


def test_an_empty_payload_is_an_empty_collection(tmp_path):
  staging = _staging(tmp_path)
  with BeamTestPipeline() as p:
    assert_that(staged.create(p, "Refs", [], staging), equal_to([]))
  assert [o.elements for o in staging.objects] == [0]


@pytest.mark.parametrize("spread", [False, True])
@pytest.mark.parametrize(
    "values", [
        client_rows(),
        [(("users", 0), {
            "h": frozenset({1, 2})
        }), (("users", 1), None)],
        [_Payload(3, (1.5, 2.5))],
        [],
    ],
    ids=["rows", "pairs", "one-object", "empty"])
def test_the_staged_collection_has_beam_creates_element_type(
    tmp_path, values, spread):
  """What `beam.Create(values)` types its output, the staged collection
  is typed too: the coders downstream are the same ones."""
  p = beam.Pipeline()
  embedded = p | "Embedded" >> beam.Create(values)
  kept = staged.create(p, "Staged", values, _staging(tmp_path), spread=spread)
  assert kept.element_type == embedded.element_type


def test_the_graph_holds_a_reference_not_the_values(tmp_path):
  """Two thousand rows in the graph weigh as their content does; staged,
  the graph is the same size whatever the rows — to within the few tens
  of bytes two builds of one pipeline differ by (compressed pickles that
  hold unique ids)."""
  sizes = {}
  for n in (20, 2_000):
    embedded, kept = beam.Pipeline(), beam.Pipeline()
    staged.create(embedded, READ, _wide_rows(n))
    staged.create(kept, READ, _wide_rows(n), _staging(tmp_path / f"{n:05d}"))
    sizes[n] = (len(embedded.to_runner_api().SerializeToString()),
                len(kept.to_runner_api().SerializeToString()))
  assert sizes[2_000][0] > 2_000 * 64  # embedded: at least the tokens
  assert abs(sizes[2_000][1] - sizes[20][1]) < 1_024  # staged: build noise
  assert sizes[2_000][1] < sizes[2_000][0] / 10


def test_spread_reshuffles_the_loaded_elements_and_only_then(tmp_path):
  """`beam.Create` reshuffles a collection of several elements; the row
  collections keep that, so they are not one bundle with their readers."""
  for spread, wanted in ((True, True), (False, False)):
    p = beam.Pipeline()
    staged.create(
        p, READ, client_rows(), _staging(tmp_path / str(spread)), spread=spread)
    reshuffled = any(name.startswith(f"{READ}/Spread") for name in _labels(p))
    assert reshuffled is wanted
    assert f"{READ}/Load" in _labels(p)


# --------------------------------------------------------------------------
# the panel sides of every Sources
# --------------------------------------------------------------------------
def test_a_panel_side_is_staged_with_the_plans_columns_only(tmp_path):
  """`Sources.read` of a panel side, given a staging: the R and H rows
  are two objects, projected to the plan's columns as before, and a
  worker gets them back."""
  rows = [dict(r, source_only="x" * 64) for r in client_rows()]
  table = all_types_plan(panel=make_panel(rows[:2], rows[2:]))
  names = sorted(c.name for c in table.columns)
  staging = _staging(tmp_path)
  with BeamTestPipeline() as p:
    r = InMemorySources({}).read(p, table, Side.REFERENCE, staging=staging)
    h = BigQuerySources().read(p, table, "holdout", staging=staging)
    assert_that(
        r | "R" >> beam.Map(_described),
        equal_to([_described(row) for row in client_rows()[:2]]),
        label="reference")
    assert_that(
        h | "H" >> beam.Map(sorted),
        equal_to([names] * (len(rows) - 2)),
        label="holdout")
  assert [o.elements for o in staging.objects] == [2, len(rows) - 2]
  label = f"Read[{table.landing_table}/reference]"
  assert f"{label}/Spread" in " ".join(_labels(p))  # rows are reshuffled
  for written in staging.objects:
    held = pickle.loads(Path(written.uri).read_bytes())
    assert all("source_only" not in row for row in held)


def test_a_source_side_is_never_staged(tmp_path):
  staging = _staging(tmp_path)
  rows = client_rows()
  sources = InMemorySources({("order_items", Side.SOURCE): rows})
  with BeamTestPipeline() as p:
    found = sources.read(p, all_types_plan(), Side.SOURCE, staging=staging)
    assert_that(found | beam.Map(_described),
                equal_to([_described(row) for row in rows]))
  assert not staging.objects


# --------------------------------------------------------------------------
# integrity: never an empty or partial panel
# --------------------------------------------------------------------------
class _Marks:
  """Unpickling it creates the file `path`: proof that a payload was
  unpickled."""

  def __init__(self, path: Path):
    self._path = path

  def __reduce__(self):
    return (Path.touch, (self._path,))


def _written(tmp_path: Path) -> tuple[staged.StagedObject, Path]:
  staging = _staging(tmp_path)
  written = staging.write(READ, client_rows())
  return written, Path(written.uri)


def test_load_returns_the_values_of_an_untouched_object(tmp_path):
  written, _ = _written(tmp_path)
  found = staged.load(written.uri, written.sha256, written.elements)
  assert [_described(v) for v in found
         ] == [_described(v) for v in client_rows()]


def test_a_tampered_object_fails_before_anything_is_unpickled(tmp_path):
  written, path = _written(tmp_path)
  marker = tmp_path / "unpickled"
  path.write_bytes(pickle.dumps([_Marks(marker)]))
  with pytest.raises(StagedObjectError) as info:
    staged.load(written.uri, written.sha256, written.elements)
  message = str(info.value)
  assert written.uri in message and "sha256" in message
  assert written.sha256[:12] in message
  assert not marker.exists()  # the digest is checked first
  pickle.loads(path.read_bytes())  # the payload does mark when unpickled
  assert marker.exists()


@pytest.mark.parametrize(
    "payload, error", [
        (b"cnot_a_module_of_this_image\nMissing\n.", ModuleNotFoundError),
        (bytes([0x80, 5]) + b"not a pickle", pickle.UnpicklingError),
    ],
    ids=["a class the worker lacks", "bytes that are no pickle"])
def test_an_object_a_worker_cannot_unpickle_fails_naming_it(
    tmp_path, payload, error):
  """The bytes are the launch's (the digest matches) and the worker
  still cannot unpickle them, as when its image is not the launch's:
  the error names the object and keeps the original as its cause."""
  path = tmp_path / "Inputs_users-0123456789abcdef.pickle"
  path.write_bytes(payload)
  digest = hashlib.sha256(payload).hexdigest()
  with pytest.raises(StagedObjectError) as info:
    staged.load(str(path), digest, 1)
  assert str(path) in str(info.value)
  assert error.__name__ in str(info.value)
  assert isinstance(info.value.__cause__, error)


def test_a_worker_says_which_object_it_read_and_verified(tmp_path, caplog):
  """The one line a worker's log has per staged object: the playbook
  tells the owner to look for it."""
  written, _ = _written(tmp_path)
  with caplog.at_level("INFO", logger="sdfb_evaluation.beam.staged"):
    staged.load(written.uri, written.sha256, written.elements)
  assert [r.getMessage() for r in caplog.records] == [
      f"staged panel object {written.uri}: {written.elements} elements, "
      f"{written.size} bytes, verified"
  ]


def test_a_refused_object_is_not_said_to_be_verified(tmp_path, caplog):
  written, path = _written(tmp_path)
  path.write_bytes(path.read_bytes()[:-1])
  with caplog.at_level(
      "INFO",
      logger="sdfb_evaluation.beam.staged"), pytest.raises(StagedObjectError):
    staged.load(written.uri, written.sha256, written.elements)
  assert not [r for r in caplog.records if "verified" in r.getMessage()]


def test_objects_are_pickled_with_the_pinned_protocol(tmp_path):
  """Protocol 5, fixed: the worker that reads an object is not the
  interpreter that wrote it."""
  _, path = _written(tmp_path)
  assert staged.PICKLE_PROTOCOL == 5
  assert path.read_bytes()[:2] == bytes([0x80, 5])  # PROTO, its version


class _SlowToPickle:
  """A value whose pickling takes `SECONDS`: CPU time of the launch that
  is no storage time."""
  SECONDS = 0.2

  def __reduce__(self):
    time.sleep(self.SECONDS)
    return (int, (1,))


def test_the_tally_tells_the_writes_from_the_pickling(tmp_path):
  """`seconds` is everything staging did, `write_seconds` the part
  spent writing to storage: a launch can tell a slow bucket from a
  large panel."""
  staging = _staging(tmp_path)
  staging.write("Panel[users]", [_SlowToPickle()])
  assert staging.seconds >= _SlowToPickle.SECONDS
  assert 0 <= staging.write_seconds < staging.seconds - _SlowToPickle.SECONDS / 2


def test_a_missing_object_fails_naming_it(tmp_path):
  written, path = _written(tmp_path)
  path.unlink()
  with pytest.raises(StagedObjectError) as info:
    staged.load(written.uri, written.sha256, written.elements)
  assert written.uri in str(info.value)
  assert "could not be read" in str(info.value)


@pytest.mark.parametrize("off", [-1, 1])
def test_a_wrong_element_count_fails_naming_the_object(tmp_path, off):
  written, _ = _written(tmp_path)
  with pytest.raises(StagedObjectError) as info:
    staged.load(written.uri, written.sha256, written.elements + off)
  message = str(info.value)
  assert written.uri in message
  assert f"{written.elements} elements" in message
  assert str(written.elements + off) in message


def test_a_truncated_object_fails_on_its_digest(tmp_path):
  written, path = _written(tmp_path)
  path.write_bytes(path.read_bytes()[:-7])
  with pytest.raises(StagedObjectError, match="sha256"):
    staged.load(written.uri, written.sha256, written.elements)


@pytest.mark.parametrize("damage", ["tampered", "missing"])
def test_a_damaged_object_fails_the_pipeline_that_reads_it(tmp_path, damage):
  """Through the transform, on a runner: the step fails, loudly, with
  the object's name — the consumers never see an empty collection."""
  staging = _staging(tmp_path)
  p = BeamTestPipeline()
  found = staged.create(p, READ, client_rows(), staging, spread=True)
  seen = found | beam.combiners.Count.Globally()
  assert_that(seen, equal_to([len(client_rows())]))
  path = Path(staging.objects[0].uri)
  if damage == "tampered":
    path.write_bytes(pickle.dumps([]))
  else:
    path.unlink()
  with pytest.raises(StagedObjectError) as info:
    p.run().wait_until_finish()
  assert path.name in str(info.value)
  assert f"{READ}/Load" in str(info.value)  # the step that failed


# --------------------------------------------------------------------------
# the launch's own failures raise
# --------------------------------------------------------------------------
def test_a_write_that_fails_raises_naming_the_payload(tmp_path):
  blocker = tmp_path / "not-a-directory"
  blocker.write_text("a file where the root's directory should be")
  staging = Staging(str(blocker / "panel"))
  with pytest.raises(StagedObjectError) as info:
    staged.create(beam.Pipeline(), READ, client_rows(), staging)
  assert READ in str(info.value) and str(blocker) in str(info.value)
  assert not staging.objects


def test_a_value_that_cannot_be_pickled_raises_at_the_launch(tmp_path):
  staging = _staging(tmp_path)
  with pytest.raises(StagedObjectError) as info:
    staged.create(beam.Pipeline(), "Refs", [lambda: None], staging)
  assert "Refs" in str(info.value)
  assert not staging.objects
  assert not (tmp_path / "panel").exists()  # nothing half-written


# --------------------------------------------------------------------------
# where the objects go
# --------------------------------------------------------------------------
@pytest.mark.parametrize("temp",
                         ["gs://demo-bucket/tmp", "gs://demo-bucket/tmp/"])
def test_the_root_is_under_the_jobs_temp_location(temp):
  root = staged.panel_root(temp, "eval-20260914T080000Z-0badc0de")
  assert root == ("gs://demo-bucket/tmp/sdfb-evaluation/"
                  "eval-20260914T080000Z-0badc0de/panel/")


def test_the_root_of_a_local_temp_location_is_a_directory(tmp_path):
  root = staged.panel_root(str(tmp_path), "eval-1")
  assert Path(root) == tmp_path / "sdfb-evaluation" / "eval-1" / "panel"


@pytest.mark.parametrize("temp", [None, "", "   "])
def test_no_temp_location_is_refused(temp):
  with pytest.raises(ValueError, match="temp_location"):
    staged.panel_root(temp, "eval-1")
