# The crosspointd model

`src/morph/examples/crosspointd.yaml` models manual take and release in the real Crosspoint control plane, `Core.manualTake` and `Core.manualRelease` in `crosspoint/server/src/core.mjs`. Unlike `crosspoint.yaml`, which is an illustrative example, it is checked against crosspointd itself.

## How it is checked

`tools/crosspointd_oracle.mjs` imports crosspointd's `Core`, builds every combination of the inputs that decide a manual request, calls `manualTake` or `manualRelease`, and records what crosspointd did. Only the I/O edges are stubbed: sending the command to the destination's owner and forwarding to a peer node. The lock derivation in `recompute()`, id normalisation, and the order of the checks are crosspointd's own code.

The inputs are request action and force, destination kind, accepted source kinds, operator-only, peer-owned, on programme (owner flag), programme tally (switcher), command in flight, source kind, peer-owned source, and pull grant. That gives 2,560 scenarios and 13 distinct outcomes. They are stored in `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` with the Crosspoint commit in the header line.

`tests/test_crosspointd_model.py` requires MORPH to reach the same outcome for every scenario. Setting `CROSSPOINT_DIR` to a Crosspoint checkout also re-records the scenarios and fails if crosspointd's behaviour has drifted from the fixture. To update after a deliberate crosspointd change:

```bash
node tools/crosspointd_oracle.mjs /path/to/crosspoint | gzip -n -9 > tests/fixtures/crosspointd_manual_decisions.jsonl.gz
```

The check is sensitive to the details that matter. Each of these plausible mistakes makes it fail:

| Mistake in the model | Scenarios that disagree |
| --- | --- |
| Checking on air for operator-only destinations | 84 |
| Taking the lock from the owner's flag only, ignoring switcher tally | 28 |
| Checking in flight before on air | 42 |
| Running local checks before forwarding a peer's destination | 1,098 |

## What crosspointd actually does

These differ from the illustrative `crosspoint.yaml`:

- **The lock is "on air"**: the owner reports the destination on programme, or a switcher's tally does. There is no lock owner. An operator overrides it with `force`, and `force` is sent only when it overrides something.
- **There are no per-operator capabilities.** Access is an admin or viewer code at the API, not a decision input.
- **Operator-only destinations get proposals, not commands**, and are never checked for on air: a proposal changes nothing until their operator acts. A command already in flight still blocks a proposal.
- **A peer node's destination is forwarded to that node before any local check.** The owner decides.
- **Latency plays no part** in a manual take.

## Not yet covered

- The automatic rules engine (`Core.runRules`), which never touches an on-air, operator-only, offline, or peer destination and has its own if-free and release policies.
- Proposal resolution: taken, declined, expired, cancelled, superseded, withdrawn.
- Federation on the owner's side, where a forwarded request is decided.

## A gap in MORPH this exposed

The safety rules here are about decisions, for example "never take an on-air destination without force". MORPH invariants are predicates over the context only, so they cannot state that. The agreement test is what protects these rules today. Invariants that can read the decision would let `morph diff` and the evaluator protect them directly.
