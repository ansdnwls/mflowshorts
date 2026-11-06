import copy
import unittest

import analysis_script
import editing
import narration


def fixture():
    analysis = analysis_script.confirm_analysis({'scenes': [
        {'id': 's1', 'start': 2, 'end': 3, 'facts': ['문이 열린다']},
        {'id': 's2', 'start': 7, 'end': 12, 'facts': ['강아지가 들어온다']}]})
    script = analysis_script.generate_script(analysis)
    segments, start = [], 0
    for sentence, duration in zip(script['sentences'], [2, 3, 1]):
        segments.append({'sentence_id': sentence['id'], 'scene_id': sentence['scene_id'],
                         'text': sentence['text'], 'start': start, 'end': start + duration, 'duration': duration})
        start += duration
    audio = {'duration': start, 'segments': segments, 'script_fingerprint': narration.script_fingerprint(script)}
    return analysis, script, audio


class PlanTests(unittest.TestCase):
    def test_matches_exact_source_scene_and_holds_short_clip(self):
        analysis, script, audio = fixture()
        cuts = editing.build_plan(analysis, script, audio, 15)
        self.assertEqual([(c['source_start'], c['source_end']) for c in cuts], [(2, 3), (7, 10), (10, 11)])
        self.assertEqual([(c['start'], c['end']) for c in cuts], [(0, 2), (2, 5), (5, 6)])
        self.assertEqual(cuts[0]['duration'], 2)
        self.assertEqual(cuts[0]['scene_id'], 's1')

    def test_captions_use_measured_sentence_times(self):
        _, script, audio = fixture()
        events = editing.caption_events(script, audio)
        self.assertEqual([e['text'] for e in events], [s['text'] for s in script['sentences']])
        self.assertEqual([(e['start'], e['end']) for e in events], [(0, 2), (2, 5), (5, 6)])

    def test_rejects_stale_or_mismatched_narration(self):
        for key, value in [('text', '잘못된 대본'), ('scene_id', 's2'), ('sentence_id', 'wrong'),
                           ('start', .1), ('end', 1), ('duration', 1), ('start', float('nan'))]:
            analysis, script, audio = fixture()
            audio['segments'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                editing.build_plan(analysis, script, audio, 15)
        analysis, script, audio = fixture()
        audio['script_fingerprint'] = 'old'
        with self.assertRaises(ValueError): editing.build_plan(analysis, script, audio, 15)

    def test_rejects_outside_source_unconfirmed_or_long_timeline(self):
        analysis, script, audio = fixture()
        with self.assertRaises(ValueError): editing.build_plan(analysis, script, audio, 10)
        unconfirmed = analysis_script.validate_analysis(analysis)
        with self.assertRaises(ValueError): editing.build_plan(unconfirmed, script, audio, 15)
        audio['segments'][-1].update(end=65, duration=60); audio['duration'] = 65
        with self.assertRaises(ValueError): editing.build_plan(analysis, script, audio, 15)

    def test_rejects_gap_and_total_duration_mismatch(self):
        analysis, script, audio = fixture()
        audio['segments'][1].update(start=3, end=6)
        with self.assertRaises(ValueError): editing.build_plan(analysis, script, audio, 15)
        analysis, script, audio = fixture(); audio['duration'] = 7
        with self.assertRaises(ValueError): editing.build_plan(analysis, script, audio, 15)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.plan = editing.build_plan(*fixture(), 15)

    def test_matching_freeze_audio_padding_and_ducking(self):
        result = editing.ffmpeg_filters(self.plan)
        graph = ';'.join(result['filters'])
        self.assertEqual(result['video_label'], 'matched_video')
        self.assertEqual(result['audio_label'], 'mixed_audio')
        self.assertIn('[0:v]split=3[srcv0][srcv1][srcv2]', graph)
        self.assertIn('[0:a]asplit=3[srca0][srca1][srca2]', graph)
        self.assertIn('trim=start=2.000000:end=3.000000', graph)
        self.assertIn('tpad=stop_mode=clone:stop_duration=1.000000', graph)
        self.assertIn('apad=whole_dur=2.000000', graph)
        self.assertIn('concat=n=3:v=1:a=0[matched_video]', graph)
        self.assertIn("volume=0.180000:enable='between(t,0.000000,2.000000)", graph)
        self.assertIn('normalize=0', graph)
        self.assertIn('alimiter=limit=0.95:level=0', graph)

    def test_muted_or_missing_original_preserves_narration(self):
        for kwargs in ({'mute': True}, {'has_source_audio': False}):
            with self.subTest(kwargs=kwargs):
                graph = ';'.join(editing.ffmpeg_filters(self.plan, **kwargs)['filters'])
                self.assertNotIn('[0:a]', graph)
                self.assertNotIn('amix', graph)
                self.assertIn('[1:a]', graph)
                self.assertIn('[narration_audio]alimiter=', graph)

    def test_invalid_plan_and_ducking_rejected(self):
        for volume in (-1, 2, float('inf'), True, '0.5'):
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                editing.ffmpeg_filters(self.plan, duck_volume=volume)
        for key, value in [('source_start', -1), ('source_end', 8), ('start', 1),
                           ('duration', float('nan')), ('source_end', '1;evil')]:
            plan = copy.deepcopy(self.plan); plan[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): editing.ffmpeg_filters(plan)


if __name__ == '__main__':
    unittest.main()
