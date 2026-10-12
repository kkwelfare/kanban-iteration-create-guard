"""Regression tests for host-selected request-goal reference failures."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN = Path(__file__).with_name("__init__.py")

# Minimal stand-in for jev-route-screening/request_goals.py so these tests do not
# depend on a sibling checkout. It mirrors only the surface the guard uses:
# RequestGoalStore(path)._rows(), .select_for_session(session_id), GoalError.
REQUEST_GOALS_STUB = """
import json
from pathlib import Path


class GoalError(ValueError):
    pass


class RequestGoalStore:
    def __init__(self, path):
        self.path = Path(path)

    def _rows(self):
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def select_for_session(self, session_id):
        rows = self._rows()
        selections = [row for row in rows if row.get("kind") == "selection"
                      and row.get("identity", {}).get("session_id") == session_id]
        if not selections:
            return None
        root_id = selections[-1]["root_id"]
        roots = [row for row in rows if row.get("kind") == "root" and row.get("root_id") == root_id]
        if not roots or roots[-1].get("identity", {}).get("session_id") != session_id:
            return None
        return roots[-1]
"""


def load_plugin():
    spec = importlib.util.spec_from_file_location("request_goal_reference_guard_fixture", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_scope():
    return {
        "outcome_target": "fixture scoped create",
        "exact_target_candidates": ["/tmp/request-goal-fixture"],
        "action_mode": "mutate",
        "allowed_actions": ["isolated test create"],
        "forbidden_actions": ["live effects"],
        "decision_points": [],
        "completion_conditions": ["fixture readback"],
        "status": "defined",
    }


def call_create(module, monkeypatch, tmp_path, *, reference=None):
    import tools.kanban_tools as kt
    repo_root = tmp_path / "fake-root"
    expected_plugin = repo_root / "jev-route-screening" / "request_goals.py"
    expected_plugin.parent.mkdir(parents=True)
    expected_plugin.write_text(REQUEST_GOALS_STUB, encoding="utf-8")
    fake_file = repo_root / "kanban-iteration-create-guard" / "__init__.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("# fixture", encoding="utf-8")
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        if str(path) == str(PLUGIN):
            return fake_file
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir()
    ref_path = bridge_root / "request-goals.jsonl"
    if reference is not None:
        ref_path.write_text(reference, encoding="utf-8")
    monkeypatch.setenv("HERMES_JEV_BRIDGE_DIR", str(bridge_root))
    native_calls = []
    monkeypatch.setattr(module, "_profile_jev_input_requirement", lambda _: (False, None))
    monkeypatch.setattr(module, "_scope_admission", lambda args: (valid_scope(), None))
    monkeypatch.setattr(module, "_subscribe_on_completion", lambda args: (False, None))
    monkeypatch.setattr(module, "_approval_record", lambda args: (None, None))
    monkeypatch.setattr(module, "_docs_artifact_completion_packet", lambda args: (None, None))
    monkeypatch.setattr(module, "_create_without_default_completion_notice",
                        lambda args, subscribe: native_calls.append(dict(args)) or
                        json.dumps({"ok": True, "created": True, "task_id": "t_fixture"}))
    with patch.object(kt, "_handle_create", side_effect=lambda args: json.dumps({"ok": True})):
        result = json.loads(module._guarded_create({"title": "fixture", "assignee": "research",
            "body": "fixture"}, session_id="fixture-session"))
    return result, native_calls


def test_no_selection_reference_preserves_legacy_creation(tmp_path, monkeypatch):
    module = load_plugin()
    result, native_calls = call_create(module, monkeypatch, tmp_path)
    assert result["ok"] is True and result["task_id"] == "t_fixture"
    assert len(native_calls) == 1
    assert "request_goal_ref_json:" not in native_calls[0]["body"]


def test_unreadable_selection_reference_returns_error_without_creating(tmp_path, monkeypatch):
    module = load_plugin()
    import tools.kanban_tools as kt
    repo_root = tmp_path / "fake-root"
    plugin_file = repo_root / "jev-route-screening" / "request_goals.py"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text(REQUEST_GOALS_STUB, encoding="utf-8")
    fake_file = repo_root / "kanban-iteration-create-guard" / "__init__.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("# fixture", encoding="utf-8")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: fake_file if str(path) == str(PLUGIN) else original_resolve(path, *a, **k))
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir()
    (bridge_root / "request-goals.jsonl").mkdir()
    monkeypatch.setenv("HERMES_JEV_BRIDGE_DIR", str(bridge_root))
    native_calls = []
    monkeypatch.setattr(module, "_profile_jev_input_requirement", lambda _: (False, None))
    with patch.object(kt, "_handle_create", side_effect=lambda args: native_calls.append(args)):
        result = json.loads(module._guarded_create({"title": "fixture", "assignee": "research", "body": "fixture"}, session_id="fixture-session"))
    assert result["ok"] is False and result["created"] is False
    assert "unreadable or invalid" in result["error"]
    assert native_calls == []


@pytest.mark.parametrize("contents", ["{invalid json", "[]\n"])
def test_invalid_selected_reference_returns_error_without_creating(tmp_path, monkeypatch, contents):
    module = load_plugin()
    selection = json.dumps({"kind": "selection", "root_id": "root_fixture",
                            "identity": {"session_id": "fixture-session"}}) + "\n"
    result, native_calls = call_create(module, monkeypatch, tmp_path, reference=contents + selection)
    assert result["ok"] is False and result["created"] is False
    assert "request goal reference" in result["error"]
    assert native_calls == []


def test_valid_selected_reference_is_bound_to_created_card(tmp_path, monkeypatch):
    module = load_plugin()
    identity = {"session_id": "fixture-session", "turn_id": "turn", "sender_id": "owner", "platform": "discord"}
    root = {"kind": "root", "root_id": "root_fixture", "version": 1, "outcome": "fixture goal",
            "completion_conditions": [], "identity": identity, "local_milestones": []}
    selection = {"kind": "selection", "root_id": "root_fixture", "identity": identity, "version": 1}
    reference = json.dumps(root) + "\n" + json.dumps(selection) + "\n"
    result, native_calls = call_create(module, monkeypatch, tmp_path, reference=reference)
    assert result["ok"] is True
    assert len(native_calls) == 1
    marker = native_calls[0]["body"].split("request_goal_ref_json:", 1)[1].splitlines()[0].strip()
    assert json.loads(marker) == {"root_id": "root_fixture"}
