"""Hostile model actions, malformed data, stale proofs, and actual local redirects."""
import copy
import io
import itertools
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from scripts.adversarial import prepared, probe
from src.agent import run
from src.cli import ROOT, load_case
from src.domain import action, canonical_slot, validate_action, validate_request
from src.providers import DeepSeekProvider, MockProvider, ModelError
from src.storage import DraftStore
from tests.test_agent import ScriptedProvider, S18, S19


class AdversarialTests(unittest.TestCase):
    def test_nine_reproduced_attacks_are_now_safe(self):
        names = ['archive_risk_laundering', 'unknown_risk_tag', 'material_changed_after_review',
                 'latest_failed_save_hidden_by_old_success', 'old_draft_survives_new_blocking_review',
                 'failed_request_id_can_be_rebound', 'malformed_risk_tags_crash',
                 'false_confirmation_silently_accepted', 'fractional_hard_time_silently_truncated']
        for name in names:
            with self.subTest(attack=name):
                result = probe(name)
                self.assertTrue(result['safe'], result)

    def test_rule_change_invalidates_review_before_save(self):
        store = DraftStore(':memory:')
        try:
            tools = prepared(store)
            tools.rules['required_fields'].append('release_approval')
            result = tools.execute('save_draft', {'slot': S18})
            self.assertEqual(result['error'], 'stale_review')
            self.assertEqual(store.count(), 0)
        finally:
            store.close()

    def test_source_change_after_success_revokes_slot_and_keeps_history(self):
        store = DraftStore(':memory:')
        try:
            tools = prepared(store)
            self.assertTrue(tools.execute('save_draft', {'slot': S18})['ok'])
            tools.world['materials']['M01']['rights_status'] = 'unlicensed'
            result = tools.result('model_finished')
            self.assertNotEqual(result['task_status'], 'completed')
            self.assertEqual(result['draft_status'], 'invalidated')
            self.assertIsNone(result['effective_slot'])
            self.assertEqual(result['persisted_draft']['previous_effective_slot'], S18)
            self.assertEqual(store.count(), 1)
        finally:
            store.close()

    def test_occupancy_change_after_success_is_not_reported_as_valid(self):
        store = DraftStore(':memory:')
        try:
            tools = prepared(store)
            tools.execute('save_draft', {'slot': S18})
            tools.world['schedule'].append({'channel': 'video', 'slot': S18})
            result = tools.result('model_finished')
            self.assertEqual(result['task_status'], 'incomplete')
            self.assertEqual(result['save_status'], 'saved')
            self.assertEqual(result['draft_status'], 'invalidated')
            self.assertIsNone(result['effective_slot'])
        finally:
            store.close()

    def test_archive_cannot_be_used_without_primary_identity(self):
        store = DraftStore(':memory:')
        try:
            case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
            from src.tools import BusinessTools
            tools = BusinessTools(validate_request(case['request']), world, rules, store)
            self.assertEqual(tools.execute('read_material', {'source': 'archive'})['error'], 'primary_read_required')
        finally:
            store.close()

    def test_hard_original_slot_cannot_be_ignored_while_available(self):
        store = DraftStore(':memory:')
        try:
            tools = prepared(store)
            result = tools.execute('save_draft', {'slot': S19})
            self.assertEqual(result['error'], 'required_slot_available')
            self.assertEqual(store.count(), 0)
        finally:
            store.close()

    def test_rejected_request_identity_does_not_call_model(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
        store = DraftStore(':memory:')
        try:
            run(case['request'], world, rules, store, MockProvider())
            result = run({**case['request'], 'requested_slot': S19}, world, rules, store, ScriptedProvider([]))
            self.assertEqual(result['model_calls'], 0)
            self.assertEqual(result['result']['stop_reason'], 'request_rejected')
            self.assertEqual(result['result']['error']['error'], 'idempotency_conflict')
        finally:
            store.close()

    def test_identity_rejection_has_no_draft_mutation_side_effect(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
        store = DraftStore(':memory:')
        try:
            run(case['request'], world, rules, store, MockProvider())
            other = {**case['request'], 'request_id': 'other', 'material_id': 'M03', 'requested_slot': S19}
            good = run(other, world, rules, store, MockProvider())
            self.assertEqual(good['result']['task_status'], 'completed')
            before = store.get(validate_request(other))
            world['materials']['M03']['rights_status'] = 'unlicensed'
            rejected = run({**other, 'request_id': case['request']['request_id']}, world, rules, store, ScriptedProvider([]))
            self.assertEqual(rejected['result']['stop_reason'], 'request_rejected')
            self.assertEqual(store.get(validate_request(other)), before)
        finally:
            store.close()

    def test_equivalent_timezone_duplicate_is_one_draft(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
        store = DraftStore(':memory:')
        try:
            first = run(case['request'], world, rules, store, MockProvider())
            second = run({**case['request'], 'requested_slot': '2026-11-06T10:00:00Z'}, world, rules, store, MockProvider())
            self.assertEqual(second['result']['save_status'], 'existing')
            self.assertEqual(first['result']['draft_id'], second['result']['draft_id'])
            self.assertEqual(store.count(), 1)
        finally:
            store.close()

    def test_action_specific_arguments_and_publish_are_rejected(self):
        attacks = [action('finish', 'fake confirm', slot=S19), action('query_slots', 'fake source', source='archive'),
                   action('save_draft', 'fake source', source='archive', slot=S18), action('publish', 'publish'),
                   action('save_draft', 'no slot'), action('read_material', 'ignored slot', slot=S19)]
        for chosen in attacks:
            with self.subTest(action=chosen):
                with self.assertRaises(ValueError):
                    validate_action(chosen)

    def test_invalid_action_feedback_can_be_repaired_without_executing_it(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
        store = DraftStore(':memory:')
        try:
            invalid = action('read_material', 'irrelevant parameter', slot=S19)
            choices = [invalid, action('read_material', 'corrected'), action('check_rules', 'review'),
                       action('query_slots', 'query'), action('save_draft', 'save', slot=S18), action('finish', 'done')]
            report = run(case['request'], world, rules, store, ScriptedProvider(choices))
            self.assertEqual(report['trace'][0]['action'], 'invalid_action')
            self.assertEqual(report['trace'][0]['next_action'], 'read_material')
            self.assertEqual(report['tool_calls'], 4)
            self.assertEqual(report['model_calls'], 6)
            self.assertEqual(report['result']['task_status'], 'completed')
        finally:
            store.close()

    def test_repeated_forbidden_actions_use_budget_without_side_effects(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/normal.json')
        store = DraftStore(':memory:')
        try:
            report = run(case['request'], world, rules, store,
                         ScriptedProvider([action('publish', 'forbidden')] * 3), max_steps=3)
            self.assertEqual(report['result']['stop_reason'], 'step_limit')
            self.assertEqual(report['model_calls'], 3)
            self.assertEqual(report['tool_calls'], 0)
            self.assertEqual(store.count(), 0)
        finally:
            store.close()

    def test_malformed_confirmation_values_and_fractional_time(self):
        case, _, _ = load_case(ROOT / 'examples/inputs/normal.json')
        for value in (False, True, [], {}, 0, '', '   '):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_request({**case['request'], 'confirmed_slot': value})
        self.assertEqual(canonical_slot('2026-11-06T10:00:00.5Z'), '2026-11-06T18:00:00.500000+08:00')

    def test_evaluation_rejects_forged_success_and_persistence(self):
        from scripts.oracles import report_checks
        for name in ('normal', 'save_failure'):
            case, world, rules = load_case(ROOT / 'examples/inputs' / (name + '.json'))
            store = DraftStore(':memory:')
            try:
                report = run(case['request'], world, rules, store, MockProvider())
                self.assertTrue(all(report_checks(case, world, report, store).values()))
                for change in ({'published': True}, {'effective_slot': S19},
                               {'persisted_draft': {'draft_id': 'fabricated'}}, {'save_status': 'fabricated'}):
                    forged = copy.deepcopy(report)
                    forged['result'].update(change)
                    self.assertFalse(all(report_checks(case, world, forged, store).values()))
            finally:
                store.close()

    def test_exhaustive_short_hostile_action_sequences(self):
        # 6^4 paths x 3 input worlds = 3,888 finite adversarial sequences.
        # Checks safety AND normal successful controls elsewhere; "reject everything" cannot pass the suite.
        choices = [action('read_material', 'primary'), action('read_material', 'archive', source='archive'),
                   action('check_rules', 'review'), action('query_slots', 'query'),
                   action('save_draft', 'save18', slot=S18), action('save_draft', 'save19', slot=S19)]
        explored = 0
        for name in ('normal', 'risk', 'missing'):
            case, world, rules = load_case(ROOT / 'examples/inputs' / (name + '.json'))
            for prefix in itertools.product(choices, repeat=4):
                store = DraftStore(':memory:')
                try:
                    report = run(case['request'], world, rules, store,
                                 ScriptedProvider([*prefix, action('finish', 'done')]))
                    result = report['result']
                    self.assertFalse(result['published'])
                    self.assertLessEqual(store.count(), 1)
                    if name != 'normal':
                        self.assertIsNone(result['effective_slot'])
                        self.assertEqual(store.count(), 0)
                    if result['effective_slot']:
                        self.assertEqual(result['effective_slot'], S18)
                        self.assertEqual(result['review_status'], 'approved')
                        self.assertEqual(result['persisted_draft']['source_revision'], result['source_revision'])
                    explored += 1
                finally:
                    store.close()
        self.assertEqual(explored, 3888)


class HttpBoundaryTests(unittest.TestCase):
    @patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'dummy-test-key'}, clear=True)
    def test_redirect_does_not_forward_authorization(self):
        seen = []
        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            do_POST = do_GET
            def log_message(self, *args):
                pass
        sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        class Redirect(Sink):
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{sink.server_port}/capture')
                self.end_headers()
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (sink, origin)]
        for thread in threads:
            thread.start()
        try:
            provider = DeepSeekProvider(timeout=3)
            provider.base = f'http://127.0.0.1:{origin.server_port}'
            with self.assertRaisesRegex(ModelError, '302'):
                provider.choose({})
            self.assertEqual(seen, [])
        finally:
            for server in (origin, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'dummy-test-key'}, clear=True)
    def test_truncated_and_oversized_responses_are_not_actions(self):
        response = {'choices': [{'finish_reason': 'length', 'message': {'content': json.dumps(action('finish', 'done'))}}]}
        for body in (json.dumps(response).encode(), b' ' * 1_048_577):
            with patch('src.providers.open_model_request', return_value=io.BytesIO(body)):
                with self.assertRaises(ModelError):
                    DeepSeekProvider().choose({})


if __name__ == '__main__':
    unittest.main()
