# ADR 0037 — Multi-parent children: every declared FK edge gets a role and a DAG path

**Status:** ACCEPTED (2026-09-14) — laptop acceptance green on `ws12-fanout-generation` (registry shape sweep + the DirectRunner shapes in `test_fanout_shapes.py`), and Dataflow acceptance met by the five-table relational launch `2026-09-13_06_10_16-12600311608685394436`, in which every table succeeded and the driver resolved a child's two edges as `driving` + `implied` with the driving parent's edge widened to carry the inherited columns.
**Design:** [`2026-09-11-multi-parent-children.md`](../designs/2026-09-11-multi-parent-children.md) — the argument, the mechanism in detail, and the scale analysis. This ADR records the decision only.
**Evidence:** the three 2026-09-11 preflight stops on the five-table expansion of the anonymised production-shaped model (`ef66717`, `ca8f948`, `8573665`) and the registry shape sweep written afterwards (`packages/sdfb-tests/tests/unit/contracts/test_relationship_shapes.py`)
**Amends:** [ADR 0036](0036-parent-driven-fanout-generation.md) — D1's "a driven child has no side input at all" and D4's "anything that is neither driving nor implied is a `RelationshipError`" (see ADR 0036 **Rev 3**)
**Keeps:** ADR 0030's single-job DAG and generation waves · ADR 0031's side-input joint key pools and the `fk.orphan` gate (they ARE the independent path) · ADR 0032's `config/relationships/*.yaml` as the single source of truth (no new file format, no new YAML key) · ADR 0035's PK-capacity preflight (P4 gains two factors, not a new check) · ADR 0036's driving-edge guarantees, fan-out histogram, cell draw and seeding, unchanged

## Context

ADR 0036 gave a driven child exactly ONE parent: the driving edge. Every
other in-job enforced edge had to be **implied** — a subset of the
driving edge's columns, carried by the driving parent from that other
parent — or the launch stopped, because before ADR 0036 the engine wrote
each edge's FK columns in turn and the last one silently won.

That was the right stop for the shapes ADR 0036 saw. It is the wrong
stop for the shapes an operator actually declares. On 2026-09-11 the
five-table expansion of the anonymised production-shaped model (`A_TABLE`,
`C_TABLE`, `E_TABLE`, `F_TABLE` enabled; hub `B_TABLE` and `D_TABLE`
detached) stopped at preflight three times in one evening, each on a
shape ADR 0036 never saw:

- a root whose FK member points at a **disabled** parent — P4 counted a
  capacity factor for an edge the launch never draws (fixed `ef66717`);
- a pattern member whose space is ~3.6e27 strings — the duplicate-share
  formula cancelled to a false 100% stop (fixed `ca8f948`, ADR 0035);
- a true **1:1 child** whose PK *is* its driving edge — no cell table to
  check, so max fan-out 1 must pass and anything above 1 is a PK fault
  (fixed `8573665`).

The shape sweep written after those fixes
(`test_relationship_shapes.py`) pins what the registry resolves — hub
with many children, chain, tree, forest, 1:1 chains, child → parent and
grandparent — and names the two it cannot:

| shape | registry before this ADR | why |
|---|---|---|
| **star-schema fact**: child → `dim_a`, child → `dim_b`, no ancestry between them | stops | the second edge is neither driving nor implied |
| **true diamond**: `left`/`right` both under `top`, `bottom` → both | stops | the second edge shares `T` with the driving edge and adds `R` |

Both are ordinary warehouse shapes. Refusing them means the operator
must either detach a real parent or hand-mark an edge that has no
correct mark. The two edges do not need a stop — they need a role and a
mechanism.

## Decision

**D1 — five roles, assigned by the registry, one per enforced edge.**
`RelationshipRegistry.edge_roles(table)`
(`sdfb_core/contracts/relationships.py`) returns one of
`driving | implied | independent | conditional | external` for every
enforced edge of a table. `driving` and `implied` keep exactly the
meaning ADR 0036 D4 gave them; `external` (parent outside the launch) is
unchanged. The two new roles are decided by **column overlap with the
driving edge**, by child column name:

| role | when | DAG path | integrity |
|---|---|---|---|
| `independent` | no column shared with the driving edge (a star-schema dimension) | the ADR 0030/0031 sampled key pool as a side input, unchanged | by construction from the pool; the `fk.orphan` gate measures it |
| `conditional` | at least one column shared with the driving edge, and possibly more (a diamond branch) | a co-partitioned `CoGroupByKey` on the shared columns, before batching | by construction; unmatched keys follow D5 |

`edge_overlap(table, edge)` is the shared child columns in `edge.cols`
order; `edge_rest(table, edge)` is the remainder. An empty `rest` is a
pure **existence filter** (`(K)->P` driving and `(K)->Q` conditional:
the child's `K` must exist in `Q` too). Every internal edge now has a
role, so the only stop `edge_roles` still raises is D2's rule 5. The
card tags them `[enforced, independent]` and
`[enforced, conditional on (T)]`; `card.py --mermaid` labels them on the
edge.

**D2 — the driving rule is total; with no marker and no ancestry, the
first declared edge drives (ruling A).** In order:

1. one internal enforced edge → it drives (`driving_choice = "single"`);
2. exactly one edge marked `drives: true` → it (`"marked"`);
3. no marker: the parent that descends from every other candidate parent
   drives, and its edge to the other parent is widened with the child's
   pins (`"derived"`, ADR 0036 rev 2 — unchanged);
4. **no marker and no ancestry between the parents: the FIRST DECLARED
   internal enforced edge drives** (`"first_declared"`, ruling A,
   2026-09-11). The launcher logs `fk_driving_edge_defaulted table=
   edge= hint='mark drives: true to choose'` at WARNING and the card
   tags it `DRIVES (first declared — mark drives: true to choose)`;
5. more than one `drives: true` is still a `RelationshipError` — the one
   stop left.

Toggling `enabled` stays enough to launch; `drives: true` remains the
override. `driving_choice(table)` exposes which rule fired, so a report
never has to re-derive it. A duplicate `(cols, ref, ref_cols)` entry on
one table is a parse error ("declared twice") — two identical edges
would make "the first declared edge" a question of which copy.

**D3 — a conditional edge is a co-partitioned join on the shared
columns, capped at Top-M candidates per shared value.** The driving key
stream is keyed by the overlap columns; the conditional parent's valid
rows are projected to `(join_key, rest_value)` and reduced to at most
`M` candidates per join key by a deterministic
`blake2b(run_id, rest_value)` order (`Top.SmallestPerKey`), so a hot
shared key never carries an unbounded list. One `CoGroupByKey` per
conditional edge attaches `matches = {edge_id: [candidates per key]}` to
the existing Reshuffle → BatchElements → request payload
(`edge_id = f"({','.join(edge.cols)})->{edge.ref}"`, one helper —
`sdfb_core.engines.fanout.conditional_edge_id` — for the composer, the
plan and preflight's `conditional_rest`; the parent is part of the id so
two conditional edges from the same child columns to different parents
cannot collide, review ruling 14). Parent rows whose join key holds a
NULL are dropped and counted (`fanout/candidates_dropped_null`).

Inside the engine, the cell each child carries and every conditional
edge's candidate are decided JOINTLY (final review fix wave A1, cell
rule corrected by fix wave E1) —
`sdfb_core.engines.base.conditional_draws` calls
`sdfb_core.engines.fanout.joint_key_draw` once per key, one walk over
the CROSS PRODUCT of the dimensions that genuinely BOUND the
combination. This replaced two INDEPENDENT `i % len` cyclic walks
(`conditional_values`, deleted) whose realised combinations were only
`lcm(n_cells, c_1, …, c_m)` — NOT their product, so e.g. 2 cells × 2
candidates gave 2 combinations for 4 children, not 4 — and which RAISED
inside `CellTable.draw(k, exact=True)` the moment a key's fan-out
exceeded an exact cell table, killing the whole batch as
`engine_failure` instead of drawing from the candidates that make those
children representable.

The cell rule turns on EXACTNESS, not on whether conditional edges are
present:

- `plan.exact_cells` (every PK-completing member outside the driving
  edge is a cell or an edge-supplied column) — the cells must KEY the
  child, so they lead the walk: child `i` takes
  `_mixed_radix(i, (n_cells, c_1, …, c_m))` and the FIRST digit indexes
  a per-key seeded weighted PERMUTATION of the cell rows
  (`CellTable.permutation`), so two children of one key never share a
  cell. The candidate digits only start advancing once the cells are
  exhausted.
- otherwise — an unbounded PK member (pattern, numeric, temporal) keys
  the child instead, so the cells carry only their measured MARGINAL:
  each child draws one independently WITH replacement
  (`CellTable.draw(exact=False)`), and the cell dimension is NOT part of
  the mixed-radix walk at all. A permutation prefix here handed a 99:1
  cell table out 50:50 at a fan-out of 2 — silently inverting that
  column's distribution for the whole table (fix wave E1's finding).

Each conditional edge is shuffled per key either way
(`derive_key_seed(run_id, key, salt=edge_id)`).

Capacity is the product of the dimensions that genuinely bound the
PRIMARY KEY: the cell count ONLY when `exact_cells`, times — per
conditional edge whose `rest` supplies a PK member — its ACTUAL
candidate count for that key, or **1** when that edge has none, because
NULL-filling it is exactly ONE combination (a NULL is not a key member,
ADR 0031, so it neither drops the key nor multiplies what it can
represent — fix wave E2).

An edge the PK does NOT read multiplies **nothing** (fix wave G1). Each
plan entry carries `pk_member`, set by the launcher from the PK the run
enforces (`effective_pk_of` — the relationship model's `pk:`), and only
those edges enter the capacity; the others are a FREE dimension that
still hands every child its own candidate, off its own walk. `edge_roles`
calls an edge `conditional` for SHARING a column with the driving edge,
which says nothing about whether its `rest` keys anything — so a lookup
edge used to inflate the cap by its whole candidate count: a child PK
`(T, GRADE, R)` with 2 cells x 3 keying candidates x 20 lookup
candidates "represented" a fan-out of 100, emitted all 100 children and
reported `shortfall == 0`, while only 6 distinct `(GRADE, R)` pairs
exist — 94 rows that land or divert as `pk.duplicate`, with no
`fanout_rows_capped` in the log. Preflight P4 always counted only the
PK-touching edges (D6), so the engine and the check now spell ONE rule.
`pk_member` defaults to **false** in a payload that omits it:
under-counting caps early and SAYS so, over-counting is silent. The fan-out is CAPPED at that capacity
(`min(k, capacity)`) if, and only if, `plan.exact_cells`; a capped key's
shortfall is reported once per worker process **per driven table**
(fix wave E3) as `fanout_rows_capped`. An INEXACT PK never caps — an
unbounded member completes it, so capping would drop rows the PK can
legitimately represent — and its candidate digits wrap (`i % capacity`
over the candidate radices alone) while its cells keep drawing
independently from their measured weights, one per child, untouched by
the wrap. `apply_conditional_overrides` reads the candidate half of the
`KeyDraw` and writes it on `rest`'s child columns after the pool draws
and before the driving/cell overrides. The shared columns always come
from the driving key, so the diamond's `T` is one value satisfying both
parents by construction. The join is keyed on narrow tuples, never on
rows: no new side input grows with the driving parent.

**D4 — `--fk_candidate_cap` (default 64) is the Top-M bound, and it is
a flag because the source's tail decides.**

![Candidate cap vs wrapping](../designs/assets/multi-parent-candidate-cap.png)

*Raising the cap buys back only the wrapping the cap itself caused.*
Formally, the PARENT-side candidate LIST for a shared value holds
`min(c, M)` entries, where `c` is the distinct candidates the
conditional parent holds for that value (concept figure, seeded; the
list is built by `_conditional_candidates` in `sdfb_beam/pipeline.py`).
`M` sizes this SAMPLE — one per shared JOIN VALUE, reused by every
driving key that carries it, not a per-key allotment. What the ENGINE
does with a fan-out `k` beyond the list it receives is D3's capping
rule: with `plan.exact_cells` it CAPS at the joint capacity
(`joint_key_draw`, `sdfb_core/engines/fanout.py`) — including a
candidate-less nullable edge, which contributes a capacity factor of 1
rather than disabling capping (fix wave E2) — and reports the shortfall
as `fanout_rows_capped`; an INEXACT PK never caps, and its candidate
digits wrap instead. Within the list itself the wrapping attributable to
`M` is confined to `c ≥ k > M` — the orange wedge — while `k > c` is
forced by the source and identical at every cap. 64 is the default
because it leaves the overwhelming majority of shared values with a
full, uncapped list while keeping a request's candidate list bounded;
raise it when a branch's within-key variety matters more than the
shuffle, lower it when a request gets too wide.

`--fk_candidate_cap` is `M` — the operator's flag, used VERBATIM: the
composer's Top-M combine and every request payload carry it unclamped
(fix wave F1 reverted an A3 attempt to clamp it to the measured max
fan-out — that clamp collapsed a 1:1 driving edge's shared value to
`Top.SmallestPerKey` keeping exactly ONE of the co-parent's rows, so
EVERY driving key sharing that value landed the identical candidate: a
point mass with nine of ten parent rows never referenced, on the
default flag). The payload-size concern A3 was reaching for is already
served by `keys_per_batch` (`in_set_parent_edges` in `run_pipeline.py`):
with `n` conditional edges present it is lowered so a request never
carries more than `keys_per_batch × M × n ≈ 100_000` candidate TUPLES
(`100_000 // (M × n)`, floored at 1). That floor is where the bound
STOPS bounding: once `M × n` alone exceeds 100k, one key per request
still carries `M × n` tuples and nothing bounds it further, so the
launcher logs one `fk_candidate_request_unbounded table= candidate_cap=
conditional_edges= tuples_per_request= ceiling=` WARNING naming the knob
(fix wave G5). The cap itself is still NOT clamped — F1 reverted that — that is not the same as the
per-request VALUE count, since each tuple carries `|rest|` columns, so
the true value count is `keys_per_batch × M × Σ|rest|` (summed over the
conditional edges), which exceeds the 100k tuple ceiling whenever any
conditional edge's `rest` spans more than one column.

**D5 — an unmatched conditional key writes NULL when it can, and is
dropped, counted and reported when it cannot (ruling B).** A driving key
whose shared value has no candidate in the conditional parent is not a
silent defect:

- when **every** `rest` column is NULLABLE in BOTH the landing schema
  AND the generation schema (fix wave A4 — landing alone let a REQUIRED
  generation column reject the row inside `model_validate` and vanish
  silently through the engine's own `except Exception: continue`) AND
  **absent from the generation schema's declared `primary_keys`** (fix
  wave F2 — `derive_record_model`'s `_make_pk_base` rejects `None` on
  every declared PK column whatever its mode says, BQ allows a NULLABLE
  PK column, and the engines swallow that same `ValidationError` the
  same silent way; on ADR 0037's own diamond the child PK's last member
  IS the co-parent's column, so a rest column in the declared PK is the
  default shape, not a corner), the engine writes NULL there — the tuple
  is then legitimately parentless and the ADR 0031 orphan query excludes
  it, as it already excludes every NULL tuple. Either disagreement is
  treated as NON-nullable and logged once per edge as
  `fk_nullable_schema_mismatch table= edge= landing= generation=
  reason= pk=` (WARNING) — `reason=` is `declared_pk`,
  `schema_mode_mismatch`, or both comma-joined, and `pk=` names the
  offending rest columns;
- otherwise `GenerateRecordsDoFn` removes the key **before** generation
  (no GPU spend on a row that cannot be valid), increments
  `fanout/keys_unmatched`, logs `batch_unmatched batch_id= keys_dropped=`
  and emits one DLQ envelope per dropped key with
  `rule_id="fk.unmatched"`, `error_type="referential_integrity"`,
  weighted by that key's expected rows (`n / len(keys)`) so the BLOCKER
  gate sees the rows that were lost rather than one envelope per batch.

A `rest` that is empty (a pure existence filter) is never nullable: an
unmatched key is always dropped and counted. `fk.unmatched` is a
distinct rule from `fk.orphan` on purpose — an orphan is a generator
regression and never expected; an unmatched key means the SOURCE lacks
that branch, which a report should read as an input fact.

**D6 — preflight P4 gains ONE per-key factor, not two, and it is an
UPPER bound (fix wave A2 corrected the original design).**
`_check_driven_pk` (`sdfb_beam/cli/preflight.py`) keeps its shape (exact
members only, ADR 0035/0036): a **conditional** edge whose `rest` sits
in the child's PK contributes at most `--fk_candidate_cap` (the
operator's flag, used as-is — D4), so the check becomes
`max_k ≤ n_cells × Π (--fk_candidate_cap)` — one factor per conditional
edge in the PK — or the launch stops naming the edge or the flag. An
**independent** edge is deliberately NOT a
factor: its pool is drawn per ROW WITH REPLACEMENT (`_draw_fk_columns`),
so two children of one parent key CAN and DO draw the same parent tuple
— counting its pool cap here declared PKs safe that the engine then
duplicated (the review's finding). Its columns still count as `known`
(no cell table is measured over them, so they cost nothing), but the
stop names it as a NON-factor ("drawn per ROW with replacement, so it
bounds nothing per key") and no longer offers "a parent that lands more
keys" as a remedy, because it never was one. The conditional factor
itself is an UPPER bound, not a promise: `joint_key_draw` (D3) caps a
key at what its co-parent ACTUALLY offered that key, not at the flag,
and reports any shortfall as `fanout_rows_capped` — a model whose
co-parents are thin passes P4 and still caps at run time, observably
rather than silently. Members outside the PK never enter P4; an inexact
PK still skips it and streaming uniqueness measures `pk.duplicate`.

**D7 — every role and every path is one milestone line.** Launcher:
`fk_edge_role edge= role= overlap=` per enforced edge (the `overlap=`
field is new), `fk_driving_edge_defaulted table= edge= hint=` (WARNING)
when rule 4 fired, `fk_edge_overlap_external table= edge= other=
overlap= note=` (WARNING) once per overlapping PAIR with at least one
external end — driving∩external, external∩external, or external∩any
non-driving edge (fix wave F3; `edge=` is always the external one,
`other=` the edge it clashes with), reported from
`registry.external_overlaps` whether or not edge ROLES were resolved, so
a single-table launch (where they are not) warns too (fix wave G5's
sibling, G3) — `fk_candidate_request_unbounded table= candidate_cap=
conditional_edges= tuples_per_request= ceiling=` (WARNING) when
`--fk_candidate_cap × conditional edges` passes the per-request ceiling
on its own (D4, fix wave G5) — and
`fk_nullable_schema_mismatch table= edge= landing= generation= reason=
pk=` (WARNING) once per conditional edge when nullability fails (D5,
fix waves A4 + F2 + G2; `reason=` is `declared_pk`,
`schema_mode_mismatch`, or both). The PK that guard reads is the one the
run ENFORCES — the relationship model's `pk:` (ADR 0032), with
`TableSchema.primary_keys` (the BQ table constraint copied into
`_ddl.json`) standing in only when the model declares none. Reading the
constraint alone never fired on the canonical setup, where it is `None`:
this diamond's own `R` — a declared key member — was NULL-filled, landed,
and its repeats diverted as `pk.duplicate` (fix wave G2). Worker: `relational_fk_edge mode=side_input|conditional
overlap=` per edge, `fanout_bound … conditional=<n> candidate_cap=` once
per engine build (`candidate_cap` is `--fk_candidate_cap` verbatim — D4),
and `fanout_rows_capped requested= emitted= capacity=` (WARNING) once
per worker process **per driven table** (fix wave E3 — a single-job
relational run with several driven tables used to report only the
FIRST one to cap; the per-table scope is pinned at both engines' call
sites, fix wave G4) the first time `joint_key_draw` caps a key below its
requested fan-out (D3, fix waves A1/E2/G1). Counters
`fanout/candidates_dropped_null` and `fanout/keys_unmatched`; DLQ rule
`fk.unmatched`. No mermaid in any log (ADR 0036 rev 2).

## Alternatives considered

- **Serve a diamond branch from a side-input pool** (the ADR 0031 path,
  as for an independent edge). Rejected: the pool is drawn as a WHOLE
  tuple, so the branch would write its own value into the shared column
  `T` and then the driving key would overwrite it (or not, depending on
  ordering) — precisely the "last edge silently wins" corruption ADR
  0036 D4 stopped the launch to prevent. A shared column has one value;
  only a join conditioned on that value can pick a `rest` that is
  consistent with it.
- **Join rows after generation** (generate the child, then repair the
  branch columns with a join against the parent). Rejected: it shuffles
  ROWS, not keys — a child-sized shuffle at 100M rows, exactly the cost
  ADR 0034's single-barrier work removed — and a repair that changes a
  landed column after the PK cells were drawn re-opens the uniqueness
  question ADR 0036 D3 closed. The join in D3 moves narrow key tuples
  only, before batching.
- **Keep refusing multi-parent children** (ADR 0036 D1/D4 as written).
  Rejected by the evidence: the star fact and the true diamond are
  ordinary warehouse shapes, and the only workarounds are to detach a
  real parent (losing its integrity entirely) or to mark an edge
  `drives: true` when no mark is correct. A stop is right when the
  alternative is silent corruption; it is wrong when a correct mechanism
  exists.
- **Weight the conditional draw by the child's marginal** (IPF, as ADR
  0031 does for the independent pool). Deferred, not rejected:
  candidates are uniform within a match set today. The weighting would
  have to be fitted per shared value, and no evidence yet says the
  within-key marginal matters.

## Consequences

- **ADR 0036 D1's mutual exclusion is gone.** A driven child may carry
  side-input pools next to its driving edge; what remains on that path
  is disjoint from the driving columns by construction, because an
  overlapping edge is now `conditional`. `_route_parent_edges` no longer
  raises "a driven child's other in-job edges must be implied".
- **No model file changes.** No new YAML key, no new flag in
  `config/relationships/*.yaml`: roles are derived from `cols` and the
  model's own DAG. A model that stopped yesterday launches today.
- **One new CLI flag**, `--fk_candidate_cap` (default 64), and one new
  DLQ rule, `fk.unmatched`. `validation_runs.dlq_by_rule` gains a key
  that older runs do not have.
- **Cost per conditional edge**: one `Distinct` (skipped when the
  projection holds the parent's declared PK) plus one Top-M combine on
  the parent side, and one `CoGroupByKey` on the driving keys. Per
  request, `keys_per_batch × M × |rest|` values. The independent and
  driving paths are unchanged.
- **A new launch stop: two NON-driving, IN-MODEL edges cannot write the
  same child column.** `edge_roles` gave every non-driving edge a role
  from its overlap with the DRIVING edge alone and never compared
  non-driving edges with EACH OTHER, so two of them could claim one
  column and the last one drawn silently won — two `independent` edges
  land nearly every row as `fk.orphan` (after the GPU already generated
  it), and two `conditional` edges whose `rest` overlaps are not gated
  by `fk.orphan` at all, so those referentially broken rows LAND
  uncaught. `RelationshipRegistry._check_column_ownership` (final
  review) now raises `RelationshipError` on the first such pair:

  ```text
  child: edges (X,Y)->pa [independent] and (X,Z)->pb [independent] both write (X) — one child column cannot be owned by two edges: the second draw overwrites the first, landing a tuple its parent never held. Make one edge's columns a SUBSET of the other's so it is implied, mark the edge this table is generated from `drives: true`, document one edge (`enforced: false`, so no keys are drawn from it), or disable one parent (`enabled: false`).
  ```

  Four ways out: make one edge's columns a subset of the other's (so it
  resolves `implied`), mark the actually-driving edge `drives: true`,
  document one edge (`enforced: false`), or disable one of the two
  parents (`enabled: false`). **`external` edges are excluded from this
  stop** (fix wave F3 — see the Limitations bullet below): none of the
  four remedies apply to one, since an external edge never becomes
  `implied`, `drives: true` is inert for it, and an external parent has
  no `tables:` entry to disable or document. **Out of scope for now**:
  resolving an in-model clash automatically instead of stopping the
  launch, via a candidate/pool-level join on the columns the two parents
  share, which would let both edges draw jointly from the intersection
  instead of one overwriting the other. Not implemented (final-review
  ruling 15).
- **A driving-edge mislabel fixed.** A child with two enforced edges to
  the SAME parent and no `drives:` marker used to report the choice as
  `"derived"` (rule 3's ancestry check ran over an empty set of "other"
  candidate parents and was vacuously true for every edge). Rule 3 now
  requires at least two DISTINCT candidate parents; with only one, the
  launch falls through to rule 4 (`"first_declared"`), so the operator
  now gets the `fk_driving_edge_defaulted` WARNING and the card tag
  `DRIVES (first declared — mark drives: true to choose)`. The edge
  actually chosen is unchanged.
- **Limitations, named (design §9), not defects:**
  - *any external edge sharing a written column with another edge* — not
    just the pre-ADR-0037 driving∩external case (the driver-side pool is
    drawn as a whole tuple and the driving edge overwrites the shared
    columns): fix wave F3 extended this to external∩external and
    external∩any non-driving edge too, since the cross-edge ownership
    stop above cannot apply a remedy to an external edge. The launcher
    logs one `fk_edge_overlap_external table= edge= other= overlap=
    note=` WARNING per overlapping pair (`edge=` is always the external
    one). Named plainly, not resolved: the last edge written keeps the
    shared column, so the OTHER edge's tuple may not exist in its own
    parent — a real risk, unchanged from before ADR 0037, now visible
    instead of silent. Bring the parent inside the launch to resolve it.
  - *uniform candidates* — no weighting within a match set (see
    Alternatives).
  - *no correlation between branches* — a diamond's `L` and `R` are
    drawn independently given `T`, so the source's joint `(L, R)`
    distribution is not reproduced. Each branch's own marginal is.

## Acceptance

- [x] Registry: the shape sweep
  `packages/sdfb-tests/tests/unit/contracts/test_relationship_shapes.py`
  passes with both `xfail` markers removed (star fact and true diamond
  resolve, plus the cross-edge column-ownership stop and the
  `TestExternalEdgesWarnRatherThanStop` shapes fix wave F3 added — 21
  passed, 0 xfail markers left on `ws12-fanout-generation`), and
  `test_relationship_models.py`'s two unmarked-parent cases now assert
  "first declared drives, the other is conditional" instead of a stop
  (47 passed).
- [x] DirectRunner, fake client, whole-tuple checks — the eight shapes in
  `packages/sdfb-tests/tests/unit/test_fanout_shapes.py` (8 passed):
  - **star** — `dim_a`, `dim_b`, `fact`: every `fact` row's key tuple
    exists in each dimension, PK unique, row count within the histogram;
  - **diamond** — `top`, `left`, `right`, `bottom`: every `(T,L)` in
    `left`, every `(T,R)` in `right`, and the two `T`s are one value;
  - **beyond the cells** (fix wave A1,
    `test_fanout_beyond_the_cells_rides_the_conditional_candidates`) — a
    driven child asks 3 children
    per key from a 2-row cell table with a conditional edge covering the
    rest: the cells and candidates jointly offer 2×2=4 combinations, so
    all 3 are representable and pairwise distinct — the exact regime
    that used to raise inside `CellTable.draw(k, exact=True)`;
  - **inexact cells keep their skewed marginal** (fix wave E1,
    `test_inexact_cells_keep_their_skewed_marginal`) — an UNBOUNDED PK
    member (`SEQ_ID`) plus a conditional edge together: the cell column
    is drawn WITH replacement from its 9:1 source weights, landing
    > 0.75 on the heavy cell (a permutation prefix would land exactly
    0.5) and at least one key repeating a cell across its children — the
    combination no other shape here exercises end to end;
  - **existence filter** — `(K)->P` driving, `(K)->Q` conditional with
    empty rest: every landed `K` is in `Q`, and the keys that are not
    reach the DLQ as `fk.unmatched` with the expected-row weight;
  - **nullable branch** — `R` NULLABLE and half the `T`s absent from
    `right`: those rows land with `R = NULL` and no DLQ envelope;
  - **graph** — six tables mixing a tree, a star fact, a diamond and a
    1:1 chain: zero orphans on every enforced edge, `generation_waves`
    order respected;
  - **two conditional edges to different parents** (review ruling 14) —
    a child column that is an existence filter against TWO co-parents at
    once: each edge's candidate list is looked up by its own
    `edge_id = (cols)->ref`, so neither overwrites the other in
    `matches` and each is the sole reason for its own keys' DLQ entries.
- [x] M4/Dataflow: a relational launch whose model declares a star or a
  diamond — `fk_edge_role … role=independent|conditional overlap=` and
  `relational_fk_edge mode=conditional` read as this ADR predicts, the
  RUN_PLAYBOOK §8.4 whole-tuple orphan query returns 0 on every enforced
  edge including the conditional ones, and `fk.unmatched` in
  `validation_runs.dlq_by_rule` is either 0 or explained by a branch the
  source genuinely lacks.
