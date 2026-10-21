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
"""Driver-built panel data on its way to the workers: in the job graph,
or as an object beside it (Ruling R154).

The reference panel is read by the driver, and four transforms hand
what the driver built from it to the workers: the R and H rows
(`io.Sources.read`), the census's column views (`census.CensusMetrics`),
a table's panel hashes (`membership.Membership`) and a table's privacy
inputs (`privacy.Privacy`). `beam.Create` embeds its elements in the job
graph, so the graph grew with tables x panel rows — 44 MB for five
invented tables at 10,000 reference rows, measured on a laptop — and a
launcher has to serialise that graph, more than once, inside a fixed
number of minutes.

    no staging (a local run, a fixture, every test that passes none)
      driver ── beam.Create(values) ──────────────► the job graph

    a staging root (the driver sets one for a Dataflow run)
      driver ── pickle ─► sha256 ─► <root>/<label>-<sha256[:16]>.pickle
        └────── beam.Create([(uri, sha256, count)]) ─► the job graph:
                                                       a reference, bytes
      worker ── open uri ─► sha256 == the graph's? ─► unpickle
                ─► count == the graph's? ─► the same elements as before
                [─► Reshuffle, for a collection of rows]

`create` is the one door: without a `Staging` it IS `beam.Create(values)`
under the same label. With one, the values are written once, while the
graph is built, and the collection a consumer reads has the element type
`beam.Create(values)` would have given it, so the coders downstream are
the same.

Integrity. A worker never unpickles bytes other than the ones whose
digest the job graph carries: the digest is checked against the object's
bytes BEFORE they are unpickled (unpickling runs code, and the object
sits in a bucket), and the element count after. A missing object,
another digest, another count or bytes this worker cannot unpickle raise
`StagedObjectError` naming the object: the step fails, the runner
retries it and then fails the job. Never an empty or a partial panel.

What that is worth: it stops an accident (a truncated, overwritten or
missing object) and a party who can write under the temp location but
cannot replace the graph. It keeps nothing secret (the objects are the
rows, in clear), and it does not stop whoever can replace the graph: the
digest travels in it. The graph is already pickled code the workers run,
so that is no new exposure.

The root is `<temp_location>/sdfb-evaluation/<evaluation_id>/panel/`
(`panel_root`): the job's own temp location, which its workers already
read and write. The objects are not deleted when the job ends; they
follow the temp location's lifecycle, as the staged job graph does.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import re
import time
from collections.abc import Iterator, Sequence
from typing import Any, NamedTuple

import apache_beam as beam
from apache_beam.io.filesystem import CompressionTypes
from apache_beam.io.filesystems import FileSystems

__all__ = [
    "PICKLE_PROTOCOL",
    "StagedObject",
    "StagedObjectError",
    "Staging",
    "create",
    "load",
    "panel_root",
]

# Fixed, not `pickle.HIGHEST_PROTOCOL`: the worker that reads an object
# is not the interpreter that wrote it.
PICKLE_PROTOCOL = 5
_SUFFIX = ".pickle"
_ROOT_PARTS = ("sdfb-evaluation", "panel")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_NAME_CHARS = 150  # of the label, in an object's name
_DIGEST_CHARS = 16  # of the sha256, in an object's name
_SHOWN_DIGEST = 12  # of a sha256, in a message
_LOGGER = logging.getLogger(__name__)


class StagedObjectError(RuntimeError):
  """A staged object could not be written (the launch) or is not what
  the launch wrote (a worker): the message names the object. Never a
  table's own data error — it is not among the errors a transform turns
  into a not_evaluated table, so it fails the launch or the job."""


class StagedObject(NamedTuple):
  """One staged payload: where it is, the sha256 of its bytes, its
  number of elements and its size in bytes. The first three are the
  reference the job graph carries."""
  uri: str
  sha256: str
  elements: int
  size: int


def panel_root(temp_location: str | None, evaluation_id: str) -> str:
  """`<temp_location>/sdfb-evaluation/<evaluation_id>/panel/`: where one
  evaluation's panel objects go (module docstring).

  `temp_location` is the one Beam settled on, from the pipeline's own
  options (`pipeline.options`): `beam.Pipeline` deep-copies the options
  it is given and, validating a Dataflow service pipeline, fills a
  missing or invalid temp location in its copy only (the staging
  location, or a default bucket).

  Raises:
    ValueError: no temp location. Beam settles one on every pipeline it
      validates as a Dataflow service pipeline, so there is none only
      where it did not: a `--dataflow_endpoint` on localhost, or a
      runner of another class whose name says Dataflow.
  """
  if not temp_location or not temp_location.strip():
    raise ValueError(
        "the panel is staged under the job's temp_location and the pipeline "
        "options carry none: pass --temp_location")
  product, leaf = _ROOT_PARTS
  root: str = FileSystems.join(temp_location.strip(), product, evaluation_id,
                               leaf, "")
  return root


def _object_name(label: str, digest: str) -> str:
  """`<label, path-safe>-<sha256[:16]>.pickle`: readable in a bucket
  listing, and two payloads never share a name unless they are the same
  payload under the same label."""
  slug = _UNSAFE_NAME_CHARS.sub("_", label).strip("_")[:_NAME_CHARS]
  slug = slug or "payload"  # not inside the f-string: its quotes (W1405)
  return f"{slug}-{digest[:_DIGEST_CHARS]}{_SUFFIX}"


class Staging:
  """Where one launch stages its panel payloads (`root`) and what it
  has staged so far (the driver's log line): `objects` in the order
  they were written, their total `size` in bytes, the `seconds` staging
  took in all (pickling, hashing and writing) and, of those, the
  `write_seconds` spent writing to storage — a launch can tell a slow
  bucket from a large panel. Driver-side only; it never enters the
  graph."""

  def __init__(self, root: str):
    self.root = root
    self.objects: list[StagedObject] = []
    self.seconds = 0.0
    self.write_seconds = 0.0

  @property
  def size(self) -> int:
    """The bytes of every object written."""
    return sum(written.size for written in self.objects)

  def write(self, label: str, values: Sequence[Any]) -> StagedObject:
    """Write `values` as ONE object under the root and return its
    reference. The object is the pickle of `list(values)`.

    Raises:
      StagedObjectError: the values cannot be pickled, or the object
        cannot be written (the message names the label and the object).
    """
    began = time.monotonic()
    try:
      payload = pickle.dumps(list(values), protocol=PICKLE_PROTOCOL)
    except Exception as exc:
      raise StagedObjectError(
          f"the panel data of {label} could not be pickled for staging under "
          f"{self.root} ({type(exc).__name__}: {exc})") from exc
    digest = hashlib.sha256(payload).hexdigest()
    uri: str = FileSystems.join(self.root, _object_name(label, digest))
    writing = time.monotonic()
    try:
      with FileSystems.create(
          uri, compression_type=CompressionTypes.UNCOMPRESSED) as handle:
        handle.write(payload)
    except Exception as exc:
      raise StagedObjectError(
          f"the panel data of {label} could not be written to {uri} "
          f"({type(exc).__name__}: {exc}): the launch stages it under the "
          "job's temp location, where it needs to create objects") from exc
    ended = time.monotonic()
    written = StagedObject(uri, digest, len(values), len(payload))
    self.objects.append(written)
    self.write_seconds += ended - writing
    self.seconds += ended - began
    return written


def load(uri: str, sha256: str, count: int) -> list[Any]:
  """The values of the object at `uri`, as a worker reads them (module
  docstring, Integrity): its bytes must hash to `sha256`, checked before
  anything is unpickled, and hold `count` elements.

  Raises:
    StagedObjectError: the object cannot be read, its digest is not
      `sha256`, it cannot be unpickled here, or it does not hold `count`
      elements; the message names the object.
  """
  try:
    with FileSystems.open(
        uri, compression_type=CompressionTypes.UNCOMPRESSED) as handle:
      payload = handle.read()
  except Exception as exc:
    raise StagedObjectError(
        f"the staged panel object {uri} could not be read "
        f"({type(exc).__name__}: {exc}): the launch wrote it under the job's "
        "temp location, and the job needs it for as long as it runs") from exc
  found = hashlib.sha256(payload).hexdigest()
  if found != sha256:
    raise StagedObjectError(
        f"the staged panel object {uri} is not the object the launch wrote: "
        f"its {len(payload)} bytes have sha256 {found[:_SHOWN_DIGEST]}… and "
        f"the job graph records {sha256[:_SHOWN_DIGEST]}…. It was not "
        "unpickled")
  try:
    values = pickle.loads(payload)
  except Exception as exc:
    raise StagedObjectError(
        f"the staged panel object {uri} is the object the launch wrote (its "
        f"sha256 matches) and this worker cannot unpickle it "
        f"({type(exc).__name__}: {exc}): most likely the worker's image is "
        "not the launch's, and a class or a library differs") from exc
  if not isinstance(values, list) or len(values) != count:
    shown = f"{len(values)} elements" if isinstance(values, list) else "no list"
    raise StagedObjectError(
        f"the staged panel object {uri} holds {shown} and the job graph "
        f"records {count}")
  _LOGGER.info("staged panel object %s: %d elements, %d bytes, verified", uri,
               count, len(payload))
  return values


class _LoadFn(beam.DoFn):
  """`(uri, sha256, count)` → the object's elements (`load`)."""

  def process(self, element: tuple[str, str, int]) -> Iterator[Any]:
    uri, sha256, count = element
    yield from load(uri, sha256, count)


class _FromObject(beam.PTransform):
  """The elements of one staged object: the reference as the graph's
  only constant, read by a worker, typed as `beam.Create` types the
  values, and reshuffled when `spread`."""

  def __init__(self, written: StagedObject, element_type: Any, spread: bool):
    super().__init__()
    self._reference = (written.uri, written.sha256, written.elements)
    self._element_type = element_type
    self._spread = spread

  def expand(self, input_or_inputs: Any) -> beam.PCollection:
    out: beam.PCollection = (
        input_or_inputs
        | "Object" >> beam.Create([self._reference])
        | "Load" >> beam.ParDo(_LoadFn()).with_output_types(self._element_type))
    if self._spread:
      out = out | "Spread" >> beam.Reshuffle()
    return out


def _element_type(values: Sequence[Any]) -> Any:
  """The element type `beam.Create(values)` gives its output: Beam's own
  inference, run once. (`Create(values)` infers it when it is made and
  again when it is applied; over a panel's rows that inference is a
  measurable part of building the graph, so the transform is made empty
  and asked once.)"""
  probe = beam.Create(())
  probe.values = tuple(values)
  return probe.get_output_type()


def create(p: Any,
           label: str,
           values: Sequence[Any],
           staging: Staging | None = None,
           *,
           spread: bool = False) -> beam.PCollection:
  """`p | label >> beam.Create(values)` — or, with a `staging`, the same
  collection read by a worker from ONE object the driver writes now
  (module docstring).

  `spread` reshuffles the staged elements, as `beam.Create` reshuffles a
  collection of several: for the row collections, so a table's panel
  rows are not one bundle fused with the steps that read them. Without a
  `staging` it has no effect (`beam.Create` decides for itself).

  Raises:
    StagedObjectError: the object could not be written (`Staging.write`).
  """
  if staging is None:
    out: beam.PCollection = p | label >> beam.Create(values)
    return out
  held = list(values)
  written = staging.write(label, held)
  kept: beam.PCollection = p | label >> _FromObject(written,
                                                    _element_type(held), spread)
  return kept
