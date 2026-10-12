#!/usr/bin/env python3
"""Read-only one-task Kanban wait for the default profile.

This wrapper intentionally exposes only a task id, timeout, interval, and board;
it is not a general command runner and never mutates the Kanban database.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_INIT = Path(__file__).with_name("__init__.py")


def _hermes_source_root() -> str:
    """Locate the Hermes source tree without assuming a user-specific path."""
    try:
        import hermes_cli
        return str(Path(hermes_cli.__file__).resolve().parent.parent)
    except ImportError:
        explicit = os.environ.get("HERMES_AGENT_SOURCE", "").strip()
        if explicit:
            return str(Path(explicit).expanduser())
        home = os.environ.get("HERMES_HOME", "").strip()
        base = Path(home).expanduser() if home else Path.home() / ".hermes"
        # A profile home lives at <root>/profiles/<name>; the source sits at <root>/hermes-agent.
        if base.parent.name == "profiles":
            base = base.parent.parent
        return str(base / "hermes-agent")


_REEXEC_MARKER = "KANBAN_WAIT_REEXECUTED"


def _ensure_hermes_interpreter(source_root: str) -> None:
    """Re-run under the Hermes venv when this interpreter lacks Hermes deps.

    A bare ``python3`` on PATH may be a different interpreter than the one
    Hermes is installed into; the Kanban DB layer then fails on missing
    third-party modules. Re-exec once into ``<source>/venv`` (or ``.venv``).
    """
    try:
        importlib.import_module("hermes_cli.kanban_db")
        return
    except ImportError:
        if os.environ.get(_REEXEC_MARKER):
            raise
    current = Path(sys.executable).resolve()
    for name in ("venv", ".venv"):
        candidate = Path(source_root) / name / "bin" / "python"
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate.resolve() != current:
            env = dict(os.environ, **{_REEXEC_MARKER: "1"})
            os.execve(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]], env)
    importlib.import_module("hermes_cli.kanban_db")


def _load_plugin():
    source_root = _hermes_source_root()
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    _ensure_hermes_interpreter(source_root)
    spec = importlib.util.spec_from_file_location("kanban_iteration_create_guard_cli", PLUGIN_INIT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {PLUGIN_INIT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only wait for one Kanban task id")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--timeout-seconds", "--timeout", dest="timeout_seconds", type=float, default=300.0)
    parser.add_argument("--interval-seconds", "--interval", dest="interval_seconds", type=float, default=1.0)
    parser.add_argument("--board", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        module = _load_plugin()
        payload = module.wait_for_task(vars(args))
    except Exception as exc:
        payload = {"ok": False, "error": f"kanban_wait: {exc}"}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
