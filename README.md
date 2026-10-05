# MORPH

MORPH is a small, AI-native system specification prototype focused on explicit state, policy, capability, and execution semantics.

## Phase 1 summary

This repository captures the core findings from the initial architecture review:

- MORPH is not another programming language.
- The right foundation is an AI-native IR with validator and runtime semantics.
- The first realistic domain should be a parking/ANPR/access-control flow.
- Safety and capability boundaries are critical for AI-generated system changes.

## Included prototype

The repo currently contains a minimal Python implementation of MORPH-style runtime logic for parking access decisions. It exercises allow/deny decisions based on permit validity, payment validity, and grace-period rules.

See the Phase 1 findings in [docs/phase1-findings.md](docs/phase1-findings.md).

## Quick start

```bash
python -m pip install -e .[dev]
pytest -q
```
