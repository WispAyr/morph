"""Run both arms of one task through a provider-neutral JSON adapter."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "benchmarks/agent-study/corpus.json"
EXCLUDED_AGENT_FILES = {"benchmarks/agent-study/reference.json"}


def _run_git(args: list[str], *, cwd: Path = REPO_ROOT, capture: bool = False) -> bytes | None:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.PIPE,
    )
    return completed.stdout if capture else None


def _snapshot(commit: str, destination: Path) -> str:
    """Export one commit without its history or evaluator files, then init local Git."""
    destination.mkdir(parents=True, mode=0o700)
    archive = _run_git(["archive", "--format=tar", commit], capture=True)
    assert archive is not None
    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe path in baseline archive: {member.name}")
            if relative.as_posix() in EXCLUDED_AGENT_FILES:
                continue
            target = (root / Path(*relative.parts)).resolve()
            if root not in target.parents:
                raise ValueError(f"unsafe path in baseline archive: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"unable to read baseline archive member: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(f"unsupported baseline archive member: {member.name}")

    _run_git(["init", "--quiet"], cwd=destination)
    _run_git(["add", "-A"], cwd=destination)
    _run_git([
        "-c", "user.name=MORPH Evaluation", "-c", "user.email=morph-eval@localhost",
        # A throwaway local snapshot: never signed, so a user's signing setup cannot break or slow a run.
        "-c", "commit.gpgsign=false",
        "commit", "--quiet", "-m", "Pinned baseline snapshot",
    ], cwd=destination)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=destination, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return revision


def _adapter_environment(pass_env: list[str]) -> dict[str, str]:
    env_names = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
    env = {name: os.environ[name] for name in env_names if name in os.environ}
    for name in pass_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid environment variable name '{name}'")
        if name not in os.environ:
            raise ValueError(f"requested adapter environment variable '{name}' is not set")
        env[name] = os.environ[name]
    return env


def _invoke_adapter(
    command: list[str], request: dict[str, Any], workspace: Path, timeout: int, env: dict[str, str],
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=workspace,
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"adapter exited {completed.returncode}: {completed.stderr[-4000:]}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"adapter stdout must be one JSON object: {exc}") from exc
    if not isinstance(response, dict):
        raise ValueError("adapter response must be a JSON object")
    return response


def _validate_start(response: dict[str, Any], label: str) -> tuple[dict[str, str], str, dict[str, Any]]:
    agent = response.get("agent")
    required_agent_fields = ("provider", "model", "version")
    if not isinstance(agent, dict) or any(not isinstance(agent.get(key), str) or not agent[key] for key in required_agent_fields):
        raise ValueError(f"{label}: adapter must return agent provider, model, and version")
    session = response.get("session_token")
    if not isinstance(session, str) or not session:
        raise ValueError(f"{label}: adapter must return an opaque session_token")
    artifact = response.get("pre_implementation")
    if not isinstance(artifact, dict):
        raise ValueError(f"{label}: adapter must return a pre_implementation object")
    analysis = artifact.get("analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("affected_subjects"), list):
        raise ValueError(f"{label}: pre_implementation.analysis must include affected_subjects")
    if not all(isinstance(item, str) for item in analysis["affected_subjects"]):
        raise ValueError(f"{label}: affected_subjects must contain strings")
    if analysis.get("relationship") not in {"equivalent", "narrower", "broader", "overlapping", "conflicting", "unknown"}:
        raise ValueError(f"{label}: analysis.relationship is invalid")
    for key in ("understanding", "semantic_proposal", "morph_analysis"):
        if not isinstance(artifact.get(key), dict):
            raise ValueError(f"{label}: pre_implementation.{key} must be an object")
    return agent, session, artifact


def _write_frozen_artifact(path: Path, artifact: dict[str, Any]) -> str:
    payload = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8", errors="strict")
    path.chmod(0o400)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_prompt_workspace(task: dict[str, Any], destination: Path) -> str:
    return _snapshot(task["baseline_commit"], destination)


def _analysis_workspace(task: dict[str, Any], prompt: str, destination: Path) -> Path:
    destination.mkdir(parents=True, mode=0o700)
    definition = subprocess.run(
        ["git", "show", f"{task['baseline_commit']}:{task['baseline_definition']}"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    (destination / "task.md").write_text(prompt, encoding="utf-8")
    (destination / "morph.yaml").write_text(definition, encoding="utf-8")
    return destination


def run_pair(
    task_id: str,
    pair_id: str,
    adapter: list[str],
    output_dir: Path,
    timeout: int,
    *,
    pass_env: list[str] | None = None,
    allow_development_fixtures: bool = False,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pair_id):
        raise ValueError("pair_id must be a simple identifier containing letters, digits, '.', '_' or '-'")
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    task = next((item for item in corpus["tasks"] if item.get("task_id") == task_id), None)
    if task is None:
        raise ValueError(f"unknown task '{task_id}'")
    if task.get("evaluation_status") != "frozen" and not allow_development_fixtures:
        raise ValueError(f"task '{task_id}' is a development fixture and cannot be run as a benchmark task")
    prompt_path = (REPO_ROOT / task["prompt"]).resolve()
    prompt = prompt_path.read_text(encoding="utf-8")
    adapter_env = _adapter_environment(pass_env or [])

    output_dir = output_dir.resolve()
    if REPO_ROOT == output_dir or REPO_ROOT in output_dir.parents:
        raise ValueError("output directory must be outside the source repository")
    pair_dir = output_dir / pair_id
    pair_dir.mkdir(parents=True, mode=0o700)
    arms = ["direct_source", "morph_mediated"]
    random.SystemRandom().shuffle(arms)
    run_results: dict[str, dict[str, Any]] = {}

    for arm in arms:
        started = time.monotonic()
        arm_dir = pair_dir / arm
        arm_dir.mkdir(mode=0o700)
        if arm == "direct_source":
            workspace = arm_dir / "source"
            baseline_revision = _source_prompt_workspace(task, workspace)
            source_access = True
        else:
            workspace = _analysis_workspace(task, prompt, arm_dir / "analysis-only")
            baseline_revision = None
            source_access = False

        start_request = {
            "protocol_version": 1,
            "operation": "start",
            "task_id": task_id,
            "pair_id": pair_id,
            "arm": arm,
            "phase": "pre_implementation",
            "prompt": prompt,
            "workspace": str(workspace),
            "source_access": source_access,
            "baseline_definition": task["baseline_definition"] if source_access else "morph.yaml",
            "semantic_operations": ["inspect", "impact", "propose", "diff", "plan", "simulate", "reason"],
        }
        start_response = _invoke_adapter(adapter, start_request, workspace, timeout, adapter_env)
        agent, session_token, pre_implementation = _validate_start(start_response, arm)

        analysis_path = arm_dir / "pre_implementation.json"
        analysis_hash = _write_frozen_artifact(analysis_path, pre_implementation)
        if arm == "direct_source":
            revision_after_prediction = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            if revision_after_prediction != baseline_revision:
                raise ValueError("direct-source adapter changed the baseline before freezing its prediction")
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=workspace,
                check=True, capture_output=True, text=True,
            ).stdout
            if dirty:
                raise ValueError("direct-source adapter edited files before freezing its prediction")

        if arm == "morph_mediated":
            workspace = arm_dir / "source"
            baseline_revision = _source_prompt_workspace(task, workspace)

        resume_request = {
            "protocol_version": 1,
            "operation": "resume",
            "session_token": session_token,
            "task_id": task_id,
            "pair_id": pair_id,
            "arm": arm,
            "phase": "implementation",
            "prompt": prompt,
            "workspace": str(workspace),
            "source_access": True,
            "baseline_definition": task["baseline_definition"],
            # The files the task may change. The evaluator fails any change outside them, so the agent is told.
            "implementation_boundary": task["implementation_boundary"],
            "pre_implementation": pre_implementation,
            "pre_implementation_sha256": analysis_hash,
        }
        implementation = _invoke_adapter(adapter, resume_request, workspace, timeout, adapter_env)
        if "patch" in implementation and not isinstance(implementation["patch"], str):
            raise ValueError(f"{arm}: implementation.patch must be a string when supplied")
        if not isinstance(implementation.get("implementation_complete"), bool):
            raise ValueError(f"{arm}: implementation response must include Boolean implementation_complete")
        interventions = implementation.get("human_interventions", 0)
        if not isinstance(interventions, int) or isinstance(interventions, bool) or interventions < 0:
            raise ValueError(f"{arm}: human_interventions must be a non-negative integer")
        _run_git(["add", "-N", "--", "."], cwd=workspace)
        patch = subprocess.run(
            ["git", "diff", "--binary", baseline_revision, "--"],
            cwd=workspace, check=True, capture_output=True,
        ).stdout.decode("utf-8", errors="replace")
        changed_paths = subprocess.run(
            ["git", "diff", "--name-only", baseline_revision, "--"],
            cwd=workspace, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        (arm_dir / "patch.diff").write_text(patch, encoding="utf-8")
        run_results[arm] = {
            "agent": agent,
            "baseline_commit": task["baseline_commit"],
            "source_snapshot_revision": baseline_revision,
            "pre_implementation": pre_implementation,
            "pre_implementation_sha256": analysis_hash,
            "implementation": {key: value for key, value in implementation.items() if key != "session_token"},
            "patch": patch,
            "changed_paths": changed_paths,
            "implementation_complete": implementation["implementation_complete"],
            "human_interventions": interventions,
            "workspace": str(workspace),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    direct = run_results["direct_source"]["agent"]
    mediated = run_results["morph_mediated"]["agent"]
    if any(direct[key] != mediated[key] for key in ("provider", "model", "version")):
        raise ValueError("paired arms must use the same provider, model, and version")

    result = {
        "protocol_version": 1,
        "task_id": task_id,
        "pair_id": pair_id,
        "evaluation_status": task["evaluation_status"],
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "adapter_environment_names": sorted(adapter_env),
        "arm_order": arms,
        "runs": run_results,
    }
    (pair_dir / "pair.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("pair_id")
    parser.add_argument("--adapter", required=True, help="Provider adapter executable")
    parser.add_argument("--adapter-arg", action="append", default=[], help="Additional adapter argv item; repeat as needed")
    parser.add_argument("--output-dir", type=Path, required=True, help="Private directory for workspaces and run artifacts")
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per adapter phase")
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME", help="Pass this environment variable to the adapter; repeat as needed")
    parser.add_argument("--allow-development-fixtures", action="store_true", help="Permit runs that are not benchmark evidence")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        adapter = [args.adapter, *args.adapter_arg]
        if "/" in adapter[0]:
            adapter[0] = str(Path(adapter[0]).resolve())
        result = run_pair(
            args.task_id, args.pair_id,
            adapter, args.output_dir, args.timeout,
            pass_env=args.pass_env,
            allow_development_fixtures=args.allow_development_fixtures,
        )
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(json.dumps({"task_id": result["task_id"], "pair_id": result["pair_id"], "arm_order": result["arm_order"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
