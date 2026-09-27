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
"""Worker-side model localization shared by every engine-building DoFn.

The engine's embedder loads from a **local directory** only, so a gs://
``embedder_uri`` must be warm-pulled to worker-local disk and the ctx
rewritten to the local path BEFORE the engine builds its embedder. This
lived inline in ``GenerateRecordsDoFn._setup_inner`` — and the 2026-07-28
R1 run showed exactly what that DRY failure costs: the new
``BuildFreeTextPoolsDoFn`` (WS5) skipped the localization, ``BgeEmbedder``
handed the raw gs:// URI to ``AutoTokenizer.from_pretrained``, transformers
treated it as a HuggingFace repo id, and the job died on
``HFValidationError`` after 4 bundle retries. One definition, every caller.

The LLM weights need no equivalent here — the ``ModelClient`` pulls those
itself in its own ``setup()``.
"""

from __future__ import annotations

import time

from sdfb_core.observability import log_milestone

from sdfb_beam.gcs import localize_gcs_prefix

# Dataflow GPU workers mount fast local SSD here (gpu-dockerfile recipe).
EMBEDDER_LOCAL_DIR = "/local-ssd/embedder"


def localize_embedder(ctx):
  """Return a ctx whose ``embedder_uri`` is a worker-local path.

    A gs:// URI is warm-pulled to ``EMBEDDER_LOCAL_DIR`` (idempotent —
    ``localize_gcs_prefix`` marker-skips a completed pull) and the ctx is
    rewritten via ``model_copy``. Anything else (local path, empty string ⇒
    HashingEmbedder default) passes through unchanged.
    """
  if not ctx.embedder_uri.startswith("gs://"):
    return ctx
  log_milestone("embedder_pull_start", uri=ctx.embedder_uri)
  t_pull = time.monotonic()
  local_dir = localize_gcs_prefix(ctx.embedder_uri, EMBEDDER_LOCAL_DIR)
  log_milestone(
      "embedder_pull_done",
      seconds=round(time.monotonic() - t_pull, 1),
  )
  return ctx.model_copy(update={"embedder_uri": local_dir})


__all__ = ["EMBEDDER_LOCAL_DIR", "localize_embedder"]
