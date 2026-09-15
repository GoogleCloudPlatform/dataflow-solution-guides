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
