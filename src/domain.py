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
        'reason': {'type': 'string', 'maxLength': 300, 'description': 'One short observable decision reason, no chain of thought.'},
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
    try:
        return dt.astimezone(timezone(timedelta(hours=8))).isoformat()
    except OverflowError:
        raise ValueError('slot is outside the supported datetime range') from None


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
    if value.get('confirmed_slot') is not None and (
            not isinstance(value['confirmed_slot'], str) or not value['confirmed_slot'].strip()):
        raise ValueError('confirmed_slot must be null or a nonempty ISO 8601 string')
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
    if len(value['reason']) > 300:
        raise ValueError('decision reason must be at most 300 characters')
    if value['action'] == 'read_material' and args['slot'] is not None:
        raise ValueError('read_material does not accept slot')
    if value['action'] == 'save_draft' and (args['source'] is not None or args['slot'] is None):
        raise ValueError('save_draft requires slot and does not accept source')
    if value['action'] in ('check_rules', 'query_slots', 'finish') and any(v is not None for v in args.values()):
        raise ValueError('this action does not accept business arguments')
    return value


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def action(name, reason, source=None, slot=None):
    return {'action': name, 'arguments': {'source': source, 'slot': slot}, 'reason': reason}
