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
morph run demo.yaml --set route.status=ready
morph compile demo.yaml --target node
```

The CLI gives MORPH a real developer workflow: initialize a YAML definition, scaffold a reusable project, evaluate it against a context, and compile it into Python, Node, or SQL execution targets. `morph run` exits 0 on an allow decision and 2 on a deny, so it can gate scripts directly.

## Evaluation semantics

- Decisions are deny-by-default. A policy matches only when every `when` clause holds and every capability it `requires` is granted.
- A clause may combine operators (`equals`, `lt`, `lte`, `gt`, `gte`, `contains`); all of them must hold.
- A field that is missing from the context, null, or not comparable with the operand never matches. Evaluation does not raise on bad input.
- Capabilities resolve through `operator.capabilities` unless the definition declares the capability under `capabilities` with its own `requires` context paths.
- Every entry point (runtime, planner, workflow, state machine, compiler, CLI) validates the definition before evaluating it.
- Compiled plans keep each policy's conditions, so the Python, Node, and SQL targets reach the same decision as the runtime. The SQL target emits one parameterised statement per policy and refuses `contains`, which it cannot express.

```bash
morph new my_service --template service
cd my_service
morph compile morph.yaml --target node
```
