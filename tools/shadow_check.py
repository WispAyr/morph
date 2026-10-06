"""Re-decide a crosspointd decision log with the MORPH models and report every disagreement.

Usage: python tools/shadow_check.py decisions.jsonl [--pretty] [--show N]

crosspointd writes the log when its `decisionLog` setting names a file (crosspoint server/src/decisionlog.mjs). Each
line is a manual take/release or a rules-engine decision: the inputs in the MORPH model's shape and crosspointd's raw
outcome. This tool classifies the raw outcome the same way the oracles do, evaluates the matching MORPH model on
the same inputs, and exits 1 if any decision differs. Outcomes it cannot classify, such as an error that is not a
routing decision, are counted and listed, never treated as agreement.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morph import MORPHRuntime, load_system_definition  # noqa: E402

MODELS = {
    "manual": ROOT / "src/morph/examples/crosspointd.yaml",
    "rule": ROOT / "src/morph/examples/crosspointd_rules.yaml",
}

# Kept in step with tools/crosspointd_oracle.mjs and tools/crosspointd_rules_oracle.mjs.
MANUAL_ERRORS = [
    (r"is a screen — it shows layouts", "screen_needs_layout"),
    (r"does not accept", "not_accepted"),
    (r"^not-granted:", "pull_not_granted"),
    (r"is ON AIR", "on_air"),
    (r"already in flight", "in_flight"),
]
RULE_KINDS = [
    (r"^disabled$", "disabled"),
    (r"rules may only target local destinations", "peer_destination"),
    (r"^destination .* is (offline|not registered)$", "destination_unavailable"),
    (r"operator-only", "operator_only"),
    (r"^release waiting: .* is on air$", "release_waiting_on_air"),
    (r"^waiting: destination on air", "queued_on_air"),
    (r"\(on air\)$", "on_air_routed"),
    (r"^idle$", "on_air_idle"),
    (r"^command in flight$", "in_flight"),
    (r"^take failed: .* retrying$", "take_retrying"),
    (r"^release failed: .* retrying$", "release_retrying"),
    (r"^taking ", "take"),
    (r"^releasing ", "release"),
    (r"^waiting: .* busy with ", "busy"),
    (r"^idle: no matching live source$", "idle_no_source"),
    (r" → ", "already_routed"),
]


class Unclassified(Exception):
    pass


def manual_outcome(raw: dict[str, Any]) -> str:
    """crosspointd's raw manual outcome as allow:<action> or deny:<reason>."""
    error = raw.get("error")
    if error:
        reason = next((kind for pattern, kind in MANUAL_ERRORS if re.search(pattern, error["message"])), None)
        if reason is None:
            raise Unclassified(f"error {error.get('status')}: {error['message']}")
        return f"deny:{reason}"
    calls = raw.get("calls") or []
    if len(calls) != 1:
        raise Unclassified(f"{len(calls)} commands for one request")
    call = calls[0]
    if "forward" in call:
        return f"allow:forward_{call['forward']}"
    if call["action"] == "propose":
        return f"allow:propose_{call['propose']}"
    return f"allow:{'forced_' if call['force'] else ''}{call['action']}"


def rule_outcome(raw: dict[str, Any]) -> str:
    """crosspointd's raw rule outcome as allow:<command>|<level> or deny:<status kind>|<level>."""
    calls = raw.get("calls") or []
    if len(calls) > 1:
        raise Unclassified(f"{len(calls)} commands in one tick")
    kind = next((name for pattern, name in RULE_KINDS if re.search(pattern, raw["text"])), None)
    if kind is None:
        raise Unclassified(f"status: {raw['text']}")
    if calls:
        return f"allow:{calls[0]['action']}|{raw['level']}"
    return f"deny:{kind}|{raw['level']}"


def morph_outcome(decision: dict[str, Any], *, levels: bool) -> str:
    text = f"allow:{decision['action']}" if decision["status"] == "allow" else f"deny:{decision.get('reason') or ''}"
    return text + (f"|{decision.get('level')}" if levels else "")


def check(records: list[dict[str, Any]]) -> dict[str, Any]:
    runtimes = {}
    for kind, path in MODELS.items():
        model = load_system_definition(path)
        runtimes[kind] = MORPHRuntime(name=model.name, version=model.version, policies=model.policies,
                                      capabilities=model.capabilities, entities=model.entities, actions=model.actions)
    agreed, disagreements, unclassified = {"manual": 0, "rule": 0}, [], []
    for line, record in records:
        kind = record.get("kind")
        if kind not in runtimes:
            unclassified.append({"line": line, "why": f"unknown record kind {kind!r}"})
            continue
        try:
            actual = manual_outcome(record["outcome"]) if kind == "manual" else rule_outcome(record["outcome"])
        except Unclassified as exc:
            unclassified.append({"line": line, "kind": kind, "why": str(exc)})
            continue
        decision = runtimes[kind].evaluate(record["context"])
        predicted = morph_outcome(decision, levels=kind == "rule")
        if predicted == actual:
            agreed[kind] += 1
        else:
            disagreements.append({"line": line, "kind": kind, "at": record.get("at"), "crosspointd": actual, "morph": predicted,
                                  "morph_policy": decision.get("policy"), "context": record["context"]})
    return {"records": len(records), "agreed": agreed, "disagreements": disagreements, "unclassified": unclassified}


def read_log(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            records.append((number, json.loads(line)))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, help="crosspointd decision log (JSON lines)")
    parser.add_argument("--show", type=int, default=20, help="Disagreements and unclassified records to list (default 20)")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    result = check(read_log(args.log))
    summary = {
        "records": result["records"], "agreed": result["agreed"],
        "disagreements": len(result["disagreements"]), "unclassified": len(result["unclassified"]),
        "first_disagreements": result["disagreements"][:args.show], "first_unclassified": result["unclassified"][:args.show],
    }
    print(json.dumps(summary, indent=2 if args.pretty else None, ensure_ascii=False))
    return 1 if result["disagreements"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
