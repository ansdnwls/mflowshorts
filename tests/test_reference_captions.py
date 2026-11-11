import io
import unittest
from unittest.mock import patch

import caption_drafts
import reference_captions as reference


class ReferenceCaptionsTests(unittest.TestCase):
    def test_region_requires_normalized_finite_coordinates_and_minimum_size(self):
        self.assertIsNone(reference.parse_region(None))
        self.assertEqual(reference.parse_region('0.1,0.6,0.9,1'), (0.1, 0.6, 0.9, 1))
        for bad in ('', 'nan,0,1,1', '0,0,2,1', '0.8,0,0.2,1', '0,0,1,0.02', '0,1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reference.parse_region(bad)

    def test_speech_segments_are_limited_to_analyzed_duration(self):
        srt = ('1\n00:00:01,100 --> 00:00:03,400\n한 손으로\n열어요.\n\n'
               '2\n00:00:04,000 --> 00:00:06,000\n다음 설명\n')
        self.assertEqual(reference.parse_speech(srt, 3),
                         [{'start': 1.1, 'end': 3, 'text': '한 손으로 열어요.'}])

    def test_only_adjacent_matching_screen_samples_merge(self):
        samples = [{'second': i, 'text': text} for i, text in
                   enumerate(['한 손', '한 손 ', '', '한 손'])]
        self.assertEqual(reference.merge_screen(samples, 3.5), [
            {'start': 0, 'end': 2, 'text': '한 손'},
            {'start': 3, 'end': 3.5, 'text': '한 손'},
        ])

    def test_engine_failure_keeps_other_results_and_cleans_upload(self):
        screen = [{'start': 0, 'end': 1, 'text': '한 손으로'}]
        with patch.object(reference.production, 'probe', return_value={'duration': 80, 'audio': True}), \
                patch.object(reference, 'speech', side_effect=ValueError('모델 없음')), \
                patch.object(reference, 'screen', return_value=screen) as read_screen:
            data = reference.recognize(io.BytesIO(b'reference'), 9)
        self.assertEqual(data['duration'], 60)
        self.assertEqual(data['screen'], screen)
        self.assertEqual(data['warnings'], ['모델 없음'])
        self.assertFalse(read_screen.call_args.args[0].exists())

    def test_recognition_only_passes_region_to_screen_engine(self):
        region = reference.parse_region('0,0.55,1,1')
        with patch.object(reference.production, 'probe', return_value={'duration': 8, 'audio': True}), \
                patch.object(reference, 'speech', return_value=[]) as read_speech, \
                patch.object(reference, 'screen', return_value=[]) as read_screen:
            reference.recognize(io.BytesIO(b'video'), 5, region)
        self.assertEqual(len(read_speech.call_args.args), 3)
        self.assertEqual(read_screen.call_args.args[-1], region)

    def test_file_provenance_survives_cue_validation(self):
        lines = caption_drafts.build({
            'product': '밀폐용기', 'reference': '[0.0–2.0초 화면] 한 손으로 열기',
            'reference_filename': '한국 참고 영상.mp4', 'scene': '뚜껑을 열어요.',
        })
        [cue] = reference.production.cues([lines[0]])
        self.assertEqual(cue['evidence']['filename'], '한국 참고 영상.mp4')
        self.assertIn('0.0–2.0초', cue['evidence']['note'])
