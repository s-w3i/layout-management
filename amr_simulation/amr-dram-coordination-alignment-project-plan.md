# AMR Simulation–DRAM Coordination Alignment Project Plan

## 1. Project summary

This project will align the multi-robot coordination behavior in
`layout-management/amr_simulation` with the behavior of the
`dram_ws/field-testing` coordination stack while preserving the speed,
determinism, and batch-processing advantages of a discrete-event simulator.

The simulator will reproduce DRAM coordination as an event-driven protocol. It
will not run ROS 2 nodes, poll robot state continuously, or advance time in
fixed-size simulation steps.

## 2. Scope

### 2.1 In scope

- Five-node, straight-segment reservation windows
- Periodic allocation semantics without continuous time stepping
- Reservation release after simulated controller acknowledgement
- Windowed head-to-head and path-overlap detection
- Partial-cycle and wait-for deadlock detection
- Corridor deadlock detection and corridor-side arbitration
- Loaded-robot-first reservation ordering
- Field-compatible wait-versus-replan decisions
- Directed-edge tabu constraints
- Replanning feasibility checks
- Explicit deadlock and degraded-fallback reporting
- Coordination parity fixtures and deterministic regression tests
- Performance profiling for batch simulation

### 2.2 Explicitly out of scope

The following differences are intentionally excluded from this project:

1. Active path-planner selection and path-cost parity
2. Default robot/fleet scale
3. Task allocation policy
4. Dynamic shelf-return policy
5. Workstation processing and queue behavior

The existing deterministic route generator, task dispatcher, fixed shelf-home
behavior, workstation model, and configurable AMR count will remain in place.

## 3. Goals and success criteria

### 3.1 Functional goals

- Produce the same reservation grants as the DRAM coordination contract for
  defined golden scenarios.
- Produce the same conflict classification for head-to-head, overlap, partial
  cycle, wait-for deadlock, and corridor deadlock scenarios.
- Select the same wait/replan participant under the configured field priority
  policy.
- Apply tabu constraints to directed edges instead of unnecessarily blocking
  entire nodes.
- Retain reservations until a simulated acknowledgement is received.
- Expose unresolved deadlocks instead of silently reporting successful runs.

### 3.2 Performance goals

- Preserve heap-based discrete-event execution.
- Avoid empty one-second allocation events when no robot needs coordination.
- Keep coordination processing approximately proportional to traversed edges
  and relevant allocation epochs.
- Complete representative 40-AMR batch runs within 20% of the current runtime,
  excluding detailed event-log generation.
- Keep deterministic runs byte-stable for identical inputs and configuration.

### 3.3 Acceptance criteria

The project is complete when:

- All coordination golden scenarios pass.
- Existing routing, input-validation, and simulation tests remain green.
- No node is occupied by two robots at the same simulated time.
- No opposing edge traversal is permitted without an explicit resolution.
- Reservation lookahead never exceeds the configured field horizon.
- Deadlocked runs finish with `FAILED_DEADLOCK` or `DEGRADED`, never an
  unqualified success.
- Batch performance meets the agreed runtime threshold.
- Debug playback remains consistent with the event log.

## 4. Design principles

### 4.1 Model behavior, not ROS middleware

The simulation will implement the observable coordination contract as pure
Python state transitions. ROS publishers, subscribers, services, quality-of-
service behavior, and wall-clock executors will not be reproduced.

### 4.2 Separate coordination permission from physical motion

A reservation grant authorizes future node entry; it does not start a new
rest-to-rest journey. The motion model must not force a robot to stop every time
its five-node reservation window is extended.

### 4.3 Make timing assumptions explicit

Allocation period, acknowledgement latency, conflict windows, replan delay,
and deadlock timeout must be configuration values. No field timing constant
should be hidden in engine code.

### 4.4 Preserve deterministic ordering

Events sharing a timestamp will use a documented priority order. Robot IDs will
be final tie-breakers wherever field execution would otherwise be asynchronous.

### 4.5 Never conceal degraded coordination

Serialization may be offered as an optional batch-completion mechanism, but a
run that uses it must be marked degraded and excluded from valid layout
comparison unless the caller explicitly opts in.

## 5. Proposed architecture

The implementation will add a coordination layer between stage routing and
motion execution:

1. The existing engine requests a complete stage route.
2. The coordinator records the route and queues a reservation request.
3. A relevant allocation epoch grants a bounded path prefix.
4. Motion proceeds only through the authorized frontier.
5. Node crossing produces a delayed acknowledgement event.
6. Acknowledgement releases the previous node and may request a window top-up.
7. Path or reservation changes trigger conflict analysis.
8. The resolver produces `wait`, `replan`, or unresolved-deadlock outcomes.

### 5.1 New module

Create `amr_simulation/coordination.py` containing:

- `CoordinationConfig`
- `RobotCoordinationState`
- `CoordinationState`
- `Conflict`
- `ReservationDecision`
- Reservation-window construction
- Allocation ordering
- Conflict detectors
- Resolution selection
- Replanning-feasibility checks

The module must not import GUI, CLI, or ROS packages.

### 5.2 Per-robot coordination state

Each active robot will track:

- Complete planned stage path
- Current acknowledged path index
- Current physical path index
- Furthest authorized path index
- Reserved/movement buffer
- Loaded or unloaded state
- Current conflict resolution state
- Wait start time
- Last acknowledgement time
- Directed tabu edges
- Current wait-for dependencies

### 5.3 Global coordination state

The coordinator will track:

- Node-to-owner reservation map
- Pending reservation requests
- Scheduled allocation epochs
- Robot wait-for graph
- Active conflicts
- Conflict-resolution history
- Deadlock state
- Coordination metrics

## 6. Event model

### 6.1 New event types

- `coordination_route_registered`
- `reservation_request`
- `allocation_epoch`
- `reservation_granted`
- `reservation_denied`
- `motion_frontier_reached`
- `node_crossing`
- `controller_acknowledgement`
- `conflict_scan`
- `conflict_resolution`
- `replan_requested`
- `replan_completed`
- `deadlock_declared`
- `degraded_serialization_started`

### 6.2 Same-timestamp event priority

Use this deterministic order:

1. Physical node crossings
2. Controller acknowledgements and releases
3. Conflict scans
4. Conflict resolutions
5. Replanning completion
6. Allocation epochs
7. Motion continuation/start
8. Reporting-only events

The priority must be implemented as part of the event-calendar key rather than
depending on insertion order.

### 6.3 Sparse allocation epochs

The field allocator's one-second period will be represented by scheduling only
the next relevant epoch:

```python
epoch = ceil(request_time / allocation_period) * allocation_period
```

An epoch is scheduled only when at least one robot has a pending request. Empty
periods are skipped. Requests arriving in the same period are processed as one
deterministic batch.

## 7. Detailed workstreams

### Workstream A — Configuration and data models

#### Changes

- Add a nested coordination configuration to `amr_simulation/models.py`.
- Add a named field-alignment profile to
  `amr_simulation/config/default.json`.
- Add validation for all periods, windows, horizons, latency, and policy values.
- Include coordination configuration in result snapshots.

#### Initial defaults

```json
{
  "coordination": {
    "profile": "dram_field_v1",
    "allocation_period_seconds": 1.0,
    "max_reservation_nodes": 5,
    "head_to_head_window": 3,
    "partial_cycle_window": 5,
    "deadlock_window": 5,
    "blocked_replan_seconds": 5.0,
    "acknowledgement_latency_seconds": 0.25,
    "allocation_order": "ascPathLength",
    "tabu_mode": "directed_edge",
    "deadlock_timeout_seconds": 30.0,
    "deadlock_policy": "fail"
  }
}
```

#### Acceptance criteria

- Invalid configuration fails before simulation begins.
- Configuration snapshots contain every coordination parameter.
- Existing configuration files receive backward-compatible defaults.

### Workstream B — Directed-edge routing constraints

#### Changes

- Extend `GridRouter.route()` to accept `blocked_edges` in addition to
  `blocked_nodes`.
- Reject only the specified direction during graph expansion.
- Add blocked edges to the route-cache key.
- Retain blocked-node support for rack obstacles and genuine physical closures.
- Clear stage-local tabu edges at the same lifecycle boundary as the field path.

#### Acceptance criteria

- Blocking `A -> B` does not block `B -> A`.
- Blocking `A -> B` does not block `C -> B`.
- Replanning uses the next valid directed alternative.
- Route caching cannot return a path calculated under different constraints.

### Workstream C — Bounded reservations

#### Changes

- Port the straight-segment window logic.
- Limit each request to five path nodes under the field profile.
- Confirm through fixtures whether the current occupied node consumes one of
  the five positions.
- Grant a safe prefix rather than requiring the full window atomically.
- Support reservation top-ups while a robot is already moving.
- Remove the simulator-only immediate-turn atomic reservation rule unless field
  fixtures demonstrate equivalent behavior.

#### Acceptance criteria

- No grant exceeds the configured maximum.
- A blockage preserves already granted prefix nodes.
- A robot can extend its window before reaching the current frontier.
- Same-direction following does not force unnecessary replanning.

### Workstream D — Motion and acknowledgement separation

#### Changes

- Track physical and acknowledged path indices separately.
- Schedule only the next authorized crossing in batch mode.
- Schedule `controller_acknowledgement` after crossing using configured latency.
- Release the previous node only on acknowledgement.
- Request a reservation top-up after acknowledgement when necessary.
- Stop at the authorization frontier only when no extension has arrived.
- Preserve continuous travel when the frontier is extended early.

#### Acceptance criteria

- Reservations remain held between crossing and acknowledgement.
- The robot never enters an unreserved node.
- Extending a reservation does not create an artificial full stop.
- Reaching a blocked frontier generates waiting time and not teleportation.

### Workstream E — Conflict detection

#### Changes

- Refactor `amr_simulation/conflict_solver.py` into pure, independently tested
  detectors.
- Implement general path overlap.
- Implement windowed head-to-head detection.
- Implement partial next-move cycle detection.
- Maintain a wait-for graph and detect directed cycles.
- Implement corridor-side grouping and opposing-queue detection.
- Trigger scans only after relevant route, reservation, or position changes.

#### Acceptance criteria

- Each conflict class is distinguishable in logs and metrics.
- All robots involved in a multi-robot cycle are reported.
- Nonconflicting following paths do not produce false head-to-head conflicts.
- Conflict detection results are independent of dictionary iteration order.

### Workstream F — Conflict resolution

#### Changes

- Implement loaded-robot-first allocation groups.
- Within each group, implement `ascPathLength` ordering.
- Use robot priority and robot ID as deterministic tie-breakers.
- Implement `can_replan()` using adjacent-node availability and loaded-rack
  restrictions.
- Resolve head-to-head and deadlock conflicts by replanning the lowest-priority
  eligible robot.
- Resolve path overlap through waiting unless escalation is required.
- Resolve corridor conflicts by selecting one yielding side.
- Add the selected directed conflict edge to the yielding robot's tabu set.

#### Acceptance criteria

- Equivalent input state always produces the same selected yielding robot.
- Loaded robots are processed before unloaded robots.
- An immobile robot is never selected to replan when an eligible alternative
  exists.
- Corridor resolution operates on a side/queue, not an arbitrary single robot.

### Workstream G — Deadlock and fallback policy

#### Changes

- Replace unconditional automatic serialization with explicit policies.
- In `fail` mode, terminate the run with a complete deadlock snapshot.
- In `degraded_serialize` mode, serialize only after recording the deadlock.
- Mark degraded runs in summary and comparison output.
- Exclude degraded runs from normal winner selection by default.

#### Required deadlock snapshot

- Simulation timestamp
- Involved AMRs and jobs
- Current and acknowledged nodes
- Reserved nodes and owners
- Wait-for graph
- Current route windows
- Loaded state
- Tabu edges
- Conflict classification
- Last progress timestamp

#### Acceptance criteria

- `deadlock_count` reflects detected deadlocks.
- A serialized run cannot be reported as fully valid.
- Debug mode exposes enough state to reproduce the deadlock.

### Workstream H — Metrics and observability

#### New metrics

- Allocation epoch count
- Reservation requests, grants, and denials
- Mean and maximum granted-window size
- Acknowledgement delay total
- Reservation hold-after-crossing time
- Conflict count by type
- Wait and replan resolution count
- Directed tabu-edge count
- Replan success/failure count
- Wait-for deadlock count
- Corridor deadlock count
- Authorization-frontier stop count and duration
- Coordination status: `VALID`, `DEGRADED`, or `FAILED_DEADLOCK`

Detailed event rows remain optional to protect batch performance.

## 8. Test plan

### 8.1 Unit tests

- Straight-segment detection
- Five-node window truncation
- Partial prefix grant
- Same-timestamp allocation ordering
- Directed-edge tabu behavior
- Acknowledgement-delayed release
- Loaded/unloaded priority grouping
- `can_replan()` eligibility
- Wait-for graph construction
- Every conflict detector independently

### 8.2 Golden coordination scenarios

Create compact graph fixtures for:

1. Two robots following in the same direction
2. Two robots approaching head-to-head
3. Crossing paths at an intersection
4. Three-robot cyclic wait
5. Four-robot wait-for cycle
6. Opposing queues in a single-width corridor
7. Loaded versus unloaded contention
8. Directed-edge tabu reroute
9. Five-node window truncation
10. Delayed acknowledgement while another robot waits
11. Simultaneous requests in one allocation epoch
12. No robot with a feasible replanning escape

Each fixture must define expected:

- Allocation order
- Reservation ownership after every epoch
- Conflict type and participants
- Wait/replan selection
- Directed tabu edge
- Node-release sequence
- Terminal coordination status

### 8.3 Differential parity tests

Capture coordination-contract fixtures from `dram_ws/field-testing` rather than
importing ROS modules into the simulator. Each fixture should contain serialized
input paths, positions, loaded states, priority order, reservations, conflicts,
and expected resolutions.

The simulator must reproduce the expected decisions using pure Python.

### 8.4 Integration tests

- One complete pickup–delivery–return job
- Multiple jobs sharing an aisle
- Multiple loaded AMRs approaching a workstation route
- Replanning after reservation timeout
- Strict deadlock termination
- Degraded serialization and output labeling
- Batch run with detailed logging disabled
- Debug replay matching event positions and reservations

### 8.5 Performance tests

Benchmark at least:

- 4 AMRs on a compact field-style map
- 20 AMRs on the warehouse map
- 40 AMRs for one representative order date
- Four-layout parallel comparison

Record wall time, event count, allocation epochs, conflict scans, peak memory,
and route-cache hit rate.

## 9. Implementation phases

### Phase 0 — Parity contract and baselines

Deliverables:

- Documented field coordination contract
- Initial golden fixtures
- Current simulator runtime and metrics baseline
- Decision on whether the current node counts toward the five-node horizon
- Defined same-timestamp event ordering

Exit gate: ambiguous field behaviors are represented by an explicit chosen
contract rather than assumptions hidden in code.

### Phase 1 — Configuration and directed-edge routing

Deliverables:

- Coordination configuration model
- Directed-edge tabu routing
- Route-cache updates
- Unit tests

Exit gate: directed-edge tests pass and existing static routing remains
unchanged without tabu edges.

### Phase 2 — Event-driven reservation coordinator

Deliverables:

- New coordination module
- Sparse allocation epochs
- Five-node reservation windows
- Reservation top-ups
- Deterministic allocation ordering

Exit gate: reservation golden scenarios match expected ownership transitions.

### Phase 3 — Acknowledgement-aware motion integration

Deliverables:

- Separate physical and acknowledged position state
- Delayed release events
- Authorization-frontier handling
- Continuous-motion preservation across early top-ups

Exit gate: no unauthorized entry or artificial window-boundary stop occurs.

### Phase 4 — Complete conflict detection and resolution

Deliverables:

- Full detector set
- Wait-for graph
- Corridor arbitration
- Replan feasibility and directed tabu application
- Conflict metrics

Exit gate: conflict and resolution golden fixtures pass.

### Phase 5 — Deadlock policy and reporting

Deliverables:

- Strict failure mode
- Degraded serialization mode
- Deadlock snapshots
- Comparison-output validity flags

Exit gate: no deadlock can be silently converted into a valid successful run.

### Phase 6 — Regression, profiling, and rollout

Deliverables:

- Full test suite
- Performance benchmark report
- Debug replay validation
- Migration notes and configuration documentation

Exit gate: functional acceptance criteria and performance targets are met.

## 10. Recommended pull-request sequence

1. `Add coordination configuration and directed-edge tabu routing`
2. `Introduce pure event-driven DRAM coordinator`
3. `Add bounded reservation epochs and top-ups`
4. `Model acknowledgement-based reservation release`
5. `Port DRAM conflict detection and arbitration`
6. `Expose deadlock and degraded coordination outcomes`
7. `Add DRAM parity fixtures and performance benchmarks`

Keep each pull request independently testable. Do not combine routing-cache,
event-calendar, conflict-resolution, and result-schema changes into one review.

## 11. Risks and mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| ROS callback order is nondeterministic | Exact event sequence cannot be copied | Define and document a deterministic simulator ordering |
| Reservation windows fragment motion | Artificial stops inflate travel time | Decouple authorization frontier from motion trajectory |
| Route cache grows with tabu combinations | Memory and runtime regression | Use immutable small edge sets, bounded stage-local tabu, and cache metrics |
| Corridor logic over-detects conflicts | Excessive replanning | Validate against explicit field fixtures and negative cases |
| Serialization hides algorithm failure | Misleading throughput | Mark runs degraded and exclude by default |
| Fixed acknowledgement latency is unrealistic | Timing mismatch remains | Make latency configurable, then calibrate from logs |
| Ported logic drifts from field code | Parity degrades over time | Maintain versioned coordination fixtures and periodic differential checks |

## 12. Rollout strategy

Introduce the coordinator behind a profile switch:

```text
coordination.profile = legacy_v1
coordination.profile = dram_field_v1
```

Rollout order:

1. Preserve `legacy_v1` as the default during development.
2. Run both profiles in CI on compact deterministic cases.
3. Compare layout-ranking stability and runtime on representative dates.
4. Make `dram_field_v1` the default after parity and performance gates pass.
5. Retain `legacy_v1` temporarily for result reproducibility.
6. Deprecate the legacy profile only after stored benchmark results have been
   versioned with their coordination profile.

## 13. Definition of done

- Code is organized around a pure coordinator with no ROS dependency.
- All scoped DRAM coordination behaviors have golden tests.
- Reservations are bounded and allocated at sparse field-equivalent epochs.
- Releases occur through acknowledgement events.
- Conflict detection includes windowed, cyclic, wait-for, and corridor cases.
- Replanning uses directed tabu edges and field-compatible eligibility.
- Deadlocks are visible and correctly classified.
- Batch speed remains within the agreed performance budget.
- Results record the active coordination profile and all relevant parameters.
- Documentation explains remaining out-of-scope differences.

## 14. Current-to-target implementation map

| Current component | Current responsibility | Target change |
|---|---|---|
| `amr_simulation/models.py` | Simulation and motion configuration | Add validated coordination configuration and outcome enums |
| `amr_simulation/routing.py` | Static A* route and motion timing | Accept directed blocked edges; preserve the existing route objective |
| `amr_simulation/conflict_solver.py` | Simplified head-to-head and immediate-cycle checks | Become a complete set of pure DRAM conflict detectors |
| `amr_simulation/engine.py` | Dispatch, reservations, movement, queues, metrics | Delegate coordination decisions and schedule coordination events |
| `amr_simulation/results.py` | CSV and JSON output | Add coordination status, conflict metrics, and deadlock snapshots |
| `amr_simulation/config/default.json` | Default run parameters | Add versioned coordination profile values |
| `tests/test_amr_simulation.py` | End-to-end simulator behavior | Retain broad integration coverage |
| New `tests/test_amr_coordination.py` | Not present | Unit, golden, and deterministic coordination tests |
| New `tests/fixtures/dram_coordination/` | Not present | Versioned field-contract fixtures |

The first refactor should preserve current behavior behind `legacy_v1`. The
field profile is then implemented incrementally without forcing every change
through the existing `_Engine` reservation methods.

## 15. Required invariants

The implementation must assert these invariants in debug/test mode and validate
them at key event boundaries:

### 15.1 Reservation invariants

1. A node has zero or one reservation owner.
2. Every physically occupied node is reserved by the occupying robot.
3. A robot enters only a node present in its authorized path prefix.
4. A robot's reserved path is a contiguous prefix of its current planned path.
5. A reservation grant contains no more than `max_reservation_nodes` considered
   positions under the chosen field contract.
6. A robot never releases a previously occupied node before acknowledgement.
7. Reservations belonging to a completed or cancelled job are removed.

### 15.2 Path invariants

1. Every consecutive path pair is a valid directed graph edge.
2. The current physical node appears in the active path.
3. Physical, acknowledged, and authorized indices are monotonically
   non-decreasing within one path revision.
4. Replanning creates a new path revision and resets indices atomically.
5. A directed tabu edge cannot occur in the newly accepted path.
6. Stage-local tabu state is cleared only at the defined stage boundary.

### 15.3 Conflict invariants

1. Every reported conflict references active robots and valid path positions.
2. A conflict resolution references the conflict revision it resolves.
3. Stale resolution events cannot mutate a newer path revision.
4. A robot cannot simultaneously be in `MOVING` and `WAITING_RESOLUTION`.
5. A wait-for edge is removed when its blocking reservation disappears.
6. A declared wait-for deadlock contains an actual directed cycle.

### 15.4 Determinism invariants

1. All unordered collections are sorted before affecting decisions.
2. Event ordering includes timestamp, event priority, and monotonic sequence.
3. Robot ID is the final arbitration tie-breaker.
4. Fixed configuration and inputs produce identical summaries and event logs.

## 16. Proposed public and internal APIs

### 16.1 Configuration types

```python
class DeadlockPolicy(str, Enum):
    FAIL = "fail"
    DEGRADED_SERIALIZE = "degraded_serialize"


class CoordinationStatus(str, Enum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    FAILED_DEADLOCK = "FAILED_DEADLOCK"


@dataclass(frozen=True, slots=True)
class CoordinationConfig:
    profile: str = "dram_field_v1"
    allocation_period_seconds: float = 1.0
    max_reservation_nodes: int = 5
    head_to_head_window: int = 3
    partial_cycle_window: int = 5
    deadlock_window: int = 5
    blocked_replan_seconds: float = 5.0
    acknowledgement_latency_seconds: float = 0.25
    allocation_order: str = "ascPathLength"
    tabu_mode: str = "directed_edge"
    deadlock_timeout_seconds: float = 30.0
    deadlock_policy: DeadlockPolicy = DeadlockPolicy.FAIL
```

### 16.2 Coordinator API

```python
class DramCoordinator:
    def register_route(
        self,
        robot_id: str,
        path: tuple[GridPosition, ...],
        *,
        loaded: bool,
        now: float,
        path_revision: int,
    ) -> list[CoordinationCommand]: ...

    def request_reservation(
        self,
        robot_id: str,
        now: float,
    ) -> list[CoordinationCommand]: ...

    def run_allocation_epoch(
        self,
        now: float,
    ) -> list[CoordinationCommand]: ...

    def acknowledge_crossing(
        self,
        robot_id: str,
        previous: GridPosition,
        reached: GridPosition,
        *,
        now: float,
        path_revision: int,
    ) -> list[CoordinationCommand]: ...

    def complete_route(
        self,
        robot_id: str,
        now: float,
    ) -> list[CoordinationCommand]: ...

    def snapshot(self) -> CoordinationSnapshot: ...
```

The coordinator returns commands instead of scheduling engine events directly.
This keeps it independently testable.

### 16.3 Coordinator commands

```python
CoordinationCommand = (
    ScheduleAllocationEpoch
    | GrantReservation
    | DenyReservation
    | ContinueMotion
    | HoldPosition
    | RequestReplan
    | ReleaseNode
    | DeclareDeadlock
    | StartDegradedSerialization
)
```

The engine translates commands into calendar events and metrics. The
coordinator must not access the event heap.

### 16.4 Conflict detector API

```python
def detect_conflicts(
    snapshots: tuple[RobotPathSnapshot, ...],
    wait_for: Mapping[str, frozenset[str]],
    graph: GridRouter,
    config: CoordinationConfig,
) -> tuple[Conflict, ...]: ...
```

Conflict results should be sorted by:

1. Conflict severity/type order
2. Earliest path step
3. Sorted participant IDs
4. Conflict edge/node

## 17. State definitions

### 17.1 Robot coordination phase

```python
class CoordinationPhase(str, Enum):
    IDLE = "IDLE"
    NEEDS_RESERVATION = "NEEDS_RESERVATION"
    RESERVED = "RESERVED"
    MOVING = "MOVING"
    WAITING_RESERVATION = "WAITING_RESERVATION"
    WAITING_RESOLUTION = "WAITING_RESOLUTION"
    REPLANNING = "REPLANNING"
    DEADLOCKED = "DEADLOCKED"
```

Allowed transitions:

| From | Event | To |
|---|---|---|
| `IDLE` | route registered | `NEEDS_RESERVATION` |
| `NEEDS_RESERVATION` | grant received | `RESERVED` |
| `NEEDS_RESERVATION` | denial received | `WAITING_RESERVATION` |
| `RESERVED` | motion starts | `MOVING` |
| `MOVING` | frontier reached | `WAITING_RESERVATION` |
| `MOVING` | route completed | `IDLE` |
| `WAITING_RESERVATION` | conflict resolution needed | `WAITING_RESOLUTION` |
| `WAITING_RESOLUTION` | wait selected | `WAITING_RESERVATION` |
| `WAITING_RESOLUTION` | replan selected | `REPLANNING` |
| `REPLANNING` | route accepted | `NEEDS_RESERVATION` |
| Any active phase | unresolved deadlock | `DEADLOCKED` |

Invalid transitions must raise in tests and emit a structured simulation error
in production runs.

### 17.2 Path revisioning

Every initial route and replan receives an incrementing `path_revision`.
Crossing, acknowledgement, conflict, and resolution events carry the revision.
An event whose revision does not match the robot's current revision is stale and
must be ignored with a metric increment.

This prevents a delayed acknowledgement or resolution from mutating a newer
route.

## 18. Event-calendar specification

### 18.1 Calendar key

Use:

```python
CalendarKey = tuple[float, int, int]
# (simulated_time, event_priority, insertion_sequence)
```

Do not rely on payload comparison or dictionary ordering in the heap.

### 18.2 Recommended event priorities

```python
EVENT_PRIORITY = {
    "node_crossing": 10,
    "controller_acknowledgement": 20,
    "reservation_release": 30,
    "conflict_scan": 40,
    "conflict_resolution": 50,
    "replan_completed": 60,
    "allocation_epoch": 70,
    "motion_continue": 80,
    "metrics_checkpoint": 90,
}
```

### 18.3 Allocation epoch deduplication

Maintain:

```python
scheduled_allocation_epoch: float | None
```

When a new request arrives:

```python
def ensure_allocation_epoch(now: float) -> None:
    epoch = next_epoch(now)
    if scheduled_allocation_epoch is None or epoch < scheduled_allocation_epoch:
        schedule(epoch, "allocation_epoch", None)
        scheduled_allocation_epoch = epoch
```

After processing the epoch, clear the marker and schedule the next epoch only
if pending requests remain.

### 18.4 Exact-boundary rule

Choose and test one rule for requests at an exact epoch boundary. Recommended:

- State-changing events at time `t` run before the allocation epoch at `t`.
- Therefore, a request produced by an acknowledgement at exactly `t` is
  eligible for the allocation epoch at `t`.

This follows the declared event priority and avoids an unnecessary additional
one-second wait.

## 19. Reservation algorithm specification

### 19.1 Candidate window

```python
def candidate_window(robot, graph, max_nodes):
    path = robot.planned_path
    start = robot.next_unreserved_index
    if start >= len(path):
        return ()

    result = []
    previous_direction = None

    for index in range(start, len(path)):
        if len(result) >= max_nodes:
            break

        if index > start:
            direction = graph.direction(path[index - 1], path[index])
            if previous_direction is not None and direction != previous_direction:
                break
            previous_direction = direction
        elif index > 0:
            previous_direction = graph.direction(path[index - 1], path[index])

        result.append(index)

    return tuple(result)
```

Phase 0 must settle whether `start` refers to the current node or first forward
node. The implementation must encode the answer in one function and one fixture,
not distribute offset arithmetic through the engine.

### 19.2 Prefix grant

```python
def grant_prefix(robot, candidates, node_owners):
    granted = []
    blocker = None

    for index in candidates:
        node = robot.planned_path[index]
        owner = node_owners.get(node)
        if owner is not None and owner != robot.robot_id:
            blocker = (node, owner)
            break
        granted.append(index)

    return tuple(granted), blocker
```

After node-ownership screening, conflict screening may shorten the prefix
further. Every retained index is then committed to `node_owners` as one atomic
state update.

### 19.3 Wait-for updates

On denial:

```python
wait_for[requesting_robot].add(blocking_robot)
```

On release, replan, cancellation, or successful grant beyond the blocker,
remove obsolete dependencies. Rebuild a robot's wait-for set from its current
blockers rather than accumulating historical edges indefinitely.

## 20. Conflict semantics

### 20.1 Same-direction following

Two paths sharing nodes in the same direction are not automatically a
head-to-head conflict. Node ownership and reservation distance provide spacing.
The trailing robot waits without adding a tabu edge unless the blocked timeout
or another conflict class requires escalation.

### 20.2 Head-to-head

Within the configured horizon, detect opposing traversal of the same directed
edge pair:

```text
Robot A: X -> Y
Robot B: Y -> X
```

Extended reversed overlap should report the earliest opposing edge and the
complete participant set.

### 20.3 Path overlap

Report shared future nodes when timing/order does not yet form head-to-head.
The normal first action is `wait`, allowing reservations to serialize access.
Escalate only after the configured blocked duration or if a wait-for cycle
forms.

### 20.4 Partial cycle

Build intended next moves from the configured path window:

```text
A waits for B's node
B waits for C's node
C waits for A's node
```

Report every robot in the cycle. Do not reduce the conflict to only the robot
whose request happened to trigger detection.

### 20.5 Wait-for deadlock

Run directed-cycle detection on `wait_for`. Use Tarjan strongly connected
components or deterministic DFS. Any component with more than one robot, or a
self-loop, is a deadlock candidate.

Before declaring deadlock, confirm:

- Dependencies still correspond to current reservations.
- No involved robot has progressed since the candidate began.
- The condition has persisted for the configured deadlock window/timeout.

### 20.6 Corridor deadlock

Represent a single-width corridor as an ordered sequence of graph nodes. Group
robots by entry side and travel direction. When opposing groups cannot pass:

1. Identify the front robot on each side.
2. Count followers behind each front robot.
3. Determine whether each front robot can replan or back out.
4. Prefer yielding the side with an escapable front and fewer followers.
5. Use priority order as the next tie-breaker.
6. Apply replan to the yielding front or side according to the fixture contract.

Corridor identification may initially use an explicit test/config annotation.
Automatic graph-degree inference should be a later optimization because branch
nodes and rack spurs can make naive degree-based inference unreliable.

## 21. Resolution algorithm specification

### 21.1 Allocation order

```python
def allocation_key(robot):
    return (
        0 if robot.loaded else 1,
        remaining_path_length(robot),
        -robot.priority,
        robot.robot_id,
    )
```

`remaining_path_length` should count graph nodes for field parity with
`ascPathLength`, not metric distance.

### 21.2 Replan candidate order

For conflicts requiring one robot to yield:

1. Sort participants from lowest to highest effective priority.
2. Select the first robot for which `can_replan()` is true.
3. If no robot can replan, retain the conflict and start/continue its deadlock
   persistence timer.

### 21.3 Replan lifecycle

```text
WAITING_RESOLUTION
  -> add directed tabu edge
  -> increment path revision
  -> call existing router with blocked_edges
  -> validate path
  -> release obsolete lookahead reservations
  -> register new path
  -> request allocation at next eligible epoch
```

The robot's currently occupied node remains reserved during replanning.
Reservations shared by the old and new path may be retained; obsolete future
reservations must be released deterministically.

### 21.4 Replan failure

If no route exists with the new tabu edge:

1. Do not silently clear every tabu constraint and retry the original path.
2. Remove only the newly proposed edge if the field contract requires rollback.
3. Try the next eligible conflict participant, when applicable.
4. Otherwise retain the conflict and let the deadlock policy decide.

Every failed attempt increments `coordination.replan_failed_count`.

## 22. Motion integration design

### 22.1 Required state

Track:

- Current physical node
- Previous physical node awaiting acknowledgement
- Current velocity/heading or current motion-phase reference
- Next authorized node
- Authorization frontier
- Scheduled crossing token
- Path revision

### 22.2 Avoiding artificial stops

When a reservation top-up extends the frontier before the robot reaches it,
the existing motion continues. Do not schedule a new acceleration phase merely
because authorization increased.

When the frontier is reached without extension:

1. Finish motion at the frontier.
2. Record an `authorization_frontier_stop`.
3. Wait for a later grant.
4. Resume using the existing acceleration model.

### 22.3 Event cancellation

Heap events will not be physically removed. Give motion and acknowledgement
events tokens containing robot ID, path revision, and motion sequence. Ignore
events whose token no longer matches current robot state.

This avoids expensive heap deletion during replanning.

### 22.4 Batch versus debug mode

Batch mode:

- Schedule node crossings and aggregate timing.
- Avoid retaining detailed motion segments.

Debug mode:

- Generate rotation and translation segments.
- Preserve reservation and acknowledgement markers for playback.
- Display authorized frontier, reserved nodes, and active tabu edges.

## 23. Result schema additions

Add a versioned coordination block to `summary.json`:

```json
{
  "coordination": {
    "profile": "dram_field_v1",
    "status": "VALID",
    "allocation_epoch_count": 0,
    "reservation_request_count": 0,
    "reservation_grant_count": 0,
    "reservation_denial_count": 0,
    "mean_granted_window_nodes": 0.0,
    "max_granted_window_nodes": 0,
    "frontier_stop_count": 0,
    "frontier_wait_seconds": 0.0,
    "acknowledgement_delay_seconds": 0.0,
    "replan_requested_count": 0,
    "replan_completed_count": 0,
    "replan_failed_count": 0,
    "tabu_edge_count": 0,
    "conflicts": {
      "path_overlap": 0,
      "head_to_head": 0,
      "partial_cycle": 0,
      "wait_for_deadlock": 0,
      "corridor_deadlock": 0
    },
    "deadlock_count": 0,
    "serialization_fallback_used": false,
    "stale_event_count": 0
  }
}
```

If the run fails, write a separate `deadlock_snapshot.json` and reference it
from the summary.

## 24. Golden fixture format

Use JSON fixtures independent of Python object internals:

```json
{
  "schema": "dram_coordination_fixture/v1",
  "name": "two_robot_head_to_head",
  "config": {
    "max_reservation_nodes": 5,
    "head_to_head_window": 3
  },
  "graph": {
    "nodes": ["A", "B", "C", "D"],
    "directed_edges": [
      ["A", "B"], ["B", "A"],
      ["B", "C"], ["C", "B"],
      ["C", "D"], ["D", "C"]
    ]
  },
  "robots": [
    {
      "id": "R1",
      "loaded": true,
      "current_node": "A",
      "path": ["A", "B", "C"]
    },
    {
      "id": "R2",
      "loaded": false,
      "current_node": "D",
      "path": ["D", "C", "B"]
    }
  ],
  "expected": {
    "conflict_type": "head_to_head",
    "participants": ["R1", "R2"],
    "resolution": {
      "R1": "continue",
      "R2": "replan"
    }
  }
}
```

Fixture runners should print a minimal state diff on failure: expected versus
actual ownership, wait-for edges, conflicts, and resolutions.

## 25. Performance engineering plan

### 25.1 Complexity budget

For `R` robots, conflict window `W`, and traversed edges `E`:

- Reservation allocation: approximately `O(R * W)` per relevant epoch
- Pairwise path conflict scan: `O(R^2 * W)`
- Wait-for cycle detection: `O(R + dependencies)`
- Movement processing: `O(E log events)` through the event heap

At `R = 40` and `W <= 5`, simple algorithms are preferable to complex indexes.

### 25.2 Route caching

Use two cache levels:

1. Static `(start, goal)` route cache.
2. Bounded constrained cache keyed by `(start, goal, tabu_edges)`.

Requirements:

- Stage-local tabu sets must remain small.
- Track constrained-cache size and hit rate.
- Apply an LRU capacity if profiling shows unbounded growth.
- Do not cache failed routes indefinitely if graph state may change.

### 25.3 Conflict-scan suppression

Maintain a monotonically increasing coordination-state revision. Schedule at
most one pending conflict scan per timestamp/revision. Multiple reservation or
release changes at the same time are evaluated as one batch.

### 25.4 Logging controls

Recommended levels:

- `none`: aggregate metrics only
- `conflicts`: grants, denials, conflicts, replans, deadlocks
- `full`: every request, crossing, acknowledgement, ownership change

Batch comparisons should default to `none` or `conflicts`, not `full`.

### 25.5 Benchmark gate

For each phase, compare against the recorded Phase 0 baseline:

| Benchmark | Maximum regression |
|---|---:|
| Compact 4-AMR scenario | 30% or 0.5 s, whichever is larger |
| One-date 20-AMR batch | 20% |
| One-date 40-AMR batch | 20% |
| Four-layout comparison | 20% |
| Peak memory | 25% |

Correctness gates take precedence over performance gates. Performance should be
optimized only after parity tests pass.

## 26. Detailed test matrix

| ID | Scenario | Expected detector | Expected action | Critical assertion |
|---|---|---|---|---|
| C01 | Same-direction leader/follower | None or overlap-only | Follower waits | No tabu edge |
| C02 | Two-node opposing swap | Head-to-head | Lower priority replans | Swap never executes |
| C03 | Extended reversed corridor | Head-to-head | One side yields | Earliest opposing edge reported |
| C04 | Three-node rotation | Partial cycle | Eligible robot replans | All participants reported |
| C05 | Persistent reservation cycle | Wait-for deadlock | Replan or fail | Persistence threshold respected |
| C06 | Two opposing corridor queues | Corridor deadlock | One side yields | Followers remain ordered |
| C07 | Loaded versus unloaded | Applicable conflict | Unloaded yields | Loaded-first policy |
| C08 | Both robots loaded | Applicable conflict | Path-length/priority tie-break | Deterministic winner |
| C09 | No adjacent escape | Deadlock candidate | Wait then fail/degrade | No impossible replan |
| C10 | Directed tabu | None after replan | Alternate direction/path | Reverse edge remains legal |
| R01 | Six-node straight request | None | Grant maximum five | Horizon enforced |
| R02 | Blocker at third node | Ownership | Grant safe prefix | First two remain granted |
| R03 | Early top-up | None | Extend authorization | No artificial stop |
| R04 | Late top-up | None | Stop then resume | Frontier wait measured |
| A01 | Delayed acknowledgement | Ownership | Previous node remains held | Release timestamp exact |
| A02 | Stale acknowledgement after replan | None | Ignore | Revision guard increments metric |
| E01 | Two requests same epoch | Varies | Ordered deterministic batch | Same result across runs |
| E02 | Request exactly at epoch | None | Eligible same epoch | Event priority honored |
| D01 | Strict deadlock | Deadlock | Terminate | Snapshot written |
| D02 | Degraded policy | Deadlock | Serialize | Status is `DEGRADED` |

## 27. Work breakdown and estimates

Estimates are engineering effort, not elapsed calendar promises. They assume one
developer familiar with the existing simulator and access to field-contract
examples.

| Work item | Estimate | Dependencies |
|---|---:|---|
| Field behavior contract and fixtures | 2–4 days | Access to representative DRAM states/logs |
| Configuration and state types | 1–2 days | Contract defaults |
| Directed-edge routing and cache tests | 1–2 days | None |
| Pure coordinator skeleton | 2–3 days | State types |
| Sparse allocation epochs | 2–3 days | Coordinator skeleton |
| Reservation windows and top-ups | 2–4 days | Allocation epochs |
| Acknowledgement-aware release | 2–4 days | Reservation integration |
| Motion/frontier integration | 3–5 days | Release semantics |
| Conflict detector suite | 4–7 days | Golden fixtures |
| Resolution and replan lifecycle | 3–5 days | Conflict detector, edge tabu |
| Corridor handling | 3–5 days | Corridor contract/annotations |
| Deadlock policy and snapshots | 2–3 days | Wait-for detector |
| Result schema and visual debugging | 2–4 days | Engine integration |
| Regression and performance tuning | 3–6 days | All functional work |

Indicative total: 27–50 developer-days. The largest uncertainty is not coding;
it is defining and validating the exact field coordination contract.

## 28. Pull-request review checklist

### Correctness

- [ ] Does every state mutation preserve the documented invariants?
- [ ] Are directed lanes and directed tabu edges handled independently?
- [ ] Can stale events affect a newer path revision?
- [ ] Can any robot enter an unreserved node?
- [ ] Can two robots own the same node?
- [ ] Are wait-for dependencies removed when blockers disappear?
- [ ] Are tie-breakers explicit and deterministic?

### Event simulation

- [ ] Does the change add unnecessary periodic events?
- [ ] Are equal-time event priorities tested?
- [ ] Are cancelled operations handled with tokens instead of heap deletion?
- [ ] Does debug tracing agree with batch state transitions?

### Performance

- [ ] Are paths represented compactly?
- [ ] Does a cache key contain every constraint affecting its result?
- [ ] Is detailed logging optional?
- [ ] Has the relevant benchmark been run?
- [ ] Is any new `O(R^2)` operation bounded by the conflict horizon?

### Results and safety

- [ ] Is degraded or failed coordination visible in summaries?
- [ ] Can invalid runs enter layout winner selection?
- [ ] Does a deadlock snapshot contain enough state to reproduce the failure?
- [ ] Are configuration values included in the snapshot?

## 29. Migration and compatibility

### 29.1 Existing result reproducibility

Every result must record `coordination.profile`. Old results without the field
profile should be interpreted as `legacy_v1`.

### 29.2 Configuration migration

When loading an existing config with no `coordination` section:

- Use `legacy_v1` during the compatibility period.
- Emit one nonfatal migration warning.
- Never silently reinterpret an old run as `dram_field_v1`.

### 29.3 Output migration

Add coordination fields without removing existing metrics. Where an existing
metric changes meaning, retain the old name under the legacy profile and add a
new unambiguous field rather than silently changing semantics.

### 29.4 Rollback

If the field profile causes unacceptable regression:

1. Switch the default profile back to `legacy_v1`.
2. Retain field-profile code and fixtures for diagnosis.
3. Do not rewrite or relabel stored results.
4. Report the failed acceptance gate and benchmark evidence.

## 30. Required design decisions before coding

Record these as short architecture decision records:

1. Does the current occupied node count toward `max_reservation_nodes`?
2. Does a partial reservation grant remain committed when a later node blocks?
3. At what exact event does field-equivalent node release occur?
4. Is acknowledgement latency fixed, trace-backed, or seeded-distributed?
5. Which path revision owns acknowledgements already in flight during replan?
6. How are corridors identified: explicit annotations or inferred topology?
7. Does corridor resolution replan only the front robot or the full yielding
   group?
8. What persistence interval converts a transient wait cycle into deadlock?
9. When a tabu-edge replan fails, which rollback/escalation order is correct?
10. Are degraded runs excluded from every comparison or permitted by an
    explicit CLI flag?

Coding should not begin on the affected subsystem until its decision is
resolved or deliberately represented as a configurable policy.

## 31. First implementation sprint backlog

### Sprint objective

Establish testable coordination foundations without changing default simulation
results.

### Tasks

1. Add `CoordinationConfig`, enums, validation, and snapshot serialization.
2. Add `legacy_v1` and `dram_field_v1` configuration profiles.
3. Extend `GridRouter.route()` with directed blocked edges.
4. Add route-cache correctness tests for node and edge constraints.
5. Create coordinator state and command dataclasses.
6. Add event priority to the engine calendar key.
7. Create the fixture schema and fixture loader.
8. Add the first four fixtures: following, head-to-head, three-cycle, and
   directed tabu.
9. Record the current runtime baseline for 4, 20, and 40 AMRs.
10. Add CI commands for coordination unit tests and benchmark smoke tests.

### Sprint exit criteria

- Legacy-profile results remain unchanged.
- Directed-edge routing tests pass.
- Coordination fixtures load and validate.
- Coordinator state can be constructed without engine or ROS dependencies.
- Equal-time event ordering is deterministic.
- Baseline benchmark artifacts are recorded.

## 32. Second implementation sprint backlog

### Sprint objective

Deliver bounded event-driven reservations and acknowledgement-based release.

### Tasks

1. Implement sparse allocation epochs.
2. Implement loaded-first `ascPathLength` ordering.
3. Implement straight-segment reservation windows.
4. Implement prefix grants and ownership denial.
5. Integrate authorization frontier with movement.
6. Implement delayed acknowledgement and release.
7. Implement reservation top-ups.
8. Add reservation, frontier, and acknowledgement metrics.
9. Complete R01–R04, A01–A02, and E01–E02 fixtures.
10. Profile event and cache growth.

### Sprint exit criteria

- No robot crosses an unauthorized node.
- Reservation grants match field fixtures.
- Release occurs at acknowledgement time.
- Early top-up preserves continuous motion.
- Runtime remains within the interim performance budget.

## 33. Third implementation sprint backlog

### Sprint objective

Complete conflict parity, deadlock transparency, and rollout readiness.

### Tasks

1. Implement all conflict detectors.
2. Implement wait-for graph maintenance.
3. Implement priority and feasibility-based resolution.
4. Integrate directed-edge replanning lifecycle.
5. Implement corridor annotations and arbitration.
6. Implement strict and degraded deadlock policies.
7. Write deadlock snapshots and comparison validity flags.
8. Complete C01–C10 and D01–D02 fixtures.
9. Run full simulator regression tests.
10. Run final 4-, 20-, and 40-AMR performance benchmarks.

### Sprint exit criteria

- All golden and differential fixtures pass.
- Deadlocks are correctly classified and reproducible.
- Degraded runs are excluded from default ranking.
- Debug playback matches coordination events.
- Final performance and functional acceptance gates pass.
