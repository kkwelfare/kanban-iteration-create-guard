#!/usr/bin/env python3
"""Focused isolated P1 fixture for kanban_create_guarded scope admission."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).with_name("__init__.py")
SOURCE = os.environ.get("HERMES_SOURCE_ROOT")
if SOURCE:
    sys.path.insert(0, SOURCE)
from hermes_cli import kanban_db_connect as kbc


def load_module():
    spec = importlib.util.spec_from_file_location("scope_admission_fixture", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scope(*, status="defined", action_mode="mutate", decision_points=None):
    return {
        "outcome_target": "fixture target",
        "exact_target_candidates": ["/tmp/fixture-target"],
        "action_mode": action_mode,
        "allowed_actions": ["fixture create"],
        "forbidden_actions": ["gateway restart"],
        "decision_points": [] if decision_points is None else decision_points,
        "completion_conditions": ["fixture readback"],
        "status": status,
    }


def params(**overrides):
    value = {
        "title": "P1 fixture",
        "assignee": "default",
        "body": "isolated only",
        "initial_status": "blocked",
        "scope": scope(),
    }
    value.update(overrides)
    return value


def task_count(kb, db):
    with kbc.connect_closing(db_path=db) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def event_count(kb, db):
    with kbc.connect_closing(db_path=db) as conn:
        return conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]


def load_structured_contract_tests():
    configured = os.environ.get("JEV_CONTRACT_FIXTURE")
    if not configured:
        raise RuntimeError("JEV_CONTRACT_FIXTURE must name the canonical offline Jev contract test module")
    path = Path(configured)
    name = "scope_admission_structured_contract_fixture"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load structured contract fixture: {path}")
    fixture = importlib.util.module_from_spec(spec)
    sys.modules[name] = fixture
    spec.loader.exec_module(fixture)
    return fixture


def decision_input_roundtrip(module, kb, db):
    fixture = load_structured_contract_tests()
    factory = getattr(fixture, "_generic_input", None) or getattr(fixture, "_decision_input", None)
    if not callable(factory):
        raise RuntimeError("JEV_CONTRACT_FIXTURE must expose _generic_input() or _decision_input()")
    value = factory()
    now = datetime.now(timezone.utc)
    value["freshness"]["observed_at"] = (now - timedelta(minutes=1)).isoformat()
    value["freshness"]["valid_until"] = (now + timedelta(minutes=30)).isoformat()
    value["deduplication"]["checked_at"] = (now - timedelta(seconds=30)).isoformat()
    value["provenance"]["captured_at"] = (now - timedelta(seconds=20)).isoformat()

    before = (task_count(kb, db), event_count(kb, db))
    extra_field = dict(value)
    extra_field["unexpected"] = "must be rejected"
    rejected = json.loads(module._guarded_create(params(decision_input=extra_field)))
    assert rejected["ok"] is False and "unsupported field" in rejected["error"]
    missing_field = dict(value)
    missing_field.pop("objective")
    rejected_missing = json.loads(module._guarded_create(params(decision_input=missing_field)))
    assert rejected_missing["ok"] is False and "missing fields" in rejected_missing["error"]
    injected = json.loads(module._guarded_create(params(
        body="caller text jev_decision_input_json: {}", decision_input=value
    )))
    assert injected["ok"] is False and "reserved" in injected["error"]
    assert (task_count(kb, db), event_count(kb, db)) == before

    created = json.loads(module._guarded_create(params(assignee="ops", decision_input=value)))
    assert created["ok"] is True, created
    with kbc.connect_closing(db_path=db) as conn:
        row = conn.execute("SELECT body FROM tasks WHERE id = ?", (created["task_id"],)).fetchone()
    assert row is not None
    assert row["body"].count("jev_decision_input_json:") == 1
    bridge = module._ops_decision_input_bridge()
    persisted = bridge._extract_decision_input(row["body"])
    expected = copy.deepcopy(value)
    expected["provenance"]["task_id"] = created["task_id"]
    assert persisted == expected
    assert bridge._validate_decision_input(persisted, require_bound=True) == expected
    assert created["status"] == "ready"

    after = (task_count(kb, db), event_count(kb, db))
    assert after[0] == before[0] + 1
    assert after[1] >= before[1] + 1
    return {"task_id": created["task_id"], "bound_readback": True}


def run() -> dict:
    module = load_module()
    from hermes_cli import kanban_db as kb

    with tempfile.TemporaryDirectory(prefix="p1-scope-admission-") as tmp:
        db = Path(tmp) / "kanban.db"
        env_keys = (
            "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
            "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_JEV_BRIDGE_DIR",
            "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACE",
        )
        old_env = {key: os.environ.get(key) for key in env_keys}
        os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        os.environ.pop("HERMES_KANBAN_WORKSPACES_ROOT", None)
        os.environ["HERMES_KANBAN_DB"] = str(db)
        os.environ["HERMES_JEV_BRIDGE_DIR"] = os.environ.get("JEV_BRIDGE_DIR", "")
        for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
            os.environ.pop(key, None)
        try:
            before_tasks, before_events = task_count(kb, db), event_count(kb, db)
            needs_master = json.loads(module._guarded_create(params(scope=scope(
                status="needs_master", decision_points=["Tokyo address data meaning"]
            ))))
            assert needs_master == {
                "ok": False,
                "needs_master": True,
                "decision_points": ["Tokyo address data meaning"],
                "scope": scope(status="needs_master", decision_points=["Tokyo address data meaning"]),
            }
            assert (task_count(kb, db), event_count(kb, db)) == (before_tasks, before_events)

            malformed = json.loads(module._guarded_create(params(scope={"status": "defined"})))
            assert malformed["ok"] is False and "scope must contain only" in malformed["error"]
            assert (task_count(kb, db), event_count(kb, db)) == (before_tasks, before_events)

            contradictory = json.loads(module._guarded_create(params(scope=scope(
                decision_points=["unresolved meaning"]
            ))))
            assert contradictory["ok"] is False and "decision_points" in contradictory["error"]
            assert (task_count(kb, db), event_count(kb, db)) == (before_tasks, before_events)

            readonly = json.loads(module._guarded_create(params(
                title="defined read-only retrieval", scope=scope(action_mode="read_only")
            )))
            mutate = json.loads(module._guarded_create(params(
                title="defined resolved mutate", scope=scope(action_mode="mutate")
            )))
            assert readonly["ok"] is True and mutate["ok"] is True
            assert task_count(kb, db) == before_tasks + 2
            assert event_count(kb, db) >= before_events + 2

            amendment = module.format_scope_amendment({
                "reason": "new target candidate",
                "proposed_change": "add candidate after master review",
                "impact": "scope expansion",
                "requires_master": True,
            })
            assert amendment["ok"] is True and amendment["proposed"] is True
            assert module.format_scope_amendment({"reason": "missing fields"})["ok"] is False

            registrations = []
            class Ctx:
                profile_name = "default"
                def register_tool(self, **kwargs):
                    registrations.append(kwargs)
            module.register(Ctx())
            schema = registrations[0]["schema"]["parameters"]
            assert "scope" in schema["required"]
            assert "decision_input" in schema["properties"]
            assert "decision_input" not in schema["required"]
            scope_schema = schema["properties"]["scope"]
            assert scope_schema["additionalProperties"] is False
            assert scope_schema["required"] == list(module.SCOPE_FIELDS)
            decision_roundtrip = decision_input_roundtrip(module, kb, db)
            return {
                "result": "ok",
                "blocked_rows": {"tasks": before_tasks, "events": before_events},
                "allowed_rows": {"tasks": task_count(kb, db), "events": event_count(kb, db)},
                "created_task_ids": [readonly["task_id"], mutate["task_id"]],
                "schema_scope_required": True,
                "decision_input_roundtrip": decision_roundtrip,
            }
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
