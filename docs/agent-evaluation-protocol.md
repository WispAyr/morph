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
    "task_cases_passed": false,
    "invariants_status": "not_applicable"
  },
  "human_interventions": 0,
  "elapsed_seconds": 0
}
```

The scorer requires one `direct_source` and one `morph_mediated` record per `pair_id`. Score a run file with:

```bash
python benchmarks/score_agent_runs.py runs.jsonl \
  --reference /secure/evaluator/reference.json \
  --corpus benchmarks/agent-study/corpus.json --pretty
```

The corpus index is required. It pins each task to a baseline commit, prompt, definition, and expected implementation boundary. The scorer checks that the index and evaluator reference agree and that the run file covers every indexed task. Development fixtures require `--allow-development-fixtures` and their results are not benchmark evidence.

The deterministic definition-case judge is `benchmarks/judge_candidate.py`. The evaluator runs it against each candidate definition and records its `task_cases_passed` result in the run artifact. It replays the pinned baseline first as a reference sanity check. For example:

```bash
python benchmarks/judge_candidate.py crosspoint-route-success-from-idle candidate.yaml \
  --reference /secure/evaluator/reference.json --allow-development-fixture
```

This judge checks MORPH policy decisions and state transitions. It does not replace hidden implementation tests or establish implementation equivalence.

## Paired Runner

`benchmarks/run_paired_task.py` coordinates a pair through a provider adapter. The adapter is an executable that reads one JSON request from stdin and writes one JSON response to stdout. A `start` request includes the arm, phase, task prompt, workspace path, and whether source is available. It returns:

```json
{
  "agent": {"provider": "...", "model": "...", "version": "..."},
  "session_token": "opaque-provider-session-id",
  "pre_implementation": {
    "understanding": {},
    "semantic_proposal": {},
    "morph_analysis": {},
    "analysis": {"affected_subjects": [], "relationship": "unknown"}
  }
}
```

The runner freezes this artifact before sending a `resume` request with the same session token and full source snapshot. The adapter returns an `implementation` object and may include a textual `patch`; the runner also captures the actual workspace diff. Both arms receive snapshots at the corpus-pinned commit with one local baseline commit and no remote history. The MORPH pre-implementation workspace contains only the prompt and definition. The pair order is randomized and recorded.

The implementation response must include a Boolean `implementation_complete`; it may include a non-negative `human_interventions` count. Keep provider/model/version metadata identical across both arms. The runner saves raw phase artifacts and diffs in `pair.json`. After both arms finish, evaluate tests and invariants independently and save gate outcomes in this format:

```json
{
  "direct_source": {"tests_passed": true, "simulation_passed": true, "invariants_status": "pass"},
  "morph_mediated": {"tests_passed": true, "simulation_passed": true, "invariants_status": "pass"}
}
```

Then generate the two scorer records with the deterministic task-case judge:

```bash
python benchmarks/finalize_paired_run.py /private/morph-runs/pair-001/pair.json \
  --gates /private/morph-evaluator/pair-001/gates.json \
  --reference /secure/evaluator/reference.json \
  --output /private/morph-results/pair-001.runs.jsonl
```

Append each pair's JSONL records to the corpus-wide run file before scoring. The finalizer must be run only after both arms have completed; it verifies the frozen prediction hashes, prompt hash, task status, and pinned baseline before reading the evaluator reference. Its definition-case judge replays the pinned baseline as a sanity check. A failed baseline sanity check is an evaluation setup failure and must be resolved before interpreting candidate results.

Example invocation:

```bash
python benchmarks/run_paired_task.py TASK_ID pair-001 \
  --adapter python --adapter-arg /path/to/provider_adapter.py \
  --output-dir /private/morph-runs
```

Development fixtures require `--allow-development-fixtures` for both runner and finalizer and are never benchmark evidence. The runner prepares separate workspaces and omits known evaluator files, but it does not provide an operating-system sandbox. Run adapters inside a container or remote environment that can access only the supplied workspace; otherwise the adapter may still read other host files. The pair output is raw agent evidence until finalized with independent gate results.

Keep the evaluator reference and hidden acceptance tests outside the agent-visible mount until both arms for a task are complete. The repository's two current tasks are marked `development_fixture`: their expected cases were pushed in Git history, so they are compromised and must not count as blind benchmark tasks. A private copy of those labels is stored at `/Users/ewanrichardson/Library/Application Support/MORPH/evaluator/reference.json` for local judge development only. New frozen task labels must be authored directly in private evaluator storage and must never be added to this repository or its agent-visible worktrees.

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
- Deterministic task-case pass rate from the MORPH definition judge.
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

The repository contains a latency pilot and a second state-transition task in a versioned corpus index; both are development fixtures, not hidden evaluation data. Provider runner integrations and a fresh frozen twenty-task corpus are still prerequisites for executing and making claims from this experiment; the protocol and task index are not results.

The task index is in [benchmarks/agent-study/corpus.json](../benchmarks/agent-study/corpus.json). The pilot and state-transition prompts are in [benchmarks/agent-study/pilot-crosspoint-latency.md](../benchmarks/agent-study/pilot-crosspoint-latency.md) and [benchmarks/agent-study/task-crosspoint-transition-scope.md](../benchmarks/agent-study/task-crosspoint-transition-scope.md). Their evaluator reference is intentionally external to the repository; these two tasks remain local development fixtures.
