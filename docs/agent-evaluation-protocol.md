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

Every run record holds only what the agent produced and how the run went:

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
  "human_interventions": 0,
  "elapsed_seconds": 0
}
```

Gate outcomes belong to the evaluator, not the agent, so they live in a separate evaluation record keyed by `pair_id` and `arm`. `benchmarks/evaluate_candidate.py` produces it; the main fields are:

```json
{
  "task_id": "...",
  "pair_id": "...",
  "arm": "direct_source|morph_mediated",
  "within_boundary": true,
  "structural_passed": true,
  "tests_passed": true,
  "task_cases_passed": true,
  "simulation_passed": true,
  "invariants_status": "not_applicable",
  "semantic_relationship": "narrower",
  "implementation_complete": true,
  "agent_claimed_complete": true
}
```

The record also lists the evaluated files with their hashes, every changed file, files changed outside the boundary, regressions, acceptance-test failures, simulation failures, and per-invariant outcomes, so a reviewer can see why each gate passed or failed.

The scorer rejects a run record that carries `judge` or `evaluation` fields, requires exactly one evaluation for every run and no evaluation without a run, and requires one `direct_source` and one `morph_mediated` run per `pair_id`. Score a run file with:

```bash
python benchmarks/score_agent_runs.py runs.jsonl \
  --evaluations evaluations.jsonl \
  --reference /secure/evaluator/reference.json \
  --corpus benchmarks/agent-study/corpus.json --pretty
```

The corpus index is required. It pins each task to a baseline commit, prompt, definition, and expected implementation boundary. The scorer checks that the index and evaluator reference agree and that the run file covers every indexed task. Development fixtures require `--allow-development-fixtures` and their results are not benchmark evidence.

The deterministic definition-case judge is `benchmarks/judge_candidate.py`. The evaluator runs it against each candidate definition and records its `task_cases_passed` result in the evaluation record. It replays the pinned baseline first as a reference sanity check. For example:

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

The implementation response must include a Boolean `implementation_complete`; it may include a non-negative `human_interventions` count. That claim is recorded as `agent_claimed_complete` and is never a gate. Keep provider/model/version metadata identical across both arms. The runner saves raw phase artifacts and diffs in `pair.json`.

## Independent Evaluation

After both arms finish, the finalizer evaluates each arm with `benchmarks/evaluate_candidate.py`. No gate result is supplied from outside: the evaluator computes every gate from trusted inputs. Those are the source repository, the corpus, the private reference labels, and a private manifest kept beside the hidden tests:

```json
{
  "schema_version": 1,
  "tasks": {
    "TASK_ID": {
      "tests": ["TASK_ID/test_acceptance.py"],
      "test_timeout_seconds": 900,
      "simulation_scenarios": [{"context": {"source": {"status": "live"}}, "expected_status": "allow"}],
      "approved_invariant_changes": []
    }
  }
}
```

Test paths are relative to the manifest and resolved only within that private directory. Each simulation scenario declares its context and expected `allow` or `deny` decision. The manifest and reference must be outside the source repository and the pair directory.

- **Workspace containment.** An arm's workspace must be exactly `<pair>/<arm>/source`, the path the runner created, and neither it nor its arm directory may be a symbolic link. A workspace recorded anywhere else is rejected, as is any symbolic link or special file inside it.
- **Trusted evaluation tree.** The evaluator exports the pinned baseline commit from the source repository, without evaluator-only files, into a private temporary directory, and copies over it only the workspace files inside the task's implementation boundary. The agent's Git history, its edits outside the boundary, and its own reports are never inputs to a gate. Hidden tests run in this tree, with `MORPH_CANDIDATE_WORKSPACE` pointing at it.
- **`within_boundary`**: no file outside the implementation boundary differs from the baseline. Changed files outside it are listed and make the run incomplete.
- **`structural_passed`**: the candidate MORPH definition loads, validates, and builds a runtime.
- **`tests_passed`**: every baseline test that passes on the baseline also passes on the candidate, and every hidden test in the manifest passes. Baseline test files inside the boundary are left out, because the agent may change them; the agent's own tests run separately and are reported as `agent_tests_passed`, which is not a gate.
- **`task_cases_passed`**: the evaluator-only definition cases.
- **`simulation_passed`**: every manifest scenario reaches its expected decision and the candidate's invariants hold in it.
- **`invariants_status`**: `pass` when every baseline invariant is unchanged or proven equivalent, `fail` when one is removed or proven different, `unknown` when a rewrite cannot be proven, and `not_applicable` when the baseline has none. Names listed in `approved_invariant_changes` may change.
- **`semantic_relationship`**: the classifier's relationship between baseline and candidate; the scorer compares it with the reference label.
- **`implementation_complete`**: every gate above passed.

Test outcomes are read per test from a JUnit report the evaluator writes, never from pytest's exit code, and a run that produces no report fails. A process that exits early therefore cannot pass a gate.

**Evaluator execution is trusted infrastructure and must run inside a disposable sandbox.** Running baseline, hidden, and agent tests executes candidate code, which can inspect its environment, arguments, and mounts. MORPH does not enforce operating-system isolation itself: run the evaluator in a disposable container or remote worker with no credentials, no network, and only the pair directory, the source repository, and the private evaluator storage mounted.

Generate the scorer inputs after both arms complete. The finalizer writes the two run records and two independent evaluation records to separate files:

```bash
python benchmarks/finalize_paired_run.py /private/morph-runs/pair-001/pair.json \
  --evaluator-config /private/morph-evaluator/manifest.json \
  --reference /secure/evaluator/reference.json \
  --output /private/morph-results/pair-001.runs.jsonl \
  --evaluations-output /private/morph-results/pair-001.evaluations.jsonl
```

Append each pair's run and evaluation records to private corpus-wide run and evaluation files before scoring. Keep the manifest, hidden tests, reference labels, and finalized results outside both agent workspaces and the source checkout. The finalizer enforces the workspace and output boundaries, runs only after both arms have completed, and verifies frozen prediction hashes, prompt hash, task status, and pinned baseline before evaluation. A failed baseline sanity check is an evaluation setup failure and must be resolved before interpreting candidate results.

Example invocation:

```bash
python benchmarks/run_paired_task.py TASK_ID pair-001 \
  --adapter python --adapter-arg /path/to/provider_adapter.py \
  --output-dir /private/morph-runs
```

Development fixtures require `--allow-development-fixtures` for both runner and finalizer and are never benchmark evidence. The runner prepares separate workspaces but does not provide an operating-system sandbox. The finalizer executes candidate code while running private tests. Run both agent adapters and the evaluator in disposable containers or remote workers with no credentials and only the required mounts; otherwise either process may read or modify other host files. The pair output is raw agent evidence until finalized with independent gate results.

Keep the evaluator reference and hidden acceptance tests outside the agent-visible mount until both arms for a task are complete. The repository's two current tasks are marked `development_fixture`: their expected cases were pushed in Git history, so they are compromised and must not count as blind benchmark tasks. A private copy of those labels is kept in evaluator storage outside the repository for local judge development only. Set `MORPH_EVALUATOR_REFERENCE` to its path to have `pytest` also check the latency pilot against those labels; without it, that test is skipped and a self-contained Crosspoint test still covers the pilot's semantics. New frozen task labels must be authored directly in private evaluator storage and must never be added to this repository or its agent-visible worktrees.

Before source access in the MORPH-mediated arm, the agent records affected entities, policies, capabilities, state transitions, invariants, expected behavior, simulation scenarios, assumptions, and unresolved questions. Every invariant claim includes MORPH evidence or is marked UNKNOWN. Preserve this artifact so the later implementation cannot rewrite the agent's initial impact estimate.

MORPH's UNKNOWN result is recorded as unresolved, never converted into PASS. The same MORPH judge runs on both arms after implementation; for the direct-source arm, it evaluates the resulting candidate definition and implementation.

## Running a Pilot Pair

`benchmarks/adapters/claude_code.py` runs each arm with Claude Code in headless mode. Both arms name affected subjects as `kind:name` with the definition's own names (`policy:`, `invariant:`, `action:`, `capability:`, `entity:`, `field:`, `transition:`), the same vocabulary as the labels, so impact scores measure understanding rather than naming. The runner passes the task's implementation boundary with the implementation request, and the adapter tells both arms which files they may change, because the evaluator fails any change outside it. Both arms get the same model, instructions, and tools, except that the MORPH-mediated arm may run the `morph` CLI and, before freezing its prediction, sees only the task and the MORPH definition (and may write scratch files there). Claude Code runs with `--safe-mode` and `--strict-mcp-config`, so no CLAUDE.md, skill, plugin, hook, memory, or MCP server reaches either arm, and inherited Claude Code session variables are removed.

1. **Prepare private evaluator storage** outside the repository: `reference.json` with labels for every corpus task, `manifest.json`, and the hidden tests the manifest names. Labels for a new task are merged into both files.
2. **Run the pair** in a disposable container that has the repository, `git`, Python with `pip install -e .[dev]` (so `morph` is on PATH), Claude Code, and credentials, and that cannot see the private evaluator storage:

   ```bash
   python benchmarks/run_paired_task.py crosspointd-screen-accepts-raw-sources pilot-001 \
     --adapter python --adapter-arg benchmarks/adapters/claude_code.py \
     --adapter-arg --model --adapter-arg claude-opus-5-5 \
     --pass-env HOME --pass-env ANTHROPIC_API_KEY --output-dir /private/morph-runs
   ```

3. **Finalize** in a separate disposable container that can see the pair directory and the private evaluator storage, but has no credentials:

   ```bash
   python benchmarks/finalize_paired_run.py /private/morph-runs/pilot-001/pair.json \
     --evaluator-config /private/morph-evaluator/manifest.json \
     --reference /private/morph-evaluator/reference.json \
     --output /private/morph-results/pilot-001.runs.jsonl \
     --evaluations-output /private/morph-results/pilot-001.evaluations.jsonl
   ```

4. **Score just the pilot task**; `--task` limits coverage checks to the tasks named and marks the result partial:

   ```bash
   python benchmarks/score_agent_runs.py /private/morph-results/pilot-001.runs.jsonl \
     --evaluations /private/morph-results/pilot-001.evaluations.jsonl \
     --reference /private/morph-evaluator/reference.json \
     --corpus benchmarks/agent-study/corpus.json \
     --task crosspointd-screen-accepts-raw-sources --pretty
   ```

5. **Inspect by hand** before scaling: both frozen `pre_implementation.json` files, each arm's `patch.diff`, the evaluation records (changed and out-of-boundary files, regressions, hidden-test failures, per-invariant outcomes), and the scorer output.

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
