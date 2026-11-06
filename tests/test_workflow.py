"""Integration checks for persisted review, approval, and narration boundaries."""
import io
import json
import shutil
import subprocess
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

    def real_source(self, seconds=6):
        source = Path(self.temp.name) / 'src.mp4'
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=540x960:rate=30',
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', str(seconds), '-pix_fmt', 'yuv420p',
                        '-shortest', str(source)], check=True)
        with source.open('rb') as file:
            production.upload(self.ident, file, source.stat().st_size)

    def two_scene_voice(self, blurs=None):
        workflow.analyze(self.ident)
        analysis = self.document()['analysis']
        analysis['scenes'] = [
            {'id': 'scene-1', 'start': 0, 'end': 1, 'description': '첫 장면', 'dialogue': '',
             'subtitle_clues': [], 'id_clues': [], 'facts': ['고양이가 앉아 있다'], 'inferences': []},
            {'id': 'scene-2', 'start': 3, 'end': 6, 'description': '둘째 장면', 'dialogue': '',
             'subtitle_clues': [], 'id_clues': [], 'facts': ['고양이가 일어선다'], 'inferences': ['배가 고팠을 수 있다']}]
        workflow.save_analysis(self.ident, analysis)
        workflow.confirm_analysis(self.ident)
        workflow.generate_script(self.ident)
        doc = self.document()
        production.update_project({'id': self.ident, 'script': doc['script'], 'headline': '고양이',
                                   'settings': {**doc['settings'], 'layout': 'fit'}, 'blurs': blurs or []})
        production.approve(self.ident)
        workflow.generate_narration(self.ident, narration.MockTTS())

    def render_now(self):
        p = production.project(self.ident)
        production.LOCK.acquire()
        production.render(p)
        p = production.project(self.ident)
        self.assertEqual(p['status'], 'completed', p['document']['message'])
        return p['document'], (production.PROJECT_ROOT / self.ident / p['document']['output']).parent

    @unittest.skipUnless(shutil.which('ffmpeg') and Path(production.FONT).is_file(), 'needs ffmpeg and a Korean font')
    def test_voice_render_follows_narration_and_ducks_original(self):
        self.real_source()
        self.two_scene_voice(blurs=[{'x': 0.1, 'y': 0.3, 'w': 0.3, 'h': 0.05, 'mode': 'blur'}])
        doc, run = self.render_now()
        voice = doc['narration']
        info = production.probe(run / 'short.mp4')
        self.assertEqual((info['width'], info['height'], info['audio']), (1080, 1920, True))
        self.assertAlmostEqual(info['duration'], voice['duration'], delta=0.15)
        report = doc['qa_report']
        self.assertEqual(report['audio_scope'], 'rendered_mix')
        self.assertNotIn('audio_duration_mismatch', [item['code'] for item in report['issues']], report)
        self.assertTrue(report['passed'], report)
        manifest = json.loads((run / 'project.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'], 2)
        self.assertEqual([cut['scene_id'] for cut in manifest['edit_plan']], ['scene-1', 'scene-2', 'scene-2'])
        srt = (run / 'captions.srt').read_text(encoding='utf-8')
        last = voice['segments'][-1]
        self.assertIn(production.srt_time(last['end']), srt)

    @unittest.skipUnless(shutil.which('ffmpeg') and Path(production.FONT).is_file(), 'needs ffmpeg and a Korean font')
    def test_voice_render_with_text_eraser_reads_source_from_zero(self):
        self.real_source()
        self.two_scene_voice(blurs=[{'x': 0.1, 'y': 0.6, 'w': 0.8, 'h': 0.05, 'mode': 'text'}])
        tasks = []

        def fake_eraser(task):
            # Stand-in for the OpenCV eraser: lay out the window it was asked for.
            tasks.append(task)
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-ss', str(task['start']), '-i', task['source'],
                            '-t', str(task['duration']), '-an', '-vf', task['chain'], '-pix_fmt', 'yuv420p',
                            task['output']], check=True)
        with patch.object(production, 'erase_letters', fake_eraser):
            doc, run = self.render_now()
        self.assertEqual(tasks[0]['start'], 0)
        self.assertAlmostEqual(tasks[0]['duration'], 6, delta=0.01)
        info = production.probe(run / 'short.mp4')
        self.assertEqual((info['width'], info['height'], info['audio']), (1080, 1920, True))
        self.assertTrue(doc['qa_report']['passed'], doc['qa_report'])


if __name__ == '__main__':
    unittest.main()
