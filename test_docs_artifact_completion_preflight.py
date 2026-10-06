#!/usr/bin/env python3
"""Isolated docs completion packet regression tests; never creates live tasks."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).with_name("__init__.py")


def load_guard():
    spec = importlib.util.spec_from_file_location("docs_artifact_completion_preflight_fixture", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def consumer_fixture(root: Path) -> Path:
    """Model only real consumer declarations/metadata keys, without importing it."""
    plugin = root / "plugins" / "docs-final-output-completion-guard"
    (plugin / "quality_rules").mkdir(parents=True, exist_ok=True)
    (plugin / "scripts").mkdir(exist_ok=True)
    (plugin / "__init__.py").write_text(
        "target_suffixes = {'.pdf', '.docx', '.html'}\n"
        "ordinary_docx = 'ordinary-docx-baseline-1'\n"
        "required = ('final_output_filter', 'artifact_quality', 'finalization_manifest', 'receiver_receipt')\n",
        encoding="utf-8",
    )
    (plugin / "quality_rules" / "common.py").write_text(
        "TARGET_SUFFIXES = frozenset({'.pdf', '.html'})\n", encoding="utf-8"
    )
    return plugin / "__init__.py"


def run() -> dict[str, bool]:
    guard = load_guard()
    with tempfile.TemporaryDirectory() as tmp:
        docs_root = Path(tmp) / "docs-profile"
        consumer = consumer_fixture(docs_root)
        profiles = types.ModuleType("hermes_cli.profiles")
        setattr(profiles, "get_profile_dir", lambda profile: str(docs_root) if profile == "docs" else (_ for _ in ()).throw(ValueError(profile)))
        hermes = types.ModuleType("hermes_cli")
        hermes.__path__ = []
        with patch.dict(sys.modules, {"hermes_cli": hermes, "hermes_cli.profiles": profiles}):
            scope = {"action_mode": "production"}
            packet, error = guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": scope,
                "artifact_outputs": [{"path": "/tmp/final/index.html", "format": "html"}],
            })
            assert error is None and packet
            assert "final_output_filter.receipts" in packet
            assert "consumer sha256:" in packet and "helper requirements source sha256:" in packet
            assert "canonical final-output command:" in packet and "strict quality command:" in packet
            assert "artifact_quality" in packet and "finalization_manifest" in packet and "receiver_receipt" in packet
            assert "not QA or a passing evidence receipt" in packet
            assert "does not by itself establish a standalone HTML visual-quality pass" in packet
            assert str(consumer) in packet

            assert guard._docs_artifact_completion_packet({
                "assignee": "docs", "scope": {"action_mode": "mutate"},
                "artifact_outputs": [{"path": "/tmp/final/report.pdf", "format": "pdf"}],
            })[0]
            assert guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": {"action_mode": "read_only"},
            }) == (None, None)
            assert guard._docs_artifact_completion_packet({"work_class": "intl", "scope": scope}) == (None, None)
            assert "artifact_outputs" in guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": scope,
            })[1]
            assert "does not match" in guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": scope,
                "artifact_outputs": [{"path": "/tmp/final/index.html", "format": "pdf"}],
            })[1]

            # Normal create route: output declarations are consumed locally and
            # only the readiness packet is appended before the native handler.
            tools = types.ModuleType("tools")
            setattr(tools, "kanban_tools", types.SimpleNamespace())
            captured = {}
            setattr(guard, "_profile_jev_input_requirement", lambda _profile: (False, None))
            setattr(guard, "_scope_admission", lambda _args: (scope, None))
            setattr(guard, "_subscribe_on_completion", lambda _args: (False, None))
            setattr(guard, "_approval_record", lambda _args: (None, None))

            def create(args, subscribe):
                captured.update(args)
                captured["subscribe"] = subscribe
                return json.dumps({"ok": True, "task_id": "isolated-fixture", "status": "ready"})

            setattr(guard, "_create_without_default_completion_notice", create)
            with patch.dict(sys.modules, {"tools": tools}):
                created = json.loads(guard._guarded_create({
                    "title": "HTML output fixture", "assignee": "docs", "scope": scope,
                    "work_class": "docs", "artifact_outputs": [{"path": "/tmp/final/index.html", "format": "html"}],
                    "body": "bounded task body",
                }))
            assert created["task_id"] == "isolated-fixture"
            assert "docs artifact completion readiness packet" in captured["body"]
            assert "artifact_outputs" not in captured
            assert captured["body"].count("docs artifact completion readiness packet") == 1

            schema_context = types.SimpleNamespace(profile_name="default", registrations=[])
            schema_context.register_tool = lambda **kwargs: schema_context.registrations.append(kwargs)
            guard.register(schema_context)
            tool = next(item for item in schema_context.registrations if item["name"] == "kanban_create_guarded")
            prop = tool["schema"]["parameters"]["properties"]["artifact_outputs"]
            assert prop["items"]["additionalProperties"] is False
            assert set(prop["items"]["required"]) == {"path", "format"}

            # An absent install must fail closed for docs production, while
            # non-docs and output-less read-only routes do not need it.
            (consumer.parent / "__init__.py").unlink()
            missing, missing_error = guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": scope,
                "artifact_outputs": [{"path": "/tmp/final/a.pdf", "format": "pdf"}],
            })
            assert missing is None and "not created" in missing_error

            consumer_fixture(docs_root)
            # An incompatible dynamic expression must be rejected without execution.
            consumer.write_text("target_suffixes = dangerous()\n", encoding="utf-8")
            bad_packet, bad_error = guard._docs_artifact_completion_packet({
                "work_class": "docs", "scope": scope,
                "artifact_outputs": [{"path": "/tmp/final/a.pdf", "format": "pdf"}],
            })
            assert bad_packet is None and "not created" in bad_error

    return {
        "portable_profile_resolution_and_ast_fixture": True,
        "supported_outputs_html_limitation_and_consumer_hash_command_readback": True,
        "missing_mismatch_read_only_and_non_docs_controls": True,
        "normal_create_packet_propagation_and_local_output_consumption": True,
        "registered_schema_exposes_artifact_outputs": True,
        "missing_or_unparseable_consumer_fails_closed": True,
        "no_live_task_created": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))