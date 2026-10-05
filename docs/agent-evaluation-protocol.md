# MORPH Agent Evaluation Protocol

## Objective

Test whether agents given a MORPH semantic interface make safer and more accurate repository changes than agents given the same task and source tree alone.

The hypothesis is falsifiable: MORPH-mediated runs should improve semantic-impact accuracy and invariant preservation without reducing task completion or increasing human interventions. A successful test suite alone is not evidence that an agent understood the change.

## Paired Experiment

Use one agent per run and two arms for every task:

- Direct-source arm: the agent receives the task and repository source tree immediately.
- MORPH-mediated arm: the agent first receives the task, canonical MORPH IR, and semantic tools, but no source tree. It records its understanding and proposed semantic change. MORPH then returns analysis; only after that artifact is frozen does the same agent receive source access to implement the change.

This is a controlled comparison, not a multi-agent system. Use the same provider, model version, task wording, time/token budgets, and implementation/test tools in each paired run. Randomize arm order. Give each run a clean worktree and conversation; do not share transcripts, caches, or patches. Record provider, model/version, tool versions, elapsed time, and human interventions.

The MORPH-mediated workflow should use the available semantic operations: inspect, impact/query, propose/diff/plan, typed semantic reasoning, simulation, invariants, and runtime replay/counterfactual where the task has runtime state. MORPH's deterministic judge records results independently of the agent. The provider adapter remains outside MORPH, so Claude Code, OpenHands, Codex, Aider, SWE-agent, or a local agent can run the same protocol.

## Required Artifacts

Every run records:

```json
{
  "task_id": "...",
  "pair_id": "...",
  "arm": "direct_source|morph_mediated",
  "agent": {"provider": "...", "model": "...", "version": "..."},
  "pre_implementation_understanding": {},
  "semantic_proposal": {},
  "morph_analysis": {},
  "analysis": {"affected_subjects": [], "relationship": "unknown"},
  "patch": "...",
  "judge": {
    "implementation_complete": false,
    "tests_passed": false,
    "simulation_passed": false,
    "invariants_status": "not_applicable"
  },
  "human_interventions": 0,
  "elapsed_seconds": 0
}
```

The scorer requires one `direct_source` and one `morph_mediated` record per `pair_id`. Score a run file with:

```bash
python benchmarks/score_agent_runs.py runs.jsonl --reference benchmarks/agent-study/reference.json --pretty
```

The runner must keep `reference.json` and hidden acceptance tests outside the agent-visible mount until both arms for a task are complete. The checked-in pilot reference is for local judge validation; it is not an agent input.

Before source access in the MORPH-mediated arm, the agent records affected entities, policies, capabilities, state transitions, invariants, expected behavior, simulation scenarios, assumptions, and unresolved questions. Every invariant claim includes MORPH evidence or is marked UNKNOWN. Preserve this artifact so the later implementation cannot rewrite the agent's initial impact estimate.

MORPH's UNKNOWN result is recorded as unresolved, never converted into PASS. The same MORPH judge runs on both arms after implementation; for the direct-source arm, it evaluates the resulting candidate definition and implementation.

## Judge Gates

Report gates separately; do not collapse them into one score:

- Structural: definition and implementation validate.
- Semantic: supported relationships are proven; unsupported reasoning is UNKNOWN.
- Invariants: baseline invariants remain preserved or an explicit human-approved change exists.
- Simulation: all declared scenarios pass. Report scenario count; finite simulation is not a proof.
- Implementation equivalence: observed implementation behavior matches the MORPH contract on the task's test corpus.
- Approval: a human explicitly accepts the final change.

The first experiment does not require a separate Adversary agent. Use hidden regression tests and MORPH's independent semantic checks to challenge changes. Only a fully approved run with all required gates passing is PR-ready. A passing simulation cannot substitute for semantic proof, and a semantic proof cannot substitute for implementation tests.

## Measures

For each arm and across paired tasks, report:

- Task completion and regression-test pass rate.
- Precision and recall of affected-subject predictions against a human-authored reference.
- Invariant regressions, including regressions found only by hidden tests or MORPH checks.
- UNKNOWN rate and the number of UNKNOWN results that agents incorrectly report as safe.
- Proposal relationship accuracy: equivalent, narrower, broader, conflicting, or UNKNOWN.
- Simulation failures found before implementation and after implementation.
- Human interventions, elapsed time, and token/tool budget.
- Implementation-equivalence failures between MORPH behavior and code behavior.

Publish per-task results as well as aggregates. Do not claim MORPH outperforms source-only agents from a single successful example; pre-register tasks, budgets, scoring rules, and exclusion criteria before running the comparison.

## Task Corpus

Freeze twenty genuine change requests before the first run. Each task needs a baseline commit, MORPH definition, exact task statement, hidden regression tests, reference impact/invariant annotations, expected implementation boundary, and difficulty rating. Include at least two tasks in each category:

- Trivial safe changes.
- Semantically equivalent rewrites.
- Narrowing behavior.
- Broadening behavior.
- Invariant violations.
- Changes whose safety is UNKNOWN to the current reasoner.
- State-transition changes.
- Capability/effect changes.

Use the remaining four tasks for compound changes spanning multiple categories. Do not invent post-hoc tasks to favor either arm.

The current repository does not contain provider runner integrations or a frozen twenty-task corpus. Those are prerequisites for executing and making claims from this experiment; this document defines the provider-neutral protocol, not results.

The first pilot task and its evaluator-only reference cases are in [benchmarks/agent-study/pilot-crosspoint-latency.md](../benchmarks/agent-study/pilot-crosspoint-latency.md) and [benchmarks/agent-study/reference.json](../benchmarks/agent-study/reference.json). The pilot validates the broadening category; nineteen more tasks are needed before the planned study.