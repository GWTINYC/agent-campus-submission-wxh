"""Bounded model -> tool -> observation loop. No business action is auto-selected."""
import copy
from datetime import datetime, timezone
from .domain import validate_action, validate_request
from .providers import ModelError
from .tools import BusinessTools


def run(request, world, rules, store, provider, max_steps=12):
    if not 1 <= max_steps <= 30:
        raise ValueError('max_steps must be between 1 and 30')
    request = validate_request(request)
    tools = BusinessTools(request, world, rules, store)
    history, trace = [], []
    stop = 'step_limit'
    model_error = None
    for step in range(1, max_steps + 1):
        context = {'request': copy.deepcopy(request), 'observations': copy.deepcopy(history),
                   'remaining_steps': max_steps - step + 1}
        try:
            chosen = validate_action(provider.choose(context))
        except (ModelError, ValueError, TypeError) as exc:
            stop = 'model_error'
            model_error = str(exc) if isinstance(exc, ModelError) else 'Invalid model action schema.'
            break
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
    return {'provider': provider.name, 'model': provider.model, 'evidence': provider.evidence,
            'started_from': 'immutable_user_request', 'request': request, 'max_steps': max_steps,
            'model_calls': len(trace) + int(model_error is not None),
            'tool_calls': sum(x['action'] != 'finish' for x in trace),
            'model_error': model_error, 'result': result, 'trace': trace}
