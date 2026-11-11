"""Isolated real FFmpeg regression smoke (Python 3.11, no browser dependency).

Run ``python scripts/stability_smoke.py``. Every run keeps its own SQLite DB,
projects, rendered MP4s and JSON report in data/stability-smoke/<unique run>.
Synthetic red/blue source frames and silent MockTTS make checks reproducible;
this does not claim to validate a real Korean speech engine.
"""
from __future__ import annotations

import argparse
from array import array
import datetime as dt
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-6000:])
    return result.stdout


def ffmpeg(*args):
    return command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *map(str, args)])


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def make_sources(work):
    picture = work / 'picture.mp4'
    source = work / 'source-audio.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=320x240:r=30:d=2',
           '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=30:d=2',
           '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
           '-map', '[v]', '-c:v', 'libx264', '-threads', 2, '-pix_fmt', 'yuv420p', picture)
    ffmpeg('-i', picture, '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
           '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-shortest', source)
    return source, picture


def rms(path, start, duration=0.1):
    pcm = ffmpeg('-ss', start, '-i', path, '-t', duration, '-vn',
                 '-ac', 1, '-ar', 24000, '-c:a', 'pcm_s16le', '-f', 's16le', '-')
    values = array('h', pcm)
    if sys.byteorder != 'little':
        values.byteswap()
    check(bool(values), 'Audio measurement returned no samples')
    return math.sqrt(sum(value * value for value in values) / len(values)) / 32768


def color_at(path, at, width, height):
    # Keep clear of centered title/captions and any explicit test overlays.
    rgb = ffmpeg('-ss', at, '-i', path, '-frames:v', 1,
                 '-vf', f'crop=2:2:{width // 4}:{height // 2},format=rgb24', '-f', 'rawvideo', '-')
    check(len(rgb) >= 3, 'Scene sample returned no pixels')
    red, green, blue = rgb[:3]
    if red > blue * 2 and red > green * 2:
        return 'red'
    if blue > red * 2 and blue > green * 2:
        return 'blue'
    raise AssertionError(f'Unexpected source scene color: {list(rgb[:3])}')


def wait_render(production, ident, timeout=120):
    job=production.start_render(ident)
    check(production.start_render(ident)['job_id']==job['job_id'], 'Duplicate request created a job')
    production.process_next_render()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        project = production.project(ident)
        if project['status'] != 'rendering':
            check(project['status'] == 'completed', project['document'].get('message', project['status']))
            return project
        time.sleep(0.1)
    raise TimeoutError('Actual render exceeded 120 seconds')


def run_cut_case(production, channel, source):
    ident=production.create_project({'title':'구간 편집 검증','channel_id':channel})
    with source.open('rb') as stream:
        production.upload(ident,stream,source.stat().st_size)
    cuts=[{'start':2.25,'end':3.25},{'start':0.25,'end':1.25}]
    quote={'text':'앞으로 옮긴 하이라이트','start':0.1,'end':0.8,'x':0.5,'y':0.65,'size':54,'font':''}
    production.update_project({'id':ident,'clips':cuts,'overlays':[quote],
                               'cues':[{'text':'별도 자막','start':1,'end':1.9}]})
    production.approve(ident)
    wait_render(production,ident)
    path=production.asset(ident,'output')
    info=production.probe(path)
    check(abs(info['duration']-2)<0.08,'Cut render duration mismatch')
    check(info['audio'] and rms(path,0.4)>0.01,'Original audio lost')
    check([color_at(path,t,info['width'],info['height']) for t in (0.5,1.5)]==['blue','red'],
          'Highlights were not rendered in their edited order')
    def white_pixels(at):
        frame=ffmpeg('-ss',at,'-i',path,'-frames:v',1,'-vf',
                     'crop=960:100:60:1160,format=rgb24','-f','rawvideo','-')
        return sum(r>200 and g>200 and b>200 for r,g,b in zip(frame[::3],frame[1::3],frame[2::3]))
    check(white_pixels(0.4)>50 and white_pixels(1.4)==0,'Quote was not timed independently')
    with zipfile.ZipFile(production.asset(ident,'archive')) as archive:
        manifest=json.loads(archive.read('project.json'))
        check(manifest['clips']==cuts and manifest['overlays']==[quote],'Timeline not saved in archive')
        check('00:00:01,000 --> 00:00:01,900' in archive.read('captions.srt').decode('utf-8'),
              'Subtitle did not use output time')
    return {'name':'manual-reordered-clips','passed':True,'output':str(path),'render_info':info}


def run_case(production, workflow, narration, channel, name, source, generated, short_scene=False):
    ident = production.create_project({'title': name, 'channel_id': channel})
    with source.open('rb') as reader:
        production.upload(ident, reader, source.stat().st_size)
    settings = {**production.default_settings(), 'duration': 3, 'layout': 'fill'}
    production.update_project({'id': ident, 'headline': '실제 렌더 검증', 'settings': settings})
    if generated:
        span = 0.2 if short_scene else 2.0
        workflow.save_analysis(ident, {'scenes': [
            {'id': 'red', 'start': 0, 'end': span, 'description': '빨간 화면', 'facts': ['빨간 화면이다.']},
            {'id': 'blue', 'start': 2, 'end': 2 + span, 'description': '파란 화면', 'facts': ['파란 화면이다.']},
        ]})
        workflow.confirm_analysis(ident)
        workflow.generate_script(ident)
        production.approve(ident)
        from unittest.mock import patch
        with patch.dict('os.environ', {'MFLOW_TTS_ENGINE': 'mock'}):
            check(workflow.queue_narration(ident)['status']=='queued', 'Narration not queued')
            check(production.process_next_render(), 'Narration queue did not run')
            voice = workflow.queue_narration(ident)['narration']
        check(voice['mock'] is True, 'Smoke must not pretend mock silence is speech')
    else:
        production.update_project({'id': ident, 'script': '직접 작성한 자막입니다.',
                                   'credit': '렌더 검증 출처', 'credit_on': False,
                                   'cues': [{'text': '수동 타임라인', 'start': 0.2, 'end': 1.2,
                                             'pos': 'bottom', 'look': 'outline', 'y': 0}],
                                   'blurs': [{'x': 0.1, 'y': 0.25, 'w': 0.1, 'h': 0.1,
                                              'start': 0, 'end': 0, 'mode': 'blur'}]})
        production.approve(ident)
    project = wait_render(production, ident)
    doc = project['document']
    output = production.asset(ident, 'output')
    info = production.probe(output)
    check((info['width'], info['height']) == (production.WIDTH, production.HEIGHT), 'Render size changed')
    check(info['audio'] == (generated or doc['source']['audio']), 'Unexpected output audio presence')
    expected_duration = doc['narration']['duration'] if generated else settings['duration']
    check(abs(info['duration'] - expected_duration) < 0.12, 'Render duration differs from approved timeline')
    with zipfile.ZipFile(production.asset(ident, 'archive')) as archive:
        names = set(archive.namelist())
        check({'short.mp4', 'captions.srt', 'project.json'} <= names, 'Archive assets missing')
        manifest = json.loads(archive.read('project.json'))
        captions = archive.read('captions.srt').decode('utf-8')
        check(bool(captions.strip()), 'Subtitles are empty')
        if not generated:
            check('수동 타임라인' in captions, 'Manual caption timeline was lost')
        else:
            check('qa-report.json' in names, 'Rendered QA report missing')
            report = json.loads(archive.read('qa-report.json'))
            check(report['audio_scope'] == 'rendered_mix', 'QA did not inspect final mixed audio')
            check(report['passed'], 'QA failed: ' + json.dumps(report['issues'], ensure_ascii=False))
            check('mock_audio' in {item['code'] for item in report['issues']}, 'Mock audio warning missing')
            check(manifest['narration']['mock'], 'Archive hides mock TTS status')
    measurements = []
    if generated:
        for cut in manifest['edit_plan']:
            at = cut['start'] + cut['duration'] * 0.75
            expected_color = cut['scene_id']
            actual_color = color_at(output, at, info['width'], info['height'])
            check(actual_color == expected_color, 'Rendered frame does not match analyzed scene')
            measurements.append({'at': round(at, 4), 'scene': actual_color})
        if doc['source']['audio']:
            # Early in the first scene we hear only ducked original sine + silent mock.
            source_level = rms(source, 0.05)
            output_level = rms(output, 0.05)
            ratio = output_level / source_level
            check(0.12 <= ratio <= 0.25, f'Original audio ducking ratio is {ratio:.3f}, expected about 0.18')
            measurements.append({'original_audio_duck_ratio': round(ratio, 4)})
            if short_scene:
                held_level = rms(output, min(1.0, doc['narration']['segments'][0]['end'] - 0.2))
                check(held_level < 0.001, 'Short scene held picture should pad original audio with silence')
                measurements.append({'held_frame_audio_rms': round(held_level, 6)})
        else:
            level = rms(output, 0.1)
            check(level < 0.001, 'No-audio source plus mock narration should remain silent')
            measurements.append({'mock_only_audio_rms': round(level, 6)})
    return {'name': name, 'passed': True, 'project_id': ident, 'output': str(output),
            'render_info': info, 'mock_tts': bool(generated), 'measurements': measurements}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='Parent folder for a new isolated run')
    args = parser.parse_args(argv)
    base = args.output_dir or ROOT / 'data' / 'stability-smoke'
    work = base.resolve() / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    work.mkdir(parents=True)
    report = {'python': sys.version, 'workdir': str(work), 'cases': [], 'passed': False,
              'limitations': ['MockTTS is silence, not Korean speech.', 'This is a render/API module smoke, not a browser test.']}
    try:
        check(sys.version_info >= (3, 11), 'Python 3.11 or newer required')
        check(bool(shutil.which('ffmpeg') and shutil.which('ffprobe')), 'FFmpeg and ffprobe required')
        import app
        import narration
        import production
        import workflow
        app.DB = work / 'smoke.sqlite'
        production.PROJECT_ROOT = work / 'projects'
        app.init()
        channel = production.create_channel({'name': '격리된 실제 렌더 검증', 'language': 'ko'})
        audio_source, silent_source = make_sources(work)
        try:
            case=run_cut_case(production,channel,audio_source)
        except Exception as exc:
            case={'name':'manual-reordered-clips','passed':False,'error':str(exc)}
        report['cases'].append(case)
        print(('PASS ' if case['passed'] else 'FAIL ')+case['name'],flush=True)
        for name, source, generated, short_scene in [
                ('manual-audio', audio_source, False, False),
                ('manual-noaudio', silent_source, False, False),
                ('narration-audio', audio_source, True, False),
                ('narration-noaudio', silent_source, True, False),
                ('narration-short-scene-hold', audio_source, True, True)]:
            try:
                case = run_case(production, workflow, narration, channel, name, source, generated, short_scene)
            except Exception as exc:
                case = {'name': name, 'passed': False, 'error': str(exc)}
            report['cases'].append(case)
            print(('PASS ' if case['passed'] else 'FAIL ') + name, flush=True)
        report['passed'] = all(case['passed'] for case in report['cases'])
    except Exception as exc:
        report['error'] = str(exc)
    report_path = work / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report: ' + str(report_path), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
