import datetime as dt
import json
import csv
import io
import statistics
import re
import subprocess
import sys
import uuid
import shutil
import mimetypes
import production
import scene_analysis
import script_drafts
import material_library
import narration
import comment_picker
import os
import sqlite3
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DB = Path(os.environ.get('MFLOW_DB', ROOT / 'data' / 'mflow.db'))
UTC = dt.timezone.utc
KST = dt.timezone(dt.timedelta(hours=9))
SCAN_LOCK = threading.Lock()
MEDIA_LOCK = threading.Lock()
MEDIA_ROOT = ROOT / 'data/downloads'
MEDIA_PYTHON = Path(os.environ.get('MFLOW_MEDIA_PYTHON',ROOT / ('data/media-venv/Scripts/python.exe' if os.name=='nt' else 'data/media-venv/bin/python')))
LOCAL_HOSTS = {'127.0.0.1','localhost','::1'}

class ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3, then also close the connection."""
    def __exit__(self, *exc):
        try: return super().__exit__(*exc)
        finally: self.close()

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=15, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    return con

def init():
    with connect() as con: con.execute('PRAGMA journal_mode=WAL')
    production.init(connect)
    scene_analysis.init()
    script_drafts.init()
    material_library.init()
    narration.init()
    with connect() as con:
        con.executescript('''CREATE TABLE IF NOT EXISTS videos (
          id TEXT PRIMARY KEY, platform TEXT, title TEXT, channel TEXT, url TEXT,
          published TEXT, views INTEGER, likes INTEGER, comments INTEGER,
          previous_views INTEGER, previous_at TEXT, checked_at TEXT,
          source_status TEXT DEFAULT 'unverified');
          CREATE TABLE IF NOT EXISTS media_jobs (id TEXT PRIMARY KEY, platform TEXT, action TEXT, status TEXT, code TEXT, message TEXT, result TEXT, created_at TEXT, updated_at TEXT);
          CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, median_views REAL, sample_count INTEGER, subscribers INTEGER, checked_at TEXT);
          CREATE TABLE IF NOT EXISTS research (video_id TEXT PRIMARY KEY, saved INTEGER DEFAULT 0, source_url TEXT DEFAULT '', attribution TEXT DEFAULT '', notes TEXT DEFAULT '', status TEXT DEFAULT 'unverified');
          CREATE TABLE IF NOT EXISTS bookmarks (url TEXT PRIMARY KEY, platform TEXT, created_at TEXT);
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS runs (at TEXT, status TEXT, message TEXT);
        ''')
        defaults = {'queries': 'funny animals, amazing skills, 搞笑动物, 生活小技巧, 奇闻趣事', 'days': '14', 'min_views': '10000', 'hour_kst': '9', 'schedule': 'false'}
        con.executemany('INSERT OR IGNORE INTO settings VALUES (?,?)', defaults.items())
        con.execute("INSERT OR IGNORE INTO settings VALUES ('region','ALL')")
        columns = {row[1] for row in con.execute('PRAGMA table_info(videos)')}
        for name in ('thumbnail','description','channel_id'):
            if name not in columns: con.execute(f'ALTER TABLE videos ADD COLUMN {name} TEXT')

def settings():
    with connect() as con:
        return dict(con.execute('SELECT key,value FROM settings').fetchall())

def youtube(endpoint, params):
    params['key'] = os.environ.get('YOUTUBE_API_KEY', '')
    req = urllib.request.Request('https://www.googleapis.com/youtube/v3/' + endpoint + '?' + urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try: reasons = {e.get('reason') for e in json.load(exc).get('error',{}).get('errors',[])}
        except Exception: reasons = set()
        if 'commentsDisabled' in reasons: raise ValueError('이 영상은 댓글이 꺼져 있어요. 댓글 카드는 직접 입력하세요.') from None
        if reasons & {'quotaExceeded','dailyLimitExceeded'}: raise ValueError('오늘 YouTube API 사용량을 다 썼어요. 내일(한국 시각 오후 4~5시 이후) 다시 시도하세요.') from None
        if 'videoNotFound' in reasons: raise ValueError('YouTube 영상을 찾을 수 없어요. 비공개·삭제 여부와 원본 링크를 확인하세요.') from None
        raise ValueError('YouTube API 요청에 실패했습니다. API 키, 할당량, 네트워크 허용 설정을 확인하세요.') from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('YouTube API 요청에 실패했습니다. API 키, 할당량, 네트워크 허용 설정을 확인하세요.') from None

def save_video(video, now):
    s, stats = video['snippet'], video.get('statistics', {})
    with connect() as con:
        old = con.execute('SELECT views,checked_at,previous_views,previous_at FROM videos WHERE id=?', (video['id'],)).fetchone()
        # Keep a meaningful baseline; rapid repeated scans must not erase it.
        previous_views, previous_at = None, None
        if old:
            previous_views, previous_at = old['previous_views'], old['previous_at']
            if (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(old['checked_at'])).total_seconds() >= 3600:
                previous_views, previous_at = old['views'], old['checked_at']
        number = lambda key: int(stats[key]) if key in stats else None
        con.execute('''INSERT INTO videos(id,platform,title,channel,url,published,views,likes,comments,previous_views,previous_at,checked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        title=excluded.title,views=excluded.views,likes=excluded.likes,comments=excluded.comments,
        previous_views=excluded.previous_views,previous_at=excluded.previous_at,checked_at=excluded.checked_at''',
        (video['id'], 'YouTube', s['title'], s['channelTitle'], 'https://www.youtube.com/watch?v='+video['id'], s['publishedAt'], number('viewCount'), number('likeCount'), number('commentCount'), previous_views, previous_at, now))
        thumbnails = s.get('thumbnails', {})
        thumbnail = thumbnails.get('high', thumbnails.get('medium', thumbnails.get('default',{}))).get('url','')
        con.execute('UPDATE videos SET thumbnail=?,description=?,channel_id=? WHERE id=?', (thumbnail,s.get('description',''),s.get('channelId',''),video['id']))

def scan():
    if not SCAN_LOCK.acquire(blocking=False):
        raise ValueError('이미 수집 중입니다.')
    now = dt.datetime.now(UTC).isoformat()
    try:
        if not os.environ.get('YOUTUBE_API_KEY'):
            raise ValueError('실제 영상 수집에는 YOUTUBE_API_KEY가 필요합니다. 환경 설정에 안전하게 등록하세요.')
        config = settings()
        since = (dt.datetime.now(UTC)-dt.timedelta(days=int(config['days']))).isoformat().replace('+00:00', 'Z')
        ids = set()
        for query in config['queries'].split(',')[:5]:
            if not query.strip(): continue
            params = {'part':'snippet','q':query.strip(),'type':'video','order':'viewCount','publishedAfter':since,'maxResults':25,'videoDuration':'short'}
            if config.get('region','ALL') != 'ALL': params['regionCode'] = config['region']
            result = youtube('search', params)
            ids.update(item['id']['videoId'] for item in result.get('items', []))
        found = list(ids)
        count = 0
        for offset in range(0, len(found), 50):
            result = youtube('videos', {'part':'snippet,statistics','id':','.join(found[offset:offset+50])})
            for video in result.get('items', []):
                save_video(video, now)
                count += 1
        message = f'{count}개 영상의 공개 통계를 확인했습니다.'
        with connect() as con: con.execute('INSERT INTO runs VALUES (?,?,?)', (now,'success',message))
        return message
    except ValueError as exc:
        with connect() as con: con.execute('INSERT INTO runs VALUES (?,?,?)', (now,'error',str(exc)))
        raise
    finally:
        SCAN_LOCK.release()

def metrics(row, now=None):
    video = dict(row)
    now = now or dt.datetime.now(UTC)
    published = dt.datetime.fromisoformat(video['published'].replace('Z','+00:00')) if video['published'] else None
    age = max((now-published).total_seconds()/86400, 1/24) if published else None
    views = video['views'] or 0
    known = video['likes'] is not None and video['comments'] is not None
    video['engagement'] = round((video['likes']+video['comments'])/views*100, 2) if known and views else None
    video['views_per_day'] = round(views/age) if age and video['views'] is not None else None
    video['vph'] = round(views/max((now-published).total_seconds()/3600,1/60)) if published and video['views'] is not None else None
    video['growth_per_hour'] = None
    if video['previous_at'] and video['previous_views'] is not None:
        hours = (dt.datetime.fromisoformat(video['checked_at'])-dt.datetime.fromisoformat(video['previous_at'])).total_seconds()/3600
        if hours > 0: video['growth_per_hour'] = round((views-video['previous_views'])/hours)
    video['age_days'] = round(age,1) if age is not None else None
    return video

def state():
    config = settings()
    with connect() as con:
        videos = [metrics(row) for row in con.execute('SELECT * FROM videos')]
        bookmarks = [dict(row) for row in con.execute('SELECT * FROM bookmarks ORDER BY created_at DESC')]
        channels = {row['id']:dict(row) for row in con.execute('SELECT * FROM channels')}
        jobs = [dict(row) for row in con.execute('SELECT * FROM media_jobs ORDER BY created_at DESC LIMIT 20')]
        research = {row['video_id']:dict(row) for row in con.execute('SELECT * FROM research')}
        runs = [dict(row) for row in con.execute('SELECT * FROM runs ORDER BY rowid DESC LIMIT 5')]
    for v in videos:
        channel = channels.get(v.get('channel_id'))
        v['channel_stats'] = channel
        v['median_ratio'] = round(v['views']/channel['median_views'],2) if channel and channel['median_views'] and v['views'] is not None else None
    all_videos = videos[:]
    videos = [v for v in videos if (v['age_days'] is None or v['age_days'] <= int(config['days'])) and (v['views'] or 0) >= int(config['min_views'])]
    videos.sort(key=lambda v:(v['engagement'] or 0, v['views_per_day'] or 0), reverse=True)
    return {'all_videos':all_videos, 'research':research, 'bookmarks':bookmarks, 'videos':videos, 'settings':config, 'runs':runs, 'media_jobs':[{**job,'result':json.loads(job['result'] or '{}')} for job in jobs], 'media_installed':MEDIA_PYTHON.is_file(), 'api_ready':bool(os.environ.get('YOUTUBE_API_KEY')), 'scanning':SCAN_LOCK.locked()}

def validate_media_task(data):
    platform = data.get('platform')
    action = data.get('action')
    if platform not in ('TikTok','Douyin') or action not in ('search','hot','inspect','download'):
        raise ValueError('지원하지 않는 플랫폼 또는 작업입니다.')
    if action == 'hot' and platform != 'Douyin': raise ValueError('인기 목록은 더우인만 지원합니다.')
    task={'platform':platform,'action':action}
    if action == 'search':
        query=str(data.get('query','')).strip()
        if not query or len(query)>100: raise ValueError('검색어는 1~100자로 입력하세요.')
        task['query']=query
    if action in ('inspect','download'):
        url=str(data.get('url','')).strip()
        parsed=urllib.parse.urlsplit(url)
        hosts={'TikTok':{'www.tiktok.com','tiktok.com','vm.tiktok.com','vt.tiktok.com'},'Douyin':{'www.douyin.com','douyin.com','v.douyin.com'}}
        if parsed.scheme!='https' or parsed.hostname not in hosts[platform] or parsed.username or parsed.password or parsed.port or len(url)>2048:
            raise ValueError('선택한 플랫폼의 HTTPS 영상 링크를 입력하세요.')
        canonical = parsed.hostname in ('www.tiktok.com','tiktok.com','www.douyin.com','douyin.com')
        pattern = r'/@[A-Za-z0-9_.]+/video/[0-9]+/?' if platform=='TikTok' else r'/video/[0-9]+/?'
        if (canonical and not re.fullmatch(pattern,parsed.path)) or (not canonical and not re.fullmatch(r'/[A-Za-z0-9_-]+/?',parsed.path)):
            raise ValueError('계정 홈이나 검색 주소 대신 개별 영상 링크를 입력하세요.')
        host = ('www.tiktok.com' if platform=='TikTok' else 'www.douyin.com') if canonical else parsed.hostname
        task['url']=urllib.parse.urlunsplit(('https',host,parsed.path,'',''))
    return task


def save_external_video(video):
    platform=video.get('platform')
    if platform not in ('TikTok','Douyin'): raise ValueError('잘못된 수집 응답입니다.')
    external_id=str(video.get('external_id',''))
    if not re.fullmatch(r'[0-9]+',external_id): raise ValueError('잘못된 영상 ID입니다.')
    task=validate_media_task({'platform':platform,'action':'inspect','url':video.get('url')})
    number=lambda key: max(0,int(video[key])) if video.get(key) is not None else None
    published=video.get('published')
    if published: dt.datetime.fromisoformat(published.replace('Z','+00:00'))
    video_id=platform.lower()+':'+external_id
    now=dt.datetime.now(UTC).isoformat()
    with connect() as con:
        old=con.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone()
        previous_views,previous_at=(old['previous_views'],old['previous_at']) if old else (None,None)
        if old and (dt.datetime.fromisoformat(now)-dt.datetime.fromisoformat(old['checked_at'])).total_seconds()>=3600:
            previous_views,previous_at=old['views'],old['checked_at']
        con.execute("""INSERT INTO videos(id,platform,title,channel,url,published,views,likes,comments,checked_at,thumbnail,description,channel_id,previous_views,previous_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,channel=excluded.channel,views=excluded.views,likes=excluded.likes,comments=excluded.comments,checked_at=excluded.checked_at,description=excluded.description,thumbnail=excluded.thumbnail,published=excluded.published,previous_views=excluded.previous_views,previous_at=excluded.previous_at""",
        (video_id,platform,str(video.get('title',''))[:2000],str(video.get('channel',''))[:500],task['url'],published,number('views'),number('likes'),number('comments'),now,str(video.get('thumbnail',''))[:2048],str(video.get('description',''))[:10000],platform.lower()+':'+str(video.get('channel_id',''))[:200],previous_views,previous_at))
    return video_id


def start_media_job(data):
    task=validate_media_task(data)
    if not MEDIA_PYTHON.is_file(): raise ValueError('미디어 도구를 먼저 설치하세요. '+(r'Windows는 scripts\install_windows.cmd' if os.name=='nt' else 'scripts/install_media.sh')+'를 실행하면 됩니다.')
    job_id=uuid.uuid4().hex
    now=dt.datetime.now(UTC).isoformat()
    with MEDIA_LOCK:
        with connect() as con:
            if con.execute("SELECT count(*) FROM media_jobs WHERE status IN ('queued','running')").fetchone()[0]>=2:
                raise ValueError('수집 작업 2개가 진행 중입니다. 완료 후 다시 시도하세요.')
            con.execute('INSERT INTO media_jobs VALUES (?,?,?,?,?,?,?,?,?)',(job_id,task['platform'],task['action'],'queued','','작업 대기 중','{}',now,now))
    threading.Thread(target=run_media_job,args=(job_id,task),daemon=True).start()
    return job_id


def run_media_job(job_id, task):
    now=lambda:dt.datetime.now(UTC).isoformat()
    directory=MEDIA_ROOT/job_id
    task={**task,'directory':str(directory)}
    with connect() as con:con.execute("UPDATE media_jobs SET status='running',message='플랫폼 연결 및 작업 실행 중',updated_at=? WHERE id=?",(now(),job_id))
    outcome={}
    try:
        process=subprocess.run([str(MEDIA_PYTHON),str(ROOT/'media_worker.py')],input=json.dumps(task),text=True,encoding='utf-8',errors='replace',stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=240)
        outcome=json.loads(process.stdout)
        if not isinstance(outcome,dict): outcome={};raise ValueError('잘못된 작업 응답')
        if not outcome.get('ok'): raise ValueError(outcome.get('message') or '작업 실패')
        result=outcome.get('result',{})
        video_ids=[save_external_video(video) for video in result.pop('videos',[])[:10]]
        result['video_ids']=video_ids
        message=f'{len(video_ids)}개 영상의 공개 정보를 저장했습니다.' if task['action']!='hot' else f"더우인 인기 검색어 {len(result.get('trends',[]))}개를 가져왔습니다."
        if task['action']=='download':message='영상 다운로드가 완료되었습니다.'
        with connect() as con:con.execute("UPDATE media_jobs SET status='success',message=?,result=?,updated_at=? WHERE id=?",(message,json.dumps(result,ensure_ascii=False),now(),job_id))
    except Exception as exc:
        if not isinstance(exc,(ValueError,subprocess.TimeoutExpired,OSError,KeyError,TypeError)): traceback.print_exc()
        code=outcome.get('code') or 'tool_error'
        message=outcome.get('message') or '작업이 시간 내 완료되지 않았거나 응답을 읽지 못했습니다. 설치 상태와 플랫폼 접근 조건을 확인하세요.'
        shutil.rmtree(directory,ignore_errors=True)
        try:
            with connect() as con:con.execute("UPDATE media_jobs SET status='error',code=?,message=?,updated_at=? WHERE id=?",(code,message,now(),job_id))
        except sqlite3.Error: traceback.print_exc()


def analyze_channel(channel_id):
    now = dt.datetime.now(UTC)
    with connect() as con: cached = con.execute('SELECT checked_at FROM channels WHERE id=?',(channel_id,)).fetchone()
    if cached and (now-dt.datetime.fromisoformat(cached[0])).total_seconds() < 21600:
        return '최근 6시간 내 분석 결과를 사용합니다.'
    if not os.environ.get('YOUTUBE_API_KEY'): raise ValueError('YouTube API 키가 필요합니다.')
    info = youtube('channels', {'part':'contentDetails,statistics','id':channel_id}).get('items',[])
    if not info: raise ValueError('공개 채널 정보를 가져올 수 없습니다.')
    channel = info[0]
    uploads = channel.get('contentDetails',{}).get('relatedPlaylists',{}).get('uploads')
    if not uploads: raise ValueError('채널 업로드 목록이 공개되지 않았습니다.')
    items = youtube('playlistItems', {'part':'contentDetails','playlistId':uploads,'maxResults':50}).get('items',[])
    ids = [item['contentDetails']['videoId'] for item in items]
    if not ids: raise ValueError('분석할 공개 영상이 없습니다.')
    result = youtube('videos', {'part':'statistics','id':','.join(ids)}).get('items',[])
    values = [int(item['statistics']['viewCount']) for item in result if 'viewCount' in item.get('statistics',{})]
    if not values: raise ValueError('조회수가 공개된 영상이 없습니다.')
    stats = channel.get('statistics',{})
    subscribers = None if stats.get('hiddenSubscriberCount') else int(stats['subscriberCount']) if 'subscriberCount' in stats else None
    with connect() as con:
        con.execute('INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?)',(channel_id,statistics.median(values),len(values),subscribers,now.isoformat()))
    return f'최근 공개 업로드 {len(values)}개의 조회수 중앙값을 계산했습니다.'

def source_comments(project_id):
    """Public top comments of a project's YouTube source, ranked for comment cards. Nothing is stored."""
    doc = production.project(project_id)['document']
    video_id = comment_picker.youtube_id(doc.get('source_url'))
    if not video_id: raise ValueError('원본 링크가 YouTube 영상일 때 댓글을 자동으로 불러올 수 있어요. TikTok·더우인 댓글은 직접 입력하세요.')
    if not os.environ.get('YOUTUBE_API_KEY'): raise ValueError('댓글을 불러오려면 YOUTUBE_API_KEY가 필요해요. 서버를 켜기 전에 set YOUTUBE_API_KEY=키 를 입력하세요.')
    items, token = [], None
    for _ in range(2):  # 2 pages = 2 quota units, about 200 top comments
        params = {'part':'snippet','videoId':video_id,'maxResults':100,'order':'relevance','textFormat':'plainText'}
        if token: params['pageToken'] = token
        page = youtube('commentThreads', params)
        items += page.get('items',[])
        token = page.get('nextPageToken')
        if not token: break
    result = comment_picker.from_youtube(items)
    if not doc.get('credit'):  # 1 more quota unit, only while the credit line is empty
        info = youtube('videos', {'part':'snippet','id':video_id}).get('items',[])
        result['credit'] = production.credit_line(info[0]['snippet'].get('channelTitle')) if info else ''
    if not result['comments']: raise ValueError('카드로 쓸 만한 공개 댓글을 찾지 못했어요. 댓글 카드는 직접 입력하세요.')
    return result

def scheduler():
    # Opt-in daily scan, persisted across process restarts.
    while True:
        try:
            config = settings()
            now = dt.datetime.now(KST)
            if config['schedule']=='true' and now.hour >= int(config['hour_kst']):
                with connect() as con:
                    last = con.execute("SELECT value FROM settings WHERE key='scheduled_date'").fetchone()
                if (not last or last[0] != now.date().isoformat()) and not SCAN_LOCK.locked():
                    with connect() as con: con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('scheduled_date',now.date().isoformat()))
                    try: scan()
                    except ValueError: pass
        except Exception:
            traceback.print_exc()
        threading.Event().wait(30)

class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        body = json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def local_request(self):
        # Blocks DNS-rebinding pages: the browser must address this server as localhost.
        host = urllib.parse.urlsplit('//'+(self.headers.get('Host') or '')).hostname
        return host in LOCAL_HOSTS
    def fail(self, exc):
        traceback.print_exc()
        try: self.send_json({'error':'서버 내부 오류가 발생했습니다. 잠시 후 다시 시도하세요.'},500)
        except OSError: pass
    def do_GET(self):
        if not self.local_request(): return self.send_error(403)
        try: self.route_get()
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as exc: self.fail(exc)
    def route_get(self):
        if self.path.startswith('/api/narration/state?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query);return self.send_json(narration.state(query.get('id',[''])[0]))
        if self.path.startswith('/api/narration/file?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:path=narration.file(query.get('id',[''])[0])
            except ValueError:return self.send_error(404)
            body=path.read_bytes();self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        if self.path == '/api/materials/state':return self.send_json(material_library.state())
        if self.path.startswith('/api/scripts/state?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self.send_json(script_drafts.state(query.get('id',[''])[0]))
        if self.path.startswith('/api/scenes/state?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self.send_json(scene_analysis.state(query.get('id',[''])[0]))
        if self.path.startswith('/api/scenes/frame?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:path=scene_analysis.frame(query.get('id',[''])[0],int(query.get('index',['-1'])[0]))
            except ValueError:return self.send_error(404)
            body=path.read_bytes();self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        if self.path == '/api/production/state': return self.send_json(production.state())
        if self.path.startswith('/api/production/font?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try: path=production.font_file(query.get('file',[''])[0])
            except ValueError: return self.send_error(404)
            body=path.read_bytes()
            self.send_response(200);self.send_header('Content-Type',{'.otf':'font/otf','.ttc':'font/collection'}.get(path.suffix.lower(),'font/ttf'));self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','max-age=3600');self.end_headers();self.wfile.write(body);return
        if self.path.startswith('/api/production/frame?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try: body=production.preview_frame(query.get('id',[''])[0],query.get('t',['0'])[0])
            except ValueError as exc: return self.send_json({'error':str(exc)},400)
            self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
        if self.path.startswith('/api/production/asset?'):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try: path=production.asset(query.get('id',[''])[0],query.get('kind',[''])[0])
            except ValueError: return self.send_error(404)
            size=path.stat().st_size;start=0;end=size-1;status=200
            range_header=self.headers.get('Range')
            if range_header:
                match=re.fullmatch(r'bytes=([0-9]*)-([0-9]*)',range_header)
                if not match or not any(match.groups()): return self.send_error(416)
                a,b=match.groups()
                if a:start=int(a);end=min(int(b) if b else end,end)
                else:start=max(0,size-int(b))
                if start>end or start>=size:
                    self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
                status=206
            mime='application/zip' if path.suffix=='.zip' else 'video/mp4'
            if path.suffix=='.source':
                with path.open('rb') as source_file:
                    if source_file.read(4)==b'\x1a\x45\xdf\xa3':mime='video/webm'
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
            if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            if query.get('download')==['1'] or path.suffix=='.zip':self.send_header('Content-Disposition','attachment; filename='+('project.zip' if path.suffix=='.zip' else 'short.mp4'))
            self.end_headers()
            try:
                with path.open('rb') as file:
                    file.seek(start);remaining=end-start+1
                    while remaining:
                        chunk=file.read(min(1024*1024,remaining))
                        if not chunk:break
                        self.wfile.write(chunk);remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        if self.path.startswith('/api/media/file?'):
            job_id = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('id',[''])[0]
            with connect() as con: job = con.execute("SELECT * FROM media_jobs WHERE id=? AND action='download' AND status='success'",(job_id,)).fetchone()
            if not job: return self.send_error(404)
            filename = json.loads(job['result']).get('filename','')
            directory = MEDIA_ROOT / job_id
            path = directory / filename
            if not filename or Path(filename).name != filename or not path.is_file() or path.resolve().parent != directory.resolve(): return self.send_error(404)
            self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(filename)[0] or 'application/octet-stream');self.send_header('Content-Length',str(path.stat().st_size));self.send_header('Content-Disposition','attachment; filename='+filename);self.end_headers()
            with path.open('rb') as file: shutil.copyfileobj(file,self.wfile)
            return
        if self.path == '/api/state': return self.send_json(state())
        if self.path == '/api/export':
            snapshot = state()
            output = io.StringIO(); writer = csv.writer(output)
            writer.writerow(['제목','채널','플랫폼','링크','게시일','조회수','좋아요','댓글','참여율(%)','누적 시간평균 조회수','관측 시간당 증가량','채널 중앙값 대비','원본 상태','원본 후보','출처 단서','메모','저장됨'])
            for v in snapshot['all_videos']:
                r = snapshot['research'].get(v['id'], {})
                cells = [v['title'],v['channel'],v['platform'],v['url'],v['published'],v['views'],v['likes'],v['comments'],v['engagement'],v['vph'],v['growth_per_hour'],v['median_ratio'],r.get('status','unverified'),r.get('source_url',''),r.get('attribution',''),r.get('notes',''),r.get('saved',0)]
                writer.writerow(["'"+str(c) if isinstance(c,str) and c.lstrip().startswith(('=','+','-','@')) else c for c in cells])
            body = ('\ufeff'+output.getvalue()).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8'); self.send_header('Content-Disposition','attachment; filename=mflow-videos.csv'); self.end_headers(); self.wfile.write(body); return
        files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css','/production.js':'production.js','/scene-analysis.js':'scene-analysis.js','/script-drafts.js':'script-drafts.js','/materials.js':'materials.js','/narration.js':'narration.js'}
        name = files.get(self.path)
        if not name: return self.send_error(404)
        body = (ROOT/'web'/name).read_bytes()
        self.send_response(200); self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css'}[name.split('.')[-1]]); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        # Accept only local same-origin browser requests; keys never reach the browser.
        if not self.local_request(): return self.send_json({'error':'허용되지 않은 요청입니다.'},403)
        origin = self.headers.get('Origin')
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'): return self.send_json({'error':'허용되지 않은 요청입니다.'},403)
        try:
            length = int(self.headers.get('Content-Length','0'))
            if self.path.startswith('/api/narration/upload?'):
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query);return self.send_json({'id':narration.upload(query.get('id',[''])[0],self.rfile,length)})
            if self.path.startswith('/api/production/upload?'):
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                info=production.upload(query.get('id',[''])[0],self.rfile,length)
                return self.send_json({'message':'원본 영상을 업로드했습니다.','source':info})
            if length < 0 or length > (131072 if self.path=='/api/scenes/review' else 32768 if self.path=='/api/materials/save' else 16384): raise ValueError('요청이 너무 큽니다.')
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data,dict): raise ValueError('JSON 객체가 필요합니다.')
            if self.path == '/api/narration/start':return self.send_json({'id':narration.start(str(data.get('project_id','')))},202)
            if self.path == '/api/narration/activate':
                narration.activate(str(data.get('id','')),data.get('cues'),data.get('confirmed'));return self.send_json({'message':'내레이션을 선택했습니다. 렌더링 전 다시 승인하세요.'})
            if self.path == '/api/narration/disable':
                narration.disable(str(data.get('project_id','')));return self.send_json({'message':'내레이션 선택을 해제했습니다. 원음/무음 설정으로 제작합니다.'})
            if self.path == '/api/materials/collect':return self.send_json({'video_ids':material_library.collect(data.get('video_ids'),data.get('min_views',1000000),data.get('min_ratio',3))})
            if self.path == '/api/materials/save':
                material_library.save(data);return self.send_json({'message':'소재 기획을 저장했습니다.'})
            if self.path == '/api/materials/outcomes':
                material_library.outcome(data);return self.send_json({'message':'재가공 버전의 성과를 기록했습니다.'})
            if self.path == '/api/scripts/start':return self.send_json({'id':script_drafts.start(str(data.get('project_id','')),data.get('kind'))},202)
            if self.path == '/api/scripts/save':
                script_drafts.save(str(data.get('id','')),data.get('data'));return self.send_json({'message':'대본 초안 수정을 저장했습니다.'})
            if self.path == '/api/scripts/apply':
                script_drafts.apply(str(data.get('id','')),data.get('confirmed'));return self.send_json({'message':'제작에 반영했습니다. 렌더링 전 대본·설정을 승인하세요.'})
            if self.path == '/api/scenes/start':return self.send_json({'id':scene_analysis.start(str(data.get('project_id','')),data.get('action'),data.get('interval',2))},202)
            if self.path == '/api/scenes/review':
                scene_analysis.review(str(data.get('project_id','')),data.get('segments'),data.get('transcript',''),data.get('confirmed',False));return self.send_json({'message':'분석 검토 내용을 저장했습니다.'})
            if self.path == '/api/production/channels':return self.send_json({'id':production.create_channel(data)})
            if self.path == '/api/production/projects':return self.send_json({'id':production.create_project(data)})
            if self.path == '/api/production/update':
                production.update_project(data);return self.send_json({'message':'프로젝트를 저장했습니다. 설정 변경으로 이전 승인은 해제됩니다.'})
            if self.path == '/api/production/comments': return self.send_json(source_comments(str(data.get('id',''))))
            if self.path == '/api/production/approve':
                production.approve(str(data.get('id','')));return self.send_json({'message':'현재 대본과 스타일을 승인했습니다.'})
            if self.path == '/api/production/render':
                production.start_render(str(data.get('id','')));return self.send_json({'message':'렌더링을 시작했습니다.'},202)
            if self.path == '/api/media/jobs': return self.send_json({'job_id':start_media_job(data)},202)
            if self.path == '/api/scan': return self.send_json({'message':scan()})
            if self.path == '/api/bookmarks':
                url = str(data.get('url','')).strip()
                parsed = urllib.parse.urlsplit(url)
                hosts = {'youtube.com':'YouTube','www.youtube.com':'YouTube','youtu.be':'YouTube','m.youtube.com':'YouTube','www.tiktok.com':'TikTok','tiktok.com':'TikTok','vm.tiktok.com':'TikTok','vt.tiktok.com':'TikTok','www.douyin.com':'Douyin','douyin.com':'Douyin','v.douyin.com':'Douyin','www.instagram.com':'Instagram','instagram.com':'Instagram'}
                if parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port or parsed.path in ('','/') or len(url)>2048:
                    raise ValueError('YouTube, TikTok, Douyin, Instagram의 HTTPS 게시물 링크를 입력하세요.')
                with connect() as con: con.execute('INSERT OR IGNORE INTO bookmarks VALUES (?,?,?)',(url,hosts[parsed.hostname],dt.datetime.now(UTC).isoformat()))
                return self.send_json({'message':'출처 후보 링크를 저장했습니다. 원저작자는 별도로 확인하세요.'})
            if self.path == '/api/channel':
                video_id = str(data.get('video_id',''))
                with connect() as con: video = con.execute('SELECT channel_id FROM videos WHERE id=?',(video_id,)).fetchone()
                if not video or not video['channel_id']: raise ValueError('채널 정보를 찾을 수 없습니다. 영상을 다시 수집하세요.')
                return self.send_json({'message':analyze_channel(video['channel_id'])})
            if self.path == '/api/research':
                video_id = str(data.get('video_id',''))
                saved = data.get('saved',0)
                status = data.get('status','unverified')
                source_url = str(data.get('source_url','')).strip()
                attribution, notes = str(data.get('attribution','')),str(data.get('notes',''))
                if status not in ('unverified','candidate','verified') or saved not in (0,1) or len(notes)>5000 or len(attribution)>1000 or len(source_url)>2048:
                    raise ValueError('추적 정보의 형식이나 길이를 확인하세요.')
                if source_url:
                    source = urllib.parse.urlsplit(source_url)
                    if source.scheme != 'https' or not source.hostname or source.username or source.password: raise ValueError('출처는 HTTPS 링크로 입력하세요.')
                with connect() as con:
                    if not con.execute('SELECT 1 FROM videos WHERE id=?',(video_id,)).fetchone(): raise ValueError('영상을 찾을 수 없습니다.')
                    con.execute('INSERT OR REPLACE INTO research VALUES (?,?,?,?,?,?)',(video_id,saved,source_url,attribution,notes,status))
                return self.send_json({'message':'추적 메모를 저장했습니다.'})
            if self.path == '/api/settings':
                if any(k not in data for k in ('queries','days','min_views','hour_kst','schedule')): raise ValueError('검색어·기간·최소 조회수·수집 시각·자동 수집 값을 모두 입력하세요.')
                config = {k:str(data[k]).strip() for k in ('queries','days','min_views','hour_kst','schedule')}
                config['region'] = str(data.get('region','ALL'))
                if config['region'] not in ('ALL','KR','US','TW','JP'): raise ValueError('지원하지 않는 검색 지역입니다.')
                if not config['queries'].strip() or len(config['queries'])>500: raise ValueError('검색어를 입력하세요. 최대 500자입니다.')
                if not all(re.fullmatch(r'[0-9]{1,13}',config[k]) for k in ('days','min_views','hour_kst')): raise ValueError('기간·최소 조회수·수집 시각은 숫자로 입력하세요.')
                if not 1 <= int(config['days']) <= 14 or not 0 <= int(config['min_views']) <= 10**12 or not 0 <= int(config['hour_kst']) <= 23 or config['schedule'] not in ('true','false'): raise ValueError('필터 설정을 확인하세요.')
                with connect() as con: con.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',config.items())
                return self.send_json({'message':'설정을 저장했습니다.'})
            self.send_error(404)
        except (ValueError,KeyError,TypeError) as exc: self.send_json({'error':str(exc)},400)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as exc: self.fail(exc)

def main():
    init()
    with connect() as con:
        con.execute("UPDATE narrations SET status='error',message='서버 재시작으로 음성 준비가 중단되었습니다.' WHERE status='preparing'")
        con.execute("UPDATE script_drafts SET status='error',message='서버 재시작으로 생성이 중단됐습니다. 다시 생성하세요.' WHERE status='generating'")
        con.execute("UPDATE scene_analyses SET status='error',message='서버 재시작으로 분석이 중단됐습니다. 프레임 추출부터 다시 실행하세요.' WHERE status IN ('preparing','analyzing')")
        con.execute("UPDATE production_projects SET status='error' WHERE status='rendering'")
        con.execute("UPDATE media_jobs SET status='error',code='interrupted',message='서버가 재시작되어 작업이 중단되었습니다. 다시 시도하세요.' WHERE status IN ('queued','running')")
    threading.Thread(target=scheduler,daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('PORT','8000'))),Handler)
    print('mflowshorts server ready',flush=True)
    server.serve_forever()

if __name__ == '__main__': main()
