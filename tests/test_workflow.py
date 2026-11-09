"""Integration checks for persisted review, approval, and narration boundaries."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import narration
import production
import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = patch.object(app, 'DB', Path(self.temp.name) / 'db.sqlite')
        self.root = patch.object(production, 'PROJECT_ROOT', Path(self.temp.name) / 'projects')
        self.db.start()
        self.root.start()
        self.addCleanup(self.db.stop)
        self.addCleanup(self.root.stop)
        app.init()
        channel = production.create_channel({'name': '검토 테스트', 'language': 'ko'})
        self.ident = production.create_project({'title': '고양이 장면', 'channel_id': channel})

    def document(self):
        return production.project(self.ident)['document']

    def upload(self):
        with patch.object(production, 'probe', return_value={
                'duration': 30, 'width': 1920, 'height': 1080, 'audio': True}):
            production.upload(self.ident, io.BytesIO(b'source'), 6)

    def reviewed(self):
        self.upload()
        workflow.analyze(self.ident)
        analysis = self.document()['analysis']
        analysis['scenes'][0].update(description='고양이가 앉아 있다.',
                                      facts=['고양이가 앉아 있다.'],
                                      inferences=['쉬려는 것일 수 있다'])
        workflow.save_analysis(self.ident, analysis)
        workflow.confirm_analysis(self.ident)
        workflow.generate_script(self.ident)
        return self.document()

    def voiced(self):
        self.reviewed()
        production.approve(self.ident)
        workflow.generate_narration(self.ident, narration.MockTTS())
        return self.document()

    def test_source_required_for_workflow(self):
        for action in (workflow.analyze, workflow.confirm_analysis, workflow.generate_script):
            with self.subTest(action=action.__name__), self.assertRaises(ValueError):
                action(self.ident)

    def test_publish_approval_requires_current_final_qa_and_rights(self):
        self.voiced()
        p = production.project(self.ident)
        doc = p['document']
        p['status'] = 'completed'
        doc['output'] = 'short.mp4'
        output = production.PROJECT_ROOT / self.ident / doc['output']
        output.write_bytes(b'fixture video')
        doc['qa_report'] = {'passed': True, 'audio_scope': 'rendered_mix',
                            'output_sha256': workflow.audio_digest(output),
                            'checks': [{'name': 'audio_clipping', 'status': 'passed', 'metrics': {'peak': 0.5}}]}
        production.store(p)
        with self.assertRaisesRegex(ValueError, '실제 한국어'):
            workflow.approve_publish(self.ident, True, '직접 촬영')
        doc['narration']['mock'] = False
        doc['qa_report']['audio_scope'] = 'narration_only'
        production.store(p)
        with self.assertRaisesRegex(ValueError, '최종 영상의 QA'):
            workflow.approve_publish(self.ident, True, '직접 촬영')
        doc['qa_report']['audio_scope'] = 'rendered_mix'
        production.store(p)
        for reviewed, rights in ((False, '직접 촬영'), (True, ' ')):
            with self.assertRaises(ValueError):
                workflow.approve_publish(self.ident, reviewed, rights)
        self.assertEqual(workflow.check_qa(self.ident)['qa_report'], doc['qa_report'])
        approval = workflow.approve_publish(self.ident, True, '직접 촬영')['publish_approval']
        self.assertEqual(approval['output_sha256'], doc['qa_report']['output_sha256'])
        output.write_bytes(b'changed fixture video')
        with self.assertRaisesRegex(ValueError, 'QA 이후 변경'):
            workflow.approve_publish(self.ident, True, '직접 촬영')
        production.update_project({'id': self.ident, 'headline': '새 제목'})
        self.assertNotIn('publish_approval', self.document())

    def test_narration_queue_recovers_and_reuses_only_intact_audio(self):
        self.reviewed()
        production.approve(self.ident)
        with patch.dict('os.environ', {'MFLOW_TTS_ENGINE': 'mock'}):
            queued = workflow.queue_narration(self.ident)
            self.assertEqual(workflow.queue_narration(self.ident)['job_id'], queued['job_id'])
            with self.assertRaises(ValueError):
                production.start_render(self.ident)
            with app.connect() as con:
                con.execute("UPDATE production_render_jobs SET status='running', attempts=1")
            app.init()
            production.recover_renders()
            self.assertTrue(production.process_next_render())
            self.assertEqual(production.project(self.ident)['status'], 'approved')
            with app.connect() as con:
                row = con.execute('SELECT id,status,attempts FROM production_render_jobs').fetchone()
            self.assertEqual(tuple(row), (queued['job_id'], 'completed', 2))
            audio = production.asset(self.ident, 'narration')
            self.assertEqual(workflow.queue_narration(self.ident)['status'], 'completed')
            self.assertFalse(production.process_next_render())
            audio.write_bytes(b'corrupted')
            self.assertEqual(workflow.queue_narration(self.ident)['status'], 'queued')
            self.assertNotIn('narration', self.document())
            self.assertTrue(production.process_next_render())
            self.assertNotEqual(production.asset(self.ident, 'narration'), audio)

    def test_narration_failure_keeps_approval_and_allows_retry(self):
        self.reviewed()
        production.approve(self.ident)
        with patch.dict('os.environ', {'MFLOW_TTS_ENGINE': 'mock'}):
            queued = workflow.queue_narration(self.ident)
            with patch.object(narration, 'synthesize', side_effect=ValueError('TTS failed')):
                self.assertTrue(production.process_next_render())
            self.assertEqual(production.project(self.ident)['status'], 'error')
            self.assertEqual(self.document()['message'], 'TTS failed')
            self.assertNotEqual(workflow.queue_narration(self.ident)['job_id'], queued['job_id'])
            self.assertTrue(production.process_next_render())
            self.assertEqual(production.project(self.ident)['status'], 'approved')

    def test_mock_template_cannot_be_confirmed_or_generated_without_review(self):
        self.upload()
        result = workflow.analyze(self.ident)
        self.assertFalse(result['analysis']['confirmed'])
        self.assertTrue(all(not scene['facts'] for scene in result['analysis']['scenes']))
        with self.assertRaises(ValueError):
            workflow.confirm_analysis(self.ident)
        with self.assertRaises(ValueError):
            workflow.generate_script(self.ident)
        self.assertNotIn('generated_script', self.document())

    def test_full_review_approval_voice_qa_and_exports(self):
        doc = self.reviewed()
        self.assertTrue(doc['analysis']['confirmed'])
        self.assertEqual([item['section'] for item in doc['generated_script']['sentences']],
                         ['hook', 'situation', 'ending'])
        with self.assertRaises(ValueError):
            workflow.generate_narration(self.ident, narration.MockTTS())
        production.approve(self.ident)
        result = workflow.generate_narration(self.ident, narration.MockTTS())
        voice = result['narration']
        self.assertTrue(voice['mock'])
        self.assertGreater(voice['duration'], 0)
        self.assertEqual(len(voice['segments']), len(doc['generated_script']['sentences']))
        self.assertEqual(voice['approved_hash'], self.document()['approved_hash'])
        self.assertTrue(production.asset(self.ident, 'narration').is_file())
        exported = json.loads(production.asset(self.ident, 'analysis').read_text(encoding='utf-8'))
        self.assertEqual(exported, self.document()['analysis'])
        report = workflow.check_qa(self.ident)['qa_report']
        self.assertEqual(report, self.document()['qa_report'])
        self.assertIn('mock_audio', [item['code'] for item in report['issues']])
        audio = next(item for item in report['checks'] if item['name'] == 'audio_clipping')
        self.assertEqual(audio['status'], 'passed')

    def test_analysis_edit_revokes_entire_generated_chain(self):
        self.voiced()
        workflow.check_qa(self.ident)
        analysis = self.document()['analysis']
        analysis['scenes'][0]['facts'] = ['고양이가 고개를 돌린다.']
        workflow.save_analysis(self.ident, analysis)
        doc = self.document()
        self.assertFalse(doc['analysis']['confirmed'])
        self.assertIsNone(doc['approved_hash'])
        self.assertEqual(doc['script'], '')
        for key in ('generated_script', 'narration', 'qa_report', 'output', 'archive'):
            self.assertNotIn(key, doc)
        with self.assertRaises(ValueError):
            workflow.generate_script(self.ident)

    def test_source_replacement_revokes_analysis_and_generated_chain(self):
        self.voiced()
        old = self.document()['source']['filename']
        self.upload()
        doc = self.document()
        self.assertNotEqual(old, doc['source']['filename'])
        self.assertIsNone(doc['approved_hash'])
        self.assertEqual(doc['script'], '')
        for key in ('analysis', 'generated_script', 'narration', 'qa_report'):
            self.assertNotIn(key, doc)

    def test_raw_script_tampering_cannot_be_approved(self):
        self.reviewed()
        production.update_project({'id': self.ident, 'script': '확인되지 않은 주장을 사실처럼 단정한다.'})
        with self.assertRaises(ValueError):
            production.approve(self.ident)
        self.assertIsNone(self.document()['approved_hash'])

    def test_rendering_locks_analysis_script_voice_and_qa(self):
        self.reviewed()
        p = production.project(self.ident)
        p['status'] = 'rendering'
        production.store(p)
        original = self.document()
        actions = [lambda: workflow.analyze(self.ident),
                   lambda: workflow.save_analysis(self.ident, original['analysis']),
                   lambda: workflow.confirm_analysis(self.ident),
                   lambda: workflow.generate_script(self.ident),
                   lambda: workflow.generate_narration(self.ident, narration.MockTTS()),
                   lambda: workflow.check_qa(self.ident)]
        for action in actions:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(original, self.document())

    def test_settings_change_blocks_stale_voice_until_recreated(self):
        doc = self.voiced()
        old_hash = doc['narration']['approved_hash']
        production.update_project({'id': self.ident, 'settings': {**doc['settings'], 'mirror': True}})
        self.assertIsNone(self.document()['approved_hash'])
        with self.assertRaises(ValueError):
            workflow.check_qa(self.ident)
        production.approve(self.ident)
        self.assertNotEqual(old_hash, self.document()['approved_hash'])
        with self.assertRaises(ValueError):
            workflow.check_qa(self.ident)
        workflow.generate_narration(self.ident, narration.MockTTS())
        self.assertEqual(self.document()['narration']['approved_hash'], self.document()['approved_hash'])

    def test_failed_analysis_validation_preserves_confirmed_data(self):
        original = self.reviewed()
        with self.assertRaises(ValueError):
            workflow.save_analysis(self.ident, {'scenes': [{'id': 'x', 'start': 3, 'end': 1}]})
        self.assertEqual(original, self.document())


if __name__ == '__main__':
    unittest.main()
