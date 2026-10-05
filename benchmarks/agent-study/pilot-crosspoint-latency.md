# Pilot Task: Crosspoint Source Latency

Task ID: `crosspoint-source-latency-150`

## Task Prompt

Raise the maximum source latency for an allowed Crosspoint route from below 120 ms to below 150 ms. Change only the source latency limit. Keep the destination latency limit below 120 ms. Preserve the faulted-destination denial, locked-route denial, route-control capability requirement, and success-only transition to `destination.state = routed`. Update or add tests for the boundary behavior.

Before editing source, record the affected semantic subjects, the relationship between the old and proposed allow condition, assumptions, and test scenarios. In the MORPH-mediated arm, do this from the Crosspoint MORPH definition and MORPH operations before source access. In the direct-source arm, use the same task prompt and record the prediction before implementation.

## Run Setup

- Baseline definition: `src/morph/examples/crosspoint.yaml`
- Baseline implementation tests: `tests/test_crosspoint_route_policy.py`
- Expected boundary: source latency 119 ms may route; 120 through 149 ms may now route; 150 ms must not route.
- Unchanged boundaries: destination latency 120 ms must not route; a faulted destination and a conflicting route lock must still be denied.
- State behavior: only a successful `route_source` effect may transition the destination to `routed`.

Use a clean worktree for each arm. Do not give the MORPH-mediated agent the source tree until its pre-implementation record and MORPH analysis have been saved. The reference labels are evaluator data, not part of the agent task prompt.