"""Persistent per-channel YouTube upload queue with a local mock provider."""

import datetime as dt
import hashlib
import http.client
import json
import os
import re
import sqlite3
import threading
from urllib.parse import urlencode, urlsplit

import production


WAKE = threading.Event()
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}')


class UploadUncertain(Exception):
    """Video bytes may have reached the platform. A retry could duplicate them."""


def init():
    with production.CONNECT() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS publishing_jobs (
            project_id TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
            due REAL NOT NULL, payload TEXT NOT NULL, message TEXT NOT NULL, video_id TEXT,
            PRIMARY KEY(project_id, mode))""")


def mode():
    return 'youtube' if os.environ.get('MFLOW_YOUTUBE_MODE')=='youtube' else 'mock'


def account(local_id):
    try:
        accounts=json.loads(os.environ.get('MFLOW_YOUTUBE_ACCOUNTS','{}'))
        target=accounts[local_id]
        channel,token=target['channel_id'],target['refresh_token']
        if (not CHANNEL_ID.fullmatch(channel) or not isinstance(token,str) or not token.strip()
                or not os.environ.get('MFLOW_YOUTUBE_CLIENT_ID') or not os.environ.get('MFLOW_YOUTUBE_CLIENT_SECRET')):
            raise ValueError()
        if sum(isinstance(item,dict) and item.get('channel_id')==channel for item in accounts.values())!=1:
            raise ValueError()
        return channel,token
    except (AttributeError,KeyError,TypeError,ValueError):
        raise ValueError('이 제작 채널의 YouTube 연결을 서버에서 설정하세요.') from None


def snapshot():
    provider=mode()
    targets={}
    with production.CONNECT() as con:
        rows=con.execute('SELECT project_id,status,due,message,video_id FROM publishing_jobs WHERE mode=?',
                         (provider,)).fetchall()
        if provider=='youtube':
            for channel in con.execute('SELECT id FROM production_channels'):
                try:targets[channel['id']]=account(channel['id'])[0]
                except ValueError:pass
    return {'mode':provider,'targets':targets,'jobs':{row['project_id']:dict(row) for row in rows}}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).timestamp()


@production.serialized
def enqueue(data):
    ids=data.get('projects')
    privacy=data.get('privacy','private')
    kids=data.get('made_for_kids')
    if (not isinstance(ids,list) or not 1<=len(ids)<=3 or any(not isinstance(i,str) for i in ids)
            or len(set(ids))!=len(ids) or privacy not in ('private','unlisted','public')
            or type(kids) is not bool or data.get('confirmed') is not True):
        raise ValueError('게시할 영상과 공개 범위·시청자 설정을 확인하세요.')
    provider=mode()
    now=utc_now()
    raw=data.get('scheduled_at')
    due=now
    if raw:
        try:
            when=dt.datetime.fromisoformat(raw)
            if when.utcoffset() is None:raise ValueError()
            due=when.timestamp()
        except (TypeError,ValueError,OverflowError):
            raise ValueError('게시 예약 시간을 확인하세요.') from None
        if due<now or due>now+366*86400:
            raise ValueError('게시 예약은 현재 시각 이후 1년 이내로 선택하세요.')

    queued=0
    targets=set()
    with production.CONNECT() as con:
        for ident in ids:
            row=con.execute('SELECT status FROM publishing_jobs WHERE project_id=? AND mode=?',
                            (ident,provider)).fetchone()
            if row and row['status'] not in ('failed','cancelled'):
                continue
            p=production.project(ident)
            doc=p['document']
            bundle=doc.get('shorts_bundle')
            if p['status']!='completed' or not bundle or bundle['id']!=data.get('bundle_id'):
                raise ValueError('이 묶음에서 렌더링이 완료된 영상만 게시할 수 있습니다.')
            title=p['title']
            description=bundle['description']
            if not 1<=len(title)<=100 or len(description.encode('utf-8'))>5000 or any(
                    c in title+description for c in '<>'):
                raise ValueError('YouTube 제목과 설명을 확인하세요.')
            target=account(p['channel_id'])[0] if provider=='youtube' else p['channel_id']
            if target in targets:
                raise ValueError('서로 다른 YouTube 채널에 배정하세요.')
            targets.add(target)
            path=production.asset(ident,'output')
            with path.open('rb') as video:
                digest=hashlib.file_digest(video,'sha256').hexdigest()
            payload={'title':title,'description':description,'privacy':privacy,'kids':kids,
                     'channel':p['channel_id'],'target':target,'digest':digest}
            con.execute("""INSERT OR REPLACE INTO publishing_jobs VALUES (?,?,'queued',?,?,?,NULL)""",
                        (ident,provider,due,json.dumps(payload,ensure_ascii=False),'업로드 대기 중'))
            queued+=1
    if queued:WAKE.set()
    return {'queued':queued}


def cancel(ident):
    with production.CONNECT() as con:
        result=con.execute("""UPDATE publishing_jobs SET status='cancelled',message='예약 취소됨'
                              WHERE project_id=? AND mode=? AND status='queued'""",(ident,mode()))
        if result.rowcount!=1:
            raise ValueError('대기 중인 업로드만 취소할 수 있습니다.')
    return {'cancelled':True}


def google(method,host,path,body=None,headers=None):
    connection=http.client.HTTPSConnection(host,timeout=60)
    try:
        connection.request(method,path,body,headers or {})
        response=connection.getresponse()
        return response.status,response.getheader('Location'),response.read(128*1024)
    finally:
        connection.close()


def youtube_upload(payload,path):
    expected,refresh=account(payload['channel'])
    if expected!=payload['target']:
        raise ValueError('예약 이후 채널 연결이 바뀌었습니다. 다시 예약하세요.')
    status,_,body=google('POST','oauth2.googleapis.com','/token',urlencode({
        'client_id':os.environ['MFLOW_YOUTUBE_CLIENT_ID'],
        'client_secret':os.environ['MFLOW_YOUTUBE_CLIENT_SECRET'],
        'refresh_token':refresh,'grant_type':'refresh_token',
    }),{'Content-Type':'application/x-www-form-urlencoded'})
    token=json.loads(body).get('access_token') if status==200 else None
    if not token:raise ValueError('YouTube 인증을 갱신하지 못했습니다.')
    auth={'Authorization':'Bearer '+token}
    status,_,body=google('GET','www.googleapis.com','/youtube/v3/channels?part=id&mine=true',headers=auth)
    items=json.loads(body).get('items',[]) if status==200 else []
    if [item.get('id') for item in items]!=[expected]:
        raise ValueError('인증된 YouTube 채널이 예약 대상과 일치하지 않습니다.')
    size=path.stat().st_size
    status,location,_=google('POST','www.googleapis.com',
        '/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
        json.dumps({'snippet':{'title':payload['title'],'description':payload['description'],'categoryId':'22'},
                    'status':{'privacyStatus':payload['privacy'],'selfDeclaredMadeForKids':payload['kids']}},
                   ensure_ascii=False).encode('utf-8'),
        {**auth,'Content-Type':'application/json; charset=utf-8','X-Upload-Content-Length':str(size),
         'X-Upload-Content-Type':'video/mp4'})
    session=urlsplit(location or '')
    if status not in (200,201) or session.scheme!='https' or session.netloc!='www.googleapis.com' or (
            session.path!='/upload/youtube/v3/videos' or not session.query):
        raise ValueError('YouTube 업로드 세션을 시작하지 못했습니다.')
    try:
        with path.open('rb') as video:
            status,_,body=google('PUT',session.netloc,session.path+'?'+session.query,video,
                                 {**auth,'Content-Type':'video/mp4','Content-Length':str(size)})
        result=json.loads(body) if status in (200,201) else {}
        ident=result.get('id','')
        if not VIDEO_ID.fullmatch(ident) or result.get('snippet',{}).get('channelId')!=expected:
            raise UploadUncertain()
        return ident
    except (OSError,http.client.HTTPException,ValueError,TypeError,AttributeError):
        raise UploadUncertain() from None


def recover():
    with production.CONNECT() as con:
        con.execute("""UPDATE publishing_jobs SET status='needs_check',message='업로드 중 앱이 종료됐습니다. YouTube Studio에서 결과를 확인하세요.'
                       WHERE status='running' AND mode='youtube'""")
        con.execute("UPDATE publishing_jobs SET status='queued' WHERE status='running' AND mode='mock'")


def process_one():
    provider=mode()
    with production.CONNECT() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("""SELECT * FROM publishing_jobs WHERE status='queued' AND mode=? AND due<=?
                           ORDER BY due,project_id LIMIT 1""",(provider,utc_now())).fetchone()
        if not row:return False
        ident=row['project_id']
        con.execute("""UPDATE publishing_jobs SET status='running',message='업로드 중'
                       WHERE project_id=? AND mode=?""",(ident,provider))
    video_id=None
    try:
        payload=json.loads(row['payload'])
        p=production.project(ident)
        if p['channel_id']!=payload['channel'] or p['title']!=payload['title']:
            raise ValueError('예약 이후 대상 또는 제목이 바뀌었습니다.')
        path=production.asset(ident,'output')
        with path.open('rb') as video:
            if hashlib.file_digest(video,'sha256').hexdigest()!=payload['digest']:
                raise ValueError('예약 이후 MP4가 바뀌었습니다. 다시 예약하세요.')
        if provider=='youtube':
            video_id=youtube_upload(payload,path)
        else:
            video_id='mock-'+ident[:11]
        status,message='completed','모의 업로드 완료' if provider=='mock' else 'YouTube 업로드 완료'
    except UploadUncertain:
        status,message='needs_check','업로드 결과가 불확실합니다. YouTube Studio에서 확인하세요.'
    except (OSError,http.client.HTTPException,ValueError,KeyError,TypeError,AttributeError) as error:
        status='failed'
        message=str(error) if type(error) is ValueError else '업로드 전에 오류가 발생했습니다. 연결과 파일을 확인하세요.'
    with production.CONNECT() as con:
        con.execute("""UPDATE publishing_jobs SET status=?,message=?,video_id=?
                       WHERE project_id=? AND mode=?""",(status,message,video_id,ident,provider))
    return True


def worker(stop):
    while not stop.is_set():
        try:
            if process_one():continue
        except (OSError,sqlite3.Error):
            pass
        WAKE.wait(2)
        WAKE.clear()
