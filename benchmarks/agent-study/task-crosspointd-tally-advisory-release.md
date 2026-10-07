# Task: A switcher's tally alone does not lock a release

Task ID: `crosspointd-tally-advisory-release`

## Task Prompt

A destination is on air when its owner reports it on programme or a switcher's tally reports it on programme. Tallies are sometimes stale, so a switcher's tally alone should no longer lock a release: a release of a destination whose owner does not report it on programme is decided as if the destination were off air, even when a tally reports it on programme. Takes still treat either signal as on air, and a release of a destination its owner reports on programme still needs force. As before, `force` is only sent when it overrides the lock. Nothing else about manual take and release changes.

Manual take and release are modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and `tests/test_crosspointd_model.py` checks the model against them. Keep these and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it. crosspointd is changing in the same way, so update the outcomes of the recorded scenarios this change affects and leave every other recorded scenario as it is.

Keep every invariant true and meaningful. Where the change contradicts one, adjust it no more than the change requires.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
