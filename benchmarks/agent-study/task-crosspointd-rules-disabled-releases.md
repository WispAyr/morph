# Task: Disabled rules release their own route

Task ID: `crosspointd-rules-disabled-releases`

## Task Prompt

Today a disabled rule does nothing: it holds with status kind `disabled`, and a route it made stays up. Change it so a disabled rule with release on clears the route it made when it safely can. When the destination shows a route this rule made, and the destination is registered, online, local and not operator-only, is not on air, and has no command in flight, the disabled rule releases it at level `info`, whether or not a matching source is live. If a release failed recently it holds with status kind `release_retrying` at level `warn` instead. In every other case a disabled rule holds with `disabled` as before. Enabled rules do not change.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
