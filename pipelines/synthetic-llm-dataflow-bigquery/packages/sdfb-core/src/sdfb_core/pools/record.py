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
"""One column's generated free-text pool, as persisted (WS5 §2)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreeTextPool:
  """A pool identified by (reference_digest, model_uri, column, target).

    Keyed on ``model_uri`` — NOT an embedder identity — because these values
    were produced by that LLM; a different model yields a different pool for
    the same reference rows.

    ``stagnated`` / ``attempts`` record what the ladder LEARNED, so a later
    worker never re-derives it. The 2026-07-26 1M run spent 68,805 s of LLM
    service time re-discovering the same three pools 36 times, most of it
    re-proving that a column stagnates below its target.
    """

  reference_digest: str
  model_uri: str
  column: str
  target: int
  values: tuple[str, ...]
  stagnated: bool = False
  attempts: int = 0


__all__ = ["FreeTextPool"]
