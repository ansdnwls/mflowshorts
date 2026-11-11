"""Local Korean transcription and sampled OCR for a reference video."""

import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

import production


ROOT = Path(__file__).resolve().parent
MAX_BYTES = 100 * 1024 * 1024
MAX_SECONDS = 60
RECOGNITION = threading.Lock()
SRT_TIME = re.compile(r'(\d+):(\d\d):(\d\d),(\d\d\d) --> (\d+):(\d\d):(\d\d),(\d\d\d)')


def run(command, timeout, cwd=None):
    return subprocess.run(command, cwd=cwd, timeout=timeout, check=True, capture_output=True,
                          text=True, encoding='utf-8', errors='replace',
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout


def ffmpeg_input(path):
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-format_whitelist', production.SOURCE_FORMATS, '-protocol_whitelist', 'file',
            '-i', str(path)]


def parse_speech(srt, duration):
    def seconds(parts):
        hours, minutes, secs, milliseconds = map(int, parts)
        return hours * 3600 + minutes * 60 + secs + milliseconds / 1000

    rows = []
    for block in re.split(r'\r?\n\s*\r?\n', srt.strip()):
        lines = block.splitlines()
        match = SRT_TIME.fullmatch(lines[1]) if len(lines) >= 3 else None
        if not match:
            continue
        start = seconds(match.groups()[:4])
        end = min(duration, seconds(match.groups()[4:]))
        text = ' '.join(' '.join(lines[2:]).split())[:500]
        if text and start < end:
            rows.append({'start': start, 'end': end, 'text': text})
    return rows


def speech(path, directory, duration):
    model = Path(os.environ.get('MFLOW_WHISPER_MODEL', ROOT / 'data/models/ggml-tiny.bin')).resolve()
    if not model.is_file():
        raise ValueError('받아쓰기 모델이 없습니다. data/models/ggml-tiny.bin을 준비하세요.')
    destination = directory / 'speech.srt'
    graph = (f'atrim=end={duration},asetpts=PTS-STARTPTS,'
             f'whisper=model={production.filter_path(model.name)}:language=ko:queue=30:'
             f'use_gpu=false:format=srt:destination={production.filter_path(destination)}')
    run(ffmpeg_input(path) + ['-vn', '-map', '0:a:0', '-af', graph, '-f', 'null', '-'], 120, cwd=model.parent)
    return parse_speech(destination.read_text(encoding='utf-8-sig'), duration)


def merge_screen(samples, duration):
    rows = []
    for sample in samples:
        start = sample['second']
        text = ' '.join(sample['text'].split())[:500]
        if not text or start >= duration:
            continue
        end = min(start + 1, duration)
        if rows and rows[-1]['end'] == start and rows[-1]['text'] == text:
            rows[-1]['end'] = end
        else:
            rows.append({'start': start, 'end': end, 'text': text})
    return rows


def parse_region(value):
    if value is None:
        return None
    try:
        left, top, right, bottom = map(float, value.split(','))
    except (AttributeError, ValueError):
        raise ValueError('자막 인식 영역을 다시 지정하세요.') from None
    bounds = left, top, right, bottom
    if (not all(math.isfinite(v) and 0 <= v <= 1 for v in bounds)
            or right-left < .0499 or bottom-top < .0499):
        raise ValueError('자막 인식 영역은 가로·세로 각각 영상의 5% 이상이어야 합니다.')
    return bounds


def screen(path, directory, duration, region=None):
    if os.name != 'nt':
        raise ValueError('화면 자막 인식에는 Windows 한국어 OCR이 필요합니다.')
    filters = ['fps=1:start_time=0:round=up']
    if region is not None:
        left, top, right, bottom = region
        x, y = f'trunc(iw*{left:.6f})', f'trunc(ih*{top:.6f})'
        filters.append(f"crop=w='max(1,trunc(iw*{right:.6f})-{x})':"
                       f"h='max(1,trunc(ih*{bottom:.6f})-{y})':x={x}:y={y}:exact=1")
    filters.append('scale=1440:1440:force_original_aspect_ratio=decrease')
    run(ffmpeg_input(path) + ['-t', str(duration), '-an', '-map', '0:v:0',
        '-vf', ','.join(filters),
        '-start_number', '0', str(directory / 'frame-%03d.png')], 45)
    output = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                  str(ROOT / 'scripts/reference_ocr.ps1'), '-Directory', str(directory)], 90)
    return merge_screen(json.loads(output), duration)


def recognize(reader, size, region=None):
    if not 0 < size <= MAX_BYTES:
        raise ValueError('참고 영상은 최대 100MB까지 올릴 수 있어요.')
    if not RECOGNITION.acquire(blocking=False):
        raise ValueError('참고 영상을 읽고 있습니다. 완료 후 다시 시도하세요.')
    try:
        with tempfile.TemporaryDirectory(prefix='mflow-reference-') as temp:
            directory = Path(temp)
            path = directory / 'reference.video'
            with path.open('wb') as out:
                while size:
                    chunk = reader.read(min(size, 1024 * 1024))
                    if not chunk:
                        raise ValueError('참고 영상 업로드가 중단되었습니다.')
                    out.write(chunk)
                    size -= len(chunk)
            info = production.probe(path)
            duration = min(info['duration'], MAX_SECONDS)
            result = {'speech': [], 'screen': [], 'warnings': [], 'duration': duration}
            for key, label, engine, args in (
                ('speech', '음성 받아쓰기', speech, ()),
                ('screen', '화면 자막 인식', screen, (region,)),
            ):
                if key == 'speech' and not info['audio']:
                    result['warnings'].append('참고 영상에 음성 트랙이 없습니다.')
                    continue
                try:
                    result[key] = engine(path, directory, duration, *args)
                except subprocess.TimeoutExpired:
                    result['warnings'].append(f'{label} 시간이 초과됐습니다.')
                except (subprocess.CalledProcessError, OSError, ValueError) as error:
                    detail = str(error) if type(error) is ValueError else ''
                    result['warnings'].append(detail or f'{label}에 실패했습니다. FFmpeg와 인식 도구를 확인하세요.')
            return result
    finally:
        RECOGNITION.release()
