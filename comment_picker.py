"""Turn a source video's public comments into comment-card candidates.

Nothing here calls the network: app.py fetches the comments, this module only
cleans, labels and ranks them so the user can pick up to three cards.
"""
import datetime as dt
import html
import re
import urllib.parse

CARD_LIMIT = 80  # same limit as production.comments()
HANGUL = re.compile(r'[가-힣ㄱ-ㆎ]')
LETTER = re.compile(r'[^\W\d_]')
LINK = re.compile(r'https?://|www\.|\.com\b', re.I)
TIMESTAMP = re.compile(r'\b\d{1,2}:\d{2}\b')
# Words that usually mean the comment reacts to a twist or asks what happened.
CURIOUS = ('?', '왜', '어떻게', '진짜', '실화', '대박', '미쳤', 'ㅋㅋ', 'ㄷㄷ', '헐', '설마', '반전', '마지막',
           'why', 'how', 'wait', 'what', 'plot twist', 'the end', 'lol', 'lmao', '为什么', '怎么', '真的')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}


def youtube_id(url):
    """Video id from watch, shorts and youtu.be links, else ''."""
    try: parts = urllib.parse.urlsplit(str(url or '').strip())
    except ValueError: return ''
    if parts.hostname not in YOUTUBE_HOSTS: return ''
    if parts.hostname == 'youtu.be': candidate = parts.path.strip('/').split('/')[0]
    elif parts.path.startswith(('/shorts/', '/embed/', '/live/')): candidate = parts.path.split('/')[2]
    else: candidate = urllib.parse.parse_qs(parts.query).get('v', [''])[0]
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate) else ''


def is_korean(text):
    letters = LETTER.findall(text)
    return bool(letters) and len(HANGUL.findall(text)) / len(letters) >= 0.3


def time_ago(published, now=None):
    """'15시간 전' style label like the YouTube app shows."""
    try: then = dt.datetime.fromisoformat(str(published).replace('Z', '+00:00'))
    except ValueError: return ''
    if then.tzinfo is None: then = then.replace(tzinfo=dt.timezone.utc)
    seconds = max(0, ((now or dt.datetime.now(dt.timezone.utc)) - then).total_seconds())
    for size, unit in ((31536000, '년'), (2592000, '개월'), (604800, '주'), (86400, '일'), (3600, '시간'), (60, '분')):
        if seconds >= size: return f'{int(seconds // size)}{unit} 전'
    return '방금 전'


def mask(name):
    """Hide most of a real nickname: '@coolguy123' -> '@co***'."""
    name = re.sub(r'\s+', '', str(name or '')).lstrip('@')
    return '@' + name[:2] + '***' if name else '@viewer***'


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(str(text or ''))).strip()


def score(text, likes):
    lowered = text.lower()
    value = min(likes, 100000) ** 0.5
    value += 15 * sum(word in lowered for word in CURIOUS)
    if 8 <= len(text) <= 60: value += 10
    if len(text) > CARD_LIMIT: value -= 25
    return round(value, 1)


def usable(text):
    """Skip links, timestamp lists, emoji-only and one-word comments."""
    if LINK.search(text) or len(TIMESTAMP.findall(text)) > 1: return False
    return len(LETTER.findall(text)) >= 3 and 5 <= len(text) <= 200


def from_youtube(items, now=None):
    """commentThreads items -> ranked candidates and a short summary."""
    seen, candidates = set(), []
    for item in items:
        snippet = ((item.get('snippet') or {}).get('topLevelComment') or {}).get('snippet') or {}
        text = clean(snippet.get('textDisplay') or snippet.get('textOriginal'))
        if not text or text in seen or not usable(text): continue
        seen.add(text)
        likes = int(snippet.get('likeCount') or 0)
        korean = is_korean(text)
        candidates.append({'text': text, 'name': mask(snippet.get('authorDisplayName')), 'when': time_ago(snippet.get('publishedAt'), now),
                           'likes': likes, 'replies': int((item.get('snippet') or {}).get('totalReplyCount') or 0),
                           'korean': korean, 'fits': len(text) <= CARD_LIMIT, 'score': score(text, likes)})
    # Most liked first; the curiosity score only breaks ties.
    candidates.sort(key=lambda c: (c['likes'], c['score']), reverse=True)
    korean = sum(c['korean'] for c in candidates)
    # "Went viral in Korea": enough Korean voices among the top comments to use them as they are.
    viral_kr = korean >= 5 or (candidates and korean / len(candidates) >= 0.3)
    return {'comments': candidates[:60], 'total': len(candidates), 'korean': korean, 'viral_kr': bool(viral_kr)}
