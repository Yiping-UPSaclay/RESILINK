# ADR-0008 — Extension architecture: systems, slots and models as versioned plugins
Status: **accepted** (revision 2, 2026-11-23). Amends blueprint §1.2, §1.4, §2, §4 and §6
(v2.2). Builds on ADR-0001 (canonical model) and ADR-0006 (Operation Registry, invariants I1–I7).

| Revision | Content |
|---|---|
| r1 (2026-11-23) | Research models become plugins in ten fixed slots, composed by a fixed episode pipeline. |
| **r2 (2026-11-23)** | **Systems and slots also become registered, versioned definitions, so a new system, a new kind of question, or a new operation model (DC-OPF, AC-OPF) can be added later without core edits.** Event-driven episode kernel (decisions react, one decision-maker per operator). Model ensembles. Data-need checks and an `observations` table family. Persistent-worker execution for heavy solvers. Draft (0.x) contracts until real theses use them. Proving ground against observed events. Easier student on-ramp. |

## Context

RESILINK is the **common platform where the results of follow-up studies (PhD theses) are integrated**.
Those models do not exist yet. Expected families:

- asset degradation and failure;
- fragility under external stress (flood, wildfire, wind, heat);
- fleet/system couplings: economic consequences, performance requirements, shared maintenance resources;
- operation and maintenance (O&M) decisions;
- within-system cascades, including **operation models** such as DC-OPF or AC-OPF for power;
- possibly **new systems** (water, roads, district heating, hydrogen).

**Sprint 0 hard-wires the analytics core.** One model per step, and the systems themselves are fixed in code:

| Hard-wiring | Where |
|---|---|
| Systems, asset classes, supplier classes and repair medians as code constants, used directly by the network builder, the Monte Carlo loop and the synthetic territory | `domain/vocab.py:28-57` → `network/compiled.py:80`, `risk/montecarlo.py:80, 91, 138`, `synthetic/territory.py:177` |
| Cascade model instantiated directly in the loop | `risk/montecarlo.py:75` |
| Binary damage; repair time drawn together with failure | `fragility/lognormal.py:52-87`, `risk/montecarlo.py:86` |
| Within-system service = graph connectivity, called from inside the cascade | `cascade/topological.py:85` → `network/compiled.py:101` |
| Consequence fixed to population × outage hours; fixed metrics | `risk/montecarlo.py:91-96, 164-205` |
| Closed `Literal` model choices; one 72-hour time scale | `domain/config.py:68, 84-87` |
| Fixed result tables usable by `ValueRef` | `knowledge/store.py:54` |

**Review of r1.** r1 made models pluggable, but the review found six weaknesses:
1. The ten slots were fixed, and designed before any thesis model existed.
2. The pipeline order was fixed: repair was planned once, before outages spread.
3. There was one decision-maker for all operators.
4. Systems could not be added.
5. There was no validation against real events.
6. There was no place for observation data (asset ages, inspections, outage records).

Two further concerns: the cost of packaging a model is a barrier for students, and model disagreement between theses is information that r1 would hide.

## Decision

### 1. A small fixed core; everything else is registered
The core keeps only what every study shares:
- the simulation **kernel**: clock, event queue, deterministic ordering;
- the shared **board** of state fields;
- RNG streams;
- composition and version pinning;
- statistics (reducers);
- the results store, provenance and tiers;
- the invariants I1–I7.

Everything else is a registered, versioned extension, discovered through Python entry points:

| Extension | Entry-point group | Examples | Who adopts it |
|---|---|---|---|
| **System pack** | `resilink.systems` | `elec` (transmission and distribution subsystems), `facility`, `gas`, `telecom`; later water, roads, heat, hydrogen | Platform review of a one-page proposal |
| **Slot definition** (a kind of modelling question) | `resilink.slots` | `hazard`, `fragility`, …; later `degradation`, `resources`, `policy`, `road_access` | Platform review of a one-page proposal |
| **Model** (a plug for a slot) | `resilink.models` | `flood_footprints`, `dc_opf_load_shedding`, `gamma_condition` | Conformance kit + supervisor (ladder L2) |
| **Analysis op** | `resilink.ops` | Rankings, optimisation tools (READ/COMPUTE/PROPOSE only) | Op tests (ladder L1) |

Built-in systems, slots and models are registered through the core's own entry points, the same path as
external ones.

### 2. System packs
A system pack defines one infrastructure system:
- **subsystems** (e.g. `elec_transmission`, `elec_distribution`);
- **node and link classes**, plus typed extra attributes with units;
- what **"served"** means and its measure (load served, pressure, travel time), and which class supplies
  the population grid;
- its **default service model** per subsystem;
- optional **data tables** (e.g. electrical parameters) and the adapters that fill them;
- repair-time assumptions, dependency-rule files and its **operators (actors)**;
- a **synthetic fixture**, so that its models can be certified on the synthetic territory.

The systems now in `domain/vocab.py` move into four built-in packs. `domain` keeps only the generic
vocabulary (tiers, cause codes, the ID grammar).

### 3. Slot definitions are registered and versioned
A slot definition declares:
- **identity:** id, version and the question it answers;
- **contract:** the protocol a model must implement;
- **cardinality:** `single`, `per_subsystem`, `per_actor` or `many`;
- **triggers:** the kernel events it reacts to, plus a **phase** that orders handlers at the same instant;
- **board fields:** those it owns (may write) and the standard fields its models may read;
- **checks:** its property checks for the conformance kit;
- **defaults and options:** a **default "do nothing" model**, whether ensembles are allowed, and the
  allowed execution modes.

**Registered now (v0.1, draft).** Seven slots, each with a model ported from Sprint 0:

| Slot | Cardinality | Built-in model |
|---|---|---|
| `hazard` | single | `flood_footprints` |
| `fragility` | single | `lognormal_library` |
| `dependency` | single | `proximity_softmax` |
| `service` | per subsystem | `connectivity` |
| `propagation` | single | `topological_temporal` |
| `restoration` | single | `independent_repair` |
| `consequence` | many | `people_hours` |

**Anticipated, not registered.** `degradation`, `resources` (per actor), `policy` (per actor) and the
`lifecycle` orchestrator. They are sketched in blueprint §6.5 and **designed together with the first thesis
that needs them**. Any later question, such as road access for repair crews or demand response, enters the
same way.

The engine also defines two engine-level slots: `orchestrator` (built-in: `episode`) and `sampler`
(built-in: `nested_mc`).

### 4. Models and model cards
Every model ships a `ModelCard` stating:
- **identity:** id, version and slot (plus the slot-version range it supports); for `per_subsystem` slots,
  the subsystems it applies to;
- **description and credit:** a summary that says *when to use it*, authors, **citation**, SPDX licence and
  **maturity**;
- **scientific basis:** assumption IDs and applicability (hazards, systems, classes, time scale);
- **interfaces:** declared board **reads and writes**, emitted **tables and metrics**, RNG streams;
- **data needs** (`requires` and `optional`: release tables and columns, with units);
- **execution mode** (§9) and a runtime hint.

Parameters are a pydantic model. For the common cases, a **function plug** (§13) generates the card and
wrapper from a plain function.

### 5. An event-driven episode kernel
The episode is no longer a fixed pipeline. It is a **discrete-event simulation on a shared clock**:
- **Events.** The kernel emits `episode.start`, `damage.changed`, `element.changed` (repair completed, trip,
  backup exhausted), `service.changed`, `decision.epoch` and `episode.end`.
- **Subscriptions.** Each slot subscribes through its triggers, and slots may define new namespaced
  events.
- **Ordering.** Handlers run in deterministic order: by time, then phase, then insertion sequence.
- **Revisable plans.** Restoration plans can be revised at each decision epoch, so decisions react to the
  evolving outage.
- **Actors.** Operators are declared in the system packs (e.g. RTE, Enedis, NaTran, mobile operators). The
  anticipated `policy` and `resources` slots are **per actor**, with observations filtered to what that
  actor can see.
- **Equivalence.** Sprint 0's fixed sequence is the special case where every handler fires once.
- **Lifecycle (anticipated).** A `lifecycle` orchestrator reuses the kernel over years and runs one episode
  at each hazard arrival.

### 6. Shared board, defaults, and a composition report
- **No direct calls.** Models never call each other. They read and write declared board fields:
  - a standard vocabulary with units (`asset.condition`, `damage.state`, `damage.functionality`,
    `repair.finish_h`, `service.served`, `service.level`, `outage.hours`, …);
  - namespaced fields owned by one model.
- **Defaults.** A field that no active model writes reads as its declared default, which keeps couplings
  optional.
- **Visible defaults.** The **composition report** (`experiments.validate`, and stored in the run manifest)
  lists every field read but defaulted, e.g. "ageing not modelled: `asset.condition` = 1.0". The Copilot and
  the views show it, so nobody assumes a coupling that was not active.

### 7. Ensembles and study designs
- **Weighted alternatives.** A slot that allows ensembles may list **weighted alternative models**.
  - The engine draws one alternative per outer (epistemic) iteration from the stream
    `(outer, slot, "model_choice")`.
  - Results carry a **model-uncertainty breakdown**: per-alternative means, and the share of variance
    between alternatives.
  - Alternatives must write the same standard fields; this is checked at composition.
  - Weights are human decisions, recorded with their evidence (e.g. proving-ground scores, §12).
- **Pluggable sampling, weighted worlds.** "How worlds are drawn" is a `sampler` plug, separate from "what
  is simulated". Every simulated world carries a **weight** `w` (1 for plain Monte Carlo), and every
  reducer is weighted. Rare-event importance sampling, Latin hypercube or Sobol designs can then be added
  later without changing anything else.

### 8. Data needs, system data and observations
- **Data needs checked before a run.** Model cards declare data needs, and composition checks them against
  the release's table catalog. A missing table or column fails at validation with a specific message, e.g.
  "`ac_opf_load_shedding` needs `elec_generators.q_max_mvar`; release `bdr-2027.01.0` has no such
  column".
- **New tables come from packs.** System packs declare optional tables (e.g. buses, branch parameters,
  generators, loads), filled by adapters when data exists. This includes estimates as graded assumptions,
  and operator data in the restricted tier.
- **Observations.** A generic **`observations`** table family joins the canonical model: subject, quantity
  (ontology term), time interval, value, unit, uncertainty, method, source snapshot and tier. It holds asset
  ages, inspection results, outage records and observed hazard extents, for degradation models,
  calibration and validation.

### 9. Execution modes, including persistent solvers
| Mode | How it runs | Suitable for |
|---|---|---|
| `in_process` (default) | Inside the Python simulation process | Any slot |
| `worker` | A **persistent** process with its **own environment** (uv project, container image digest or Julia project), exchanging Arrow messages over stdio and started once per Monte Carlo worker | Heavy solvers (AC-OPF in Julia), conflicting dependencies; also tightly coupled slots such as `service` inside the propagation fixed point |
| `batch` [deferred] | One file exchange (Arrow/Parquet) per Monte Carlo chunk | Batch-stateless slots, non-Python research code |
| FMI 3.0 / BMI wrappers [optional] | Dedicated wrappers | Models already packaged to those standards |

In every mode, seeds are passed explicitly, outputs are schema-validated, and the environment identity
enters the `run_id`.

### 10. Versioning and governance: easy to propose, reviewed to adopt
- **Everything is versioned.** The plugin API, each slot definition and each system pack carry SemVer.
- **Draft until proven.** Everything starts at **0.x (draft)**: contracts may change, and every dependent
  plug is re-certified automatically. A contract becomes **1.0 (stable) after two independent theses** have
  used it.
- **After 1.0:**
  - minor releases only add;
  - a major release runs **alongside** the previous one, with an adapter, for at least two platform
    releases or six months, whichever is longer.
- **CI catches breakage.** CI re-certifies every registered plug against the slot versions in use and
  writes an **impact report** naming the affected plugins and theses, so a contract change is never
  merged blind.
- **New slots and systems** need a one-page extension proposal (`docs/extensions/`), reviewed by the
  platform maintainers and the supervisor. The proposal must:
  - show that no existing slot already covers the question;
  - define its board fields with units, its triggers and phase, and its checks;
  - provide a default model and declare its data needs.

  This prevents "slot sprawl", i.e. several near-duplicate slots whose models can neither be compared nor
  combined.

### 11. Conformance kit
`resilink plugin check <distribution>` runs every model, slot and pack in the distribution on the synthetic
territory. It checks:
- registration and cards;
- parameter schemas;
- slot contract shapes;
- board discipline (declared reads and writes, ownership, units);
- data needs;
- output schemas and tiers;
- **bit-identical results under a fixed seed and across worker counts**;
- stream isolation;
- the slot's property checks;
- import and network hygiene;
- the runtime budget.

Packs and slots get their own checks: synthetic fixture present, vocabulary consistent, default model
passes.

### 12. Proving ground against observed events (deferred to P1–P2)
A shared set of **benchmarks** (`benchmarks/<id>/`). Each benchmark defines:
- an observed event and the matching release;
- the hazard reconstruction;
- the observations used as truth;
- which slots are scored, with all others fixed to reference models;
- the scoring rules (e.g. Brier score for asset outages, error on the time series of sites down).

Every candidate model in a slot is scored on the same events. Scores are evidence for **L3 promotion** and
for **ensemble weights** (human-approved).

First candidates:
- ARCEP's daily record of unavailable mobile sites (the start of the history and its coverage of Storm Alex,
  2–3 October 2020, are TBC);
- Storm Alex in Alpes-Maritimes, already planned as the P4 transfer back-test.

### 13. Integration ladder and student on-ramp
| Level | Delivers | Enters as | Gate |
|---|---|---|---|
| **L0** Results | Curves, parameters, datasets, observations | Knowledge libraries with evidence; `observations`; a data source | PR review (**works today**) |
| **L1** Analysis | A standalone method | Op in `resilink.ops` | Op tests |
| **L2** Composable model | A plug for an existing slot (or a new slot or system via §10) | Package in `plugins/` or a pinned repository; maturity `experimental` | Conformance kit; supervisor |
| **L3** Reference model | A validated L2 model | Default in templates; maturity `reference` | Benchmark scores, validation report, ADR, golden eval task |

**Lowering the barrier.**
- **Function plugs:** e.g. `@fragility_function` turns `p_exceed(im, asset_class, params) → probabilities`
  into a complete model. Equivalent decorators exist for intensity tables and per-hour consequence rates.
- **Notebook sandbox:** `resilink.sandbox` tries a model on the synthetic territory and compares it with the
  built-in, with no packaging.
- **Scaffolds:** `resilink plugin new` / `slot new` / `system new`.
- **Integration assistant:** the Builder agent (D25) is narrowed to plugin integration and **moved from P4
  to P2**. It reads a student's code, then drafts the plug, card and tests, and iterates on the conformance
  report before opening a PR for human review.
- **Visible maturity:** every result shows the **lowest maturity** among its models.

### 14. Agent access
- **Discovery ops (READ):** `systems.list`, `slots.list`, `models.list` and `models.describe` return packs,
  slot definitions, cards, parameter schemas, data needs and citations.
- **Validation:** `experiments.validate` returns the composition report (defaults, data needs, ensembles).
- **Knowledge graph:** packs, slots and models are indexed in the knowledge graph.
- **Installing code stays human:** a PR that changes `uv.lock`; no op installs code.

## Worked example: power operation models (DC-OPF, AC-OPF)

1. **Data.**
   - The `elec` pack declares optional tables: `elec_bus`, `elec_branch_params` (reactance, rating),
     `elec_generators` (limits, cost) and `elec_loads`.
   - Open data lacks impedances and ratings, and RTE no longer publishes geometry (F1). Two ways to fill the
     tables:
     - an adapter estimates them by voltage class and length, as graded assumptions;
     - operator data under agreement, in the restricted tier.
   - Open transmission-level model datasets such as PyPSA-Eur (ENTSO-E area) are a reference for parameter
     estimates.
2. **Plugs in the `service` slot, subsystem `elec_transmission`:**
   - `dc_opf_load_shedding`: a linear program solved in-process with SciPy (`linprog`, HiGHS by default);
   - `dc_pf_protection_cascade`: overload tripping, without redispatch;
   - `ac_opf_load_shedding` in `worker` mode. Candidate tools: PowerModels.jl (Julia/JuMP; AC, DC and SOC
     relaxation formulations) or pandapower (OPF through PYPOWER or PandaModels.jl).

   The distribution subsystem keeps `connectivity`, because open data has no switch states (A-TOPO-01).
3. **Data-need check.** An AC-OPF without reactive limits in the release fails at validation, before any
   compute.
4. **Operator behaviour as model uncertainty.** OPF assumes optimal redispatch after each failure; the
   protection cascade assumes lines trip first. Both enter as **ensemble alternatives** of the same slot,
   and the model-uncertainty breakdown shows how much the choice matters.
5. **Compute.** `service` runs at every element change in every world. Two strategies:
   - DC-OPF in the main loop;
   - AC-OPF on a sub-sample of worlds as a check, or a surrogate trained on AC runs, with the solver kept
     alive in a persistent worker.

## Alternatives considered
- **Fixed slot set (r1).** Rejected. New questions (crew road access, demand response) and new systems
  would need core edits, and designing every slot in advance risks wrong shapes.
- **Free slots without review.** Rejected: slot sprawl destroys comparability. Hence "easy to propose,
  reviewed to adopt".
- **Fixed pipeline (r1).** Rejected in favour of the event kernel: closed-loop decisions, evolving hazards
  and several operators need it. The pipeline remains the special case.
- **One model per slot (r1).** Rejected in favour of optional ensembles: disagreement between theses is
  epistemic uncertainty and should be shown.
- **pluggy** (pytest's hook system: one call runs N implementations). It does not fit one-model-per-slot
  selection with typed, pinned versions. Its discovery uses the same entry points, so it can be added later
  for observer hooks.
- **IN-CORE-style chained analyses** (`BaseAnalysis.get_spec()`: parameters and input/output datasets).
  These suit L1, but they cannot couple models within a simulated world. We adopt the declarative spec
  idea.
- **A co-simulation framework as the core** (mosaik, an FMI master). Its coordination overhead per step
  conflicts with nested Monte Carlo at about 10 ms per sample. FMI and BMI stay as optional wrappers, and
  the persistent worker covers heavy external solvers.
- **One microservice per model.** Rejected: latency, operating cost and weaker pinning.

## Consequences
**Positive.**
- New systems, new questions and new models can all be added without core edits.
- Every number is attributed to the theses and versions behind it.
- Comparisons are fair: same territory, same reducers, common random numbers, a common proving ground.
- Disagreement between models is visible.
- Agents discover and compose whatever is installed.
- The invariants are unchanged.

**Costs.**
- **A larger refactor than r1:** about three weeks for steps E1–E9.
- **Run IDs change.** The demo numbers will match Sprint 0 statistically, not bit-for-bit. This should be
  done **before the first commit**.
- **More to govern:** contracts to maintain (mitigated by draft status and impact reports).
- **Event-kernel overhead** to keep within budget.
- **Supply chain:** entry points run code at import. This is controlled by the lockfile, PR review,
  `configs/plugins.yaml` and the rule that no op installs code.

## Acceptance criteria
1. **Built-ins ported.**
   - 4 system packs: `elec` with 2 subsystems, `facility`, `gas`, `telecom`.
   - 7 slot definitions (v0.1), 7 built-in models, the `episode` kernel and the `nested_mc` sampler.
   - `domain/vocab.py` no longer lists systems or asset classes.
2. **Existing guarantees kept.**
   - Deterministic tests unchanged, including the analytic 5-node cascade.
   - Property tests and worker-count invariance pass.
   - `resilink reproduce` is IDENTICAL.
   - Demo metrics are within 3 combined Monte Carlo standard errors of Sprint 0.
3. **Extension without core edits.** Two example packages are added, and **CI asserts that no file under
   `packages/resilink/` changes**:
   - `resilink-example-ageing` registers a **new slot** (`degradation`, draft), a model (`gamma_condition`)
     and a **function plug** (`condition_lognormal` fragility);
   - `resilink-example-water` registers a **new system** (a toy water network on the synthetic territory,
     with pumps that depend on power).
4. **Ensembles.** An experiment with two fragility alternatives runs and reports the model-uncertainty
   breakdown.
5. **Data needs and defaults.** A model whose required column is missing fails at validation with a
   specific message, and defaulted fields appear in the composition report and the manifest.
6. **Persistent worker.** A toy `service` model running in a separate environment passes the conformance
   kit, including determinism.
7. **Conformance kit.**
   - It passes on all built-ins and examples.
   - Broken fixtures fail with specific messages.
   - A draft slot change produces an impact report.
8. **Agent access.** The ops `systems.list`, `slots.list`, `models.list` and `models.describe` work, and a
   golden eval task (compose an experiment with an example plugin) passes in dry-run.
9. **Performance and contracts.**
   - Demo runtime is within +15 % of Sprint 0 (12.8 s for 1,200 samples).
   - Import contracts: plugins import only `resilink.plugin_api` from the core.

## Implementation plan (approved 2026-11-23; not started)

| Step | Content |
|---|---|
| E1 | `resilink.plugin_api` 0.1 (cards, board, events, base protocols, function-plug helpers); registries for systems, slots and models (entry points, pinning) |
| E2 | System packs: move `domain/vocab.py` systems into `elec`/`facility`/`gas`/`telecom`; network compilation and the synthetic territory driven by packs; subsystems |
| E3 | Seven slot definitions; built-in models ported (repair split from damage; service per subsystem split from the cascade; consequence) |
| E4 | Event-driven `episode` kernel replacing `risk/montecarlo._run_chunk` and the cascade's internal loop; deterministic ordering; RNG streams; revisable restoration plans |
| E5 | Composition: config schema 2, subsystems, ensembles, data-need check, composition report, manifest pinning, `run_id`; `nested_mc` sampler with weights |
| E6 | Declared tables, weighted reducers, model-uncertainty breakdown, `ValueRef.table` generalised, tier rules, `observations` table family |
| E7 | Execution modes `in_process` and `worker` |
| E8 | Conformance kit, pack and slot checks, impact report, scaffolds, sandbox, function plugs |
| E9 | Example packages (ageing, water); CI "no core change" check; discovery ops and eval task; `add-model-plugin` skill; `CLAUDE.md` |

**Deferred:**
- the `degradation`, `resources` and `policy` slots and the `lifecycle` orchestrator, with the first thesis
  that needs them;
- `batch` mode and FMI/BMI wrappers;
- the proving ground, in P1–P2;
- the integration assistant, in P2;
- other samplers.

## Sources (checked 2026-11-23)
- Entry points specification (PyPA): https://packaging.python.org/en/latest/specifications/entry-points/
- `pyproject.toml` `[project.entry-points]`: https://packaging.python.org/en/latest/specifications/pyproject-toml/
- `importlib.metadata` (selection by group since Python 3.10; `EntryPoint.dist`): https://docs.python.org/3/library/importlib.metadata.html
- pluggy: https://pluggy.readthedocs.io/en/stable/
- IN-CORE `BaseAnalysis.get_spec()`: https://github.com/IN-CORE/pyincore/blob/main/pyincore/baseanalysis.py
- CSDMS Basic Model Interface: https://bmi.csdms.io/en/stable/
- FMI 3.0.2 (Co-Simulation, Model Exchange, Scheduled Execution): https://fmi-standard.org/docs/3.0.2/
- mosaik (smart-grid co-simulation): https://mosaik.readthedocs.io/
- NumPy `SeedSequence` and parallel streams: https://numpy.org/doc/stable/reference/random/parallel.html
- PowerModels.jl ("Julia/JuMP package for Steady-State Power Network Optimization"; AC, DC approximation, SOC relaxation): https://lanl-ansi.github.io/PowerModels.jl/stable/
- pandapower (AC and DC OPF via PYPOWER; PandaModels.jl): https://pandapower.readthedocs.io/en/latest/opf.html
- PyPSA (MIT) and PyPSA-Eur (open transmission-level model dataset, ENTSO-E area): https://docs.pypsa.org/ · https://pypsa-eur.readthedocs.io/
- pandapipes (gas and district heating networks): https://pandapipes.readthedocs.io/
- SciPy `linprog` (default method `highs`): https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linprog.html
- ARCEP "Sites indisponibles" (daily history; temporal coverage not stated on the page): https://www.data.gouv.fr/datasets/sites-indisponibles
