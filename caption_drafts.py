"""Create editable caption drafts from material supplied by the editor."""

import re
from urllib.parse import urlsplit


def source_url(value, required=False):
    url = str(value or '').strip()
    if not url and not required:
        return ''
    try:
        parts = urlsplit(url)
        valid = parts.scheme in ('https', 'http') and parts.hostname and not parts.username and not parts.password
    except ValueError:
        valid = False
    if not valid or len(url) > 500 or any(c.isspace() for c in url):
        raise ValueError('출처는 올바른 HTTP 또는 HTTPS 링크로 입력하세요.')
    return url


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def shorten(text, limit=55):
    text = text.replace('*', '')
    if len(text) <= limit:
        return text
    cut = text.rfind(' ', 0, limit - 1)
    return text[:cut if cut > limit * .7 else limit - 1].rstrip() + '…'


def validate_evidence(value):
    if not isinstance(value, dict) or value.get('kind') not in ('reference', 'scene', 'review'):
        raise ValueError('자막 근거 유형을 확인하세요.')
    note = clean(value.get('note'))
    if not note or len(note) > 1000:
        raise ValueError('자막 근거는 1~1000자로 입력하세요.')
    kind = value['kind']
    filename = clean(value.get('filename')) if kind == 'reference' else ''
    if len(filename) > 180:
        raise ValueError('참고 영상 파일 이름은 180자 이내로 입력하세요.')
    evidence = {'kind': kind, 'note': note, 'url': source_url(value.get('url'), kind == 'review' or (kind == 'reference' and not filename))}
    if filename:
        evidence['filename'] = filename
    return evidence


def build(data):
    product = clean(data.get('product')).replace('*', '')
    reference = clean(data.get('reference'))
    scene = clean(data.get('scene'))
    if not product or len(product) > 30:
        raise ValueError('제품 이름을 1~30자로 입력하세요.')
    if not reference or len(reference) > 1000 or not scene or len(scene) > 60:
        raise ValueError('참고 자막과 원본 컷에서 보이는 동작(60자 이내)을 입력하세요.')

    topics = (
        ('한 손', ('한 손', '한손')),
        ('세척', ('세척', '씻기', '설거지')),
        ('설치', ('설치', '조립')),
        ('휴대', ('휴대', '가방')),
        ('보관', ('보관', '수납')),
    )
    topic = next((name for name, words in topics if any(word in reference for word in words)), '')
    question = {
        '한 손': '한 손으로 쓰면 어떨까요?',
        '세척': '어떻게 씻을까요?',
        '설치': '어떻게 설치할까요?',
        '휴대': '들고 다니기 어떨까요?',
        '보관': '어떻게 보관할까요?',
    }.get(topic, '실제 사용 장면은 어떨까요?')

    def line(text, kind, note, url, sub='', filename=''):
        return {'text': text, 'sub': sub, 'evidence': validate_evidence(
            {'kind': kind, 'note': note, 'url': url, 'filename': filename})}

    drafts = [
        line(f'{product}, {question}', 'reference', reference, data.get('reference_url'),
             filename=data.get('reference_filename')),
        line(scene, 'scene', scene, data.get('scene_url')),
    ]
    reviews = list(dict.fromkeys(clean(row) for row in str(data.get('reviews') or '').splitlines() if clean(row)))
    if reviews:
        if data.get('same_product') is not True:
            raise ValueError('후기가 같은 모델 또는 옵션의 것인지 확인하세요.')
        if len(reviews) > 12 or any(len(review) > 400 for review in reviews):
            raise ValueError('후기는 줄마다 하나씩, 최대 12줄과 줄당 400자까지 입력하세요.')
        concern = re.compile(r'불편|아쉽|어렵|번거|단점|무겁|실망|안 돼|안되')
        positive = next((review for review in reviews if not concern.search(review)), '')
        negative = next((review for review in reviews if concern.search(review)), '')
        selected = [positive, negative] if positive and negative else reviews[:2]
        for review in selected:
            drafts.append(line(shorten(review), 'review', review, data.get('review_url'), '구매 후기 발췌'))
    return drafts
