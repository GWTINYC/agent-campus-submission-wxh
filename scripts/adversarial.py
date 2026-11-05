"""Deterministic hostile actions and data changes, not real-model quality evidence."""
import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from src.agent import CODE_REVISION, run
from src.cli import ROOT, load_case
from src.domain import action, validate_request
from src.providers import MockProvider
from src.storage import DraftStore
from src.tools import BusinessTools

S18 = '2026-11-06T18:00:00+08:00'
S19 = '2026-11-06T19:00:00+08:00'


def prepared(store, world=None, rules=None):
    case, base, policy = load_case(ROOT / 'examples/inputs/normal.json')
    tools = BusinessTools(validate_request(case['request']), world or base, rules or policy, store)
    tools.execute('read_material', {})
    tools.execute('check_rules', {})
    tools.execute('query_slots', {})
    return tools


def probe(name):
    case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
    store = DraftStore(':memory:')
    try:
        if name == 'archive_risk_laundering':
            world['materials']['M01']['rights_status'] = 'unlicensed'
            world['archive']['M01'] = {'rights_status': 'licensed', 'risk_tags': []}
            tools = prepared(store, world)
            tools.execute('read_material', {'source': 'archive'})
            review = tools.execute('check_rules', {})
            tools.execute('query_slots', {})
            outcome = tools.execute('save_draft', {'slot': S18})
            safe = not outcome['ok'] and review.get('status') != 'approved' and store.count() == 0
        elif name == 'unknown_risk_tag':
            world['materials']['M01']['risk_tags'] = ['new_unclassified_risk']
            tools = prepared(store, world)
            outcome = tools.execute('save_draft', {'slot': S18})
            safe = not outcome['ok'] and store.count() == 0
        elif name == 'material_changed_after_review':
            tools = prepared(store)
            tools.world['materials']['M01']['rights_status'] = 'unlicensed'
            outcome = tools.execute('save_draft', {'slot': S18})
            safe = not outcome['ok'] and store.count() == 0
        elif name == 'latest_failed_save_hidden_by_old_success':
            tools = prepared(store)
            first = tools.execute('save_draft', {'slot': S18})
            assert first['ok'] and store.count() == 1, 'Attack requires a successful initial save'
            tools.world['faults']['save_draft'] = 'always'
            tools.execute('save_draft', {'slot': S19})
            outcome = tools.result('model_finished')
            safe = outcome['task_status'] == 'failed' and outcome['save_status'] == 'failed' and store.count() == 1
        elif name == 'old_draft_survives_new_blocking_review':
            first = run(case['request'], world, rules, store, MockProvider())
            assert first['result']['task_status'] == 'completed' and store.count() == 1, 'Initial save required'
            world['materials']['M01']['rights_status'] = 'unlicensed'
            outcome = run(case['request'], world, rules, store, MockProvider())['result']
            reserved = store.db.execute('SELECT COUNT(*) FROM drafts WHERE effective_slot IS NOT NULL').fetchone()[0]
            safe = outcome['task_status'] == 'blocked' and reserved == 0
            outcome['reserved_rows_after_block'] = reserved
        elif name == 'failed_request_id_can_be_rebound':
            world['faults']['save_draft'] = 'always'
            run(case['request'], world, rules, store, MockProvider())
            world['faults'] = {}
            request = {**case['request'], 'material_id': 'M03'}
            outcome = run(request, world, rules, store, MockProvider())['result']
            safe = outcome['task_status'] == 'failed' and outcome.get('error', {}).get('error') == 'idempotency_conflict'
        elif name == 'malformed_risk_tags_crash':
            world['materials']['M01']['risk_tags'] = None
            outcome = run(case['request'], world, rules, store, MockProvider())['result']
            safe = outcome['task_status'] in ('needs_information', 'blocked', 'needs_confirmation') and store.count() == 0
        elif name == 'false_confirmation_silently_accepted':
            try:
                outcome = validate_request({**case['request'], 'confirmed_slot': False})
                safe = False
            except ValueError as exc:
                outcome, safe = {'validation_error': str(exc)}, True
        elif name == 'fractional_hard_time_silently_truncated':
            request = {**case['request'], 'requested_slot': '2026-11-06T18:00:00.500000+08:00'}
            outcome = run(request, world, rules, store, MockProvider())['result']
            safe = outcome['task_status'] != 'completed' and outcome['effective_slot'] is None
        else:
            raise ValueError(name)
        return {'attack': name, 'safe': safe, 'observed': outcome, 'draft_count': store.count()}
    except Exception as exc:
        return {'attack': name, 'safe': False, 'exception_type': type(exc).__name__}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='examples/adversarial/after.json')
    args = parser.parse_args()
    names = ['archive_risk_laundering', 'unknown_risk_tag', 'material_changed_after_review',
             'latest_failed_save_hidden_by_old_success', 'old_draft_survives_new_blocking_review',
             'failed_request_id_can_be_rebound', 'malformed_risk_tags_crash',
             'false_confirmation_silently_accepted', 'fractional_hard_time_silently_truncated']
    cases = [probe(name) for name in names]
    report = {'ran_at': datetime.now(timezone.utc).isoformat(), 'code_revision': CODE_REVISION,
              'evidence': 'hostile_test_double_not_live_model',
              'safe': sum(c['safe'] for c in cases), 'total': len(cases), 'cases': cases}
    target = ROOT / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'safe': report['safe'], 'total': report['total'],
                      'cases': [{k: c[k] for k in ('attack', 'safe')} for c in cases]}, indent=2))
    return 0 if all(c['safe'] for c in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
