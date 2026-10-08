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

## The rules engine

`src/morph/examples/crosspointd_rules.yaml` models one tick of the automatic rules engine (`Core.runRules`) for one rule and its destination. It either sends a take or a release, or holds with a status saying why. Commands are allow decisions; a status without a command is a deny whose `reason` is the status kind, and both carry the status level crosspointd shows.

`tools/crosspointd_rules_oracle.mjs` drives crosspointd's own `runRules` over 6,080 combinations of what it reads:
- **The rule:** enabled, `if-free` or `replace`, release.
- **The destination:** registered, offline, peer, operator-only, on air, command in flight, and whether it shows nothing, the candidate, or another source.
- **The candidate:** whether a live matching source exists.
- **The current route and retries:** whether the source currently shown is live, whether this rule made the current route, and recent failed attempts.

It stubs only `sendCommand`. Lock derivation is covered by the manual oracle, so the derived fields are set directly here. Choosing which matching source is the candidate is not part of the decision. `tests/test_crosspointd_rules_model.py` requires all 6,080 decisions to match, and re-records them when `CROSSPOINT_DIR` is set.

Eight decision invariants state the engine's safety rules:
- it never touches an on-air destination;
- it never targets a peer or operator-only destination;
- it only commands registered, online destinations;
- disabled rules do nothing;
- it sends one command at a time;
- an `if-free` rule never displaces a live source it did not put there;
- it releases only its own route;
- a failed command waits before retrying.

Removing the policy behind any of them breaks it in simulation. Reordering the on-air and in-flight checks breaks no invariant, but disagrees with crosspointd in 160 scenarios.

## Shadow mode and Crosspoint CI

crosspointd can log every routing decision it makes (Crosspoint `docs/decision-log.md`). The log is off unless the config's `decisionLog` names a file. Each line holds the inputs in these models' shape and crosspointd's raw outcome. `tools/shadow_check.py LOG` re-decides every record with these models and exits 1 if any decision differs, so the models can be checked against real traffic before MORPH is trusted with a decision.

`tests/test_shadow_check.py` drives all 8,640 oracle scenarios through crosspointd's own decision log (`--shadow` on both oracles) when `CROSSPOINT_DIR` is set, and requires MORPH to re-decide every one identically. Crosspoint's `.github/workflows/routing-model.yml` runs that test and both re-record tests on every Crosspoint push and pull request. A change to a routing decision therefore fails Crosspoint's CI until this model and its fixtures are updated with it.

## Not yet covered

- Proposal resolution: taken, declined, expired, cancelled, superseded, withdrawn.
- Federation on the owner's side, where a forwarded request is decided.

## Safety invariants

The rules that make manual control safe are about decisions, so the model states them as decision invariants (invariants that read `decision`):

| Invariant | Rule |
| --- | --- |
| `on_air_changed_only_with_force` | A take or release on an on-air destination needs force |
| `force_sent_only_when_on_air` | Force is sent only when it overrides an on-air lock |
| `operator_only_gets_proposals` | An operator-only destination is never commanded, only proposed to |
| `peer_destinations_are_forwarded` | A peer's destination is always forwarded to the peer |
| `screens_take_only_layouts` | A screen is only given a composition, or a source of a kind its owner explicitly lists in `accepts` (crosspoint #59, the CSU van displays) |
| `no_command_while_one_is_in_flight` | No command is sent while one is awaiting an ack |

All 2,560 recorded scenarios satisfy them. They also protect the model without the oracle: removing the policy that enforces any one of them makes `morph simulate` report that invariant as broken, and `morph diff` blocks a candidate that rewrites one in a way it cannot prove equivalent. The evaluator uses both, so an agent's change to this model is checked against these rules directly.
