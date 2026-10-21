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
"""Small, standalone mirrors of the generator's relationship model and
reference-sample logic.

`sdfb_evaluation` cannot import `sdfb_core`/`sdfb_beam` (it ships and is
tested independently of the generator), so it cannot call the originals
directly. `context.relationships` and `context.reference` reimplement the
slice of their behaviour this package needs, pinned against the originals
by a two-sided golden-file parity test rather than by import — see
`packages/sdfb-evaluation/tests/unit/test_parity_goldens.py` and
`packages/sdfb-tests/tests/unit/evaluation_parity/test_goldens.py`.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations
