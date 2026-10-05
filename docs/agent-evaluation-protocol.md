# MORPH Agent Evaluation Protocol

## Objective

Test whether agents given a MORPH semantic interface make safer and more accurate repository changes than agents given the same task and source tree alone.

The hypothesis is falsifiable: MORPH-mediated runs should improve semantic-impact accuracy and invariant preservation without reducing task completion or increasing human interventions. A successful test suite alone is not evidence that an agent understood the change.

## Roles

| Role | Inputs | Permissions | Required output |
| --- | --- | --- | --- |
| Architect | Task statement and canonical MORPH IR | Read-only MORPH operations; no source tree | Proposed intent, affected subjects, invariant risks, scenarios, UNKNOWNs, and a structured change proposal |
| Builder | Task statement and human-approved proposal | Source-tree write access in an isolated worktree | Implementation patch, tests, and a mapping from proposal claims to code/tests |
| Adversary | Task statement, baseline IR, candidate IR, implementation patch, and tests | Read-only | Findings with severity, evidence, and reproducible counterexamples or commands |
| MORPH Judge | Baseline/candidate definitions, contexts, and implementation | Deterministic checks only | Structural, semantic, invariant, simulation, and implementation-equivalence results |

The Architect must not see source code in the MORPH-mediated arm. The Adversary starts from a fresh context and does not receive the Architect's rationale. The Builder cannot approve its own proposal. Humans retain approval authority.

## Run Design

Use paired control and treatment runs for each task:

- Control: one agent receives the issue and repository source tree.
- Treatment: Architect receives only MORPH IR and MORPH operations; after a human approves the proposal, Builder receives the proposal and source tree; Adversary independently challenges the result.

Use the same model versions, task text, time/token budgets, and tool permissions in both arms. Randomize run order. Give each run a clean worktree and conversation; do not share agent transcripts, caches, or patches between arms. Record provider, model/version, tool versions, elapsed time, and human interventions.

The provider adapter is intentionally outside MORPH. OpenHands, Claude Code, Codex, Aider, SWE-agent, or a local agent may implement the role contract, but MORPH judges artifacts rather than trusting provider claims.

## Required Artifacts

Every run records:

```json
{
  "task_id": "...",
  "arm": "control|morph",
  "agent": {"provider": "...", "model": "...", "version": "..."},
  "architect_proposal": {},
  "approved_proposal": {},
  "patch": "...",
  "tests": {"commands": [], "results": []},
  "adversary_findings": [],
  "morph_judgement": {},
  "human_interventions": [],
  "elapsed_seconds": 0
}
```

Architect proposals identify affected entities, policies, capabilities, state transitions, invariants, expected behavior, simulation scenarios, assumptions, and unresolved questions. Every invariant claim includes its MORPH evidence or is marked UNKNOWN.

Adversary findings identify the violated claim, a minimal counterexample or reproduction, and severity. MORPH's UNKNOWN result is recorded as unresolved, never converted into PASS.

## Judge Gates

Report gates separately; do not collapse them into one score:

- Structural: definition and implementation validate.
- Semantic: supported relationships are proven; unsupported reasoning is UNKNOWN.
- Invariants: baseline invariants remain preserved or an explicit human-approved change exists.
- Simulation: all declared scenarios pass. Report scenario count; finite simulation is not a proof.
- Adversarial: no unresolved high-severity finding remains.
- Implementation equivalence: observed implementation behavior matches the MORPH contract on the task's test corpus.
- Approval: a human explicitly accepts the final change.

Only a fully approved run with all required gates passing is PR-ready. A passing simulation cannot substitute for semantic proof, and a semantic proof cannot substitute for implementation tests.

## Measures

For each arm and across paired tasks, report:

- Task completion and regression-test pass rate.
- Precision and recall of affected-subject predictions against a human-authored reference.
- Invariant regressions, including regressions found only by the Adversary.
- Proposal relationship accuracy: equivalent, narrower, broader, conflicting, or UNKNOWN.
- Simulation failures found before implementation and after implementation.
- Adversary yield: unique actionable findings per run.
- Human interventions, elapsed time, and token/tool budget.
- Implementation-equivalence failures between MORPH behavior and code behavior.

Publish per-task results as well as aggregates. Do not claim MORPH outperforms source-only agents from a single successful example; pre-register tasks, budgets, scoring rules, and exclusion criteria before running the comparison.

## Task Corpus

Freeze ten genuine change requests before the first run. Each task needs a baseline commit, MORPH definition, exact task statement, hidden regression tests, reference impact/invariant annotations, expected implementation boundary, and difficulty rating. Sample across policy/invariant changes, entity state transitions, capability/effect contracts, and runtime or CLI behavior. Do not invent post-hoc tasks to favor either arm.

The current repository does not contain provider runner integrations or a frozen ten-task corpus. Those are prerequisites for executing and making claims from this experiment; this document defines the provider-neutral protocol, not results.