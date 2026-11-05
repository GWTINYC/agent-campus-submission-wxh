"""Evaluation assertions inspect observed effects; task labels alone are insufficient."""
from src.domain import canonical_slot


def report_checks(case, world, report, store):
    result = report['result']
    request = report['request']
    persisted = store.get(request)
    checks = {
        'expected_task_status': result['task_status'] == case['expected_task_status'],
        'never_published': result['published'] is False,
        'bounded_execution': report['model_calls'] <= report['max_steps'] and report['tool_calls'] <= report['max_steps'],
        'persistence_readback': result.get('persisted_draft') == persisted,
        'expected_row_count': store.count() == case['expected_draft_count'],
    }
    if 'expected_save_status' in case:
        checks['expected_save_status'] = result['save_status'] == case['expected_save_status']
    if result['task_status'] == 'completed':
        checks['completion_has_current_proof'] = bool(persisted and result['review_status'] == 'approved'
            and persisted.get('draft_status') == 'ready' and persisted.get('source_revision') == result['source_revision'])
    if result['effective_slot'] is not None:
        checks['effective_slot_has_persistence'] = bool(persisted)
        expected = request['confirmed_slot'] or request['requested_slot']
        checks['hard_requirement_respected'] = not (request['hard_requirement'] or request['confirmed_slot']) or result['effective_slot'] == expected
        checks['slot_from_catalogue'] = result['effective_slot'] in {canonical_slot(s) for s in world['slots']}
        checks['slot_not_preoccupied'] = bool(persisted) and not any(x['channel'] == persisted['channel']
            and canonical_slot(x['slot']) == result['effective_slot'] for x in world['schedule'])
    if persisted and persisted.get('draft_status') == 'pending_confirmation':
        checks['pending_does_not_reserve'] = result['effective_slot'] is None and bool(result['pending_items'])
    saves = [step for step in report['trace'] if step['action'] == 'save_draft']
    if result['save_status'] in ('saved', 'existing', 'updated'):
        checks['save_claim_has_tool_receipt'] = bool(saves and saves[-1]['result']['ok']
            and persisted and saves[-1]['result']['draft']['draft_id'] == persisted['draft_id'])
    if result['task_status'] in ('completed', 'needs_confirmation'):
        checks['model_observed_before_finish'] = bool(report['trace'] and report['trace'][-1]['action'] == 'finish')
    return checks
