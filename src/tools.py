"""Business authority: decisions are proposals; current evidence authorizes effects."""
import copy
import sqlite3
from .domain import canonical_slot, revision


class BusinessTools:
    def __init__(self, request, world, rules, store):
        self.request = copy.deepcopy(request)
        self.world = copy.deepcopy(world)
        self.rules = copy.deepcopy(rules)
        self.store = store
        self.material = None
        self.sources = {}
        self.field_conflicts = []
        self.observed_revision = None
        self.review = None
        self.slots = None
        self.save_result = None
        self.proposed_slot = None
        self.last_error = None
        self.calls = {}

    def source_revision(self):
        mid = self.request['material_id']
        return revision({'primary': self.world.get('materials', {}).get(mid),
                         'archive': self.world.get('archive', {}).get(mid), 'rules': self.rules})

    def refresh_validity(self):
        current = self.source_revision()
        self.store.invalidate(self.request['material_id'], current, 'material_or_rules_changed')
        stale = self.observed_revision is not None and self.observed_revision != current
        if stale:
            self.material, self.review, self.slots = None, None, None
            self.sources, self.field_conflicts = {}, []
            self.observed_revision = None
        return stale

    def execute(self, name, args):
        if name not in ('read_material', 'check_rules', 'query_slots', 'save_draft'):
            return {'ok': False, 'error': 'unknown_tool', 'retryable': False}
        self.calls[name] = self.calls.get(name, 0) + 1
        faults = self.world.get('faults', {})
        try:
            if name != 'save_draft' and faults.get(name) in ('always', self.calls[name]):
                result = {'ok': False, 'error': 'tool_unavailable', 'retryable': faults[name] != 'always'}
            else:
                result = getattr(self, name)(**args)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
            result = {'ok': False, 'error': 'tool_failure', 'retryable': False,
                      'error_category': type(exc).__name__}
        self.last_error = None if result['ok'] else {'tool': name, **result}
        if name == 'save_draft':
            self.save_result = copy.deepcopy(result)
        return result

    def issues(self):
        if not isinstance(self.material, dict):
            return ['primary_material']
        invalid = []
        for key in self.rules['required_fields']:
            value = self.material.get(key)
            if key == 'risk_tags':
                if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                    invalid.append(key)
            elif not isinstance(value, str) or not value.strip():
                invalid.append(key)
        # A malformed risk collection must never be interpreted as an empty collection.
        tags = self.material.get('risk_tags')
        if not isinstance(tags, list) or any(not isinstance(v, str) or not v.strip() for v in tags):
            invalid.append('risk_tags')
        return sorted(set(invalid))

    def read_material(self, source=None, slot=None):
        self.refresh_validity()
        source = source or 'primary'
        if source == 'archive' and 'primary' not in self.sources:
            return {'ok': False, 'error': 'primary_read_required', 'retryable': True, 'required_source': 'primary'}
        data = self.world.get('materials' if source == 'primary' else 'archive', {}).get(self.request['material_id'])
        if data is None:
            return {'ok': False, 'error': 'material_not_found', 'retryable': False, 'source': source}
        if not isinstance(data, dict):
            return {'ok': False, 'error': 'invalid_material_metadata', 'retryable': False, 'source': source}
        previous = revision({'sources': self.sources, 'rules': self.rules})
        self.sources[source] = copy.deepcopy(data)
        self.material = copy.deepcopy(self.sources['primary'])
        self.field_conflicts = []
        for key, value in self.sources.get('archive', {}).items():
            original = self.material.get(key)
            if original is None or original == '':
                self.material[key] = copy.deepcopy(value)
            elif original != value:
                self.field_conflicts.append(key)
                # Risk signals accumulate. Archive metadata cannot launder primary risk.
                if key == 'risk_tags' and isinstance(original, list) and isinstance(value, list):
                    if all(isinstance(v, str) for v in original + value):
                        self.material[key] = sorted(set(original + value))
        self.observed_revision = self.source_revision()
        if previous != revision({'sources': self.sources, 'rules': self.rules}):
            self.review, self.slots = None, None
        if self.field_conflicts:
            self.store.invalidate(self.request['material_id'], reason='conflicting_metadata')
        return {'ok': True, 'source': source, 'material': copy.deepcopy(self.material),
                'missing_fields': self.issues(), 'field_conflicts': self.field_conflicts,
                'source_revision': self.observed_revision}

    def check_rules(self, source=None, slot=None):
        if self.refresh_validity():
            return {'ok': False, 'error': 'stale_material', 'retryable': True, 'required_source': 'primary'}
        if self.material is None:
            return {'ok': False, 'error': 'read_material_first', 'retryable': True}
        missing = self.issues()
        raw_tags = self.material.get('risk_tags', [])
        tags = {t for t in raw_tags if isinstance(t, str)} if isinstance(raw_tags, list) else set()
        blocked = sorted(tags.intersection(self.rules['blocked_tags']))
        manual = sorted(tags.intersection(self.rules['manual_review_tags']))
        known = set(self.rules['blocked_tags']) | set(self.rules['manual_review_tags'])
        manual += ['unknown_risk_tag:' + tag for tag in sorted(tags - known)]
        manual += ['conflicting_metadata:' + key for key in self.field_conflicts]
        rights = self.material.get('rights_status')
        if rights == 'unlicensed':
            blocked.append('unlicensed_rights')
        elif isinstance(rights, str) and rights not in ('licensed', ''):
            manual.append('rights_need_review')
        status = 'blocked' if blocked else 'needs_information' if missing else 'needs_review' if manual else 'approved'
        self.review = {'status': status, 'risk_reasons': blocked + manual, 'missing_fields': missing,
                       'source_revision': self.observed_revision}
        if status != 'approved':
            self.store.invalidate(self.request['material_id'], reason='review_' + status)
        return {'ok': True, **copy.deepcopy(self.review)}

    def query_slots(self, source=None, slot=None):
        if self.refresh_validity():
            return {'ok': False, 'error': 'stale_material', 'retryable': True, 'required_source': 'primary'}
        if not self.material or not isinstance(self.material.get('channel'), str) or not self.material['channel'].strip():
            return {'ok': False, 'error': 'channel_unknown', 'retryable': True}
        channel = self.material['channel']
        occupied = {canonical_slot(x['slot']) for x in self.world.get('schedule', []) if x['channel'] == channel}
        occupied |= self.store.occupied(channel, self.request)
        candidates = sorted({canonical_slot(x) for x in self.world['slots']})
        self.slots = {'available': [x for x in candidates if x not in occupied],
                      'occupied': sorted(occupied), 'channel': channel}
        return {'ok': True, **copy.deepcopy(self.slots)}

    def save_draft(self, source=None, slot=None):
        faults = self.world.get('faults', {})
        if faults.get('material_changed_on_save') and self.calls['save_draft'] == 1:
            self.world['materials'][self.request['material_id']]['rights_status'] = 'unlicensed'
        if self.refresh_validity():
            return {'ok': False, 'error': 'stale_review', 'retryable': True, 'required_source': 'primary'}
        if not self.review or self.review['status'] != 'approved':
            return {'ok': False, 'error': 'review_not_approved', 'retryable': False}
        if not self.slots:
            return {'ok': False, 'error': 'query_slots_first', 'retryable': True}
        if slot not in self.slots['available']:
            return {'ok': False, 'error': 'slot_not_available', 'retryable': True}
        self.proposed_slot = slot
        if faults.get('race_on_save') and self.calls['save_draft'] == 1:
            self.world.setdefault('schedule', []).append({'channel': self.material['channel'], 'slot': slot})
        latest = self.query_slots()
        if slot not in latest.get('available', []):
            return {'ok': False, 'error': 'slot_conflict', 'retryable': True}
        req = self.request
        target = req['confirmed_slot'] or req['requested_slot']
        if (req['hard_requirement'] or req['confirmed_slot']) and target in latest['available'] and slot != target:
            return {'ok': False, 'error': 'required_slot_available', 'required_slot': target, 'retryable': True}
        pending = bool((req['hard_requirement'] or req['confirmed_slot']) and slot != target)
        draft = {'material_id': req['material_id'], 'channel': self.material['channel'],
                 'requested_slot': req['requested_slot'], 'suggested_slot': slot,
                 'effective_slot': None if pending else slot,
                 'confirmation_status': 'pending' if pending else 'confirmed',
                 'draft_status': 'pending_confirmation' if pending else 'ready',
                 'source_revision': self.review['source_revision'],
                 'kind': 'draft', 'review_status': 'approved'}
        return self.store.save(req, draft, fail=bool(faults.get('save_draft')))

    def result(self, stop_reason):
        try:
            # An identity-rejected request must not mutate another request's drafts.
            if stop_reason != 'request_rejected':
                self.refresh_validity()
            persisted = self.store.get(self.request) if stop_reason != 'request_rejected' else None
            if persisted and persisted.get('draft_status') in ('ready', 'pending_confirmation'):
                occupied = {canonical_slot(x['slot']) for x in self.world.get('schedule', [])
                            if x['channel'] == persisted['channel']}
                occupied |= self.store.occupied(persisted['channel'], self.request)
                if persisted['suggested_slot'] in occupied:
                    self.store.invalidate(self.request['material_id'], reason='slot_changed', request=self.request)
                    persisted = self.store.get(self.request)
                    self.last_error = {'tool': 'final_guard', 'ok': False, 'error': 'stale_slot', 'retryable': True}
        except (sqlite3.Error, OSError):
            persisted = None
            self.last_error = {'tool': 'storage', 'ok': False, 'error': 'storage_error', 'retryable': False}
        missing = self.issues() if self.material is not None else []
        if self.last_error and self.last_error.get('error') in ('material_not_found', 'invalid_material_metadata', 'primary_read_required') and not self.material:
            missing = ['primary_material']
        review = self.review or {'status': 'not_checked', 'risk_reasons': [], 'missing_fields': missing}
        pending = list(review['missing_fields'])
        if review['status'] == 'needs_review' or self.field_conflicts:
            pending.append('人工复核风险或资料冲突')
        active = persisted is not None and persisted.get('draft_status') in ('ready', 'pending_confirmation')
        if active and persisted['confirmation_status'] == 'pending':
            pending.append('确认替代时段 ' + persisted['suggested_slot'])
        if review['status'] == 'blocked':
            status = 'blocked'
        elif review['missing_fields']:
            status = 'needs_information'
        elif review['status'] == 'needs_review' or self.field_conflicts:
            status = 'needs_confirmation'
        elif (self.save_result and not self.save_result['ok']) or self.last_error:
            status = 'failed'
        elif active and review['status'] == 'approved':
            status = 'needs_confirmation' if pending else 'completed'
        else:
            status = 'incomplete'
        if stop_reason in ('step_limit', 'model_error') or (self.last_error and self.last_error.get('error') == 'stale_slot'):
            status = 'incomplete'
        if self.slots and not self.slots['available']:
            pending.append('无可用时段，请提供其他时段或日期')
        error = self.last_error
        if self.save_result and not self.save_result['ok']:
            error = {'tool': 'save_draft', **self.save_result}
        save_status = ('failed' if not self.save_result['ok'] else self.save_result['save_status']) if self.save_result else 'not_attempted'
        reasons = {'completed': '当前有效审核与草稿已确认；未执行发布。',
                   'needs_confirmation': '存在待确认事项，尚未完成排期确认。',
                   'needs_information': '资料缺失或格式无效，不能确认排期。',
                   'blocked': '当前审核存在阻断风险，旧草稿已失效。',
                   'failed': '最新工具或保存尝试失败；已有草稿另见 persisted_draft。',
                   'incomplete': '未完成：' + stop_reason}
        return {'request_id': self.request['request_id'], 'material_id': self.request['material_id'],
                'task_status': status, 'review_status': review['status'], 'status_reason': reasons[status],
                'risk_reasons': review['risk_reasons'], 'missing_fields': review['missing_fields'],
                'requested_slot': self.request['requested_slot'],
                'suggested_slot': persisted['suggested_slot'] if active else self.proposed_slot,
                'effective_slot': persisted['effective_slot'] if active else None,
                'pending_items': pending, 'save_status': save_status,
                'draft_id': persisted['draft_id'] if persisted else None,
                'draft_status': persisted.get('draft_status') if persisted else None,
                'persisted_draft': persisted, 'error': error, 'stop_reason': stop_reason,
                'source_revision': self.source_revision(), 'published': False}
