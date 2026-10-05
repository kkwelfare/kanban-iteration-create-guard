#!/usr/bin/env python3
"""Read-only one-task Kanban wait for the default profile.

This wrapper intentionally exposes only a task id, timeout, interval, and board;
it is not a general command runner and never mutates the Kanban database.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_INIT = Path(__file__).with_name("__init__.py")


def _load_plugin():
    import hermes_cli
    source_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
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
