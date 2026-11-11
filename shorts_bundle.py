"""Create three editable shopping shorts from one verified product and source."""

import json
import os
from itertools import product as combinations
from pathlib import Path
import re
import shutil
import tempfile
import traceback
import uuid

import caption_drafts
import analysis_script
import audio_mixer
import manual_timeline
import narration
import production
import qa


ANGLES = {
    'demo': ('사용 시연', '실제 사용 장면을 먼저 볼게요.'),
    'review': ('구매 후기', '구매자는 어떤 점을 좋게 봤을까요?'),
    'guide': ('구매 전 확인', '구매 전에 이 부분을 확인해 보세요.'),
}
TOPICS = (
    ('세척', ('세척','설거지','씻','닦','물때')),
    ('분리', ('분리','탈착','떼어','뗀다')),
    ('여닫기', ('열기','열어','열리','연다','열었다','닫','잠그','잠금','개폐')),
    ('조립', ('설치','조립','끼우','고정')),
    ('이동', ('바퀴','굴리','밀어','이동','끌')),
    ('보관', ('보관','수납','선반','쌓','정리')),
    ('휴대', ('휴대','가방','들고','접어','접히')),
)


def topics(note):
    return [label for label, words in TOPICS if any(word in note for word in words)]


def terms(note):
    ignored = {'제품','상품','정말','그냥','부분','구매','사용','후기','좋아요','있어요','합니다','되네요'}
    return {token for token in re.findall(r'[가-힣A-Za-z0-9]{2,}', note) if token not in ignored}


def candidate_for(angle, notes, review):
    review_topics = set(topics(review))
    review_terms = terms(review)
    scored = []
    for note in notes:
        scene_topics = topics(note)
        shared = [topic for topic in scene_topics if topic in review_topics]
        words = terms(note)&review_terms
        if angle == 'demo':
            if not scene_topics:
                continue
            score = len(scene_topics) + 2*('한 손' in note or '한손' in note)
            topic = scene_topics[0]
            hook = f'{topic} 장면을 먼저 볼게요.'
        else:
            if not shared and (review_topics or len(words)<2):
                continue
            score = 3*len(shared) + len(words)
            topic = shared[0] if shared else ''
            if angle == 'review':
                hook = f'{topic}, 구매 후기에선 어땠을까요?' if topic else ANGLES[angle][1]
            else:
                hook = f'구매 전에 {topic}도 확인해 보세요.' if topic else ANGLES[angle][1]
        scored.append((score,note,hook))
    return max(scored, key=lambda item:item[0], default=None)


def recommend(data):
    doc = production.project(str(data.get('source_project_id','')))['document']
    source = doc.get('source')
    if not source:
        raise ValueError('원본 영상이 있는 프로젝트를 선택하세요.')
    candidates = {key:[] for key in ANGLES}
    try:
        analysis, _ = analysis_script.require_confirmed(doc.get('analysis'))
    except ValueError:
        return {'confirmed':False,'angles':candidates}
    reviews = {'demo':'','review':caption_drafts.clean(data.get('positive_review'))[:400],
               'guide':caption_drafts.clean(data.get('concern_review'))[:400]}
    for scene in analysis['scenes']:
        start, end = round(scene['start'],3), min(round(scene['end'],3),source['duration'])
        if not 4 <= round(end-start,3) <= 60:
            continue
        notes = [caption_drafts.clean(note) for note in scene['facts']]
        notes = [note for note in notes if 1 <= len(note) <= 60]
        for angle in ANGLES:
            match = candidate_for(angle,notes,reviews[angle])
            if match:
                score, observed, hook = match
                candidates[angle].append({'scene_id':scene['id'],'start':start,'end':end,
                                          'observed':observed,'hook':hook,'score':score})
    for angle, options in candidates.items():
        by_start = {}
        for item in sorted(options,key=lambda item:(-item['score'],item['start'])):
            by_start.setdefault(item['start'],item)
        candidates[angle] = list(by_start.values())[:3]
    # Prefer recommendations that cover more angles without reusing the same opening.
    choices = [[None,*options] for options in candidates.values()]
    feasible = (items for items in combinations(*choices)
                if len({item['start'] for item in items if item})==sum(item is not None for item in items))
    best = max(feasible,key=lambda items:(sum(item is not None for item in items),
                                          sum(item['score'] for item in items if item)))
    for options, selected in zip(candidates.values(),best):
        for item in options:
            item['preferred'] = item is selected
            del item['score']
        options.sort(key=lambda item:not item['preferred'])
    return {'confirmed':True,'angles':candidates}


def text(value, label, limit):
    if not isinstance(value, str):
        raise ValueError(f'{label}을 입력하세요.')
    value = caption_drafts.clean(value)
    if not value or len(value) > limit:
        raise ValueError(f'{label}은 1~{limit}자로 입력하세요.')
    return value


def variant_lines(key, hook, observed, source_url, review, review_url):
    lines = [{'text': hook}]
    scene = {'text': observed, 'evidence': {'kind': 'scene', 'note': observed, 'url': source_url}}
    if key != 'demo':
        quoted = {'text': caption_drafts.shorten(review), 'sub': '구매 후기 발췌',
                  'evidence': {'kind': 'review', 'note': review, 'url': review_url}}
        lines.extend((quoted, scene) if key == 'review' else (scene, quoted))
    else:
        lines.append(scene)
    return lines


def narration_profile(data):
    engine = data.get('engine','onecore')
    voice, closing = data.get('voice',''), data.get('closing','')
    rate = data.get('rate',0)
    if (engine not in ('onecore','sapi') or not isinstance(voice,str) or len(voice)>100
            or not isinstance(closing,str) or len(closing)>60 or type(rate) is not int or not -3<=rate<=3):
        raise ValueError('음성 엔진·목소리·속도(-3~3)·마무리 문구를 확인하세요.')
    return {'engine':engine,'voice':voice.strip(),'rate':rate,'closing':caption_drafts.clean(closing)}


@production.serialized
def save_narration_profile(channel_id, data):
    profile=narration_profile(data)
    with production.CONNECT() as con:
        updated=con.execute('UPDATE production_channels SET narration_profile=? WHERE id=?',
                            (json.dumps(profile,ensure_ascii=False),channel_id))
        if updated.rowcount!=1:
            raise ValueError('제작 채널을 찾을 수 없습니다.')
    return profile


@production.serialized
def queue_automation(bundle_id):
    if not production.state()['render_available']:
        raise ValueError('FFmpeg/FFprobe와 한국어 글꼴 설치가 필요합니다.')
    queued=[]
    with production.CONNECT() as con:
        rows=con.execute("""SELECT p.*, c.narration_profile FROM production_projects p
            JOIN production_channels c ON c.id=p.channel_id
            WHERE json_extract(p.document,'$.shorts_bundle.id')=?""",(bundle_id,)).fetchall()
        if not rows:
            raise ValueError('쇼츠 묶음을 찾을 수 없습니다.')
        for row in rows:
            project=dict(row)
            doc=json.loads(project['document'])
            project['document']=doc
            if project['status']=='rendering' or (project['status']=='completed' and doc.get('bundle_narration')):
                continue
            doc['bundle_job_profile']=narration_profile(json.loads(project['narration_profile']))
            queued.append(production.enqueue_job(project,engine='bundle',con=con))
    if queued:
        production.RENDER_WAKE.set()
    return {'queued':len(queued)}


def prepare_automation(project):
    doc=project['document']
    try:
        profile=doc['bundle_job_profile']
        if not doc.get('source') or not doc.get('clips') or doc.get('generated_script'):
            raise ValueError('쇼츠 편집 구간과 자막을 확인하세요.')
        cuts=manual_timeline.clips(doc['clips'],doc['source']['duration'])
        duration=manual_timeline.duration(cuts)
        cues=[cue for cue in doc.get('cues',[]) if not cue.get('bundle_closing')]
        if not cues:
            raise ValueError('나레이션에 사용할 타임라인 자막을 먼저 입력하세요.')
        if profile['closing'] and cues[-1]['text']!=profile['closing']:
            cues=production.cues([*cues,{'text':profile['closing'],'start':duration,'bundle_closing':True}])
        directory=production.audio_directory(project)
        mix=audio_mixer.settings(doc.get('audio_mix') or {'source_volume':18},directory)
        if mix['narration']['muted'] or not mix['narration']['volume']:
            raise ValueError('나레이션 음소거를 해제하고 볼륨을 올린 뒤 다시 실행하세요.')
        script={'approved':True,'sentences':[
            {'id':str(index),'scene_id':'bundle','text':cue['text'].replace('*','')}
            for index,cue in enumerate(cues)
        ]}
        engine_class=narration.WindowsOneCore if profile['engine']=='onecore' else narration.WindowsSAPI
        engine=engine_class(profile['voice'],profile['rate'])
        with tempfile.TemporaryDirectory(prefix='mflow-bundle-') as scratch:
            voice=narration.synthesize(script,scratch,engine)
            if voice['duration']>duration+0.0005:
                longest=max(voice['segments'],key=lambda segment:segment['duration'])
                raise ValueError(f"나레이션 {voice['duration']:.2f}초가 컷 {duration:.2f}초를 넘습니다. "
                                 f"컷을 늘리거나 가장 긴 문장을 줄이세요. {longest['text']} ({longest['duration']:.2f}초)")
            aligned=production.cues([{**cue,'start':segment['start'],'end':min(duration,segment['end'])}
                                     for cue,segment in zip(cues,voice['segments'])])
            path=Path(scratch)/voice['audio_file']
            signal=qa.audio_stats(path)
            if not signal['peak'] or signal['clipped_samples']:
                raise ValueError('합성 음성이 무음이거나 클리핑됐습니다. 채널 음성 설정을 확인하세요.')
            with path.open('rb') as stream:
                audio=audio_mixer.upload(directory,stream,path.stat().st_size)
        mix['narration']['clip']={**audio,'in':0,'out':audio['duration'],'at':0,'name':'채널 자동 나레이션'}
        doc.update(cues=aligned,audio_mix=mix)
        doc['settings']['captions']=True
        doc['bundle_narration']={**voice,'profile':profile,'clip_id':audio['id'],'signal':signal}
        for key in ('bundle_job_profile','qa_report','publish_approval','output','archive'):
            doc.pop(key,None)
        doc['approved_hash']=production.fingerprint(doc)
        return True
    except Exception as error:
        if not isinstance(error,(OSError,ValueError)):
            traceback.print_exc()
        doc.pop('bundle_job_profile',None)
        project['status']='error'
        doc['message']=str(error) if isinstance(error,ValueError) else '나레이션 파일을 준비하지 못했습니다.'
        production.store(project)
        return False


@production.serialized
def create(data):
    parent = production.project(str(data.get('source_project_id','')))
    doc = parent['document']
    source = doc.get('source')
    if not source:
        raise ValueError('원본 영상이 있는 프로젝트를 선택하세요.')
    product = text(data.get('product'), '상품 이름', 30)
    option = text(data.get('option'), '확인한 모델·옵션', 50)
    if data.get('same_option') is not True:
        raise ValueError('영상과 구매 후기가 같은 모델·옵션인지 확인하세요.')
    product_url = caption_drafts.source_url(data.get('product_url'), required=True)
    review_url = caption_drafts.source_url(data.get('review_url') or product_url, required=True)
    source_url = doc.get('source_url','')
    reviews = {'review': text(data.get('positive_review'), '장점 후기', 400),
               'guide': text(data.get('concern_review'), '구매 전 확인할 후기', 400)}
    if reviews['review'] == reviews['guide']:
        raise ValueError('장점 후기와 구매 전 확인할 후기를 각각 입력하세요.')

    variants = data.get('variants')
    if not isinstance(variants, dict) or set(variants) != set(ANGLES):
        raise ValueError('시연·후기·구매 전 확인의 컷과 채널을 지정하세요.')
    bundle_id = uuid.uuid4().hex
    created = []
    starts, assigned = set(), set()
    with production.CONNECT() as con:
        for key, (label, _) in ANGLES.items():
            variant = variants[key]
            if not isinstance(variant, dict):
                raise ValueError('각 영상의 컷과 채널을 지정하세요.')
            channel_id = str(variant.get('channel_id',''))
            channel = con.execute('SELECT template FROM production_channels WHERE id=?', (channel_id,)).fetchone()
            if not channel or channel_id in assigned:
                raise ValueError('서로 다른 제작 채널 3개를 선택하세요.')
            assigned.add(channel_id)
            [cut] = manual_timeline.clips([variant], source['duration'])
            duration = manual_timeline.duration([cut])
            if duration < 4 or cut['start'] in starts:
                raise ValueError('각 영상은 4초 이상이며 시작 장면의 시점은 서로 달라야 합니다.')
            starts.add(cut['start'])
            observed = text(variant.get('observed'), '해당 컷에서 보이는 동작', 60)
            hook = text(variant.get('hook') or ANGLES[key][1], '첫 문장', 60)
            lines = variant_lines(key, hook, observed, source_url, reviews.get(key), review_url)
            cues = production.cues([{**line, 'start': duration*i/len(lines),
                                     'end': duration*(i+1)/len(lines)} for i,line in enumerate(lines)])
            title = f'{product} · {label}'
            cfg = production.settings({**json.loads(channel['template']),
                                       'start':0, 'duration':duration, 'captions':True})
            description = f'{product} / {option}\n{observed}\n\n상품 링크\n{product_url}'
            posting = {'id':bundle_id, 'angle':key, 'label':label, 'product':product,
                       'option':option, 'description':description, 'source_project_id':parent['id'],
                       'rights_note':(doc.get('share_import') or {}).get('rights_note','')}
            child = {'settings':cfg, 'source':dict(source), 'source_url':source_url,
                     'headline':caption_drafts.shorten(title,30), 'script':'\n'.join(line['text'] for line in lines),
                     'credit':doc.get('credit',''), 'credit_on':doc.get('credit_on',True),
                     'context':f'확인한 모델·옵션: {option}', 'approved_hash':None,
                     'clips':[cut], 'cues':cues, 'shorts_bundle':posting,
                     'message':'초안의 컷과 자막을 검토한 뒤 승인하세요.'}
            created.append((uuid.uuid4().hex,title,channel_id,child))

        original = production.asset(parent['id'],'source')
        copies = []
        try:
            for ident, title, channel_id, child in created:
                folder = production.PROJECT_ROOT/ident
                folder.mkdir()
                target = folder/source['filename']
                copies.append(target)
                try:
                    os.link(original,target)
                except OSError:
                    shutil.copyfile(original,target)
                timestamp = production.now()
                con.execute('INSERT INTO production_projects VALUES (?,?,?,?,?,?,?)',
                            (ident,title,channel_id,'script_ready',json.dumps(child,ensure_ascii=False),
                             timestamp,timestamp))
            con.commit()
        except BaseException:
            con.rollback()
            for target in copies:
                target.unlink(missing_ok=True)
                target.parent.rmdir()
            raise
    return {'id':bundle_id,'projects':[item[0] for item in created]}
