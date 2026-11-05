import copy
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from src.agent import run
from src.cli import ROOT, load_case, load_json
from src.domain import action, canonical_slot, validate_request
from src.providers import DeepSeekProvider, MockProvider, ModelError, OpenAIProvider
from src.storage import DraftStore
from src.tools import BusinessTools

S18 = '2026-11-06T18:00:00+08:00'
S19 = '2026-11-06T19:00:00+08:00'


class ScriptedProvider:
    name, model, evidence = 'test_double', 'scripted-adversary', 'flow_only'

    def __init__(self, actions):
        self.actions = iter(actions)

    def choose(self, context):
        return copy.deepcopy(next(self.actions))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.store = DraftStore(':memory:')
        self.case, self.world, self.rules = load_case(ROOT / 'examples/inputs/normal.json')

    def tearDown(self):
        self.store.close()

    def execute(self, name='normal', **kwargs):
        case, world, rules = load_case(ROOT / 'examples/inputs' / (name + '.json'))
        return run(case['request'], world, rules, self.store, kwargs.pop('provider', MockProvider()), **kwargs)

    def test_all_acceptance_scenarios(self):
        for file in sorted((ROOT / 'examples/inputs').glob('*.json')):
            with self.subTest(case=file.stem):
                store = DraftStore(':memory:')
                try:
                    case, world, rules = load_case(file)
                    report = run(case['request'], world, rules, store, MockProvider())
                    self.assertEqual(report['result']['task_status'], case['expected_task_status'])
                    self.assertFalse(report['result']['published'])
                finally:
                    store.close()

    def test_normal_saves_original_slot(self):
        report = self.execute()
        self.assertEqual(report['result']['effective_slot'], S18)
        self.assertEqual(report['result']['save_status'], 'saved')
        self.assertEqual(self.store.count(), 1)

    def test_conflict_does_not_override_hard_requirement(self):
        report = self.execute('conflict')
        self.assertEqual(report['request']['requested_slot'], S18)
        self.assertEqual(report['result']['suggested_slot'], S19)
        self.assertIsNone(report['result']['effective_slot'])
        self.assertTrue(report['result']['pending_items'])

    def test_explicit_confirmation_allows_alternate_slot(self):
        self.assertEqual(self.execute('confirmed')['result']['effective_slot'], S19)

    def test_duplicate_uses_one_draft_even_with_new_request_id(self):
        first = self.execute()
        repeated = self.execute()
        self.assertEqual(repeated['result']['save_status'], 'existing')
        self.assertEqual(first['result']['draft_id'], repeated['result']['draft_id'])
        case = copy.deepcopy(self.case)
        case['request']['request_id'] = 'new-transport-id'
        third = run(case['request'], self.world, self.rules, self.store, MockProvider())
        self.assertEqual(third['result']['draft_id'], first['result']['draft_id'])
        self.assertEqual(self.store.count(), 1)

    def test_changed_occupancy_updates_same_pending_draft(self):
        case, world, rules = load_case(ROOT / 'examples/inputs/conflict.json')
        first = run(case['request'], world, rules, self.store, MockProvider())
        world['schedule'] = [{'channel': 'video', 'slot': S19}]
        second = run(case['request'], world, rules, self.store, MockProvider())
        self.assertEqual(second['result']['save_status'], 'updated')
        self.assertEqual(second['result']['effective_slot'], S18)
        self.assertEqual(second['result']['draft_id'], first['result']['draft_id'])
        self.assertEqual(self.store.count(), 1)

    def test_request_id_reuse_with_changed_demand_fails(self):
        self.execute()
        request = {**self.case['request'], 'material_id': 'M03'}
        report = run(request, self.world, self.rules, self.store, MockProvider())
        self.assertEqual(report['result']['task_status'], 'failed')
        self.assertEqual(report['result']['error']['error'], 'idempotency_conflict')
        self.assertEqual(self.store.count(), 1)

    def test_save_failure_never_claims_saved(self):
        report = self.execute('save_failure')
        self.assertEqual(report['result']['save_status'], 'failed')
        self.assertEqual(report['result']['suggested_slot'], S18)
        self.assertIsNone(report['result']['draft_id'])
        self.assertEqual(self.store.count(), 0)

    def test_risk_blocks_even_if_model_attempts_save(self):
        sequence = [action('read_material', 'read'), action('check_rules', 'review'),
                    action('query_slots', 'query'), action('save_draft', 'unsafe attempt', slot=S18),
                    action('finish', 'done')]
        report = self.execute('risk', provider=ScriptedProvider(sequence))
        self.assertEqual(report['result']['task_status'], 'blocked')
        self.assertEqual(report['trace'][3]['result']['error'], 'review_not_approved')
        self.assertEqual(self.store.count(), 0)

    def test_model_cannot_inject_confirmation(self):
        malicious = action('save_draft', 'override', slot=S19)
        malicious['arguments']['confirmed'] = True
        report = self.execute(provider=ScriptedProvider([malicious, action('finish', 'Rejected unsafe proposal')]))
        self.assertEqual(report['result']['task_status'], 'incomplete')
        self.assertEqual(report['trace'][0]['result']['error'], 'invalid_action')
        self.assertEqual(report['tool_calls'], 0)
        self.assertEqual(self.store.count(), 0)

    def test_save_requires_review_and_availability_query(self):
        tools = BusinessTools(validate_request(self.case['request']), self.world, self.rules, self.store)
        self.assertEqual(tools.execute('save_draft', {'slot': S18})['error'], 'review_not_approved')
        tools.execute('read_material', {})
        tools.execute('check_rules', {})
        self.assertEqual(tools.execute('save_draft', {'slot': S18})['error'], 'query_slots_first')

    def test_successful_result_survives_later_failed_save_attempt(self):
        sequence = [action('read_material', 'read'), action('check_rules', 'review'),
                    action('query_slots', 'query'), action('save_draft', 'save', slot=S18),
                    action('save_draft', 'invalid repeat', slot='2026-11-08T18:00:00+08:00'),
                    action('finish', 'done')]
        report = self.execute(provider=ScriptedProvider(sequence))
        self.assertEqual(report['result']['save_status'], 'failed')
        self.assertEqual(report['result']['task_status'], 'failed')
        self.assertIsNotNone(report['result']['persisted_draft'])
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(report['result']['error']['error'], 'slot_not_available')

    def test_missing_information_is_not_invented(self):
        report = self.execute('missing')
        self.assertIn('rights_status', report['result']['missing_fields'])
        self.assertEqual(report['result']['save_status'], 'not_attempted')
        self.assertEqual(self.store.count(), 0)

    def test_model_can_finish_after_failed_supplement_before_rule_check(self):
        sequence = [action('read_material', 'read'), action('read_material', 'supplement', source='archive'),
                    action('finish', 'Missing copyright metadata cannot be recovered')]
        report = self.execute('missing', provider=ScriptedProvider(sequence))
        self.assertEqual(report['result']['task_status'], 'needs_information')
        self.assertEqual(report['result']['review_status'], 'not_checked')
        self.assertIn('rights_status', report['result']['pending_items'])
        self.assertEqual(report['result']['save_status'], 'not_attempted')

    def test_supplement_is_rechecked_before_save(self):
        report = self.execute('supplement')
        names = [s['action'] for s in report['trace']]
        self.assertEqual(names[:4], ['read_material', 'check_rules', 'read_material', 'check_rules'])
        self.assertEqual(report['result']['task_status'], 'completed')

    def test_transient_tool_error_changes_action_and_preserves_review(self):
        report = self.execute('query_retry')
        queries = [s for s in report['trace'] if s['action'] == 'query_slots']
        self.assertFalse(queries[0]['result']['ok'])
        self.assertTrue(queries[1]['result']['ok'])
        self.assertEqual(sum(s['action'] == 'check_rules' for s in report['trace']), 1)

    def test_race_at_save_is_rechecked_and_model_requeries(self):
        report = self.execute('race')
        saves = [s for s in report['trace'] if s['action'] == 'save_draft']
        self.assertEqual(saves[0]['result']['error'], 'slot_conflict')
        self.assertEqual(saves[0]['next_action'], 'query_slots')
        self.assertTrue(saves[1]['result']['ok'])
        self.assertIsNone(report['result']['effective_slot'])

    def test_step_limit_is_explicitly_incomplete(self):
        report = self.execute(max_steps=2)
        self.assertEqual(report['result']['task_status'], 'incomplete')
        self.assertEqual(report['result']['stop_reason'], 'step_limit')
        self.assertEqual(report['tool_calls'], 2)

    def test_limit_after_save_keeps_truthful_save_status(self):
        report = self.execute(max_steps=4)
        self.assertEqual(report['result']['task_status'], 'incomplete')
        self.assertEqual(report['result']['save_status'], 'saved')

    def test_model_failure_is_not_replaced_by_mock(self):
        class Failing(MockProvider):
            def choose(self, context):
                raise ModelError('test timeout')
        report = self.execute(provider=Failing())
        self.assertEqual(report['result']['stop_reason'], 'model_error')
        self.assertEqual(report['trace'], [])
        self.assertEqual(self.store.count(), 0)

    def test_naive_time_rejected_and_offsets_normalized(self):
        with self.assertRaises(ValueError):
            validate_request({**self.case['request'], 'requested_slot': '2026-11-06T18:00:00'})
        self.assertEqual(canonical_slot('2026-11-06T10:00:00Z'), S18)

    def test_pending_proposal_does_not_reserve_slot(self):
        self.execute('conflict')
        request = {**self.case['request'], 'request_id': 'another', 'material_id': 'M03', 'requested_slot': S19}
        report = run(request, self.world, self.rules, self.store, MockProvider())
        self.assertEqual(report['result']['effective_slot'], S19)

    def test_ready_draft_reserves_slot_against_other_material(self):
        self.execute()
        request = {**self.case['request'], 'request_id': 'another', 'material_id': 'M03'}
        report = run(request, self.world, self.rules, self.store, MockProvider())
        self.assertEqual(report['result']['suggested_slot'], S19)
        self.assertIsNone(report['result']['effective_slot'])

    def test_concurrent_saves_cannot_reserve_same_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'concurrent.sqlite'
            first = DraftStore(database)
            first.close()
            barrier = threading.Barrier(2)
            def worker(index):
                store = DraftStore(database)
                try:
                    request = validate_request({**self.case['request'], 'request_id': str(index), 'material_id': 'M' + str(index)})
                    draft = {'channel': 'video', 'effective_slot': S18, 'suggested_slot': S18}
                    barrier.wait(timeout=5)
                    return store.save(request, draft)
                finally:
                    store.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, [10, 11]))
            self.assertEqual(sum(x['ok'] for x in results), 1)
            self.assertEqual(next(x for x in results if not x['ok'])['error'], 'slot_conflict')


class ProviderTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_api_requires_explicit_configuration(self):
        with self.assertRaises(ModelError):
            OpenAIProvider()

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-not-a-secret'}, clear=True)
    def test_deepseek_json_contract_and_observation_feedback(self):
        value = {'id': 'synthetic-response', 'model': 'deepseek-flash', 'usage': {'total_tokens': 12},
                 'choices': [{'message': {'content': json.dumps(action('finish', 'Observed tool failure'))}}]}
        context = {'request': {}, 'observations': [{'tool': 'save_draft', 'result': {'ok': False}}]}
        with patch('src.providers.open_model_request', return_value=io.BytesIO(json.dumps(value).encode())) as transport:
            provider = DeepSeekProvider()
            self.assertEqual(provider.choose(context)['action'], 'finish')
            body = json.loads(transport.call_args.args[0].data)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(json.loads(body['messages'][1]['content']), context)
            self.assertNotIn('test-not-a-secret', body['messages'][0]['content'])
            self.assertEqual(provider.last_metadata['id'], 'synthetic-response')

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-not-a-secret'}, clear=True)
    def test_blank_json_is_model_error(self):
        data = {'choices': [{'message': {'content': '   '}}]}
        with patch('src.providers.open_model_request', return_value=io.BytesIO(json.dumps(data).encode())):
            with self.assertRaises(ModelError):
                DeepSeekProvider().choose({})


if __name__ == '__main__':
    unittest.main()
