"""Isolated third-party media tooling. JSON stdin/stdout; diagnostics stay private."""
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'data/vendor/douyin-downloader'

class MediaError(Exception):
    def __init__(self, code, message): self.code, self.message = code, message


def preflight(url):
    try:
        request = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read(32)
    except urllib.error.URLError as exc:
        text = str(exc).lower()
        if 'tunnel connection failed: 403' in text:
            raise MediaError('network_blocked','클라우드 네트워크 정책이 플랫폼 접속을 차단했습니다. 환경 설정에서 해당 도메인을 허용한 뒤 다시 시도하세요.') from None
        if 'certificate' in text or 'ssl' in text:
            raise MediaError('tls_error','플랫폼 TLS 인증서 검증에 실패했습니다. 인증서 신뢰 설정을 확인하세요.') from None
        if getattr(exc,'code',None) in (403,429):
            raise MediaError('platform_blocked','플랫폼이 요청을 거절했습니다. 로그인·지역 제한·요청 제한을 확인하세요.') from None
        raise MediaError('network_error','플랫폼 연결에 실패했습니다. 네트워크 상태를 확인하세요.') from None


def cookies(platform):
    path = os.environ.get(platform.upper()+'_COOKIE_FILE')
    if path and not Path(path).is_file():
        raise MediaError('cookie_file_missing','설정된 쿠키 파일을 찾을 수 없습니다. 파일 경로를 확인하세요.')
    return path


def normalize_yt(info, platform):
    timestamp = info.get('timestamp')
    published = dt.datetime.fromtimestamp(timestamp,dt.timezone.utc).isoformat() if timestamp else None
    return {'external_id':str(info['id']), 'platform':platform,'title':info.get('title') or info.get('description') or '제목 미제공', 'channel':info.get('uploader') or info.get('creator') or '작성자 미제공','channel_id':info.get('uploader_id') or '', 'url':info.get('webpage_url') or info.get('original_url'), 'published':published, 'views':info.get('view_count'),'likes':info.get('like_count'),'comments':info.get('comment_count'),'description':info.get('description') or '', 'thumbnail':info.get('thumbnail') or ''}


def normalize_douyin(info):
    stats=info.get('statistics') or {}; author=info.get('author') or {}; stamp=info.get('create_time')
    return {'external_id':str(info['aweme_id']), 'platform':'Douyin','title':info.get('desc') or '제목 미제공','channel':author.get('nickname') or '작성자 미제공','channel_id':author.get('unique_id') or author.get('sec_uid') or '', 'url':'https://www.douyin.com/video/'+str(info['aweme_id']), 'published':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat() if stamp else None, 'views':stats.get('play_count'),'likes':stats.get('digg_count'),'comments':stats.get('comment_count'),'description':info.get('desc') or '', 'thumbnail':((((info.get('video') or {}).get('cover') or {}).get('url_list')) or [''])[0]}


async def douyin_discover(task):
    import http.cookiejar
    sys.path.insert(0,str(VENDOR))
    from core.api_client import DouyinAPIClient
    cookie_values={}
    path=cookies('douyin')
    if path:
        jar=http.cookiejar.MozillaCookieJar(path);jar.load(ignore_discard=True,ignore_expires=False)
        cookie_values={c.name:c.value for c in jar if (c.domain.lstrip('.')=='douyin.com' or c.domain.endswith('.douyin.com'))}
    async with DouyinAPIClient(cookie_values,proxy=os.environ.get('HTTPS_PROXY')) as api:
        if task['action']=='hot':
            result=await api.get_hot_search_board()
            entries=[{'keyword':str(item.get('word') or ''),'heat':item.get('hot_value'),'rank':index+1} for index,item in enumerate(result.get('items',[])[:20])]
            if not entries: raise MediaError('empty_unverified','더우인 인기 목록을 받지 못했습니다. 로그인 또는 플랫폼 검증이 필요할 수 있습니다. 빈 응답을 정상 수집으로 처리하지 않았습니다.')
            return {'trends':entries,'videos':[]}
        result=await api.search_aweme(task['query'],count=10,sort_type=1,publish_time=7)
        videos=[normalize_douyin(item) for item in result.get('items',[])[:10] if item.get('aweme_id')]
        if not videos: raise MediaError('empty_unverified','더우인 검색 응답이 비어 있습니다. 검색 결과 없음인지 접근 제한인지 확인되지 않았습니다.')
        return {'videos':videos}


def tiktok_search(task):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise MediaError('browser_missing','TikTok 검색용 Playwright와 Chromium이 설치되지 않았습니다.') from None
    items={}
    with sync_playwright() as p:
        proxy=os.environ.get('HTTPS_PROXY')
        options={'headless':True,'args':['--no-sandbox']}
        chromium=os.environ.get('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').is_file() else None)
        if chromium: options['executable_path']=chromium
        if proxy: options['proxy']={'server':proxy,'bypass':'localhost,127.0.0.1'}
        browser=p.chromium.launch(**options)
        context=browser.new_context(locale='en-US')
        cookie_path=cookies('tiktok')
        if cookie_path:
            import http.cookiejar
            jar=http.cookiejar.MozillaCookieJar(cookie_path);jar.load(ignore_discard=True,ignore_expires=False)
            context.add_cookies([{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path or '/','secure':c.secure} for c in jar if (c.domain.lstrip('.')=='tiktok.com' or c.domain.endswith('.tiktok.com'))])
        page=context.new_page()
        def capture(response):
            if response.url.startswith('https://www.tiktok.com/api/search/'):
                try:
                    payload=response.json()
                    candidates=payload.get('item_list') or payload.get('itemList') or []
                    for entry in payload.get('data') or []:
                        if isinstance(entry,dict) and isinstance(entry.get('item'),dict): candidates.append(entry['item'])
                    for item in candidates:
                        if isinstance(item,dict) and item.get('id'): items[str(item['id'])]=item
                except Exception: pass
        page.on('response',capture)
        try:
            page.goto('https://www.tiktok.com/search?q='+urllib.parse.quote(task['query']),wait_until='domcontentloaded',timeout=30000)
            page.wait_for_timeout(5000)
            text=page.locator('body').inner_text(timeout=5000).lower()
            if not items and any(word in text for word in ['captcha','verify to continue','verify you are human']):
                raise MediaError('verification_required','TikTok에서 사람 확인을 요구합니다. 검증을 우회하지 않았습니다.')
            for selector in ['script#__UNIVERSAL_DATA_FOR_REHYDRATION__','script#SIGI_STATE']:
                if page.locator(selector).count():
                    payload=json.loads(page.locator(selector).first.inner_text())
                    for item in payload.get('ItemModule',{}).values():
                        if isinstance(item,dict) and item.get('id'):items[str(item['id'])]=item
        finally: browser.close()
    videos=[]
    for item in list(items.values())[:10]:
        author=item.get('author') or {}; stats=item.get('stats') or item.get('statistics') or {}
        unique=author.get('uniqueId') if isinstance(author,dict) else author
        if not unique or not re.fullmatch(r'[A-Za-z0-9_.]+',str(unique)):continue
        stamp=item.get('createTime')
        videos.append({'external_id':str(item['id']),'platform':'TikTok','title':item.get('desc') or '제목 미제공','channel':author.get('nickname',unique) if isinstance(author,dict) else unique,'channel_id':unique,'url':f'https://www.tiktok.com/@{unique}/video/{item["id"]}','published':dt.datetime.fromtimestamp(int(stamp),dt.timezone.utc).isoformat() if stamp else None,'views':stats.get('playCount'),'likes':stats.get('diggCount'),'comments':stats.get('commentCount'),'description':item.get('desc') or '', 'thumbnail':(item.get('video') or {}).get('cover') or ''})
    if not videos:raise MediaError('empty_unverified','TikTok 공개 검색 결과를 읽지 못했습니다. 로그인·화면 변경·접근 제한 여부를 확인해야 합니다.')
    return {'videos':videos}


def run(task):
    platform=task['platform']; action=task['action']
    preflight('https://www.douyin.com/' if platform=='Douyin' else 'https://www.tiktok.com/')
    if action in ('search','hot'):
        return asyncio.run(douyin_discover(task)) if platform=='Douyin' else tiktok_search(task)
    import yt_dlp
    options={'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':15,'retries':0,'fragment_retries':0,'extractor_retries':0,'cachedir':False}
    cookie_path=cookies(platform)
    if cookie_path:options['cookiefile']=cookie_path
    if action=='download':
        directory=Path(task['directory']);directory.mkdir(parents=True,exist_ok=True)
        def enforce_size(progress):
            if progress.get('downloaded_bytes',0)>200*1024*1024: raise MediaError('size_limit','다운로드 제한 200MB를 초과했습니다.')
        options['progress_hooks']=[enforce_size]
        options.update({'outtmpl':str(directory/'video.%(ext)s'),'format':'best[ext=mp4]/best','max_filesize':200*1024*1024,'overwrites':False,'restrictfilenames':True})
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info=downloader.extract_info(task['url'],download=action=='download')
        if action=='inspect':return {'videos':[normalize_yt(info,platform)]}
        files=[p for p in directory.iterdir() if p.is_file() and p.suffix in ('.mp4','.webm','.mov') and p.stat().st_size>0]
        if not files:raise MediaError('download_incomplete','다운로드가 완료된 영상 파일을 확인하지 못했습니다.')
        return {'filename':files[0].name,'bytes':files[0].stat().st_size,'videos':[normalize_yt(info,platform)]}
    except yt_dlp.utils.DownloadError as exc:
        text=str(exc).lower()
        if 'tunnel connection failed: 403' in text:raise MediaError('network_blocked','플랫폼 또는 영상 CDN 도메인이 클라우드 네트워크에서 차단되었습니다.') from None
        if any(word in text for word in ['cookie','log in','login','sign in']):raise MediaError('login_required','플랫폼이 로그인 쿠키를 요구합니다. 본인 계정의 쿠키 파일을 보안 경로에 설정한 뒤 재시도하세요. 채팅에 쿠키를 보내지 마세요.') from None
        raise MediaError('platform_error','영상 수집에 실패했습니다. 비공개·삭제·플랫폼 검증·추출기 호환성을 확인하세요.') from None

if __name__=='__main__':
    import signal
    def time_limit(signum,frame): raise MediaError('timeout','작업 시간 제한을 초과했습니다. 부분 다운로드를 정상 완료로 처리하지 않았습니다.')
    for stream in (sys.stdin,sys.stdout,sys.stderr):  # Windows defaults to a legacy code page
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8',errors='replace')
    if hasattr(signal,'SIGALRM'):  # Not on Windows; app.py's subprocess timeout still applies there.
        signal.signal(signal.SIGALRM,time_limit)
        signal.alarm(235)
    output={}
    try:
        task=json.loads(sys.stdin.read())
        with contextlib.redirect_stdout(sys.stderr):output={'ok':True,'result':run(task)}
    except MediaError as exc:output={'ok':False,'code':exc.code,'message':exc.message}
    except Exception:output={'ok':False,'code':'tool_error','message':'수집 도구가 실행되지 않았습니다. 설치 상태와 로그인·플랫폼 접근 조건을 확인하세요.'}
    print(json.dumps(output,ensure_ascii=False))
