"""Regressions for manual overlays combined with reviewed narration."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import app
import production
import workflow


class MergeRegressionTests(unittest.TestCase):
    def test_manual_cues_override_narration_and_preserve_offsets(self):
        doc = {'headline': '제목', 'script': '자동 대본', 'credit': '출처', 'credit_on': False,
               'cues': production.cues([{'text': '직접 편집', 'start': .2, 'end': .8}]),
               'narration': {'duration': 1, 'segments': [{'sentence_id': 's1', 'text': '자동 대본',
                                                       'start': 0, 'end': 1, 'duration': 1}]}}
        with patch.object(production, 'font_info', return_value=('Test Font', 700)):
            ass, srt = production.subtitles(doc, production.STYLES['basic'], 1, True,
                                           offsets={'title_y': 10, 'caption_y': 20})
        self.assertIn('직접 편집', srt)
        self.assertNotIn('자동 대본', srt)
        self.assertNotIn(',Credit,,', ass)
        self.assertIn(f'{production.CAPTION_BOTTOM["band"] + production.px(20)})', ass)
        voice = production.narration_qa_data(doc, [{'source_start': 0, 'source_end': 1}])
        self.assertEqual(voice['captions'], [{'text': '직접 편집', 'start': .2, 'end': .8}])

    def test_all_merged_controls_invalidate_approval(self):
        base = {'source': {'filename': 'a.mp4'}, 'script': '대본', 'headline': '제목',
                'settings': production.default_settings()}
        fingerprint = production.fingerprint(base)
        for key, value in [('cues', [{'text': '직접 자막'}]), ('blurs', [{'x': .1}]),
                           ('credit_on', False), ('analysis', {'confirmed': True}),
                           ('generated_script', {'text': '검토 대본'})]:
            with self.subTest(key=key):
                self.assertNotEqual(fingerprint, production.fingerprint({**base, key: value}))

    def test_blur_labels_do_not_capture_narration_graph_labels(self):
        boxes = production.blurs([{'x': .1, 'y': .4, 'w': .2, 'h': .1}])
        graph = production.blur_graph('null', boxes, 1, prefix='overlay_')
        self.assertIn('[overlay_v0]', graph)
        self.assertIn('[overlay_a0]', graph)
        self.assertNotIn('[v0]', graph)
        self.assertNotIn('[a0]', graph)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                         'needs FFmpeg/ffprobe and Korean font')
    def test_narration_blurs_and_erasers_render_together(self):
        class ShortVoice:
            name, mock = 'short-test-silence', True
            def synthesize(self, text, output_path):
                with wave.open(str(output_path), 'wb') as output:
                    output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                    output.writeframes(b'\0\0' * 4800)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=blue:size=320x240:rate=30',
                            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-pix_fmt', 'yuv420p',
                            str(source)], check=True, capture_output=True)
            with patch.object(app, 'DB', root / 'test.sqlite'), patch.object(production, 'PROJECT_ROOT', root / 'projects'):
                app.init()
                channel = production.create_channel({'name': '통합 테스트', 'language': 'ko'})
                for mode in (None, 'text', 'ai'):
                    with self.subTest(eraser=mode):
                        ident = production.create_project({'title': '통합 테스트', 'channel_id': channel})
                        with source.open('rb') as stream:
                            production.upload(ident, stream, source.stat().st_size)
                        workflow.save_analysis(ident, {'scenes': [{'id': 's1', 'start': 1, 'end': 1.3,
                                                                  'facts': ['파란 화면이 보인다']}]})
                        workflow.confirm_analysis(ident); workflow.generate_script(ident)
                        boxes = [{'x': .1, 'y': .4, 'w': .2, 'h': .1, 'mode': 'blur'}]
                        if mode:
                            boxes.append({'x': .4, 'y': .4, 'w': .2, 'h': .1, 'mode': mode})
                        production.update_project({'id': ident, 'blurs': boxes,
                            'cues': [{'text': '직접 편집한 자막', 'start': .1, 'end': .5}],
                            'settings': {**production.default_settings(), 'mute': True}})
                        production.approve(ident); workflow.generate_narration(ident, ShortVoice())
                        def letters(task):
                            self.assertEqual(task['chain'], 'null')
                            self.assertEqual(task['start'], 0)
                            shutil.copyfile(task['source'], task['output'])
                        def ai(src, dst, boxes, log=None):
                            self.assertEqual(Path(src).name, 'matched-laid.mp4')
                            shutil.copyfile(src, dst)
                        with patch.object(production, 'erase_letters', side_effect=letters), patch.object(production, 'erase_ai', side_effect=ai):
                            project = production.project(ident)
                            production.LOCK.acquire()
                            production.render(project)
                        project = production.project(ident)
                        self.assertEqual(project['status'], 'completed', project['document']['message'])
                        output = production.asset(ident, 'output')
                        info = production.probe(output)
                        self.assertTrue(info['audio'])  # Muting original never removes narration.
                        self.assertAlmostEqual(info['duration'], .6, delta=.06)
                        manifest = json.loads((output.parent / 'project.json').read_text(encoding='utf-8'))
                        self.assertEqual(manifest['caption_timing'], 'manual timeline')
                        self.assertEqual(manifest['edit_plan'][0]['source_start'], 1)
                        self.assertEqual(len(manifest['blurs']), 1 if mode is None else 2)


if __name__ == '__main__':
    unittest.main()
