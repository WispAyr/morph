# Task: A forced request supersedes a command in flight

Task ID: `crosspointd-force-supersedes-in-flight`

## Task Prompt

Today a command in flight for a destination blocks every new manual request for it, which leaves an operator unable to override a stuck command. Change it so a forced take or release is no longer blocked by a command in flight: it is decided exactly as it would be if no command were in flight. Requests without force are still refused with reason `in_flight` while a command is in flight. Nothing else about manual take and release changes; in particular, `force` is still only sent when it overrides an on-air lock.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
