# Task: Fill an on-air destination that shows nothing

Task ID: `crosspointd-rules-on-air-idle-take`

## Task Prompt

A rule never touches an on-air destination today: with a matching source it holds with status kind `queued_on_air`, or `on_air_routed` when the destination already shows that source. When an on-air destination shows nothing at all, holding only leaves the programme empty. Change it so that when an on-air destination shows no source and the rule has a matching source, the rule takes it as it would off air; the in-flight hold and the take retry back-off still apply. Every other on-air case holds exactly as before, and nothing else about the rules engine changes.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
