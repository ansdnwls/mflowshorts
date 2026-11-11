"""Approved-script narration, measured sentence cues, and explicit activation."""
import hashlib,json,math,os,re,subprocess,threading,time,uuid,wave,textwrap,contextlib
from pathlib import Path
import urllib.request,urllib.error
import production
LOCK=threading.Lock()

def init():
 with production.CONNECT() as con:con.execute('CREATE TABLE IF NOT EXISTS narrations (id TEXT PRIMARY KEY,project_id TEXT,signature TEXT,status TEXT,mode TEXT,message TEXT,data TEXT,updated_at TEXT)')

def signature(doc):
 if not doc.get('source') or not doc.get('script'):raise ValueError('원본과 대본을 먼저 준비하세요.')
 return hashlib.sha256(json.dumps({'script':doc['script'],'source':doc['source'],'start':doc['settings']['start'],'duration':doc['settings']['duration']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def approved(project_id):
 p=production.project(project_id)
 if p['status']=='rendering' or p['document'].get('approved_hash')!=production.fingerprint(p['document']):raise ValueError('현재 대본·설정을 먼저 승인하세요.')
 signature(p['document']);return p

def get(ident):
 with production.CONNECT() as con:r=con.execute('SELECT * FROM narrations WHERE id=?',(ident,)).fetchone()
 if not r:raise ValueError('내레이션을 찾을 수 없습니다.')
 a=dict(r);a['data']=json.loads(a['data']);return a

def folder(a):return production.PROJECT_ROOT/a['project_id']/'narration'/a['id']
def store(a,status,message):
 with production.CONNECT() as con:con.execute('UPDATE narrations SET status=?,message=?,data=?,updated_at=? WHERE id=?',(status,message,json.dumps(a['data'],ensure_ascii=False),production.now(),a['id']))

def state(project_id):
 p=production.project(project_id)
 with production.CONNECT() as con:r=con.execute('SELECT id FROM narrations WHERE project_id=? ORDER BY rowid DESC LIMIT 1',(project_id,)).fetchone()
 a=get(r[0]) if r else None
 if a:
  try:a['stale']=a['signature']!=signature(p['document'])
  except ValueError:a['stale']=True
 return {'narration':a,'selected':p['document'].get('narration_id'),'ai_configured':bool(os.environ.get('MFLOW_AI_KEY'))}

def probe(path):
 try:
  result=subprocess.run(['ffprobe','-v','error','-format_whitelist','wav,mp3,mov,mp4,m4a,ogg,flac','-protocol_whitelist','file','-show_format','-show_streams','-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15,check=True)
  info=json.loads(result.stdout);seconds=float(info['format']['duration'])
  if not math.isfinite(seconds) or not 0<seconds<=120 or not any(s['codec_type']=='audio' for s in info['streams']) or any(s['codec_type']=='video' for s in info['streams']):raise ValueError('audio')
  return seconds
 except (OSError,ValueError,KeyError,subprocess.SubprocessError):raise ValueError('120초 이하의 유효한 WAV/MP3/M4A/OGG/FLAC 음성을 선택하세요.') from None

def normalize(source,destination):
 probe(source)
 subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-format_whitelist','wav,mp3,mov,mp4,m4a,ogg,flac','-protocol_whitelist','file','-i',str(source),'-vn','-ac','1','-ar','24000','-c:a','pcm_s16le',str(destination)],capture_output=True,check=True,timeout=30)
 return probe(destination)

def chunks(script,limit=20):
 result=[' '.join(word for line in group for word,highlight in line) for group in production.caption_chunks(script)]
 if not 1<=len(result)<=limit:raise ValueError(f'음성 자막은 최대 {limit}개 구간입니다. 대본을 간결하게 정리하세요.')
 return result


def create(p,mode):
 ident=uuid.uuid4().hex
 with production.CONNECT() as con:con.execute('INSERT INTO narrations VALUES (?,?,?,?,?,?,?,?)',(ident,p['id'],signature(p['document']),'preparing',mode,'음성을 준비합니다.','{}',production.now()))
 a=get(ident);folder(a).mkdir(parents=True,exist_ok=True);return a

def upload(project_id,reader,size):
 p=approved(project_id)
 if not 0<size<=20*1024*1024:raise ValueError('음성 파일은 20MB 이하입니다.')
 if not LOCK.acquire(blocking=False):raise ValueError('다른 음성 작업이 진행 중입니다.')
 a=None
 try:
  a=create(p,'upload');source=folder(a)/'original.audio'
  with source.open('xb') as file:
   remaining=size
   while remaining:
    b=reader.read(min(1024*1024,remaining))
    if not b:raise ValueError('음성 업로드가 중단되었습니다.')
    file.write(b);remaining-=len(b)
  seconds=normalize(source,folder(a)/'voice.wav');parts=chunks(p['document']['script'],40)
  a['data']={'duration':seconds,'timing':'manual_estimate','cues':[{'text':t,'start':round(seconds*i/len(parts),3),'end':round(seconds*(i+1)/len(parts),3)} for i,t in enumerate(parts)],'sha256':hashlib.sha256((folder(a)/'voice.wav').read_bytes()).hexdigest()}
  store(a,'ready','음성 업로드 완료. 자막 시각은 예상 균등 배치입니다. 들으면서 수정·확인하세요.')
  return a['id']
 except Exception as e:
  if a:
   store(a,'error',str(e) if isinstance(e,ValueError) else '음성을 변환하지 못했습니다.')
   for path in folder(a).glob('*'):
    if path.is_file():path.unlink(missing_ok=True)
  raise ValueError(str(e) if isinstance(e,ValueError) else '음성을 변환하지 못했습니다.') from None
 finally:LOCK.release()

def start(project_id):
 p=approved(project_id)
 if not os.environ.get('MFLOW_AI_KEY'):raise ValueError('AI 음성에는 MFLOW_AI_KEY 등록이 필요합니다.')
 parts=chunks(p['document']['script'])
 if not LOCK.acquire(blocking=False):raise ValueError('다른 음성 작업이 진행 중입니다.')
 try:
  a=create(p,'tts');threading.Thread(target=generate,args=(a,p,parts),daemon=True).start();return a['id']
 except Exception:LOCK.release();raise

def speech(text):
 request=urllib.request.Request('https://api.openai.com/v1/audio/speech',data=json.dumps({'model':os.environ.get('MFLOW_TTS_MODEL','tts-1'),'voice':'alloy','input':text,'response_format':'wav'},ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+os.environ['MFLOW_AI_KEY'],'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(request,timeout=45) as response:body=response.read(10*1024*1024+1)
  if len(body)>10*1024*1024:raise ValueError('음성 응답이 너무 큽니다.')
  return body
 except (urllib.error.URLError,TimeoutError):raise ValueError('음성 API 연결에 실패했습니다. 키·할당량·api.openai.com 접근을 확인하세요.') from None

def generate(a,p,parts):
 try:
  elapsed=0;data=[];started=time.monotonic();target=min(p['document']['settings']['duration'],p['document']['source']['duration']-p['document']['settings']['start']);pcm=[]
  for i,text in enumerate(parts):
   if time.monotonic()-started>180:raise ValueError('음성 생성 시간 제한을 초과했습니다.')
   source=folder(a)/f'raw-{i}.wav';source.write_bytes(speech(text));dest=folder(a)/f'part-{i}.wav';seconds=normalize(source,dest)
   if elapsed+seconds>target:raise ValueError('음성이 영상 구간보다 깁니다. 대본을 줄이거나 영상 길이를 늘린 후 다시 생성하세요.')
   data.append({'text':text,'start':round(elapsed,3),'end':round(elapsed+seconds,3)});elapsed+=seconds
   with wave.open(str(dest),'rb') as w:pcm.append(w.readframes(w.getnframes()))
  with wave.open(str(folder(a)/'voice.wav'),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b''.join(pcm))
  a['data']={'duration':probe(folder(a)/'voice.wav'),'timing':'measured_sentences','cues':data,'sha256':hashlib.sha256((folder(a)/'voice.wav').read_bytes()).hexdigest(),'voice':'alloy','model':os.environ.get('MFLOW_TTS_MODEL','tts-1')}
  store(a,'ready','문장별 실제 음성 길이로 자막을 배치했습니다. 단어별 강제 정렬은 아닙니다. 들어보고 적용하세요.')
 except Exception as e:store(a,'error',str(e) if isinstance(e,ValueError) else '음성 생성 중 오류가 발생했습니다. 기존 대본·제작 음성은 유지됩니다.')
 finally:
  for pattern in ('raw-*.wav','part-*.wav'):
   for path in folder(a).glob(pattern):
    with contextlib.suppress(OSError):path.unlink(missing_ok=True)
  LOCK.release()

def file(ident):
 a=get(ident)
 if a['status']!='ready':raise ValueError('완료된 음성이 없습니다.')
 path=folder(a)/'voice.wav'
 if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=a['data']['sha256']:raise ValueError('음성 파일이 없거나 변경되었습니다.')
 return path

@production.serialized
def activate(ident,cues,confirmed):
 a=get(ident);p=production.project(a['project_id']);doc=p['document']
 if confirmed is not True:raise ValueError('대본·음성·자막 시각 일치를 직접 확인하세요.')
 if p['status']=='rendering' or a['signature']!=signature(doc):raise ValueError('원본·대본·구간이 변경됐습니다. 현재 대본을 승인하고 새 음성을 준비하세요.')
 path=file(ident);target=min(doc['settings']['duration'],doc['source']['duration']-doc['settings']['start'])
 if a['data']['duration']>target:raise ValueError('음성이 영상 구간보다 깁니다. 잘라서 적용하지 않았습니다.')
 rows=production.cues(cues)
 if not rows:raise ValueError('음성에 맞는 자막 구간을 입력하세요.')
 previous=0
 for cue in rows:
  if cue['start']<previous or cue['end']<=cue['start'] or cue['end']>a['data']['duration']+0.01:raise ValueError('자막은 겹치지 않고 음성 길이 안에 있어야 합니다.')
  previous=cue['end']
 doc['narration_id']=ident;doc['cues']=rows;doc['approved_hash']=None;doc.pop('output',None);doc.pop('archive',None);p['status']='script_ready';doc['message']='내레이션과 자막을 적용했습니다. 렌더링 전 다시 승인하세요.';production.store(p)

@production.serialized
def disable(project_id):
 p=production.project(project_id)
 if p['status']=='rendering':raise ValueError('렌더링 중에는 음성을 바꿀 수 없습니다.')
 p['document'].pop('narration_id',None);p['document']['approved_hash']=None;p['document'].pop('output',None);p['document'].pop('archive',None);p['status']='script_ready' if p['document']['script'] else 'draft';production.store(p)

def for_render(p):
 ident=p['document'].get('narration_id')
 if not ident:return None
 a=get(ident)
 if a['project_id']!=p['id'] or a['signature']!=signature(p['document']):raise ValueError('선택한 내레이션이 이전 대본·원본용입니다. 새 음성을 준비하거나 선택 해제하세요.')
 return file(ident),a

def timing(p,a):
 return 'measured_sentences' if a['mode']=='tts' and p['document'].get('cues')==production.cues(a['data']['cues']) else 'manual_reviewed'
