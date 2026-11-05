"""Authoritative business tools; no tool exists for publishing or approving a request."""
import copy
from .domain import canonical_slot


class BusinessTools:
    def __init__(self, request, world, rules, store):
        self.request = copy.deepcopy(request)
        self.world = copy.deepcopy(world)
        self.rules = rules
        self.store = store
        self.material = None
        self.review = None
        self.slots = None
        self.save_result = None
        self.successful_save = None
        self.proposed_slot = None
        self.last_error = None
        self.calls = {}

    def execute(self, name, args):
        self.calls[name] = self.calls.get(name, 0) + 1
        faults = self.world.get('faults', {})
        if name != 'save_draft' and faults.get(name) in ('always', self.calls[name]):
            result = {'ok': False, 'error': 'tool_unavailable', 'retryable': faults[name] != 'always'}
        else:
            result = getattr(self, name)(**args)
        self.last_error = None if result['ok'] else {'tool': name, **result}
        if name == 'save_draft':
            self.save_result = result
            if result['ok']:
                self.successful_save = copy.deepcopy(result)
        return result

    def read_material(self, source=None, slot=None):
        source = source or 'primary'
        material = self.world.get('materials' if source == 'primary' else 'archive', {}).get(self.request['material_id'])
        if material is None:
            return {'ok': False, 'error': 'material_not_found', 'retryable': False, 'source': source}
        self.material = {**(self.material or {}), **copy.deepcopy(material)}
        self.review = None
        self.slots = None
        return {'ok': True, 'source': source, 'material': self.material,
                'missing_fields': [k for k in self.rules['required_fields'] if not self.material.get(k)]}

    def check_rules(self, source=None, slot=None):
        if self.material is None:
            return {'ok': False, 'error': 'read_material_first', 'retryable': True}
        missing = [k for k in self.rules['required_fields'] if not self.material.get(k)]
        tags = set(self.material.get('risk_tags', []))
        blocked = sorted(tags.intersection(self.rules['blocked_tags']))
        manual = sorted(tags.intersection(self.rules['manual_review_tags']))
        if self.material.get('rights_status') == 'unlicensed':
            blocked.append('unlicensed_rights')
        elif self.material.get('rights_status') not in ('licensed', None, ''):
            manual.append('rights_need_review')
        status = 'blocked' if blocked else 'needs_information' if missing else 'needs_review' if manual else 'approved'
        self.review = {'status': status, 'risk_reasons': blocked + manual, 'missing_fields': missing}
        return {'ok': True, **self.review}

    def query_slots(self, source=None, slot=None):
        if not self.material or not self.material.get('channel'):
            return {'ok': False, 'error': 'channel_unknown', 'retryable': True}
        channel = self.material['channel']
        occupied = {canonical_slot(x['slot']) for x in self.world.get('schedule', []) if x['channel'] == channel}
        occupied |= self.store.occupied(channel, self.request)
        candidates = sorted({canonical_slot(x) for x in self.world['slots']})
        self.slots = {'available': [x for x in candidates if x not in occupied],
                      'occupied': sorted(occupied), 'channel': channel}
        return {'ok': True, **self.slots}

    def save_draft(self, source=None, slot=None):
        if not self.review or self.review['status'] != 'approved':
            return {'ok': False, 'error': 'review_not_approved', 'retryable': False}
        if not self.slots:
            return {'ok': False, 'error': 'query_slots_first', 'retryable': True}
        if slot not in self.slots['available']:
            return {'ok': False, 'error': 'slot_not_available', 'retryable': True}
        self.proposed_slot = slot
        # Simulates a competing writer after the model's query; rechecked at save.
        if self.world.get('faults', {}).get('race_on_save') and self.calls['save_draft'] == 1:
            self.world.setdefault('schedule', []).append({'channel': self.material['channel'], 'slot': slot})
        latest = self.query_slots()
        if slot not in latest['available']:
            return {'ok': False, 'error': 'slot_conflict', 'retryable': True}
        req = self.request
        pending = bool(req['hard_requirement'] and slot != (req['confirmed_slot'] or req['requested_slot']))
        # Once a user has explicitly confirmed a slot, alternatives still require confirmation.
        if req['confirmed_slot'] and slot != req['confirmed_slot']:
            pending = True
        draft = {'material_id': req['material_id'], 'channel': self.material['channel'],
                 'requested_slot': req['requested_slot'], 'suggested_slot': slot,
                 'effective_slot': None if pending else slot,
                 'confirmation_status': 'pending' if pending else 'confirmed',
                 'kind': 'draft', 'review_status': 'approved'}
        return self.store.save(req, draft, fail=bool(self.world.get('faults', {}).get('save_draft')))

    def result(self, stop_reason):
        observed_missing = ([k for k in self.rules['required_fields'] if not self.material.get(k)]
                            if self.material else [])
        review = self.review or {'status': 'not_checked', 'risk_reasons': [], 'missing_fields': observed_missing}
        saved = self.successful_save
        draft = saved['draft'] if saved else None
        pending = list(review['missing_fields'])
        if review['status'] == 'needs_review':
            pending.append('人工复核素材风险')
        if draft and draft['confirmation_status'] == 'pending':
            pending.append('确认替代时段 ' + draft['suggested_slot'])
        if draft:
            status = 'needs_confirmation' if pending else 'completed'
        elif review['status'] == 'blocked':
            status = 'blocked'
        elif review['missing_fields']:
            status = 'needs_information'
        elif review['status'] == 'needs_review':
            status = 'needs_confirmation'
        elif self.save_result or self.last_error:
            status = 'failed'
        else:
            status = 'incomplete'
        if stop_reason != 'model_finished':
            status = 'incomplete'
        if not draft and self.slots and not self.slots['available']:
            pending.append('无可用时段，请提供其他时段或日期')
        reasons = {'completed': '草稿已保存；未执行发布。',
                   'needs_confirmation': '存在待确认事项；尚未完成排期确认。',
                   'needs_information': '素材信息不足，未保存排期。',
                   'blocked': '素材存在阻断风险，未新增排期。',
                   'failed': '工具或保存失败，请查看 error 和 Trace。',
                   'incomplete': '未完成：' + stop_reason}
        if stop_reason == 'model_finished' and status == 'incomplete':
            reasons['incomplete'] = '模型结束时尚无可保存的完整排期；请查看待确认项和 Trace。'
        error = self.last_error
        if not error and self.save_result and not self.save_result['ok']:
            error = {'tool': 'save_draft', **self.save_result}
        return {'request_id': self.request['request_id'], 'material_id': self.request['material_id'],
                'task_status': status, 'review_status': review['status'],
                'status_reason': reasons[status],
                'risk_reasons': review['risk_reasons'], 'missing_fields': review['missing_fields'],
                'requested_slot': self.request['requested_slot'],
                'suggested_slot': draft['suggested_slot'] if draft else self.proposed_slot,
                'effective_slot': draft['effective_slot'] if draft else None,
                'pending_items': pending, 'save_status': saved['save_status'] if saved else 'failed' if self.save_result else 'not_attempted',
                'draft_id': draft['draft_id'] if draft else None,
                'error': error, 'stop_reason': stop_reason, 'published': False}
