"""Local, explicit script approval and FFmpeg production workflow."""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import threading
import uuid
import zipfile
import functools
import traceback

PROJECT_ROOT = Path(os.environ.get('MFLOW_PROJECT_ROOT',Path(__file__).parent/'data/projects'))
FONT_CANDIDATES = ['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc','C:/Windows/Fonts/malgunbd.ttf','/System/Library/Fonts/AppleSDGothicNeo.ttc']
FONT = os.environ.get('MFLOW_FONT') or next((f for f in FONT_CANDIDATES if Path(f).is_file()),FONT_CANDIDATES[0])
# Demuxers we accept for uploads. Playlist formats such as HLS can make FFmpeg read other local files.
SOURCE_FORMATS = 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm'
STYLES = {'basic':{'name':'기본 스타일','title':'yellow','caption':'white'},'clean':{'name':'깔끔 자막','title':'white','caption':'white'},'red':{'name':'강렬 빨강','title':'0xff626e','caption':'yellow'},'news':{'name':'뉴스형','title':'0x73d9ff','caption':'white'}}
LOCK = threading.Lock()
CONNECT = None
CONFIG_LOCK = threading.RLock()

def serialized(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        with CONFIG_LOCK: return function(*args, **kwargs)
    return call

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()

def init(connect):
    global CONNECT
    CONNECT = connect
    with CONNECT() as con:
        con.executescript('''CREATE TABLE IF NOT EXISTS production_channels (id TEXT PRIMARY KEY,name TEXT,language TEXT,template TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS production_projects (id TEXT PRIMARY KEY,title TEXT,channel_id TEXT,status TEXT,document TEXT,created_at TEXT,updated_at TEXT);''')


def state():
    with CONNECT() as con:
        channels=[{**dict(r),'template':json.loads(r['template'])} for r in con.execute('SELECT * FROM production_channels ORDER BY created_at')]
        projects=[]
        for row in con.execute('SELECT * FROM production_projects ORDER BY updated_at DESC'):
            p=dict(row);p['document']=json.loads(p['document']);projects.append(p)
    return {'channels':channels,'projects':projects,'styles':STYLES,'render_available':bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(FONT).is_file())}


def project(project_id):
    with CONNECT() as con: row=con.execute('SELECT * FROM production_projects WHERE id=?',(project_id,)).fetchone()
    if not row: raise ValueError('프로젝트를 찾을 수 없습니다.')
    p=dict(row);p['document']=json.loads(p['document']);return p


def store(p):
    with CONNECT() as con: con.execute('UPDATE production_projects SET title=?,channel_id=?,status=?,document=?,updated_at=? WHERE id=?',(p['title'],p['channel_id'],p['status'],json.dumps(p['document'],ensure_ascii=False),now(),p['id']))


def default_settings():return {'style':'basic','duration':58,'start':0,'mute':False,'mirror':False,'captions':True}


def settings(data):
    result={**default_settings(),**{k:v for k,v in data.items() if k in default_settings()}}
    try:result['duration']=float(result['duration']);result['start']=float(result['start'])
    except (ValueError,TypeError):raise ValueError('영상 길이와 시작 시간을 확인하세요.') from None
    if not math.isfinite(result['duration']) or not 1<=result['duration']<=60 or not math.isfinite(result['start']) or not 0<=result['start']<=5400:raise ValueError('길이는 1~60초, 시작은 0~5400초입니다.')
    if result['style'] not in STYLES or any(type(result[k]) is not bool for k in ('mute','mirror','captions')):raise ValueError('스타일 설정을 확인하세요.')
    return result


@serialized
def create_channel(data):
    name=str(data.get('name','')).strip();language=data.get('language','ko')
    if not name or len(name)>60 or language not in ('ko','en','zh'):raise ValueError('채널 이름(60자 이하)과 언어를 확인하세요.')
    ident=uuid.uuid4().hex
    with CONNECT() as con:con.execute('INSERT INTO production_channels VALUES (?,?,?,?,?)',(ident,name,language,json.dumps(default_settings()),now()))
    return ident


@serialized
def create_project(data):
    title=str(data.get('title','')).strip();channel_id=str(data.get('channel_id',''))
    if not title or len(title)>100:raise ValueError('프로젝트 이름은 1~100자입니다.')
    with CONNECT() as con: channel=con.execute('SELECT * FROM production_channels WHERE id=?',(channel_id,)).fetchone()
    if not channel:raise ValueError('제작 채널을 먼저 등록하세요.')
    ident=uuid.uuid4().hex
    headline=title[:60]
    while len(textwrap.wrap(headline,18))>3:headline=headline[:-1].rstrip()
    doc={'settings':settings(json.loads(channel['template'])),'script':'','headline':headline,'source':None,'approved_hash':None,'message':'원본 영상을 업로드하세요.','source_url':'','context':''}
    if data.get('source_video_id'):
        with CONNECT() as con:
            video=con.execute('SELECT url FROM videos WHERE id=?',(str(data['source_video_id']),)).fetchone()
            note=con.execute('SELECT notes,attribution FROM research WHERE video_id=?',(str(data['source_video_id']),)).fetchone()
        if not video:raise ValueError('발굴한 영상을 찾을 수 없습니다.')
        doc['source_url']=video['url']
        doc['context']=((note['attribution']+'\n'+note['notes']).strip() if note else '')[:2000]
        doc['source_video_id']=str(data['source_video_id'])
    with CONNECT() as con:con.execute('INSERT INTO production_projects VALUES (?,?,?,?,?,?,?)',(ident,title,channel_id,'draft',json.dumps(doc,ensure_ascii=False),now(),now()))
    return ident


@serialized
def update_project(data):
    p=project(str(data.get('id','')))
    if p['status']=='rendering':raise ValueError('렌더링 중에는 설정을 변경할 수 없습니다.')
    doc=p['document'];title=str(data.get('title',p['title'])).strip()
    script=str(data.get('script',doc['script'])).strip();headline=str(data.get('headline',doc['headline'])).strip()
    context=str(data.get('context',doc.get('context',''))).strip()
    source_url=str(data.get('source_url',doc.get('source_url',''))).strip()
    if len(textwrap.wrap(headline,18))>3:raise ValueError('상단 제목은 화면에서 3줄(약 50자) 이내여야 합니다. 줄여서 입력하세요.')
    if not title or len(title)>100 or not headline or len(headline)>60 or len(script)>800 or len(context)>2000 or len(source_url)>2048:raise ValueError('이름 100자·상단 제목 60자·대본 800자·맥락 메모 2000자 이내로 입력하세요.')
    if source_url:
        from urllib.parse import urlsplit
        u=urlsplit(source_url)
        if u.scheme!='https' or not u.hostname or u.username or u.password:raise ValueError('출처는 HTTPS 링크로 입력하세요.')
    p['title']=title;doc.update({'script':script,'headline':headline,'context':context,'source_url':source_url,'settings':settings(data.get('settings',doc['settings'])),'approved_hash':None})
    doc.pop('output',None);doc.pop('archive',None)
    p['status']='script_ready' if script and doc['source'] else 'source_ready' if doc['source'] else 'draft'
    doc['message']='대본과 설정을 검토한 뒤 승인하세요.' if p['status']=='script_ready' else '원본과 대본을 준비하세요.'
    store(p)
    if data.get('save_template'):
        with CONNECT() as con:con.execute('UPDATE production_channels SET template=? WHERE id=?',(json.dumps(doc['settings']),p['channel_id']))


def probe(path):
    try:
        result=subprocess.run(['ffprobe','-v','error','-format_whitelist',SOURCE_FORMATS,'-protocol_whitelist','file','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,timeout=20,check=True)
        info=json.loads(result.stdout);duration=float(info['format']['duration'])
        if not set(info['format'].get('format_name','').split(','))&set(SOURCE_FORMATS.split(',')):raise ValueError('unsupported format')
        stream=next(s for s in info['streams'] if s.get('codec_type')=='video')
        if not math.isfinite(duration) or not 0<duration<=5400 or not 0<int(stream['width'])<=7680 or not 0<int(stream['height'])<=7680:raise ValueError('unsupported')
        return {'duration':round(duration,3),'width':stream['width'],'height':stream['height'],'audio':any(s.get('codec_type')=='audio' for s in info['streams'])}
    except (OSError,ValueError,KeyError,StopIteration,subprocess.SubprocessError):raise ValueError('유효한 영상이 아닙니다. 최대 90분·8K 이하의 MP4/MOV/WebM 영상을 사용하세요.') from None


def upload(project_id, reader, size):
    # Receiving up to 200MB and probing it can be slow, so only the final save holds CONFIG_LOCK.
    p=project(project_id)
    if p['status']=='rendering':raise ValueError('렌더링 중에는 원본을 바꿀 수 없습니다.')
    if not 0<size<=200*1024*1024:raise ValueError('업로드는 200MB 이하의 영상만 지원합니다.')
    directory=PROJECT_ROOT/p['id'];directory.mkdir(parents=True,exist_ok=True)
    path=directory/(uuid.uuid4().hex+'.source')
    try:
        with path.open('xb') as out:
            remaining=size
            while remaining:
                chunk=reader.read(min(1024*1024,remaining))
                if not chunk:raise ValueError('영상 업로드가 중단되었습니다.')
                out.write(chunk);remaining-=len(chunk)
        info=probe(path)
        return save_source(project_id,path,info)
    except BaseException:
        path.unlink(missing_ok=True);raise


@serialized
def save_source(project_id,path,info):
    p=project(project_id)
    if p['status']=='rendering':raise ValueError('렌더링 중에는 원본을 바꿀 수 없습니다.')
    p['document'].update({'source':{'filename':path.name,**info},'approved_hash':None,'message':'원본 업로드 완료. 스타일과 대본을 준비하세요.'})
    p['document'].pop('output',None);p['document'].pop('archive',None)
    p['status']='script_ready' if p['document']['script'] else 'source_ready';store(p)
    return info


def fingerprint(doc):
    return hashlib.sha256(json.dumps({k:doc.get(k) for k in ('source','script','headline','settings')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


@serialized
def approve(project_id):
    p=project(project_id);doc=p['document']
    if p['status']=='rendering' or not doc.get('source') or not doc.get('script'):raise ValueError('원본과 대본을 준비한 뒤 승인하세요.')
    if doc['settings']['start']>=doc['source']['duration']:raise ValueError('시작 시간이 원본 길이보다 깁니다.')
    doc['approved_hash']=fingerprint(doc);doc['message']='대본과 설정이 승인되었습니다. 렌더링을 시작할 수 있습니다.';p['status']='approved';store(p)


@serialized
def start_render(project_id):
    p=project(project_id);doc=p['document']
    if p['status'] not in ('approved','completed','error') or not doc.get('approved_hash') or doc['approved_hash']!=fingerprint(doc):raise ValueError('현재 대본과 설정을 먼저 승인하세요.')
    if not state()['render_available']:raise ValueError('FFmpeg/FFprobe와 한국어 글꼴 설치가 필요합니다.')
    if not LOCK.acquire(blocking=False):raise ValueError('다른 영상이 렌더링 중입니다. 완료 후 다시 시도하세요.')
    p['status']='rendering';doc['message']='세로 영상과 제목·대본 자막을 렌더링하고 있습니다.';store(p)
    threading.Thread(target=render,args=(p,),daemon=True).start()


def filter_path(path):
    """Quote a file path for an FFmpeg filter option (handles Windows drive colons and backslashes)."""
    value=str(path).replace('\\','/')
    if "'" in value:raise ValueError('파일 경로에 작은따옴표가 있으면 렌더링할 수 없습니다.')
    return "'"+value.replace(':','\\:')+"'"


def srt_time(seconds):
    ms=round(seconds*1000);hours,ms=divmod(ms,3600000);minutes,ms=divmod(ms,60000);seconds,ms=divmod(ms,1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def render(p):
    doc=p['document'];directory=PROJECT_ROOT/p['id'];run=directory/uuid.uuid4().hex
    try:
        run.mkdir(parents=True)
        source=directory/doc['source']['filename'];cfg=doc['settings'];duration=min(cfg['duration'],doc['source']['duration']-cfg['start'])
        style=STYLES[cfg['style']];title=run/'headline.txt';title.write_text('\n'.join(textwrap.wrap(doc['headline'],18)),encoding='utf-8')
        vf=['scale=720:960:force_original_aspect_ratio=decrease','pad=720:1280:(ow-iw)/2:(oh-ih)/2:black','setsar=1']
        if cfg['mirror']:vf.append('hflip')
        vf.extend(['drawbox=x=0:y=0:w=iw:h=230:color=black:t=fill',f"drawtext=fontfile={filter_path(FONT)}:textfile={filter_path(title)}:expansion=none:fontcolor={style['title']}:fontsize=38:x=(w-text_w)/2:y=65:line_spacing=10"])
        chunks=textwrap.wrap(re.sub(r'\s+',' ',doc['script']),38)
        srt=[]
        for index,chunk in enumerate(chunks):
            start=duration*index/len(chunks);end=duration*(index+1)/len(chunks)
            srt.extend([str(index+1),f'{srt_time(start)} --> {srt_time(end)}','\n'.join(textwrap.wrap(chunk,19)),''])
            if cfg['captions']:
                text=run/f'caption-{index}.txt';text.write_text('\n'.join(textwrap.wrap(chunk,19)),encoding='utf-8')
                vf.append(f"drawtext=fontfile={filter_path(FONT)}:textfile={filter_path(text)}:expansion=none:fontcolor={style['caption']}:fontsize=30:box=1:boxcolor=black@0.8:boxborderw=12:x=(w-text_w)/2:y=1080:line_spacing=8:enable='gte(t,{start:.6f})*lt(t,{end:.6f})'")
        (run/'captions.srt').write_text('\n'.join(srt),encoding='utf-8')
        output=run/'short.mp4'
        command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(cfg['start']),'-threads','2','-format_whitelist',SOURCE_FORMATS,'-protocol_whitelist','file','-i',str(source),'-t',str(duration),'-map','0:v:0']
        if not cfg['mute']:command+=['-map','0:a:0?']
        command+=['-vf',','.join(vf),'-c:v','libx264','-preset','veryfast','-crf','23','-threads','2','-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k','-movflags','+faststart',str(output)]
        subprocess.run(command,capture_output=True,timeout=240,check=True)
        result=probe(output)
        if result['width']!=720 or result['height']!=1280:raise ValueError('render size')
        manifest={'version':1,'title':p['title'],'headline':doc['headline'],'script':doc['script'],'settings':cfg,'source_url':doc.get('source_url',''),'context':doc.get('context',''),'duration':duration,'caption_timing':'equal segments; not speech aligned','narration':'original audio only; no synthetic voice','approved_hash':doc['approved_hash']}
        (run/'project.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(run/'project.zip','w',compression=zipfile.ZIP_STORED) as archive:
            for name in ('short.mp4','captions.srt','project.json'):archive.write(run/name,name)
        doc.update({'output':str(output.relative_to(directory)),'archive':str((run/'project.zip').relative_to(directory)),'message':'MP4와 자막·프로젝트 설정 묶음 제작이 완료되었습니다. 원음 유지 방식이며 합성 음성은 없습니다.','render_info':result})
        p['status']='completed';store(p)
    except Exception as exc:
        if not isinstance(exc,(ValueError,OSError,subprocess.SubprocessError)):traceback.print_exc()
        shutil.rmtree(run,ignore_errors=True)
        p['status']='error';doc['message']='렌더링에 실패했습니다. 원본 형식과 시작 시간·길이를 확인하고 다시 시도하세요.'
        try:store(p)
        except Exception:traceback.print_exc()
    finally:LOCK.release()


def asset(project_id,kind):
    p=project(project_id);doc=p['document'];directory=(PROJECT_ROOT/p['id']).resolve()
    if kind=='source':relative=(doc.get('source') or {}).get('filename')
    elif kind in ('output','archive'):relative=doc.get(kind) if p['status']=='completed' else None
    else:relative=None
    if not relative:raise ValueError('파일이 아직 준비되지 않았습니다.')
    path=(directory/relative).resolve()
    if not path.is_relative_to(directory) or not path.is_file():raise ValueError('파일을 찾을 수 없습니다.')
    return path
