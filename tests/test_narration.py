import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import narration


class MeasuredEngine:
    name = 'fixture-voice'
    mock = False

    def synthesize(self, text, output_path):
        frames = 1000 if text == '첫 문장입니다.' else 2500
        with wave.open(str(output_path), 'wb') as audio:
            audio.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * frames)


class NarrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.script = {'approved': True, 'sentences': [
            {'id': 's1', 'scene_id': 'a', 'kind': 'fact', 'section': 'hook', 'text': '첫 문장입니다.'},
            {'id': 's2', 'scene_id': 'b', 'kind': 'fact', 'section': 'ending', 'text': '두 번째 문장입니다.'},
        ]}

    def test_unapproved_script_is_rejected_before_engine_runs(self):
        for approval in (False, None, 1, 'true'):
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                narration.synthesize({**self.script, 'approved': approval}, self.folder)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_actual_pcm_frame_counts_determine_sentence_timings(self):
        result = narration.synthesize(self.script, self.folder, MeasuredEngine())
        self.assertFalse(result['mock'])
        self.assertEqual(result['duration'], 3.5)
        self.assertEqual([(s['start'], s['end'], s['duration']) for s in result['segments']],
                         [(0, 1, 1), (1, 3.5, 2.5)])
        self.assertEqual([s['scene_id'] for s in result['segments']], ['a', 'b'])
        with wave.open(str(self.folder / 'narration.wav'), 'rb') as audio:
            self.assertEqual(audio.getnframes(), 3500)
        self.assertTrue((self.folder / 'narration.json').is_file())

    def test_mock_is_explicit_and_contains_only_silence(self):
        result = narration.synthesize(self.script, self.folder, narration.MockTTS())
        self.assertTrue(result['mock'])
        self.assertEqual(result['engine'], 'mock-silence')
        with wave.open(str(self.folder / 'narration.wav'), 'rb') as audio:
            self.assertEqual(set(audio.readframes(audio.getnframes())), {0})

    def test_modified_script_changes_audio_binding(self):
        changed = copy.deepcopy(self.script)
        changed['sentences'][0]['text'] = '수정한 문장입니다.'
        self.assertNotEqual(narration.script_fingerprint(changed), narration.script_fingerprint(self.script))

    def test_bad_sentence_ids_and_empty_sentences_are_rejected(self):
        invalid = [[], [self.script['sentences'][0]] * 2,
                   [{**self.script['sentences'][0], 'scene_id': ''}],
                   [{**self.script['sentences'][0], 'text': ' '}],
                   [None]]
        for sentences in invalid:
            with self.subTest(sentences=sentences), self.assertRaises(ValueError):
                narration.synthesize({'approved': True, 'sentences': sentences}, self.folder)

    def test_failed_provider_preserves_previous_output(self):
        narration.synthesize(self.script, self.folder, MeasuredEngine())
        before = (self.folder / 'narration.wav').read_bytes()

        class Broken(MeasuredEngine):
            def synthesize(self, text, output_path):
                raise ValueError('provider failed')

        with self.assertRaisesRegex(ValueError, 'provider failed'):
            narration.synthesize(self.script, self.folder, Broken())
        self.assertEqual((self.folder / 'narration.wav').read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['narration.json', 'narration.wav'])

    def test_mismatched_pcm_formats_rejected(self):
        class ChangedFormat(MeasuredEngine):
            def synthesize(self, text, output_path):
                with wave.open(str(output_path), 'wb') as audio:
                    audio.setparams((1, 2, 1000 if text.startswith('첫') else 2000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(b'\x00\x00' * 1000)

        with self.assertRaisesRegex(ValueError, '형식'):
            narration.synthesize(self.script, self.folder, ChangedFormat())
        self.assertFalse((self.folder / 'narration.wav').exists())

    def test_default_and_explicit_engine_selection(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(narration.configured_engine(), narration.MockTTS)
        with patch.dict(os.environ, {'MFLOW_TTS_ENGINE': 'sapi'}):
            self.assertIsInstance(narration.configured_engine(), narration.WindowsSAPI)
        with patch.dict(os.environ, {'MFLOW_TTS_ENGINE': 'external'}), self.assertRaises(ValueError):
            narration.configured_engine()


if __name__ == '__main__':
    unittest.main()
