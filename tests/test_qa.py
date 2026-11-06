import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

import qa


class QATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.analysis = {'confirmed': True, 'scenes': [{'id': 'a', 'start': 2, 'end': 5}]}
        self.script = {'approved': True, 'sentences': [{'id': 's1', 'scene_id': 'a', 'text': '문이 열립니다.'}]}
        self.narration = {'duration': 1, 'mock': False, 'segments': [
            {'sentence_id': 's1', 'scene_id': 'a', 'text': '문이 열립니다.', 'start': 0, 'end': 1, 'duration': 1,
             'source_start': 2, 'source_end': 3}]}
        self.audio = self.folder / 'audio.wav'
        self.write_audio(b'\x01\x00' * 1000)

    def write_audio(self, pcm, width=2):
        with wave.open(str(self.audio), 'wb') as audio:
            audio.setparams((1, width, 1000, 0, 'NONE', 'not compressed'))
            audio.writeframes(pcm)

    def report(self):
        return qa.check_report(self.analysis, self.script, self.narration, self.audio)

    def codes(self):
        return {issue['code'] for issue in self.report()['issues']}

    def test_valid_mapping_and_unclipped_audio_pass_with_manual_review_limit(self):
        report = self.report()
        self.assertTrue(report['passed'])
        self.assertTrue(report['needs_review'])
        self.assertTrue(all(c['status'] == 'passed' for c in report['checks']))
        self.assertTrue(report['limitations'])

    def test_subtitle_overflow_and_out_of_bounds_timing(self):
        self.narration['segments'][0].update(text='가' * 49, end=2)
        self.assertTrue({'caption_overflow', 'caption_timing', 'script_audio_mismatch'} <= self.codes())
        self.assertFalse(self.report()['passed'])

    def test_actual_caption_chunks_override_whole_sentence_heuristic(self):
        self.narration['segments'][0]['text'] = self.script['sentences'][0]['text'] = '가' * 49
        self.narration['captions'] = [
            {'text': '가' * 24, 'start': 0, 'end': .5, 'sentence_id': 's1'},
            {'text': '가' * 25, 'start': .5, 'end': 1, 'sentence_id': 's1'}]
        self.assertNotIn('caption_overflow', self.codes())

    def test_scene_and_source_excerpt_mismatches(self):
        self.narration['segments'][0]['scene_id'] = 'other'
        self.assertIn('scene_mismatch', self.codes())
        self.narration['segments'][0]['scene_id'] = 'a'
        self.narration['segments'][0]['source_end'] = 6
        self.assertIn('source_range_mismatch', self.codes())

    def test_missing_sentence_audio_and_duplicate_segments(self):
        self.script['sentences'].append({'id': 's2', 'scene_id': 'a', 'text': '다음 장면입니다.'})
        self.narration['segments'].append(copy.deepcopy(self.narration['segments'][0]))
        self.assertTrue({'narration_missing', 'sentence_duplicate', 'caption_overlap'} <= self.codes())

    def test_clip_detection_in_all_pcm_widths(self):
        for width in (1, 2, 3, 4):
            positive = (1 << (width * 8 - 1)) - 1
            if width == 1:
                pcm = bytes([255, 0] + [128] * 998)
            else:
                negative = -(1 << (width * 8 - 1))
                pcm = positive.to_bytes(width, 'little', signed=True) + negative.to_bytes(width, 'little', signed=True) + bytes(width * 998)
            self.write_audio(pcm, width)
            with self.subTest(width=width):
                self.assertEqual(qa.audio_stats(self.audio)['clipped_samples'], 2)
                self.assertIn('audio_clipping', self.codes())

    def test_missing_audio_is_reported_as_not_run(self):
        report = qa.check_report(self.analysis, self.script, self.narration)
        self.assertIn({'name': 'audio_clipping', 'status': 'not_run'}, report['checks'])
        self.assertIn('audio_not_checked', [i['code'] for i in report['issues']])

    def test_mock_and_unapproved_are_visible(self):
        self.narration['mock'] = True
        self.script['approved'] = False
        self.analysis['confirmed'] = False
        self.assertTrue({'mock_audio', 'script_unapproved', 'analysis_unconfirmed'} <= self.codes())

    def test_nonfinite_timing_is_rejected(self):
        self.narration['segments'][0]['end'] = float('nan')
        self.assertIn('caption_timing', self.codes())

    def test_cli_outputs_json_and_nonzero_exit_for_failure(self):
        paths = {}
        for name, value in [('analysis', self.analysis), ('script', self.script), ('narration', self.narration)]:
            paths[name] = self.folder / f'{name}.json'
            paths[name].write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        output = self.folder / 'qa.json'
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts' / 'qa_check.py')]
        for name, path in paths.items():
            command += ['--' + name, str(path)]
        command += ['--audio', str(self.audio), '--output', str(output)]
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(output.read_text(encoding='utf-8'))['passed'])
        self.write_audio(b'\xff\x7f' * 1000)
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(output.read_text(encoding='utf-8'))['passed'])


if __name__ == '__main__':
    unittest.main()
