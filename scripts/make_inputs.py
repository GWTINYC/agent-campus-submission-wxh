"""Generate synthetic INPUTS only; outputs are produced by actual agent runs."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S18, S19, S20 = [f'2026-11-06T{hour}:00:00+08:00' for hour in (18, 19, 20)]


def make_inputs():
    normal = {'request_id': 'request-normal', 'material_id': 'M01',
              'requested_slot': S18, 'hard_requirement': True}
    definitions = {
        'normal': ({}, {}, 'completed'),
        'missing': ({'material_id': 'M02'}, {}, 'needs_information'),
        'supplement': ({'material_id': 'M03'}, {}, 'completed'),
        'risk': ({'material_id': 'M04'}, {}, 'blocked'),
        'manual_review': ({'material_id': 'M05'}, {}, 'needs_confirmation'),
        'conflict': ({}, {'schedule': [{'channel': 'video', 'slot': S18}]}, 'needs_confirmation'),
        'confirmed': ({'confirmed_slot': S19}, {'schedule': [{'channel': 'video', 'slot': S18}]}, 'completed'),
        'save_failure': ({}, {'faults': {'save_draft': 'always'}}, 'failed'),
        'query_retry': ({}, {'faults': {'query_slots': 1}}, 'completed'),
        'race': ({}, {'faults': {'race_on_save': True}}, 'needs_confirmation'),
        'no_slots': ({}, {'schedule': [{'channel': 'video', 'slot': s} for s in (S18, S19, S20)]}, 'incomplete'),
        'changed_occupancy': ({}, {'schedule': [{'channel': 'video', 'slot': S19}]}, 'completed'),
    }
    folder = ROOT / 'examples/inputs'
    folder.mkdir(parents=True, exist_ok=True)
    for name, (request_patch, world_patch, expected) in definitions.items():
        case = {'request': {**normal, 'request_id': 'request-' + name, **request_patch},
                'world_patch': world_patch, 'expected_task_status': expected}
        (folder / (name + '.json')).write_text(json.dumps(case, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    make_inputs()
