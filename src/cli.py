"""Run from repository root: python -m src.cli --input examples/inputs/normal.json."""
import argparse
import copy
import json
import sqlite3
import sys
from pathlib import Path
from .agent import run
from .providers import ModelError, create_provider
from .storage import DraftStore

ROOT = Path(__file__).resolve().parents[1]


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def load_case(path):
    case = load_json(path)
    world = load_json(ROOT / 'data/world.json')
    world.update(copy.deepcopy(case.get('world_patch', {})))
    return case, world, load_json(ROOT / 'data/rules.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Draft-only material scheduling agent (Q1)')
    parser.add_argument('--input', required=True)
    parser.add_argument('--provider', choices=['mock', 'openai', 'deepseek'], default='mock')
    parser.add_argument('--db', default='runtime/drafts.sqlite')
    parser.add_argument('--output', help='Full actual result and trace JSON path')
    parser.add_argument('--max-steps', type=int, default=12)
    parser.add_argument('--timeout', type=float, default=90, help='Seconds per real model call')
    args = parser.parse_args(argv)
    store = None
    try:
        case, world, rules = load_case(args.input)
        provider = create_provider(args.provider, args.timeout)
        store = DraftStore(args.db)
        report = run(case['request'], world, rules, store, provider, args.max_steps)
    except (ValueError, OSError, KeyError, sqlite3.Error, ModelError) as exc:
        report = {'result': {'task_status': 'incomplete', 'stop_reason': 'configuration_or_input_error',
                             'error': str(exc), 'published': False}, 'trace': []}
    finally:
        if store:
            store.close()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n', encoding='utf-8')
    # Unicode works on Windows consoles as well as redirected CI output.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(text)
    return 0 if report['result']['task_status'] in ('completed', 'needs_confirmation') else 2


if __name__ == '__main__':
    raise SystemExit(main())
