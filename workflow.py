"""Persist the reviewed analysis -> approved script -> narration workflow."""
import json
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
    for key in ('narration', 'qa_report', 'output', 'archive'):
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
    import narration
    import editing
    p = editable(project_id)
    doc = p['document']
    script = current_script(doc)
    if not doc.get('approved_hash') or doc['approved_hash'] != production.fingerprint(doc):
        raise ValueError('현재 대본을 먼저 승인하세요.')
    run = production.PROJECT_ROOT / p['id'] / ('voice-' + uuid.uuid4().hex)
    result = narration.synthesize({**script, 'approved': True}, run, engine)
    editing.build_plan(doc['analysis'], script, result, doc['source']['duration'])
    result['audio_file'] = str((run / result['audio_file']).relative_to(production.PROJECT_ROOT / p['id']))
    result['approved_hash'] = doc['approved_hash']
    doc['narration'] = result
    for key in ('output', 'archive', 'qa_report'):
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
