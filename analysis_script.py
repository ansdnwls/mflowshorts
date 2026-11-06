"""Reviewed scene evidence and grounded Korean script generation (Python 3.11).

The default providers are deterministic mocks, not computer vision/ASR/LLM claims.
Confirmation fingerprints detect stale edits; they are not authentication tokens.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

TEXT_FIELDS = ('description', 'dialogue')
LIST_FIELDS = ('subtitle_clues', 'id_clues', 'facts', 'inferences')
SECTIONS = ('hook', 'situation', 'ending')
FACT_PREFIXES = {'hook': '눈여겨볼 장면은', 'situation': '화면에서 확인된 내용은',
                 'ending': '마지막으로 짚을 사실은'}


def _text(value, label, limit=2000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{label}: {limit}자 이내의 문자열이 필요합니다.')
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValueError(f'{label}: 제어 문자를 사용할 수 없습니다.')
    return value.strip()


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f'{label}: 유한한 숫자가 필요합니다.')
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f'{label}: 유한한 숫자가 필요합니다.') from None
    if not math.isfinite(value):
        raise ValueError(f'{label}: 유한한 숫자가 필요합니다.')
    return value


def validate_analysis(data, duration=None):
    """Normalize user/provider JSON. Every edit needs fresh explicit confirmation."""
    if not isinstance(data, dict):
        raise ValueError('분석은 JSON 객체여야 합니다.')
    scenes = data.get('scenes')
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 200:
        raise ValueError('분석에는 1~200개의 장면이 필요합니다.')
    if duration is not None:
        duration = _number(duration, '원본 길이')
        if duration <= 0:
            raise ValueError('원본 길이는 0보다 커야 합니다.')
    normalized, seen, previous_start = [], set(), -1.0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError('각 장면은 객체여야 합니다.')
        ident = _text(scene.get('id', f'scene-{index + 1}'), '장면 ID', 80)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident) or ident in seen:
            raise ValueError('장면 ID는 고유한 영문·숫자·하이픈·밑줄이어야 합니다.')
        seen.add(ident)
        start = _number(scene.get('start'), '시작 시각')
        end = _number(scene.get('end'), '종료 시각')
        if start < 0 or end <= start or start < previous_start:
            raise ValueError('장면은 시작 시간순이며 0 ≤ 시작 < 종료여야 합니다.')
        if duration is not None and end > duration + 0.001:
            raise ValueError('장면 종료 시각이 원본 길이를 넘습니다.')
        previous_start = start
        item = {'id': ident, 'start': start, 'end': end}
        for field in TEXT_FIELDS:
            item[field] = _text(scene.get(field, ''), field)
        for field in LIST_FIELDS:
            values = scene.get(field, [])
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError(f'{field}: 최대 100개의 문자열 목록이 필요합니다.')
            item[field] = [_text(value, field) for value in values]
            if any(not value for value in item[field]):
                raise ValueError(f'{field}: 빈 항목은 제거하세요.')
        normalized.append(item)
    return {'version': 1, 'provider': _text(data.get('provider', 'manual'), '분석 제공자', 80),
            'scenes': normalized, 'confirmed': False}


def analysis_fingerprint(data):
    normalized = validate_analysis(data)
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def confirm_analysis(data, duration=None):
    normalized = validate_analysis(data, duration)
    if not any(scene['facts'] for scene in normalized['scenes']):
        raise ValueError('직접 확인한 사실을 하나 이상 입력한 뒤 분석을 확정하세요.')
    normalized['fingerprint'] = analysis_fingerprint(normalized)
    normalized['confirmed'] = True
    return normalized


def require_confirmed(analysis):
    normalized = validate_analysis(analysis)
    fingerprint = analysis_fingerprint(normalized)
    if analysis.get('confirmed') is not True or analysis.get('fingerprint') != fingerprint:
        raise ValueError('분석을 검토·확정한 뒤 대본을 생성하세요. 수정한 분석은 다시 확정해야 합니다.')
    if not any(scene['facts'] for scene in normalized['scenes']):
        raise ValueError('확인된 사실이 없는 분석으로 대본을 생성할 수 없습니다.')
    return normalized, fingerprint


class AnalysisProvider(Protocol):
    def analyze(self, source: dict) -> dict: ...


class ScriptProvider(Protocol):
    def generate(self, analysis: dict) -> dict: ...


class MockAnalysisProvider:
    """Make an editable empty worksheet, never pretend to have seen/heard media."""
    def analyze(self, source):
        if not isinstance(source, dict):
            raise ValueError('원본 정보가 필요합니다.')
        duration = _number(source.get('duration'), '원본 길이')
        return validate_analysis({'provider': 'mock', 'scenes': [
            {'id': 'scene-1', 'start': 0, 'end': duration,
             'description': '', 'dialogue': '', 'subtitle_clues': [], 'id_clues': [],
             'facts': [], 'inferences': []}]}, duration)


def _sentence_text(kind, evidence, section):
    # Quoting a conjecture in an explicitly unverified frame avoids converting it
    # into a narrator assertion. This conservative grammar is intentionally fixed.
    if kind == 'inference':
        return f'추정으로는 「{evidence}」라는 해석이 가능하지만 확인되지 않았습니다.'
    return f'{FACT_PREFIXES[section]} 「{evidence}」입니다.'


class MockScriptProvider:
    """A reproducible hook → situation → ending, grounded only in supplied facts."""
    def generate(self, analysis):
        normalized, fingerprint = require_confirmed(analysis)
        facts = [(scene['id'], fact) for scene in normalized['scenes'] for fact in scene['facts']]
        inferences = [(scene['id'], item) for scene in normalized['scenes'] for item in scene['inferences']]
        selections = [('hook', 'fact', facts[0])]
        selections.extend(('situation', 'fact', fact) for fact in (facts[1:] or facts[:1]))
        selections.append(('ending', 'inference' if inferences else 'fact',
                           inferences[-1] if inferences else facts[-1]))
        sentences = [{'id': f'sentence-{index + 1}', 'scene_id': evidence[0],
                      'section': section, 'kind': kind,
                      'text': _sentence_text(kind, evidence[1], section)}
                     for index, (section, kind, evidence) in enumerate(selections)]
        return {'version': 1, 'provider': 'mock', 'analysis_fingerprint': fingerprint,
                'sentences': sentences, 'text': '\n'.join(item['text'] for item in sentences)}


def validate_script(script, analysis):
    """Fail closed on stale evidence, invented facts, or assertive conjectures.

Currently accepts verbatim evidence in the documented sentence frames only.
External paraphrasing needs a separately reviewed grounding policy/provider.
"""
    normalized, fingerprint = require_confirmed(analysis)
    if not isinstance(script, dict) or script.get('analysis_fingerprint') != fingerprint:
        raise ValueError('대본의 분석 버전이 현재 확정된 분석과 일치하지 않습니다.')
    sentences = script.get('sentences')
    if not isinstance(sentences, list) or not 3 <= len(sentences) <= 500:
        raise ValueError('대본에는 훅·상황·마무리 문장이 필요합니다.')
    scenes = {scene['id']: scene for scene in normalized['scenes']}
    result, seen, section_index = [], set(), -1
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ValueError('대본 문장은 객체여야 합니다.')
        ident = _text(sentence.get('id', ''), '문장 ID', 80)
        section, kind = sentence.get('section'), sentence.get('kind')
        scene_id = sentence.get('scene_id')
        if not all(isinstance(value, str) for value in (section, kind, scene_id)):
            raise ValueError('문장 구조·종류·장면 ID는 문자열이어야 합니다.')
        scene = scenes.get(scene_id)
        if not ident or ident in seen or section not in SECTIONS or kind not in ('fact', 'inference') or scene is None:
            raise ValueError('문장 ID·구조·사실/추정 표시·장면 연결을 확인하세요.')
        if SECTIONS.index(section) < section_index:
            raise ValueError('대본은 훅 → 상황 → 마무리 순서여야 합니다.')
        section_index = SECTIONS.index(section)
        seen.add(ident)
        text = _text(sentence.get('text', ''), '대본 문장', 2200)
        evidence = scene['facts' if kind == 'fact' else 'inferences']
        allowed = {_sentence_text(kind, item, section) for item in evidence}
        if text not in allowed:
            raise ValueError('대본은 해당 장면의 확인된 사실·추정만 사용해야 하며 추정은 미확인 표현을 유지해야 합니다.')
        result.append({'id': ident, 'scene_id': scene['id'], 'section': section, 'kind': kind, 'text': text})
    if set(item['section'] for item in result) != set(SECTIONS):
        raise ValueError('훅·상황·마무리가 모두 필요합니다.')
    text = '\n'.join(item['text'] for item in result)
    if script.get('text', text) != text:
        raise ValueError('대본 본문과 문장 목록이 일치하지 않습니다.')
    return {'version': 1, 'provider': _text(script.get('provider', 'manual'), '대본 제공자', 80),
            'analysis_fingerprint': fingerprint, 'sentences': result, 'text': text}


def generate_script(analysis, provider=None):
    require_confirmed(analysis)
    return validate_script((provider or MockScriptProvider()).generate(analysis), analysis)
