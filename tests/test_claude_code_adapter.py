"""The Claude Code adapter drives a full pair through the runner, finalizer, and scorer.

A fake ``claude`` executable stands in for the model: it records its arguments, answers the
analysis phase, and makes the task's edit in the implementation phase. No API is called.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "benchmarks" / "adapters"))

import claude_code  # noqa: E402
from finalize_paired_run import finalize_pair  # noqa: E402
from run_paired_task import run_pair  # noqa: E402
from score_agent_runs import score_runs  # noqa: E402

TASK_ID = "crosspoint-route-success-from-idle"
TASK = next(task for task in json.loads((ROOT / "benchmarks/agent-study/corpus.json").read_text())["tasks"] if task["task_id"] == TASK_ID)

FAKE_CLAUDE = r'''#!PYTHON
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"argv": args, "cwd": os.getcwd(), "env": sorted(k for k in os.environ if k.startswith("CLAUDE"))}) + "\n")
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
if "--resume" in args:
    source = Path(value("--add-dir") or os.getcwd())
    definition = source / "src/morph/examples/crosspoint.yaml"
    definition.write_text(definition.read_text().replace('{on: route_source.succeeded, from: "*", to: routed}', "{on: route_source.succeeded, from: idle, to: routed}"))
    answer = {"implementation_complete": True, "summary": "scoped route success to idle"}
else:
    answer = {"understanding": {"transitions": "route success moves any state to routed"}, "semantic_proposal": {"change": "from idle only"},
              "morph_analysis": {}, "analysis": {"affected_subjects": ["entity:destination.state"], "relationship": "narrower",
              "test_scenarios": ["faulted stays faulted"], "unresolved_questions": []}}
print(json.dumps({"type": "result", "is_error": False, "session_id": value("--session-id") or str(uuid.uuid4()),
                  "structured_output": answer, "result": json.dumps(answer), "total_cost_usd": 0.01, "num_turns": 3}))
'''


def _has_baseline_commit():
    return subprocess.run(["git", "cat-file", "-e", f"{TASK['baseline_commit']}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return path, log


def test_tools_differ_between_arms_only_where_the_protocol_says():
    direct_analysis, direct_denied = claude_code._tools("direct_source", "pre_implementation")
    mediated_analysis, _ = claude_code._tools("morph_mediated", "pre_implementation")
    direct_implementation, direct_implementation_denied = claude_code._tools("direct_source", "implementation")
    mediated_implementation, _ = claude_code._tools("morph_mediated", "implementation")

    assert direct_analysis == ["Read", "Glob", "Grep"] and {"Edit", "Write", "Bash(morph *)"} <= set(direct_denied)
    assert {"Bash(morph *)", "Write"} <= set(mediated_analysis)
    assert set(mediated_implementation) - set(direct_implementation) == {"Bash(morph *)"}
    assert "Bash(morph *)" in direct_implementation_denied


def test_the_adapter_strips_inherited_claude_session_state(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/config")
    env = claude_code._environment()
    assert "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDECODE" not in env and env["CLAUDE_CONFIG_DIR"] == "/config"


@pytest.mark.skipif(not _has_baseline_commit(), reason="needs the full Git history for the pinned baseline commit")
def test_a_pair_runs_end_to_end_through_the_adapter(tmp_path, fake_claude, monkeypatch):
    claude, log = fake_claude
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    adapter = [sys.executable, str(ROOT / "benchmarks/adapters/claude_code.py"), "--model", "test-model", "--claude", str(claude)]

    pair = run_pair(TASK_ID, "pair-1", adapter, tmp_path / "runs", 120, pass_env=["FAKE_CLAUDE_LOG", "CLAUDE_CODE_SESSION_ID"], allow_development_fixtures=True)

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 4 and all("--safe-mode" in call["argv"] and "--strict-mcp-config" in call["argv"] for call in calls)
    assert all(call["env"] == [] for call in calls), "inherited Claude Code session state reached the model"
    mediated_resume = next(call for call in calls if "--resume" in call["argv"] and "analysis-only" in call["cwd"])
    assert mediated_resume["argv"][mediated_resume["argv"].index("--add-dir") + 1].endswith("morph_mediated/source")
    for arm in ("direct_source", "morph_mediated"):
        run = pair["runs"][arm]
        assert run["agent"] == {"provider": "anthropic", "model": "test-model", "version": "9.9.9 (Claude Code)"}
        assert run["implementation_complete"] is True and run["changed_paths"] == [TASK["baseline_definition"]]
        assert run["implementation"]["usage"]["num_turns"] == 3

    evaluator = tmp_path / "evaluator"
    (evaluator / TASK_ID).mkdir(parents=True)
    (evaluator / TASK_ID / "test_loads.py").write_text(
        "import os\nfrom pathlib import Path\nfrom morph import load_system_definition\n"
        "def test_loads():\n"
        f"    load_system_definition(Path(os.environ['MORPH_CANDIDATE_WORKSPACE']) / '{TASK['baseline_definition']}')\n"
    )
    cases = [
        {"starting_state": "idle", "event": "route_source.succeeded", "baseline_state": "routed", "candidate_state": "routed"},
        {"starting_state": "faulted", "event": "route_source.succeeded", "baseline_state": "routed", "candidate_state": "faulted"},
    ]
    reference = evaluator / "reference.json"
    reference.write_text(json.dumps({"tasks": {TASK_ID: {"category": TASK["category"], "relationship": "narrower",
                                                         "affected_subjects": ["entity:destination.state"], "cases": cases}}}))
    live = {"source": {"id": "cam1", "status": "live", "latency_ms": 40}, "destination": {"id": "wall", "status": "ready", "latency_ms": 60, "state": "idle"},
            "route": {"locked": False}, "operator": {"id": "ewan", "capabilities": ["route_control"]}}
    manifest = evaluator / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "tasks": {TASK_ID: {
        "tests": [f"{TASK_ID}/test_loads.py"], "simulation_scenarios": [{"context": live, "expected_status": "allow"}]}}}))

    records, evaluations = finalize_pair(tmp_path / "runs" / "pair-1" / "pair.json", manifest, reference, allow_development_fixtures=True)

    assert all(evaluation["implementation_complete"] and evaluation["within_boundary"] for evaluation in evaluations)
    scored = score_runs(records, json.loads(reference.read_text()), evaluations)
    assert scored["by_arm"]["morph_mediated"]["task_cases_passed_rate"] == 1.0
    assert scored["by_arm"]["direct_source"]["relationship_correct_rate"] == 1.0
