# Task: Pull grants are checked before forwarding

Task ID: `crosspointd-peer-pull-before-forward`

## Task Prompt

Today a take of a peer node's destination is forwarded to that node before any local check, even when the source is imported from a peer that has not granted pull, so the owner is sent a source this node cannot deliver. Change it so a take of a peer's destination whose source is imported without a pull grant is refused here, with reason `pull_not_granted`, instead of being forwarded. Every other request for a peer's destination is still forwarded before any local check, and nothing else about manual take and release changes.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

State as an invariant that a source imported without a pull grant is never taken, whether here or by forwarding. Keep the existing invariants true, adjusting only what the change contradicts.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
