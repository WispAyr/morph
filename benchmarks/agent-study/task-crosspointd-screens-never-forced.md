# Task: Screens are never forced

Task ID: `crosspointd-screens-never-forced`

## Task Prompt

Screens show layouts on a video wall. Two changes apply to a screen that is on air and is neither operator-only nor a peer node's destination:

- A forced take is refused with reason `screen_on_air`: a wall layout is never forced. A take without force is still refused with reason `on_air`.
- A release no longer needs force: clearing a wall is always safe, so it is sent as a plain release, with or without force, and never as a forced release.

The layout, accepts and pull checks still come before these, and a command in flight still blocks a release. Operator-only screens, peer nodes' screens, screens that are off air, and every other destination kind are unchanged.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

State as an invariant that a screen is never sent a forced command. Keep the existing invariants true, adjusting only what the change contradicts.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
