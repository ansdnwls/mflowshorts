"""Deterministic QA checks; visual semantics still require a human review.

Caption overflow is a conservative character/line heuristic, not pixel OCR.
Scene matching checks evidence IDs and optional source time ranges, not vision.
Audio clipping is measured from PCM sample peaks without optional packages.
"""
from __future__ import annotations

import math
from pathlib import Path
import wave


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def audio_stats(path) -> dict:
    """Scan 8/16/24/32-bit PCM WAV samples for full-scale clipping."""
    count, clipped, peak = 0, 0, 0.0
    with wave.open(str(path), 'rb') as audio:
        width, channels, rate = audio.getsampwidth(), audio.getnchannels(), audio.getframerate()
        frames = audio.getnframes()
        if width not in (1, 2, 3, 4) or audio.getcomptype() != 'NONE' or not frames:
            raise ValueError('QA 오디오는 비어 있지 않은 PCM WAV여야 합니다.')
        maximum = (1 << (width * 8 - 1)) - 1
        minimum = -(1 << (width * 8 - 1))
        while True:
            data = audio.readframes(8192)
            if not data:
                break
            if len(data) % (width * channels):
                raise ValueError('PCM 오디오 파일이 잘렸습니다.')
            for offset in range(0, len(data), width):
                sample = data[offset] - 128 if width == 1 else int.from_bytes(data[offset:offset + width], 'little', signed=True)
                peak = max(peak, abs(sample) / (maximum if sample >= 0 else -minimum))
                clipped += sample in (minimum, maximum)
                count += 1
        if count != frames * channels:
            raise ValueError('PCM 오디오 파일이 잘렸습니다.')
    return {'samples': count, 'clipped_samples': clipped, 'peak': round(peak, 6),
            'duration': frames / rate, 'channels': channels, 'sample_rate': rate}


def check_report(analysis, script, narration, audio_path=None,
                 max_chars_per_line=24, max_lines=2) -> dict:
    """Return a JSON-serializable report, without claiming semantic certainty.

    Optional ``narration.captions`` can contain actual rendered caption chunks
    ({text,start,end,sentence_id}); otherwise each sentence is checked as a cue.
    Optional segment source_start/source_end checks the chosen source excerpt.
    ``audio_path`` should be the final mixed audio extracted as PCM WAV when
    validating a render; passing narration audio checks only that voice track.
    """
    if not isinstance(max_chars_per_line, int) or max_chars_per_line < 1 or not isinstance(max_lines, int) or max_lines < 1:
        raise ValueError('자막 글자 수와 줄 수 제한은 양의 정수여야 합니다.')
    issues, checks = [], []

    def issue(code, message, sentence_id=None, severity='error'):
        value = {'code': code, 'severity': severity, 'message': message}
        if sentence_id is not None:
            value['sentence_id'] = sentence_id
        issues.append(value)

    if not all(isinstance(value, dict) for value in (analysis, script, narration)):
        raise ValueError('분석, 대본, 음성 데이터는 JSON 객체여야 합니다.')
    scenes = analysis.get('scenes', [])
    sentences = script.get('sentences', [])
    segments = narration.get('segments', [])
    if not all(isinstance(items, list) and all(isinstance(item, dict) for item in items)
               for items in (scenes, sentences, segments)):
        raise ValueError('장면, 문장, 음성 구간은 JSON 객체 배열이어야 합니다.')
    scene_map = {s.get('id'): s for s in scenes if isinstance(s.get('id'), str)}
    sentence_map = {s.get('id'): s for s in sentences if isinstance(s.get('id'), str)}
    if not sentences or len(sentence_map) != len(sentences):
        issue('script_invalid', '대본 문장이 없거나 문장 ID가 중복·누락되었습니다.')
    if not scenes or len(scene_map) != len(scenes):
        issue('analysis_invalid', '장면이 없거나 장면 ID가 중복·누락되었습니다.')
    if analysis.get('confirmed') is not True:
        issue('analysis_unconfirmed', '분석이 확정되지 않았습니다.')
    if script.get('approved') is not True:
        issue('script_unapproved', '대본이 승인되지 않았습니다.')
    duration = narration.get('duration')
    if not _number(duration) or duration <= 0:
        issue('duration_invalid', '나레이션 전체 길이가 올바르지 않습니다.')
        duration = 0
    if not segments:
        issue('segments_missing', '문장별 음성 구간이 없습니다.')

    before = len(issues)
    previous_end, seen = 0, set()
    for segment in segments:
        ident = segment.get('sentence_id')
        if not isinstance(ident, str):
            issue('sentence_missing', '음성 구간의 문장 ID가 누락되었습니다.')
            continue
        if ident in seen:
            issue('sentence_duplicate', '동일한 문장의 음성 구간이 중복되었습니다.', ident)
        seen.add(ident)
        start, end = segment.get('start'), segment.get('end')
        if not _number(start) or not _number(end) or start < 0 or end <= start or end > duration + 0.01:
            issue('caption_timing', '자막/음성 구간이 전체 길이를 벗어나거나 유효하지 않습니다.', ident)
        else:
            if start < previous_end - 0.001:
                issue('caption_overlap', '자막/음성 구간이 이전 문장과 겹칩니다.', ident)
            previous_end = max(previous_end, end)
            recorded = segment.get('duration')
            if recorded is not None and (not _number(recorded) or abs(recorded - (end - start)) > 0.01):
                issue('duration_mismatch', '문장 길이 기록과 시작·종료 시간이 다릅니다.', ident)
    checks.append({'name': 'timing', 'status': 'failed' if len(issues) > before or not segments else 'passed'})

    before = len(issues)
    cues = narration.get('captions', segments)
    if not isinstance(cues, list) or not all(isinstance(c, dict) for c in cues):
        raise ValueError('자막은 JSON 객체 배열이어야 합니다.')
    for cue in cues:
        text = cue.get('text', '')
        if not isinstance(text, str) or not text.strip():
            issue('caption_empty', '자막 텍스트가 비어 있습니다.', cue.get('sentence_id'))
            continue
        lines = text.replace('*', '').splitlines()
        line_count = sum(max(1, math.ceil(len(line) / max_chars_per_line)) for line in lines)
        if line_count > max_lines:
            issue('caption_overflow', f'자막이 권장 {max_lines}줄을 초과합니다. 실제 화면에서 잘림을 확인하세요.', cue.get('sentence_id'))
        start, end = cue.get('start'), cue.get('end')
        if not _number(start) or not _number(end) or start < 0 or end <= start or end > duration + 0.01:
            issue('caption_timing', '자막 표시 시간이 유효하지 않습니다.', cue.get('sentence_id'))
    checks.append({'name': 'caption_bounds_heuristic', 'status': 'failed' if len(issues) > before else 'passed'})

    before = len(issues)
    for sentence in sentences:
        if sentence.get('scene_id') not in scene_map:
            issue('scene_missing', '대본 문장에 연결된 분석 장면이 없습니다.', sentence.get('id'))
        if sentence.get('id') not in seen:
            issue('narration_missing', '대본 문장의 음성이 없습니다.', sentence.get('id'))
    for segment in segments:
        ident = segment.get('sentence_id')
        if not isinstance(ident, str):
            continue
        sentence = sentence_map.get(ident)
        if sentence is None:
            issue('sentence_missing', '음성에 대응하는 대본 문장이 없습니다.', ident)
            continue
        if segment.get('text') != sentence.get('text'):
            issue('script_audio_mismatch', '음성 텍스트가 승인 대본과 다릅니다.', ident)
        scene_id = segment.get('scene_id')
        if scene_id != sentence.get('scene_id') or scene_id not in scene_map:
            issue('scene_mismatch', '음성 구간과 대본의 장면 연결이 다릅니다.', ident)
        scene = scene_map.get(scene_id, {})
        if 'source_start' in segment or 'source_end' in segment:
            start, end = segment.get('source_start'), segment.get('source_end')
            if not all(_number(v) for v in (start, end, scene.get('start'), scene.get('end'))) or start < scene['start'] - 0.01 or end > scene['end'] + 0.01 or start >= end:
                issue('source_range_mismatch', '매칭한 원본 구간이 분석 장면 범위를 벗어납니다.', ident)
    checks.append({'name': 'scene_evidence_mapping', 'status': 'failed' if len(issues) > before else 'passed'})

    if narration.get('mock'):
        issue('mock_audio', '모의 음성은 무음입니다. 실제 한국어 나레이션을 생성하고 검수하세요.', severity='warning')
    if audio_path is None:
        issue('audio_not_checked', 'PCM WAV를 지정하지 않아 오디오 클리핑을 검사하지 않았습니다.', severity='warning')
        checks.append({'name': 'audio_clipping', 'status': 'not_run'})
    else:
        try:
            stats = audio_stats(Path(audio_path))
            if stats['clipped_samples']:
                issue('audio_clipping', f"최대 진폭에 도달한 샘플 {stats['clipped_samples']}개를 발견했습니다.")
            if abs(stats['duration'] - duration) > 0.05:
                issue('audio_duration_mismatch', '오디오 실측 길이가 나레이션 기록과 다릅니다.')
            checks.append({'name': 'audio_clipping', 'status': 'failed' if stats['clipped_samples'] else 'passed', 'metrics': stats})
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            issue('audio_invalid', '오디오를 검사할 수 없습니다: ' + str(exc))
            checks.append({'name': 'audio_clipping', 'status': 'failed'})
    return {'passed': not any(i['severity'] == 'error' for i in issues),
            'needs_review': True, 'checks': checks, 'issues': issues,
            'limitations': ['자막 잘림은 글자 수·줄 수 기반 추정입니다. 최종 렌더 화면을 확인하세요.',
                            '화면과 대본의 의미 일치는 자동 판정하지 않습니다. 장면 ID와 시간 연결만 검사합니다.',
                            '오디오 클리핑 검사는 전달된 PCM WAV에만 적용됩니다. 최종 믹스도 별도로 검사하세요.']}
