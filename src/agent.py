"""Bounded model -> tool -> observation loop. No business action is auto-selected."""
import copy
from pathlib import Path
from datetime import datetime, timezone
from .domain import revision, validate_action, validate_request
from .providers import ModelError
from .tools import BusinessTools

CODE_REVISION = revision({p.name: p.read_text(encoding='utf-8') for p in sorted(Path(__file__).parent.glob('*.py'))})


def run(request, world, rules, store, provider, max_steps=12):
    if type(max_steps) is not int or not 1 <= max_steps <= 30:
        raise ValueError('max_steps must be between 1 and 30')
    request = validate_request(request)
    tools = BusinessTools(request, world, rules, store)
    history, trace = [], []
    stop = 'step_limit'
    model_error = None
    preflight = store.register_request(request)
    if not preflight['ok']:
        tools.last_error = {'tool': 'request_guard', **preflight}
        stop = 'request_rejected'
    for step in range(1, max_steps + 1) if preflight['ok'] else []:
        context = {'request': copy.deepcopy(request), 'observations': copy.deepcopy(history),
                   'remaining_steps': max_steps - step + 1}
        try:
            raw = provider.choose(context)
        except ModelError as exc:
            stop = 'model_error'
            model_error = str(exc)
            break
        try:
            chosen = validate_action(raw)
        except (ValueError, TypeError) as exc:
            # Rejection is an observation, never a tool invocation. The model may repair
            # its next proposal within the SAME budget; no forbidden action is executed.
            rejected = {'ok': False, 'error': 'invalid_action', 'retryable': True,
                        'validation_error': str(exc)[:300]}
            if trace:
                trace[-1]['next_action'] = 'invalid_action'
            entry = {'step': step, 'at': datetime.now(timezone.utc).isoformat(), 'action': 'invalid_action',
                     'arguments': {}, 'reason': 'Action rejected by schema; no business tool executed.',
                     'result': rejected, 'next_action': None}
            if getattr(provider, 'last_metadata', None):
                entry['model_response'] = copy.deepcopy(provider.last_metadata)
            trace.append(entry)
            history.append({'tool': 'action_validation', 'arguments': {}, 'result': rejected})
            continue
        entry = {'step': step, 'at': datetime.now(timezone.utc).isoformat(),
                 'action': chosen['action'], 'arguments': chosen['arguments'], 'reason': chosen['reason']}
        if getattr(provider, 'last_metadata', None):
            entry['model_response'] = copy.deepcopy(provider.last_metadata)
        if trace:
            trace[-1]['next_action'] = chosen['action']
        if chosen['action'] == 'finish':
            entry.update(result={'ok': True, 'message': 'Final status rendered from authoritative tool state.'},
                         next_action=None)
            trace.append(entry)
            stop = 'model_finished'
            break
        result = tools.execute(chosen['action'], chosen['arguments'])
        entry['result'] = copy.deepcopy(result)
        entry['next_action'] = None
        trace.append(entry)
        history.append({'tool': chosen['action'], 'arguments': chosen['arguments'], 'result': copy.deepcopy(result)})
    result = tools.result(stop)
    return {'provider': provider.name, 'model': provider.model, 'evidence': provider.evidence, 'code_revision': CODE_REVISION,
            'started_from': 'immutable_user_request', 'request': request, 'max_steps': max_steps,
            'model_calls': len(trace) + int(model_error is not None),
            'tool_calls': sum(x['action'] in ('read_material', 'check_rules', 'query_slots', 'save_draft') for x in trace),
            'preflight': preflight, 'model_error': model_error, 'result': result, 'trace': trace}
