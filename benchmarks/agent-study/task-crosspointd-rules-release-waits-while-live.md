# Task: A release waits while the shown source is live

Task ID: `crosspointd-rules-release-waits-while-live`

## Task Prompt

A rule with release on releases its route when no matching live source is left. When the source the destination is showing is still live (it stopped matching the rule but is still up), the rule now holds instead, with status kind `release_waiting_live` at level `info`, until that source goes. This hold comes after the on-air and in-flight holds and before the release retry back-off: a live source is reported as waiting, not as retrying. Nothing else about the rules engine changes.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

State the new rule as an invariant. Keep the existing invariants true, adjusting only what the change contradicts.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
