"""Provider adapter that runs a paired-task arm with Claude Code in headless mode.

run_paired_task.py invokes this once per phase with one JSON request on stdin and reads one JSON
response from stdout (see docs/agent-evaluation-protocol.md, "Paired Runner"):

    python benchmarks/run_paired_task.py TASK_ID pair-001 \\
      --adapter python --adapter-arg benchmarks/adapters/claude_code.py \\
      --adapter-arg --model --adapter-arg claude-opus-5-5 \\
      --pass-env HOME --pass-env ANTHROPIC_API_KEY --output-dir /private/morph-runs

Claude Code needs HOME for its configuration and session store, and an API key or a logged-in
HOME for authentication. The ``morph`` command must be on PATH for the MORPH-mediated arm.

Both arms run the same model with the same instructions and tool budget. They differ only in what
the protocol says they differ in. Before freezing its prediction, the direct-source arm reads
the source tree read-only, while the MORPH-mediated arm sees only the task and the MORPH definition,
may run the ``morph`` CLI on it, and may write scratch files (scenarios, a candidate definition)
in its analysis directory, which holds no source. During implementation both arms have the source,
and only the MORPH-mediated arm may run ``morph``.

Claude Code runs with ``--safe-mode`` and ``--strict-mcp-config``, so no CLAUDE.md, skill,
plugin, hook, memory, or MCP server reaches either arm. Answers are requested with
``--json-schema`` and validated here before they are returned.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

PROVIDER = "anthropic"
RELATIONSHIPS = ["equivalent", "narrower", "broader", "overlapping", "conflicting", "unknown"]
# Subjects use the same kind:name vocabulary as the evaluator's labels, so impact scores measure understanding, not naming.
SUBJECT_PATTERN = r"^(policy|invariant|action|capability|entity|field|transition):[A-Za-z0-9_.\-]+$"

ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["understanding", "semantic_proposal", "morph_analysis", "analysis"],
    "properties": {
        "understanding": {
            "type": "object",
            "description": "What the system does today in the area the task touches, and your assumptions.",
        },
        "semantic_proposal": {
            "type": "object",
            "description": "The change you will make, stated as behaviour: what is decided differently, and what must not change.",
        },
        "morph_analysis": {
            "type": "object",
            "description": "MORPH commands you ran and what they showed. Empty when MORPH was not available to you.",
        },
        "analysis": {
            "type": "object",
            "required": ["affected_subjects", "relationship", "test_scenarios", "unresolved_questions"],
            "properties": {
                "affected_subjects": {
                    "type": "array",
                    "items": {"type": "string", "pattern": SUBJECT_PATTERN},
                    "description": (
                        "The existing parts of the definition the change affects, as kind:name using the definition's "
                        "own names exactly. List: every policy that will decide some request differently "
                        "(policy:<name>), every invariant that must change or be removed (invariant:<name>), every "
                        "action, capability, or transition whose definition must change, and every field the task adds "
                        "(field:<entity>.<field>). Do not list fields a change only reads, parts you would add, or "
                        "parts whose definition and decisions stay the same."
                    ),
                },
                "relationship": {
                    "enum": RELATIONSHIPS,
                    "description": "How the set of requests the system allows changes: broader if it allows everything it did and more, narrower if only some of what it did, equivalent if the same, overlapping if it allows some new requests and refuses some it allowed, conflicting if the old and new sets are disjoint, unknown if you cannot tell.",
                },
                "test_scenarios": {"type": "array", "items": {"type": "string"}},
                "unresolved_questions": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

IMPLEMENTATION_SCHEMA = {
    "type": "object",
    "required": ["implementation_complete", "summary"],
    "properties": {
        "implementation_complete": {"type": "boolean", "description": "True only if the change is made and the tests you ran pass."},
        "summary": {"type": "string"},
    },
}

READ_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Edit", "Write"]
TEST_TOOLS = ["Bash(python *)", "Bash(python3 *)", "Bash(pytest *)", "Bash(git status*)", "Bash(git diff*)"]
MORPH_TOOLS = ["Bash(morph *)"]


def _tools(arm: str, phase: str) -> tuple[list[str], list[str]]:
    """Allowed and denied tools for one arm and phase."""
    mediated = arm == "morph_mediated"
    if phase == "pre_implementation":
        # The mediated arm's analysis directory holds no source, so scratch files there are harmless.
        allowed = READ_TOOLS + (EDIT_TOOLS + MORPH_TOOLS if mediated else [])
        denied = ["NotebookEdit"] + ([] if mediated else EDIT_TOOLS + MORPH_TOOLS)
    else:
        allowed = READ_TOOLS + EDIT_TOOLS + TEST_TOOLS + (MORPH_TOOLS if mediated else [])
        denied = ["NotebookEdit"] + ([] if mediated else MORPH_TOOLS)
    return allowed, denied


def _analysis_prompt(request: dict[str, Any]) -> str:
    workspace = request["workspace"]
    if request["source_access"]:
        access = (
            f"The full source tree is at {workspace}. Read whatever you need. "
            "Do not edit, create, or delete any file in this phase."
        )
    else:
        definition = request["baseline_definition"]
        access = (
            f"You do not have the source tree in this phase. {workspace} holds only task.md and "
            f"{definition}, the system's MORPH definition. Use the MORPH CLI on it, for example "
            f"`morph inspect {definition}`, `morph impact {definition} SUBJECT`, "
            f"`morph simulate {definition} --context scenarios.yaml`, and `morph diff {definition} candidate.yaml`. "
            "`morph diff` reports the invariants a candidate keeps, adds, or breaks, how the set of allowed requests "
            "changes (relationship), and under decisions which policies decide different inputs, with an example of each. "
            "You may write scratch files such as scenarios.yaml or candidate.yaml in this directory; they are "
            "discarded. Do not change the source tree, which you will get in the next phase."
        )
    return (
        "You are about to change a software system. This phase is analysis only: record your prediction "
        "before any implementation. It is frozen once you answer and cannot be revised later.\n\n"
        f"{access}\n\n"
        "Answer with the structured result: your understanding, the semantic change you propose, any MORPH "
        "analysis, and the affected subjects, relationship, test scenarios, and unresolved questions.\n\n"
        f"--- Task ---\n{request['prompt']}"
    )


def _implementation_prompt(request: dict[str, Any]) -> str:
    morph = " You may also run the morph CLI." if request["arm"] == "morph_mediated" else ""
    boundary = request.get("implementation_boundary") or []
    allowed = (
        " Change only these files, relative to the source tree; a change to any other file, including a new one, "
        "fails the task: " + ", ".join(f"`{path}`" for path in boundary) + "." if boundary else ""
    )
    return (
        "Your prediction is now frozen. Implement the task.\n\n"
        f"The source tree is at {request['workspace']}. Make all changes there, using absolute paths, and run "
        f"the tests from that directory (`cd {request['workspace']} && python -m pytest -q`).{allowed}{morph} "
        "Do not commit. When you are done, answer with the structured result: whether the implementation is "
        "complete with its tests passing, and a short summary.\n\n"
        f"--- Task ---\n{request['prompt']}"
    )


def _claude(claude: str, args: list[str], *, cwd: str, schema: dict[str, Any], model: str, arm: str, phase: str) -> dict[str, Any]:
    allowed, denied = _tools(arm, phase)
    command = [
        claude, "-p", *args,
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--safe-mode", "--strict-mcp-config",
        "--allowedTools", *allowed,
        "--disallowedTools", *denied,
    ]
    completed = subprocess.run(
        command, cwd=cwd, env=_environment(), capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude exited {completed.returncode}: {completed.stderr[-2000:]}")
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"claude did not print one JSON object: {exc}") from exc
    if not isinstance(output, dict) or output.get("is_error"):
        raise RuntimeError(f"claude reported an error: {str(output)[-2000:]}")
    return output


def _environment() -> dict[str, str]:
    """The adapter's environment without any inherited Claude Code session state.

    A nested ``claude`` that inherits a parent session's variables can attach to that session.
    """
    return {key: value for key, value in os.environ.items() if not (key.startswith("CLAUDE") and key != "CLAUDE_CONFIG_DIR")}


def _structured(output: dict[str, Any]) -> dict[str, Any]:
    """The schema-validated answer, from structured_output or a JSON object in the final result."""
    answer = output.get("structured_output")
    if answer is None and isinstance(output.get("result"), str):
        text = output["result"].strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                answer = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                answer = None
    if not isinstance(answer, dict):
        raise ValueError("claude returned no structured answer")
    return answer


def _usage(output: dict[str, Any]) -> dict[str, Any]:
    return {key: output.get(key) for key in ("total_cost_usd", "num_turns", "duration_ms") if key in output}


def _version(claude: str) -> str:
    return subprocess.run([claude, "--version"], env=_environment(), capture_output=True, text=True, check=True).stdout.strip()


def start(request: dict[str, Any], *, claude: str, model: str) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    output = _claude(
        claude, [_analysis_prompt(request), "--session-id", session_id],
        cwd=request["workspace"], schema=ANALYSIS_SCHEMA, model=model, arm=request["arm"], phase="pre_implementation",
    )
    answer = _structured(output)
    analysis = answer.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("relationship") not in RELATIONSHIPS:
        raise ValueError("claude's analysis is missing a valid relationship")
    malformed = [s for s in analysis.get("affected_subjects") or [] if not isinstance(s, str) or not re.match(SUBJECT_PATTERN, s)]
    if malformed:
        raise ValueError(f"affected subjects must be kind:name: {malformed[:5]}")
    return {
        "agent": {"provider": PROVIDER, "model": model, "version": _version(claude)},
        # Claude Code finds a session by the directory it started in, so the token carries both.
        "session_token": json.dumps({"session_id": output.get("session_id") or session_id, "cwd": request["workspace"]}),
        "pre_implementation": {key: answer.get(key) if isinstance(answer.get(key), dict) else {} for key in ANALYSIS_SCHEMA["required"]},
        "usage": _usage(output),
    }


def resume(request: dict[str, Any], *, claude: str, model: str) -> dict[str, Any]:
    token = json.loads(request["session_token"])
    args = [_implementation_prompt(request), "--resume", token["session_id"]]
    if Path(request["workspace"]).resolve() != Path(token["cwd"]).resolve():
        args += ["--add-dir", request["workspace"]]
    output = _claude(
        claude, args, cwd=token["cwd"], schema=IMPLEMENTATION_SCHEMA, model=model, arm=request["arm"], phase="implementation",
    )
    answer = _structured(output)
    if not isinstance(answer.get("implementation_complete"), bool):
        raise ValueError("claude's answer is missing a Boolean implementation_complete")
    return {
        "implementation_complete": answer["implementation_complete"],
        "summary": str(answer.get("summary", "")),
        "human_interventions": 0,
        "usage": _usage(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Claude model for both arms, for example claude-opus-5-5")
    parser.add_argument("--claude", default="claude", help="Claude Code executable")
    args = parser.parse_args()
    request = json.load(sys.stdin)
    if request.get("protocol_version") != 1:
        raise SystemExit("unsupported protocol_version")
    handlers = {"start": start, "resume": resume}
    handler = handlers.get(request.get("operation"))
    if handler is None:
        raise SystemExit(f"unsupported operation {request.get('operation')!r}")
    json.dump(handler(request, claude=args.claude, model=args.model), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
