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
import tempfile
import textwrap
import threading
import uuid
import zipfile
import functools
import traceback
import audio_mixer
import caption_drafts
import manual_timeline

PROJECT_ROOT = Path(os.environ.get('MFLOW_PROJECT_ROOT',Path(__file__).parent/'data/projects'))
FONT_CANDIDATES = ['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc','C:/Windows/Fonts/malgunbd.ttf','/System/Library/Fonts/AppleSDGothicNeo.ttc']
FONT = os.environ.get('MFLOW_FONT') or next((f for f in FONT_CANDIDATES if Path(f).is_file()),FONT_CANDIDATES[0])
# Extra fonts the user drops in this folder (.ttf/.otf/.ttc) can be picked per project for the title and captions.
# The folder is not in git because many free fonts do not allow redistribution.
# The OpenCV text eraser runs in the media venv, like the platform tools.
MEDIA_PYTHON = Path(os.environ.get('MFLOW_MEDIA_PYTHON',Path(__file__).parent/('data/media-venv/Scripts/python.exe' if os.name=='nt' else 'data/media-venv/bin/python')))
_TOOL_CACHE = {}
# video-subtitle-remover (AI, NVIDIA) unpacked by scripts\install_ai_eraser.cmd. Its OCR cannot load models from
# folders with Korean names on Windows, so the installer may put it elsewhere and write the place to data/vsr-path.txt.
def _ai_root():
    if os.environ.get('MFLOW_VSR_DIR'):return Path(os.environ['MFLOW_VSR_DIR'])
    pointer=Path(__file__).parent/'data/vsr-path.txt'
    try:return Path(pointer.read_text(encoding='utf-8-sig').strip()) if pointer.is_file() else Path(__file__).parent/'data/vsr'
    except OSError:return Path(__file__).parent/'data/vsr'
AI_ROOT = _ai_root()
FONTS_DIR = Path(os.environ.get('MFLOW_FONTS_DIR',Path(__file__).parent/'fonts'))
FONT_SUFFIXES = ('.ttf','.otf','.ttc')
_FONT_CACHE = {}
# Demuxers we accept for uploads. Playlist formats such as HLS can make FFmpeg read other local files.
SOURCE_FORMATS = 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm'
# title: first line of the headline, title2: the other lines, caption: narration text, highlight: words marked *like this* in the script.
STYLES = {'basic':{'name':'기본 스타일','title':'#ffe34a','title2':'#ff5a5a','caption':'#ffffff','highlight':'#5ac8ff'},
          'clean':{'name':'깔끔 자막','title':'#ffffff','title2':'#5ac8ff','caption':'#ffffff','highlight':'#ffffff'},
          'red':{'name':'강렬 빨강','title':'#ff4d4d','title2':'#ffffff','caption':'#ffffff','highlight':'#ff4d4d'},
          'news':{'name':'뉴스형','title':'#ffffff','title2':'#5ac8ff','caption':'#ffffff','highlight':'#5ac8ff'}}
LOCK = threading.Lock()
CONNECT = None
CONFIG_LOCK = threading.RLock()
RENDER_WAKE = threading.Event()
MAX_RENDER_ATTEMPTS = 3

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
        CREATE TABLE IF NOT EXISTS production_projects (id TEXT PRIMARY KEY,title TEXT,channel_id TEXT,status TEXT,document TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS production_recovery_drafts (
            project_id TEXT PRIMARY KEY, revision TEXT NOT NULL, snapshot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS production_render_jobs (
            project_id TEXT PRIMARY KEY, id TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL, created_at TEXT NOT NULL
        );''')
        columns={row['name'] for row in con.execute('PRAGMA table_info(production_render_jobs)')}
        if 'engine' not in columns:
            con.execute('ALTER TABLE production_render_jobs ADD COLUMN engine TEXT')
        channels={row['name'] for row in con.execute('PRAGMA table_info(production_channels)')}
        if 'narration_profile' not in channels:
            con.execute("ALTER TABLE production_channels ADD COLUMN narration_profile TEXT NOT NULL DEFAULT '{}'")


def recovery_drafts():
    with CONNECT() as con:
        rows = con.execute('SELECT snapshot FROM production_recovery_drafts')
        return [json.loads(row['snapshot']) for row in rows]


def write_recovery_draft(data):
    project_id, revision = data.get('id'), data.get('revision')
    if not isinstance(project_id, str) or not isinstance(revision, str):
        raise ValueError('복구본 식별자가 올바르지 않습니다.')
    with CONNECT() as con:
        if data.get('discard') is True:
            con.execute('DELETE FROM production_recovery_drafts WHERE project_id=? AND revision=?',
                        (project_id, revision))
            return
        if not isinstance(data.get('edit'), str) or not isinstance(data.get('at'), int):
            raise ValueError('복구본 형식이 올바르지 않습니다.')
        result = con.execute('''
            INSERT INTO production_recovery_drafts (project_id, revision, snapshot)
            SELECT id, ?, ? FROM production_projects WHERE id=?
            ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision, snapshot=excluded.snapshot
        ''', (revision, json.dumps(data, ensure_ascii=False), project_id))
        if not result.rowcount:
            raise ValueError('프로젝트를 찾을 수 없습니다.')


def state():
    with CONNECT() as con:
        channels=[{**dict(r),'template':json.loads(r['template']),
                   'narration_profile':json.loads(r['narration_profile'])}
                  for r in con.execute('SELECT * FROM production_channels ORDER BY created_at')]
        projects=[]
        for row in con.execute('SELECT * FROM production_projects ORDER BY updated_at DESC'):
            p=dict(row);p['document']=json.loads(p['document']);projects.append(p)
        for p in projects:p['comment_info']=comment_info(con,p['document'])
    return {'channels':channels,'projects':projects,'styles':STYLES,'fonts':available_fonts(),'letters_ready':letters_ready(),'ai_ready':ai_eraser() is not None,'gpu':gpu_info(),'render_available':bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(FONT).is_file())}


def comment_info(con,doc):
    """Whether the source has public comments to turn into cards ("댓글 작업 가능")."""
    from comment_picker import youtube_id
    count=None
    if doc.get('source_video_id'):
        try:row=con.execute('SELECT comments FROM videos WHERE id=?',(doc['source_video_id'],)).fetchone()
        except Exception:row=None  # discovery tables are created by app.py
        count=row[0] if row else None
    return {'count':count,'auto':bool(youtube_id(doc.get('source_url'))),'available':bool(count) or (count is None and bool(youtube_id(doc.get('source_url'))))}


def project(project_id):
    with CONNECT() as con: row=con.execute('SELECT * FROM production_projects WHERE id=?',(project_id,)).fetchone()
    if not row: raise ValueError('프로젝트를 찾을 수 없습니다.')
    p=dict(row);p['document']=json.loads(p['document']);return p


def store(p, con=None):
    if con is None:
        with CONFIG_LOCK, CONNECT() as connection:
            return store(p, connection)
    con.execute('UPDATE production_projects SET title=?,channel_id=?,status=?,document=?,updated_at=? WHERE id=?',(p['title'],p['channel_id'],p['status'],json.dumps(p['document'],ensure_ascii=False),now(),p['id']))
    if p.get('render_job_id') and p['status'] in ('approved', 'completed', 'error'):
        job_status='completed' if p['status']=='approved' else p['status']
        con.execute('UPDATE production_render_jobs SET status=? WHERE project_id=? AND id=?',
                    (job_status, p['id'], p['render_job_id']))


def default_settings():return {'style':'basic','duration':58,'start':0,'mute':False,'mirror':False,'captions':True,'layout':'band','title_font':'','caption_font':'',**{k:0 for k in OFFSETS}}
# Up/down nudges set in the editor, in 720x1280 design pixels (positive = lower). Saved with the channel template.
OFFSETS=('title_y','credit_y','caption_y','comment_y')
OFFSET_LIMIT=300


def available_fonts():
    """Fonts in FONTS_DIR as [{'file','name'}]. '' (not listed) means the default FONT."""
    result=[]
    if FONTS_DIR.is_dir():
        for path in sorted(FONTS_DIR.iterdir()):
            if path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():continue
            try:result.append({'file':path.name,'name':font_info(path)[0]})
            except (ValueError,OSError,IndexError,__import__('struct').error):continue
    return result


def font_path(name):
    if not name:return Path(FONT)
    path=FONTS_DIR/name
    if Path(name).name!=name or path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():raise ValueError('글씨체 파일을 찾을 수 없습니다.')
    return path


# 1080x1920 canvas: title band on top, caption area at the bottom, video in between.
# Every size below was designed on a 720x1280 canvas and is scaled by SCALE (1.5) so the look stays the same.
SCALE=1.5
def px(value):return round(value*SCALE)
# Usual shorts frame: black band with the title on top, video in the middle, black band at the bottom.
WIDTH,HEIGHT,TITLE_HEIGHT,VIDEO_BOTTOM=px(720),px(1280),px(300),px(1000)
# Headline sizes, tried in order: (characters per line, font size, max lines). Big two-line titles first.
TITLE_FITS=[(9,px(72),2),(12,px(54),2),(18,px(35),3)]
TITLE_OUTLINE=px(5)
# Narration captions: bold white text with a thick outline. Bottom edge near the bottom of the video.
CAPTION_FONT,CAPTION_LINE_CHARS,CAPTION_OUTLINE=px(50),12,px(6)
CAPTION_BOTTOM={'band':VIDEO_BOTTOM-px(18),'fit':VIDEO_BOTTOM-px(18),'fill':px(1160)}
# Small source/credit line right above the video, e.g. "에이티즈 - Drip / 출처. Jennifer Hudson Show".
CREDIT_FONT=px(22)
# Comment cards: white rounded card, nickname line + comment text.
COMMENT_MARGIN,COMMENT_PAD,COMMENT_NAME_FONT,COMMENT_FONT,COMMENT_LINE_CHARS=px(36),px(20),px(22),px(29),20
COMMENT_TOP={'band':VIDEO_BOTTOM+px(22),'fit':VIDEO_BOTTOM+px(22),'fill':TITLE_HEIGHT+px(24)}
LAYOUTS={'band':'위아래 검정','fill':'화면 꽉 채우기','fit':'원본 맞추기'}


def layout_filters(layout):
    """Video filters that place the source on the canvas without the title band hiding it.

    band: crop the source to fill the middle between the black title band and a black bottom band.
    fill: crop the source to cover everything below the title band.
    fit: show the whole source in the middle area on black.
    """
    area=VIDEO_BOTTOM-TITLE_HEIGHT
    if layout=='band':
        return [f'scale={WIDTH}:{area}:force_original_aspect_ratio=increase',f'crop={WIDTH}:{area}','setsar=1',f'pad={WIDTH}:{HEIGHT}:0:{TITLE_HEIGHT}:black']
    if layout=='fit':
        return [f'scale={WIDTH}:{area}:force_original_aspect_ratio=decrease','setsar=1',f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:{TITLE_HEIGHT}+({area}-ih)/2:black']
    area=HEIGHT-TITLE_HEIGHT
    return [f'scale={WIDTH}:{area}:force_original_aspect_ratio=increase',f'crop={WIDTH}:{area}','setsar=1',f'pad={WIDTH}:{HEIGHT}:0:{TITLE_HEIGHT}:black']


def grade_filters(grade):
    if not grade:
        return []
    contrast=1+grade['contrast']/100
    brightness=grade['brightness']/100
    warmth=grade['warmth']/100
    channels=':'.join(f"{key}='clip((val-128)*{contrast:.6f}+128+{offset*255:.6f},0,255)'"
                      for key,offset in zip('rgb',(brightness+warmth,brightness,brightness-warmth)))
    saturation=1+grade['saturation']/100
    weights=(0.2126,0.7152,0.0722)
    matrix=':'.join(f'{a}{b}={weights[j]*(1-saturation)+(saturation if i==j else 0):.6f}'
                    for i,a in enumerate('rgb') for j,b in enumerate('rgb'))
    return ['format=rgb24','lutrgb='+channels,'colorchannelmixer='+matrix,'format=yuv420p']


def clip_layout_filters(cfg, cut):
    layout = cfg.get('layout', 'fill')
    frame = cut.get('framing')
    mirror = ['hflip'] if cfg['mirror'] else []
    grade = grade_filters(cut.get('grade'))
    if not frame or frame == {'mode': 'default', 'zoom': 1, 'x': 0, 'y': 0}:
        return grade + layout_filters(layout) + mirror

    area = (HEIGHT if layout == 'fill' else VIDEO_BOTTOM) - TITLE_HEIGHT
    fit = frame['mode'] == 'fit' or (frame['mode'] == 'default' and layout == 'fit')
    bound = 'min' if fit else 'max'
    display_width = 'iw*if(gt(sar,0),sar,1)'
    factor = f'{frame["zoom"]}*{bound}({WIDTH}/({display_width}),{area}/ih)'
    # Apply mirroring before translation so dragging right always moves the picture right.
    x, y = round(frame['x']), round(frame['y'])
    padded_w, padded_h = f'max(iw,{WIDTH})+{2*abs(x)}', f'max(ih,{area})+{2*abs(y)}'
    return grade + mirror + [
        f"scale=w='max(2,round({display_width}*{factor}))':h='max(2,round(ih*{factor}))'",
        'setsar=1', 'format=yuv444p',
        f"pad=w='{padded_w}':h='{padded_h}':x=floor((ow-iw)/2):y=floor((oh-ih)/2):color=black",
        f'crop={WIDTH}:{area}:ceil((iw-ow)/2)-({x}):ceil((ih-oh)/2)-({y})',
        f'pad={WIDTH}:{HEIGHT}:0:{TITLE_HEIGHT}:black',
        'format=yuv420p',
    ]


def settings(data):
    result={**default_settings(),**{k:v for k,v in data.items() if k in default_settings()}}
    try:result['duration']=float(result['duration']);result['start']=float(result['start'])
    except (ValueError,TypeError):raise ValueError('영상 길이와 시작 시간을 확인하세요.') from None
    if not math.isfinite(result['duration']) or not 1<=result['duration']<=60 or not math.isfinite(result['start']) or not 0<=result['start']<=5400:raise ValueError('길이는 1~60초, 시작은 0~5400초입니다.')
    fonts={f['file'] for f in available_fonts()}
    if any(not isinstance(result[k],str) or (result[k] and result[k] not in fonts) for k in ('title_font','caption_font')):raise ValueError('글씨체를 다시 선택하세요. fonts 폴더에 파일이 있어야 합니다.')
    try:
        for k in OFFSETS:
            result[k]=int(round(float(result[k])))
            if not -OFFSET_LIMIT<=result[k]<=OFFSET_LIMIT:raise ValueError
    except (ValueError,TypeError,OverflowError):raise ValueError(f'위치 조정은 -{OFFSET_LIMIT}~{OFFSET_LIMIT} 사이로 입력하세요.') from None
    if result['style'] not in STYLES or result['layout'] not in LAYOUTS or any(type(result[k]) is not bool for k in ('mute','mirror','captions')):raise ValueError('스타일 설정을 확인하세요.')
    return result


@serialized
def create_channel(data):
    name=str(data.get('name','')).strip();language=data.get('language','ko')
    if not name or len(name)>60 or language not in ('ko','en','zh'):raise ValueError('채널 이름(60자 이하)과 언어를 확인하세요.')
    ident=uuid.uuid4().hex
    with CONNECT() as con:
        con.execute('INSERT INTO production_channels (id,name,language,template,created_at) VALUES (?,?,?,?,?)',
                    (ident,name,language,json.dumps(default_settings()),now()))
    return ident


def shown_credit(doc):
    """The credit text that goes on screen; empty when the user switched it off."""
    return doc.get('credit','') if doc.get('credit_on',True) else ''


def credit_line(channel):
    """Default credit under the title: '출처. 채널명' (60 characters at most)."""
    name=re.sub(r'\s+',' ',str(channel or '')).strip()
    return ('출처. '+name)[:60].rstrip() if name and name not in ('작성자 미제공',) else ''


@serialized
def create_project(data, share_record=None):
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
            video=con.execute('SELECT url,channel FROM videos WHERE id=?',(str(data['source_video_id']),)).fetchone()
            note=con.execute('SELECT notes,attribution FROM research WHERE video_id=?',(str(data['source_video_id']),)).fetchone()
        if not video:raise ValueError('발굴한 영상을 찾을 수 없습니다.')
        doc['source_url']=video['url']
        doc['credit']=credit_line(video['channel'])
        doc['context']=((note['attribution']+'\n'+note['notes']).strip() if note else '')[:2000]
        doc['source_video_id']=str(data['source_video_id'])
    if share_record is not None:
        doc['share_import']=share_record
        doc['source_url']=share_record['original_url'] or share_record['share_url']
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
    credit=re.sub(r'\s+',' ',str(data.get('credit',doc.get('credit','')))).strip();cards=comments(data.get('comments',doc.get('comments',[])));boxes=blurs(data.get('blurs',doc.get('blurs',[])));timeline=cues(data.get('cues',doc.get('cues',[])))
    overlays=text_overlays(data.get('overlays',doc.get('overlays',[])))
    cuts=manual_timeline.clips(data.get('clips',doc.get('clips',[])),(doc.get('source') or {}).get('duration',0))
    quotes=manual_timeline.quotes(data.get('quotes',doc.get('quotes',[])))
    mix=data.get('audio_mix',doc.get('audio_mix'))
    if mix is not None:mix=audio_mixer.settings(mix,audio_directory(p))
    if len(credit)>60:raise ValueError('출처 표시는 60자 이내로 입력하세요.')
    if len(textwrap.wrap(headline,18))>3:raise ValueError('상단 제목은 화면에서 3줄(약 50자) 이내여야 합니다. 줄여서 입력하세요.')
    if not title or len(title)>100 or not headline or len(headline)>60 or len(script)>800 or len(context)>2000 or len(source_url)>2048:raise ValueError('이름 100자·상단 제목 60자·대본 800자·맥락 메모 2000자 이내로 입력하세요.')
    if source_url:
        from urllib.parse import urlsplit
        u=urlsplit(source_url)
        if u.scheme!='https' or not u.hostname or u.username or u.password:raise ValueError('출처는 HTTPS 링크로 입력하세요.')
    changes={'script':script,'headline':headline,'context':context,'source_url':source_url,
             'credit':credit,'credit_on':bool(data.get('credit_on',doc.get('credit_on',True))),
             'comments':cards,'blurs':boxes,'cues':timeline,'overlays':overlays,'clips':cuts,
             'quotes':quotes,'audio_mix':mix,'settings':settings(data.get('settings',doc['settings']))}
    previous={key:doc.get(key) for key in changes}
    for key in ('comments','blurs','cues','overlays','clips','quotes'):
        previous[key]=doc.get(key,[])
    for key in ('context','source_url','credit'):
        previous[key]=doc.get(key,'')
    previous.update(credit_on=doc.get('credit_on',True),settings={**default_settings(),**doc['settings']})
    changed=title!=p['title'] or changes!=previous
    if changed:
        if (mix or doc.get('audio_mix')) and current_voice(doc):
            doc['narration']['mixer_base_hash']=fingerprint(doc, include_mix=False)
        if script != doc['script']:
            doc.pop('narration',None)
        for key in ('qa_report','publish_approval','output','archive'):
            doc.pop(key,None)
        bundle_voice=doc.get('bundle_narration')
        if bundle_voice and any(changes[key]!=previous[key] for key in ('script','cues','clips','audio_mix')):
            doc.pop('bundle_narration',None)
            track=(mix or {}).get('narration',{})
            if any(changes[key]!=previous[key] for key in ('script','cues','clips')) and (
                    track.get('clip') or {}).get('id')==bundle_voice['clip_id']:
                track.pop('clip')
        p['title']=title
        doc.update(changes,approved_hash=None)
        if mix is None:doc.pop('audio_mix',None)
        p['status']='script_ready' if (script or cuts or mix) and doc['source'] else 'source_ready' if doc['source'] else 'draft'
        doc['message']='대본과 설정을 검토한 뒤 승인하세요.' if p['status']=='script_ready' else '원본과 대본을 준비하세요.'
        store(p)
    if data.get('save_template'):
        with CONNECT() as con:con.execute('UPDATE production_channels SET template=? WHERE id=?',(json.dumps(changes['settings']),p['channel_id']))
    return changed


def probe(path):
    try:
        result=subprocess.run(['ffprobe','-v','error','-format_whitelist',SOURCE_FORMATS,'-protocol_whitelist','file','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,check=True)
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
    apply_source(p,path,info)
    record=p['document'].get('share_import')
    if record and record['status'] not in ('queued','running'):
        record.update(status='manual',message='직접 올린 원본으로 편집할 수 있습니다.')
    store(p)
    return info


def apply_source(p,path,info):
    bundle_voice=p['document'].pop('bundle_narration',None)
    track=(p['document'].get('audio_mix') or {}).get('narration',{})
    if bundle_voice and (track.get('clip') or {}).get('id')==bundle_voice['clip_id']:
        track.pop('clip')
    p['document'].update({'source':{'filename':path.name,**info},'approved_hash':None,'message':'원본 업로드 완료. 스타일과 대본을 준비하세요.'})
    p['document'].pop('output',None);p['document'].pop('archive',None)
    if p['document'].get('generated_script'):p['document']['script']=''
    for key in ('analysis','generated_script','narration','qa_report','publish_approval','clips','quotes'):p['document'].pop(key,None)
    p['status']='script_ready' if p['document']['script'] else 'source_ready'


def audio_directory(p):
    return PROJECT_ROOT/p['id']/'audio'


def upload_audio(project_id, reader, size):
    p=project(project_id)
    if p['status']=='rendering':raise ValueError('렌더링 중에는 오디오를 바꿀 수 없습니다.')
    return audio_mixer.upload(audio_directory(p),reader,size)


def fingerprint(doc, *, include_mix=True):
    # credit/comments only count when set, so projects approved before they existed stay approved.
    data={k:doc.get(k) for k in ('source','script','headline','settings')}
    data.update({k:doc[k] for k in ('analysis','generated_script') if doc.get(k)})
    data.update({k:doc[k] for k in ('credit','comments','blurs','cues','overlays','clips','quotes') if doc.get(k)})
    if include_mix and doc.get('audio_mix'):data['audio_mix']=doc['audio_mix']
    if not include_mix:data['settings']={key:value for key,value in doc['settings'].items() if key!='mute'}
    if doc.get('credit_on') is False:data['credit_on']=False
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def current_voice(doc):
    voice=doc.get('narration')
    if voice and ((doc.get('approved_hash') and voice.get('approved_hash')==doc['approved_hash']) or
                  voice.get('mixer_base_hash')==fingerprint(doc,include_mix=False)):
        return voice
    return None


def uploaded_voice(doc):
    return (doc.get('audio_mix') or {}).get('narration',{}).get('clip')


OVERLAY_STYLE = {'color':'#ffffff','outline_color':'#000000','outline_width':3,
                 'background_color':'#000000','background_opacity':0}


def overlay_style(value):
    if not isinstance(value,dict):
        raise ValueError('글귀 스타일을 확인하세요.')
    style={**OVERLAY_STYLE}
    for key in ('color','outline_color','background_color'):
        color=value.get(key,style[key])
        if not isinstance(color,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',color):
            raise ValueError('글귀 색상은 6자리 HEX 색상으로 입력하세요.')
        style[key]=color.lower()
    for key,maximum in (('outline_width',20),('background_opacity',100)):
        number=value.get(key,style[key])
        if type(number) not in (int,float) or not 0<=number<=maximum:
            raise ValueError('외곽선은 0~20px, 배경 불투명도는 0~100%로 입력하세요.')
        style[key]=round(number)
    return style


def text_overlays(value):
    if not isinstance(value,list) or len(value)>40:
        raise ValueError('글귀는 최대 40개까지 추가할 수 있습니다.')
    fonts={font['file'] for font in available_fonts()} if value else set()
    result=[]
    for item in value:
        if not isinstance(item,dict) or not isinstance(item.get('text'),str):
            raise ValueError('글귀 내용을 입력하세요.')
        text=' '.join(item['text'].split())
        if not 1<=len(text)<=60:
            raise ValueError('글귀는 1~60자로 입력하세요.')
        try:
            x,y,size,start,end=(float(item[k]) for k in ('x','y','size','start','end'))
        except (KeyError,TypeError,ValueError,OverflowError):
            raise ValueError('글귀 위치·크기·시간을 숫자로 입력하세요.') from None
        if not (0<=x<=1 and 0<=y<=1 and 24<=size<=144):
            raise ValueError('글귀 위치와 크기(24~144px)를 확인하세요.')
        if not (0<=start<=60 and 0<=end<=60) or (end and round(end,2)<=round(start,2)):
            raise ValueError('글귀 시간은 0~60초이며 끝은 시작보다 뒤여야 합니다(0이면 영상 끝까지).')
        font=item.get('font','')
        if not isinstance(font,str) or (font and font not in fonts):
            raise ValueError('글귀 서체를 다시 선택하세요.')
        overlay={'text':text,'x':round(x,4),'y':round(y,4),'size':round(size),
                 'start':round(start,2),'end':round(end,2),'font':font}
        if 'style' in item:
            overlay['style']=overlay_style(item['style'])
        if item.get('fixed') is True:
            overlay['fixed']=True
        result.append(overlay)
    return result


MAX_BLURS=6
def blurs(value):
    """Validate blur boxes that hide text burned into the source: {'x','y','w','h'} as 0~1 of the 1080x1920 frame, 'start'/'end' seconds (end 0 = to the end)."""
    if not isinstance(value,list) or len(value)>MAX_BLURS:raise ValueError(f'블러 박스는 최대 {MAX_BLURS}개입니다.')
    result=[]
    for item in value:
        if not isinstance(item,dict):raise ValueError('블러 박스 형식을 확인하세요.')
        try:box={k:float(item.get(k) or 0) for k in ('x','y','w','h','start','end')}
        except (TypeError,ValueError):raise ValueError('블러 박스 값은 숫자로 입력하세요.') from None
        if not all(math.isfinite(v) for v in box.values()):raise ValueError('블러 박스 값은 숫자로 입력하세요.')
        box['x'],box['y']=min(max(box['x'],0),1),min(max(box['y'],0),1)
        box['w'],box['h']=min(max(box['w'],0),1-box['x']),min(max(box['h'],0),1-box['y'])
        if box['w']*WIDTH<16 or box['h']*HEIGHT<16:continue  # too small to matter
        if not 0<=box['start']<=60 or not 0<=box['end']<=60 or (box['end'] and box['end']<=box['start']):raise ValueError('블러 시간은 0~60초이고, 끝 시간은 시작보다 뒤여야 합니다(0이면 끝까지).')
        mode=item.get('mode') or 'erase'
        if mode not in BLUR_MODES:raise ValueError('가리기 방식을 다시 고르세요.')
        result.append({**{k:round(v,4) for k,v in box.items()},'mode':mode})
    return result


BLUR_MODES=('erase','blur','text','ai')
PRE_MODES=('text','ai')  # handled outside FFmpeg, before the blur graph
FEATHER=px(11)  # soft edge so the box outline does not show


def blur_graph(chain,boxes,duration,prefix=''):
    """Filter graph that hides each box on the laid-out frame, before the title and captions are drawn on top.

    erase: delogo fills the box from the pixels around it, so the text looks gone rather than smudged.
    blur: strong box blur. Both fade out over FEATHER pixels at the edges.
    """
    if not boxes:return chain
    graph=[f'{chain},format=yuv420p[v0]']
    for i,box in enumerate(boxes):
        # delogo needs a band of real pixels around the box, so keep it 2px inside the frame.
        x=min(max(round(box['x']*WIDTH),2),WIDTH-18);y=min(max(round(box['y']*HEIGHT),2),HEIGHT-18)
        w=min(max(16,round(box['w']*WIDTH)),WIDTH-2-x);h=min(max(16,round(box['h']*HEIGHT)),HEIGHT-2-y)
        left,top=min(FEATHER,x),min(FEATHER,y);right,bottom=min(FEATHER,WIDTH-x-w),min(FEATHER,HEIGHT-y-h)
        cx,cy,cw,ch=x-left,y-top,w+left+right,h+top+bottom
        if box.get('mode','erase')=='erase':hide=f'delogo=x={left}:y={top}:w={w}:h={h},gblur=sigma=6:sigmaV=1.5'  # mostly sideways: smooths delogo's vertical streaks
        else:hide=f'boxblur={max(2,min(px(14),min(cw,ch)//4-1))}:3'  # boxblur radius must fit the (chroma) size
        # alphamerge reads the mask's luma. Keep it yuv420p: split forces one format on both branches, and gray would make the whole video gray.
        mask=f"geq=lum='255*min(1,min(min(X,W-1-X),min(Y,H-1-Y))/{FEATHER})':cb=128:cr=128"
        on=f"between(t,{box['start']},{box['end'] or duration+1})"
        graph.append(f"[v{i}]split[m{i}][s{i}];[s{i}]crop={cw}:{ch}:{cx}:{cy},split[k{i}][q{i}];[q{i}]{mask}[a{i}];[k{i}]{hide}[h{i}];[h{i}][a{i}]alphamerge[f{i}];[m{i}][f{i}]overlay={cx}:{cy}:enable='{on}'[v{i+1}]")
    result=';'.join(graph)+f';[v{len(boxes)}]null'
    # A narration graph already owns v0/a0 labels. Keep overlay branches distinct.
    return re.sub(r'\[([a-z]+\d+)\]',lambda match:'['+prefix+match[1]+']',result) if prefix else result


class ToolError(ValueError):
    """A text-removal step failed; its message is shown to the user as is."""


def letters_ready():
    """Whether the letters-only eraser can run (media venv with OpenCV). Cached for a minute."""
    import time
    cached=_TOOL_CACHE.get('letters')
    if cached and time.time()-cached[0]<60:return cached[1]
    ok=False
    if MEDIA_PYTHON.is_file():
        try:ok=subprocess.run([str(MEDIA_PYTHON),'-c','import cv2,numpy'],capture_output=True,timeout=30).returncode==0
        except (OSError,subprocess.SubprocessError):ok=False
    _TOOL_CACHE['letters']=(time.time(),ok);return ok


def gpu_info():
    """Graphics cards on this PC, to suggest the right text-removal method. Read once per run."""
    if 'gpu' in _TOOL_CACHE:return _TOOL_CACHE['gpu']
    names=[];nvidia=False
    try:
        result=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total','--format=csv,noheader'],capture_output=True,text=True,timeout=10)
        if result.returncode==0:
            names=[line.strip() for line in result.stdout.splitlines() if line.strip()];nvidia=bool(names)
    except (OSError,subprocess.SubprocessError):pass
    if not names and os.name=='nt':
        try:
            result=subprocess.run(['powershell','-NoProfile','-Command','(Get-CimInstance Win32_VideoController).Name'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
            names=[line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (OSError,subprocess.SubprocessError):pass
    info={'names':names[:4],'nvidia':nvidia or any('nvidia' in n.lower() for n in names)}
    _TOOL_CACHE['gpu']=info;return info


def erase_letters(task):
    """Run text_eraser.py in the media venv. Returns its stdout (PNG bytes in frame mode)."""
    if not letters_ready():raise ToolError('글자만 지우기(정밀)를 쓰려면 scripts\\install_windows.cmd 를 한 번 다시 실행해 OpenCV를 설치하세요.')
    task={'width':WIDTH,'height':HEIGHT,'formats':SOURCE_FORMATS,**task}
    result=subprocess.run([str(MEDIA_PYTHON),str(Path(__file__).parent/'text_eraser.py')],input=json.dumps(task).encode('utf-8'),capture_output=True,timeout=900)
    if result.returncode:raise ToolError('글자만 지우기 처리에 실패했어요. 원본 영상과 박스 위치를 확인하세요.')
    return result.stdout


def ai_eraser():
    """(python, folder) of video-subtitle-remover under data/vsr, or None. Cached for a minute."""
    import time
    cached=_TOOL_CACHE.get('ai')
    if cached and time.time()-cached[0]<60:return cached[1]
    found=None
    if AI_ROOT.is_dir():
        for main in [m for pattern in ('backend/main.py','*/backend/main.py','*/*/backend/main.py') for m in AI_ROOT.glob(pattern)]:
            root=main.parent.parent
            if os.environ.get('MFLOW_VSR_PYTHON'):candidates=[Path(os.environ['MFLOW_VSR_PYTHON'])]
            else:candidates=[root/name for name in ('python/python.exe','python.exe','runtime/python.exe','.venv/Scripts/python.exe','venv/Scripts/python.exe','.venv/bin/python','venv/bin/python')]+sorted(root.glob('*/python.exe'))+sorted(AI_ROOT.glob('python.exe'))+sorted(AI_ROOT.glob('*/python.exe'))
            python=next((c for c in candidates if c.is_file()),None)
            if python:found=(python,root);break
    _TOOL_CACHE['ai']=(time.time(),found);return found


def erase_ai(source,output,boxes,log=None):
    """Remove subtitles inside the boxes with video-subtitle-remover.

    Video: STTN with OCR detection (sttn-det), so only frames that really show text are repainted.
    Picture (preview): the tool always uses OCR + LaMa. Box times are not passed: detection finds when text is shown.
    """
    tool=ai_eraser()
    if not tool:raise ToolError('서버에 AI 자막 지우개(VSR)가 준비되지 않았어요.')
    python,root=tool;source,output=Path(source),Path(output)
    # Work on copies inside the tool's own (ASCII) folder: its video reader fails on Korean paths on Windows.
    work=Path(tempfile.mkdtemp(dir=AI_ROOT if os.name=='nt' else None)).resolve()
    staged_in,staged_out=work/('in'+source.suffix),work/('out'+output.suffix)
    try:
        shutil.copyfile(source,staged_in)
        _run_ai(python,root,staged_in,staged_out,boxes,log)
        shutil.move(str(staged_out),str(output))
    finally:shutil.rmtree(work,ignore_errors=True)


def _run_ai(python,root,source,output,boxes,log):
    command=[str(python),'backend/main.py','-i',str(source),'-o',str(output),'--inpaint-mode','sttn-det']
    for box in boxes:
        x,y=round(box['x']*WIDTH),round(box['y']*HEIGHT);w,h=round(box['w']*WIDTH),round(box['h']*HEIGHT)
        command+=['-c',str(max(y,0)),str(min(y+h,HEIGHT)),str(max(x,0)),str(min(x+w,WIDTH))]
    env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','QT_QPA_PLATFORM':'offscreen','PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK':'True'}
    try:result=subprocess.run(command,cwd=root,capture_output=True,env=env,timeout=3600)
    except subprocess.TimeoutExpired:raise ToolError('AI 자막 지우기가 1시간 안에 끝나지 않았어요.') from None
    text=(result.stdout+result.stderr).decode('utf-8','replace')
    if log:Path(log).write_text(text,encoding='utf-8')
    if output.is_file() and output.stat().st_size:return
    if 'No subtitles detected' in text:shutil.copyfile(source,output);return  # nothing to remove in the boxes
    raise ToolError('AI 자막 지우기에 실패했어요. 그래픽카드 드라이버와 박스 위치를 확인하세요.'+(f' (기록: {log})' if log else ''))


def layout_frame(source,seek,vf,output):
    """One laid-out 1080×1920 PNG frame, before any box is applied."""
    command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(seek),'-format_whitelist',SOURCE_FORMATS,'-protocol_whitelist','file','-i',str(source),'-frames:v','1','-vf',','.join(vf) or 'null',str(output)]
    if subprocess.run(command,capture_output=True,timeout=60).returncode or not Path(output).is_file():raise ValueError('미리보기 화면을 만들지 못했어요. 원본 영상과 시작 시간을 확인하세요.')


IMAGE_FORMATS='image2,png_pipe'


def preview_frame(project_id,at,draft=None):
    """One PNG frame of the source with layout and blur boxes applied, to check the boxes before rendering."""
    p=project(project_id);doc=p['document'];cfg=doc['settings']
    if not doc.get('source'):raise ValueError('원본 영상을 먼저 업로드하세요.')
    cuts=doc.get('clips') or []
    if draft is not None:
        changes=draft.get('settings',{})
        if not isinstance(changes,dict):raise ValueError('미리보기 설정을 확인하세요.')
        cfg=settings({**cfg,**changes})
        cuts=manual_timeline.clips(draft.get('clips',cuts),doc['source']['duration'])
        doc={**doc,'blurs':blurs(draft.get('blurs',doc.get('blurs',[])))}
    duration=manual_timeline.duration(cuts) if cuts else min(cfg['duration'],doc['source']['duration']-cfg['start'])
    at=float(at)
    if not math.isfinite(at):at=0
    at=min(max(at,0),max(duration-0.05,0))
    cut, source_at = manual_timeline.clip_at(cuts, at) if cuts else ({}, cfg['start'] + at)
    vf=clip_layout_filters(cfg, cut)
    # -ss before -i resets t to 0, so shift the box times by the same amount.
    boxes=[{**b,'start':b['start']-at,'end':(b['end']-at) if b['end'] else 0} for b in doc.get('blurs') or [] if not b['end'] or b['end']>at]
    boxes=[{**b,'start':max(b['start'],0),'end':max(b['end'],0.01) if b['end'] else 0} for b in boxes]
    active=lambda b:b['start']<=at and (not b['end'] or at<b['end'])
    letters=[b for b in doc.get('blurs') or [] if b.get('mode')=='text']
    ai=[b for b in doc.get('blurs') or [] if b.get('mode')=='ai' and active(b)]
    boxes=[b for b in boxes if b.get('mode') not in PRE_MODES]
    source,start,formats=PROJECT_ROOT/project_id/doc['source']['filename'],cfg['start'],SOURCE_FORMATS
    if cuts:
        letters=[{**b,'start':0,'end':0} for b in letters if active(b)]
        start=source_at-at
    with tempfile.TemporaryDirectory() as tmp:
        if ai:
            # The AI tool works on the laid-out frame; the steps below then start from its result.
            laid,source=Path(tmp)/'frame.png',Path(tmp)/'clean.png'
            layout_frame(PROJECT_ROOT/project_id/doc['source']['filename'],source_at,vf,laid)
            erase_ai(laid,source,ai)
            vf,start,at,formats=[],0,0,IMAGE_FORMATS
            letters=[{**b,'start':0,'end':0} for b in letters if active(b)]
        if letters:
            png=erase_letters({'mode':'frame','source':str(source),'start':start,'at':at,'chain':blur_graph(','.join(vf) or 'null',boxes,1),'boxes':letters,'formats':formats})
            if not png.startswith(b'\x89PNG'):raise ValueError('미리보기 화면을 만들지 못했어요.')
            return png
        graph=blur_graph(','.join(vf) or 'null',boxes,1)+',scale=540:-2'
        command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-ss',str(start+at),'-format_whitelist',formats,'-protocol_whitelist','file','-i',str(source),'-frames:v','1','-vf',graph,'-f','image2pipe','-c:v','png','-']
        result=subprocess.run(command,capture_output=True,timeout=60)
    if result.returncode or not result.stdout:raise ValueError('미리보기 화면을 만들지 못했어요. 원본 영상과 시작 시간을 확인하세요.')
    return result.stdout


# Caption timeline: each script line with its own time, place and look (empty = equal split of the script).
CUE_POSITIONS=('bottom','middle','top')
CUE_LOOKS={'outline':'Caption','box':'CaptionBox','accent':'CaptionAccent','lyric':'Lyric'}
CUE_ACCENT='#ffe34a'
MAX_CUES=40


def cue_style(value,fonts):
    if not isinstance(value,dict):
        raise ValueError('자막 스타일을 확인하세요.')
    result={}
    for key in ('font','size','color','outline','outline_color','background'):
        if key not in value:
            continue
        item=value[key]
        if key=='font':
            valid=isinstance(item,str) and (not item or item in fonts)
        elif key in ('size','outline'):
            low,high=(24,144) if key=='size' else (0,20)
            valid=type(item) is int and low<=item<=high
        else:
            valid=isinstance(item,str) and (re.fullmatch(r'#[0-9a-fA-F]{6}',item) or (key=='background' and item==''))
            if valid:item=item.lower()
        if not valid:
            raise ValueError('자막 서체·크기·색상·외곽선·배경 값을 확인하세요.')
        result[key]=item
    return result


def cues(value):
    """Validate timeline lines: {'text','sub','start','end','pos','look','y'}. end 0 = until the next line."""
    if not isinstance(value,list) or len(value)>MAX_CUES:raise ValueError(f'자막 타임라인은 최대 {MAX_CUES}줄입니다.')
    result=[]
    fonts={f['file'] for f in available_fonts()} if value else set()
    for item in value:
        if not isinstance(item,dict):raise ValueError('자막 타임라인 형식을 확인하세요.')
        text=re.sub(r'\s+',' ',str(item.get('text',''))).strip();sub=re.sub(r'\s+',' ',str(item.get('sub',''))).strip()
        if not text:continue
        try:start,end,y=float(item.get('start') or 0),float(item.get('end') or 0),int(round(float(item.get('y') or 0)))
        except (TypeError,ValueError,OverflowError):raise ValueError('자막 시점은 숫자로 입력하세요.') from None
        pos=item.get('pos') or 'bottom';look=item.get('look') or 'outline'
        if len(text)>60 or len(sub)>60:raise ValueError('자막 한 줄은 60자 이내로 입력하세요.')
        if not all(math.isfinite(v) for v in (start,end)) or not 0<=start<=60 or not 0<=end<=60 or (end and round(end,2)<=round(start,2)):raise ValueError('자막 시점은 0~60초이고, 끝은 시작보다 뒤여야 합니다(0이면 다음 줄까지).')
        if pos not in CUE_POSITIONS or look not in CUE_LOOKS or not -OFFSET_LIMIT<=y<=OFFSET_LIMIT:raise ValueError('자막 위치와 모양을 다시 고르세요.')
        row={'text':text,'sub':sub,'start':round(start,2),'end':round(end,2),'pos':pos,'look':look,'y':y}
        custom=cue_style(item.get('style',{}),fonts)
        if custom:row['style']=custom
        if item.get('fixed') is True:row['fixed']=True
        if 'evidence' in item:row['evidence']=caption_drafts.validate_evidence(item['evidence'])
        if item.get('bundle_closing') is True:row['bundle_closing']=True
        result.append(row)
    return sorted(result,key=lambda c:c['start'])


def cue_lines(text):
    """Wrap one timeline line like the captions (about 12 characters a line, 3 lines at most)."""
    lines,line=[],[]
    for word in caption_words(text):
        if line and sum(len(w)+1 for w,_ in line)+len(word[0])>CAPTION_LINE_CHARS:lines.append(line);line=[]
        line.append(word)
    if line:lines.append(line)
    return lines[:3]


def cue_events(items,style,layout,duration,shift=0):
    """ASS events and SRT text for the caption timeline."""
    video_bottom=HEIGHT if layout=='fill' else VIDEO_BOTTOM
    anchors={'top':(8,TITLE_HEIGHT+px(40)),'middle':(5,(TITLE_HEIGHT+video_bottom)//2),'bottom':(2,CAPTION_BOTTOM.get(layout,CAPTION_BOTTOM['band']))}
    events,srt=[],[]
    for index,cue in enumerate(items):
        start=min(cue['start'],duration);end=cue['end'] or (items[index+1]['start'] if index+1<len(items) else duration+1)
        end=min(end,duration+1)
        if end<=start:continue
        custom=cue.get('style',{})
        name=f'Cue{index}' if custom else CUE_LOOKS[cue['look']]
        base=custom.get('color',cue_color(cue['look'],style))
        mark=custom.get('color',style['highlight'] if cue['look']!='accent' else style['caption'])
        text=r'\N'.join(' '.join(('{\\c'+ass_color(mark)+'}'+ass_text(w)+'{\\c'+ass_color(base)+'}') if marked else ass_text(w) for w,marked in line) for line in cue_lines(cue['text']))
        if cue['sub']:text+=r'\N{\fs'+str(cue_sub_size(cue['look'],custom))+r'\i0\c'+ass_color(custom.get('color','#ffffff'))+'}'+ass_text(cue['sub'])
        align,y=anchors[cue['pos']]
        events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},{name},,0,0,0,,{{\\an{align}\\pos({WIDTH//2},{y+px(cue["y"])+shift})}}{text}')
        plain=cue['text'].replace('*','')+('\n'+cue['sub'] if cue['sub'] else '')
        srt.extend([str(len(srt)//4+1),f'{srt_time(start)} --> {srt_time(min(end,duration))}',plain,''])
    return events,'\n'.join(srt)


def comments(value):
    """Validate up to 3 comment cards: {'name','when','text','at'}. Rows without text are dropped."""
    if not isinstance(value,list):raise ValueError('댓글 형식을 확인하세요.')
    result=[]
    for item in value[:3]:
        if not isinstance(item,dict):raise ValueError('댓글 형식을 확인하세요.')
        text=re.sub(r'\s+',' ',str(item.get('text',''))).strip();name=str(item.get('name','')).strip();when=str(item.get('when','')).strip()
        if not text:continue
        try:at=float(item.get('at') or 0)
        except (TypeError,ValueError):raise ValueError('댓글 표시 시작 시간을 숫자로 입력하세요.') from None
        if len(text)>80 or len(name)>30 or len(when)>15 or not math.isfinite(at) or not 0<=at<=60:raise ValueError('댓글은 80자, 닉네임 30자, 시간 표시 15자, 표시 시작 0~60초 이내로 입력하세요.')
        result.append({'name':name,'when':when,'text':text,'at':at})
    return sorted(result,key=lambda c:c['at'])


@serialized
def approve(project_id):
    p=project(project_id);doc=p['document']
    cuts=doc.get('clips') or []
    if p['status']=='rendering' or not doc.get('source') or not (doc.get('script') or cuts or doc.get('audio_mix')):raise ValueError('원본과 대본, 편집 구간 또는 오디오 믹스를 준비한 뒤 승인하세요.')
    if cuts and doc.get('generated_script'):raise ValueError('수동 구간 편집과 생성 대본의 음성 맞춤 편집은 별도 프로젝트에서 사용하세요.')
    if not cuts and doc['settings']['start']>=doc['source']['duration']:raise ValueError('시작 시간이 원본 길이보다 깁니다.')
    if not cuts and (doc.get('analysis') or doc.get('generated_script')):
        import workflow
        workflow.current_script(doc)
    voice=current_voice(doc)
    doc['approved_hash']=fingerprint(doc)
    if voice and voice.get('mixer_base_hash')==fingerprint(doc,include_mix=False):
        voice['approved_hash']=doc['approved_hash']
    doc['message']='대본과 설정이 승인되었습니다. 렌더링을 시작할 수 있습니다.';p['status']='approved';store(p)


@serialized
def start_render(project_id):
    p=project(project_id);doc=p['document']
    existing=active_job(p)
    if existing:
        return existing
    if p['status'] not in ('approved','completed','error') or not doc.get('approved_hash') or doc['approved_hash']!=fingerprint(doc):raise ValueError('현재 대본과 설정을 먼저 승인하세요.')
    if not state()['render_available']:raise ValueError('FFmpeg/FFprobe와 한국어 글꼴 설치가 필요합니다.')
    if doc.get('generated_script'):
        import workflow
        workflow.current_script(doc)
        if not uploaded_voice(doc) and (not doc.get('narration') or doc['narration'].get('approved_hash') != doc['approved_hash']):
            raise ValueError('현재 승인된 대본의 나레이션을 먼저 생성하세요.')
    report=doc.get('qa_report') or {}
    if p['status']=='completed' and (not doc.get('generated_script') or
            report.get('output_sha256') and any(c.get('name')=='narration_presence' for c in report.get('checks',[]))):
        try:
            for kind in ('output','archive'):
                if not asset(project_id,kind).stat().st_size:
                    raise ValueError('empty render asset')
            expected=(doc.get('qa_report') or {}).get('output_sha256')
            if expected:
                with asset(project_id,'output').open('rb') as video:
                    if hashlib.file_digest(video,'sha256').hexdigest()!=expected:
                        raise ValueError('render asset changed')
        except (OSError,ValueError):
            pass
        else:
            return {'status':'completed','message':'같은 설정으로 완료된 영상을 사용할 수 있습니다.'}
    return enqueue_job(p)


def active_job(p, *, voice=False):
    if p['status']!='rendering':
        return None
    with CONNECT() as con:
        job=con.execute("SELECT id,status,engine FROM production_render_jobs WHERE project_id=? AND status IN ('queued','running')",(p['id'],)).fetchone()
    if job:
        if bool(job['engine']) != voice:
            raise ValueError('이 프로젝트의 다른 제작 작업이 진행 중입니다.')
        return {'job_id':job['id'],'status':job['status'],'message':p['document']['message']}


def enqueue_job(p, engine=None, con=None):
    """Register a validated job while the caller holds CONFIG_LOCK."""
    if con is None:
        with CONNECT() as connection:
            result=enqueue_job(p,engine,connection)
        RENDER_WAKE.set()
        return result
    job_id=uuid.uuid4().hex
    p['status']='rendering'
    if not engine:
        p['document'].pop('publish_approval',None)
    message=('쇼츠 자동 제작 대기열에 등록했습니다.' if engine=='bundle' else
             '나레이션 대기열에 등록했습니다.' if engine else '렌더 대기열에 등록했습니다.')
    p['document']['message']=message
    con.execute("""INSERT OR REPLACE INTO production_render_jobs
            (project_id,id,status,attempts,created_at,engine) VALUES (?,?,'queued',0,?,?)""",
                    (p['id'],job_id,now(),engine))
    store(p,con)
    return {'job_id':job_id,'status':'queued','message':message}


@serialized
def recover_renders():
    """Restore persisted jobs on startup, before starting the worker."""
    with CONNECT() as con:
        rows=con.execute("""SELECT p.*, j.id AS render_job_id, j.attempts
            FROM production_projects p LEFT JOIN production_render_jobs j
            ON j.project_id=p.id AND j.status IN ('queued','running')
            WHERE p.status='rendering'""").fetchall()
        for row in rows:
            p=dict(row)
            p['document']=json.loads(p['document'])
            if p['render_job_id'] and p['attempts'] < MAX_RENDER_ATTEMPTS:
                con.execute("UPDATE production_render_jobs SET status='queued' WHERE id=?",(p['render_job_id'],))
                p['document']['message']='앱이 다시 시작되어 제작 대기열을 복구했습니다.'
            else:
                p['status']='error'
                p['document']['message']='제작 작업이 중단되었습니다. 다시 시도하세요.'
            store(p,con)


def process_next_render():
    """Claim one queued render and finish it before taking the next."""
    if not LOCK.acquire(blocking=False):
        return False
    handed_off=False
    try:
        with CONFIG_LOCK, CONNECT() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("""SELECT p.*, j.id AS render_job_id, j.engine AS job_engine
                FROM production_render_jobs j JOIN production_projects p ON p.id=j.project_id
                WHERE j.status='queued' AND p.status='rendering'
                ORDER BY j.created_at,j.id LIMIT 1""").fetchone()
            if not row:
                return False
            p=dict(row)
            p['document']=json.loads(p['document'])
            con.execute("UPDATE production_render_jobs SET status='running',attempts=attempts+1 WHERE id=?",
                        (p['render_job_id'],))
            p['document']['message']='나레이션을 생성하고 있습니다.' if p['job_engine'] else '세로 영상과 자막을 렌더링하고 있습니다.'
            store(p,con)
        if p['job_engine']=='bundle':
            import shorts_bundle
            if not shorts_bundle.prepare_automation(p):
                return True
            handed_off=True
            render(p)
            return True
        if p['job_engine']:
            import workflow
            workflow.run_narration_job(p)
            return True
        handed_off=True
        render(p)
        return True
    finally:
        if not handed_off:
            LOCK.release()


def render_worker(stop):
    while not stop.is_set():
        try:
            if process_next_render():
                continue
        except Exception:
            traceback.print_exc()
        RENDER_WAKE.wait(1)
        RENDER_WAKE.clear()


def filter_path(path):
    """Quote a file path for an FFmpeg filter option (handles Windows drive colons and backslashes)."""
    value=str(path).replace('\\','/')
    if "'" in value:raise ValueError('파일 경로에 작은따옴표가 있으면 렌더링할 수 없습니다.')
    return "'"+value.replace(':','\\:')+"'"


def font_info(path):
    """(family name, weight) of a TTF/OTF/TTC file, cached by modification time."""
    path=Path(path);key=(str(path),path.stat().st_mtime_ns)
    if key not in _FONT_CACHE:_FONT_CACHE[key]=read_font_info(path)
    return _FONT_CACHE[key]


def read_font_info(path):
    """Read the family name and weight from the font's name and OS/2 tables so libass can pick it from fontsdir."""
    import struct
    data=Path(path).read_bytes()
    offset=struct.unpack('>I',data[12:16])[0] if data[:4]==b'ttcf' else 0
    tables=struct.unpack('>H',data[offset+4:offset+6])[0];names={};weight=400
    for i in range(tables):
        tag,_,table,_=struct.unpack('>4sIII',data[offset+12+16*i:offset+28+16*i])
        if tag==b'OS/2':weight=struct.unpack('>H',data[table+4:table+6])[0]
        if tag!=b'name':continue
        _,count,strings=struct.unpack('>HHH',data[table:table+6])
        for j in range(count):
            platform,encoding,language,name_id,length,start=struct.unpack('>6H',data[table+6+12*j:table+18+12*j])
            if name_id==1 and platform==3 and encoding in (1,10):
                names[language]=data[table+strings+start:table+strings+start+length].decode('utf-16-be',errors='ignore')
    family=(names.get(0x409) or next(iter(names.values()),'')).replace(',',' ').strip()
    if not family:raise ValueError('font name')
    return family,weight


def ass_color(value):return '&H00'+value[5:7]+value[3:5]+value[1:3]+'&'


def ass_text(value):return value.replace('\\','＼').replace('{','｛').replace('}','｝')


def title_lines(headline):
    for width,size,limit in TITLE_FITS:
        lines=textwrap.wrap(headline,width)
        if len(lines)<=limit:return lines,size
    return textwrap.wrap(headline,TITLE_FITS[-1][0]),TITLE_FITS[-1][1]


def caption_words(script):
    """Split the script into (word, highlighted) pairs. Words wrapped in *asterisks* are highlighted."""
    words=[]
    for index,part in enumerate(re.split(r'\*([^*]+)\*',script)):
        for word in part.split():
            while len(word)>CAPTION_LINE_CHARS:words.append((word[:CAPTION_LINE_CHARS],index%2==1));word=word[CAPTION_LINE_CHARS:]
            if word:words.append((word.replace('*',''),index%2==1))
    return [w for w in words if w[0]]


def caption_chunks(script):
    """Group words into on-screen captions of up to two lines each."""
    chunks,lines,line=[],[],[]
    for word in caption_words(script):
        if line and sum(len(w)+1 for w,_ in line)+len(word[0])>CAPTION_LINE_CHARS:
            lines.append(line);line=[]
            if len(lines)==2:chunks.append(lines);lines=[]
        line.append(word)
        if word[0][-1] in '.?!。':lines.append(line);line=[];chunks.append(lines);lines=[]
    if line:lines.append(line)
    if lines:chunks.append(lines)
    return chunks


def ass_font(path):
    """Fontname and Bold flag for an ASS style. Only ask for bold when the file is bold, so display fonts are not fake-bolded."""
    family,weight=font_info(path)
    return family,-1 if weight>=600 else 0


def rounded_box(w,h,r):
    """ASS vector drawing of a w x h rectangle with rounded corners."""
    return f'm {r} 0 l {w-r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h-r} b {w} {h} {w} {h} {w-r} {h} l {r} {h} b 0 {h} 0 {h} 0 {h-r} l 0 {r} b 0 0 0 0 {r} 0'


def comment_events(cards,layout,duration,shift=0):
    """Comment cards, each shown from its 'at' time until the next card (or the end)."""
    events=[];top=COMMENT_TOP.get(layout,COMMENT_TOP['band'])+shift;width=WIDTH-2*COMMENT_MARGIN;left=COMMENT_MARGIN
    for index,card in enumerate(cards):
        start=min(card['at'],duration);end=cards[index+1]['at'] if index+1<len(cards) else duration+1
        if end<=start:continue
        lines=textwrap.wrap(card['text'],COMMENT_LINE_CHARS)[:3];name=card['name']
        if name and not name.startswith('@'):name='@'+name
        header=' · '.join(x for x in (name,card['when']) if x)
        header_h=round(COMMENT_NAME_FONT*1.5) if header else 0
        height=2*COMMENT_PAD+header_h+round(len(lines)*COMMENT_FONT*1.3)
        span=f'{ass_time(start)},{ass_time(end)}'
        events.append(f'Dialogue: 1,{span},Card,,0,0,0,,{{\\an7\\pos({left},{top})\\p1}}{rounded_box(width,height,px(12))}{{\\p0}}')
        y=top+COMMENT_PAD
        if header:events.append(f'Dialogue: 2,{span},CommentName,,0,0,0,,{{\\an7\\pos({left+COMMENT_PAD},{y})}}{ass_text(header)}');y+=header_h
        events.append(f'Dialogue: 2,{span},Comment,,0,0,0,,{{\\an7\\pos({left+COMMENT_PAD},{y})}}'+r'\N'.join(ass_text(line) for line in lines))
    return events


def overlay_events(items,duration):
    for index,item in enumerate(items):
        start=item['start']
        end=min(item['end'] or duration,duration)
        if ass_time(end)==ass_time(start) or end<=start:
            continue
        size=item['size']
        family,bold=ass_font(font_path(item['font']))
        x,y=round(item['x']*WIDTH),round(item['y']*HEIGHT)
        lines=textwrap.wrap(item['text'],max(1,round(WIDTH*0.85/size)),break_on_hyphens=False)
        text=r'\N'.join(ass_text(line) for line in lines)
        style={**OVERLAY_STYLE, **item.get('style',{})}
        layer=3+2*index
        span=f'{ass_time(start)},{ass_time(end)}'
        if style['background_opacity']:
            padding=math.ceil(size*0.16)+style['outline_width']
            width=max(map(len,lines))*size+2*padding
            height=math.floor(len(lines)*size*1.2+0.5)+2*padding
            alpha=255-math.floor(style['background_opacity']*2.55+0.500001)
            bg=rf'{{\an7\pos({x-width//2},{y-height//2})\p1\bord0\shad0\1c{ass_color(style["background_color"]).upper()}\1a&H{alpha:02X}&}}'
            yield f'Dialogue: {layer},{span},Caption,,0,0,0,,{bg}m 0 0 l {width} 0 {width} {height} 0 {height}'
            layer+=1
        tags=rf'{{\an5\pos({x},{y})\fn{ass_text(family)}\fs{size}\b{int(bool(bold))}\c{ass_color(style["color"]).upper()}\3c{ass_color(style["outline_color"]).upper()}\bord{style["outline_width"]}\shad0}}'
        yield f'Dialogue: {layer},{span},Caption,,0,0,0,,{tags}{text}'


def cue_color(look,style):
    return {'accent':CUE_ACCENT,'lyric':'#ffffff'}.get(look,style['caption'])


def cue_sub_size(look,custom):
    return round(px(26)*custom['size']/CUE_SIZES[look]) if 'size' in custom else px(26)


CUE_SIZES={'outline':CAPTION_FONT,'box':px(40),'accent':CAPTION_FONT,'lyric':px(32)}


def cue_ass_style(name,look,style,family,bold,custom):
    box=look=='box'
    italic=int(look=='lyric')
    bold=0 if italic else bold
    if 'font' in custom:
        family,bold=ass_font(font_path(custom['font']))
    color=ass_color(cue_color(look,style) if 'color' not in custom else custom['color'])
    border=3 if box else 1
    outline=px(10) if box else px(3) if italic else CAPTION_OUTLINE
    outline_color='&H20000000&' if box else '&H00000000&'
    back='&H00000000&' if box else '&H80000000&'
    shadow=0 if box else px(1 if italic else 2)
    if 'background' in custom or (box and ('outline' in custom or 'outline_color' in custom)):
        background=custom.get('background','#000000' if box else '')
        border=4 if background else 1
        back=(ass_color(background) if 'background' in custom else '&H20000000&') if background else '&HFF000000&'
        shadow=(px(10) if box else px(4)) if background else 0
        if box:
            outline,outline_color=0,'&H00000000&'
    outline=custom.get('outline',outline)
    if 'outline_color' in custom:
        outline_color=ass_color(custom['outline_color'])
    size=custom.get('size',CUE_SIZES[look])
    return f'Style: {name},{family},{size},{color},&H000000FF&,{outline_color},{back},{bold},{-italic},0,0,100,100,0,0,{border},{outline},{shadow},2,0,0,0,1'


def subtitles(doc,style,duration,captions,title_font=None,caption_font=None,layout='band',offsets=None):
    """Build the ASS subtitle file (headline + narration captions) and the matching SRT text."""
    shift={k:px((offsets or {}).get(k) or 0) for k in OFFSETS}
    lines,size=title_lines(doc['headline'])
    title=r'\N'.join(('{\\c'+ass_color(style['title' if i==0 else 'title2'])+'}')+ass_text(line) for i,line in enumerate(lines))
    title_family,title_bold=ass_font(title_font or FONT);caption_family,caption_bold=ass_font(caption_font or FONT);ui_family,ui_bold=ass_font(FONT)
    credit=shown_credit(doc);title_bottom=TITLE_HEIGHT-(px(46) if credit else px(26))
    ass=['[Script Info]','ScriptType: v4.00+',f'PlayResX: {WIDTH}',f'PlayResY: {HEIGHT}','WrapStyle: 2','ScaledBorderAndShadow: yes','',
         '[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
         f'Style: Title,{title_family},{size},{ass_color(style["title"])},&H000000FF&,&H00000000&,&H00000000&,{title_bold},0,0,0,100,100,0,0,1,{TITLE_OUTLINE},0,2,0,0,0,1',
         *(cue_ass_style(name,look,style,caption_family,caption_bold,{}) for look,name in CUE_LOOKS.items()),
         *(cue_ass_style(f'Cue{i}',cue['look'],style,caption_family,caption_bold,cue['style'])
           for i,cue in enumerate(doc.get('cues',[])) if cue.get('style')),
         f'Style: Credit,{ui_family},{CREDIT_FONT},&H00D8D8D8&,&H000000FF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,{px(1)},0,2,0,0,0,1',
         f'Style: Card,{ui_family},20,&H00FFFFFF&,&H000000FF&,&H00000000&,&H60000000&,0,0,0,0,100,100,0,0,1,0,{px(3)},7,0,0,0,1',
         f'Style: CommentName,{ui_family},{COMMENT_NAME_FONT},&H00707070&,&H000000FF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1',
         f'Style: Comment,{ui_family},{COMMENT_FONT},&H00141414&,&H000000FF&,&H00000000&,&H00000000&,{ui_bold},0,0,0,100,100,0,0,1,0,0,7,0,0,0,1',
         '','[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
         f'Dialogue: 0,{ass_time(0)},{ass_time(duration+1)},Title,,0,0,0,,{{\\pos({WIDTH//2},{title_bottom+shift["title_y"]})}}{title}']
    if credit:ass.append(f'Dialogue: 0,{ass_time(0)},{ass_time(duration+1)},Credit,,0,0,0,,{{\\pos({WIDTH//2},{TITLE_HEIGHT-px(12)+shift["credit_y"]})}}{ass_text(credit)}')
    ass.extend(comment_events(doc.get('comments') or [],layout,duration,shift['comment_y']))
    ass.extend(overlay_events(doc.get('overlays',[]),duration))
    for quote in doc.get('quotes',[]):
        start,end=quote['start'],min(quote['end'] or duration,duration)
        if end<=start or ass_time(end)==ass_time(start):
            continue
        size=min(54,960//len(quote['text']))
        y=1515 if layout=='fill' else 1260
        tags=rf'{{\an2\pos({WIDTH//2},{y})\fs{size}\c{ass_color("#ffe34a")}\bord5\shad0}}'
        ass.append(f'Dialogue: 15,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{tags}{ass_text(quote["text"])}')
    # Explicit timeline edits take precedence over automatic sentence captions.
    if doc.get('cues'):
        events,srt=cue_events(doc['cues'],style,layout,duration,shift['caption_y'])
        if captions:ass.extend(events)
        return '\n'.join(ass)+'\n',srt
    srt=[];events=[]
    if doc.get('narration'):
        for segment in doc['narration']['segments']:
            chunks=caption_chunks(segment['text'])
            for index,chunk in enumerate(chunks):
                start=segment['start']+(segment['end']-segment['start'])*index/len(chunks)
                end=segment['start']+(segment['end']-segment['start'])*(index+1)/len(chunks)
                events.append((start,end,chunk))
    else:
        chunks=caption_chunks(doc['script'])
        events=[(duration*index/len(chunks),duration*(index+1)/len(chunks),chunk) for index,chunk in enumerate(chunks)]
    for index,(start,end,chunk) in enumerate(events):
        srt.extend([str(index+1),f'{srt_time(start)} --> {srt_time(end)}','\n'.join(' '.join(w for w,_ in line) for line in chunk),''])
        text=r'\N'.join(' '.join(('{\\c'+ass_color(style['highlight'])+'}'+ass_text(w)+'{\\c'+ass_color(style['caption'])+'}') if marked else ass_text(w) for w,marked in line) for line in chunk)
        if captions:ass.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{{\\pos({WIDTH//2},{CAPTION_BOTTOM.get(layout,CAPTION_BOTTOM["band"])+shift["caption_y"]})}}{text}')
    return '\n'.join(ass)+'\n','\n'.join(srt)


def ass_time(seconds):
    cs=round(seconds*100);hours,cs=divmod(cs,360000);minutes,cs=divmod(cs,6000);seconds,cs=divmod(cs,100)
    return f'{hours}:{minutes:02}:{seconds:02}.{cs:02}'


def srt_time(seconds):
    ms=round(seconds*1000);hours,ms=divmod(ms,3600000);minutes,ms=divmod(ms,60000);seconds,ms=divmod(ms,1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def narration_qa_data(doc, plan):
    """Describe the same caption chunks and source cuts used by the renderer."""
    voice={**doc['narration'],'segments':[],'captions':[]}
    for segment,cut in zip(doc['narration']['segments'],plan):
        voice['segments'].append({**segment,'source_start':cut['source_start'],'source_end':cut['source_end']})
        chunks=caption_chunks(segment['text'])
        for index,chunk in enumerate(chunks):
            start=segment['start']+segment['duration']*index/len(chunks)
            end=segment['start']+segment['duration']*(index+1)/len(chunks)
            voice['captions'].append({'sentence_id':segment['sentence_id'],'text':'\n'.join(' '.join(w for w,_ in line) for line in chunk),'start':start,'end':end})
    if doc.get('cues'):
        voice['captions']=[]
        duration=doc['narration']['duration'];items=doc['cues']
        for index,cue in enumerate(items):
            start=min(cue['start'],duration)
            end=min(cue['end'] or (items[index+1]['start'] if index+1<len(items) else duration),duration)
            if end<=start:continue
            text='\n'.join(' '.join(word for word,_ in line) for line in cue_lines(cue['text']))
            if cue['sub']:text+='\n'+cue['sub']
            voice['captions'].append({'text':text,'start':start,'end':end})
    return voice


def render_timeline(doc, voice):
    cfg=doc['settings']
    cuts=doc.get('clips') or []
    if cuts:
        return manual_timeline.duration(cuts), None, None
    if voice:
        import editing
        import workflow
        script=workflow.current_script(doc)
        plan=editing.build_plan(doc['analysis'],script,voice,doc['source']['duration'])
        return voice['duration'], plan, script
    duration=min(cfg['duration'],doc['source']['duration']-cfg['start'])
    if duration<=0:raise ValueError('시작 시간이 원본 길이보다 깁니다.')
    return duration, None, None


def media_graph(p, doc, source, plan, frame_filters=None):
    cfg=doc['settings']
    source_args=audio_mixer.input_args(source,SOURCE_FORMATS)
    original=doc['source'].get('audio',False)
    mix=doc.get('audio_mix')
    if plan:
        import editing
        graph=editing.ffmpeg_filters(plan,original,cfg['mute'],video_only=bool(mix))
        if not mix:source_args+=audio_mixer.input_args(asset(p['id'],'narration'))
    elif doc.get('clips'):
        graph=manual_timeline.graph(doc['clips'],original and not cfg['mute'] and not mix,frame_filters)
    else:
        return ['-ss',str(cfg['start']),*source_args],None
    return source_args,graph


def render_mix(p, doc, source, duration, plan, output):
    mix=doc.get('audio_mix')
    if not mix:return None
    if plan:
        cuts=[(cut['source_start'],cut['source_end'],cut['duration']) for cut in plan]
    elif doc.get('clips'):
        cuts=[(cut['start'],cut['end'],cut['end']-cut['start']) for cut in doc['clips']]
    else:
        start=doc['settings']['start']
        cuts=[(start,start+duration,duration)]
    generated=(asset(p['id'],'narration'),duration) if plan else None
    audio_mixer.render(output,source,SOURCE_FORMATS,cuts,duration,mix,
                       doc['source'].get('audio',False) and not doc['settings']['mute'],
                       audio_directory(p),generated)
    return output


def check_rendered_narration(p, report, duration, scratch):
    """Inspect the narration contribution in isolation from the other audio tracks."""
    import qa

    doc=p['document']
    track=(doc.get('audio_mix') or {}).get('narration',{})
    volume=track.get('volume',100)
    metrics={}
    if track.get('muted') or not volume:
        reason='나레이션 트랙이 음소거됐거나 볼륨이 0입니다.'
    else:
        reason='내보낸 영상 구간에 들리는 나레이션 신호가 없습니다.'
        try:
            source=asset(p['id'],'narration')
            clip={'in':0,'out':doc['narration']['duration'],'at':0}
            filters=audio_mixer.placed_audio(clip,duration)+f',volume={volume / 100}'
            subprocess.run([*audio_mixer.COMMAND,'-loglevel','error',*audio_mixer.input_args(source),
                            '-map','0:a:0','-af',filters,'-c:a','pcm_s16le',str(scratch)],
                           capture_output=True,check=True,timeout=60)
            metrics=qa.audio_stats(scratch)
        except (OSError, ValueError, EOFError, subprocess.SubprocessError):
            reason='최종 믹스에 사용된 나레이션 신호를 확인하지 못했습니다.'
        finally:
            scratch.unlink(missing_ok=True)
    present=metrics.get('peak',0)>0
    report['checks'].append({'name':'narration_presence','status':'passed' if present else 'failed',
                             'metrics':metrics})
    if not present:
        report['issues'].append({'code':'narration_not_audible','severity':'error','message':reason})
        report['passed']=False


def mapped_mix(args, mix):
    if not mix:return args,[]
    index=args.count('-i')
    return [*args,'-i',str(mix)],['-map',f'{index}:a:0']


def preview_mix(data):
    p=project(str(data.get('id','')))
    doc=p['document']
    if not doc.get('source'):raise ValueError('원본 영상을 먼저 업로드하세요.')
    voice=current_voice(doc) if data.get('script',doc['script'])==doc['script'] else None
    changes=data.get('settings',{})
    if not isinstance(changes,dict):raise ValueError('미리보기 설정을 확인하세요.')
    doc['settings']=settings({**doc['settings'],**changes})
    doc['clips']=manual_timeline.clips(data.get('clips',doc.get('clips',[])),doc['source']['duration'])
    if 'audio_mix' in data:
        doc['audio_mix']=audio_mixer.settings(data['audio_mix'],audio_directory(p)) if data['audio_mix'] is not None else None
    if doc['clips'] or uploaded_voice(doc):voice=None
    duration,plan,_=render_timeline(doc,voice)
    source=PROJECT_ROOT/p['id']/doc['source']['filename']
    with tempfile.TemporaryDirectory() as folder:
        output=Path(folder)/'preview.mp4'
        try:
            mix=render_mix(p,doc,source,duration,plan,Path(folder)/'mix.wav')
            cfg=doc['settings']
            frame_filters=None
            if doc['clips']:
                def frame_filters(cut):
                    return clip_layout_filters(cfg,cut)+['format=yuv420p']
            args,graph=media_graph(p,doc,source,plan,frame_filters)
            args,maps=mapped_mix(args,mix)
            picture="scale=w='min(480,iw)':h=-2,setsar=1"
            if cfg['mirror'] and not frame_filters:picture+=',hflip'
            if graph:
                filters=graph['filters']+[f'[{graph["video_label"]}]{picture}[preview_video]']
                args+=['-filter_complex_threads','2','-filter_complex',';'.join(filters),'-map','[preview_video]']
                if graph['audio_label']:maps=['-map',f'[{graph["audio_label"]}]']
            else:
                args+=['-map','0:v:0','-vf',picture]
                if not mix and not doc['settings']['mute']:maps=['-map','0:a:0?']
            subprocess.run(
                [*audio_mixer.COMMAND,'-loglevel','error','-threads','2',*args,*maps,'-t',str(duration),
                 '-c:v','libx264','-preset','ultrafast','-crf','28','-threads','2','-pix_fmt','yuv420p',
                 '-r','30','-c:a','aac','-b:a','128k','-movflags','+faststart',str(output)],
                capture_output=True,check=True,timeout=120,
            )
        except (OSError,subprocess.SubprocessError):
            raise ValueError('오디오 미리보기를 만들 수 없습니다. 파일과 재생 구간을 확인하세요.') from None
        return output.read_bytes()


def render(p):
    doc=p['document'];directory=PROJECT_ROOT/p['id'];run=directory/uuid.uuid4().hex
    try:
        run.mkdir(parents=True)
        source=directory/doc['source']['filename'];cfg=doc['settings'];cuts=doc.get('clips') or []
        voice=doc.get('narration') if not cuts and not uploaded_voice(doc) else None
        if voice and voice.get('approved_hash') != doc.get('approved_hash'):raise ValueError('stale narration')
        duration,plan,script=render_timeline(doc,voice)
        mix=render_mix(p,doc,source,duration,plan,run/'mix.wav')
        style=STYLES[cfg['style']]
        vf=layout_filters(cfg.get('layout','fill'))
        if cfg['mirror']:vf.append('hflip')
        boxes=doc.get('blurs') or [];letters=[b for b in boxes if b.get('mode')=='text'];ai=[b for b in boxes if b.get('mode')=='ai']
        source_args,graph=media_graph(p,doc,source,plan,lambda cut:clip_layout_filters(cfg,cut)+['format=yuv420p'])
        graph_args=source_args
        if cuts:vf=['null']
        graph_audio=['-map',f'[{graph["audio_label"]}]'] if graph and graph['audio_label'] else []
        if ai or letters:
            # These erasers work on the laid-out video outside FFmpeg; their result replaces the source picture.
            picture=None
            if graph:
                # Match scenes before erasing: box coordinates/times refer to the final
                # canvas and spoken timeline, not a contiguous slice of the source.
                laid=run/'matched-laid.mp4'
                filters=graph['filters']+[f'[{graph["video_label"]}]'+','.join(vf)+'[laid_video]']
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',*graph_args,
                                '-filter_complex_threads','2','-filter_complex',';'.join(filters),'-t',str(duration),
                                '-map','[laid_video]',*graph_audio,
                                '-c:v','libx264','-preset','veryfast','-crf','14','-pix_fmt','yuv420p',
                                '-c:a','aac','-b:a','192k',str(laid)],capture_output=True,timeout=900,check=True)
                picture=laid
                source_args=['-i',str(laid)]  # Preserve its scene-matched, ducked audio.
            if ai:
                if picture is None:
                    laid=run/'laid.mp4'
                    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',*source_args,'-t',str(duration),'-map','0:v:0','-an','-vf',','.join(vf+['fps=30']) ,'-c:v','libx264','-preset','veryfast','-crf','14','-pix_fmt','yuv420p',str(laid)],capture_output=True,timeout=900,check=True)
                else:laid=picture
                picture=run/'ai.mp4';erase_ai(laid,picture,ai,run/'ai_eraser.log')
            if letters:
                cleaned=run/'cleaned.mp4'
                if picture:erase_letters({'mode':'video','source':str(picture),'start':0,'duration':duration,'chain':'null','boxes':letters,'output':str(cleaned)})
                else:erase_letters({'mode':'video','source':str(source),'start':cfg['start'],'duration':duration,'chain':','.join(vf),'boxes':letters,'output':str(cleaned)})
                picture=cleaned
            vf=[];source_args=['-i',str(picture)]+source_args
        vf=[blur_graph(','.join(vf) or 'null',[b for b in boxes if b.get('mode') not in PRE_MODES],duration,prefix='overlay_')]
        # libass loads the font from its own folder, so it does not depend on the system font setup (fontconfig on Windows).
        title_font=font_path(cfg.get('title_font',''));caption_font=font_path(cfg.get('caption_font',''))
        fonts=run/'fonts';fonts.mkdir()
        overlay_fonts={font_path(item['font']) for item in doc.get('overlays',[])}
        overlay_fonts.update(font_path(cue['style']['font']) for cue in doc.get('cues',[]) if 'font' in cue.get('style',{}))
        for path in {title_font,caption_font,Path(FONT)} | overlay_fonts:
            shutil.copyfile(path,fonts/path.name)
        caption_doc={**doc,'narration':None} if uploaded_voice(doc) else doc
        ass,srt=subtitles(caption_doc,style,duration,cfg['captions'],title_font,caption_font,cfg.get('layout','fill'),cfg)
        (run/'overlay.ass').write_text(ass,encoding='utf-8')
        vf.extend([f'drawbox=x=0:y=0:w=iw:h={TITLE_HEIGHT}:color=black:t=fill',f"ass=filename={filter_path(run/'overlay.ass')}:fontsdir={filter_path(fonts)}"])
        (run/'captions.srt').write_text(srt,encoding='utf-8')
        output=run/'short.mp4'
        if graph and not (ai or letters):
            graph_args,mix_map=mapped_mix(graph_args,mix)
            filters=graph['filters']+[f'[{graph["video_label"]}]'+','.join(vf)+'[finished_video]']
            command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-threads','2',*graph_args,'-filter_complex_threads','2','-filter_complex',';'.join(filters),'-t',str(duration),'-map','[finished_video]',*graph_audio,*mix_map]
        else:
            audio=1 if letters or ai else 0
            source_args,mix_map=mapped_mix(source_args,mix)
            command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-threads','2',*source_args,'-t',str(duration),'-map','0:v:0']
            if mix:command+=mix_map
            elif voice or not cfg['mute']:command+=['-map',f'{audio}:a:0?']
            command+=['-vf',','.join(vf)]
        command+=['-c:v','libx264','-preset','veryfast','-crf','23','-threads','2','-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k','-movflags','+faststart',str(output)]
        subprocess.run(command,capture_output=True,timeout=480,check=True)
        if mix:mix.unlink()
        result=probe(output)
        if result['width']!=WIDTH or result['height']!=HEIGHT:raise ValueError('render size')
        manifest={'version':1,'title':p['title'],'headline':doc['headline'],'script':doc['script'],'settings':cfg,'source_url':doc.get('source_url',''),'context':doc.get('context',''),'duration':duration,'caption_timing':'manual timeline' if doc.get('cues') else 'equal segments; not speech aligned','narration':'original audio only; no synthetic voice','blurs':doc.get('blurs') or [],'cues':doc.get('cues') or [],'credit':doc.get('credit',''),'credit_on':doc.get('credit_on',True),'approved_hash':doc['approved_hash']}
        archive_files=['short.mp4','captions.srt','project.json']
        if bundle:=doc.get('shorts_bundle'):
            with CONNECT() as con:
                channel=con.execute('SELECT name FROM production_channels WHERE id=?',(p['channel_id'],)).fetchone()
            posting={**bundle,'title':p['title'],'channel':channel['name'] if channel else p['channel_id']}
            manifest['posting']=posting
            (run/'posting.txt').write_text(
                f"채널: {posting['channel']}\n제목: {posting['title']}\n\n{posting['description']}\n",encoding='utf-8')
            archive_files.append('posting.txt')
        manifest['overlays']=doc.get('overlays',[])
        if mix:manifest['audio_mix']=doc['audio_mix']
        if uploaded_voice(doc):manifest['narration']='uploaded audio'
        if cuts:manifest['clips']=cuts
        if doc.get('quotes'):manifest['quotes']=doc['quotes']
        if voice:
            import qa
            mixed_audio=run/'mixed-audio.wav'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(output),'-vn','-c:a','pcm_s16le',str(mixed_audio)],capture_output=True,timeout=60,check=True)
            report=qa.check_report(doc['analysis'],{**script,'approved':True},narration_qa_data(doc,plan),mixed_audio)
            check_rendered_narration(p,report,min(duration,result['duration']),run/'narration-check.wav')
            report['audio_scope']='rendered_mix'
            with output.open('rb') as video:
                report['output_sha256']=hashlib.file_digest(video,'sha256').hexdigest()
            doc['qa_report']=report
            manifest.update(version=2,analysis=doc['analysis'],generated_script=script,narration=voice,edit_plan=plan,caption_timing='manual timeline' if doc.get('cues') else 'measured sentence WAV duration; chunks within sentence',qa_report=report)
            (run/'qa-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            archive_files.append('qa-report.json')
            mixed_audio.unlink()
        if bundle_voice:=doc.get('bundle_narration'):
            import qa
            track=doc['audio_mix']['narration']
            if track['muted'] or not track['volume'] or track['clip']['id']!=bundle_voice['clip_id']:
                raise ToolError('최종 영상에 자동 나레이션 트랙을 넣을 수 없습니다.')
            mixed_audio=run/'bundle-final.wav'
            subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(output),
                            '-map','0:a:0','-c:a','pcm_s16le',str(mixed_audio)],
                           capture_output=True,check=True,timeout=60)
            stats=qa.audio_stats(mixed_audio)
            mixed_audio.unlink()
            if not stats['peak'] or stats['clipped_samples']:
                raise ToolError('최종 MP4 오디오가 무음이거나 클리핑됐습니다.')
            manifest.update(narration=bundle_voice,caption_timing='measured sentence WAV duration',
                            bundle_audio_qa=stats)
        (run/'project.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(run/'project.zip','w',compression=zipfile.ZIP_STORED) as archive:
            for name in archive_files:archive.write(run/name,name)
        doc.update({'output':str(output.relative_to(directory)),'archive':str((run/'project.zip').relative_to(directory)),'message':'MP4와 자막·프로젝트 설정 묶음 제작이 완료되었습니다. 원음 유지 방식이며 합성 음성은 없습니다.','render_info':result})
        if voice:doc['message']='음성 길이에 맞춘 장면·자막과 원음 덕킹을 렌더링했습니다. QA 리포트를 확인하세요.'+(' 모의 음성은 무음입니다.' if voice['mock'] else '')
        if bundle_voice:doc['message']='채널 나레이션과 음성 타이밍에 맞춘 자막을 렌더링했습니다.'
        shutil.rmtree(fonts,ignore_errors=True)
        p['status']='completed';store(p)
    except Exception as exc:
        if not isinstance(exc,(ValueError,OSError,subprocess.SubprocessError)):traceback.print_exc()
        shutil.rmtree(run,ignore_errors=True)
        p['status']='error';doc['message']=str(exc) if isinstance(exc,ToolError) else '렌더링에 실패했습니다. 원본 형식과 시작 시간·길이를 확인하고 다시 시도하세요.'
        try:store(p)
        except Exception:traceback.print_exc()
    finally:LOCK.release()


def font_file(name):
    """A font from FONTS_DIR for the edit-screen preview."""
    if not name or not any(f['file']==name for f in available_fonts()):raise ValueError('font')
    return font_path(name)


def asset(project_id,kind):
    p=project(project_id);doc=p['document'];directory=(PROJECT_ROOT/p['id']).resolve()
    if kind=='source':relative=(doc.get('source') or {}).get('filename')
    elif kind=='analysis':relative='analysis.json' if doc.get('analysis') else None
    elif kind=='narration':relative=(doc.get('narration') or {}).get('audio_file')
    elif kind in ('output','archive'):relative=doc.get(kind) if p['status']=='completed' else None
    else:relative=None
    if not relative:raise ValueError('파일이 아직 준비되지 않았습니다.')
    path=(directory/relative).resolve()
    if not path.is_relative_to(directory) or not path.is_file():raise ValueError('파일을 찾을 수 없습니다.')
    return path


def waveform(project_id,kind,start,duration):
    import waveforms
    if kind in ('source','narration'):
        path=asset(project_id,kind)
        formats=SOURCE_FORMATS if kind=='source' else 'wav'
    elif re.fullmatch(r'[0-9a-f]{32}',kind):
        directory=audio_directory(project(project_id)).resolve()
        path=(directory/(kind+'.audio')).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError('오디오 파일을 찾을 수 없습니다.')
        formats=audio_mixer.FORMATS
    else:
        raise ValueError('파형을 표시할 오디오를 선택하세요.')
    return waveforms.peaks(path,formats,start,duration)
