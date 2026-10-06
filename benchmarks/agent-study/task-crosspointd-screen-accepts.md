# Task: Let screens take the raw sources they accept

Task ID: `crosspointd-screen-accepts-raw-sources`

## Task Prompt

Crosspoint is changing how screen destinations are fed. Today a manual take onto a screen is refused unless the source is a composition (a layout). Venue screens should also be able to show a raw source full-screen when the screen's `accepts` list names that source's kind. A screen whose `accepts` list is empty still takes only compositions.

Everything else about manual take and release must stay as it is: forwarding a peer node's destination, refusing sources a destination does not accept, pull grants for peer sources, the on-air lock and `force`, proposals for operator-only destinations, refusing while a command is in flight, and the order in which crosspointd applies these checks.

The Crosspoint control plane is modelled in `src/morph/examples/crosspointd.yaml`. Its invariants state the safety rules of manual control; keep every one of them true, and keep them meaningful for the new behaviour rather than weakening them. `tests/fixtures/crosspointd_manual_decisions.jsonl.gz` records crosspointd's decisions, and crosspointd is changing in the same way, so update the outcomes of the scenarios this change affects; leave every other recorded scenario as it is. Keep `tests/test_crosspointd_model.py` and `docs/crosspointd-model.md` consistent with the new behaviour, and add tests for it.

Before editing source, record the affected semantic subjects, the relationship between the old and new set of allowed requests, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.
