"""Persist the reviewed analysis -> approved script -> narration workflow."""
import hashlib
import json
import os
import traceback
import uuid

import analysis_script
import production


def editable(project_id):
    p = production.project(project_id)
    if p['status'] == 'rendering':
        raise ValueError('렌더링 중에는 분석·대본·음성을 변경할 수 없습니다.')
    if not p['document'].get('source'):
        raise ValueError('원본 영상을 먼저 업로드하세요.')
    return p


def invalidate(doc, script=False):
    doc['approved_hash'] = None
    for key in ('narration', 'qa_report', 'output', 'archive', 'publish_approval'):
        doc.pop(key, None)
    if script:
        if doc.pop('generated_script', None):
            doc['script'] = ''


def persist_analysis(p, analysis):
    doc = p['document']
    invalidate(doc, script=True)
    doc['analysis'] = analysis
    p['status'] = 'analysis_confirmed' if analysis['confirmed'] else 'analysis_ready'
    doc['message'] = '분석이 확정되었습니다. 대본을 생성하세요.' if analysis['confirmed'] else '장면별 사실과 추정을 검토하고 분석을 확정하세요.'
    # The DB document is authoritative; the standalone file is a readable export.
    directory = production.PROJECT_ROOT / p['id']
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / (uuid.uuid4().hex + '.analysis.json')
    temp.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(directory / 'analysis.json')
    production.store(p)
    return {'analysis': analysis, 'message': doc['message']}


@production.serialized
def analyze(project_id, provider=None):
    p = editable(project_id)
    source = p['document']['source']
    data = (provider or analysis_script.MockAnalysisProvider()).analyze(source)
    return persist_analysis(p, analysis_script.validate_analysis(data, source['duration']))


@production.serialized
def save_analysis(project_id, data):
    p = editable(project_id)
    return persist_analysis(p, analysis_script.validate_analysis(data, p['document']['source']['duration']))


@production.serialized
def confirm_analysis(project_id):
    p = editable(project_id)
    return persist_analysis(p, analysis_script.confirm_analysis(p['document'].get('analysis'), p['document']['source']['duration']))


def current_script(doc):
    script = analysis_script.validate_script(doc.get('generated_script'), doc.get('analysis'))
    if script['text'] != doc.get('script'):
        raise ValueError('대본 본문이 생성된 문장과 다릅니다. 분석을 수정한 뒤 대본을 다시 생성하세요.')
    return script


@production.serialized
def generate_script(project_id, provider=None):
    p = editable(project_id)
    doc = p['document']
    script = analysis_script.generate_script(doc.get('analysis'), provider)
    if len(script['text']) > 800:
        raise ValueError('생성된 대본이 800자를 넘습니다. 분석의 사실·추정 항목을 줄여주세요.')
    invalidate(doc)
    doc.update(generated_script=script, script=script['text'], message='모의 대본을 생성했습니다. 문장별 사실·추정 표시를 검토하고 승인하세요.' if script['provider'] == 'mock' else '대본을 검토하고 승인하세요.')
    p['status'] = 'script_ready'
    production.store(p)
    return {'script': script, 'message': doc['message']}


@production.serialized
def generate_narration(project_id, engine=None):
    return synthesize_narration(editable(project_id), engine)


def approved_script(doc):
    script = current_script(doc)
    if not doc.get('approved_hash') or doc['approved_hash'] != production.fingerprint(doc):
        raise ValueError('현재 대본을 먼저 승인하세요.')
    return script


def audio_digest(path):
    with path.open('rb') as audio:
        return hashlib.file_digest(audio, 'sha256').hexdigest()


@production.serialized
def queue_narration(project_id):
    import narration
    import editing
    p = production.project(project_id)
    existing = production.active_job(p, voice=True)
    if existing:
        return existing
    p = editable(project_id)
    doc = p['document']
    script = approved_script(doc)
    engine_key = os.environ.get('MFLOW_TTS_ENGINE', 'mock').strip().lower()
    engine = narration.configured_engine(engine_key)
    voice = doc.get('narration') or {}
    if voice.get('approved_hash') == doc['approved_hash'] and voice.get('audio_sha256'):
        try:
            editing.build_plan(doc['analysis'], script, voice, doc['source']['duration'])
            intact = audio_digest(production.asset(project_id, 'narration')) == voice['audio_sha256']
        except (OSError, ValueError):
            intact = False
        if not intact:
            for key in ('narration', 'output', 'archive', 'qa_report', 'publish_approval'):
                doc.pop(key, None)
        elif voice.get('engine') == engine.name:
            return {'status': 'completed', 'narration': voice, 'message': '생성된 나레이션을 다시 사용합니다.'}
    return production.enqueue_job(p, engine_key)


def run_narration_job(p):
    import narration
    try:
        synthesize_narration(p, narration.configured_engine(p['job_engine']))
    except Exception as exc:
        if not isinstance(exc, ValueError):
            traceback.print_exc()
        failed = production.project(p['id'])
        failed.update(status='error', render_job_id=p['render_job_id'])
        failed['document']['message'] = str(exc) if isinstance(exc, ValueError) else '나레이션 생성에 실패했습니다. 다시 시도하세요.'
        production.store(failed)


def synthesize_narration(p, engine):
    import narration
    import editing
    doc = p['document']
    script = approved_script(doc)
    run = production.PROJECT_ROOT / p['id'] / ('voice-' + uuid.uuid4().hex)
    result = narration.synthesize({**script, 'approved': True}, run, engine)
    editing.build_plan(doc['analysis'], script, result, doc['source']['duration'])
    result['audio_sha256'] = audio_digest(run / result['audio_file'])
    result['audio_file'] = str((run / result['audio_file']).relative_to(production.PROJECT_ROOT / p['id']))
    result['approved_hash'] = doc['approved_hash']
    doc['narration'] = result
    for key in ('output', 'archive', 'qa_report', 'publish_approval'):
        doc.pop(key, None)
    doc['message'] = '모의 음성 타이밍을 만들었습니다. WAV는 무음이며 실제 나레이션이 아닙니다.' if result['mock'] else '한국어 나레이션과 문장별 길이를 저장했습니다.'
    p['status'] = 'approved'
    production.store(p)
    return {'narration': result, 'message': doc['message']}


@production.serialized
def check_qa(project_id):
    import qa
    import editing
    p = editable(project_id)
    doc = p['document']
    existing = doc.get('qa_report') or {}
    if p['status']=='completed' and existing.get('audio_scope')=='rendered_mix':
        return {'qa_report': existing, 'message': '최종 영상의 QA 결과입니다.'}
    script = current_script(doc)
    voice = doc.get('narration')
    if not voice or voice.get('approved_hash') != doc.get('approved_hash') or doc.get('approved_hash') != production.fingerprint(doc):
        raise ValueError('현재 승인된 대본의 나레이션을 먼저 생성하세요.')
    audio = production.asset(project_id, 'narration')
    plan = editing.build_plan(doc['analysis'], script, voice, doc['source']['duration'])
    report = qa.check_report(doc['analysis'], {**script, 'approved': True}, production.narration_qa_data(doc, plan), audio)
    report['audio_scope'] = 'narration_only'
    doc['qa_report'] = report
    production.store(p)
    return {'qa_report': report, 'message': 'QA 점검을 완료했습니다.'}


def check_publish_candidate(p):
    """Return the identity of the final file that passed QA."""
    doc = p['document']
    if p['status'] != 'completed':
        raise ValueError('완성 영상을 먼저 렌더링하세요.')
    approved_script(doc)
    voice = doc.get('narration') or {}
    if voice.get('mock') is not False or voice.get('approved_hash') != doc['approved_hash']:
        raise ValueError('현재 승인 대본으로 실제 한국어 나레이션을 생성하세요.')
    report = doc.get('qa_report') or {}
    if report.get('audio_scope') != 'rendered_mix' or report.get('passed') is not True or not report.get('output_sha256'):
        raise ValueError('최종 영상의 QA를 통과해야 합니다. 필요한 경우 다시 렌더링하세요.')
    if not any(check.get('name')=='audio_clipping' and check.get('status')=='passed' and
               check.get('metrics', {}).get('peak', 0) > 0 for check in report.get('checks', [])):
        raise ValueError('최종 영상에서 유효한 오디오를 확인하지 못했습니다.')
    try:
        digest = audio_digest(production.asset(p['id'], 'output'))
    except OSError as exc:
        raise ValueError('최종 영상 파일을 읽을 수 없습니다.') from exc
    if digest != report['output_sha256']:
        raise ValueError('최종 영상이 QA 이후 변경되었습니다. 다시 렌더링하세요.')
    return digest


@production.serialized
def approve_publish(project_id, reviewed, rights_note):
    if reviewed is not True:
        raise ValueError('최종 영상과 음성을 검토했다고 확인하세요.')
    if not isinstance(rights_note, str) or not 1 <= len(rights_note.strip()) <= 500:
        raise ValueError('원본 사용 권한의 근거를 1~500자로 입력하세요.')
    p = production.project(project_id)
    digest = check_publish_candidate(p)
    approval = {'output_sha256': digest, 'rights_note': rights_note.strip(), 'approved_at': production.now()}
    p['document']['publish_approval'] = approval
    production.store(p)
    return {'publish_approval': approval, 'message': '검토한 영상의 게시 준비를 승인했습니다.'}
