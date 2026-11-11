import io
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from array import array
from unittest.mock import patch

import app
import audio_mixer
import production


def ffmpeg(*args):
    return subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *map(str, args)],
        capture_output=True, check=True, timeout=60,
    ).stdout


def samples(path):
    return array('f', ffmpeg('-i', path, '-map', '0:a:0', '-ac', 1, '-ar', 48000,
                            '-c:a', 'pcm_f32le', '-f', 'f32le', '-'))


def amplitude(audio, frequency, start, end):
    window = audio[round(start * 48000):round(end * 48000)]
    angle = 2 * math.pi * frequency / 48000
    real = sum(value * math.cos(index * angle) for index, value in enumerate(window))
    imag = sum(value * math.sin(index * angle) for index, value in enumerate(window))
    return 2 * math.hypot(real, imag) / len(window)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                     'FFmpeg, FFprobe and the production font are required')
class AudioMixerTests(unittest.TestCase):
    def test_generated_speech_ducks_original_even_without_music(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, voice, output = (root / name for name in ('source.wav', 'voice.wav', 'mix.wav'))
            ffmpeg('-f', 'lavfi', '-i', 'sine=f=400:r=48000:d=2', source)
            ffmpeg('-f', 'lavfi', '-i', r'aevalsrc=0.1*sin(2*PI*800*t)*between(t\,0.7\,1.2):s=48000:d=2', voice)
            mix = audio_mixer.settings({'source_volume': 100}, root)
            audio_mixer.render(output, source, 'wav', [(0, 2, 2)], 2, mix, True, root,
                               (voice, 2), duck_generated_source=True)
            mixed = samples(output)
            original = amplitude(mixed, 400, 0.25, 0.45)
            self.assertGreater(original, 0.1)
            self.assertAlmostEqual(amplitude(mixed, 400, 0.85, 1.1) / original, 0.18, delta=0.02)
            self.assertAlmostEqual(amplitude(mixed, 400, 1.65, 1.85), original, delta=0.002)

    def test_saved_reordered_mix_matches_preview_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(app, 'DB', root / 'test.sqlite'), patch.object(production, 'PROJECT_ROOT', root / 'projects'):
                app.init()
                channel = production.create_channel({'name': 'Mixer', 'language': 'ko'})
                project = production.create_project({'title': 'Mixer', 'channel_id': channel})
                source = root / 'source.mp4'
                voice = root / 'voice.wav'
                music = root / 'music.wav'
                ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=160x90:r=30:d=2',
                       '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30:d=2',
                       '-f', 'lavfi', '-i', 'sine=f=400:r=48000:d=2',
                       '-f', 'lavfi', '-i', 'sine=f=600:r=48000:d=2',
                       '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]concat=n=2:v=0:a=1[a]',
                       '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-c:a', 'aac', source)
                ffmpeg('-f', 'lavfi', '-i', "aevalsrc=0.12*sin(2*PI*800*t)*between(t\,0.35\,1):s=48000:d=1.6", voice)
                ffmpeg('-f', 'lavfi', '-i', 'sine=f=1000:r=48000:d=5', music)
                with source.open('rb') as reader:
                    production.upload(project, reader, source.stat().st_size)
                tracks = {}
                for kind, path, volume, at, start, end in (
                    ('narration', voice, 65, 0.75, 0.1, 1.4), ('music', music, 50, 0.25, 0.5, 4),
                ):
                    content = path.read_bytes()
                    asset = production.upload_audio(project, io.BytesIO(content), len(content))
                    tracks[kind] = {'volume': volume, 'muted': False, 'clip': {
                        **asset, 'name': path.name, 'at': at, 'in': start, 'out': end,
                    }}
                mix = {'source_volume': 40, **tracks, 'duck': {'enabled': True, 'reduction': 75}}
                earlier, later = {'start': 0, 'end': 2}, {'start': 2, 'end': 4}
                production.update_project({'id': project, 'clips': [earlier, later], 'audio_mix': mix})
                production.update_project({'id': project, 'clips': [later, earlier]})

                # Reopen through SQLite, then render the reopened edit through the worker.
                app.init()
                saved = production.project(project)['document']
                self.assertEqual(saved['audio_mix'], mix)
                self.assertEqual([(c['start'], c['end']) for c in saved['clips']], [(2, 4), (0, 2)])
                preview = root / 'preview.mp4'
                preview.write_bytes(production.preview_mix({'id': project}))
                production.approve(project)
                production.start_render(project)
                self.assertTrue(production.process_next_render())
                self.assertEqual(production.project(project)['status'], 'completed')
                output = production.asset(project, 'output')
                exported = samples(output)
                self.assertEqual(samples(preview), exported)

                def level(frequency, start, end):
                    return amplitude(exported, frequency, start, end)

                self.assertGreater(level(600, 0.4, 0.7), 0.03)
                self.assertLess(level(400, 0.4, 0.7), 0.002)
                self.assertGreater(level(400, 2.3, 2.6), 0.03)
                self.assertLess(level(600, 2.3, 2.6), 0.002)
                # The output starts in the blue clip with the 600 Hz tone and
                # switches to the red clip with the 400 Hz tone at the same cut.
                for time, channel in ((0.5, 2), (2.5, 0)):
                    pixel = ffmpeg('-ss', time, '-i', output, '-frames:v', 1,
                                   '-vf', 'crop=2:2:540:960,format=rgb24', '-f', 'rawvideo', '-')
                    self.assertGreater(pixel[channel], 200)
                    self.assertLess(pixel[2-channel], 30)
                self.assertLess(level(800, 0.8, 0.9), 0.002)
                self.assertAlmostEqual(level(800, 1.15, 1.5), 0.12 * 0.65, delta=0.005)
                self.assertLess(level(800, 1.8, 1.9), 0.002)
                self.assertLess(level(1000, 0.05, 0.15), 0.002)
                self.assertLess(level(1000, 3.85, 3.95), 0.002)
                normal = level(1000, 0.4, 0.7)
                self.assertAlmostEqual(normal, 0.125 * 0.5, delta=0.004)
                ducked = level(1000, 1.15, 1.5)
                self.assertAlmostEqual(ducked / normal, 0.25, delta=0.03)
                self.assertLess(ducked, level(1000, 1.75, 1.85))
                self.assertLess(level(1000, 1.75, 1.85), normal)
                self.assertAlmostEqual(level(1000, 2.1, 2.3), normal, delta=0.003)

                mix['narration']['muted'] = True
                mix['source_volume'] = 100
                preview.write_bytes(production.preview_mix({'id': project, 'audio_mix': mix}))
                draft = samples(preview)
                self.assertAlmostEqual(level(600, 0.4, 0.7) / amplitude(draft, 600, 0.4, 0.7), 0.4, delta=0.03)
                self.assertLess(amplitude(draft, 800, 1.15, 1.5), 0.002)
                self.assertAlmostEqual(amplitude(draft, 1000, 1.15, 1.5), normal, delta=0.003)

                mix['music']['muted'] = True
                mix['narration']['muted'] = False
                production.update_project({'id': project, 'audio_mix': mix,
                                           'settings': {**saved['settings'], 'mute': True}})
                production.approve(project)
                production.start_render(project)
                self.assertTrue(production.process_next_render())
                muted = samples(production.asset(project, 'output'))
                self.assertLess(amplitude(muted, 600, 0.4, 0.7), 0.002)
                self.assertLess(amplitude(muted, 1000, 1.15, 1.5), 0.002)
                self.assertAlmostEqual(amplitude(muted, 800, 1.15, 1.5), 0.12 * 0.65, delta=0.005)
