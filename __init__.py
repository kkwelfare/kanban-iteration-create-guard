"""Cooperative default create wrapper; canonical Kanban DB remains authoritative.

This tool is intentionally not a universal authorization boundary. It covers
model calls to this wrapper only; CLI, dashboard, and direct DB callers retain
their existing Kanban validation paths.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

POLICY_VERSION = "kanban-iteration-guard/1"
LIVE_BROWSER_SKILL = "docs-production-loop"
APPROVAL_FIELDS = ("approved_by", "source_platform", "source_message_id", "approved_scope")
SCOPE_FIELDS = (
    "outcome_target",
    "exact_target_candidates",
    "action_mode",
    "allowed_actions",
    "forbidden_actions",
    "decision_points",
    "completion_conditions",
    "status",
)
SCOPE_ACTION_MODES = {"read_only", "mutate", "production"}
SCOPE_STATUSES = {"defined", "needs_master"}
RECONFIRMATION_BOUNDARIES = (
    "scope_expansion",
    "external_send",
    "payment",
    "deletion",
    "authentication",
    "authorization",
    "personal_data",
    "concrete_security_risk",
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


VALID_INITIAL_STATUSES = frozenset({"blocked", "running"})
_WAIT_FAILURE_OUTCOMES = frozenset({"crashed", "failed", "gave_up", "spawn_failed", "timed_out"})
_WAIT_FAILURE_STATUSES = frozenset({"crashed", "failed", "gave_up", "timed_out"})
_JEV_BRIDGE_DIR = ""


def _wait_number(params: dict[str, Any], name: str, default: float, *, minimum: float, maximum: float) -> tuple[float | None, str | None]:
    value = params.get(name, default)
    if isinstance(value, bool):
        return None, f"{name} must be a number"
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, f"{name} must be a number"
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return None, f"{name} must be between {minimum:g} and {maximum:g}"
    return parsed, None


def _wait_connection(board: str | None) -> sqlite3.Connection:
    """Open the configured Kanban DB read-only; never initialize or mutate it."""
    from hermes_cli import kanban_db as kb

    path = Path(kb.kanban_db_path(board=board))
    if not path.is_file():
        raise FileNotFoundError(f"kanban DB not found: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _wait_observation(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    task_row = conn.execute(
        "SELECT id, status, current_run_id, started_at, completed_at, session_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task_row is None:
        return None
    run_row = conn.execute(
        "SELECT id, status, outcome, ended_at FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    task = {name: task_row[name] for name in task_row.keys()}
    run = ({name: run_row[name] for name in run_row.keys()} if run_row is not None else None)
    return {"task": task, "run": run, "task_id": task_id, "status": task["status"]}


def _wait_terminal_kind(observation: dict[str, Any]) -> str | None:
    status = observation["status"]
    run = observation["run"]
    if status == "done":
        return "done"
    if status == "archived":
        # Archival can mean abandoned/stopped work, not successful completion.
        # Return the non-success review path while preserving status=archived.
        return "blocked"
    if status in _WAIT_FAILURE_STATUSES:
        return "failure"
    if status in {"blocked", "triage"}:
        if run and run["ended_at"] is not None and run["outcome"] in _WAIT_FAILURE_OUTCOMES:
            return "failure"
        return "blocked"
    if (
        run
        and run["id"] == observation["task"].get("current_run_id")
        and run["ended_at"] is not None
        and run["outcome"] in _WAIT_FAILURE_OUTCOMES
    ):
        return "failure"
    return None


def _wait_result(kind: str, observation: dict[str, Any]) -> dict[str, Any]:
    result = {
        "ok": True,
        "task_id": observation["task_id"],
        "outcome": kind,
        "status": observation["status"],
        "state": observation,
    }
    if kind == "failure":
        run = observation.get("run") or {}
        result["failure_outcome"] = run.get("outcome") or observation["status"]
    return result


def _report_event_kinds(result: dict[str, Any]) -> tuple[str, ...]:
    """Return the narrow event-kind allowlist for one explicit report intent."""
    outcome = result.get("outcome")
    if outcome == "done":
        return ("completed",)
    if outcome == "blocked":
        # Prefer the escalation event when a same-run triage transition exists.
        return ("block_loop_detected", "blocked")
    if outcome == "failure":
        failure = result.get("failure_outcome")
        return (failure,) if failure in _WAIT_FAILURE_OUTCOMES else ()
    return ()


def _bind_wait_report_candidate(
    params: dict[str, Any], result: dict[str, Any],
) -> tuple[bool, int | None]:
    """Bind one authoritative terminal event for an explicit report intent.

    The wait result selects only the exact task/run and one relevant event kind:
    completed, blocked/triage, or the concrete failure outcome.  The same
    authoritative task read supplies the origin ``session_id``; the private
    gateway context supplies the current turn's trusted recipient fields. This
    function never acknowledges a report or infers a recipient from tool
    arguments.
    """
    event_kinds = _report_event_kinds(result)
    if not event_kinds:
        return False, None
    state = result.get("state")
    run = state.get("run") if isinstance(state, dict) else None
    task_state = state.get("task") if isinstance(state, dict) else None
    run_id = run.get("id") if isinstance(run, dict) else None
    task_id = result.get("task_id")
    authoritative_session_id = (
        task_state.get("session_id") if isinstance(task_state, dict) else None
    )
    if not isinstance(task_id, str) or not task_id.strip():
        return False, None
    if (
        not isinstance(authoritative_session_id, str)
        or not authoritative_session_id.strip()
    ):
        # Explicit report intent must use the task's trusted origin session;
        # never fall back to the currently executing user-turn session.
        return False, None
    if isinstance(run_id, bool) or (run_id is not None and (not isinstance(run_id, int) or run_id < 0)):
        return False, None
    if result.get("outcome") != "blocked" and run_id is None:
        return False, None
    try:
        from hermes_cli import kanban_db as kb
        conn = _wait_connection(params.get("board"))
    except Exception:
        return False, None
    try:
        event = next(
            (
                candidate for candidate in reversed(kb.list_events(conn, task_id))
                if candidate.task_id == task_id
                and candidate.kind in event_kinds
                and candidate.run_id == run_id
            ),
            None,
        )
    except Exception:
        return False, None
    finally:
        conn.close()
    if event is None:
        return False, None

    # The board slug is resolved by the same canonical DB namespace used for
    # the read. Only the already-bound gateway turn can provide the recipient.
    try:
        board = kb._normalize_board_slug(params.get("board")) or kb.get_current_board()
        from gateway import kanban_report_context
        context = kanban_report_context.current()
        bound = bool(
            context is not None
            and context.publish_event_candidate(
                board=board, task_id=event.task_id, event_id=event.id,
                run_id=event.run_id, kind=event.kind,
                authoritative_session_id=authoritative_session_id,
            )
        )
    except Exception:
        bound = False
    return bound, event.id


def wait_for_task(params: dict[str, Any] | None) -> dict[str, Any]:
    """Wait for one explicit task id using read-only state polling.

    The first read happens before the timeout window is evaluated, so an already
    terminal task cannot be missed.  Only this task's row and latest run are
    read; unrelated task events cannot wake or satisfy the wait.
    """
    params = dict(params or {})
    task_id = params.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return {"ok": False, "error": "task_id is required"}
    task_id = task_id.strip()
    timeout, timeout_error = _wait_number(
        params, "timeout_seconds", 300.0, minimum=0.0, maximum=3600.0)
    interval, interval_error = _wait_number(
        params, "interval_seconds", 1.0, minimum=0.01, maximum=60.0)
    if timeout_error or interval_error:
        return {"ok": False, "error": timeout_error or interval_error}
    try:
        conn = _wait_connection(params.get("board"))
    except Exception as exc:
        return {"ok": False, "error": f"kanban_wait: {exc}"}
    try:
        deadline = time.monotonic() + float(timeout)
        while True:
            observation = _wait_observation(conn, task_id)
            if observation is None:
                return {"ok": False, "task_id": task_id, "outcome": "not_found", "error": f"task {task_id} not found"}
            terminal_kind = _wait_terminal_kind(observation)
            if terminal_kind is not None:
                return _wait_result(terminal_kind, observation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _wait_result("timeout", observation)
            time.sleep(min(float(interval), remaining))
    finally:
        conn.close()


def _string_list(value: Any, *, nonempty: bool = False) -> list[str] | None:
    """Validate an exact list of non-empty strings without coercion."""
    if not isinstance(value, list) or (nonempty and not value):
        return None
    if not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def _scope_admission(args: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Consume strict wrapper scope data before native Kanban creation.

    ``needs_master`` is a non-create result. This remains a cooperative,
    default-only wrapper boundary and does not cover native CLI, dashboard,
    API, or direct database callers.
    """
    raw = args.pop("scope", None)
    if not isinstance(raw, dict):
        return None, {"ok": False, "error": "scope must be an object"}
    if set(raw) != set(SCOPE_FIELDS):
        return None, {"ok": False, "error": "scope must contain only the required scope-admission fields"}
    outcome_target = raw.get("outcome_target")
    action_mode = raw.get("action_mode")
    status = raw.get("status")
    exact_target_candidates = _string_list(raw.get("exact_target_candidates"), nonempty=True)
    allowed_actions = _string_list(raw.get("allowed_actions"), nonempty=True)
    forbidden_actions = _string_list(raw.get("forbidden_actions"))
    decision_points = _string_list(raw.get("decision_points"))
    completion_conditions = _string_list(raw.get("completion_conditions"), nonempty=True)
    if not isinstance(outcome_target, str) or not outcome_target.strip():
        return None, {"ok": False, "error": "scope.outcome_target must be a non-empty string"}
    if action_mode not in SCOPE_ACTION_MODES:
        return None, {"ok": False, "error": "scope.action_mode must be read_only, mutate, or production"}
    if status not in SCOPE_STATUSES:
        return None, {"ok": False, "error": "scope.status must be defined or needs_master"}
    if any(value is None for value in (
        exact_target_candidates, allowed_actions, forbidden_actions,
        decision_points, completion_conditions,
    )):
        return None, {"ok": False, "error": "scope list fields must contain only strings; required lists must be non-empty"}
    normalized = {
        "outcome_target": outcome_target.strip(),
        "exact_target_candidates": exact_target_candidates,
        "action_mode": action_mode,
        "allowed_actions": allowed_actions,
        "forbidden_actions": forbidden_actions,
        "decision_points": decision_points,
        "completion_conditions": completion_conditions,
        "status": status,
    }
    if status == "needs_master":
        return None, {
            "ok": False,
            "needs_master": True,
            "decision_points": decision_points,
            "scope": normalized,
        }
    if decision_points:
        return None, {"ok": False, "error": "scope.status=defined requires scope.decision_points to be empty"}
    return normalized, None


def validate_scope_amendment(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the minimal proposed-only amendment handoff shape (SC-02)."""
    fields = {"reason", "proposed_change", "impact", "requires_master"}
    if not isinstance(value, dict) or set(value) != fields:
        return None, "scope amendment must contain only reason, proposed_change, impact, requires_master"
    if not all(isinstance(value.get(field), str) and value[field].strip() for field in ("reason", "proposed_change", "impact")):
        return None, "scope amendment reason, proposed_change, and impact must be non-empty strings"
    if not isinstance(value.get("requires_master"), bool):
        return None, "scope amendment requires_master must be a boolean"
    return {
        "reason": value["reason"].strip(),
        "proposed_change": value["proposed_change"].strip(),
        "impact": value["impact"].strip(),
        "requires_master": value["requires_master"],
    }, None


def format_scope_amendment(value: Any) -> dict[str, Any]:
    """Return a proposed-only SC-02 handoff; it does not mutate card state."""
    amendment, error = validate_scope_amendment(value)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "proposed": True, "scope_amendment": amendment}


def _live_browser_contract(body: str) -> str:
    contract = (
        "\n\niteration_guard:\n"
        f"  policy_version: {POLICY_VERSION}\n"
        "  milestone_contract: browser-call-count; checkpoint-at-35; "
        "final-readback-or-continuation-at-46; exploration-block-at-56\n"
        "  checkpoint_fields: authoritative-url-or-object; completed-milestone; "
        "evidence-handle; current-ui-state-summary; exact-next-operation; rollback-path\n"
    )
    return body if "iteration_guard:" in body else body.rstrip() + contract


def _approval_record(args: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """Consume and validate inherited approval data before native creation."""
    raw = args.pop("approval_record", None)
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "approval_record must be an object"
    if set(raw) != set(APPROVAL_FIELDS):
        return None, "approval_record must contain only approved_by, source_platform, source_message_id, approved_scope"
    record = {field: str(raw[field]).strip() for field in APPROVAL_FIELDS}
    if not all(record.values()):
        return None, "approval_record fields must be non-empty"
    return record, None


def _approval_contract(body: str, record: dict[str, str] | None) -> str:
    if not record:
        return body
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    contract = (
        "\n\napproval_record_json: " + serialized + "\n"
        "approval_inheritance:\n"
        "  status: approved-for-stated-scope\n"
        "  worker_rule: do-not-request-review-for-same-approved-scope\n"
        "  reconfirm_if: " + "; ".join(RECONFIRMATION_BOUNDARIES) + "\n"
    )
    return body if "approval_record_json:" in body else body.rstrip() + contract


def _scope_admission_contract(body: str, scope: dict[str, Any]) -> str:
    """Persist validated P1 scope for later specialist child-create admission."""
    serialized = json.dumps(scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    contract = "\n\nscope_admission_json: " + serialized + "\n"
    return body if "scope_admission_json:" in body else body.rstrip() + contract


def _subscribe_on_completion(args: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Consume the per-card terminal-notification policy.

    Ordinary same-turn work is shared by the default agent after it verifies
    the worker result, so it must not also receive a watcher completion
    message.  Asynchronous/user-waiting work opts in explicitly.
    """
    value = args.pop("subscribe_on_completion", False)
    if not isinstance(value, bool):
        return None, "subscribe_on_completion must be a boolean"
    return value, None


def _remove_auto_subscription(task_id: str, board: str | None) -> bool:
    """Remove only this call's gateway/TUI auto-subscription, if present."""
    from gateway.session_context import get_session_env
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        session_key = (
            get_session_env("HERMES_SESSION_KEY", "")
            or os.environ.get("HERMES_SESSION_KEY", "")
        )
        if not session_key:
            return False
        platform, chat_id = "tui", session_key
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
    # ``tools.kanban_tools._connect`` was removed from the installed adapter.
    # Use the current public connection and notification modules directly.
    conn = kbc.connect(board=board)
    try:
        return kbn.remove_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
    finally:
        conn.close()


def _post_create_failure(payload: dict[str, Any], detail: str) -> str:
    """Report a post-create bookkeeping failure without inviting a duplicate."""
    task_id = str(payload.get("task_id") or "") or None
    return json.dumps({
        "ok": False,
        "task_id": task_id,
        "status": payload.get("status"),
        "created": bool(task_id),
        "partial": bool(task_id),
        "no_duplicate": bool(task_id),
        "subscribed": payload.get("subscribed"),
        "error": detail,
    })


def _create_without_default_completion_notice(args: dict[str, Any], subscribe: bool) -> str:
    """Use native creation, then suppress its just-created subscription by policy."""
    from tools import kanban_tools as kt

    result = kt._handle_create(args)
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return result
    if not payload.get("ok") or subscribe or not payload.get("subscribed"):
        return result
    task_id = str(payload.get("task_id") or "")
    try:
        subscription_removed = bool(
            task_id and _remove_auto_subscription(task_id, args.get("board"))
        )
    except Exception as exc:
        return _post_create_failure(
            payload,
            f"Kanban card was created but completion-subscription cleanup failed: {exc}",
        )
    if not task_id or not subscription_removed:
        return _post_create_failure(
            payload,
            "Kanban card was created but its automatic completion subscription could not be removed",
        )
    payload["subscribed"] = False
    payload["subscription_policy"] = "suppressed_for_same_turn_share"
    return json.dumps(payload)


def _ops_decision_input_bridge():
    """Load the configured Jev bridge's canonical reader and validator.

    Set HERMES_JEV_BRIDGE_DIR to the directory containing the compatible
    Jev ``bridge.py``. No profile-specific path is assumed; missing or
    incompatible bridges fail closed.
    """
    bridge_root = (_JEV_BRIDGE_DIR or os.environ.get("HERMES_JEV_BRIDGE_DIR", "")).strip()
    if not bridge_root:
        raise RuntimeError("configure plugins.entries.kanban-iteration-create-guard.settings.jev_bridge_dir or HERMES_JEV_BRIDGE_DIR")
    bridge_path = (Path(bridge_root).expanduser() / "bridge.py").resolve()
    if not bridge_path.is_file():
        raise RuntimeError(f"configured Jev bridge is unavailable: {bridge_path}")
    module_name = "_kanban_guard_jev_bridge_" + hashlib.sha256(
        str(bridge_path).encode("utf-8")
    ).hexdigest()[:16]
    module = sys.modules.get(module_name)
    if module is None:
        package_dir = bridge_path.parent
        package_name = "_kanban_guard_jev_package_" + hashlib.sha256(
            str(package_dir).encode("utf-8")
        ).hexdigest()[:16]
        package = sys.modules.get(package_name)
        if package is None:
            package_spec = importlib.util.spec_from_loader(package_name, loader=None, is_package=True)
            if package_spec is None:
                raise RuntimeError("cannot create isolated Jev bridge package")
            package = importlib.util.module_from_spec(package_spec)
            package.__path__ = [str(package_dir)]
            sys.modules[package_name] = package
        module_name = package_name + ".bridge"
        module = sys.modules.get(module_name)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(module_name, bridge_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load configured Jev decision-input bridge: {bridge_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    validator = getattr(module, "_validate_decision_input", None)
    extractor = getattr(module, "_extract_decision_input", None)
    if not callable(validator) or not callable(extractor):
        raise RuntimeError("configured Jev bridge lacks callable _validate_decision_input/_extract_decision_input APIs")
    return module


def _ops_decision_input_validator():
    return _ops_decision_input_bridge()._validate_decision_input


_DECISION_INPUT_FIELDS = frozenset({
    "schema_version", "scope", "objective", "target_candidates", "requested_outcome",
    "action_mode", "allowed_actions", "forbidden_actions", "decision_points", "freshness",
    "deduplication", "baseline", "completion_conditions", "prior_context", "signals",
    "feedback", "output_requirements", "uncertainty", "provenance", "domain_extension",
})


def _validate_decision_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("decision_input must be a complete JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("decision_input object keys must be strings")
    missing = sorted(_DECISION_INPUT_FIELDS - set(value))
    extra = sorted(set(value) - _DECISION_INPUT_FIELDS)
    if missing:
        raise ValueError("decision_input missing fields: " + ", ".join(missing))
    if extra:
        raise ValueError("decision_input contains unsupported field(s): " + ", ".join(extra))
    return _ops_decision_input_validator()(value)


def _ast_calls(node: ast.AST | None, function_name: str) -> bool:
    return bool(node) and any(
        isinstance(item, ast.Call)
        and (
            (isinstance(item.func, ast.Name) and item.func.id == function_name)
            or (isinstance(item.func, ast.Attribute) and item.func.attr == function_name)
        )
        for item in ast.walk(node)
    )


def _profile_jev_input_requirement(profile: str) -> tuple[bool | None, str | None]:
    """Resolve a target profile's active Jev structured-input contract.

    A producer role alone is not enough: the profile must have the plugin enabled,
    be included in its producer registry, and its installed hook source must
    load and validate the task-bound decision input. Unknown registry/source
    shapes fail closed instead of silently treating the profile as unaffected.
    """
    if not isinstance(profile, str) or not profile.strip():
        return None, "assignee profile is empty"
    try:
        from ruamel.yaml import YAML
        from hermes_cli.profiles import get_profile_dir

        yaml_reader = YAML(typ="safe", pure=True)

        profile_dir = Path(get_profile_dir(profile.strip()))
        config_path = profile_dir / "config.yaml"
        if not config_path.is_file():
            return None, f"profile configuration is unavailable for {profile.strip()}"
        config = yaml_reader.load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            return None, f"profile configuration is not readable for {profile.strip()}"
        plugins = config.get("plugins")
        if not isinstance(plugins, dict) or not isinstance(plugins.get("enabled"), list):
            return None, f"plugin registry is not explicit for {profile.strip()}"
        plugin_name = "jev-route-screening"
        if plugin_name not in plugins["enabled"]:
            return False, None

        entry = (plugins.get("entries") or {}).get(plugin_name)
        if not isinstance(entry, dict) or not isinstance(entry.get("settings"), dict):
            return None, f"active Jev settings are unavailable for {profile.strip()}"
        settings = entry["settings"]
        role = settings.get("role")
        if role != "producer":
            return False, None
        producer_profiles = settings.get("producer_profiles")
        if not isinstance(producer_profiles, list) or not all(
            isinstance(item, str) and item.strip() for item in producer_profiles
        ):
            return None, f"active Jev producer registry is not explicit for {profile.strip()}"
        if profile.strip() not in producer_profiles:
            return False, None
        if not isinstance(settings.get("bridge_dir"), str) or not settings["bridge_dir"].strip():
            return None, f"active Jev bridge directory is not explicit for {profile.strip()}"

        plugin_dir = profile_dir / "plugins" / plugin_name
        manifest_path = plugin_dir / "plugin.yaml"
        bridge_path = plugin_dir / "bridge.py"
        if not manifest_path.is_file() or not bridge_path.is_file():
            return None, f"active Jev hook files are unavailable for {profile.strip()}"
        manifest = yaml_reader.load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("name") != plugin_name:
            return None, f"active Jev hook manifest is not verifiable for {profile.strip()}"
        hooks = manifest.get("provides_hooks")
        if not isinstance(hooks, list) or "pre_tool_call" not in hooks:
            return None, f"active Jev pre-tool hook is not verifiable for {profile.strip()}"

        tree = ast.parse(bridge_path.read_text(encoding="utf-8"), filename=str(bridge_path))
        loader = next(
            (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == "_load_dispatcher_contract"),
            None,
        )
        contract_class = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskContract"),
            None,
        )
        producer_class = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Producer"),
            None,
        )
        pre_tool = next(
            (node for node in ast.walk(producer_class or ast.Module(body=[], type_ignores=[]))
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "pre_tool_call"),
            None,
        )
        binds_input = bool(loader) and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "TaskContract"
            and any(keyword.arg == "decision_input" for keyword in node.keywords)
            for node in ast.walk(loader)
        )
        declares_input = bool(contract_class) and any(
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "decision_input"
            for node in ast.walk(contract_class)
        )
        if not (
            _ast_calls(loader, "_extract_decision_input")
            and binds_input
            and declares_input
            and _ast_calls(pre_tool, "_validate_decision_input")
        ):
            return None, f"active Jev input requirement cannot be established for {profile.strip()}"
        return True, None
    except Exception:
        return None, f"active Jev requirement cannot be read deterministically for {profile.strip()}"


def _bind_decision_input(value: Any, task_id: str) -> dict[str, Any]:
    """Preserve caller semantics while binding provenance to the created task."""
    try:
        bound = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("decision_input must be strict JSON") from exc
    if not isinstance(bound, dict):
        raise ValueError("decision_input must be a complete JSON object")
    provenance = bound.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("decision_input.provenance must be an object")
    provenance["task_id"] = task_id
    return _validate_decision_input(bound)


def _stage_decision_input_with_api(
    task_id: str,
    decision_input: dict[str, Any],
    bridge: Any,
    *,
    get_task,
    edit_task,
    unblock_task,
) -> dict[str, Any]:
    """Persist, read back, canonically validate, then release one blocked task."""
    status = "blocked"
    try:
        task = get_task(task_id)
        if task is None:
            raise RuntimeError("created task was not found on readback")
        status = getattr(task, "status", None)
        if status != "blocked":
            raise RuntimeError(f"created task was not blocked before Jev staging (status={status})")
        body = str(getattr(task, "body", None) or "")
        marker = "jev_decision_input_json:"
        if marker in body:
            raise RuntimeError("created task body already contains the reserved decision-input marker")
        serialized = json.dumps(
            decision_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        updated_body = body.rstrip() + "\n\n" + marker + " " + serialized + "\n"
        if not edit_task(task_id, updated_body):
            raise RuntimeError("supported Kanban task edit rejected Jev input persistence")

        stored = get_task(task_id)
        if stored is None or getattr(stored, "status", None) != "blocked":
            raise RuntimeError("task did not remain blocked after Jev input persistence")
        stored_body = str(getattr(stored, "body", None) or "")
        if stored_body.count(marker) != 1:
            raise RuntimeError("persisted task body does not contain exactly one decision-input marker")
        persisted = bridge._extract_decision_input(stored_body)
        if persisted != decision_input:
            raise RuntimeError("persisted decision input differs from the bound validated object")
        if persisted.get("provenance", {}).get("task_id") != task_id:
            raise RuntimeError("persisted provenance.task_id does not match the created task id")

        if not unblock_task(task_id):
            raise RuntimeError("supported Kanban unblock rejected the validated task")
        final_task = get_task(task_id)
        if final_task is None:
            raise RuntimeError("task disappeared after Jev unblock")
        final_status = getattr(final_task, "status", None)
        if final_status not in {"ready", "todo", "running"}:
            raise RuntimeError(f"task did not enter a resumable state after Jev unblock (status={final_status})")
        return {"ok": True, "task_id": task_id, "status": final_status, "marker_count": 1}
    except Exception as exc:
        return {"ok": False, "task_id": task_id, "status": status, "error": str(exc)}


def _stage_decision_input(task_id: str, board: str | None, raw_input: Any) -> dict[str, Any]:
    """Use canonical schema and public Kanban task lifecycle APIs, never raw SQL."""
    try:
        bridge = _ops_decision_input_bridge()
        bound = _bind_decision_input(raw_input, task_id)
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(board=board) as conn:
            return _stage_decision_input_with_api(
                task_id,
                bound,
                bridge,
                get_task=lambda tid: kb.get_task(conn, tid),
                edit_task=lambda tid, body: kb.edit_task(conn, tid, body=body, board=board),
                unblock_task=lambda tid: kb.unblock_task(conn, tid),
            )
    except Exception as exc:
        return {"ok": False, "task_id": task_id, "status": "blocked", "error": str(exc)}


def _finish_jev_create(
    create_result: str,
    *,
    profile: str,
    board: str | None,
    decision_input_present: bool,
    raw_decision_input: Any,
) -> str:
    """Leave incomplete Jev-required tasks parked, without retrying creation."""
    try:
        payload = json.loads(create_result)
    except (TypeError, json.JSONDecodeError):
        return create_result
    if not payload.get("ok"):
        return create_result
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _post_create_failure(payload, "created Jev-required task has no readable task id")
    if payload.get("status") != "blocked":
        return _post_create_failure(
            payload,
            f"Jev-required task was not staged as blocked (status={payload.get('status')}); no unblock attempted",
        )
    if not decision_input_present:
        return _post_create_failure(
            payload,
            f"active Jev producer profile {profile} requires decision_input; task remains blocked",
        )

    staged = _stage_decision_input(task_id, board, raw_decision_input)
    if not staged.get("ok"):
        failure = _post_create_failure(payload, str(staged.get("error") or "Jev input staging failed"))
        try:
            failed_payload = json.loads(failure)
            failed_payload["status"] = staged.get("status") or payload.get("status")
            return json.dumps(failed_payload, ensure_ascii=False)
        except (TypeError, json.JSONDecodeError):
            return failure
    payload["status"] = staged["status"]
    payload["jev_input"] = {
        "persisted": True,
        "marker_count": staged["marker_count"],
        "task_id_bound": True,
        "canonical_readback_validated": True,
    }
    return json.dumps(payload, ensure_ascii=False)


def _guarded_create(params: dict[str, Any] | None, **host: Any) -> str:
    """Create a Kanban card, enforcing the live-browser contract when selected."""
    from tools import kanban_tools as kt

    args = dict(params or {})
    # Only bind a selected request root from host-provided session identity;
    # model parameters/body cannot select or replace that root.
    session_id = host.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        try:
            import importlib.util
            bridge_root = os.environ.get("HERMES_JEV_BRIDGE_DIR", "").strip()
            plugin_file = Path(__file__).resolve().parents[1] / "jev-route-screening" / "request_goals.py"
            if bridge_root and plugin_file.is_file():
                spec = importlib.util.spec_from_file_location("_jev_request_goal_guard", plugin_file)
                module = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(module)
                selected = module.RequestGoalStore(Path(bridge_root) / "request-goals.jsonl").select_for_session(session_id)
                if selected:
                    body = str(args.get("body") or "")
                    marker = "request_goal_ref_json:"
                    if marker in body:
                        raw = body.split(marker, 1)[1].lstrip().splitlines()[0]
                        requested = json.loads(raw)
                        if requested.get("root_id") != selected["root_id"]:
                            return json.dumps({"ok": False, "created": False,
                                "error": "request goal reference cannot replace the host-selected root"})
                    else:
                        args["body"] = body.rstrip() + "\n\nrequest_goal_ref_json: " + json.dumps(
                            {"root_id": selected["root_id"]}, separators=(",", ":")) + "\n"
        except Exception:
            # Unknown/unavailable host binding preserves the legacy unbound path.
            pass
    if "jev_decision_input_json:" in str(args.get("body") or ""):
        return json.dumps({"ok": False, "error": "body contains reserved decision_input marker"}, ensure_ascii=False)
    decision_input_present = "decision_input" in args
    raw_decision_input = args.pop("decision_input", None)
    decision_input = None

    title = str(args.get("title") or "").strip()
    assignee = str(args.get("assignee") or "").strip()
    if not title or not assignee:
        return json.dumps({"ok": False, "error": "title and assignee are required"})

    jev_requires_input, jev_requirement_error = _profile_jev_input_requirement(assignee)
    if jev_requires_input is None:
        return json.dumps({
            "ok": False,
            "created": False,
            "error": jev_requirement_error or f"active Jev requirement is unknown for {assignee}",
        }, ensure_ascii=False)
    if not jev_requires_input and decision_input_present:
        try:
            decision_input = _validate_decision_input(raw_decision_input)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"decision_input invalid: {exc}"}, ensure_ascii=False)
    if jev_requires_input:
        try:
            _ops_decision_input_bridge()
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "created": False,
                "error": f"canonical Ops Jev bridge is unavailable; task not created: {exc}",
            }, ensure_ascii=False)

    initial_status = args.get("initial_status", "running")
    if not isinstance(initial_status, str) or initial_status not in VALID_INITIAL_STATUSES:
        return json.dumps({
            "ok": False,
            "error": "initial_status must be one of ['blocked', 'running']",
        })

    scope, scope_result = _scope_admission(args)
    if scope_result:
        return json.dumps(scope_result)
    args["body"] = _scope_admission_contract(str(args.get("body") or ""), scope)

    subscribe_on_completion, subscription_error = _subscribe_on_completion(args)
    if subscription_error:
        return json.dumps({"ok": False, "error": subscription_error})

    approval_record, approval_error = _approval_record(args)
    if approval_error:
        return json.dumps({"ok": False, "error": approval_error})
    if approval_record:
        args["body"] = _approval_contract(str(args.get("body") or ""), approval_record)
    if decision_input is not None and not jev_requires_input:
        serialized = json.dumps(
            decision_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        args["body"] = str(args.get("body") or "").rstrip() + "\n\n" + "jev_decision_input_json: " + serialized + "\n"

    if jev_requires_input:
        # The generated task ID does not exist yet. Keep the card non-claimable
        # until its marker has been rebound, persisted, and read back.
        args["initial_status"] = "blocked"

    work_class = str(args.pop("work_class", "") or "").strip().lower()
    execution_class = str(args.pop("execution_class", "") or "").strip().lower()
    is_docs_live_browser = work_class == "docs" and execution_class == "live_browser"
    if not is_docs_live_browser:
        create_result = _create_without_default_completion_notice(args, subscribe_on_completion)
        if jev_requires_input:
            return _finish_jev_create(
                create_result,
                profile=assignee,
                board=args.get("board"),
                decision_input_present=decision_input_present,
                raw_decision_input=raw_decision_input,
            )
        return create_result

    skills = _as_list(args.get("skills"))
    if LIVE_BROWSER_SKILL not in skills:
        skills.append(LIVE_BROWSER_SKILL)
    body = str(args.get("body") or "")
    args["skills"] = skills
    args["body"] = _live_browser_contract(body)

    try:
        kb, conn = kt._connect(board=args.get("board"))
        try:
            task_id = kb.create_task(
                conn,
                title=title,
                body=args["body"],
                assignee=assignee,
                parents=tuple(_as_list(args.get("parents"))),
                tenant=args.get("tenant") or os.environ.get("HERMES_TENANT"),
                priority=int(args.get("priority") or 0),
                workspace_kind=str(args.get("workspace_kind") or "scratch"),
                workspace_path=args.get("workspace_path"),
                project_id=args.get("project") or args.get("project_id"),
                triage=bool(args.get("triage", False)),
                idempotency_key=args.get("idempotency_key"),
                max_runtime_seconds=(int(args["max_runtime_seconds"])
                                     if args.get("max_runtime_seconds") is not None else None),
                max_retries=1,
                skills=skills,
                goal_mode=bool(args.get("goal_mode", False)),
                goal_max_turns=(int(args["goal_max_turns"])
                                if args.get("goal_max_turns") is not None else None),
                initial_status=str(args.get("initial_status") or "running"),
                created_by=os.environ.get("HERMES_PROFILE") or "default",
                session_id=args.get("session_id") or os.environ.get("HERMES_SESSION_ID"),
            )
            task = kb.get_task(conn, task_id)
            subscribed = kt._maybe_auto_subscribe(conn, task_id) if subscribe_on_completion else False
            create_result = json.dumps({
                "ok": True,
                "task_id": task_id,
                "status": task.status if task else None,
                "subscribed": subscribed,
                "guard": {
                    "policy_version": POLICY_VERSION,
                    "enforced": {
                        "skills": [LIVE_BROWSER_SKILL],
                        "max_retries": 1,
                        "milestone_contract": True,
                    },
                    "coverage": "cooperative model-tool wrapper only",
                },
            })
        finally:
            conn.close()
    except Exception as exc:
        create_result = json.dumps({"ok": False, "error": f"kanban_create_guarded: {exc}"})
    if jev_requires_input:
        return _finish_jev_create(
            create_result,
            profile=assignee,
            board=args.get("board"),
            decision_input_present=decision_input_present,
            raw_decision_input=raw_decision_input,
        )
    return create_result


KANBAN_WAIT_SCHEMA = {
    "name": "kanban_wait",
    "description": "Wait for one explicit Kanban task id using read-only state polling. Returns done, blocked, failure, or timeout; unrelated task events are ignored. Set report_intent=true explicitly when a terminal result is the substantive report for this same gateway turn; that binds one exact event candidate only, and a successful non-empty final transport is still required.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Explicit task id to wait on."},
            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 3600, "description": "Maximum wait window; default 300 seconds."},
            "interval_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 60, "description": "Read interval; default 1 second."},
            "board": {"type": "string", "description": "Optional Kanban board slug."},
            "report_intent": {"type": "boolean", "default": False, "description": "Explicitly opt in to binding one authoritative terminal event (completion, blocked/triage, or concrete failure) as this turn's report candidate. This is not an ACK; only the successful final transport can create the durable receipt."},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}


def _handle_wait(args: dict[str, Any], **_: Any) -> str:
    params = dict(args or {})
    report_intent = params.get("report_intent", False)
    if not isinstance(report_intent, bool):
        return json.dumps({"ok": False, "error": "report_intent must be a boolean"}, ensure_ascii=False)
    result = wait_for_task(params)
    if report_intent:
        bound, event_id = _bind_wait_report_candidate(params, result)
        result["report_candidate_bound"] = bound
        result["report_event_id"] = event_id
    return json.dumps(result, ensure_ascii=False)


def register(ctx):
    global _JEV_BRIDGE_DIR
    get_config = getattr(ctx, "get_config", None)
    configured_bridge_dir = get_config("jev_bridge_dir", "") if callable(get_config) else ""
    _JEV_BRIDGE_DIR = configured_bridge_dir.strip() if isinstance(configured_bridge_dir, str) else ""
    # Default alone gets the wrapper.  Do not depend on the native Kanban
    # worker/orchestrator gate: that would either hide this one tool from the
    # normal default profile or require exposing the complete Kanban surface.
    def _check_default_profile() -> bool:
        return ctx.profile_name == "default"

    ctx.register_tool(
        name="kanban_create_guarded",
        toolset="kanban",
        schema={
            "name": "kanban_create_guarded",
            "description": "Create a Kanban card. For docs live-browser cards, enforce docs-production-loop, max_retries=1, and an iteration milestone contract. For assignees with an active Jev producer hook requiring structured input, create blocked, bind and persist the input to the generated task ID, verify the stored marker with the canonical bridge, then unblock; missing or invalid input stays blocked. Other profiles retain native creation behavior.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "assignee": {"type": "string"},
                    "body": {"type": "string"},
                    "decision_input": {
                        "type": "object",
                        "description": "Optional complete schema v1 input. Required when the target profile's active Jev producer hook validates structured input; the wrapper binds provenance.task_id to the generated card ID, persists one marker while blocked, validates its exact stored value, then unblocks. Missing or malformed input leaves the created task blocked.",
                    },
                    "scope": {
                        "type": "object",
                        "description": "Required P1 scope admission. needs_master returns a non-create structured result; defined with no decision_points permits this wrapper route only.",
                        "properties": {
                            "outcome_target": {"type": "string", "minLength": 1},
                            "exact_target_candidates": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                            "action_mode": {"type": "string", "enum": ["read_only", "mutate", "production"]},
                            "allowed_actions": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                            "forbidden_actions": {"type": "array", "items": {"type": "string", "minLength": 1}},
                            "decision_points": {"type": "array", "items": {"type": "string", "minLength": 1}},
                            "completion_conditions": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                            "status": {"type": "string", "enum": ["defined", "needs_master"]}
                        },
                        "required": ["outcome_target", "exact_target_candidates", "action_mode", "allowed_actions", "forbidden_actions", "decision_points", "completion_conditions", "status"],
                        "additionalProperties": False
                    },
                    "approval_record": {
                        "type": "object",
                        "description": "Structured inherited approval for exactly this stated scope. Requires approved_by, source_platform, source_message_id, and approved_scope."
                    },
                    "work_class": {"type": "string", "description": "Use docs for docs-owned work."},
                    "execution_class": {"type": "string", "description": "Use live_browser only for serial authenticated/browser work."},
                    "subscribe_on_completion": {"type": "boolean", "description": "Set true only when an asynchronous or user-waiting card needs a terminal completion notification. Defaults to false for same-turn default sharing."},
                    "parents": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "tenant": {"type": "string"},
                    "priority": {"type": "integer"},
                    "workspace_kind": {"type": "string"},
                    "workspace_path": {"type": "string"},
                    "project": {"type": "string"},
                    "triage": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                    "max_runtime_seconds": {"type": "integer"},
                    "goal_mode": {"type": "boolean"},
                    "goal_max_turns": {"type": "integer"},
                    "initial_status": {"type": "string", "enum": ["running", "blocked"], "description": "Initial card status. Only running or blocked are supported by native create."},
                    "board": {"type": "string"}
                },
                "required": ["title", "assignee", "scope"]
            }
        },
        handler=_guarded_create,
        check_fn=_check_default_profile,
        description="Cooperative guarded Kanban create for docs live-browser cards and active Jev decision-input producers.",
        emoji="🧭",
    )
    ctx.register_tool(
        name="kanban_wait",
        toolset="kanban",
        schema=KANBAN_WAIT_SCHEMA,
        handler=_handle_wait,
        check_fn=_check_default_profile,
        description="Read-only explicit task-ID wait for the default profile.",
        emoji="⏳",
    )
