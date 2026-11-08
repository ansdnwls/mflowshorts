"""Reviewed reuse ideas and production history; popularity never implies reuse rights."""
import json
import math
import re
import uuid
from urllib.parse import urlsplit,urlunsplit
import production

RIGHTS={'unknown':'미확인','requested':'허가 문의 중','permission':'사용 허가 받음','licensed':'라이선스 확인','owned':'직접 소유','restricted':'사용 불가'}

def init():
    with production.CONNECT() as con:
        con.executescript('''CREATE TABLE IF NOT EXISTS material_notes (video_id TEXT PRIMARY KEY,hypothesis TEXT DEFAULT '',angle TEXT DEFAULT '',rights TEXT DEFAULT 'unknown',evidence TEXT DEFAULT '',group_url TEXT DEFAULT '',baseline TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS material_outcomes (id TEXT PRIMARY KEY,video_id TEXT,project_id TEXT,url TEXT,views INTEGER,likes INTEGER,note TEXT,recorded_at TEXT);''')

def text(value,limit):
    if not isinstance(value,str) or len(value)>limit:raise ValueError(f'메모는 {limit}자 이내로 입력하세요.')
    return value.strip()

def public_url(value,required=False):
    value=text(value,2048)
    if not value and not required:return ''
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port:raise ValueError('HTTPS 공개 링크를 입력하세요.')
    return urlunsplit(('https',p.netloc.lower(),p.path,p.query,''))

def baseline(con,video_id):
    row=con.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone()
    if not row:raise ValueError('발굴한 영상을 먼저 선택하세요.')
    v=dict(row);c=con.execute('SELECT * FROM channels WHERE id=?',(v.get('channel_id'),)).fetchone()
    ratio=round(v['views']/c['median_views'],2) if c and c['median_views'] and v['views'] is not None else None
    return {'views':v['views'],'likes':v['likes'],'comments':v['comments'],'median_ratio':ratio,'checked_at':v['checked_at'],'collected_at':production.now()}

def ensure(con,video_id):
    data=baseline(con,video_id)
    con.execute('INSERT OR IGNORE INTO material_notes(video_id,baseline,updated_at) VALUES (?,?,?)',(video_id,json.dumps(data),production.now()))

def collect(ids,min_views=1000000,min_ratio=3):
    if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) or len(i)>200 for i in ids):raise ValueError('현재 결과에서 1~500개 영상을 선택하세요.')
    try:views=int(min_views);ratio=float(min_ratio)
    except (ValueError,TypeError,OverflowError):raise ValueError('조회수·배수 기준을 확인하세요.') from None
    if not 1<=views<=10**12 or not math.isfinite(ratio) or not 1<=ratio<=1000:raise ValueError('조회수·배수 기준을 확인하세요.')
    matched=[]
    with production.CONNECT() as con:
        for ident in dict.fromkeys(ids):
            b=baseline(con,ident)
            if (b['views'] is not None and b['views']>=views) or (b['median_ratio'] is not None and b['median_ratio']>=ratio):
                ensure(con,ident)
                con.execute('INSERT INTO research(video_id,saved) VALUES (?,1) ON CONFLICT(video_id) DO UPDATE SET saved=1',(ident,));matched.append(ident)
    return matched

def save(data):
    ident=str(data.get('video_id',''));rights=data.get('rights','unknown')
    if rights not in RIGHTS:raise ValueError('사용 가능 상태를 확인하세요.')
    hypothesis=text(data.get('hypothesis',''),2000);angle=text(data.get('angle',''),2000);evidence=text(data.get('evidence',''),2000);group=public_url(data.get('group_url',''))
    with production.CONNECT() as con:
        ensure(con,ident)
        con.execute('UPDATE material_notes SET hypothesis=?,angle=?,rights=?,evidence=?,group_url=?,updated_at=? WHERE video_id=?',(hypothesis,angle,rights,evidence,group,production.now(),ident))
        con.execute('INSERT INTO research(video_id,saved) VALUES (?,1) ON CONFLICT(video_id) DO UPDATE SET saved=1',(ident,))

def number(value):
    if value in ('',None):return None
    if isinstance(value,bool) or not re.fullmatch(r'[0-9]{1,13}',str(value)) or int(value)>10**12:raise ValueError('조회수·좋아요는 0 이상의 정수 또는 미입력입니다.')
    return int(value)

def outcome(data):
    ident=str(data.get('video_id',''));project_id=str(data.get('project_id',''));url=public_url(data.get('url',''),True)
    views=number(data.get('views'));likes=number(data.get('likes'));note=text(data.get('note',''),1000)
    with production.CONNECT() as con:
        ensure(con,ident)
        row=con.execute('SELECT document FROM production_projects WHERE id=?',(project_id,)).fetchone()
        if not row or json.loads(row['document']).get('source_video_id')!=ident:raise ValueError('이 소재로 만든 프로젝트를 선택하세요.')
        con.execute('INSERT INTO material_outcomes VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,ident,project_id,url,views,likes,note,production.now()))

def state():
    with production.CONNECT() as con:
        materials={r['video_id']:{**dict(r),'baseline':json.loads(r['baseline'])} for r in con.execute('SELECT * FROM material_notes')}
        research={r['video_id']:dict(r) for r in con.execute('SELECT * FROM research')}
        projects=[]
        for r in con.execute('SELECT id,title,status,document FROM production_projects ORDER BY updated_at DESC'):
            doc=json.loads(r['document'])
            if doc.get('source_video_id'):projects.append({'id':r['id'],'title':r['title'],'status':r['status'],'video_id':doc['source_video_id']})
        outcomes=[dict(r) for r in con.execute('SELECT * FROM material_outcomes ORDER BY recorded_at DESC')]
    for ident,m in materials.items():
        m['ready']=bool(m['angle'] and m['evidence'] and m['rights'] in ('permission','licensed','owned') and research.get(ident,{}).get('status')=='verified')
        m['projects']=[p for p in projects if p['video_id']==ident];m['outcomes']=[o for o in outcomes if o['video_id']==ident]
        # Only identical user-entered source links are grouped; this does not verify common ownership.
        m['same_source']=[key for key,value in materials.items() if key!=ident and m['group_url'] and value['group_url']==m['group_url']]
    return {'materials':materials,'rights':RIGHTS}
