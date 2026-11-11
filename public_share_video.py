"""Read the requested post and its public player in an isolated Playwright session."""
import os
import math
from pathlib import Path
import re
import time
from urllib import error, parse, request

import share_import


CDNS = {
    'Douyin': ('douyinvod.com','zjcdn.com','amemv.com'),
    'Kuaishou': ('kwaicdn.com','kwimgs.com','yximgs.com'),
    'Xiaohongshu': ('xhscdn.com',),
}
MAX_BYTES = 200 * 1024 * 1024


def media_url(platform,url):
    """Accept HTTPS media hosts only. Static placeholders are deliberately excluded."""
    if not isinstance(url,str) or len(url)>8192 or any(ord(c)<33 or c=='\\' for c in url):
        return False
    try:
        p=parse.urlsplit(url)
        host=p.hostname or ''
        return (p.scheme=='https' and p.username is None and p.password is None and p.port is None
                and any(host.endswith('.'+domain) for domain in CDNS.get(platform,())))
    except ValueError:
        return False


def post_info(platform,post_id,payload):
    """Use structured first-party post details, never related or recommended videos."""
    if not isinstance(payload,dict):
        return None
    if platform=='Douyin':
        post=payload.get('aweme_detail')
        if not isinstance(post,dict) or str(post.get('aweme_id'))!=post_id:
            return None
        author=post.get('author') or {}
        duration=(post.get('video') or {}).get('duration')
        return {'title':post.get('desc'),'author':author.get('nickname'),'author_id':author.get('sec_uid'),
                'duration':duration/1000 if isinstance(duration,(int,float)) and math.isfinite(duration) and duration>0 else None,
                'is_video':bool(post.get('video')) and not post.get('images')}
    if platform=='Kuaishou':
        data=payload.get('data') or {}
        detail=data.get('visionVideoDetail') or {}
        post=detail.get('photo') or {}
        if not isinstance(post,dict) or str(post.get('id') or post.get('photoId'))!=post_id:
            return None
        author=detail.get('author') or post.get('author') or {}
        duration=post.get('duration')
        return {'title':post.get('caption'),'author':author.get('name') or author.get('userName'),
                'author_id':author.get('id'),'is_video':not bool(post.get('imageUrls')),
                'duration':duration/1000 if isinstance(duration,(int,float)) and math.isfinite(duration) and duration>0 else None}
    if platform=='Xiaohongshu':
        post=payload.get('note')
        if not isinstance(post,dict) or post.get('noteId')!=post_id:
            return None
        author=post.get('user') or {}
        return {'title':post.get('title') or post.get('desc'),'author':author.get('nickname'),
                'author_id':author.get('userId'),'is_video':post.get('type')=='video'}
    return None


def same_post(platform,url,post_id):
    try:
        found,canonical,short=share_import.classify(url)
        return found==platform and not short and parse.urlsplit(canonical).path.split('/')[-1]==post_id
    except (ValueError,TypeError):
        return False


def browser_details(platform,url):
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        return None,None,'공개 페이지용 브라우저 도구를 설치해야 합니다.'
    post_id=parse.urlsplit(url).path.split('/')[-1]
    info=None
    visible_title=''
    launch={'headless':True}
    chromium=os.environ.get('CHROMIUM_PATH')
    if chromium:
        launch['executable_path']=chromium
    elif os.name=='nt':
        chrome=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Google/Chrome/Application/chrome.exe'
        if chrome.is_file():
            launch['executable_path']=str(chrome)
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(**launch)
            try:
                page=browser.new_page()

                def response(received):
                    nonlocal info
                    endpoint=parse.urlsplit(received.url)
                    expected={'Douyin':'/aweme/v1/web/aweme/detail/','Kuaishou':'/graphql'}.get(platform)
                    if (info or not expected or endpoint.path!=expected or
                            endpoint.hostname not in share_import.HOSTS[platform] or not received.ok or
                            not same_post(platform,page.url,post_id)):
                        return
                    try:
                        length=int(received.headers.get('content-length','0'))
                        if 0<=length<=4*1024*1024:
                            info=post_info(platform,post_id,received.json()) or info
                    except (Error,ValueError,TypeError,AttributeError):
                        pass

                page.on('response',response)
                try:
                    page.goto(url,wait_until='domcontentloaded',timeout=18000)
                except (Error,TimeoutError):
                    if not same_post(platform,page.url,post_id):
                        raise
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    page.wait_for_timeout(250)
                    if not same_post(platform,page.url,post_id):
                        continue
                    try:
                        data=page.evaluate("""({id,platform}) => {
                            const result = {
                                title: document.title,
                                agent: navigator.userAgent,
                                videos: [...document.querySelectorAll('video')].filter(v =>
                                    v.readyState >= 1 && v.videoWidth > 0 && Number.isFinite(v.duration) &&
                                    v.getBoundingClientRect().width >= 100).map(v => [v.currentSrc,v.duration])
                            };
                            if (platform === 'Xiaohongshu') {
                                const note=window.__INITIAL_STATE__?.note?.noteDetailMap?.[id]?.note;
                                if (note) result.note={noteId:note.noteId,title:note.title,desc:note.desc,type:note.type,
                                    user:{nickname:note.user?.nickname,userId:note.user?.userId}};
                            }
                            if (platform === 'Kuaishou') {
                                const db=window.__APOLLO_STATE__?.defaultClient;
                                const photo=db?.['VisionVideoDetailPhoto:'+id];
                                const ref=db?.ROOT_QUERY?.['visionVideoDetail('+JSON.stringify({page:'detail',photoId:id})+')'];
                                const detail=db?.[ref?.__ref||ref?.id];
                                const author=db?.[detail?.author?.__ref||detail?.author?.id];
                                if (photo) result.data={visionVideoDetail:{
                                    photo:{id:photo.id,caption:photo.caption,duration:photo.duration,imageUrls:photo.imageUrls},
                                    author:{id:author?.id,name:author?.name}
                                }};
                            }
                            return result;
                        }""",{'id':post_id,'platform':platform})
                    except Error:
                        # The public page can reload while bootstrapping its player.
                        continue
                    if platform in ('Xiaohongshu','Kuaishou'):
                        observed=post_info(platform,post_id,data)
                        if observed:
                            info={**(info or {}),**{key:value for key,value in observed.items() if value is not None}}
                    if platform=='Kuaishou' and data['title'].endswith('-快手'):
                        visible_title=data['title'].removesuffix('-快手').strip()
                    if info and not info['is_video']:
                        return info,None,'재생 가능한 영상 게시물만 가져올 수 있습니다.'
                    expected_duration=info.get('duration') if info else None
                    urls={value for value,duration in data['videos'] if media_url(platform,value) and
                          (not expected_duration or abs(duration-expected_duration)<2)}
                    if info and len(urls)==1:
                        return info,(urls.pop(),data['agent'],page.url),''
            finally:
                browser.close()
    except (Error,TimeoutError,OSError):
        return info,None,'격리된 브라우저에서 공개 영상 페이지를 열 수 없습니다.'
    if not info and visible_title:
        info={'title':visible_title,'is_video':None}
    return info,None,'공개 페이지에서 해당 게시물의 재생 파일을 확인하지 못했습니다. 플랫폼에서 접근을 제한할 수 있습니다.'


def download(platform, media, directory):
    url,agent,referer=media
    opener=request.build_opener(share_import.NoRedirect())
    folder=Path(directory)
    folder.mkdir(parents=True,exist_ok=True)
    for _ in range(5):
        if not media_url(platform,url):
            raise ValueError('공개 영상 서버 주소를 확인할 수 없습니다.')
        try:
            req=request.Request(url,headers={'User-Agent':agent,'Referer':referer})
            with opener.open(req,timeout=20) as response:
                content_type=response.headers.get_content_type()
                try:
                    content_length=int(response.headers.get('Content-Length','0'))
                except ValueError:
                    raise ValueError('영상 파일 크기를 확인할 수 없습니다.') from None
                if not (content_type.startswith('video/') or content_type=='application/octet-stream'):
                    raise ValueError('영상 파일 응답을 확인하지 못했습니다.')
                if content_length<0 or content_length>MAX_BYTES:
                    raise ValueError('다운로드 제한 200MB를 초과했습니다.')
                if response.status==206:
                    match=re.fullmatch(r'bytes 0-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                    if not match or int(match[1])+1!=int(match[2]) or int(match[2])!=content_length:
                        raise ValueError('플랫폼이 전체 영상 파일을 제공하지 않았습니다.')
                target=folder/'video.mp4'
                total=0
                with target.open('xb') as stream:
                    while chunk:=response.read(256*1024):
                        total+=len(chunk)
                        if total>MAX_BYTES:
                            raise ValueError('다운로드 제한 200MB를 초과했습니다.')
                        stream.write(chunk)
                if not total or content_length and total!=content_length:
                    raise ValueError('영상 파일이 끝까지 내려오지 않았습니다.')
                return target.name
        except error.HTTPError as exc:
            with exc:
                if exc.code not in (301,302,303,307,308) or not exc.headers.get('Location'):
                    raise ValueError('공개 영상 서버가 다운로드를 거절했습니다.') from None
                url=parse.urljoin(url,exc.headers['Location'])
        except (OSError,TimeoutError):
            raise ValueError('공개 영상 파일에 연결할 수 없습니다.') from None
    raise ValueError('공개 영상 서버의 이동 경로가 너무 깁니다.')
