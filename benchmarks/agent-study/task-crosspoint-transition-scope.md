# Task: Limit route success transitions to idle destinations

Task ID: `crosspoint-route-success-from-idle`

## Task Prompt

Change the Crosspoint state machine so a successful `route_source` effect moves a destination to `routed` only when that destination was `idle`. A successful route event must leave a destination in any other state unchanged. Preserve the existing observation-driven transitions: observing a faulted destination moves it to `faulted`, and observing it ready again moves it from `faulted` to `idle`.

Do not change route policy conditions, capability grants, effect behavior, or the destination's initial state. Add or update tests for the transition boundaries.

Before editing source, record the affected semantic subjects, the relationship between the old and proposed transition behavior, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the Crosspoint MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.

## Expected Boundary

- `idle` + `route_source.succeeded` becomes `routed`.
- `faulted` + `route_source.succeeded` remains `faulted`.
- `faulted` + observation with `status: ready` becomes `idle`.
- No policy, capability, or action behavior changes.
