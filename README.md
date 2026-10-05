# kanban-iteration-create-guard

Guarded Kanban task creation and read-only task waiting as a Hermes native Python plugin. The wrapper covers model calls to its own tool only; it is not a universal authorization boundary and does not replace Kanban's canonical validation, approvals, or task state.

## Requirements and compatibility

- Hermes Agent `>=0.21.5`. Tested with Hermes Agent `0.21.5+6241.gc8301ea` (upstream source commit `c8301ea6c9`, 2026-09-24 build metadata).
- Python dependencies: none declared by this plugin. Hermes supplies the runtime modules for native Kanban operations.
- A Hermes-compatible `jev-route-screening` package is optional for ordinary users, but required when using an assignee profile whose active Jev producer contract requires structured `decision_input`. That package must expose `bridge.py` with callable canonical `_validate_decision_input` and `_extract_decision_input` APIs, and any sibling modules it imports. The plugin does not vendor or replace that validator.

## Install and enable

Install through Hermes from this repository, then explicitly enable it:

```sh
hermes plugins install kkwelfare/kanban-iteration-create-guard --no-enable
hermes plugins enable kanban-iteration-create-guard
```

Hermes plugins are opt-in. Review the source and requested behavior before enabling it.

## Jev bridge configuration

Configure the Jev package directory per Hermes profile using either supported plugin settings:

```yaml
plugins:
  entries:
    kanban-iteration-create-guard:
      settings:
        jev_bridge_dir: /path/to/jev-route-screening
```

or set `HERMES_JEV_BRIDGE_DIR` in the environment of the Hermes process. The directory must contain the canonical Jev `bridge.py` and its required sibling modules. Leave unset if Jev staging is not used. When a profile requires Jev input but no compatible bridge is configured, or the required APIs are missing, creation fails closed before creating a task. Configure each profile that uses this plugin; profile paths are not inferred or hardcoded.

For a Jev-required task, the wrapper creates it blocked, binds `provenance.task_id` to the generated task ID, stores a single marker, reads it back through Jev's canonical extractor, validates the exact stored object, then unblocks. Missing/malformed input or failed readback leaves the created task blocked; the plugin does not retry task creation or fall back to an ordinary create route.

## Tools

- `kanban_create_guarded`: wraps native Kanban creation and applies its declared scope-admission rules; it also preserves the docs live-browser contract and supports optional Jev structured-input staging when the target profile requires it.
- `kanban_wait`: read-only wait for one explicit task ID, ignoring unrelated tasks.

The Jev decision input is advisory context, not authorization or final judgment. The tool wrapper is cooperative coverage only; CLI, dashboard, direct DB, and other callers are outside it and retain their own validation paths.

See [the specialist creation guide](references/specialist-create.md) and [the complete payload template](templates/specialist-create.json).

## Tests and validation

Validate the package with the Hermes CLI version you intend to use:

```sh
hermes plugins validate . --json
```

The offline test suite uses the installed Hermes source/runtime, an isolated temporary Kanban database, and the canonical Jev bridge plus contract fixture. Supply those locations explicitly; no local account path is embedded in the tests:

```sh
export HERMES_SOURCE_ROOT=/path/to/hermes-agent
export JEV_BRIDGE_DIR=/path/to/jev-route-screening
export JEV_CONTRACT_FIXTURE=/path/to/jev-route-screening/test_structured_decision_input_contract.py
python test_create_wait.py
python test_scope_admission.py
python test_specialist_create_template.py
python test_specialist_child_create_guard.py
```

`test_specialist_child_create_guard.py` verifies only this package's registration; external handoff-control-plane integration is mocked and not claimed here. Tests may create `__pycache__/`; it is generated output and must not be committed.

## Privacy and side effects

The plugin may read the selected Hermes profile's plugin configuration and the configured Jev bridge package. Kanban create/write operations are performed only when its tool is called; `kanban_wait` is read-only. This plugin does not perform network calls, send telemetry, spawn background processes, or access credentials. Do not configure its bridge path to untrusted code.

## License

MIT; copyright kkwelfare. See [LICENSE](LICENSE).
