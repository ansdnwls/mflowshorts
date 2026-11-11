"""Compare seven-day shopping shorts outcomes against each channel's own history."""

import datetime as dt
from itertools import islice
from statistics import median
from urllib.parse import urlsplit

import caption_drafts
import production
import shorts_bundle


HOSTS = {
    'youtube': {'youtube.com','www.youtube.com','m.youtube.com','youtu.be'},
    'instagram': {'instagram.com','www.instagram.com'},
    'tiktok': {'tiktok.com','www.tiktok.com','vm.tiktok.com','vt.tiktok.com'},
}
BASELINE_MIN = 3
BASELINE_MAX = 10
ANGLE_MIN = 2


def init():
    with production.CONNECT() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS shorts_performance (
            project_id TEXT PRIMARY KEY, platform TEXT NOT NULL, url TEXT NOT NULL,
            posted_on TEXT NOT NULL, views INTEGER NOT NULL, clicks INTEGER)""")


def count(value, label, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or not 0<=value<=1_000_000_000:
        raise ValueError(f'{label}는 0~10억 사이의 정수로 입력하세요.')
    return value


@production.serialized
def save(data):
    project=production.project(str(data.get('id','')))
    if not project['document'].get('shorts_bundle'):
        raise ValueError('쇼츠 묶음에서 만든 영상만 성과를 기록할 수 있습니다.')
    platform=data.get('platform')
    url=caption_drafts.source_url(data.get('url'),required=True)
    parts=urlsplit(url)
    if (not isinstance(platform,str) or platform not in HOSTS or parts.scheme!='https' or
            parts.hostname not in HOSTS[platform] or parts.netloc.lower()!=parts.hostname or
            (parts.path in ('','/') and not parts.query)):
        raise ValueError('선택한 플랫폼의 게시물 HTTPS 링크를 입력하세요.')
    try:
        posted=dt.date.fromisoformat(data.get('posted_on',''))
    except (ValueError,TypeError):
        raise ValueError('게시 날짜를 확인하세요.') from None
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    if (today-posted).days<7:
        raise ValueError('게시일로부터 7일이 지난 뒤 첫 7일 성과를 기록하세요.')
    views=count(data.get('views'),'조회수')
    clicks=count(data.get('clicks'),'클릭 수',optional=True)
    with production.CONNECT() as con:
        con.execute("""INSERT INTO shorts_performance VALUES (?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET platform=excluded.platform,url=excluded.url,
            posted_on=excluded.posted_on,views=excluded.views,clicks=excluded.clicks""",
                    (project['id'],platform,url,posted.isoformat(),views,clicks))
    return {'saved':True}


def snapshot():
    with production.CONNECT() as con:
        rows=con.execute("""SELECT s.*,p.channel_id,
            json_extract(p.document,'$.shorts_bundle.angle') AS angle,
            json_extract(p.document,'$.shorts_bundle.id') AS bundle_id
            FROM shorts_performance s JOIN production_projects p ON p.id=s.project_id
            ORDER BY s.posted_on,s.project_id""").fetchall()
    records={}
    histories={}
    angles={key:[] for key in shorts_bundle.ANGLES}
    for row in rows:
        record=dict(row)
        history=histories.setdefault((row['channel_id'],row['platform']),[])
        previous=list(islice((item['views'] for item in reversed(history)
                             if item['posted_on']<row['posted_on'] and item['bundle_id']!=row['bundle_id']),
                            BASELINE_MAX))
        record['baseline_count']=len(previous)
        baseline=median(previous) if len(previous)>=BASELINE_MIN else None
        record['baseline_views']=baseline
        record['view_ratio']=round(row['views']/baseline,3) if baseline else None
        if record['view_ratio'] is not None and row['angle'] in angles:
            angles[row['angle']].append(record['view_ratio'])
        records[row['project_id']]=record
        history.append(record)
    summaries=[{'angle':key,'label':shorts_bundle.ANGLES[key][0],'count':len(values),
                'median_ratio':round(median(values),3) if len(values)>=ANGLE_MIN else None}
               for key,values in angles.items()]
    eligible=[item for item in summaries if item['median_ratio'] is not None]
    # Equal observed ratios do not establish a preference.
    eligible.sort(key=lambda item:item['median_ratio'],reverse=True)
    suggested=eligible[0] if len(eligible)>=2 and eligible[0]['median_ratio']>eligible[1]['median_ratio'] else None
    return {'records':records,'angles':summaries,'suggested':suggested}
