# Task: Refuse force for a peer's destination

Task ID: `crosspointd-peer-force-refused`

## Task Prompt

Crosspoint forwards a take or release of a peer node's destination to that node, but never forwards `force`. Today a forced take or release of a peer's destination is forwarded without force, which silently drops the operator's override. Change it so a forced take or release of a peer's destination is refused with reason `force_not_forwarded`, before any other check. Requests without force are forwarded exactly as before. Nothing else about manual take and release changes.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

State the new rule as an invariant. Keep the existing invariants true, adjusting only what the change contradicts.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
