"""Translate editable subtitle lines through a server-configured LibreTranslate instance."""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def translate(data):
    lines = data.get('lines')
    source, target = data.get('source'), data.get('target')
    if not isinstance(lines, list) or not 1 <= len(lines) <= 40 or any(
        not isinstance(line, str) or not line.strip() or len(line) > 60 for line in lines
    ):
        raise ValueError('번역할 자막은 1~40줄, 한 줄은 60자 이내여야 합니다.')
    for code in (source, target):
        if not isinstance(code, str) or not re.fullmatch(r'auto|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?', code):
            raise ValueError('자막 언어 코드를 확인하세요.')
    if target == 'auto' or source == target:
        raise ValueError('원본 언어와 다른 번역 언어를 선택하세요.')
    base_url = os.environ.get('MFLOW_TRANSLATE_URL', '').strip()
    if not base_url:
        raise ValueError('서버에 자막 번역 서비스가 연결되지 않았어요.')
    payload = {'q': lines, 'source': source, 'target': target, 'format': 'text'}
    if key := os.environ.get('MFLOW_TRANSLATE_API_KEY'):
        payload['api_key'] = key
    request = Request(base_url.rstrip('/') + '/translate',
                      data=json.dumps(payload).encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=45) as response:
            result = json.loads(response.read(128 * 1024))
    except HTTPError as exc:
        if exc.code == 400:
            raise ValueError('번역 서버에서 이 언어 조합이나 자막을 처리할 수 없어요.') from None
        raise ValueError('번역 서버가 요청을 처리하지 못했어요.') from None
    except (URLError, OSError, ValueError):
        raise ValueError('번역 서버에 연결하거나 응답을 읽지 못했어요.') from None
    translated = result.get('translatedText') if isinstance(result, dict) else None
    if not isinstance(translated, list) or len(translated) != len(lines) or any(
        not isinstance(line, str) or not line.strip() for line in translated
    ):
        raise ValueError('번역 서버가 자막 줄 수에 맞는 결과를 반환하지 않았어요.')
    return [' '.join(line.split()) for line in translated]
