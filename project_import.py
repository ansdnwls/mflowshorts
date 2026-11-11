"""Start a production project from a share link and safely attach its source."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid

import production
import share_import


SLOTS=threading.BoundedSemaphore(2)
FALLBACK=' 같은 프로젝트에서 원본 파일을 직접 올릴 수 있습니다.'


def source_record(text,note):
    platform,url,short=share_import.from_share(text)
    return {'platform':platform,'share_url':url,'original_url':'' if short else share_import.public_url(url,platform),
            'title':'','author':'','author_id':'','rights_note':share_import.rights_note(note),
            'status':'pending','message':'공유 링크를 저장했습니다.'}


@production.serialized
def start(project_id, worker, python, note=None):
    p=production.project(project_id)
    if p['status']=='rendering':
        raise ValueError('제작 중에는 공유 링크 설정을 바꿀 수 없습니다.')
    doc=p['document']
    record=doc.get('share_import')
    if not record:
        raise ValueError('이 프로젝트에 저장된 공유 링크가 없습니다.')
    if record['status'] in ('queued','running'):
        return record
    if note is not None:
        previous=record['rights_note']
        record['rights_note']=share_import.rights_note(note)
        if previous!=record['rights_note']:
            doc.pop('publish_approval',None)
    if doc.get('source'):
        if record['status']!='imported':
            record.update(status='manual',message='프로젝트에 원본 파일이 있습니다. 그대로 편집할 수 있습니다.')
    elif not Path(python).is_file():
        record.update(status='error',message='Windows 미디어 도구를 먼저 설치하세요.'+FALLBACK)
    elif not SLOTS.acquire(blocking=False):
        record.update(status='error',message='가져오기 작업 두 개가 진행 중입니다. 잠시 뒤 다시 시도하세요.'+FALLBACK)
    else:
        ident=uuid.uuid4().hex
        record.update(status='queued',message='공유 링크 가져오기를 대기하고 있습니다.',job_id=ident)
        try:
            production.store(p)
            threading.Thread(target=run,args=(project_id,ident,dict(record),worker,python),daemon=True).start()
        except Exception:
            SLOTS.release()
            record.update(status='error',message='가져오기 작업을 시작하지 못했습니다.'+FALLBACK)
            production.store(p)
        return record
    production.store(p)
    return record


def run(project_id,job_id,record,worker,python):
    try:
        update(project_id,job_id,{'status':'running','message':'플랫폼의 공개 정보를 확인하고 있습니다.'})
        directory=production.PROJECT_ROOT/project_id
        directory.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='import-',dir=directory) as staging:
            task={'platform':record['platform'],'url':record['share_url'],'action':'share_import',
                  'directory':staging,'download':False}
            result=worker_result(worker,python,task,80)
            if result.get('status')=='metadata_only' and record['rights_note']:
                update(project_id,job_id,{**result,'status':'running','message':'공개 정보를 저장했습니다. 원본을 가져오고 있습니다.'})
                if production.project(project_id)['document'].get('source'):
                    update(project_id,job_id,result)
                    return
                result=worker_result(worker,python,{**task,'download':True},240)
            media=None
            if result.get('status')=='downloaded':
                name=result.get('filename')
                if name in ('video.mp4','video.webm','video.mov'):
                    path=Path(staging)/name
                    try:
                        if not path.is_symlink() and 0<path.stat().st_size<=200*1024*1024:
                            media=(path,production.probe(path))
                    except (OSError,ValueError):
                        pass
                if not media:
                    result.update(status='error',message='가져온 파일을 영상으로 열 수 없습니다.')
            update(project_id,job_id,result,media)
    except (OSError,ValueError,subprocess.SubprocessError):
        update(project_id,job_id,{'status':'error','message':'가져오기 연결 또는 파일 처리에 실패했습니다.'+FALLBACK})
    finally:
        SLOTS.release()


def worker_result(worker,python,task,timeout):
    process=subprocess.run([str(python),str(worker)],input=json.dumps(task),text=True,encoding='utf-8',
                           errors='replace',capture_output=True,timeout=timeout)
    outcome=json.loads(process.stdout)
    result=outcome.get('result') if isinstance(outcome,dict) and outcome.get('ok') else None
    if not isinstance(result,dict):
        return {'status':'error','message':'플랫폼 정보를 가져오지 못했습니다.'}
    return result


@production.serialized
def update(project_id,job_id,result,media=None):
    p=production.project(project_id)
    doc=p['document']
    record=doc.get('share_import') or {}
    if record.get('job_id')!=job_id:
        return
    old_url=record['original_url'] or share_import.public_url(record['share_url'],record['platform'])
    for key in ('title','author','author_id'):
        if isinstance(result.get(key),str) and result[key]:
            record[key]=result[key][:300]
    if result.get('original_url'):
        try:
            platform,url,short=share_import.classify(result['original_url'])
            if platform==record['platform'] and not short:
                record['original_url']=share_import.public_url(url,platform)
                if doc.get('source_url')==old_url and not doc.get('approved_hash') and p['status']!='rendering':
                    doc['source_url']=record['original_url']
        except ValueError:
            pass
    status=result.get('status')
    message=result.get('message','')
    if status not in ('running','metadata_only','unsupported','error','downloaded'):
        status,message='error','가져오기 응답을 읽지 못했습니다.'
    target=None
    if status=='downloaded' and media and not doc.get('source') and p['status']!='rendering':
        path,info=media
        target=production.PROJECT_ROOT/project_id/(uuid.uuid4().hex+'.source')
        path.replace(target)
        production.apply_source(p,target,info)
        status,message='imported','공유 링크 영상이 원본으로 추가됐습니다. 타임라인에서 재생할 수 있습니다.'
    elif doc.get('source') and status!='running':
        status,message='manual','프로젝트에 직접 올린 원본을 사용합니다. 확인된 링크 정보는 저장했습니다.'
    elif status in ('unsupported','error') and FALLBACK not in message:
        message+=FALLBACK
    record.update(status=status,message=message)
    try:
        production.store(p)
    except Exception:
        if target:
            target.unlink(missing_ok=True)
        raise


@production.serialized
def recover():
    with production.CONNECT() as con:
        rows=con.execute("""SELECT id FROM production_projects
            WHERE json_extract(document,'$.share_import.status') IN ('queued','running')""").fetchall()
    for row in rows:
        p=production.project(row['id'])
        record=p['document']['share_import']
        update(p['id'],record['job_id'],{'status':'error','message':'앱 재시작으로 가져오기가 중단됐습니다. 다시 요청하세요.'})
