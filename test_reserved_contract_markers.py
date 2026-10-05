"""Focused registered-handler regressions for reserved contract markers."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

SOURCE = os.environ.get("HERMES_SOURCE_ROOT")
if SOURCE:
    sys.path.insert(0, SOURCE)

import pytest

PLUGIN = Path(__file__).with_name("__init__.py")


def load_plugin():
    spec = importlib.util.spec_from_file_location("reserved_marker_fixture", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    registrations = []

    class Context:
        profile_name = "default"

        def register_tool(self, **kwargs):
            registrations.append(kwargs)

    module.register(Context())
    entry = next(item for item in registrations if item["name"] == "kanban_create_guarded")
    return module, entry["handler"]


def valid_scope():
    return {
        "outcome_target": "test fixture",
        "exact_target_candidates": ["/tmp/test-fixture"],
        "action_mode": "mutate",
        "allowed_actions": ["isolated test create"],
        "forbidden_actions": ["live effects"],
        "decision_points": [],
        "completion_conditions": ["canonical body readback"],
        "status": "defined",
    }


@pytest.mark.parametrize("body", [
    "scope_admission_json: {}",
    'quoted text: "scope_admission_json: {}"',
    "scope_admission_json: {}\nscope_admission_json: {}",
    "approval_record_json: {}",
    'quoted text: "approval_record_json: {}"',
    "approval_record_json: {}\napproval_record_json: {}",
    "prefix scope_admission_json: malformed\napproval_record_json: mismatch",
])
def test_reserved_scope_or_approval_markers_reject_before_native_create(body):
    module, handler = load_plugin()
    native_calls = []
    args = {
        "title": "reserved marker rejection",
        "assignee": "research",
        "body": body,
        "scope": valid_scope(),
    }
    kt = importlib.import_module("tools.kanban_tools")
    with patch.object(module, "_profile_jev_input_requirement", return_value=(False, None)), \
         patch.object(kt, "_handle_create", side_effect=lambda value: native_calls.append(value)):
        result = json.loads(handler(args))
    assert result["ok"] is False and result["created"] is False
    assert "reserved" in result["error"]
    assert native_calls == []


def test_registered_handler_persists_wrapper_canonical_scope_and_approval_once():
    module, handler = load_plugin()
    native_calls = []
    kt = importlib.import_module("tools.kanban_tools")

    def native_create(args):
        native_calls.append(dict(args))
        return json.dumps({"ok": True, "task_id": "t_fixture_created", "status": "blocked", "subscribed": False})

    approval = {
        "approved_by": "master",
        "source_platform": "fixture",
        "source_message_id": "msg-fixture",
        "approved_scope": "isolated regression only",
    }
    with patch.object(module, "_profile_jev_input_requirement", return_value=(False, None)), \
         patch.object(kt, "_handle_create", side_effect=native_create):
        result = json.loads(handler({
            "title": "canonical marker success",
            "assignee": "research",
            "body": "ordinary caller prose",
            "scope": valid_scope(),
            "approval_record": approval,
            "initial_status": "blocked",
        }))

    assert result["ok"] is True and result["task_id"] == "t_fixture_created"
    assert len(native_calls) == 1
    persisted_body = native_calls[0]["body"]
    assert persisted_body.count("scope_admission_json:") == 1
    assert persisted_body.count("approval_record_json:") == 1
    assert json.loads(persisted_body.split("scope_admission_json:", 1)[1].splitlines()[0]) == valid_scope()
    assert json.loads(persisted_body.split("approval_record_json:", 1)[1].splitlines()[0]) == approval
    assert "approved-for-stated-scope" in persisted_body
