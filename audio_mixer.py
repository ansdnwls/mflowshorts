"""Project audio clips and the shared preview/export mix."""
import json
import math
import re
import subprocess
import uuid


TRACKS = ('narration', 'music')
FORMATS = 'wav,mp3,flac,ogg,aac,mov,mp4,m4a,3gp,3g2,mj2,matroska,webm'
COMMAND = ['ffmpeg', '-hide_banner', '-nostdin', '-y']


def number(value, minimum, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = math.nan
    if not minimum <= result <= maximum:
        raise ValueError('오디오 볼륨이나 재생 구간을 확인하세요.')
    return round(result, 3)


def input_args(path, formats=FORMATS):
    return ['-format_whitelist', formats, '-protocol_whitelist', 'file', '-i', str(path)]


def upload(directory, reader, size):
    if not 0 < size <= 100 * 1024 * 1024:
        raise ValueError('오디오 파일은 최대 100MB입니다.')
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = directory / (token + '.audio')
    try:
        with path.open('xb') as out:
            remaining = size
            while remaining:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('오디오 업로드가 중단되었습니다.')
                out.write(chunk)
                remaining -= len(chunk)
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-format_whitelist', FORMATS, '-protocol_whitelist', 'file',
             '-select_streams', 'a:0', '-show_entries', 'stream=duration:format=duration',
             '-of', 'json', str(path)],
            capture_output=True, check=True, timeout=20,
        )
        info = json.loads(result.stdout)
        stream = info['streams'][0]
        length = stream.get('duration')
        if length in (None, 'N/A'):
            length = info['format']['duration']
        duration = number(length, 0.001, 5400)
        (directory / (token + '.json')).write_text(json.dumps(duration), encoding='utf-8')
        return {'id': token, 'duration': duration}
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        path.unlink(missing_ok=True)
        (directory / (token + '.json')).unlink(missing_ok=True)
        raise ValueError('유효한 오디오 파일을 사용하세요. 최대 100MB·90분입니다.') from None


def settings(value, directory):
    if not isinstance(value, dict):
        raise ValueError('오디오 믹서 설정을 확인하세요.')
    result = {'source_volume': number(value.get('source_volume', 100), 0, 100)}
    duck = value.get('duck', {})
    if not isinstance(duck, dict) or type(duck.get('enabled', False)) is not bool:
        raise ValueError('자동 더킹 설정을 확인하세요.')
    result['duck'] = {'enabled': duck.get('enabled', False),
                      'reduction': number(duck.get('reduction', 75), 0, 100)}
    for key in TRACKS:
        track = value.get(key, {})
        if not isinstance(track, dict) or type(track.get('muted', False)) is not bool:
            raise ValueError('오디오 트랙 설정을 확인하세요.')
        normalized = {'volume': number(track.get('volume', 100 if key == 'narration' else 35), 0, 100),
                      'muted': track.get('muted', False)}
        clip = track.get('clip')
        if clip is not None:
            token = clip.get('id') if isinstance(clip, dict) else None
            if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
                raise ValueError('오디오 파일을 다시 선택하세요.')
            try:
                duration = json.loads((directory / (token + '.json')).read_text(encoding='utf-8'))
                if not (directory / (token + '.audio')).is_file():
                    raise OSError
            except (OSError, ValueError):
                raise ValueError('오디오 파일을 찾을 수 없습니다.') from None
            start = number(clip.get('in', 0), 0, duration)
            end = number(clip.get('out', duration), 0, duration)
            if end <= start:
                raise ValueError('오디오의 자르기 끝은 시작보다 뒤여야 합니다.')
            normalized['clip'] = {'id': token, 'duration': duration, 'in': start, 'out': end,
                                  'at': number(clip.get('at', 0), 0, 60),
                                  'name': str(clip.get('name', ''))[:160]}
        result[key] = normalized
    return result


def placed_audio(clip, duration):
    return (f"atrim=start={clip['in']}:end={clip['out']},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,adelay={round(clip['at'] * 1000)}:all=1,"
            f"asetpts=N/SR/TB,atrim=duration={duration}")


def speech_envelope(args, chain, duration, reduction):
    result = subprocess.run(
        [*COMMAND, '-loglevel', 'info', *args, '-map', '0:a:0',
         '-af', chain + ',silencedetect=noise=-40dB:d=0.18', '-f', 'null', '-'],
        capture_output=True, check=True, timeout=90,
    )
    spans = []
    start = 0.0
    for event, time in re.findall(rb'silence_(start|end): (-?[\d.]+)', result.stderr):
        time = min(duration, max(0, float(time)))
        if event == b'start' and start < time:
            spans.append((start, time))
        start = time if event == b'end' else duration
    if start < duration:
        spans.append((start, duration))
    if not spans:
        return '1'
    ramps = [f'max(0,min(1,min((t-{a:.6f}+0.08)/0.08,({b:.6f}+0.35-t)/0.35)))'
             for a, b in spans]
    return f"1-{reduction / 100}*min(1,{'+'.join(ramps)})"


def render(output, source, source_formats, cuts, duration, mix, original, directory, generated=None):
    """Write the same PCM mix for both the preview and the final picture."""
    inputs = []
    filters = []
    labels = []

    def add_input(args):
        index = inputs.count('-i')
        inputs.extend(args)
        return f'[{index}:a:0]'

    if original and mix['source_volume']:
        label = add_input(input_args(source, source_formats))
        branches = ''.join(f'[orig{i}]' for i in range(len(cuts)))
        filters.append(f'{label}aresample=48000:async=1:first_pts=0,'
                       f'aformat=channel_layouts=stereo,apad,asplit={len(cuts)}{branches}')
        for i, (start, end, span) in enumerate(cuts):
            filters.append(f'[orig{i}]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,'
                           f'apad,atrim=duration={span:.6f}[origcut{i}]')
        parts = ''.join(f'[origcut{i}]' for i in range(len(cuts)))
        filters.append(f"{parts}concat=n={len(cuts)}:v=0:a=1,volume={mix['source_volume'] / 100}[original]")
        labels.append('[original]')

    duck = mix['duck']
    envelope = '1'
    for key in TRACKS:
        track = mix[key]
        clip = track.get('clip')
        if not clip and key == 'narration' and generated:
            path, length = generated
            clip = {'in': 0, 'out': length, 'at': 0}
        elif clip:
            path = directory / (clip['id'] + '.audio')
        if not clip or track['muted'] or not track['volume'] or clip['at'] >= duration:
            continue
        args = input_args(path)
        chain = placed_audio(clip, duration) + f",volume={track['volume'] / 100}"
        if key == 'narration' and duck['enabled'] and duck['reduction'] and mix['music'].get('clip') and not mix['music']['muted'] and mix['music']['volume']:
            # Include trailing silence so detection can locate the final release point.
            detection = chain + f',apad=whole_dur={duration + 0.2:.6f}'
            envelope = speech_envelope(args, detection, duration, duck['reduction'])
        if key == 'music' and envelope != '1':
            chain += f",asetnsamples=n=480:p=0,volume='{envelope}':eval=frame"
        label = f'[{key}]'
        filters.append(f'{add_input(args)}{chain}{label}')
        labels.append(label)

    filters.append(f'anullsrc=r=48000:cl=stereo:d={duration}[silence]')
    labels.append('[silence]')
    filters.append(''.join(labels) + f'amix=inputs={len(labels)}:duration=longest:normalize=0,'
                   'alimiter=limit=0.95:level=0:latency=1[mix]')
    # A long speech envelope plus forty cuts can exceed Windows' command-line limit.
    graph = output.with_suffix('.ffgraph')
    graph.write_text(';'.join(filters), encoding='ascii')
    try:
        command = [*COMMAND, '-loglevel', 'error', *inputs, '-filter_complex_threads', '2']
        output_args = ['-map', '[mix]', '-t', str(duration), '-c:a', 'pcm_f32le', str(output)]
        try:
            subprocess.run([*command, '-/filter_complex', str(graph), *output_args],
                           capture_output=True, check=True, timeout=120)
        except subprocess.CalledProcessError as error:
            if b"Unrecognized option '/filter_complex'" not in error.stderr:
                raise
            subprocess.run([*command, '-filter_complex_script', str(graph), *output_args],
                           capture_output=True, check=True, timeout=120)
    finally:
        graph.unlink(missing_ok=True)
