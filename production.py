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
# Extra fonts the user drops in this folder (.ttf/.otf/.ttc) can be picked per project for the title and captions.
# The folder is not in git because many free fonts do not allow redistribution.
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
        for p in projects:p['comment_info']=comment_info(con,p['document'])
    return {'channels':channels,'projects':projects,'styles':STYLES,'fonts':available_fonts(),'render_available':bool(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(FONT).is_file())}


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


def store(p):
    with CONNECT() as con: con.execute('UPDATE production_projects SET title=?,channel_id=?,status=?,document=?,updated_at=? WHERE id=?',(p['title'],p['channel_id'],p['status'],json.dumps(p['document'],ensure_ascii=False),now(),p['id']))


def default_settings():return {'style':'basic','duration':58,'start':0,'mute':False,'mirror':False,'captions':True,'layout':'band','title_font':'','caption_font':''}


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


def settings(data):
    result={**default_settings(),**{k:v for k,v in data.items() if k in default_settings()}}
    try:result['duration']=float(result['duration']);result['start']=float(result['start'])
    except (ValueError,TypeError):raise ValueError('영상 길이와 시작 시간을 확인하세요.') from None
    if not math.isfinite(result['duration']) or not 1<=result['duration']<=60 or not math.isfinite(result['start']) or not 0<=result['start']<=5400:raise ValueError('길이는 1~60초, 시작은 0~5400초입니다.')
    fonts={f['file'] for f in available_fonts()}
    if any(not isinstance(result[k],str) or (result[k] and result[k] not in fonts) for k in ('title_font','caption_font')):raise ValueError('글씨체를 다시 선택하세요. fonts 폴더에 파일이 있어야 합니다.')
    if result['style'] not in STYLES or result['layout'] not in LAYOUTS or any(type(result[k]) is not bool for k in ('mute','mirror','captions')):raise ValueError('스타일 설정을 확인하세요.')
    return result


@serialized
def create_channel(data):
    name=str(data.get('name','')).strip();language=data.get('language','ko')
    if not name or len(name)>60 or language not in ('ko','en','zh'):raise ValueError('채널 이름(60자 이하)과 언어를 확인하세요.')
    ident=uuid.uuid4().hex
    with CONNECT() as con:con.execute('INSERT INTO production_channels VALUES (?,?,?,?,?)',(ident,name,language,json.dumps(default_settings()),now()))
    return ident


def credit_line(channel):
    """Default credit under the title: '출처. 채널명' (60 characters at most)."""
    name=re.sub(r'\s+',' ',str(channel or '')).strip()
    return ('출처. '+name)[:60].rstrip() if name and name not in ('작성자 미제공',) else ''


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
            video=con.execute('SELECT url,channel FROM videos WHERE id=?',(str(data['source_video_id']),)).fetchone()
            note=con.execute('SELECT notes,attribution FROM research WHERE video_id=?',(str(data['source_video_id']),)).fetchone()
        if not video:raise ValueError('발굴한 영상을 찾을 수 없습니다.')
        doc['source_url']=video['url']
        doc['credit']=credit_line(video['channel'])
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
    credit=re.sub(r'\s+',' ',str(data.get('credit',doc.get('credit','')))).strip();cards=comments(data.get('comments',doc.get('comments',[])))
    if len(credit)>60:raise ValueError('출처 표시는 60자 이내로 입력하세요.')
    if len(textwrap.wrap(headline,18))>3:raise ValueError('상단 제목은 화면에서 3줄(약 50자) 이내여야 합니다. 줄여서 입력하세요.')
    if not title or len(title)>100 or not headline or len(headline)>60 or len(script)>800 or len(context)>2000 or len(source_url)>2048:raise ValueError('이름 100자·상단 제목 60자·대본 800자·맥락 메모 2000자 이내로 입력하세요.')
    if source_url:
        from urllib.parse import urlsplit
        u=urlsplit(source_url)
        if u.scheme!='https' or not u.hostname or u.username or u.password:raise ValueError('출처는 HTTPS 링크로 입력하세요.')
    p['title']=title;doc.update({'script':script,'headline':headline,'context':context,'source_url':source_url,'credit':credit,'comments':cards,'settings':settings(data.get('settings',doc['settings'])),'approved_hash':None})
    doc.pop('output',None);doc.pop('archive',None)
    p['status']='script_ready' if script and doc['source'] else 'source_ready' if doc['source'] else 'draft'
    doc['message']='대본과 설정을 검토한 뒤 승인하세요.' if p['status']=='script_ready' else '원본과 대본을 준비하세요.'
    store(p)
    if data.get('save_template'):
        with CONNECT() as con:con.execute('UPDATE production_channels SET template=? WHERE id=?',(json.dumps(doc['settings']),p['channel_id']))


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
    p['document'].update({'source':{'filename':path.name,**info},'approved_hash':None,'message':'원본 업로드 완료. 스타일과 대본을 준비하세요.'})
    p['document'].pop('output',None);p['document'].pop('archive',None)
    p['status']='script_ready' if p['document']['script'] else 'source_ready';store(p)
    return info


def fingerprint(doc):
    # credit/comments only count when set, so projects approved before they existed stay approved.
    data={k:doc.get(k) for k in ('source','script','headline','settings')}
    data.update({k:doc[k] for k in ('credit','comments') if doc.get(k)})
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


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


def comment_events(cards,layout,duration):
    """Comment cards, each shown from its 'at' time until the next card (or the end)."""
    events=[];top=COMMENT_TOP.get(layout,COMMENT_TOP['band']);width=WIDTH-2*COMMENT_MARGIN;left=COMMENT_MARGIN
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


def subtitles(doc,style,duration,captions,title_font=None,caption_font=None,layout='band'):
    """Build the ASS subtitle file (headline + narration captions) and the matching SRT text."""
    lines,size=title_lines(doc['headline'])
    title=r'\N'.join(('{\\c'+ass_color(style['title' if i==0 else 'title2'])+'}')+ass_text(line) for i,line in enumerate(lines))
    title_family,title_bold=ass_font(title_font or FONT);caption_family,caption_bold=ass_font(caption_font or FONT);ui_family,ui_bold=ass_font(FONT)
    credit=doc.get('credit','');title_bottom=TITLE_HEIGHT-(px(46) if credit else px(26))
    ass=['[Script Info]','ScriptType: v4.00+',f'PlayResX: {WIDTH}',f'PlayResY: {HEIGHT}','WrapStyle: 2','ScaledBorderAndShadow: yes','',
         '[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
         f'Style: Title,{title_family},{size},{ass_color(style["title"])},&H000000FF&,&H00000000&,&H00000000&,{title_bold},0,0,0,100,100,0,0,1,{TITLE_OUTLINE},0,2,0,0,0,1',
         f'Style: Caption,{caption_family},{CAPTION_FONT},{ass_color(style["caption"])},&H000000FF&,&H00000000&,&H80000000&,{caption_bold},0,0,0,100,100,0,0,1,{CAPTION_OUTLINE},{px(2)},2,0,0,0,1',
         f'Style: Credit,{ui_family},{CREDIT_FONT},&H00D8D8D8&,&H000000FF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,{px(1)},0,2,0,0,0,1',
         f'Style: Card,{ui_family},20,&H00FFFFFF&,&H000000FF&,&H00000000&,&H60000000&,0,0,0,0,100,100,0,0,1,0,{px(3)},7,0,0,0,1',
         f'Style: CommentName,{ui_family},{COMMENT_NAME_FONT},&H00707070&,&H000000FF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1',
         f'Style: Comment,{ui_family},{COMMENT_FONT},&H00141414&,&H000000FF&,&H00000000&,&H00000000&,{ui_bold},0,0,0,100,100,0,0,1,0,0,7,0,0,0,1',
         '','[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
         f'Dialogue: 0,{ass_time(0)},{ass_time(duration+1)},Title,,0,0,0,,{{\\pos({WIDTH//2},{title_bottom})}}{title}']
    if credit:ass.append(f'Dialogue: 0,{ass_time(0)},{ass_time(duration+1)},Credit,,0,0,0,,{{\\pos({WIDTH//2},{TITLE_HEIGHT-px(12)})}}{ass_text(credit)}')
    ass.extend(comment_events(doc.get('comments') or [],layout,duration))
    srt=[];chunks=caption_chunks(doc['script'])
    for index,chunk in enumerate(chunks):
        start=duration*index/len(chunks);end=duration*(index+1)/len(chunks)
        srt.extend([str(index+1),f'{srt_time(start)} --> {srt_time(end)}','\n'.join(' '.join(w for w,_ in line) for line in chunk),''])
        text=r'\N'.join(' '.join(('{\\c'+ass_color(style['highlight'])+'}'+ass_text(w)+'{\\c'+ass_color(style['caption'])+'}') if marked else ass_text(w) for w,marked in line) for line in chunk)
        if captions:ass.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{{\\pos({WIDTH//2},{CAPTION_BOTTOM.get(layout,CAPTION_BOTTOM["band"])})}}{text}')
    return '\n'.join(ass)+'\n','\n'.join(srt)


def ass_time(seconds):
    cs=round(seconds*100);hours,cs=divmod(cs,360000);minutes,cs=divmod(cs,6000);seconds,cs=divmod(cs,100)
    return f'{hours}:{minutes:02}:{seconds:02}.{cs:02}'


def srt_time(seconds):
    ms=round(seconds*1000);hours,ms=divmod(ms,3600000);minutes,ms=divmod(ms,60000);seconds,ms=divmod(ms,1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def render(p):
    doc=p['document'];directory=PROJECT_ROOT/p['id'];run=directory/uuid.uuid4().hex
    try:
        run.mkdir(parents=True)
        source=directory/doc['source']['filename'];cfg=doc['settings'];duration=min(cfg['duration'],doc['source']['duration']-cfg['start'])
        style=STYLES[cfg['style']]
        vf=layout_filters(cfg.get('layout','fill'))
        if cfg['mirror']:vf.append('hflip')
        # libass loads the font from its own folder, so it does not depend on the system font setup (fontconfig on Windows).
        title_font=font_path(cfg.get('title_font',''));caption_font=font_path(cfg.get('caption_font',''))
        fonts=run/'fonts';fonts.mkdir()
        for path in {title_font,caption_font,Path(FONT)}:shutil.copyfile(path,fonts/path.name)
        ass,srt=subtitles(doc,style,duration,cfg['captions'],title_font,caption_font,cfg.get('layout','fill'))
        (run/'overlay.ass').write_text(ass,encoding='utf-8')
        vf.extend([f'drawbox=x=0:y=0:w=iw:h={TITLE_HEIGHT}:color=black:t=fill',f"ass=filename={filter_path(run/'overlay.ass')}:fontsdir={filter_path(fonts)}"])
        (run/'captions.srt').write_text(srt,encoding='utf-8')
        output=run/'short.mp4'
        command=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-ss',str(cfg['start']),'-threads','2','-format_whitelist',SOURCE_FORMATS,'-protocol_whitelist','file','-i',str(source),'-t',str(duration),'-map','0:v:0']
        if not cfg['mute']:command+=['-map','0:a:0?']
        command+=['-vf',','.join(vf),'-c:v','libx264','-preset','veryfast','-crf','23','-threads','2','-pix_fmt','yuv420p','-r','30','-c:a','aac','-b:a','128k','-movflags','+faststart',str(output)]
        subprocess.run(command,capture_output=True,timeout=480,check=True)
        result=probe(output)
        if result['width']!=WIDTH or result['height']!=HEIGHT:raise ValueError('render size')
        manifest={'version':1,'title':p['title'],'headline':doc['headline'],'script':doc['script'],'settings':cfg,'source_url':doc.get('source_url',''),'context':doc.get('context',''),'duration':duration,'caption_timing':'equal segments; not speech aligned','narration':'original audio only; no synthetic voice','approved_hash':doc['approved_hash']}
        (run/'project.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(run/'project.zip','w',compression=zipfile.ZIP_STORED) as archive:
            for name in ('short.mp4','captions.srt','project.json'):archive.write(run/name,name)
        doc.update({'output':str(output.relative_to(directory)),'archive':str((run/'project.zip').relative_to(directory)),'message':'MP4와 자막·프로젝트 설정 묶음 제작이 완료되었습니다. 원음 유지 방식이며 합성 음성은 없습니다.','render_info':result})
        shutil.rmtree(fonts,ignore_errors=True)
        p['status']='completed';store(p)
    except Exception as exc:
        if not isinstance(exc,(ValueError,OSError,subprocess.SubprocessError)):traceback.print_exc()
        shutil.rmtree(run,ignore_errors=True)
        p['status']='error';doc['message']='렌더링에 실패했습니다. 원본 형식과 시작 시간·길이를 확인하고 다시 시도하세요.'
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
    elif kind in ('output','archive'):relative=doc.get(kind) if p['status']=='completed' else None
    else:relative=None
    if not relative:raise ValueError('파일이 아직 준비되지 않았습니다.')
    path=(directory/relative).resolve()
    if not path.is_relative_to(directory) or not path.is_file():raise ValueError('파일을 찾을 수 없습니다.')
    return path
