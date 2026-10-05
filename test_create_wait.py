#!/usr/bin/env python3
"""Focused isolated regression tests for guarded create and task wait."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).with_name("__init__.py")
SOURCE = os.environ.get("HERMES_SOURCE_ROOT")
if SOURCE:
    sys.path.insert(0, SOURCE)
from hermes_cli import kanban_db_connect as kbc


def load_module():
    spec = importlib.util.spec_from_file_location("create_wait_fixture", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scope():
    return {
        "outcome_target": "isolated fixture",
        "exact_target_candidates": ["/tmp/create-wait-fixture"],
        "action_mode": "mutate",
        "allowed_actions": ["fixture create"],
        "forbidden_actions": ["gateway restart"],
        "decision_points": [],
        "completion_conditions": ["fixture readback"],
        "status": "defined",
    }


def params(**overrides):
    value = {
        "title": "create-wait fixture",
        "assignee": "default",
        "body": "isolated only",
        "initial_status": "blocked",
        "scope": scope(),
    }
    value.update(overrides)
    return value


def task_count(kb, db):
    with kbc.connect_closing(db_path=db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def event_count(kb, db):
    with kbc.connect_closing(db_path=db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])


def valid_jev_decision_input() -> dict:
    configured = os.environ.get("JEV_CONTRACT_FIXTURE")
    if not configured:
        raise RuntimeError("JEV_CONTRACT_FIXTURE must name the canonical offline Jev contract test module")
    path = Path(configured)
    spec = importlib.util.spec_from_file_location("create_wait_jev_contract_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load structured Jev contract fixture: {path}")
    fixture = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixture
    spec.loader.exec_module(fixture)
    factory = getattr(fixture, "_generic_input", None) or getattr(fixture, "_decision_input", None)
    if not callable(factory):
        raise RuntimeError("JEV_CONTRACT_FIXTURE must expose _generic_input() or _decision_input()")
    value = factory()
    now = datetime.now(timezone.utc)
    value["freshness"]["observed_at"] = (now - timedelta(minutes=1)).isoformat()
    value["freshness"]["valid_until"] = (now + timedelta(minutes=30)).isoformat()
    value["deduplication"]["checked_at"] = (now - timedelta(seconds=30)).isoformat()
    value["provenance"]["captured_at"] = (now - timedelta(seconds=20)).isoformat()
    return value


def run_jev_staging(module, kb, db) -> dict:
    jev_before = task_count(kb, db)
    missing = json.loads(module._guarded_create(params(
        title="Jev required input missing", assignee="ops",
    )))
    assert missing["ok"] is False and missing["created"] is True and missing["no_duplicate"] is True
    assert missing["task_id"] and missing["status"] == "blocked"
    assert "requires decision_input" in missing["error"]
    assert task_count(kb, db) == jev_before + 1
    with kbc.connect_closing(db_path=db) as conn:
        missing_task = kb.get_task(conn, missing["task_id"])
    assert missing_task is not None and missing_task.status == "blocked"
    assert missing_task.current_run_id is None
    assert "jev_decision_input_json:" not in missing_task.body

    malformed = json.loads(module._guarded_create(params(
        title="Jev malformed input", assignee="ops", decision_input={"provenance": {}},
    )))
    assert malformed["ok"] is False and malformed["created"] is True and malformed["no_duplicate"] is True
    assert malformed["task_id"] and malformed["status"] == "blocked"
    assert "missing fields" in malformed["error"]
    assert task_count(kb, db) == jev_before + 2
    with kbc.connect_closing(db_path=db) as conn:
        malformed_task = kb.get_task(conn, malformed["task_id"])
    assert malformed_task is not None and malformed_task.status == "blocked"
    assert malformed_task.current_run_id is None
    assert "jev_decision_input_json:" not in malformed_task.body

    valid_input = valid_jev_decision_input()
    staged = json.loads(module._guarded_create(params(
        title="Jev bound successful create", assignee="ops", decision_input=valid_input,
    )))
    assert staged["ok"] is True, staged
    assert staged["status"] == "ready"
    assert staged["jev_input"] == {
        "persisted": True,
        "marker_count": 1,
        "task_id_bound": True,
        "canonical_readback_validated": True,
    }
    with kbc.connect_closing(db_path=db) as conn:
        staged_task = kb.get_task(conn, staged["task_id"])
    assert staged_task is not None and staged_task.status == "ready"
    assert staged_task.body.count("jev_decision_input_json:") == 1
    assert staged_task.current_run_id is None
    canonical = module._ops_decision_input_bridge()
    persisted = canonical._extract_decision_input(staged_task.body)
    assert persisted["provenance"]["task_id"] == staged["task_id"]
    expected_input = json.loads(json.dumps(valid_input))
    expected_input["provenance"]["task_id"] = staged["task_id"]
    assert persisted == expected_input
    return {
        "missing": missing,
        "malformed": malformed,
        "staged": staged,
        "persisted": persisted,
    }


def run() -> dict:
    module = load_module()
    from hermes_cli import kanban_db as kb

    env_keys = (
        "HERMES_KANBAN_DB", "HERMES_PROFILE", "HERMES_SESSION_KEY",
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_JEV_BRIDGE_DIR", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACE", "HERMES_DELEGATED_CHILD_CONTEXT",
    )
    old_env = {key: os.environ.get(key) for key in env_keys}
    with tempfile.TemporaryDirectory(prefix="create-wait-fixture-") as tmp:
        db = Path(tmp) / "kanban.db"
        os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        os.environ.pop("HERMES_KANBAN_WORKSPACE", None)
        os.environ.pop("HERMES_KANBAN_WORKSPACES_ROOT", None)
        os.environ["HERMES_KANBAN_DB"] = str(db)
        os.environ["HERMES_PROFILE"] = "default"
        os.environ["HERMES_SESSION_KEY"] = "fixture-session"
        os.environ["HERMES_JEV_BRIDGE_DIR"] = os.environ.get("JEV_BRIDGE_DIR", "")
        for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
            os.environ.pop(key, None)
        try:
            with kbc.connect_closing(db_path=db):
                pass
            before_tasks, before_events = task_count(kb, db), event_count(kb, db)

            invalid = json.loads(module._guarded_create(params(initial_status="ready")))
            assert invalid["ok"] is False and "blocked" in invalid["error"] and "running" in invalid["error"]
            assert (task_count(kb, db), event_count(kb, db)) == (before_tasks, before_events)

            created = json.loads(module._guarded_create(params(title="successful isolated create")))
            assert created["ok"] is True
            assert created["status"] == "blocked"
            assert created["subscribed"] is False
            created_id = created["task_id"]

            jev = run_jev_staging(module, kb, db)
            tasks_before_partial = task_count(kb, db)
            # Native auto-subscription depends on host delivery context. Force
            # the subscribed response only for this cleanup-failure fixture,
            # so a CLI without a delivery binding exercises the same branch.
            from tools import kanban_tools as kt
            original_create = kt._handle_create
            original_remove = module._remove_auto_subscription
            def subscribed_fixture(args):
                payload = json.loads(original_create(args))
                assert payload["ok"] is True
                payload["subscribed"] = True
                return json.dumps(payload)
            kt._handle_create = subscribed_fixture
            module._remove_auto_subscription = lambda task_id, board: False
            try:
                partial = json.loads(module._guarded_create(params(title="finalization failure")))
            finally:
                kt._handle_create = original_create
                module._remove_auto_subscription = original_remove
            assert partial["ok"] is False
            assert partial["task_id"]
            assert partial["status"] == "blocked"
            assert partial["created"] is True and partial["partial"] is True and partial["no_duplicate"] is True
            assert task_count(kb, db) == tasks_before_partial + 1

            blocked_wait = module.wait_for_task({"task_id": created_id, "timeout_seconds": 0, "interval_seconds": 0.01})
            assert blocked_wait["ok"] is True and blocked_wait["outcome"] == "blocked"

            with kbc.connect_closing(db_path=db) as conn:
                assert kb.archive_task(conn, created_id)
            archived_wait = module.wait_for_task({"task_id": created_id, "timeout_seconds": 0, "interval_seconds": 0.01})
            assert archived_wait["ok"] is True
            assert archived_wait["outcome"] == "blocked" and archived_wait["status"] == "archived"

            target = json.loads(module._guarded_create(params(title="wait target", initial_status="running")))
            unrelated = json.loads(module._guarded_create(params(title="unrelated terminal", initial_status="blocked")))
            assert target["ok"] is True and unrelated["ok"] is True
            target_id, unrelated_id = target["task_id"], unrelated["task_id"]
            with kbc.connect_closing(db_path=db) as conn:
                assert kb.complete_task(conn, unrelated_id, result="unrelated done", fire_lifecycle_hook=False)
            timeout = module.wait_for_task({"task_id": target_id, "timeout_seconds": 0.02, "interval_seconds": 0.01})
            assert timeout["ok"] is True and timeout["outcome"] == "timeout" and timeout["status"] == "ready"

            with kbc.connect_closing(db_path=db) as conn:
                assert kb.complete_task(conn, target_id, result="target done", fire_lifecycle_hook=False)
            done = module.wait_for_task({"task_id": target_id, "timeout_seconds": 0, "interval_seconds": 0.01})
            assert done["ok"] is True and done["outcome"] == "done" and done["status"] == "done"

            failure_observation = {
                "task_id": "t_failure_fixture",
                "status": "blocked",
                "task": {"current_run_id": 7},
                "run": {"id": 7, "status": "gave_up", "outcome": "gave_up", "ended_at": 123},
            }
            assert module._wait_terminal_kind(failure_observation) == "failure"

            registrations = []
            class Ctx:
                profile_name = "default"
                def register_tool(self, **kwargs):
                    registrations.append(kwargs)
            module.register(Ctx())
            registered = {item["name"]: item for item in registrations}
            assert "kanban_wait" in registered
            assert registered["kanban_wait"]["schema"]["parameters"]["required"] == ["task_id"]
            initial_schema = registered["kanban_create_guarded"]["schema"]["parameters"]["properties"]["initial_status"]
            assert initial_schema["enum"] == ["running", "blocked"]
            assert registered["kanban_create_guarded"]["check_fn"]() is True
            assert registered["kanban_wait"]["check_fn"]() is True

            worker_registrations = []
            class WorkerCtx:
                profile_name = "ops"
                def register_tool(self, **kwargs):
                    worker_registrations.append(kwargs)
            module.register(WorkerCtx())
            assert {item["name"] for item in worker_registrations} == {"kanban_create_guarded", "kanban_wait"}
            assert all(item["check_fn"]() is False for item in worker_registrations)
            return {
                "result": "ok",
                "invalid_no_mutation": True,
                "created_task_ids": [created_id, partial["task_id"], jev["missing"]["task_id"], jev["malformed"]["task_id"], jev["staged"]["task_id"], target_id, unrelated_id],
                "wait_outcomes": [blocked_wait["outcome"], timeout["outcome"], done["outcome"], "failure"],
                "task_count": task_count(kb, db),
                "event_count": event_count(kb, db),
                "registered_tools": sorted(registered),
                "jev_staging": {
                    "missing_blocked": jev["missing"]["status"],
                    "malformed_blocked": jev["malformed"]["status"],
                    "valid_status": jev["staged"]["status"],
                    "task_id_bound": jev["persisted"]["provenance"]["task_id"] == jev["staged"]["task_id"],
                },
            }
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
