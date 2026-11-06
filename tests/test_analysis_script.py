import copy
import json
import unittest

import analysis_script as module


def evidence():
    return {'scenes': [
        {'id': 'scene-1', 'start': 0, 'end': 2, 'description': '강아지가 문 앞에 서 있다',
         'dialogue': '이리 와', 'subtitle_clues': ['잠깐만'], 'id_clues': ['@candidate'],
         'facts': ['강아지가 문 앞에 서 있다'], 'inferences': ['밖에 나가고 싶다']},
        {'id': 'scene-2', 'start': 2, 'end': 4,
         'facts': ['문이 열린다'], 'inferences': []}]}


class AnalysisTests(unittest.TestCase):
    def test_roundtrip_and_edit_requires_confirmation(self):
        confirmed = module.confirm_analysis(evidence(), 4)
        self.assertTrue(confirmed['confirmed'])
        self.assertEqual(json.loads(json.dumps(confirmed, ensure_ascii=False)), confirmed)
        edited = module.validate_analysis(confirmed)
        self.assertFalse(edited['confirmed'])
        self.assertNotIn('fingerprint', edited)
        self.assertEqual(edited['scenes'][0]['facts'], ['강아지가 문 앞에 서 있다'])
        self.assertEqual(edited['scenes'][0]['inferences'], ['밖에 나가고 싶다'])

    def test_invalid_timestamps(self):
        for start, end in [(-1, 2), (2, 2), (3, 2), (False, 1), (0, float('nan')),
                           (0, float('inf')), ('0', 1), (0, 6)]:
            data = evidence()
            data['scenes'][0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                module.validate_analysis(data, 4)

    def test_invalid_ids_fields_and_order(self):
        variants = []
        for key, value in [('id', '../file'), ('facts', 'string'), ('inferences', ['']),
                           ('description', 1), ('id_clues', [True])]:
            data = evidence(); data['scenes'][0][key] = value; variants.append(data)
        data = evidence(); data['scenes'][1]['id'] = 'scene-1'; variants.append(data)
        data = evidence(); data['scenes'].reverse(); variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                module.validate_analysis(data)

    def test_mock_does_not_invent_observations(self):
        analysis = module.MockAnalysisProvider().analyze({'duration': 8})
        scene = analysis['scenes'][0]
        self.assertEqual((scene['start'], scene['end']), (0, 8))
        self.assertFalse(analysis['confirmed'])
        for field in module.LIST_FIELDS:
            self.assertEqual(scene[field], [])
        self.assertEqual(scene['description'], '')
        with self.assertRaises(ValueError): module.confirm_analysis(analysis)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.analysis = module.confirm_analysis(evidence())

    def test_requires_confirmed_unmodified_analysis(self):
        variants = [evidence(), module.validate_analysis(self.analysis)]
        changed = copy.deepcopy(self.analysis); changed['scenes'][0]['facts'] = ['바뀐 사실']; variants.append(changed)
        changed = copy.deepcopy(self.analysis); changed['confirmed'] = 1; variants.append(changed)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError): module.generate_script(data)

    def test_structure_grounding_and_cautious_inference(self):
        script = module.generate_script(self.analysis)
        self.assertEqual([item['section'] for item in script['sentences']], ['hook', 'situation', 'ending'])
        last = script['sentences'][-1]
        self.assertEqual(last['kind'], 'inference')
        self.assertEqual(last['scene_id'], 'scene-1')
        self.assertIn('가능하지만 확인되지 않았습니다', last['text'])
        self.assertEqual(module.validate_script(script, self.analysis), script)

    def test_single_fact_still_has_three_sections_without_inventing_twist(self):
        data = evidence(); data['scenes'] = data['scenes'][:1]; data['scenes'][0]['inferences'] = []
        script = module.generate_script(module.confirm_analysis(data))
        self.assertEqual(len(script['sentences']), 3)
        self.assertTrue(all(item['kind'] == 'fact' for item in script['sentences']))

    def test_rejects_assertion_fact_invention_and_scene_mismatch(self):
        for key, value in [('text', '강아지는 반드시 밖에 나가고 싶어 합니다.'),
                           ('kind', 'fact'), ('scene_id', 'scene-2')]:
            script = module.generate_script(self.analysis)
            script['sentences'][-1][key] = value
            script.pop('text')
            with self.subTest(key=key), self.assertRaises(ValueError): module.validate_script(script, self.analysis)

    def test_rejects_stale_analysis_and_mismatched_plain_text(self):
        script = module.generate_script(self.analysis)
        changed = evidence(); changed['scenes'][0]['dialogue'] = '다른 대사'
        with self.assertRaises(ValueError): module.validate_script(script, module.confirm_analysis(changed))
        script['text'] = '임의의 본문'
        with self.assertRaises(ValueError): module.validate_script(script, self.analysis)

    def test_provider_output_is_validated(self):
        class BadProvider:
            def generate(self, analysis):
                result = module.MockScriptProvider().generate(analysis)
                result['sentences'][0]['text'] = '없는 사실'
                return result
        with self.assertRaises(ValueError): module.generate_script(self.analysis, BadProvider())

    def test_malformed_json_fields_raise_validation_errors(self):
        for key in ('section', 'kind', 'scene_id'):
            script = module.generate_script(self.analysis)
            script['sentences'][0][key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.validate_script(script, self.analysis)


if __name__ == '__main__':
    unittest.main()
