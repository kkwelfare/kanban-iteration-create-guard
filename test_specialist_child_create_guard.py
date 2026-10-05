#!/usr/bin/env python3
"""Isolated P3c regression fixture for specialist no-parent child creation."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

SOURCE = os.environ.get("HERMES_SOURCE_ROOT")
if SOURCE:
    sys.path.insert(0, SOURCE)


def load_wrapper(name: str):
    raise RuntimeError("portable fixture mocks external handoff-control-plane wrapper integration")


def scope() -> dict:
    return {
        "outcome_target": "fixture scoped child create",
        "exact_target_candidates": ["/tmp/fixture-child"],
        "action_mode": "mutate",
        "allowed_actions": ["kanban_child_create"],
        "forbidden_actions": ["production child creation"],
        "decision_points": [],
        "completion_conditions": ["fixture readback"],
        "status": "defined",
    }


def count(kb, db: Path) -> int:
    with kb.connect_closing(db_path=db) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def set_body(kb, db: Path, task_id: str, body: str) -> None:
    with kb.connect_closing(db_path=db) as conn:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, task_id))
        conn.commit()


def create_parent(kb, db: Path) -> str:
    with kb.connect_closing(db_path=db) as conn:
        task_id = kb.create_task(conn, title="fixture parent", assignee="ops", body="fixture", initial_status="running")
    set_body(kb, db, task_id, "scope_admission_json: " + json.dumps(scope(), sort_keys=True))
    return task_id


def valid_args(current: str) -> dict:
    return {
        "title": "fixture no-parent child",
        "assignee": "research",
        "body": "fixture work\nscope_origin_task_id: " + current + "\n",
    }


def invoke(module, args: dict):
    original = copy.deepcopy(args)
    result = module._pre_tool_call(tool_name="kanban_create", args=args)
    assert args == original, "hook must not rewrite title, assignee, body, or parents"
    return result


def run() -> dict:
    from hermes_cli import kanban_db as kb
    del kb
    plugin = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("specialist_child_create_guard_plugin", plugin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    registrations = []
    class Ctx:
        profile_name = "default"
        def register_tool(self, **kwargs):
            registrations.append(kwargs)
    module.register(Ctx())
    names = sorted(item["name"] for item in registrations)
    assert names == ["kanban_create_guarded", "kanban_wait"]
    assert all(item["check_fn"]() is True for item in registrations)
    # Child origin enforcement belongs to the separate handoff-control-plane
    # plugin and is intentionally mocked/not claimed by this public package.
    assert invoke(type("NoopHook", (), {"_pre_tool_call": staticmethod(lambda **kwargs: None)})(),
                  valid_args("t_fixture_parent")) is None
    return {"result": "ok", "registered_tools": names, "external_child_guard": "mocked_not_in_package"}


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
