"""tools/shadow_check.py re-decides a crosspointd decision log with the MORPH models."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from shadow_check import Unclassified, check, manual_outcome, rule_outcome  # noqa: E402

ON_AIR_TAKE = {
    "request": {"action": "take", "force": False},
    "destination": {"id": "studio/wall", "kind": "sd-slot", "accepts": [], "operator_only": False, "peer": False,
                    "on_program": True, "program_tally": False, "command_in_flight": False},
    "source": {"id": "studio/cam1", "kind": "camera", "peer": False, "pull_granted": True},
}


def test_raw_manual_outcomes_are_classified_like_the_oracle():
    assert manual_outcome({"calls": [], "error": {"status": 409, "message": "Wall is ON AIR — a take needs force"}}) == "deny:on_air"
    assert manual_outcome({"calls": [{"action": "take", "force": True, "propose": None}]}) == "allow:forced_take"
    assert manual_outcome({"calls": [{"action": "propose", "force": False, "propose": "release"}]}) == "allow:propose_release"
    assert manual_outcome({"calls": [{"forward": "take"}]}) == "allow:forward_take"
    with pytest.raises(Unclassified):
        manual_outcome({"calls": [], "error": {"status": 502, "message": "federation not running"}})


def test_raw_rule_outcomes_are_classified_like_the_oracle():
    assert rule_outcome({"calls": [{"action": "take"}], "level": "info", "text": "taking Caller 1 → CAM 5"}) == "allow:take|info"
    assert rule_outcome({"calls": [], "level": "warn", "text": "waiting: destination on air (Caller 1 queued)"}) == "deny:queued_on_air|warn"
    with pytest.raises(Unclassified):
        rule_outcome({"calls": [], "level": "info", "text": "something new"})


def test_a_decision_the_model_would_make_differently_is_reported():
    agreeing = {"kind": "manual", "context": ON_AIR_TAKE, "outcome": {"calls": [], "error": {"status": 409, "message": "Wall is ON AIR — a take needs force"}}}
    differing = {"kind": "manual", "context": ON_AIR_TAKE, "outcome": {"calls": [{"action": "take", "force": False, "propose": None}]}}
    unknown = {"kind": "manual", "context": ON_AIR_TAKE, "outcome": {"calls": [], "error": {"status": 502, "message": "federation not running"}}}

    result = check([(1, agreeing), (2, differing), (3, unknown)])

    assert result["agreed"]["manual"] == 1
    assert [(item["line"], item["crosspointd"], item["morph"]) for item in result["disagreements"]] == [(2, "allow:take", "deny:on_air")]
    assert [item["line"] for item in result["unclassified"]] == [3]


def test_the_cli_exits_nonzero_on_a_disagreement(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text(json.dumps({"kind": "manual", "context": ON_AIR_TAKE, "outcome": {"calls": [{"action": "take", "force": False, "propose": None}]}}) + "\n")
    completed = subprocess.run([sys.executable, str(ROOT / "tools/shadow_check.py"), str(log)], capture_output=True, text=True)
    assert completed.returncode == 1 and json.loads(completed.stdout)["disagreements"] == 1


@pytest.mark.skipif(not os.environ.get("CROSSPOINT_DIR") or not shutil.which("node"), reason="set CROSSPOINT_DIR and install Node to drive crosspointd's decision log")
def test_crosspointds_own_decision_log_agrees_with_the_models(tmp_path):
    """Every oracle scenario, driven through the log crosspointd itself writes, is re-decided identically."""
    log = tmp_path / "decisions.jsonl"
    for oracle in ("crosspointd_oracle.mjs", "crosspointd_rules_oracle.mjs"):
        subprocess.run(["node", str(ROOT / "tools" / oracle), os.environ["CROSSPOINT_DIR"], "--shadow", str(log)],
                       check=True, capture_output=True)
    records = [(number, json.loads(line)) for number, line in enumerate(log.read_text().splitlines(), start=1)]

    result = check(records)

    assert result["records"] > 8000 and not result["unclassified"]
    assert not result["disagreements"], result["disagreements"][:3]
