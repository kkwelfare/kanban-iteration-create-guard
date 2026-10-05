#!/usr/bin/env python3
"""Offline checks for the registered specialist-create payload template."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
TEMPLATE = PLUGIN_DIR / "templates" / "specialist-create.json"
BRIDGE_DIR = os.environ.get("JEV_BRIDGE_DIR", "")
FIXTURE_PATH = os.environ.get("JEV_CONTRACT_FIXTURE", "")


def load(name: str, path: Path):
    package_dir = path.parent
    package_name = name + "_package"
    package_spec = importlib.util.spec_from_loader(package_name, loader=None, is_package=True)
    if package_spec is None:
        raise RuntimeError("cannot create isolated package for the Jev bridge fixture")
    package = importlib.util.module_from_spec(package_spec)
    package.__path__ = [str(package_dir)]
    sys.modules[package_name] = package
    name = package_name + "." + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fixture module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run() -> dict[str, bool]:
    guard = load("specialist_create_template_guard", PLUGIN_DIR / "__init__.py")
    if not BRIDGE_DIR or not FIXTURE_PATH:
        raise RuntimeError("set JEV_BRIDGE_DIR and JEV_CONTRACT_FIXTURE to compatible Jev package paths")
    bridge = load("specialist_create_template_jev_bridge", Path(BRIDGE_DIR) / "bridge.py")
    fixture = load("specialist_create_template_contract_fixture", Path(FIXTURE_PATH))
    payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    assert payload["scope"]["status"].startswith("<")
    assert payload["decision_input"]["freshness"]["observed_at"].startswith("<")
    assert payload["decision_input"]["deduplication"]["checked_at"].startswith("<")
    assert payload["decision_input"]["provenance"]["captured_at"].startswith("<")
    try:
        scope_result = guard._scope_admission({"scope": payload["scope"]})
    except (AssertionError, TypeError, ValueError):
        scope_result = None
    assert scope_result is None or (scope_result[0] is None and scope_result[1])

    scope = {
        "outcome_target": "offline fixture target",
        "exact_target_candidates": ["/fixture/target"],
        "action_mode": "read_only",
        "allowed_actions": ["validate fixture"],
        "forbidden_actions": ["create live task"],
        "decision_points": [],
        "completion_conditions": ["canonical scope validator accepts"],
        "status": "defined",
    }
    admitted, error = guard._scope_admission({"scope": copy.deepcopy(scope)})
    assert error is None and admitted == scope

    missing_status = copy.deepcopy(scope)
    del missing_status["status"]
    rejected, error = guard._scope_admission({"scope": missing_status})
    assert rejected is None and error and "required scope-admission fields" in error["error"]

    now = datetime.now(timezone.utc).isoformat()
    decision = fixture._generic_input()
    decision["freshness"].update(status="not_time_sensitive", observed_at=now, valid_until=None)
    decision["freshness"]["rule"] = "offline fixture; no time-sensitive data"
    decision["deduplication"].update(status="checked", checked_at=now, basis="offline fixture comparison")
    decision["provenance"].update(captured_at=now, source="offline template test fixture")
    decision["provenance"]["source_references"] = ["offline://specialist-create-template-fixture"]
    checked = bridge._validate_decision_input(decision)
    assert checked == decision

    missing_checked_at = copy.deepcopy(decision)
    del missing_checked_at["deduplication"]["checked_at"]
    try:
        bridge._validate_decision_input(missing_checked_at)
    except Exception as exc:
        assert "deduplication.checked_at" in str(exc)
    else:
        raise AssertionError("canonical validator accepted missing deduplication.checked_at")

    unfilled = copy.deepcopy(payload["decision_input"])
    try:
        guard._validate_decision_input(unfilled)
    except Exception:
        pass
    else:
        raise AssertionError("canonical validator accepted the unfilled decision_input template")

    return {
        "template_not_runnable_unfilled": True,
        "scope_positive_and_missing_status": True,
        "decision_input_positive_and_missing_checked_at": True,
        "no_live_create_performed": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
