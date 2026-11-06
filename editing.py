"""Pure sentence-aligned scene planning and FFmpeg filter graph construction."""
from __future__ import annotations

import math

import analysis_script
import narration as narration_module

TOLERANCE = 0.0001
MAX_DURATION = 60.0


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{field}: 유한한 숫자가 필요합니다.')
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f'{field}: 유한한 숫자가 필요합니다.') from None
    if not math.isfinite(value):
        raise ValueError(f'{field}: 유한한 숫자가 필요합니다.')
    return value


def _segments(script, narration):
    if not isinstance(script, dict) or not isinstance(narration, dict):
        raise ValueError('대본과 나레이션 정보가 필요합니다.')
    sentences, segments = script.get('sentences'), narration.get('segments')
    if not isinstance(sentences, list) or not isinstance(segments, list) or not 1 <= len(sentences) <= 200 or len(sentences) != len(segments):
        raise ValueError('대본과 나레이션 문장 수가 일치해야 합니다.')
    if ('script_fingerprint' in narration and
            narration['script_fingerprint'] != narration_module.script_fingerprint(script)):
        raise ValueError('나레이션이 현재 대본과 일치하지 않습니다.')
    result, seen, expected_start = [], set(), 0.0
    for sentence, segment in zip(sentences, segments):
        if not isinstance(sentence, dict) or not isinstance(segment, dict):
            raise ValueError('문장과 나레이션 구간은 객체여야 합니다.')
        ident, scene_id, text = (sentence.get(key) for key in ('id', 'scene_id', 'text'))
        if not all(isinstance(value, str) and value.strip() for value in (ident, scene_id, text)) or ident in seen:
            raise ValueError('문장 ID·장면·본문이 필요하며 문장 ID는 고유해야 합니다.')
        seen.add(ident)
        if (segment.get('sentence_id'), segment.get('scene_id'), segment.get('text')) != (ident, scene_id, text):
            raise ValueError('나레이션의 문장·장면·본문이 승인 대본과 일치하지 않습니다.')
        start, end, duration = (_number(segment.get(key), key) for key in ('start', 'end', 'duration'))
        if start < 0 or end <= start or duration <= 0 or abs(start - expected_start) > TOLERANCE or abs(end - start - duration) > TOLERANCE:
            raise ValueError('나레이션은 0초부터 빈틈·겹침 없이 이어져야 하며 문장 길이가 일치해야 합니다.')
        if end > MAX_DURATION + TOLERANCE:
            raise ValueError('음성 맞춤 영상은 최대 60초입니다. 대본을 줄여 다시 음성을 생성하세요.')
        result.append({'sentence_id': ident, 'scene_id': scene_id, 'text': text,
                       'start': start, 'end': end, 'duration': end - start})
        expected_start = end
    if abs(_number(narration.get('duration'), '전체 음성 길이') - expected_start) > TOLERANCE:
        raise ValueError('전체 음성 길이와 마지막 문장 종료 시간이 일치하지 않습니다.')
    return result


def build_plan(analysis, script, narration, source_duration):
    """Map each measured sentence to its reviewed scene, freezing short scenes.

    Source media is never stretched/slowed; a short scene holds its last frame
    while its original audio is padded with silence to the sentence boundary.
    """
    source_duration = _number(source_duration, '원본 길이')
    analysis_script.validate_analysis(analysis, source_duration)
    analysis_script.validate_script(script, analysis)
    segments = _segments(script, narration)
    scenes = {scene['id']: scene for scene in analysis['scenes']}
    result = []
    for segment in segments:
        scene = scenes.get(segment['scene_id'])
        if scene is None:
            raise ValueError('나레이션에 연결된 분석 장면이 없습니다.')
        source_start = float(scene['start'])
        source_end = min(float(scene['end']), source_start + segment['duration'])
        result.append({key: segment[key] for key in ('sentence_id', 'scene_id', 'start', 'end', 'duration')} |
                      {'source_start': source_start, 'source_end': source_end})
    return result


def caption_events(script, narration):
    """Return measured sentence windows; renderer can chunk text within each."""
    return [{'text': segment['text'], 'start': segment['start'], 'end': segment['end']}
            for segment in _segments(script, narration)]


def _fmt(value):
    return f'{value:.6f}'


def ffmpeg_filters(plan, has_source_audio=True, mute=False, duck_volume=0.18):
    """Input 0 = full source, input 1 = narration WAV; return labeled graph.

    Match source clips, hold their last frames as needed, duck only the original
    audio during spoken windows, and mix narration with a non-boosting limiter.
    A muted/absent original audio track still retains narration.
    """
    if not isinstance(plan, list) or not 1 <= len(plan) <= 200:
        raise ValueError('1~200개의 편집 구간이 필요합니다.')
    duck_volume = _number(duck_volume, '덕킹 볼륨')
    if not 0 <= duck_volume <= 1:
        raise ValueError('덕킹 볼륨은 0~1 사이여야 합니다.')
    normalized, expected_start = [], 0.0
    for cut in plan:
        if not isinstance(cut, dict):
            raise ValueError('편집 구간은 객체여야 합니다.')
        values = {key: _number(cut.get(key), key) for key in ('start', 'end', 'duration', 'source_start', 'source_end')}
        start, end, duration, source_start, source_end = (values[key] for key in ('start', 'end', 'duration', 'source_start', 'source_end'))
        if (start < 0 or end <= start or duration <= 0 or source_start < 0 or source_end <= source_start or
                abs(start - expected_start) > TOLERANCE or abs(end - start - duration) > TOLERANCE or
                source_end - source_start > duration + TOLERANCE or end > MAX_DURATION + TOLERANCE):
            raise ValueError('편집 구간의 시각·길이가 올바르지 않습니다.')
        normalized.append(values)
        expected_start = end
    count, total = len(normalized), expected_start
    with_original = bool(has_source_audio and not mute)
    filters = ['[0:v]split=' + str(count) + ''.join(f'[srcv{i}]' for i in range(count))]
    if with_original:
        filters.append('[0:a]asplit=' + str(count) + ''.join(f'[srca{i}]' for i in range(count)))
    for index, cut in enumerate(normalized):
        start, end, duration = _fmt(cut['source_start']), _fmt(cut['source_end']), _fmt(cut['duration'])
        hold = _fmt(max(0, cut['duration'] - (cut['source_end'] - cut['source_start'])))
        filters.append(f'[srcv{index}]trim=start={start}:end={end},setpts=PTS-STARTPTS,'
                       f'fps=30,tpad=stop_mode=clone:stop_duration={hold},trim=duration={duration},'
                       f'settb=AVTB[v{index}]')
        if with_original:
            filters.append(f'[srca{index}]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,'
                           f'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                           f'apad=whole_dur={duration},atrim=duration={duration}[a{index}]')
    filters.append(''.join(f'[v{i}]' for i in range(count)) + f'concat=n={count}:v=1:a=0[matched_video]')
    filters.append(f'[1:a]asetpts=PTS-STARTPTS,aresample=48000,'
                   f'aformat=sample_fmts=fltp:channel_layouts=stereo,'
                   f'apad=whole_dur={_fmt(total)},atrim=duration={_fmt(total)}[narration_audio]')
    if with_original:
        filters.append(''.join(f'[a{i}]' for i in range(count)) + f'concat=n={count}:v=0:a=1[original_matched]')
        active = '+'.join(f'between(t,{_fmt(cut["start"])},{_fmt(cut["end"])})' for cut in normalized)
        filters.append(f"[original_matched]volume={_fmt(duck_volume)}:enable='{active}'[ducked_audio]")
        filters.append('[ducked_audio][narration_audio]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,'
                       'alimiter=limit=0.95:level=0:latency=1[mixed_audio]')
    else:
        filters.append('[narration_audio]alimiter=limit=0.95:level=0:latency=1[mixed_audio]')
    return {'filters': filters, 'video_label': 'matched_video', 'audio_label': 'mixed_audio'}
