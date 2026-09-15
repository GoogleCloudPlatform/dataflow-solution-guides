# ADR 0036 — Parent-driven fan-out generation: children are generated from their parent's landed keys

**Status:** ACCEPTED (laptop 2026-09-10; Dataflow 2026-09-13). DirectRunner + unit tests (Tasks 1–11, `e084813..3ed9dde`); on Dataflow, the five-table relational launch `2026-09-13_06_10_16-12600311608685394436` generated its children from their parents' landed keys and every table succeeded (see ADR 0037 and ADR 0038)
**Design:** [`2026-09-10-parent-driven-fanout-generation.md`](../designs/2026-09-10-parent-driven-fanout-generation.md)
**Evidence:** the two 2026-09-09 launches in [ADR 0035](0035-pk-capacity-fk-bound-members.md) — 87.9% then 56.5% `pk.duplicate` on C_TABLE at 10M rows, both from random PK draws inside a per-parent key space; laptop `packages/sdfb-tests/tests/unit/test_fanout_three_tables.py` reproduces the three-table shape (`B_TABLE → C_TABLE → A_TABLE`) on DirectRunner with every FK, PK and ratio holding by construction
**Amends:** [ADR 0030](0030-single-job-relational-generation.md) (the FK key-pool side input and its cap, for in-job driven edges only) · [ADR 0031](0031-joint-fk-key-draws.md) (the IPF child-marginal weighting, for in-job driven edges only) · [ADR 0035](0035-pk-capacity-fk-bound-members.md) (the random-draw PK-capacity check, for in-job driven edges only — its sizing and ceiling stay for root tables and external parents)
**Keeps:** ADR 0030's single-job DAG and generation waves · ADR 0031's side-input path for root/external-parent edges · ADR 0032's `config/relationships/*.yaml` as the single source of truth (this ADR adds one flag and one rule to it, no new file format) · ADR 0034's single dedup barrier (kept for roots; skipped on the landing path for streaming-mode driven children, per D6)

## Context

ADR 0035 measured why a PK that contains an FK cannot be drawn at
random: C_TABLE's PK `(D_COL_001, C_COL_002, D_COL_018)` is unique
*inside* each B_TABLE key, not across the table, and two 10M-row
launches lost 87.9% then 56.5% of rows to `pk.duplicate` proving it.
ADR 0035 D4 named the ceiling explicitly: even sampling the *whole*
10M-key parent into the side input still leaves ~4% duplicates at 10M
rows, and named the co-partitioned join as the only path past it —
coverage, not a key.

The design doc this ADR accepts works out that mechanism: a driven
child does not draw its PK-completing members at random from a
marginal at all. It draws them **without replacement, per parent key**,
from the SOURCE's own joint cell distribution — the source PK
guarantees the count of children never exceeds the count of cells, so
the draw cannot collide. The child's row count is likewise not a free
`--num_rows` parameter: it is the parent's row count times the SOURCE
fan-out ratio, measured once and cached.

Ten rulings were made during implementation (Tasks 1–11) that the
design doc predates; this ADR is their record, folded into the
decisions below (D1–D6) rather than kept as a separate list, with each
ruling's number in brackets where it lands.

## Decision

**D1 — driven children generate from parent keys, not batch requests.**
`GenerationEngine.generate_for_keys(keys, cfg)` joins `generate_batch(n,
cfg)` on the ABC (`sdfb_core/engines/base.py`); roots keep the old path
untouched. In the DAG, a child whose driving edge is in-job takes its
request stream from the parent's `valid` PCollection after the parent's
own uniqueness barrier — `_fanout_requests` projects the driving tuple
plus inherited columns, `Reshuffle`s off the parent's write path so
generation spreads across the fleet, and batches keys (not rows).
`GenerateRecordsDoFn` accepts `{"keys": [...], "batch_id": ...}` next to
`{"n": ..., "batch_id": ...}`; the engine is still built once in
`setup()`. There is no side input, no cap, and no join for a driven
edge — `_partition_parent_edges` and `_route_parent_edges`
(`sdfb_beam/pipeline.py`) split a table's `parent_edges` into at most
one `fanout`-mode edge (drives the request stream) and any number of
`side_input`-mode edges (today's ADR 0030/0031 path, for edges outside
the driving one) or `implied`-mode edges (contribute nothing — D4).
The two are mutually exclusive on ONE table: a driven child has no side
input at all, so a `side_input` edge next to the driving edge would be
routed nowhere and its columns would quietly fall back to the child's
own marginals — `_route_parent_edges` raises
`"a driven child's other in-job edges must be implied"` instead, naming
both edges' columns. An in-job edge next to a driving edge is either
`implied` or the launch stops; an edge whose parent is EXTERNAL to the
launch is unaffected (it reaches the engine as a driver-side
`fk_key_pools` payload and both engines draw it as whole tuples inside
`generate_for_keys`).
[Ruling 9] `--num_rows` is a single launch-wide flag that applies to
roots; a driven child's request stream is sized entirely from its
parent's landed keys and the measured fan-out (D5) — the design doc's
"refused" wording is superseded by "derived and printed via
`rows_detail=`" (`relational_single_job … rows_detail=<name>:<rows>,…`,
one entry per table in launch order).

Two projection details the design doc left open, settled by the
composer: [Ruling 3] `_fanout_requests` runs a `Distinct` on the
projected keys unless the projection contains a NON-EMPTY parent PK
(`edge.parent_pk`) — an undeclared parent PK proves nothing about
uniqueness, so a duplicated key in the request stream would otherwise
yield PK-IDENTICAL children (seeds are per-key, D5: the key and the
cells repeat exactly; the free columns differ, being sampled per chunk
position) colliding on the child's own PK. [Ruling 2] Each request
carries `n = max(1, round(len(keys) * mean_fanout))` alongside `keys` — `GenerateRecordsDoFn`
generates from `keys`, but the BLOCKER gate's `engine_failure` DLQ
weighting (`_dlq_rule_weight`, `sdfb_beam/pipeline.py`) reads
`raw_request["n"]` to count a crashed batch as its *expected* lost rows
(`len(keys) * mean_fanout`), not as a single envelope — without `n`, a
key-batch crash would silently under-count against the gate.

**D2 — the fan-out histogram and PK cells are measured on the SOURCE,
cached by model sha.** `sdfb_beam/io/fanout_stats.py::measure_fanout`
runs three `GROUP BY` scans against the SOURCE child table (never the
5k-row reference sample, which almost never holds two rows of one
parent): the fan-out histogram (`k → parents with k children`, zero
bucket filled from `source_parent`'s distinct key count minus the
child's), and — when the PK has categorical members outside the
driving edge — their joint cell distribution with source row counts as
weights. `BigQueryFanoutStatsStore` caches the payload in
`synthetic_data_quality.fk_fanout_stats` keyed by `(source_table,
edge_cols, model_sha)`, a plain `LOAD` (`WRITE_APPEND`) table with no
partition — a fan-out measurement is a point lookup by those three
keys, not a time series. `--fk_fanout_stats_table` (empty by default)
is the table's FQN; empty means "measure every launch, no cache", which
is correct for a laptop DirectRunner run against fakes and wrong for a
repeated M4 launch against the same source. [Ruling 4] `RelationshipRegistry
.sha12()` hashes every field of the model including `drives`, so flipping
which edge drives a table invalidates the cache — a stale fan-out
payload measured against the OLD driving edge would silently mis-key
the child. [Ruling 6] `resolve_fanout` (`sdfb_beam/cli/run_pipeline.py`)
validates the driving edge's columns against the table schema (P2)
BEFORE issuing any BigQuery call — a typo'd column in the model file
stops the launch as a schema error, not a confusing BigQuery 400 three
queries later — and wraps a `measure_fanout` exception into `[preflight]
{landing_table}: fan-out measurement on {source_table} failed: …`
rather than letting it escape as a raw traceback, so the relational
runner's collect-then-fail loop (every table's preflight runs before
any table's GPU spend) reports it alongside every other table's stop.
One `fk_fanout_measured edge=
parents= children= mean= p50= p95= max= zero_share= source=measured|cache`
milestone per driving edge, launcher-side, logged whichever way the
payload was obtained.

**D3 — PK-completing cells are drawn without replacement, per key;
`k > cells` is a preflight stop, never a runtime collision.**
`sdfb_core/engines/fanout.py::CellTable.draw` takes `k` (this key's
child count, sampled from the histogram) and returns `k` distinct cells
when `exact=True` (every PK member outside the driving edge is a cell
column — `pk_cell_columns`'s `exact` flag) or `k` weighted-with-replacement
draws otherwise (an unbounded member — pattern, numeric, temporal —
completes the PK, so cells only need to follow weights, not be
collision-free by themselves). Cost is O(k) when `k` is small relative
to the cell count `C` (rejection sampling, expected O(k) draws,
`_REJECTION_MAX_FILL = 0.5`); above that fill it switches to a full
weighted permutation (Efraimidis & Spirakis 2006,
https://doi.org/10.1016/j.ipl.2005.11.003 — `u^(1/w)` order statistics,
O(C log C) here via a full sort rather than the paper's O(C log k)
partial selection, traded for simplicity at the cell-table sizes this
design expects). `k > C` under `exact=True` raises inside the engine
(`"the declared PK is not a key"`) — but preflight's `_check_driven_pk`
(`sdfb_beam/cli/preflight.py`) checks the SAME condition (`max_k >
n_cells`) against the measured histogram and cell table before any
launch, as `[preflight P4]`, so the engine's raise is a defect signal
(the source changed between measurement and generation), never the
first time an operator learns the PK does not hold.

**D4 — widened edges carry inherited columns; exactly one driving
edge per child, others implied or a launch stop.** `FkEdge.drives:
bool = False` (`sdfb_core/contracts/relationships.py`) marks the edge
whose parent's keys a child is generated from; `edge_roles` computes
`driving` / `implied` / `external` for every enforced edge of a table:
a lone enforced in-model edge drives by itself; several edges need
exactly one marked `drives: true`; every other internal edge must be
**implied** — its columns are a subset of the driving edge's columns,
and the driving parent carries those columns from that other parent
through its own enforced edge, transitively (`_carries`). Anything
that is neither driving nor implied is a `RelationshipError` at
preflight, naming the exact edit (widen the driving parent's edge, or
mark the ambiguous edge `drives: true`) — today (pre-ADR-0036) the
engine writes each edge's FK columns in turn and the last one silently
wins, corrupting every earlier edge's referential integrity without a
signal. [Ruling 4, second half] `_carries` keys its visited set by
`(table, frozenset(cols))`, not by `table` alone — the same table
reached through two different column projections (a diamond: A_TABLE
implied to B_TABLE both directly and through C_TABLE) is two different
questions, and collapsing them would give the second arrival a stale
"already visited" answer. One `fk_edge_role edge=(cols)->ref
role=driving|implied|external` milestone per enforced edge, launcher
and worker-preflight side, naming which edge generates and which are
satisfied by construction. [Ruling 8] The worker does NOT additionally
emit a `relational_fk_edge mode=fanout` milestone for a driven edge —
`fk_edge_role` (launcher) and the worker's `fanout_bound` (D3/D6) carry
the same fact between them, so a third line would be redundant, not
additive.

**D5 — `--num_rows` applies to roots; children derive, chained through
grandchildren; per-chunk seeding stops rows replaying.**
`resolve_table_rows` (`sdfb_beam/cli/run_pipeline.py`) returns
`launch_rows` for an undriven table and `derived_rows` for a driven
one; `_driven_child_rows` (`sdfb_beam/cli/preflight.py`) computes
`derived_rows = round(parent_rows * mean_fanout)` from the driving
parent's row count and the measured histogram's mean (including the
zero bucket). [Ruling 7] `fk_parent_rows` — the mapping preflight reads
`parent_rows` from — is built from `rows_by_landing`
(`_load_reference_and_preflight`), which `_run_relational_job` fills in
plan order as each table's *own* (possibly derived) `num_rows` lands on
its `TableSpec`; a grandchild therefore reads its immediate parent's
DERIVED count, never the launch-wide `--num_rows`. [Ruling 5] A driven
child whose count cannot be derived (`derived_rows` is `None` or `0` —
the histogram carries no mass, or the driving parent's row count is
unknown) is a loud preflight stop (`[preflight P4] … this table is
generated from its parent's keys but its row count could not be
derived …`), never a silent fall-through to `--num_rows`, which would
size the child by a number that has nothing to do with the source
ratio.

Seeding: the run's `derive_key_seed(run_id, key)`
(`sdfb_core/seeding.py`) seeds one key's histogram/cell draw, so a
retried bundle and a re-run of the same `run_id` reproduce the same
children for the same key. [Ruling 1, engine side] Inside one key's
batch, B.1's `generate_for_keys` re-derives `cfg.seed` per CHUNK —
`derive_batch_seed(f"{run_id}:{cfg.seed}", chunk_index)`
(`sdfb_core/engines/b1_rag/engine.py`) — because the non-key (free)
columns are sampled by the engine's ordinary vectorized path per chunk,
and without a per-chunk re-seed a hot key spanning several chunks would
replay the same free-column draw in every chunk (`GenerationConfig`'s
`seed` is otherwise batch-scoped, not chunk-scoped). B.2 consumes one
`Random` instance across all chunks of a `generate_for_keys` call
instead (`sdfb_core/engines/b2_library/engine.py`) — its backends draw
sequentially off one stream rather than vectorizing per chunk, so a
single rng is both correct and simpler. Both are reproducible per
`run_id`; neither replays a batch.

**D6 — driven children default to `streaming` uniqueness; the in-DAG
FK gate is skipped for driven edges; the playbook orphan query is the
independent check.** A PK completed by the fan-out draw is unique by
construction (D3), so the ADR 0034 full-row barrier buys nothing on
the landing path for it — duplicates are still MEASURED (the digest
branches, WS6 W3) and fold into the BLOCKER gate, but nothing blocks
landing. Streaming measures on TWO branches, not one: the whole-row
digest (`row.duplicate`) always, and the PK tuple's digest
(`pk.duplicate` — `_pk_digest` → `StreamingPkDigest` /
`StreamingPkCount`, `sdfb_beam/dofns/uniqueness.py`) whenever
`pk_columns` is declared. Measuring only the row digest would have left
the driven default blind to exactly the claim this ADR makes: a PK
regression lands duplicate PKs whose free columns differ, and the run
reads PASSED. A byte-identical row is counted under BOTH rules, so the
pair is an UPPER BOUND on distinct defective rows — the safe direction
for a gate, and the reason no reconciliation between the branches is
attempted. `resolve_driven_uniqueness_mode` (`sdfb_beam/cli/run_pipeline.py`)
returns `--driven_uniqueness_mode` (default `streaming`) for a driven
table UNLESS it declares `identity` columns, in which case it keeps
`exact` — an identity column is freshly generated per row (not derived
from the parent key), so the PK is not unique by construction the way
a pure fan-out key is, and the barrier is still buying something. Roots
keep `--uniqueness_mode` (default `exact`, ADR 0034) regardless. The
in-DAG FK gate (the side-input parent-key check) is skipped for a
`fanout`-mode edge — it needs the parent key set as a side input, which
this design removes entirely — so integrity is by construction and the
RUN_PLAYBOOK's post-run orphan query is the independent measured check,
exactly as `fk.orphan` already is for side-input edges (ADR 0031).
`fanout_bound driving_cols= cells= exact_cells= mean_fanout=`
(worker, once per engine build) and `batch_start batch_id= keys=` /
`batch_done batch_id= keys= rows= seconds=` (worker, per key batch) are
the per-run confirmation that the recipe the engine bound matches what
preflight measured.

Two engine-side guards closed a correctness gap D3/D4 opened:
[Ruling 10] B.2 gained a zero-category guard in
`backends._sample_categorical` — the driving-column placeholder profile
`_bind_fanout` installs (`categories={}`, matching B.1's own empty-domain
override) has no categories to sample from, and without the guard the
backend would raise on the very columns D4 overrides afterward — and
`_assemble_row(passthrough=...)`, so `enforce_value` cannot overwrite a
parent key or an inherited column after `generate_for_keys` has already
set it from the tuple.

**Rev 2 (2026-09-10 evening) — D4 amended after launch `…-8177138577202163642` stopped A_TABLE for two unmarked edges.** Toggling `enabled` must be enough: with no `drives:`, the most-derived candidate parent (the one that descends from every other candidate) drives, and the driving parent's edge to the other parent is widened with the column pairs the child pins (`RelationshipRegistry.derived_widenings`, logged as `fk_edge_widened`). `drives: true` remains the override for parents with no ancestry between them. Mermaid is no longer written to any log (launcher card is pipes and arrows; `card.py --mermaid` renders the diagram).

**Rev 3 (2026-09-11) — D1 and D4 superseded for multi-parent children by [ADR 0037](0037-multi-parent-children.md).** D1's mutual exclusion ("a driven child has no side input at all", so a `side_input` edge next to the driving edge raises `"a driven child's other in-job edges must be implied"`) and D4's stop for an edge that is "neither driving nor implied" both came from one correct fear — an edge whose columns overlap the driving edge would be silently overwritten. ADR 0037 separates the two cases instead of refusing them: an edge that shares NO column with the driving edge is `independent` and rides the ADR 0030/0031 side-input pool next to the driving edge (disjoint by construction); an edge that DOES share columns is `conditional` and is served by a co-partitioned `CoGroupByKey` on the shared columns (ADR 0037 D3), so the shared value comes from the driving key and the rest is a candidate that exists in the parent FOR that value. D4's rule for `implied` edges, the driving edge's own guarantees, and D2/D3/D5/D6 are untouched; the last stop in `edge_roles` is now "more than one edge marked `drives: true`" (ADR 0037 D2 rule 5), and an unmarked child with no ancestry between its parents takes its FIRST DECLARED edge as driving with a `fk_driving_edge_defaulted` WARNING rather than stopping.

## Alternatives considered

- **Co-partitioned join** (ADR 0031 §6, and design doc §10). Coverage,
  not a key: a join still assigns PK-completing values by uniform
  sampling inside each partition, so the same balls-into-bins collision
  ADR 0035 measured recurs (~4% at the whole parent, more under skew);
  it also re-adds a child-sized shuffle that ADR 0034's single-barrier
  work specifically removed. Kept as the documented escape hatch for a
  shape this design cannot key (D3's `k > cells` stop names it).
- **Hybrid** (fan-out for PK-bearing edges, join for the rest). Two
  mechanisms solving one problem; rejected for the same reason ADR 0034
  rejected keeping both the chained and single-barrier uniqueness
  paths as defaults — one mechanism, well-tested, beats two half-tested
  ones.
- **Sample-based fan-out** (measure the histogram and cells from the
  5k-row reference sample instead of the source). Rejected in the
  design doc (§10) and confirmed here: a 5k-row sample of a 10M-row
  child almost never holds two rows of one parent, so it reads `k = 1`
  everywhere — the fan-out ratio and the rare cells a per-key draw must
  reach are exactly what a small sample cannot see.

## Consequences

- ADR 0031's side-input path and ADR 0035's sizing/ceiling remain live
  for root tables and edges whose parent is external to the launch —
  this ADR narrows their scope to "not a driven in-job edge," it does
  not delete them (`_route_parent_edges` still builds `_side_input_pools`
  for `side_input`-mode edges).
- `RelationshipRegistry.sha12()` now covers `drives`, so EVERY model's
  sha changes once when this merges: the first launch after it re-measures
  each driving edge (`source=measured` rather than `source=cache`) and
  appends a fresh `fk_fanout_stats` row. One extra scan per edge, once —
  not a regression.
- New OPTIONAL BigQuery table: `synthetic_data_quality.fk_fanout_stats`
  — a cache, never a prerequisite: a store that cannot be read or
  written logs `fk_fanout_cache_unavailable` (WARNING) and the launch
  measures without it (2026-09-10 operator decision).
  (`config/bq_schema/synthetic_data_quality/fk_fanout_stats.schema.json`
  — `source_table`, `edge_cols`, `model_sha`, `measured_at`, `payload`;
  no partition, appended by `LOAD`). Provisioned the same way as `dlq`
  and `validation_runs` (DEPLOYMENT_PREREQUISITES.md).
- The real (production-shaped) model needs one edit before the M4 launch: widen C_TABLE's
  edge to B_TABLE to carry the inherited columns A_TABLE needs
  (`D_COL_024, D_COL_025, C_COL_009` alongside `D_COL_001`), and mark
  A_TABLE's edge to C_TABLE `drives: true` (A_TABLE also has an
  enforced edge to B_TABLE, which then resolves `implied`). Preflight
  prints the derived row counts before anything runs
  (`relational_single_job rows_detail=`).
- A driven child in `streaming` mode drops the ADR 0034 barrier from
  its landing path — at 100M rows the design doc (§11) identifies the
  barrier as the dominant stage for a root, so this is the largest
  single cost this ADR removes, unmeasured on Dataflow until the M4
  run.
- Two new CLI flags: `--fk_fanout_stats_table` (empty = measure every
  launch) and `--driven_uniqueness_mode` (default `streaming`).

## Acceptance

- [x] Unit: `packages/sdfb-tests/tests/unit/engines/test_fanout.py`
  (`TestFanoutHistogram`, `TestCellTable`, `TestExpandKeys`,
  `test_key_seed_is_stable_and_key_sensitive`) — histogram sampling
  honours the zero bucket, without-replacement draws never repeat a
  cell and match weights in aggregate, chunked expansion carries
  per-key state across chunk boundaries, the same key reproduces the
  same children under the same seed, `k > cells` raises.
- [x] Unit: `packages/sdfb-tests/tests/unit/contracts/test_relationship_models.py::TestEdgeRoles`
  (`test_single_edge_drives_by_itself`,
  `test_marked_edge_drives_and_the_other_is_implied`,
  `test_two_unmarked_edges_stop_with_the_edit`,
  `test_an_edge_the_driving_parent_does_not_carry_stops`,
  `test_diamond_ancestry_is_implied_through_the_reaching_branch`) — the
  driving/implied/external derivation and the diamond-ancestry fix
  (Ruling 4).
- [x] Unit: `packages/sdfb-tests/tests/unit/cli/test_preflight_fanout.py`
  (`test_fanout_within_cells_passes_and_derives_rows`,
  `test_fanout_beyond_cells_stops`,
  `test_an_unbounded_member_makes_the_check_inexact_and_passes`) — the
  per-key PK check and row derivation.
- [x] Unit: `packages/sdfb-tests/tests/unit/cli/test_fanout_launch_wiring.py`
  (`test_resolve_fanout_measures_the_driving_edge_and_caches`,
  `test_driven_uniqueness_mode`,
  `test_driven_child_without_derived_rows_stops`,
  `test_resolve_fanout_stops_on_unknown_driving_columns`,
  `test_resolve_fanout_reports_a_measurement_failure_as_a_preflight_stop`)
  — P2-before-BigQuery column validation, the loud stop on an
  underived row count (Ruling 5), measurement failures surfacing as
  `[preflight]` stops (Ruling 6).
- [x] Unit: `packages/sdfb-tests/tests/unit/io/test_fanout_stats.py` —
  `measure_fanout`'s three scans and the zero bucket; the cache store's
  round trip via a LOAD job (never streaming).
- [x] DirectRunner: `packages/sdfb-tests/tests/unit/test_fanout_three_tables.py::test_three_tables_by_construction`
  — the full `B_TABLE → C_TABLE → A_TABLE` shape: every child FK tuple
  exists in its parent, C_TABLE's PK is unique, landed counts follow
  the declared histograms, A_TABLE's driving edge to C_TABLE AND its
  implied edge to B_TABLE both hold, nothing diverts to C_TABLE's DLQ.
- [ ] M4: `B_TABLE → C_TABLE → A_TABLE` at 10M B_TABLE rows on
  Dataflow, per design doc §8.5 — C_TABLE and A_TABLE derive their row
  counts, `validation_runs` reads PASSED with `pk.duplicate` reading 0
  on both driven tables — measurable in `streaming` mode since the PK
  digest branch (D6), so a zero reading is now evidence rather than the
  absence of a measurement — the RUN_PLAYBOOK orphan query returns 0
  on both driven edges, the playbook's independent post-run PK
  `GROUP BY … HAVING COUNT(*) > 1` query returns 0, `stats_diff_<child>.md` keeps the FK columns
  inside the ADR 0025 band, and `fk_fanout_measured`/`fk_edge_role`/
  `rows_detail`/`fanout_bound` read as this ADR predicts.
