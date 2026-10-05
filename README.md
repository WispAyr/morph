# MORPH

MORPH is a reusable foundation for AI-native software across all projects. It is not a single app or a single domain. It is a general operating model for turning human intent into explicit state, policy, capability, and validated execution.

Crosspoint is the first proving ground, not the final scope. The same foundation can be applied to internal tools, production systems, automation workflows, agent orchestration, and product infrastructure.

## Phase 1 summary

This repository captures the core findings from the initial architecture review:

- MORPH is not another programming language.
- The right foundation is an AI-native IR with validator and execution semantics.
- The first realistic domain is Crosspoint, because it exposes live state, route safety, and operational failure clearly.
- The same pattern can scale to any product, workflow, or operational system.

## Included prototype

The repo currently contains a minimal Python implementation of MORPH-style runtime logic for route and operational decisions. It evaluates whether an action is allowed based on source state, destination state, capability checks, and policy rules.

See the Phase 1 findings in [docs/phase1-findings.md](docs/phase1-findings.md).

## Quick start

```bash
python -m pip install -e .[dev]
pytest -q
morph init demo.yaml
morph validate demo.yaml
morph check demo.yaml
morph run demo.yaml --set route.status=ready
morph run demo.yaml --set route.status=ready --set route.locked=true --explain
morph compile demo.yaml --target node
```

Installation needs a virtual environment or user site on Debian-based systems, because a dependency pins PyYAML and the system copy cannot be replaced.

The CLI gives MORPH a real developer workflow: initialize a YAML definition, scaffold a reusable project, evaluate it against a context, and compile it into Python, Node, or SQL execution targets. `morph run` exits 0 on an allow decision and 2 on a deny, so it can gate scripts directly.

## Definition format

```yaml
name: crosspoint
version: 0.3.0

entities:                      # the typed shape of the context
  - name: source
    fields: {status: string, latency_ms: int}
  - name: route
    fields: {locked: bool}
  - name: operator
    fields: {capabilities: list}

policies:                      # evaluated in order; first match decides
  - name: deny_locked
    when: route.locked
    result: {status: deny, action: raise_alert}
  - name: allow_live_route
    requires: [route_control]
    when: source.status == "live" && source.latency_ms < 120
    result: {status: allow, action: route_source}
```

Conditions are [CEL](https://cel.dev) expressions. CEL is typed, deterministic, and not Turing-complete, so a condition can always be checked and explained. It gives you `&&`, `||`, `!`, comparisons, `in`, `has()` for optional fields, `size()`, string functions, and the collection macros `all`, `exists`, `filter`, and `map`. The original structured clause form (`{field, equals|lt|lte|gt|gte|contains}`) is still accepted and is translated to CEL.

## Effects

Capabilities are the only path to side effects. A capability is a typed interface, an action binds a decision's action name to it, and an adapter implements it in code:

```yaml
capabilities:
  route_control:
    requires: [operator.capabilities]           # grant paths; [] means ungated
    inputs: {source: string, destination: string, operator: string}
    outputs: {route_id: string, previous_source: string}
    failures: [destination_locked, router_busy]  # codes the adapter may raise
    idempotency: [source, destination]           # inputs that identify a repeat
    retries: 2                                   # for retryable failures only

actions:
  route_source:
    capability: route_control
    inputs:                                      # CEL expressions over the context
      source: source.id
      destination: destination.id
      operator: operator.id
```

```python
from morph import EffectExecutor, EffectFailure

def route(inputs):                               # the adapter
    if locked(inputs["destination"]):
        raise EffectFailure("destination_locked", retryable=False)
    return {"route_id": ..., "previous_source": ...}

executor = EffectExecutor(runtime, {"route_control": route, "notify": notify})
result = executor.run(context)                   # evaluate, then execute
result.status                                    # executed | skipped | failed | denied | unbound
executor.log.records()                           # every attempt, with inputs, outputs, error, timing
```

The executor re-checks the capability grant before calling an adapter, type-checks inputs and outputs against the capability, skips a repeat with the same idempotency key, retries only failures the adapter marks retryable, and turns adapter exceptions into failure records rather than crashes. Once a definition declares `actions`, every allow policy must name a bound action, and this is checked at load. `morph run --adapters package.module:ATTR` executes from the command line. The worked example in `src/morph/examples/crosspoint.yaml` drives a fake studio router.

## Durable state

A `MORPHSystem` keeps state as an append-only event log, in memory or in a JSON Lines file. Observations, decisions, effect attempts, and state transitions are all events, and the current state of every entity instance is a fold over them, so a system can be rebuilt from the log alone and never re-runs an effect it already completed.

```yaml
entities:
  - name: destination
    fields: {id: string, status: string}
    states: [idle, routed, faulted]          # exposes destination.state to policies
    initial: idle
    transitions:
      - {on: route_source.succeeded, from: "*", to: routed}
      - {on: observed, to: faulted, when: 'event.fields.status == "faulted"'}
```

```python
from morph import EventStore, MORPHSystem

system = MORPHSystem(definition, adapters, EventStore("crosspoint.jsonl"))
system.observe("source", "cam1", status="live", latency_ms=40)
system.observe("destination", "wall", status="ready", latency_ms=60)
result = system.act(source="cam1", destination="wall", operator="ewan")
system.state("destination", "wall")          # {'id': 'wall', ..., 'state': 'routed'}
system.history(kinds=["transitioned"])
```

Transitions fire on `observed` and on `<action>.<outcome>` events (`succeeded`, `failed`, `skipped`, `denied`). Their `when` may read the entity, the rest of the context, and `event`. By default a transition targets the instance bound under its entity name in the context; `id: <expression>` targets another. YAML parses a bare `on:` key as boolean, which the loader tolerates, but quoting it as `"on":` avoids editor confusion. The same is available from the command line:

```bash
morph system crosspoint.yaml --store events.jsonl observe source cam1 status=live latency_ms=40
morph system crosspoint.yaml --store events.jsonl act --adapters morph.examples.crosspoint:adapters source=cam1 destination=wall operator=ewan
morph system crosspoint.yaml --store events.jsonl state destination wall
morph system crosspoint.yaml --store events.jsonl history --kind transitioned
```

## Semantic checks

`morph check` runs what the loader cannot: it finds policies that can never decide because an earlier one always wins, deny rules pre-empted by a broader allow, catch-alls that are not last, unreachable or terminal states, shadowed transitions, transitions on unknown actions, capabilities and actions nothing uses, and fields nothing reads. Policy reachability is sampled from the entity schema and the literals the policies mention, with a fixed seed, so the report is deterministic and `--samples` trades time for confidence. `--strict` fails on warnings, `--json` emits the report for tooling.

```bash
morph check crosspoint.yaml
morph check crosspoint.yaml --samples 2000 --strict --json
```

## Evaluation semantics

- Decisions are deny-by-default. A policy matches only when its condition holds and every capability it `requires` is granted.
- A condition that reads a missing field, compares incompatible types, or does not produce a boolean never matches. Evaluation does not raise on bad input. Use `has(x.y)` to make optional fields explicit.
- Once a definition declares `entities`, every field a policy reads must be declared, and this is checked when the definition loads. A context whose declared fields carry the wrong type is denied with reason `invalid_context` before any policy runs.
- Capabilities resolve through `operator.capabilities` unless the definition declares the capability under `capabilities` with its own `requires` context paths.
- Every entry point (runtime, planner, workflow, state machine, compiler, CLI) validates the definition before evaluating it. `morph validate` runs the same checks on their own, and `morph run --explain` shows how each policy fared.
- Compiled plans carry each policy's normalised CEL condition and the entity schema, so the Python and Node targets reach the same decision as the runtime. The SQL target only translates structured clause lists, emits one parameterised statement per policy, and refuses `contains`.

```bash
morph new my_service --template service
cd my_service
morph compile morph.yaml --target node
```
