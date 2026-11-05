"""Execute actual cases. Real providers never fall back to Mock."""
import argparse
import json
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from src.agent import run
from src.cli import ROOT, load_case
from src.providers import create_provider
from src.storage import DraftStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=['mock', 'openai', 'deepseek'], default='mock')
    parser.add_argument('--cases', nargs='+', default=['normal', 'missing', 'supplement', 'risk', 'manual_review',
                        'conflict', 'confirmed', 'save_failure', 'query_retry', 'race', 'no_slots', 'changed_occupancy'])
    parser.add_argument('--timeout', type=float, default=90)
    args = parser.parse_args()
    provider = create_provider(args.provider, args.timeout)
    output = ROOT / 'examples' / args.provider
    output.mkdir(parents=True, exist_ok=True)
    summary = {'provider': provider.name, 'model': provider.model, 'evidence': provider.evidence,
               'ran_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(), 'cases': []}
    for name in args.cases:
        case, world, rules = load_case(ROOT / 'examples/inputs' / (name + '.json'))
        with tempfile.TemporaryDirectory() as folder:
            store = DraftStore(Path(folder) / 'drafts.sqlite')
            try:
                report = run(case['request'], world, rules, store, provider)
                report['database_draft_count'] = store.count()
                (output / (name + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                actual = report['result']['task_status']
                passed = actual == case['expected_task_status']
                summary['cases'].append({'case': name, 'expected': case['expected_task_status'],
                                         'actual': actual, 'passed': passed, 'trace': name + '.json'})
                print(f'{name}: {actual}; expected={case["expected_task_status"]}; passed={passed}', flush=True)
                if name in ('normal', 'conflict'):
                    repeated = run(case['request'], world, rules, store, provider)
                    repeated['database_draft_count'] = store.count()
                    (output / (name + '_repeat.json')).write_text(json.dumps(repeated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    repeat_ok = repeated['result']['save_status'] == 'existing' and store.count() == 1
                    summary['cases'].append({'case': name + '_repeat', 'expected': 'existing / one draft',
                                             'actual': repeated['result']['save_status'], 'passed': repeat_ok,
                                             'trace': name + '_repeat.json'})
                    print(f'{name}_repeat: {repeated["result"]["save_status"]}; one_draft={store.count()==1}', flush=True)
                if name == 'conflict':
                    # Same immutable request and same database; change tool-side occupancy only.
                    world['schedule'] = [{'channel': 'video', 'slot': '2026-11-06T19:00:00+08:00'}]
                    moved = run(case['request'], world, rules, store, provider)
                    moved['database_draft_count'] = store.count()
                    (output / 'conflict_moved.json').write_text(json.dumps(moved, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    moved_ok = (moved['result']['effective_slot'] == case['request']['requested_slot']
                                and moved['result']['draft_id'] == report['result']['draft_id']
                                and moved['result']['save_status'] == 'updated' and store.count() == 1)
                    summary['cases'].append({'case': 'conflict_moved', 'expected': 'original slot / updated / one draft',
                                             'actual': moved['result']['save_status'], 'passed': moved_ok,
                                             'trace': 'conflict_moved.json'})
                    print(f'conflict_moved: {moved["result"]["save_status"]}; passed={moved_ok}', flush=True)
            finally:
                store.close()
        # Save incrementally so interrupted real-model evaluations retain completed evidence.
        summary['passed'] = sum(x['passed'] for x in summary['cases'])
        summary['total'] = len(summary['cases'])
        (output / 'evaluation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if all(x['passed'] for x in summary['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
