# Task: Failed releases retry immediately

Task ID: `crosspointd-rules-release-retry-immediately`

## Task Prompt

Crosspoint is changing how its rules engine retries. Today, after a rule's take or release command fails, the rule waits for the retry interval before it tries that command again. Releases should no longer wait: a release that failed is tried again on the next tick. Takes keep their back-off. Nothing else about the rules engine changes.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful for the new behaviour. Where an invariant no longer holds, narrow it to what is still true rather than deleting it.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
