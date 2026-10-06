# Task: Retry a failed take at once over a dead source

Task ID: `crosspointd-rules-dead-source-retry`

## Task Prompt

After a take fails, a rule waits for the retry interval before trying it again, holding with status kind `take_retrying`. When the destination is showing another source that is no longer live, waiting only prolongs dead air, so the rule now retries the take immediately in that case. In every other case a failed take still waits, and nothing else about the rules engine changes.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
