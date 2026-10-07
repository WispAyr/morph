# Task: Rules propose to operator-only destinations

Task ID: `crosspointd-rules-operator-only-proposals`

## Task Prompt

Today a rule whose destination is operator-only holds with status kind `operator_only`: rules never target such destinations. Change it so the rule proposes to the destination's operator instead, as manual control does. For an operator-only destination a rule is decided as for any other local destination, except:

- Where it would send a take it sends a proposal instead, decision action `propose_take`, and where it would send a release, a proposal `propose_release`, both at level `info`. A proposal uses a new capability that arms a proposal for the operator, like the manual model's `propose_to_operator`.
- On air does not hold it: a proposal changes nothing until the operator acts, so the on-air holds do not apply to operator-only destinations.

Disabled rules, peer destinations, and unregistered or offline destinations still hold as before, and those checks still come first. Rules still never send a take or release command to an operator-only destination.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
