# Task: Compositions need no pull grant

Task ID: `crosspointd-composition-pull-free`

## Task Prompt

Taking a source imported from a peer node needs that peer's pull grant. A composition is built on this node from its inputs, so taking a composition no longer needs the pull grant, even when the composition is imported from a peer. Every other source kind still needs it, and nothing else about manual take and release changes.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

State the pull rule, with this exception, as an invariant. Keep the existing invariants true, adjusting only what the change contradicts.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
