# MORPH for Crosspoint

MORPH is a small, AI-native system specification prototype for the WispAyr Crosspoint studio control plane. The focus is explicit state, policy, capability, and route semantics for live media operations rather than ad hoc imperative wiring.

## Phase 1 summary

This repository captures the core findings from the initial architecture review:

- MORPH is not another programming language.
- The right foundation is an AI-native IR with validator and execution semantics.
- The first realistic domain is the Crosspoint studio graph: callers, cameras, desk channels, screens, and data feeds.
- Safety and capability boundaries are critical for AI-generated route changes.

## Included prototype

The repo currently contains a minimal Python implementation of MORPH-style runtime logic for studio routing decisions. It evaluates whether a route is allowed based on source readiness, destination availability, and operator override rules.

See the Phase 1 findings in [docs/phase1-findings.md](docs/phase1-findings.md).

## Quick start

```bash
python -m pip install -e .[dev]
pytest -q
```
