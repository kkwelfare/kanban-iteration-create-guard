#!/usr/bin/env python3
"""Default-owned create -> bind exact evidence -> readback -> release handoff.

Only ops is enabled in this rollout. The caller supplies a reviewed JSON plan
with scope, environment_keys, actions; never infer tests from arbitrary prose.
Errors leave a created card blocked. No automatic retry, no global config writes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import yaml

from hermes_cli.profiles import get_profile_dir

PROFILE_HOME = Path(get_profile_dir('ops'))


def validate_plan(plan):
    assert isinstance(plan.get('scope'), str) and plan['scope'].strip()
    assert isinstance(plan.get('environment_keys', []), list)
    assert all(isinstance(k, str) for k in plan.get('environment_keys', []))
    actions = plan['actions']
    assert isinstance(actions, list) and 0 < len(actions) <= 24
    assert len({a['id'] for a in actions}) == len(actions)
    for a in actions:
        assert isinstance(a['id'], str) and a['id']
        assert a['tool'] in ('read_file', 'terminal')
        assert isinstance(a['args'], dict)
        assert 0 < a['max_age_seconds'] <= 3600
        assert a['dependencies']
        for raw in a['dependencies']:
            p = Path(raw)
            assert p.is_absolute() and p.is_file() and not p.is_symlink() and str(p.resolve()) == raw
        if a['tool'] == 'read_file':
            assert a['args']['path'] in a['dependencies']
        else:
            assert a.get('deterministic_local_test') is True
            assert set(a['args']) <= {'command', 'workdir', 'timeout'}
            assert isinstance(a['args']['command'], str) and a['args']['command']
            assert Path(a['args']['workdir']).is_absolute()
    return {'scope': plan['scope'], 'environment_keys': plan.get('environment_keys', []), 'actions': actions}


def register(home, card, plan):
    assert re.fullmatch(r't_[0-9a-f]{8}', card)
    contract = {**validate_plan(plan), 'task_id': card, 'run_id': '', 'session_id': ''}
    payload = {'enabled': True, 'bind_current_kanban_run': True, 'contract': contract}
    data = (json.dumps(payload, indent=2) + '\n').encode()
    assert len(data) <= 65536
    directory = home / 'evidence-reuse-contracts'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (card + '.json')
    # link is atomic and refuses replacement; concurrent cards have distinct names.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.link(tmp, target)
    finally:
        os.unlink(tmp)
    assert target.read_bytes() == data
    return {'path': str(target), 'sha256': hashlib.sha256(data).hexdigest()}


def cli(*args):
    result = subprocess.run(['hermes', 'kanban', *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Kanban command failed; inspect exact card before retry: ' + result.stdout[-1500:])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--body-file', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--max-runtime', default='5m')
    parser.add_argument('--hold', action='store_true', help='register but do not release the card')
    args = parser.parse_args()
    assert os.environ.get('HERMES_PROFILE', 'default') in ('', 'default'), 'default only'
    settings = yaml.safe_load((PROFILE_HOME/'config.yaml').read_text()).get('evidence_reuse', {})
    assert settings.get('enabled') is True and settings.get('task_contracts') is True
    plan = validate_plan(json.loads(args.plan.read_text()))
    body = args.body_file.read_text()
    assert '。' not in args.title + body
    assert args.workspace.is_absolute() and args.workspace.is_dir()
    task = json.loads(cli('create', args.title, '--assignee', 'ops', '--skill', 'ops-operational-loop',
                         '--initial-status', 'blocked', '--max-retries', '1', '--max-runtime', args.max_runtime,
                         '--workspace', 'dir:' + str(args.workspace), '--body', body, '--json'))
    card = task['id']
    print(json.dumps({'created_card': card}), flush=True)
    shown = json.loads(cli('show', card, '--json'))
    assert shown['task']['status'] == 'blocked' and not shown['runs']
    receipt = register(PROFILE_HOME, card, plan)
    receipt.update(task_id=card, profile='ops', skill='ops-operational-loop', released=not args.hold)
    if not args.hold:
        cli('unblock', card)
    shown = json.loads(cli('show', card, '--json'))
    receipt['readback_status'] = shown['task']['status']
    output = args.workspace / (card + '-handoff.json')
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
