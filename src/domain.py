"""Validated user inputs and structured model actions."""
import hashlib
import json
from datetime import datetime

ACTIONS = ('read_material', 'check_rules', 'query_slots', 'save_draft', 'finish')
ACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'action': {'type': 'string', 'enum': list(ACTIONS)},
        'arguments': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'source': {'type': ['string', 'null'], 'enum': ['primary', 'archive', None]},
                'slot': {'type': ['string', 'null']},
            }, 'required': ['source', 'slot'],
        },
        'reason': {'type': 'string', 'description': 'One short observable decision reason, no chain of thought.'},
    }, 'required': ['action', 'arguments', 'reason'],
}


def canonical_slot(value):
    if not isinstance(value, str):
        raise ValueError('slot must be an ISO 8601 string with UTC offset')
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise ValueError('slot must include UTC offset, e.g. +08:00')
    # Normalize equivalent instants so changing the offset cannot bypass occupancy.
    from datetime import timezone, timedelta
    return dt.astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError('request must be a JSON object')
    allowed = {'request_id', 'material_id', 'requested_slot', 'hard_requirement', 'confirmed_slot'}
    if set(value) - allowed:
        raise ValueError('unknown request fields: ' + ', '.join(sorted(set(value) - allowed)))
    for key in ('request_id', 'material_id'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'{key} is required and must be a nonempty string')
    if type(value.get('hard_requirement', True)) is not bool:
        raise ValueError('hard_requirement must be a boolean')
    return {
        'request_id': value['request_id'], 'material_id': value['material_id'],
        'requested_slot': canonical_slot(value.get('requested_slot')),
        'hard_requirement': value.get('hard_requirement', True),
        'confirmed_slot': canonical_slot(value['confirmed_slot']) if value.get('confirmed_slot') else None,
    }


def fingerprint(request):
    demand = {k: v for k, v in request.items() if k != 'request_id'}
    return hashlib.sha256(json.dumps(demand, sort_keys=True).encode()).hexdigest()


def validate_action(value):
    if not isinstance(value, dict) or set(value) != {'action', 'arguments', 'reason'}:
        raise ValueError('action must contain exactly action, arguments, reason')
    if value['action'] not in ACTIONS or not isinstance(value['reason'], str) or not value['reason'].strip():
        raise ValueError('invalid action or missing decision reason')
    args = value['arguments']
    if not isinstance(args, dict) or set(args) != {'source', 'slot'}:
        raise ValueError('arguments must contain source and slot (nullable)')
    if args['source'] not in ('primary', 'archive', None):
        raise ValueError('unknown material source')
    if args['slot'] is not None:
        args['slot'] = canonical_slot(args['slot'])
    return value


def action(name, reason, source=None, slot=None):
    return {'action': name, 'arguments': {'source': source, 'slot': slot}, 'reason': reason}
