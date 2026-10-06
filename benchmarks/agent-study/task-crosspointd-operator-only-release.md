# Task: On-air operator-only releases need force

Task ID: `crosspointd-operator-only-release-needs-force`

## Task Prompt

Today a release of an operator-only destination is always sent to its operator as a proposal, even while the destination is on air: Crosspoint never checks the lock for operator-only destinations. Change how releases of on-air operator-only destinations work:

- Without force, the release is refused with reason `on_air`, as for any other on-air destination.
- With force, it is sent as a forced release command, an emergency clear, instead of a proposal.

Releases of operator-only destinations that are off air are still proposals. Takes of operator-only destinations do not change. The order of the other checks does not change: a peer's destination is still forwarded first, and a command in flight still blocks.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
