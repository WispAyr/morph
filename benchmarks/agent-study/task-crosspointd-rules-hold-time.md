# Task: Rules hold a route before releasing it

Task ID: `crosspointd-rules-release-hold-time`

## Task Prompt

Crosspoint is adding a hold time to rules: after a rule takes a source, it must not release that route until the hold time has passed, so a caller who drops out for a moment is not cut. crosspointd will report this as a new Boolean field `held` on the route: true while the route this rule made is younger than its hold time. Destinations on older nodes do not report the field, and a missing `held` means not held.

While its route is held, a rule that would otherwise release holds instead, with status kind `release_held` at level `info`. The on-air and in-flight holds still come first, and a held route is reported as held, not as a release retry. Nothing else about the rules engine changes.

The rules engine is modelled in `src/morph/examples/crosspointd_rules.yaml`. Its invariants state the engine's safety rules. `tests/fixtures/crosspointd_rule_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_rules_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. The recorded scenarios come from crosspointd before this change and do not report `held`; they must still be decided as recorded.

State the new rule as an invariant, and keep every existing invariant true.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
