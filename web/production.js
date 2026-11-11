let productionState=null,activeProject=null,productionDirty=false,uploading=false;
const stageNames={draft:'원본 대기',source_ready:'대본 대기',analysis_ready:'분석 검토',analysis_confirmed:'분석 확정',script_ready:'승인 대기',approved:'렌더링 준비',rendering:'제작 작업 중',completed:'완료',error:'오류'};
const stageProgress={draft:5,source_ready:25,analysis_ready:35,analysis_confirmed:45,script_ready:50,approved:65,rendering:80,completed:100,error:0};
function productionAsset(id,kind,download=false){return `/api/production/asset?id=${encodeURIComponent(id)}&kind=${kind}${download?'&download=1':''}`;}
function channelName(id){return productionState.channels.find(c=>c.id===id)?.name||'채널';}
async function refreshProduction(){productionState=await request('/api/production/state');const filter=$('#project-channel-filter');const previous=filter.value;filter.innerHTML='<option value="">모든 채널</option>'+productionState.channels.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');filter.value=previous;renderProjects();renderBundleOverview();$('#channel-grid').innerHTML=productionState.channels.map(c=>`<article class="project-card"><span class="tag">${esc(c.language)}</span><h3>${esc(c.name)}</h3><p class="muted">채널 템플릿 · ${esc(productionState.styles[c.template.style]?.name||'기본')}</p><p class="hint">길이 ${c.template.duration}초 · 원음 ${c.template.mute?'끄기':'유지'}</p><button type="button" data-channel-narration="${esc(c.id)}">나레이션 설정</button></article>`).join('')||'<div class="empty"><h3>제작 채널을 등록하세요</h3><p>프로젝트에서 스타일을 설정하고 채널별 템플릿으로 저장할 수 있습니다.</p></div>';}
window.refreshProduction=refreshProduction;
const bundleAngles={demo:'사용 시연',review:'구매 후기',guide:'구매 전 확인'};
const bundleForm=$('#shorts-bundle-form');
const bundlePreview=$('#bundle-source-preview');
const bundlePicker=$('#shorts-bundle-picker');
const bundleCards=$('#shorts-bundle-cards');
let bundleCardMarkup='';
let bundleQueueBusy=false;
let bundleAutoBusy=false;
let bundleRecommendations={};
let bundleRecommendRevision=0;
const bundleRecommend=$('#bundle-recommend');
const bundleRecommendStatus=$('#bundle-recommend-status');
const bundleApplyRecommendations=$('#bundle-apply-recommendations');
const bundlePublishForm=$('#bundle-publish-form');
let publishingBundleId='';
const bundlePerformanceForm=$('#bundle-performance-form');
const performancePlatforms={youtube:'YouTube',instagram:'Instagram',tiktok:'TikTok'};
const performanceNumber=new Intl.NumberFormat('ko-KR',{maximumFractionDigits:2});

function performanceMarkup(project){
    const result=productionState.shorts_performance?.records[project.id];
    if(!result&&project.status!=='completed')return '';
    const metrics=result?`<p class="hint">첫 7일 · 조회 ${performanceNumber.format(result.views)}회
        ${result.clicks===null?'':` · 클릭 ${performanceNumber.format(result.clicks)}회`}</p>
        <p class="hint">${result.view_ratio===null?(result.baseline_views===0?'이전 기록 중앙값이 0회라 배수를 계산할 수 없습니다.':
            `채널 기준 비교 대기 · 이전 기록 ${result.baseline_count}편`):
            `채널 평소 대비 ${performanceNumber.format(result.view_ratio)}배 · 이전 ${result.baseline_count}편의 중앙값 기준`}</p>
        <a href="${esc(result.url)}" target="_blank" rel="noopener noreferrer">${performancePlatforms[result.platform]} 게시물</a>`:'';
    return `${metrics}<button type="button" data-record-performance="${esc(project.id)}">${result?'성과 수정':'첫 7일 성과 기록'}</button>`;
}

function renderPerformanceSummary(){
    const summary=$('#bundle-performance-summary');
    const stats=productionState.shorts_performance;
    summary.hidden=!Object.keys(stats?.records||{}).length;
    if(summary.hidden)return;
    summary.innerHTML=`<b>제작 각도별 첫 7일 조회 성과</b>
        <p class="hint">같은 채널·플랫폼의 이전 영상 조회수 중앙값을 기준으로 비교합니다.</p>
        <ul>${stats.angles.map(item=>`<li>${esc(item.label)} · ${item.median_ratio===null?'비교 표본 부족':
            `${performanceNumber.format(item.median_ratio)}배`} (${item.count}편)</li>`).join('')}</ul>
        <p>${stats.suggested?`다음에 먼저 검토할 각도 · <strong>${esc(stats.suggested.label)}</strong>`:
            '추천 각도를 고르려면 비교 가능한 기록이 더 필요합니다.'}</p>`;
}

function openPerformanceForm(id){
    const project=productionState.projects.find(p=>p.id===id);
    if(!project)return;
    const result=productionState.shorts_performance?.records[id];
    const publishing=productionState.publishing;
    const videoId=publishing?.mode==='youtube'?publishing.jobs[id]?.video_id:'';
    const form=bundlePerformanceForm;
    form.reset();
    form.elements.id.value=id;
    form.elements.platform.value=result?.platform||'youtube';
    form.elements.url.value=result?.url||(/^[A-Za-z0-9_-]{11}$/.test(videoId||'')?`https://youtu.be/${videoId}`:'');
    form.elements.posted_on.value=result?.posted_on||'';
    form.elements.views.value=result?.views??'';
    form.elements.clicks.value=result?.clicks??'';
    $('#bundle-performance-title').textContent=`${project.title} · ${channelName(project.channel_id)}`;
    $('#bundle-performance-status').textContent='';
    $('#bundle-performance-dialog').showModal();
}

bundlePerformanceForm.onsubmit=async event=>{
    event.preventDefault();
    const form=event.currentTarget,button=form.querySelector('[type=submit]');
    button.disabled=true;
    try{
        const data=Object.fromEntries(new FormData(form));
        data.views=form.elements.views.valueAsNumber;
        data.clicks=data.clicks===''?null:form.elements.clicks.valueAsNumber;
        await request('/api/production/shorts-performance',data);
        await refreshProduction();
        $('#bundle-performance-dialog').close();
        message('게시 성과를 저장했습니다.');
    }catch(error){$('#bundle-performance-status').textContent=error.message;}
    finally{button.disabled=false;}
};

function canUploadBundleProject(project){
    const publishing=productionState.publishing;
    const job=publishing?.jobs[project.id];
    return project.status==='completed'&&(!job||['failed','cancelled'].includes(job.status))&&
        (publishing?.mode!=='youtube'||Boolean(publishing.targets[project.channel_id]));
}

function bundleUploadStatus(project){
    const publishing=productionState.publishing;
    const job=publishing?.jobs[project.id];
    if(!job)return '';
    const time=job.status==='queued'?new Date(job.due*1000).toLocaleString('ko-KR'):'';
    const link=publishing.mode==='youtube'&&/^[A-Za-z0-9_-]{11}$/.test(job.video_id||'')?
        `<a href="https://youtu.be/${esc(job.video_id)}" target="_blank" rel="noopener noreferrer">YouTube에서 보기</a>`:'';
    return `<p class="hint" role="status">${esc(job.message)} ${esc(time)} ${link}</p>
        ${job.status==='queued'?`<button type="button" data-cancel-upload="${esc(project.id)}">업로드 예약 취소</button>`:''}`;
}

function renderBundleOverview(){
    const groups=new Map();
    for(const project of productionState.projects){
        const bundle=project.document.shorts_bundle;
        if(!bundle)continue;
        if(!groups.has(bundle.id))groups.set(bundle.id,[]);
        groups.get(bundle.id).push(project);
    }
    $('#shorts-bundle-overview').hidden=!groups.size;
    if(!groups.size)return;
    renderPerformanceSummary();
    const selected=bundlePicker.value;
    bundlePicker.innerHTML=[...groups].map(([id,items])=>`<option value="${esc(id)}">${esc(items[0].document.shorts_bundle.product)} · ${esc(items[0].document.shorts_bundle.option)}</option>`).join('');
    if(groups.has(selected))bundlePicker.value=selected;
    const projects=groups.get(bundlePicker.value);
    const html=Object.keys(bundleAngles).flatMap(angle=>projects.filter(p=>p.document.shorts_bundle.angle===angle)).map(project=>{
        const doc=project.document,bundle=doc.shorts_bundle;
        const finished=project.status==='completed';
        const cuts=(doc.clips||[]).map(clip=>`${clip.start}–${clip.end}초`).join(', ');
        return `<article class="bundle-card">
            <p class="muted">${esc(bundle.label)} · ${esc(channelName(project.channel_id))}</p>
            <h4>${esc(project.title)}</h4>
            <p class="hint">${esc(stageNames[project.status])}${cuts?` · 원본 ${esc(cuts)}`:''}</p>
            ${project.status==='error'||project.status==='rendering'?`<p class="hint">${esc(doc.message)}</p>`:''}
            ${doc.bundle_narration&&finished?`<p class="hint">채널 나레이션 · ${doc.bundle_narration.duration.toFixed(2)}초</p>`:''}
            ${bundleUploadStatus(project)}
            ${performanceMarkup(project)}
            <ol>${(doc.cues||[]).map(cue=>`<li>${esc(cue.text)}</li>`).join('')}</ol>
            ${finished?`<video controls playsinline preload="none" src="${productionAsset(project.id,'output')}" aria-label="${esc(project.title)}"></video>
            <div class="cue-actions"><a href="${productionAsset(project.id,'output',true)}" class="outline">MP4</a>
            <a href="${productionAsset(project.id,'archive',true)}" class="outline">게시 정보 ZIP</a></div>`:''}
            <details><summary>게시 제목·설명</summary><pre>${esc(project.title)}\n\n${esc(bundle.description)}</pre>
            <button type="button" data-copy-bundle="${esc(project.id)}">제목·설명 복사</button></details>
            <button type="button" data-edit-bundle="${esc(project.id)}">편집 열기</button>
        </article>`;
    }).join('');
    if(html!==bundleCardMarkup){bundleCards.innerHTML=html;bundleCardMarkup=html;}
    const approved=projects.filter(project=>project.status==='approved');
    const render=$('#bundle-render-approved');
    render.disabled=bundleQueueBusy||!productionState.render_available||!approved.length;
    render.textContent=approved.length?`승인된 ${approved.length}편 렌더링`:'승인된 편 렌더링';
    $('#bundle-automate').disabled=bundleAutoBusy||!productionState.render_available||
        !projects.some(p=>p.status!=='rendering'&&(p.status!=='completed'||!p.document.bundle_narration));
    const publish=$('#bundle-publish');
    publish.disabled=!projects.some(canUploadBundleProject);
    publish.textContent=productionState.publishing?.mode==='youtube'?'YouTube 업로드':'YouTube 모의 업로드';
}

$('#bundle-publish').onclick=()=>{
    publishingBundleId=bundlePicker.value;
    const publishing=productionState.publishing;
    const projects=productionState.projects.filter(p=>p.document.shorts_bundle?.id===publishingBundleId&&canUploadBundleProject(p));
    if(!projects.length)return;
    bundlePublishForm.reset();
    $('#bundle-publish-mode').textContent=publishing.mode==='mock'?
        '모의 모드입니다. 예약과 처리 상태를 이 앱에서 확인할 수 있습니다.':
        '선택한 공개 범위로 각 대상 채널에 업로드합니다. 예약은 이 앱이 실행 중일 때 처리됩니다.';
    $('#bundle-publish-projects').innerHTML=projects.map(p=>`<label class="check">
        <input type="checkbox" name="project" value="${esc(p.id)}" checked>
        ${esc(p.title)} · ${esc(channelName(p.channel_id))}${publishing.targets[p.channel_id]?
            ` (${esc(publishing.targets[p.channel_id])})`:''}</label>`).join('');
    $('#bundle-publish-status').textContent='';
    $('#bundle-publish-dialog').showModal();
};

bundlePublishForm.onsubmit=async event=>{
    event.preventDefault();
    const form=event.currentTarget,button=form.querySelector('[type=submit]');
    const fields=new FormData(form);
    const scheduled=fields.get('scheduled_at');
    const ids=fields.getAll('project');
    if(!ids.length){$('#bundle-publish-status').textContent='업로드할 편을 선택하세요.';return;}
    button.disabled=true;
    try{
        const result=await request('/api/production/publish',{
            bundle_id:publishingBundleId,projects:ids,privacy:fields.get('privacy'),
            made_for_kids:fields.get('kids')==='yes',confirmed:fields.has('confirmed'),
            scheduled_at:scheduled?new Date(scheduled).toISOString():null
        });
        $('#bundle-publish-dialog').close();
        await refreshProduction();
        message(`${result.queued}편을 업로드 대기열에 추가했습니다.`);
    }catch(error){$('#bundle-publish-status').textContent=error.message;}
    finally{button.disabled=false;}
};

async function startBundleAutomation(id){
    bundleAutoBusy=true;
    renderBundleOverview();
    try{
        const result=await request('/api/production/shorts-bundle/automate',{id});
        await refreshProduction();
        message(result.queued?`${result.queued}편의 자동 제작을 시작했습니다.`:'대기열에 추가할 영상이 없습니다.');
    }catch(error){message(error.message);}
    finally{bundleAutoBusy=false;renderBundleOverview();}
}
$('#bundle-automate').onclick=()=>startBundleAutomation(bundlePicker.value);

bundlePicker.onchange=renderBundleOverview;
bundleCards.onclick=async event=>{
    const performance=event.target.closest('[data-record-performance]');
    if(performance){openPerformanceForm(performance.dataset.recordPerformance);return;}
    const cancel=event.target.closest('[data-cancel-upload]');
    if(cancel){
        try{await request('/api/production/publish/cancel',{id:cancel.dataset.cancelUpload});await refreshProduction();}
        catch(error){message(error.message);}
        return;
    }
    const edit=event.target.closest('[data-edit-bundle]');
    if(edit){openProduction(edit.dataset.editBundle);return;}
    const copy=event.target.closest('[data-copy-bundle]');
    if(!copy)return;
    const project=productionState.projects.find(p=>p.id===copy.dataset.copyBundle);
    if(!project)return;
    try{
        await navigator.clipboard.writeText(`${project.title}\n\n${project.document.shorts_bundle.description}`);
        message('게시 제목과 설명을 복사했습니다.');
    }catch{message('복사할 수 없습니다. 펼쳐진 게시 정보를 직접 복사하세요.');}
};

$('#bundle-render-approved').onclick=async()=>{
    const ids=productionState.projects.filter(p=>p.document.shorts_bundle?.id===bundlePicker.value&&p.status==='approved').map(p=>p.id);
    if(!ids.length||bundleQueueBusy)return;
    bundleQueueBusy=true;
    renderBundleOverview();
    try{
        const results=await Promise.allSettled(ids.map(id=>request('/api/production/render',{id})));
        const queued=results.filter(result=>result.status==='fulfilled').length;
        const failures=results.filter(result=>result.status==='rejected').map(result=>result.reason.message);
        await refreshProduction();
        message(`렌더 대기열에 ${queued}편을 추가했습니다.${failures.length?' '+failures.join(' '):''}`);
    }catch(error){message(error.message);}
    finally{bundleQueueBusy=false;renderBundleOverview();}
};

function clearBundleRecommendations(){
    bundleRecommendRevision++;
    bundleRecommendations={};
    bundleRecommend.disabled=false;
    bundleApplyRecommendations.hidden=true;
    bundleRecommendStatus.textContent='';
    $('#bundle-variants').querySelectorAll('.bundle-recommendations').forEach(panel=>panel.replaceChildren());
}

function loadBundleSource(){
    clearBundleRecommendations();
    const project=productionState.projects.find(p=>p.id===bundleForm.elements.source_project_id.value);
    if(!project)return;
    const duration=project.document.source.duration;
    bundlePreview.src=productionAsset(project.id,'source');
    [...$('#bundle-variants').querySelectorAll('fieldset')].forEach((variant,index)=>{
        const start=Math.min(duration*index/3,Math.max(0,duration-4)*index/2);
        for(const [field,value] of [['start',start],['end',Math.min(duration,start+Math.max(4,Math.min(15,duration/3)))]]){
            const input=variant.querySelector(`[data-part="${field}"]`);
            input.max=duration;
            input.value=Math.floor(value*1000)/1000;
        }
        variant.querySelector('[data-observed]').value='';
        variant.querySelector('[data-hook]').value='';
    });
}

bundleRecommend.onclick=async()=>{
    clearBundleRecommendations();
    const revision=bundleRecommendRevision;
    const fields=bundleForm.elements;
    bundleRecommend.disabled=true;
    bundleRecommendStatus.textContent='확정된 장면과 후기를 대조하고 있어요…';
    try{
        const result=await request('/api/production/shorts-bundle/recommend',{
            source_project_id:fields.source_project_id.value,positive_review:fields.positive_review.value,
            concern_review:fields.concern_review.value
        });
        if(revision!==bundleRecommendRevision||!$('#shorts-bundle-dialog').open)return;
        bundleRecommendations=result.angles;
        let preferred=0;
        for(const [angle,options] of Object.entries(bundleRecommendations)){
            const panel=$(`#bundle-variants [data-angle="${angle}"] .bundle-recommendations`);
            if(!options.length){
                panel.textContent='쓸 컷이 필요합니다.';
                continue;
            }
            preferred+=options.some(option=>option.preferred)?1:0;
            panel.innerHTML=options.map((option,index)=>`<article class="bundle-candidate">
                <b>${option.preferred?'추천 · ':''}${option.start}–${option.end}초</b>
                <p>${esc(option.hook)}</p><p class="hint">${esc(option.observed)}</p>
                <div class="cue-actions"><button type="button" data-candidate-preview="${index}">프레임 보기</button>
                <button type="button" data-candidate-use="${index}">이 컷 쓰기</button></div>
            </article>`).join('');
        }
        bundleApplyRecommendations.hidden=!preferred;
        bundleRecommendStatus.textContent=!result.confirmed?
            '원본 프로젝트에서 장면별로 보이는 동작을 적고 분석을 확정하세요. 직접 컷을 지정해도 됩니다.':
            preferred===3?'추천한 첫 컷과 훅을 검토하고 적용하세요.':
                `서로 다른 첫 컷 ${preferred}편을 찾았습니다. 나머지 컷은 직접 확인하세요.`;
    }catch(error){
        if(revision===bundleRecommendRevision)bundleRecommendStatus.textContent=error.message;
    }finally{
        if(revision===bundleRecommendRevision)bundleRecommend.disabled=false;
    }
};

function useBundleCandidate(row,option){
    row.querySelector('[data-part=start]').value=option.start;
    row.querySelector('[data-part=end]').value=option.end;
    row.querySelector('[data-observed]').value=option.observed;
    row.querySelector('[data-hook]').value=option.hook;
}

bundleApplyRecommendations.onclick=()=>{
    for(const [angle,options] of Object.entries(bundleRecommendations)){
        const option=options.find(candidate=>candidate.preferred);
        if(option)useBundleCandidate($(`#bundle-variants [data-angle="${angle}"]`),option);
    }
    bundleRecommendStatus.textContent='추천한 컷과 첫 문장을 채웠습니다. 원본에서 확인하고 필요하면 고치세요.';
};

$('#new-shorts-bundle').onclick=async()=>{
    try{
        await refreshProduction();
        if(productionState.channels.length<3){message('채널 설정에서 배정할 제작 채널 3개를 등록하세요.');return;}
        const sources=productionState.projects.filter(p=>p.document.source?.duration>4.002&&!p.document.shorts_bundle);
        if(!sources.length){message('4초를 넘는 원본 영상이 있는 프로젝트를 먼저 준비하세요.');return;}
        bundleForm.reset();
        bundleForm.elements.source_project_id.innerHTML=sources.map(p=>`<option value="${esc(p.id)}">${esc(p.title)}</option>`).join('');
        const channels=productionState.channels.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
        $('#bundle-variants').innerHTML=Object.entries(bundleAngles).map(([key,label])=>`<fieldset class="bundle-card" data-angle="${key}">
            <legend>${label}</legend><label>제작 채널<select data-channel required>${channels}</select></label>
            <div class="bundle-timing">
            <label>원본 시작 (초)<input type="number" data-part="start" min="0" step="0.001" required></label>
            <label>원본 끝 (초)<input type="number" data-part="end" min="0" step="0.001" required></label>
            <button type="button" data-bundle-time="start">현재 시점을 시작으로</button>
            <button type="button" data-bundle-time="end">현재 시점을 끝으로</button>
            </div>
            <label>컷에 보이는 동작<input data-observed maxlength="60" required></label>
            <label>첫 문장<input data-hook maxlength="60" placeholder="비우면 기본 첫 문장 사용"></label>
            <div class="bundle-recommendations" aria-live="polite"></div>
        </fieldset>`).join('');
        $('#bundle-variants').querySelectorAll('[data-channel]').forEach((select,index)=>select.selectedIndex=index);
        $('#bundle-form-status').textContent='';
        const suggestion=productionState.shorts_performance?.suggested;
        const suggestionLabel=$('#bundle-performance-suggestion');
        suggestionLabel.hidden=!suggestion;
        suggestionLabel.textContent=suggestion?`이전 성과에서 먼저 검토할 각도 · ${suggestion.label}`:'';
        loadBundleSource();
        $('#shorts-bundle-dialog').showModal();
    }catch(error){message(error.message);}
};

bundleForm.elements.source_project_id.onchange=loadBundleSource;
for(const name of ['positive_review','concern_review']){
    bundleForm.elements[name].addEventListener('input',clearBundleRecommendations);
}
$('#shorts-bundle-dialog').addEventListener('close',()=>{
    clearBundleRecommendations();
    bundlePreview.removeAttribute('src');
    bundlePreview.load();
});
$('#bundle-variants').onclick=event=>{
    const candidate=event.target.closest('[data-candidate-preview],[data-candidate-use]');
    if(candidate){
        const row=candidate.closest('fieldset');
        const option=bundleRecommendations[row.dataset.angle]?.[candidate.dataset.candidatePreview??candidate.dataset.candidateUse];
        if(!option)return;
        if(candidate.hasAttribute('data-candidate-use')){
            useBundleCandidate(row,option);
            bundleRecommendStatus.textContent=`${bundleAngles[row.dataset.angle]} 편에 추천 컷을 적용했습니다.`;
        }else{
            bundlePreview.pause();
            bundlePreview.currentTime=option.start;
            bundlePreview.scrollIntoView({block:'nearest'});
        }
        return;
    }
    const button=event.target.closest('[data-bundle-time]');
    if(!button||!bundlePreview.readyState)return;
    button.closest('fieldset').querySelector(`[data-part="${button.dataset.bundleTime}"]`).value=
        Math.floor(bundlePreview.currentTime*1000)/1000;
};
bundleForm.onsubmit=async event=>{
    event.preventDefault();
    const submit=bundleForm.querySelector('[type=submit]');
    if(submit.disabled)return;
    submit.disabled=true;
    const data=Object.fromEntries(new FormData(bundleForm));
    const automate=bundleForm.elements.auto_render.checked;
    data.same_option=bundleForm.elements.same_option.checked;
    data.variants=Object.fromEntries([...$('#bundle-variants').querySelectorAll('fieldset')].map(row=>[
        row.dataset.angle,{channel_id:row.querySelector('[data-channel]').value,
            start:row.querySelector('[data-part=start]').valueAsNumber,
            end:row.querySelector('[data-part=end]').valueAsNumber,
            observed:row.querySelector('[data-observed]').value,hook:row.querySelector('[data-hook]').value}
    ]));
    try{
        const result=await request('/api/production/shorts-bundle',data);
        $('#shorts-bundle-dialog').close();
        await refreshProduction();
        bundlePicker.value=result.id;
        renderBundleOverview();
        $('#shorts-bundle-overview').scrollIntoView({block:'nearest'});
        if(automate)await startBundleAutomation(result.id);
        else message('쇼츠 초안 3편을 만들었습니다.');
    }catch(error){$('#bundle-form-status').textContent=error.message;}
    finally{submit.disabled=false;}
};

const channelNarrationForm=$('#channel-narration-form');
$('#channel-grid').addEventListener('click',event=>{
    const button=event.target.closest('[data-channel-narration]');
    if(!button)return;
    const channel=productionState.channels.find(c=>c.id===button.dataset.channelNarration);
    if(!channel)return;
    const profile=channel.narration_profile;
    Object.assign(channelNarrationForm.elements.id,{value:channel.id});
    for(const [key,value] of Object.entries({engine:'onecore',voice:'',rate:0,closing:'',...profile})){
        channelNarrationForm.elements[key].value=value;
    }
    $('#channel-narration-name').textContent=channel.name;
    $('#channel-narration-status').textContent='';
    $('#channel-narration-dialog').showModal();
});
channelNarrationForm.onsubmit=async event=>{
    event.preventDefault();
    const form=event.currentTarget,button=form.querySelector('[type=submit]');
    button.disabled=true;
    try{
        const payload=Object.fromEntries(new FormData(form));
        payload.rate=form.elements.rate.valueAsNumber;
        await request('/api/production/channel-narration',payload);
        await refreshProduction();
        $('#channel-narration-dialog').close();
        message('채널 나레이션 설정을 저장했습니다.');
    }catch(error){$('#channel-narration-status').textContent=error.message;}
    finally{button.disabled=false;}
};
function renderProjects(){if(!productionState)return;const q=$('#project-query').value.toLocaleLowerCase();const channel=$('#project-channel-filter').value;const status=$('#project-status-filter').value;const items=productionState.projects.filter(p=>(!q||p.title.toLocaleLowerCase().includes(q))&&(!channel||p.channel_id===channel)&&(!status||p.status===status));$('#project-grid').innerHTML=items.map(p=>`<article class="project-card"><div class="project-card-heading"><span class="tag">${esc(channelName(p.channel_id))}</span><span class="status ${p.status==='completed'?'verified':''}">${stageNames[p.status]}</span></div><h3>${esc(p.title)}</h3>${p.comment_info?.available?'<span class="comment-ready">💬 댓글 작업 가능</span>':''}<time>${esc(new Date(p.updated_at).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul'}))}</time><p class="muted">${esc(productionState.styles[p.document.settings.style]?.name)} · ${p.document.settings.duration}초</p><div class="project-progress-label"><span>제작 단계</span><b>${stageProgress[p.status]}%</b></div><div class="project-progress"><span style="width:${stageProgress[p.status]}%"></span></div><div class="project-review">대본 검토 <b>${p.document.approved_hash?'승인됨':'대기'}</b></div><button class="primary" data-open-project="${esc(p.id)}">${p.status==='completed'?'결과 확인':'작업 계속'} →</button></article>`).join('')||'<div class="empty"><h3>첫 제작 프로젝트를 시작하세요</h3><p>채널 등록 → 원본 업로드 → 대본 승인 → MP4 제작</p></div>';}
// Mirrors production.py: title_lines / caption_chunks, so the preview wraps text like the render.
const TITLE_FITS=[[9,108,2],[12,81,2],[18,52,3]],CAPTION_SIZE=75,CAPTION_LINE=12;
function wrapWords(text,width){const lines=[];let line='';for(let word of text.split(/\s+/).filter(Boolean)){while(word.length>width){if(line){lines.push(line);line='';}lines.push(word.slice(0,width));word=word.slice(width);}if(!word)continue;if(line&&line.length+1+word.length>width){lines.push(line);line=word;}else line=line?line+' '+word:word;}if(line)lines.push(line);return lines;}
function titleLines(text){for(const [width,size,limit] of TITLE_FITS){const lines=wrapWords(text,width);if(lines.length<=limit)return [lines,size];}return [wrapWords(text,18),52];}
function captionChunks(script){
    const words=script.split(/\*([^*]+)\*/).flatMap((part,i)=>part.split(/\s+/).flatMap(word=>{
        const letters=Array.from(word), chunks=[];
        for(let start=0;start<letters.length;start+=CAPTION_LINE){
            const text=letters.slice(start,start+CAPTION_LINE).join('').replaceAll('*','');
            if(text)chunks.push([text,i%2===1]);
        }
        return chunks;
    }));
    const chunks=[];
    let lines=[],line=[];
    for(const word of words){
        if(line.length&&line.reduce((n,[w])=>n+Array.from(w).length+1,0)+Array.from(word[0]).length>CAPTION_LINE){
            lines.push(line);
            line=[];
            if(lines.length===2){chunks.push(lines);lines=[];}
        }
        line.push(word);
        if(/[.?!。]$/.test(word[0])){chunks.push([...lines,line]);lines=[];line=[];}
    }
    if(line.length)lines.push(line);
    if(lines.length)chunks.push(lines);
    return chunks;
}
function previewCaptionLines(){
    const duration=mixerDuration();
    if(!duration)return captionChunks($('#production-form').elements.script.value||'직접 작성한 *대본 자막*')[0]||[];
    const time=outputPreviewTime()+0.000001;
    return automaticCaptionSpans(duration,mixerVoice()).find(span=>span.start<=time&&time<span.end)?.lines||[];
}
const loadedFonts=new Set();
function previewFont(file){if(!file)return '';const family='mflow-'+file.replace(/[^A-Za-z0-9]/g,'_')+'-'+file.length;if(!loadedFonts.has(file)){loadedFonts.add(file);new FontFace(family,`url(/api/production/font?file=${encodeURIComponent(file)})`).load().then(face=>{document.fonts.add(face);}).catch(()=>loadedFonts.delete(file));}return `"${family}",`;}
function fontOptions(selected){const fonts=productionState.fonts||[];const missing=selected&&!fonts.some(f=>f.file===selected)?`<option value="${esc(selected)}" selected>${esc(selected)} (파일 없음)</option>`:'';return `<option value="">기본 글씨체</option>`+fonts.map(f=>`<option value="${esc(f.file)}" ${f.file===selected?'selected':''}>${esc(f.name)}</option>`).join('')+missing;}
function formComments(form){const result=[];for(let i=0;i<3;i++){const text=form.elements['comment_text_'+i].value.trim();if(text)result.push({name:form.elements['comment_name_'+i].value.trim(),when:form.elements['comment_when_'+i].value.trim(),text,at:Number(form.elements['comment_at_'+i].value)||0});}return result.sort((a,b)=>a.at-b.at);}
const OFFSETS=['title_y','credit_y','caption_y','comment_y'];
// Same nudge as the render: design pixels x1.5 on the 1080 canvas, scaled to the preview width.
function applyOffsets(form,ratio){const targets={title_y:'#preview-headline',credit_y:'#preview-credit',caption_y:'#preview-caption',comment_y:'#preview-comment'};for(const k of OFFSETS){const value=Number(form.elements[k].value)||0;const shift=`0 ${value*1.5*ratio}px`;if(k==='title_y'){const head=$('#preview-headline');head.style.overflow=value?'visible':'';head.querySelectorAll('span').forEach(span=>span.style.translate=shift);}else $(targets[k]).style.translate=shift;form.querySelector(`[data-offset-value="${k}"]`).textContent=value>0?'+'+value:value;}}
function stylePreview(){const form=$('#production-form');const style=productionState.styles[form.elements.style?.value||'basic'];const phone=$('.phone-preview');const ratio=(phone.clientWidth||264)/1080;const [lines,size]=titleLines(form.elements.headline.value||'상단 문구 첫 줄 둘째 줄');const head=$('#preview-headline');head.style.fontFamily=previewFont(form.elements.title_font.value)+'sans-serif';head.style.fontWeight=form.elements.title_font.value?'normal':'';head.style.fontSize=size*ratio+'px';head.innerHTML=lines.map((line,i)=>`<span style="color:${i?style.title2:style.title}">${esc(line)}</span>`).join('');const caption=$('#preview-caption');caption.style.fontFamily=previewFont(form.elements.caption_font.value)+'sans-serif';caption.style.fontWeight=form.elements.caption_font.value?'normal':'';caption.style.fontSize=CAPTION_SIZE*ratio+'px';caption.innerHTML=form.elements.captions.checked?previewCaptionLines().map(line=>line.map(([w,marked])=>marked?`<span style="color:${style.highlight}">${esc(w)}</span>`:esc(w)).join(' ')).join('<br>'):'';caption.style.color=style.caption;$('#production-source-preview').style.transform=form.elements.mirror.checked?'scaleX(-1)':'none';const layout=form.elements.layout.value||'band';phone.dataset.layout=layout;const credit=form.elements.credit_on.checked?form.elements.credit.value.trim():'';head.style.paddingBottom=(credit?69:39)*ratio+'px';$('#preview-credit').textContent=credit;$('#preview-credit').style.fontSize=33*ratio+'px';const card=formComments(form)[0];const box=$('#preview-comment');if(card){let name=card.name;if(name&&!name.startsWith('@'))name='@'+name;const header=[name,card.when].filter(Boolean).join(' · ');box.innerHTML=(header?`<small>${esc(header)}</small>`:'')+`<b>${esc(card.text)}</b>`;box.style.fontSize=44*ratio+'px';box.querySelector('small')&&(box.querySelector('small').style.fontSize=33*ratio+'px');}else box.innerHTML='';$('#production-source-preview').muted=form.elements.mute.checked;previewCue(form,ratio,style);applyOffsets(form,ratio);updateOverlayPreview();renderOutputTimeline();if(typeof previewClipFraming==='function')previewClipFraming();}
function fillProduction(id,keepHistory=false){const p=productionState.projects.find(p=>p.id===id);if(!p)return;const sameProject=activeProject===id&&overlayDocument?.source?.filename===p.document.source?.filename;activeProject=id;productionDirty=false;const doc=p.document,form=$('#production-form');form.reset();for(const name of ['title','headline','script','context','source_url','credit'])form.elements[name].value=name==='title'?p.title:doc[name]||'';for(let i=0;i<3;i++){const c=(doc.comments||[])[i]||{};form.elements['comment_name_'+i].value=c.name||'';form.elements['comment_when_'+i].value=c.when||'';form.elements['comment_at_'+i].value=c.text?c.at:'';form.elements['comment_text_'+i].value=c.text||'';}for(const name of ['duration','start'])form.elements[name].value=doc.settings[name];for(const name of ['captions','mute','mirror'])form.elements[name].checked=doc.settings[name];form.elements.credit_on.checked=doc.credit_on!==false;blurBoxes=(doc.blurs||[]).map(b=>({...b}));renderBlurs();$('#preview-frame').hidden=true;loadOverlays(doc);loadClipTimeline(doc);loadAudioMixer(doc);for(const k of OFFSETS)form.elements[k].value=doc.settings[k]||0;form.elements.layout.value=doc.settings.layout||'fill';form.elements.title_font.innerHTML=fontOptions(doc.settings.title_font||'');form.elements.caption_font.innerHTML=fontOptions(doc.settings.caption_font||'');$('#production-styles').innerHTML=Object.entries(productionState.styles).map(([key,value])=>`<label class="style-choice"><input name="style" type="radio" value="${key}" ${doc.settings.style===key?'checked':''}><div class="mini-phone"><b><i style="color:${value.title}">상단 문구</i><i style="color:${value.title2}">둘째 줄</i></b><span>대본 <em style="color:${value.highlight}">강조</em> 자막</span></div><strong>${esc(value.name)}</strong></label>`).join('');$('#production-title').textContent=p.title;$('#production-message').textContent=doc.message;$('#upload-status').textContent=doc.source?`원본 ${doc.source.width}×${doc.source.height} · ${doc.source.duration}초`:'원본 영상이 필요합니다.';const source=$('#production-source-preview');const url=doc.source?productionAsset(id,'source'):'';if(source.getAttribute('src')!==url){if(url)source.src=url;else source.removeAttribute('src');source.load();}$('#production-fields').disabled=p.status==='rendering';$('#production-save').disabled=p.status==='rendering';$('#production-approve').disabled=p.status==='rendering'||!doc.source||(!doc.script&&!doc.clips?.length&&!doc.audio_mix);$('#production-render').disabled=p.status==='rendering'||!doc.approved_hash||!productionState.render_available;$('#finished-preview').hidden=p.status!=='completed';$('#production-downloads').hidden=p.status!=='completed';$('#production-downloads').innerHTML=p.status==='completed'?`<div class="export-panel"><h3>내보내기</h3><a class="primary outline" href="${productionAsset(id,'output',true)}">MP4 내려받기</a><a class="outline" href="${productionAsset(id,'archive',true)}">자막·프로젝트 ZIP</a><p class="hint">ZIP: MP4 + SRT + JSON 설정. CapCut 전용 프로젝트가 아닙니다.</p></div>`:'';if(p.status==='completed')$('#production-output-preview').src=productionAsset(id,'output');else{$('#production-output-preview').removeAttribute('src');$('#production-output-preview').load();}renderCommentStatus(p);renderShareImport(p);renderAnalysisWorkflow(p);loadCues(doc);stylePreview();rebaseEditHistory(sameProject,keepHistory);resetRecovery();}
function openProduction(id){editHistory.current=null;fillProduction(id);$('#production-dialog').showModal();restoreRecovery();}
function productionPayload(){validateClipTimeline();const form=$('#production-form');return {id:activeProject,title:form.elements.title.value,headline:form.elements.headline.value,script:form.elements.script.value,context:form.elements.context.value,source_url:form.elements.source_url.value,credit:form.elements.credit.value,credit_on:form.elements.credit_on.checked,comments:formComments(form),blurs:blurBoxes.map(b=>({...b})),cues:cueItems.map(c=>({...c})),overlays:overlayItems,clips:clipItems.map(c=>({...c})),quotes:[],audio_mix:audioMix,settings:{style:form.elements.style.value,duration:Number(form.elements.duration.value),start:Number(form.elements.start.value),captions:form.elements.captions.checked,mute:form.elements.mute.checked,mirror:form.elements.mirror.checked,layout:form.elements.layout.value||'fill',title_font:form.elements.title_font.value,caption_font:form.elements.caption_font.value,...Object.fromEntries(OFFSETS.map(k=>[k,Number(form.elements[k].value)||0]))},save_template:form.elements.save_template.checked};}
async function saveProduction(){
    if(pendingAudio.size)throw Error('오디오 업로드가 끝난 뒤 저장하세요.');
    if(mixerPanel.querySelector('input:invalid')){
        $('#production-form').reportValidity();
        throw Error('오디오 볼륨과 재생 구간을 확인하세요.');
    }
    recordEdit();
    const payload=structuredClone(productionPayload()), id=payload.id;
    const analysis=analysisDirty?readAnalysisEditor():null;
    const savedEdit=captureEdit();
    if(analysis)await request('/api/production/analysis',{id,analysis});
    const result=await request('/api/production/update',payload);
    await refreshProduction();
    await completeRecoverySave(id,savedEdit,analysis);
    return result;
}
$('#new-project').onclick=async()=>{try{await refreshProduction();if(!productionState.channels.length){switchView('channels');message('제작 채널을 먼저 등록하세요.');return;}$('#new-project-form').elements.channel_id.innerHTML=productionState.channels.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('');$('#new-project-form').reset();$('#new-project-form').elements.source_video_id.value='';$('#new-project-dialog').showModal();}catch(e){message(e.message);}};
$('#channel-form').onsubmit=async e=>{e.preventDefault();try{await request('/api/production/channels',Object.fromEntries(new FormData(e.target)));e.target.reset();await refreshProduction();message('제작 채널을 등록했습니다. 새 프로젝트를 만들 수 있습니다.');}catch(error){message(error.message);}};
$('#new-project-form').onsubmit=async e=>{e.preventDefault();try{const result=await request('/api/production/projects',Object.fromEntries(new FormData(e.target)));$('#new-project-dialog').close();e.target.reset();await refreshProduction();openProduction(result.id);}catch(error){message(error.message);}};
for(const id of ['project-query','project-channel-filter','project-status-filter'])$('#'+id).addEventListener(id==='project-query'?'input':'change',renderProjects);
$('#project-grid').onclick=e=>{const button=e.target.closest('[data-open-project]');if(button)openProduction(button.dataset.openProject);};
$('#production-form').addEventListener('input',e=>{if(e.target.closest('.analysis-panel,#cue-editor,#caption-drafts,[data-cue-select]')||e.target.matches('#clip-playhead,input[type=file],[name=save_template]'))return;invalidateMixPreview();$('#finished-preview').hidden=true;$('#production-downloads').hidden=true;if(['start','duration'].includes(e.target.name)){renderAudioLanes();renderCueTrack();}productionDirty=true;$('#production-render').disabled=true;$('#production-approve').disabled=false;$('#narration-generate').disabled=true;if(e.target.name==='script'){$('#script-status').textContent='대본을 수정했습니다. 생성 대본은 분석에서 다시 생성해야 문장 분류·장면 연결을 검증하고 승인할 수 있습니다.';$('#script-sentences').replaceChildren();}stylePreview();});
$('#production-form').onsubmit=async e=>{e.preventDefault();try{const result=await saveProduction();message(result.message);}catch(error){message(error.message);}};
$('#production-approve').onclick=async()=>{try{await saveProduction();await request('/api/production/approve',{id:activeProject});await refreshProduction();fillProduction(activeProject);message('현재 대본과 설정을 승인했습니다.');}catch(error){message(error.message);}};
$('#production-render').onclick=async()=>{if(productionDirty){message('변경사항을 저장하고 다시 승인하세요.');return;}try{await request('/api/production/render',{id:activeProject});await refreshProduction();fillProduction(activeProject);}catch(error){message(error.message);}};
async function uploadProduction(file){if(!file||uploading)return;if(file.size>200*1024*1024){message('영상은 최대 200MB입니다.');return;}uploading=true;try{await saveProduction();const id=activeProject;$('#upload-status').textContent='업로드 중…';await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',`/api/production/upload?id=${encodeURIComponent(id)}`);xhr.setRequestHeader('Content-Type','application/octet-stream');xhr.upload.onprogress=e=>{if(e.lengthComputable)$('#upload-status').textContent=`업로드 ${Math.round(e.loaded/e.total*100)}%`;};xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{reject(Error('업로드 응답을 읽지 못했습니다.'));return;}xhr.status<300?resolve(data):reject(Error(data.error||'업로드 실패'));};xhr.onerror=()=>reject(Error('업로드 연결이 중단되었습니다.'));xhr.send(file);});await refreshProduction();fillProduction(id);message('원본 업로드가 완료되었습니다.');}catch(error){message(error.message);$('#upload-status').textContent=error.message;}finally{uploading=false;}}
$('#production-upload').onchange=e=>uploadProduction(e.target.files[0]);
$('#source-drop').ondragover=e=>{e.preventDefault();$('#source-drop').classList.add('dragging');};$('#source-drop').ondragleave=()=>$('#source-drop').classList.remove('dragging');$('#source-drop').ondrop=e=>{e.preventDefault();$('#source-drop').classList.remove('dragging');uploadProduction(e.dataTransfer.files[0]);};
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>{const dialog=$('#'+button.dataset.close);dialog.querySelectorAll('video').forEach(v=>v.pause());dialog.close();});
let productionPolling=false;
setInterval(async()=>{
  if(productionPolling||workflowBusy||!productionState)return;
  const pendingUpload=Object.values(productionState.publishing?.jobs||{}).some(job=>['queued','running'].includes(job.status));
  if(!pendingUpload&&!productionState.projects.some(p=>p.status==='rendering'||shareImportPending(p)))return;
  productionPolling=true;
  try{
    const previousProjects=productionState.projects;
    await refreshProduction();
    const current=currentProduction();
    const previous=previousProjects.find(p=>p.id===activeProject);
    if(current&&$('#production-dialog').open&&shareImportPending(previous)){
      const changed=JSON.stringify(previous.document.share_import)!==JSON.stringify(current.document.share_import);
      if(changed&&!productionDirty&&!analysisDirty){fillProduction(current.id);}
      else if(changed){renderShareImport(current,false);if(!shareImportPending(current))message('공유 링크 가져오기가 끝났습니다. 현재 수정사항을 저장하면 원본 상태가 갱신됩니다.');}
    }
    if(previous?.status==='rendering'&&current&&$('#production-dialog').open&&
      (current.status!==previous.status||current.document.message!==previous.document.message)){
      fillProduction(current.id);
    }
  }catch(error){message(error.message);}finally{productionPolling=false;}
},2500);
Promise.all([refreshProduction(),loadRecoveryDrafts()]).then(openLatestRecovery).catch(e=>message(e.message));

$('#source-to-project').onclick=async()=>{try{const id=$('#research').elements.video_id.value;const data=Object.fromEntries(new FormData($('#research')));data.saved=$('#research').elements.saved.checked?1:0;await saveResearch(data);await refreshProduction();$('#research-dialog').close();if(!productionState.channels.length){switchView('channels');message('제작 채널을 등록한 뒤 원본 추적 노트에서 다시 이어가세요.');return;}const form=$('#new-project-form');form.reset();form.elements.source_video_id.value=id;form.elements.title.value=(snapshot.all_videos.find(v=>v.id===id)?.title||'새 제작 프로젝트').slice(0,100);form.elements.channel_id.innerHTML=productionState.channels.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('');$('#new-project-dialog').showModal();}catch(error){message(error.message);}};

// Comment cards from the source video's public comments ("댓글 작업 가능").
let commentPool=null,commentTab='ko';
function renderCommentStatus(p){commentPool=null;const picker=$('#comment-picker');picker.hidden=true;picker.innerHTML='';const info=p.comment_info||{},status=$('#comment-status');const count=info.count?` · 원본 댓글 ${fmt(info.count)}개`:'';status.className='comment-status'+(info.available?' ready':'');status.textContent=info.auto?`💬 댓글 작업 가능${count}`:info.available?`💬 댓글 작업 가능${count} · YouTube가 아니라서 직접 옮겨 적어야 해요`:'원본 링크가 YouTube 영상이면 댓글을 불러올 수 있어요.';$('#load-comments').hidden=!info.auto&&!!info.count;}
function renderPicker(){const picker=$('#comment-picker');if(!commentPool){picker.hidden=true;return;}const ko=commentPool.comments.filter(c=>c.korean),foreign=commentPool.comments.filter(c=>!c.korean);const items=commentTab==='ko'?ko:foreign;const note=commentPool.viral_kr?'한국 댓글이 많아요. 한국에서도 반응이 있는 영상이라 한국 댓글을 그대로 쓰면 좋아요.':'한국 댓글이 적어요. 외국어 댓글은 원문을 넣은 뒤 한국어로 고쳐 쓰세요.';picker.innerHTML=`<div class="picker-tabs"><button type="button" data-comment-tab="ko" class="${commentTab==='ko'?'active':''}">한국어 ${ko.length}</button><button type="button" data-comment-tab="foreign" class="${commentTab==='foreign'?'active':''}">외국어 ${foreign.length} · 번역 필요</button></div><p class="picker-note">${note} 좋아요 많은 순서예요. 닉네임은 가려서 넣어요.</p>`+(items.map((c,i)=>`<div class="picker-item"><p>${esc(c.text)}</p><small>♥ ${fmt(c.likes)}${c.replies?` · 답글 ${fmt(c.replies)}`:''} · ${esc(c.when)} · ${esc(c.name)}${c.fits?'':' · <span class="picker-long">80자 넘음, 줄여야 해요</span>'}</small><div class="picker-actions"><button type="button" class="primary" data-use-comment="${commentPool.comments.indexOf(c)}">${c.korean?'카드에 넣기':'원문 넣기'}</button>${c.korean?'':`<a class="outline" target="_blank" rel="noopener noreferrer" href="https://papago.naver.com/?sk=auto&tk=ko&st=${encodeURIComponent(c.text)}">파파고 번역</a>`}</div></div>`).join('')||'<p class="picker-note">이 쪽에는 쓸 만한 댓글이 없어요. 다른 탭을 보세요.</p>');picker.hidden=false;}
function clearComment(row){const form=$('#production-form');for(const key of ['name','when','at','text'])form.elements[`comment_${key}_${row}`].value='';const input=form.elements['comment_text_'+row];input.classList.remove('needs-translation');input.title='';form.dispatchEvent(new Event('input',{bubbles:true}));}
// Top 3 by likes go in automatically when every card is empty. Korean ones first when the video took off in Korea.
function autoPick(pool){const form=$('#production-form');if([0,1,2].some(i=>form.elements['comment_text_'+i].value.trim()))return 0;const fits=pool.comments.filter(c=>c.fits);const base=fits.length>=3?fits:pool.comments;const korean=base.filter(c=>c.korean);const picks=(pool.viral_kr&&korean.length?[...korean,...base.filter(c=>!c.korean)]:base).slice(0,3);picks.forEach(c=>useComment(c,true));return picks.length;}
function useComment(c,quiet=false){const form=$('#production-form');let row=-1;for(let i=0;i<3;i++)if(!form.elements['comment_text_'+i].value.trim()){row=i;break;}if(row<0){message('댓글 카드 3개가 다 찼어요. 하나를 지운 뒤 다시 넣으세요.');return;}const duration=Number(form.elements.duration.value)||30;const text=c.text.length>80?c.text.slice(0,79)+'…':c.text;const input=form.elements['comment_text_'+row];form.elements['comment_name_'+row].value=c.name;form.elements['comment_when_'+row].value=c.when;form.elements['comment_at_'+row].value=Math.round(duration*row/3*2)/2;input.value=text;input.classList.toggle('needs-translation',!c.korean);input.title=c.korean?'':'번역 필요: 한국어로 고쳐 쓰세요';form.dispatchEvent(new Event('input',{bubbles:true}));if(quiet)return;message(c.korean?`댓글 카드 ${row+1}에 넣었어요. 나오는 시점은 필요하면 고치세요.`:`댓글 카드 ${row+1}에 원문을 넣었어요. 파파고 번역을 참고해서 한국어로 고쳐 쓰세요.`);}
$('#load-comments').onclick=async()=>{const button=$('#load-comments');button.disabled=true;button.textContent='불러오는 중…';try{await saveProduction();const result=await request('/api/production/comments',{id:activeProject});commentPool=result;commentTab=result.korean?'ko':'foreign';renderPicker();$('#comment-status').textContent=`💬 카드 후보 ${result.total}개 · 한국어 ${result.korean}개`;const form=$('#production-form');const credited=!!result.credit&&!form.elements.credit.value.trim();if(credited){form.elements.credit.value=result.credit;form.dispatchEvent(new Event('input',{bubbles:true}));}const picked=autoPick(result);if(credited&&!picked)message(`출처 표시에 "${result.credit}"를 넣었어요.`);if(picked)message((credited?`출처 표시에 "${result.credit}"를 넣고, `:'')+`좋아요가 가장 많은 댓글 ${picked}개를 카드에 자동으로 넣었어요. 마음에 안 들면 ✕ 삭제 후 목록에서 다른 댓글을 넣으세요.`+(document.querySelector('#production-form .needs-translation')?' 주황색 칸은 한국어로 고쳐 쓰세요.':''));}catch(error){message(error.message);}finally{button.disabled=false;button.textContent='💬 원본 댓글 불러오기';}};
$('#comment-picker').onclick=e=>{const tab=e.target.closest('[data-comment-tab]');if(tab){commentTab=tab.dataset.commentTab;renderPicker();return;}const use=e.target.closest('[data-use-comment]');if(use&&commentPool)useComment(commentPool.comments[Number(use.dataset.useComment)]);};
$('#production-form').addEventListener('input',e=>{if(e.target.classList?.contains('needs-translation')&&/[가-힣]/.test(e.target.value)&&e.isTrusted){e.target.classList.remove('needs-translation');e.target.title='';}});
document.querySelector('.comment-cards').addEventListener('click',e=>{const button=e.target.closest('[data-clear-comment]');if(button)clearComment(Number(button.dataset.clearComment));});

// Analysis edits are kept separate from ordinary production settings until explicitly saved.
let analysisDirty=false,workflowBusy=false;
const sceneFieldLabels={description:'화면 설명',dialogue:'대사',subtitle_clues:'화면 자막 단서 (한 줄에 하나)',id_clues:'아이디·출처 단서 (한 줄에 하나)',facts:'확인된 사실 (한 줄에 하나)',inferences:'추정: 의도·원인·출처 (한 줄에 하나)'};
function currentProduction(){return productionState?.projects.find(p=>p.id===activeProject);}
function linesValue(value){return Array.isArray(value)?value.join('\n'):(value||'');}
function sceneEditor(scene,index){return `<fieldset class="analysis-scene" data-scene-id="${esc(scene.id||'scene-'+(index+1))}"><legend>장면 ${index+1} · ${esc(scene.id||'scene-'+(index+1))}</legend><div class="scene-times"><label>시작 · 초<input data-scene-field="start" type="number" min="0" step="0.01" value="${esc(String(scene.start??0))}"></label><label>끝 · 초<input data-scene-field="end" type="number" min="0" step="0.01" value="${esc(String(scene.end??1))}"></label><button type="button" data-scene-seek>장면 보기</button><button type="button" data-scene-remove aria-label="장면 ${index+1} 삭제">삭제</button></div>${Object.entries(sceneFieldLabels).map(([name,label])=>`<label class="scene-${name}">${label}<textarea data-scene-field="${name}" rows="${['facts','inferences'].includes(name)?3:2}" maxlength="5000">${esc(linesValue(scene[name]))}</textarea></label>`).join('')}</fieldset>`;}
function readAnalysisEditor(){return {scenes:[...document.querySelectorAll('#analysis-scenes .analysis-scene')].map(card=>{const scene={id:card.dataset.sceneId};for(const name of ['start','end',...Object.keys(sceneFieldLabels)]){const raw=card.querySelector(`[data-scene-field="${name}"]`).value;scene[name]=['start','end'].includes(name)?Number(raw):['facts','inferences','subtitle_clues','id_clues'].includes(name)?raw.split('\n').map(v=>v.trim()).filter(Boolean):raw.trim();}return scene;})};}
function markAnalysisDirty(){analysisDirty=true;$('#analysis-status').textContent='수정됨 · 분석을 저장하고 다시 확정하세요.';$('#script-generate').disabled=true;$('#production-approve').disabled=true;$('#production-render').disabled=true;$('#narration-generate').disabled=true;$('#analysis-confirm').disabled=false;$('#analysis-save').disabled=false;writeRecovery();}
function renderAnalysisWorkflow(p){
  analysisDirty=false;
  const doc=p.document,analysis=doc.analysis,locked=p.status==='rendering'||workflowBusy;
  $('#analysis-scenes').innerHTML=(analysis?.scenes||[]).map(sceneEditor).join('');
  $('#analysis-status').textContent=analysis?(analysis.confirmed?'분석 확정됨 · 확인된 내용을 대본에 사용할 수 있습니다.':'검토 대기 · 빈 칸을 채우고 사실과 추정을 확인하세요.'):'분석 양식을 만든 뒤 장면별 내용을 직접 확인하세요.';
  $('#analysis-start').disabled=locked||!doc.source||!!analysis;
  $('#analysis-add').disabled=locked||!doc.source;
  $('#analysis-save').disabled=locked||!analysis;
  $('#analysis-confirm').disabled=locked||!analysis||!!analysis.confirmed;
  $('#script-generate').disabled=locked||!analysis?.confirmed;
  const generated=doc.generated_script;
  $('#script-status').textContent=generated?'문장별 사실·추정 표시와 장면 연결을 검토한 뒤 대본·설정을 승인하세요.':'분석 확정 전에는 대본을 생성할 수 없습니다.';
  const sectionLabels={hook:'훅',situation:'상황',ending:'반전/마무리'};
  $('#script-sentences').innerHTML=(generated?.sentences||[]).map(sentence=>`<article class="script-sentence ${sentence.kind==='inference'?'is-inference':'is-fact'}"><div><span class="tag">${esc(sectionLabels[sentence.section]||sentence.section||'문장')}</span> <b>${sentence.kind==='inference'?'추정':'사실'}</b> <small>${esc(sentence.scene_id||'')}</small></div><p>${esc(sentence.text)}</p></article>`).join('');
  $('#narration-generate').disabled=locked||!doc.approved_hash||!generated;
  const narration=doc.narration;
  const currentVoice=!!narration&&!!doc.approved_hash&&narration.approved_hash===doc.approved_hash;
  $('#production-qa').disabled=locked||!generated||!currentVoice;
  if(generated&&!currentVoice)$('#production-render').disabled=true;
  $('#narration-status').textContent=narration?`${narration.mock?'모의 나레이션 · 무음 타이밍 시험':'나레이션 생성됨'} · ${narration.engine||''} · ${Number(narration.duration||0).toFixed(2)}초`:'나레이션을 생성하면 문장별 실제 파일 길이가 표시됩니다.';
  $('#narration-segments').innerHTML=narration?`<ol class="narration-timeline">${(narration.segments||[]).map(segment=>`<li><b>${Number(segment.start).toFixed(2)}–${Number(segment.end).toFixed(2)}초</b><span>${esc(segment.text||'')}</span><small>${esc(segment.scene_id||'')} · ${Number(segment.duration??segment.end-segment.start).toFixed(2)}초</small></li>`).join('')}</ol>`:'';
  const report=doc.qa_report;
  const narrationPresent=report?.checks?.some(check=>check.name==='narration_presence'&&check.status==='passed'&&check.metrics?.peak>0);
  const publication=narrationPresent&&doc.publish_approval;
  const publishPanel=$('#publication-review');
  publishPanel.hidden=p.status!=='completed'||!generated;
  $('#publication-confirm').checked=false;
  $('#publication-rights').value=publication?.rights_note||doc.share_import?.rights_note||'';
  $('#publication-approve').disabled=locked||!currentVoice||narration.mock!==false||!narrationPresent||report?.passed!==true||report?.audio_scope!=='rendered_mix'||!report?.output_sha256;
  const narrationIssue=report?.issues?.find(issue=>issue.code==='narration_not_audible');
  $('#publication-status').textContent=publication?'게시 준비 승인됨':
      report?.audio_scope==='rendered_mix'&&!narrationPresent?
          (narrationIssue?.message||'나레이션 신호를 점검하려면 다시 렌더링하세요.'):
          $('#publication-approve').disabled?'실제 나레이션과 최종 영상 QA 통과가 필요합니다.':'최종 영상과 원본 사용 권한을 확인한 뒤 승인하세요.';
  $('#qa-result').innerHTML=report?`<p class="workflow-status ${report.passed?'qa-passed':'qa-warning'}">QA ${report.passed?'통과':'확인 필요'} · 자동 점검은 의미·발음의 최종 검토를 대신하지 않습니다.</p><pre class="qa-report">${esc(JSON.stringify(report,null,2))}</pre>`:'';
}
async function workflowAction(path,payload={},success='완료했습니다.'){
  if(workflowBusy)return;
  const id=activeProject;
  const savedEdit=captureEdit(),analysis=analysisDirty?readAnalysisEditor():null;
  workflowBusy=true;
  document.querySelectorAll('.workflow-actions button').forEach(button=>button.disabled=true);
  try{
    const result=await request('/api/production/'+path,{id,...payload});
    await refreshProduction();
    workflowBusy=false;
    if(path==='analysis'||path==='confirm-analysis')await completeRecoverySave(id,savedEdit,analysis);
    else if(activeProject===id&&$('#production-dialog').open){
      fillProduction(id);
      restoreRecovery();
    }
    else if($('#production-dialog').open&&currentProduction()&&!analysisDirty)renderAnalysisWorkflow(currentProduction());
    const project=productionState.projects.find(p=>p.id===id);
    const narrationMessage=result.status==='completed'?result.message:project?.document.message||result.message;
    message(path==='narration'?narrationMessage:success);
  }catch(error){workflowBusy=false;const p=currentProduction();if(p&&!analysisDirty)renderAnalysisWorkflow(p);else{$('#analysis-save').disabled=false;$('#analysis-confirm').disabled=false;$('#analysis-add').disabled=false;}syncEditButtons();message(error.message);}
}
$('#analysis-scenes').addEventListener('input',markAnalysisDirty);
$('#analysis-scenes').addEventListener('click',event=>{const card=event.target.closest('.analysis-scene');if(!card)return;if(event.target.closest('[data-scene-remove]')){card.remove();markAnalysisDirty();}if(event.target.closest('[data-scene-seek]')){const time=Number(card.querySelector('[data-scene-field="start"]').value);if(Number.isFinite(time))$('#production-source-preview').currentTime=time;}});
$('#analysis-add').onclick=()=>{const scenes=readAnalysisEditor().scenes,start=scenes.length?scenes[scenes.length-1].end:0;let index=scenes.length+1;while(scenes.some(scene=>scene.id==='scene-'+index))index++;const end=Math.min(start+5,currentProduction()?.document.source?.duration||start+5);$('#analysis-scenes').insertAdjacentHTML('beforeend',sceneEditor({id:'scene-'+index,start,end},scenes.length));markAnalysisDirty();};
$('#analysis-start').onclick=async()=>{try{if(productionDirty)await saveProduction();await workflowAction('analyze',{},'모의 분석 양식을 만들었습니다. 원본을 보며 내용을 채워 주세요.');}catch(error){message(error.message);}};
$('#analysis-save').onclick=async()=>{const analysis=readAnalysisEditor();try{if(productionDirty)await saveProduction();await workflowAction('analysis',{analysis},'분석을 저장했습니다. 내용을 검토한 뒤 확정하세요.');}catch(error){message(error.message);}};
$('#analysis-confirm').onclick=async()=>{const analysis=readAnalysisEditor();try{if(productionDirty)await saveProduction();if(analysisDirty||!currentProduction()?.document.analysis){await request('/api/production/analysis',{id:activeProject,analysis});}await workflowAction('confirm-analysis',{},'분석을 확정했습니다. 대본을 생성할 수 있습니다.');}catch(error){message(error.message);}};
$('#script-generate').onclick=async()=>{if(analysisDirty){message('분석 수정사항을 먼저 저장하고 확정하세요.');return;}try{if(productionDirty)await saveProduction();await workflowAction('generate-script',{},'대본을 생성했습니다. 사실·추정과 장면을 검토한 뒤 승인하세요.');}catch(error){message(error.message);}};
$('#narration-generate').onclick=()=>{if(productionDirty||analysisDirty){message('변경사항을 저장하고 대본을 다시 승인하세요.');return;}workflowAction('narration');};
$('#production-qa').onclick=()=>{if(productionDirty||analysisDirty){message('변경사항을 저장한 뒤 QA를 실행하세요.');return;}workflowAction('qa',{},'QA 리포트를 확인하세요.');};
$('#publication-approve').onclick=()=>{
  if(productionDirty||analysisDirty){message('변경사항을 저장하고 최종 영상을 다시 렌더링하세요.');return;}
  workflowAction('approve-publish',{
    reviewed:$('#publication-confirm').checked,
    rights_note:$('#publication-rights').value
  },'게시 준비를 승인했습니다.');
};
$('#offset-reset').onclick=()=>{const form=$('#production-form');for(const k of OFFSETS)form.elements[k].value=0;markDirty();};

// Blur boxes over text burned into the source. Coordinates are 0~1 of the 1080x1920 frame, same as the render.
let blurBoxes=[];
function markDirty(){
    $('#production-form').dispatchEvent(new Event('input',{bubbles:true}));
    recordEdit();
}
const BLUR_MODE={ai:'AI 자막 지우기 (VSR)',text:'글자만 지우기 (정밀)',erase:'박스 채워 지우기',blur:'흐리게'};
function defaultBlurMode(){return productionState?.ai_ready?'ai':productionState?.letters_ready?'text':'erase';}
function blurToolStatus(){
    const aiReady=productionState?.ai_ready;
    const lettersReady=productionState?.letters_ready;
    $('#blur-add-ai').disabled=!aiReady||blurBoxes.length>=6;
    const ai=aiReady?'서버 AI 자막 지우개 사용 가능':'서버에 AI 자막 지우개가 준비되면 사용할 수 있어요.';
    const letters=lettersReady?'정밀 글자 제거 사용 가능':'서버에서 정밀 글자 제거를 사용할 수 없어요.';
    $('#blur-status').textContent=`${ai} · ${letters}`;
}
function renderBlurs(){$('#preview-blurs').innerHTML=blurBoxes.map((b,i)=>`<div class="blur-box mode-${b.mode||'erase'}" data-blur="${i}" style="left:${b.x*100}%;top:${b.y*100}%;width:${b.w*100}%;height:${b.h*100}%"><span>${i+1}</span><i data-blur-resize="${i}"></i></div>`).join('');const px=(v,size)=>Math.round(v*size);$('#blur-list').innerHTML=blurBoxes.map((b,i)=>`<div class="blur-row"><div class="blur-line"><b>박스 ${i+1}</b><select data-blur="${i}" data-k="mode">${Object.entries(BLUR_MODE).map(([k,v])=>`<option value="${k}" ${(b.mode||'erase')===k?'selected':''} ${(k==='text'&&!productionState?.letters_ready)||(k==='ai'&&!productionState?.ai_ready)?'disabled':''}>${v}</option>`).join('')}</select><label>시작<input type="number" min="0" max="60" step="0.5" value="${b.start}" data-blur="${i}" data-k="start">초</label><label>끝<input type="number" min="0" max="60" step="0.5" value="${b.end}" data-blur="${i}" data-k="end">초</label><button type="button" class="outline" data-blur-remove="${i}">✕ 삭제</button></div><div class="blur-line fine"><span>미세 조정(px)</span>${[['x','가로',1080],['y','세로',1920],['w','너비',1080],['h','높이',1920]].map(([k,label,size])=>`<label>${label}<input type="number" step="2" min="0" max="${size}" value="${px(b[k],size)}" data-blur="${i}" data-k="${k}" data-size="${size}"></label>`).join('')}</div></div>`).join('');$('#blur-add').disabled=blurBoxes.length>=6;$('#blur-check').hidden=!blurBoxes.length;blurToolStatus();}
function addRemovalBox(mode){
    if(blurBoxes.length>=6)return;
    const layout=$('#production-form').elements.layout.value||'band';
    blurBoxes.push({x:0.1,y:layout==='fill'?0.82:0.7,w:0.8,h:0.05,start:0,end:0,mode});
    renderBlurs();
    markDirty();
}
$('#blur-add').onclick=()=>addRemovalBox(defaultBlurMode());
$('#blur-add-ai').onclick=()=>{if(productionState?.ai_ready)addRemovalBox('ai');};
$('#blur-list').addEventListener('input',e=>{const t=e.target,i=Number(t.dataset.blur),k=t.dataset.k,b=blurBoxes[i];if(!k||!b)return;if(k==='mode')b.mode=t.value;else if(t.dataset.size){const v=Math.max(0,Number(t.value)||0)/Number(t.dataset.size);b[k]=Math.round(Math.min(v,1)*10000)/10000;if(b.x+b.w>1)b.w=1-b.x;if(b.y+b.h>1)b.h=1-b.y;const box=document.querySelector(`[data-blur="${i}"].blur-box`);if(box)Object.assign(box.style,{left:b.x*100+'%',top:b.y*100+'%',width:b.w*100+'%',height:b.h*100+'%'});}else b[k]=Math.max(0,Number(t.value)||0);});
$('#blur-list').addEventListener('change',e=>{if(e.target.dataset.k==='mode')renderBlurs();});
function framePreviewDraft(){
    const fields=$('#production-form').elements;
    return {id:activeProject,settings:{start:Number(fields.start.value),duration:Number(fields.duration.value),
        layout:fields.layout.value||'fill',mirror:fields.mirror.checked},clips:clipItems,blurs:blurBoxes};
}

async function checkProductionFrame(event){
    const button=event.currentTarget;
    const buttons=[$('#blur-check'),$('#clip-check-frame')];
    const label=button.textContent;
    const source=currentProduction()?.document.source?.filename;
    const time=mixVideo.hidden?cueTime():mixVideo.currentTime;
    const body=JSON.stringify({...framePreviewDraft(),t:time});
    stopClipPreview();
    mixVideo.pause();
    for(const control of buttons)control.disabled=true;
    button.textContent='만드는 중…';
    try{
        const response=await fetch('/api/production/frame',{method:'POST',
            headers:{'Content-Type':'application/json'},body});
        if(!response.ok){
            const data=await response.json().catch(()=>({}));
            throw new Error(data.error||'미리보기 화면을 만들지 못했어요.');
        }
        const blob=await response.blob();
        if(!productionDialog.open||source!==currentProduction()?.document.source?.filename||
           body!==JSON.stringify({...framePreviewDraft(),t:time}))return;
        const img=$('#preview-frame');
        if(img.dataset.url)URL.revokeObjectURL(img.dataset.url);
        img.dataset.url=URL.createObjectURL(blob);
        img.src=img.dataset.url;
        img.hidden=false;
        message('지금 시점의 실제 결과예요. 화면을 누르면 닫혀요.');
    }catch(error){message(error.message);}
    finally{
        for(const control of buttons)control.disabled=false;
        button.textContent=label;
    }
}
$('#blur-check').onclick=checkProductionFrame;
$('#preview-frame').onclick=()=>{$('#preview-frame').hidden=true;};
$('#blur-list').addEventListener('click',e=>{const b=e.target.closest('[data-blur-remove]');if(!b)return;blurBoxes.splice(Number(b.dataset.blurRemove),1);renderBlurs();markDirty();});
$('#preview-blurs').addEventListener('pointerdown',e=>{const box=e.target.closest('[data-blur]');if(!box)return;e.preventDefault();const i=Number(box.dataset.blur),resize=!!e.target.closest('[data-blur-resize]');const phone=$('.phone-preview');const W=phone.clientWidth,H=phone.clientHeight;const start={...blurBoxes[i]},sx=e.clientX,sy=e.clientY;box.setPointerCapture(e.pointerId);
const move=ev=>{const dx=(ev.clientX-sx)/W,dy=(ev.clientY-sy)/H;const b=blurBoxes[i];if(resize){b.w=Math.min(Math.max(start.w+dx,0.03),1-b.x);b.h=Math.min(Math.max(start.h+dy,0.015),1-b.y);}else{b.x=Math.min(Math.max(start.x+dx,0),1-b.w);b.y=Math.min(Math.max(start.y+dy,0),1-b.h);}for(const k of ['x','y','w','h'])b[k]=Math.round(b[k]*10000)/10000;Object.assign(box.style,{left:b.x*100+'%',top:b.y*100+'%',width:b.w*100+'%',height:b.h*100+'%'});};
const up=()=>{box.removeEventListener('pointermove',move);box.removeEventListener('pointerup',up);box.removeEventListener('pointercancel',up);markDirty();};
box.addEventListener('pointermove',move);box.addEventListener('pointerup',up);box.addEventListener('pointercancel',up);});

// Caption timeline: each line gets its own start, place and look. Mirrors production.cue_events.
let cueItems=[];
let selectedCues=new Set();
const CUE_POS={top:'위',middle:'가운데',bottom:'아래'},CUE_LOOK={outline:'기본',box:'박스',accent:'노랑 강조',lyric:'가사(기울임)'};
const CUE_SIZE={outline:75,box:60,accent:75,lyric:48};

function cueTime(){return clipItems.length?clipOutputTime():Math.min(Math.max(($('#production-source-preview').currentTime||0)-(Number($('#production-form').elements.start.value)||0),0),overlayDuration());}
function cueEnd(c,index,rows,duration){return Math.min(c.end||(rows[index+1]?.start??duration),duration);}
function activeCue(time=cueTime()){
    // Source-to-output conversion can land just below a centisecond boundary.
    time+=0.000001;
    const rows=cueItems.filter(c=>c.text.trim());
    const duration=overlayDuration();
    const active=rows.findLast((c,i)=>c.start<=time&&time<cueEnd(c,i,rows,duration));
    return active?cueItems.indexOf(active):-1;
}
function cueLines(text){const words=[];text.split(/\*([^*]+)\*/).forEach((part,i)=>part.split(/\s+/).filter(Boolean).forEach(w=>words.push([w.replaceAll('*',''),i%2===1])));const lines=[];let line=[];for(const word of words){if(line.length&&line.reduce((n,[w])=>n+w.length+1,0)+word[0].length>CAPTION_LINE){lines.push(line);line=[];}line.push(word);}if(line.length)lines.push(line);return lines.slice(0,3);}
function previewCue(form,ratio,style){
    const el=$('#preview-caption');
    el.className='';
    for(const prop of ['top','bottom','transform','webkitTextStroke','paintOrder','textShadow'])el.style[prop]='';
    const i=form.elements.captions.checked?activeCue(outputPreviewTime()):-1;
    document.querySelectorAll('.cue-row,.cue-segment').forEach(row=>row.classList.toggle('active',Number(row.dataset.cueRow??row.dataset.cueSegment)===i));
    if(!cueItems.some(c=>c.text.trim())||!form.elements.captions.checked)return;
    if(i<0){el.innerHTML='';return;}
    const c=cueItems[i], custom=c.style||{};
    const layout=form.elements.layout.value||'band';
    const accent=c.look==='accent';
    const mark=custom.color||(accent?style.caption:style.highlight);
    const size=custom.size??CUE_SIZE[c.look];
    el.className='look-'+c.look;
    el.style.color=custom.color||(accent?'#ffe34a':c.look==='lyric'?'#ffffff':style.caption);
    el.style.fontSize=size*ratio+'px';
    if('font' in custom){
        el.style.fontFamily=previewFont(custom.font)+'sans-serif';
        el.style.fontWeight=custom.font?'normal':'900';
    }
    if('outline' in custom){
        el.style.webkitTextStroke=`${2*custom.outline*ratio}px ${custom.outline_color||'#000000'}`;
        el.style.paintOrder='stroke fill';
        el.style.textShadow='none';
    }
    const lines=cueLines(c.text).map(line=>`<span class="line">${line.map(([w,m])=>m?`<span style="color:${mark}">${esc(w)}</span>`:esc(w)).join(' ')}</span>`).join('<br>');
    const sub=c.sub?`<small style="font-size:${39*size/CUE_SIZE[c.look]*ratio}px;color:${custom.color||'#ffffff'}">${esc(c.sub)}</small>`:'';
    if('background' in custom){
        el.classList.remove('look-box');
        el.innerHTML=`<span class="cue-preview-box" style="background:${custom.background||'transparent'};padding:${custom.background?6*ratio:0}px">${lines}${sub}</span>`;
    }else el.innerHTML=lines+sub;
    const shift=c.y*1.5*ratio;
    if(c.pos==='top'){el.style.top='26.56%';el.style.bottom='auto';el.style.transform=`translateY(${shift}px)`;}
    else if(c.pos==='middle'){el.style.top=(layout==='fill'?61.72:50.78)+'%';el.style.bottom='auto';el.style.transform=`translateY(calc(-50% + ${shift}px))`;}
    else el.style.transform=`translateY(${shift}px)`;
}

function updateCueSelection(){
    cueItems.forEach((c,i)=>{
        const selected=selectedCues.has(c);
        $(`[data-cue-select="${i}"]`).checked=selected;
        $(`[data-cue-row="${i}"]`).classList.toggle('selected',selected);
        $(`[data-cue-segment="${i}"]`)?.setAttribute('aria-pressed',selected);
    });
    $('#cue-selection').textContent=`${selectedCues.size}줄 선택`;
    $('#cue-apply-style').disabled=!selectedCues.size;
}
function renderCueTrack(){
    const duration=overlayDuration();
    const rows=cueItems.filter(c=>c.text.trim()).sort((a,b)=>a.start-b.start);
    const lanes=[];
    const segments=rows.map((c,index)=>{
        const end=cueEnd(c,index,rows,duration);
        if(end<=c.start)return '';
        let lane=lanes.findIndex(occupied=>occupied<=c.start);
        if(lane<0)lane=lanes.length;
        lanes[lane]=end;
        const i=cueItems.indexOf(c);
        return `<button type="button" class="cue-segment" data-cue-segment="${i}" aria-pressed="${selectedCues.has(c)}" aria-label="${esc(c.text)}, ${c.start}~${end}초" title="${esc(c.text)} · ${c.start}~${end}초" style="left:${100*c.start/duration}%;width:${100*(end-c.start)/duration}%;top:${lane*28}px">${esc(c.text)}</button>`;
    });
    const track=$('#cue-track');
    track.style.height=Math.max(1,lanes.length)*28+'px';
    track.innerHTML=segments.join('');
}
function loadCues(doc){
    clearCaptionDrafts();
    resetReferenceVideo();
    referenceFilename='';
    cueItems=(doc.cues||[]).map(c=>({...c}));
    selectedCues=new Set();
    $('#cue-style-font').innerHTML=fontOptions('');
    renderCues();
}
function splitCue(){
    const time=Math.round(cueTime()*100)/100;
    const rows=cueItems.filter(c=>c.text.trim());
    const i=activeCue(time);
    const cue=cueItems[i];
    if(!cue||time<=Math.round(cue.start*100)/100||time>=Math.round(cueEnd(cue,rows.indexOf(cue),rows,overlayDuration())*100)/100){
        message('자막의 시작과 끝 사이에서 분할하세요.');return;
    }
    if(cueItems.length>=40){message('자막은 최대 40줄까지 추가할 수 있어요.');return;}
    stopClipPreview();
    const tail={...cue,start:time};
    cueItems.splice(i,1,{...cue,end:time},tail);
    sortCues();
    selectedCues=new Set([tail]);
    renderCues();
    seekOutputTime(time);
    markDirty();
    const input=$(`[data-cue="${cueItems.indexOf(tail)}"][data-k="text"]`);
    input.focus();
    input.select();
}
function applyCueStyle(){
    if(!selectedCues.size)return;
    const patch={};
    for(const checkbox of document.querySelectorAll('[data-style-key]:checked')){
        const key=checkbox.dataset.styleKey,input=$('#cue-style-'+key);
        if(!input.reportValidity())return;
        patch[key]=input.type==='number'?input.valueAsNumber:input.value;
        if(key==='outline')patch.outline_color=$('#cue-style-outline_color').value;
        if(key==='background'&&$('#cue-style-transparent').checked)patch.background='';
    }
    const changed=[...selectedCues].filter(c=>Object.entries(patch).some(([k,v])=>c.style?.[k]!==v));
    if(!changed.length)return;
    for(const cue of changed)cue.style={...cue.style,...patch};
    updateCueSelection();
    markDirty();
    message(`자막 ${changed.length}줄에 스타일을 적용했어요.`);
}
$('#cue-split').onclick=splitCue;
$('#cue-undo').onclick=()=>stepEdit(false);
$('#cue-redo').onclick=()=>stepEdit(true);
$('#cue-apply-style').onclick=applyCueStyle;
$('#production-dialog').addEventListener('keydown',event=>{
    const target=event.target;
    if(event.defaultPrevented||event.isComposing||event.repeat||target.isContentEditable||
       target.closest('textarea,select,input:not([type=checkbox]):not([type=radio]):not([type=range])')||
       $('#production-fields').disabled)return;
    if(event.code==='KeyS'&&event.altKey&&event.shiftKey&&!event.ctrlKey&&!event.metaKey){
        event.preventDefault();splitCue();
    }
});
function selectCue(c,selected){
    if(selected)selectedCues.add(c);
    else selectedCues.delete(c);
    updateCueSelection();
}
$('#cue-track').addEventListener('click',event=>{
    const segment=event.target.closest('[data-cue-segment]');
    if(!segment)return;
    const cue=cueItems[Number(segment.dataset.cueSegment)];
    selectCue(cue,!selectedCues.has(cue));
});

function renderCues(){$('#cue-list').innerHTML=cueItems.map((c,i)=>`<div class="cue-row" data-cue-row="${i}"><div class="cue-texts"><label class="check"><input type="checkbox" data-cue-select="${i}" aria-label="자막 ${i+1} 선택"></label><input maxlength="60" data-cue="${i}" data-k="text" value="${esc(c.text)}" placeholder="자막 ${i+1} (*강조*)"><input maxlength="60" data-cue="${i}" data-k="sub" value="${esc(c.sub||'')}" placeholder="작은 번역 줄 (선택)"></div><div class="cue-controls"><label>시작<input type="number" min="0" max="60" step="0.01" data-cue="${i}" data-k="start" value="${c.start}"></label><button type="button" class="outline" data-cue-now="${i}" title="미리보기 영상의 지금 시점을 시작으로">⏱ 지금</button><label>끝<input type="number" min="0" max="60" step="0.01" data-cue="${i}" data-k="end" value="${c.end}"></label><label class="check"><input type="checkbox" data-cue="${i}" data-k="fixed" ${c.fixed?"checked":""}>완성 시점 고정</label><select data-cue="${i}" data-k="pos">${Object.entries(CUE_POS).map(([k,v])=>`<option value="${k}" ${c.pos===k?'selected':''}>${v}</option>`).join('')}</select><select data-cue="${i}" data-k="look">${Object.entries(CUE_LOOK).map(([k,v])=>`<option value="${k}" ${c.look===k?'selected':''}>${v}</option>`).join('')}</select><label title="위아래 미세 조정">↕<input type="number" min="-300" max="300" step="5" data-cue="${i}" data-k="y" value="${c.y}"></label><button type="button" class="outline" data-cue-seek="${i}" title="미리보기를 이 줄 시점으로">▶</button><button type="button" class="outline" data-cue-remove="${i}">✕</button></div>${cueEvidence(c,i)}</div>`).join('');$('#cue-add').disabled=cueItems.length>=40;renderCueTrack();updateCueSelection();}
function newCue(start){return {text:'',sub:'',start,end:0,pos:'bottom',look:'outline',y:0};}

const captionDraftPanel=$('#caption-drafts');
const captionDraftLines=$('#caption-draft-lines');
const captionDraftStatus=$('#caption-draft-status');
const captionInsert=$('#caption-insert');
const evidenceLabels={reference:'한국 참고 영상',scene:'원본 컷',review:'구매 후기'};
let captionDraft=null;
let captionRequest=0;
let referenceFilename='';
let recognitionController=null;
const recognitionResults=$('#caption-recognition-results');
const recognitionStatus=$('#caption-recognition-status');
const referenceFile=$('#caption-reference-file');
const referenceVideo=$('#reference-video');
const referenceOverlay=$('#reference-region-overlay');
const referenceRegionEdit=$('#reference-region-edit');
let referenceVideoUrl='';
let referenceRegion=null;
let referenceDrag=null;
let analyzedReferenceRegion=null;

function showReferenceRegion(region){
    const box=$('#reference-region-box');
    box.hidden=!region;
    if(!region)return;
    const [left,top,right,bottom]=region;
    Object.assign(box.style,{left:`${left*100}%`,top:`${top*100}%`,
        width:`${(right-left)*100}%`,height:`${(bottom-top)*100}%`});
}

function setReferenceDrawing(enabled){
    referenceRegionEdit.setAttribute('aria-pressed',String(enabled));
    referenceOverlay.classList.toggle('drawing',enabled);
    referenceVideo.controls=!enabled;
    if(enabled)referenceVideo.pause();
}

function setReferenceRegion(region){
    referenceRegion=region;
    showReferenceRegion(region);
    setReferenceDrawing(false);
    const scope=region?'지정한 영역에서 화면 자막을 읽습니다.':'전체 화면에서 자막을 읽습니다.';
    const changed=analyzedReferenceRegion!==null&&String(region)!==analyzedReferenceRegion;
    $('#reference-region-status').textContent=scope+(changed?' 새 영역으로 읽으려면 인식을 다시 실행하세요.':'');
}

function resetReferenceVideo(){
    clearReferenceRecognition();
    referenceDrag=null;
    if(referenceVideoUrl){
        referenceVideo.removeAttribute('src');
        referenceVideo.load();
        URL.revokeObjectURL(referenceVideoUrl);
        referenceVideoUrl='';
    }
    $('#reference-preview').hidden=true;
    setReferenceRegion(null);
}

referenceVideo.addEventListener('loadedmetadata',()=>{
    if(referenceVideo.videoHeight){
        $('#reference-stage').style.setProperty('--reference-width',
            `${300*referenceVideo.videoWidth/referenceVideo.videoHeight}px`);
    }
});
referenceRegionEdit.onclick=()=>{
    const enabled=referenceRegionEdit.getAttribute('aria-pressed')!=='true';
    setReferenceDrawing(enabled);
    $('#reference-region-status').textContent=enabled?'미리보기에서 자막 영역을 끌어서 지정하세요.':
        (referenceRegion?'지정한 영역에서 화면 자막을 읽습니다.':'전체 화면에서 자막을 읽습니다.');
};
$('#reference-region-bottom').onclick=()=>setReferenceRegion([0,.55,1,1]);
$('#reference-region-full').onclick=()=>setReferenceRegion(null);

function referencePoint(event){
    const rect=referenceOverlay.getBoundingClientRect();
    const clamp=value=>Math.min(1,Math.max(0,value));
    return [clamp((event.clientX-rect.left)/rect.width),clamp((event.clientY-rect.top)/rect.height)];
}

function referenceDragBounds(event){
    const [x,y]=referencePoint(event);
    const [sx,sy]=referenceDrag.start;
    return [Math.min(x,sx),Math.min(y,sy),Math.max(x,sx),Math.max(y,sy)];
}

referenceOverlay.addEventListener('pointerdown',event=>{
    if(referenceDrag||event.button!==0||!referenceVideo.videoWidth)return;
    referenceDrag={id:event.pointerId,start:referencePoint(event)};
    referenceOverlay.setPointerCapture(event.pointerId);
});
referenceOverlay.addEventListener('pointermove',event=>{
    if(referenceDrag?.id===event.pointerId)showReferenceRegion(referenceDragBounds(event));
});
referenceOverlay.addEventListener('pointerup',event=>{
    if(referenceDrag?.id!==event.pointerId)return;
    const bounds=referenceDragBounds(event);
    referenceDrag=null;
    if(bounds[2]-bounds[0]<.05||bounds[3]-bounds[1]<.05){
        showReferenceRegion(referenceRegion);
        $('#reference-region-status').textContent='가로·세로 각각 5% 이상을 끌어서 지정하세요.';
        return;
    }
    setReferenceRegion(bounds);
});
referenceOverlay.addEventListener('lostpointercapture',event=>{
    if(referenceDrag?.id!==event.pointerId)return;
    referenceDrag=null;
    showReferenceRegion(referenceRegion);
});

function clearReferenceRecognition(){
    recognitionController?.abort();
    recognitionController=null;
    analyzedReferenceRegion=null;
    recognitionResults.replaceChildren();
    recognitionResults.hidden=true;
    recognitionStatus.textContent='';
    $('#caption-use-recognition').hidden=true;
    $('#caption-recognize').disabled=false;
}

referenceFile.addEventListener('change',()=>{
    resetReferenceVideo();
    const file=referenceFile.files[0];
    if(!file||file.size>100*1024*1024)return;
    referenceVideoUrl=URL.createObjectURL(file);
    $('#reference-stage').style.removeProperty('--reference-width');
    referenceVideo.src=referenceVideoUrl;
    $('#reference-preview').hidden=false;
});
$('#production-dialog').addEventListener('close',resetReferenceVideo);

function renderRecognitionGroup(kind,rows){
    if(!rows.length)return;
    const group=document.createElement('fieldset');
    const title=document.createElement('legend');
    title.textContent=kind==='화면'?'화면 자막 · 1초 간격':'음성 받아쓰기';
    group.append(title);
    for(const row of rows){
        const line=document.createElement('div');
        line.className='reference-recognition-line';
        const time=`${row.start.toFixed(1)}–${row.end.toFixed(1)}초`;
        const select=document.createElement('input');
        select.type='checkbox';
        select.value=`[${time} ${kind}]`;
        select.setAttribute('aria-label',`${kind} ${time} 문구 가져오기`);
        const seek=document.createElement('button');
        seek.type='button';
        seek.textContent=time;
        seek.setAttribute('aria-label',`${kind} ${time} 참고 영상에서 보기`);
        const text=document.createElement('input');
        text.type='text';
        text.maxLength=500;
        text.value=row.text;
        text.setAttribute('aria-label',`${kind} ${time} 문구 수정`);
        seek.onclick=()=>{
            setReferenceDrawing(false);
            referenceVideo.pause();
            referenceVideo.currentTime=row.start;
            referenceVideo.scrollIntoView({block:'nearest'});
            text.focus({preventScroll:true});
        };
        line.append(select,seek,text);
        group.append(line);
    }
    recognitionResults.append(group);
}

$('#caption-recognize').onclick=async()=>{
    clearReferenceRecognition();
    const file=referenceFile.files[0];
    if(!file||file.size>100*1024*1024){
        recognitionStatus.textContent='100MB 이하의 참고 영상 파일을 선택하세요.';
        return;
    }
    const controller=new AbortController();
    recognitionController=controller;
    const button=$('#caption-recognize');
    button.disabled=true;
    setReferenceRegion(referenceRegion);
    recognitionStatus.textContent='음성과 화면 자막을 읽고 있어요…';
    try{
        const regionAtStart=String(referenceRegion);
        const query=referenceRegion?new URLSearchParams({region:referenceRegion.map(n=>n.toFixed(6)).join(',')}):'';
        const response=await fetch(`/api/production/reference-captions${query?`?${query}`:''}`,{
            method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file,signal:controller.signal
        });
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'참고 영상을 읽지 못했어요.');
        if(recognitionController!==controller)return;
        analyzedReferenceRegion=regionAtStart;
        if(regionAtStart!==String(referenceRegion))setReferenceRegion(referenceRegion);
        renderRecognitionGroup('음성',data.speech);
        renderRecognitionGroup('화면',data.screen);
        const count=data.speech.length+data.screen.length;
        recognitionResults.hidden=!count;
        $('#caption-use-recognition').hidden=!count;
        recognitionStatus.textContent=[
            count?`문구 ${count}개를 읽었어요. 사용할 줄을 선택하세요.`:'인식된 문구가 없어요.',
            ...data.warnings
        ].join(' ');
    }catch(error){
        if(recognitionController===controller)recognitionStatus.textContent=error.message;
    }finally{
        if(recognitionController===controller){
            recognitionController=null;
            button.disabled=false;
        }
    }
};

recognitionResults.addEventListener('keydown',event=>{
    if(event.key==='Enter'&&event.target.matches('input[type=text]'))event.preventDefault();
});

$('#caption-use-recognition').onclick=()=>{
    const lines=[...recognitionResults.querySelectorAll('.reference-recognition-line')];
    const text=lines.flatMap(line=>{
        const select=line.querySelector('input[type=checkbox]');
        const value=line.querySelector('input[type=text]').value.trim();
        return select.checked&&value?[`${select.value} ${value}`]:[];
    }).join('\n');
    if(!text||text.length>1000){
        recognitionStatus.textContent='가져올 문구를 1,000자 이내로 선택하세요.';
        return;
    }
    referenceFilename=referenceFile.files[0].name.slice(0,180);
    $('#caption-reference').value=text;
    clearCaptionDrafts();
    recognitionStatus.textContent='선택한 문구로 참고 자막을 채웠어요. 필요하면 고쳐 쓰세요.';
};

function evidenceMarkup(evidence){
    const link=safeShareLink(evidence.url);
    return `${evidence.filename?`<p>${esc(evidence.filename)}</p>`:''}<p>${esc(evidence.note)}</p>${link?`<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">출처 열기</a>`:''}`;
}

function cueEvidence(cue,index){
    if(!cue.evidence)return '';
    const label=evidenceLabels[cue.evidence.kind];
    const seek=cue.evidence.kind==='scene'&&index!==undefined?
        `<button type="button" data-cue-seek="${index}">연결된 컷 보기</button>`:'';
    return `<details class="cue-evidence"><summary>근거 · ${esc(label)}</summary>${evidenceMarkup(cue.evidence)}${seek}</details>`;
}

function clearCaptionDrafts(){
    captionRequest++;
    captionDraft=null;
    captionInsert.hidden=true;
    captionDraftLines.replaceChildren();
    captionDraftStatus.textContent='';
    $('#caption-build').disabled=false;
}

function captionTimelineState(){
    const form=$('#production-form');
    return JSON.stringify([activeProject,currentProduction()?.document.source?.filename,
        form.elements.start.value,overlayDuration(),clipItems.map(({start,end})=>[start,end]),
        cueItems.map(({start,end,text})=>[start,end,text])]);
}

function captionDraftTimes(lines,start,clipEnd){
    if(cueItems.length+lines.length>40)throw new Error('자막은 최대 40줄까지 추가할 수 있어요.');
    const duration=overlayDuration();
    const existing=cueItems.filter(cue=>cue.text.trim());
    if(existing.some((cue,i)=>cue.start<=start&&cueEnd(cue,i,existing,duration)>start)){
        throw new Error('기존 자막과 겹쳐요. 자막이 비어 있는 지점으로 이동하세요.');
    }
    const next=Math.min(existing.find(cue=>cue.start>start)?.start??duration,duration);
    const step=Math.floor(Math.min(2.5,(next-start)/lines.length,(clipEnd-start)/2)*100)/100;
    if(step<1)throw new Error('자막을 넣을 빈 시간이 부족해요. 여유 있는 컷이나 시점으로 이동하세요.');
    const at=index=>Math.round((start+step*index)*100)/100;
    return lines.map((line,index)=>({...newCue(at(index)),...line,end:at(index+1)}));
}

$('#caption-build').onclick=async()=>{
    clearCaptionDrafts();
    const token=captionRequest;
    const doc=currentProduction()?.document;
    if(!doc?.source){captionDraftStatus.textContent='먼저 원본 영상을 올리세요.';return;}
    const state=captionTimelineState();
    const start=Math.ceil((cueTime()-0.000001)*100)/100;
    const clipEnd=clipItems.length?clipOffset(clipIndex+1):overlayDuration();
    const button=$('#caption-build');
    button.disabled=true;
    const value=id=>$('#caption-'+id).value;
    try{
        const response=await request('/api/production/caption-drafts',{
            product:value('product'),reference:value('reference'),reference_url:value('reference-url'),
            reference_filename:referenceFilename,
            scene:value('scene'),scene_url:$('#production-form').elements.source_url.value||doc.share_import?.original_url,
            reviews:value('reviews'),review_url:value('review-url'),same_product:$('#caption-same-product').checked
        });
        if(token!==captionRequest||!productionDialog.open)return;
        if(state!==captionTimelineState())throw new Error('타임라인이 바뀌었어요. 초안을 다시 만드세요.');
        const lines=captionDraftTimes(response.lines,start,clipEnd);
        captionDraft={state,lines};
        captionDraftLines.innerHTML=lines.map((line,i)=>`<div class="caption-draft-line">
            <label>${line.start.toFixed(2)}–${line.end.toFixed(2)}초 · ${esc(evidenceLabels[line.evidence.kind])}
            <input maxlength="60" data-draft-line="${i}" value="${esc(line.text)}"></label>
            ${cueEvidence(line)}
        </div>`).join('');
        captionDraftStatus.textContent='문구와 근거를 확인하고 타임라인에 추가하세요.';
        captionInsert.hidden=false;
    }catch(error){
        if(token===captionRequest)captionDraftStatus.textContent=error.message;
    }finally{
        if(token===captionRequest)button.disabled=false;
    }
};

captionDraftPanel.addEventListener('input',event=>{
    if(!event.target.matches('[data-draft-line]'))clearCaptionDrafts();
});
captionInsert.onclick=()=>{
    if(!captionDraft)return;
    if(captionDraft.state!==captionTimelineState()){
        clearCaptionDrafts();
        captionDraftStatus.textContent='타임라인이 바뀌었어요. 초안을 다시 만드세요.';
        return;
    }
    const lines=captionDraft.lines.map((line,index)=>({...line,
        text:captionDraftLines.querySelector(`[data-draft-line="${index}"]`).value.trim()}));
    if(lines.some(line=>!line.text)){
        captionDraftStatus.textContent='비어 있는 자막 문구를 채우세요.';
        return;
    }
    cueItems.push(...lines);
    sortCues();
    $('#production-form').elements.captions.checked=true;
    renderCues();
    markDirty();
    clearCaptionDrafts();
    captionDraftStatus.textContent=`자막 ${lines.length}줄을 추가했습니다. 각 줄에서 근거를 열 수 있어요.`;
};
$('#cue-translate').onclick=async()=>{
    const button=$('#cue-translate'), status=$('#cue-translate-status');
    const projectId=activeProject, before=JSON.stringify(cueItems);
    const indexes=cueItems.flatMap((cue,index)=>cue.text.trim()?[index]:[]);
    if(!indexes.length){status.textContent='번역할 자막을 먼저 추가하거나 대본에서 줄을 만드세요.';return;}
    const source=$('#cue-source-language').value.trim()||'auto';
    const target=$('#cue-target-language').value.trim();
    if(!target){status.textContent='번역할 언어를 선택하세요.';return;}
    const mode=$('#cue-translation-mode').value;
    button.disabled=true;
    status.textContent='자막을 번역하는 중입니다.';
    try{
        const {lines}=await request('/api/production/translate-subtitles',{
            source,target,lines:indexes.map(index=>cueItems[index].text.replace(/\*([^*]+)\*/g,'$1').trim())
        });
        if(activeProject!==projectId||!productionDialog.open||JSON.stringify(cueItems)!==before||
            $('#production-fields').disabled){
            status.textContent='편집 상태가 바뀌었습니다. 현재 자막으로 다시 번역하세요.';
            return;
        }
        const longLine=lines.findIndex(line=>Array.from(line).length>60);
        if(longLine>=0)throw new Error(`자막 ${indexes[longLine]+1}번의 번역이 60자를 넘어요. 원문을 짧게 나눠 다시 번역하세요.`);
        indexes.forEach((index,i)=>{cueItems[index][mode]=lines[i];});
        renderCues();
        markDirty();
        status.textContent=`${lines.length}줄을 번역했습니다. 미리보기에서 확인하고 저장하세요.`;
    }catch(error){status.textContent=error.message;}
    finally{button.disabled=false;}
};
function sortCues(){cueItems.sort((a,b)=>a.start-b.start);}
$('#cue-from-script').onclick=()=>{const form=$('#production-form');const script=form.elements.script.value.trim();if(!script){message('대본을 먼저 쓰세요.');return;}if(cueItems.length&&!confirm('지금 타임라인을 지우고 대본에서 다시 만들까요?'))return;let lines=script.split(/\n+/).map(l=>l.trim()).filter(Boolean);if(lines.length===1)lines=script.split(/(?<=[.?!。])\s+/).map(l=>l.trim()).filter(Boolean);lines=lines.slice(0,40).map(l=>l.slice(0,60));const duration=overlayDuration();selectedCues.clear();cueItems=lines.map((text,i)=>({...newCue(Math.round(duration*i/lines.length*100)/100),text}));renderCues();markDirty();message(`대본을 ${lines.length}줄로 나눴어요. 줄마다 시점·위치·모양을 맞추세요.`);};
$('#cue-add').onclick=()=>{if(cueItems.length>=40)return;cueItems.push(newCue(Math.round(cueTime()*10)/10));sortCues();renderCues();markDirty();};
$('#cue-clear').onclick=()=>{if(cueItems.length&&!confirm('자막 타임라인을 비울까요? 대본은 그대로 남아요.'))return;selectedCues.clear();cueItems=[];renderCues();markDirty();};
$('#cue-list').addEventListener('input',e=>{
    const t=e.target,i=Number(t.dataset.cue),k=t.dataset.k;
    if(!k||!cueItems[i])return;
    if(k==='fixed'){if(t.checked)cueItems[i].fixed=true;else delete cueItems[i].fixed;return;}
    cueItems[i][k]=['start','end','y'].includes(k)?(Number(t.value)||0):t.value;
});
$('#cue-list').addEventListener('change',e=>{
    const t=e.target;
    if(t.hasAttribute('data-cue-select')){selectCue(cueItems[Number(t.dataset.cueSelect)],t.checked);return;}
    if(t.dataset.k==='start'){sortCues();renderCues();recordEdit();}
    else if(t.dataset.k)renderCueTrack();
    stylePreview();
});
$('#cue-list').addEventListener('click',e=>{
    const now=e.target.closest('[data-cue-now]'),seek=e.target.closest('[data-cue-seek]'),remove=e.target.closest('[data-cue-remove]');
    if(now){
        cueItems[Number(now.dataset.cueNow)].start=Math.round(cueTime()*100)/100;
        sortCues();renderCues();markDirty();
    }
    if(seek){stopClipPreview();seekOutputTime(cueItems[Number(seek.dataset.cueSeek)].start);stylePreview();}
    if(remove){
        const [cue]=cueItems.splice(Number(remove.dataset.cueRemove),1);
        selectedCues.delete(cue);renderCues();markDirty();
    }
});
$('#production-source-preview').addEventListener('timeupdate',()=>{if(productionDialog.open)stylePreview();});
$('#production-source-preview').addEventListener('seeked',()=>{if(productionDialog.open)stylePreview();});

let overlayItems=[],overlayDocument=null,selectedOverlay=-1;
const OVERLAY_STYLE={color:'#ffffff',outline_color:'#000000',outline_width:3,background_color:'#000000',background_opacity:0};
const overlayVideo=$('#production-source-preview');
const overlayLayer=$('#preview-overlays');
const overlayTime=$('#overlay-time');
const overlayList=$('#overlay-list');
$('#show-shorts-guides').onchange=event=>{
    $('#shorts-guides').hidden=!event.target.checked;
};

function loadOverlays(doc){
    if(overlayDocument?.source?.filename!==doc.source?.filename){overlayTime.value=0;selectedOverlay=-1;}
    overlayDocument=doc;
    const y=doc.settings.layout==='fill'?.78:.65;
    const previous=(doc.quotes||[]).map(item=>({...item,x:.5,y,font:'',
        size:Math.max(24,Math.min(54,Math.floor(960/item.text.length)))}));
    overlayItems=[...(doc.overlays||[]),...previous].map(item=>({...item}));
    renderOverlayEditor();
}

function overlayDuration(){
    if(clipItems.length)return clipTotal();
    const form=$('#production-form');
    const remaining=overlayDocument?.source?.duration-Number(form.elements.start.value);
    return mixerVoice()?.duration||Math.max(0,Math.min(
        Number(form.elements.duration.value)||60,Number.isFinite(remaining)?remaining:60));
}

function renderOverlayEditor(){
    overlayList.innerHTML=overlayItems.map((item,index)=>{
        const style={...OVERLAY_STYLE,...item.style};
        return `
        <div class="overlay-row" data-overlay-row="${index}">
            <label>글귀 ${index+1}<input required maxlength="60" data-overlay-field="text" value="${esc(item.text)}"></label>
            <div class="overlay-controls">
                <label>크기(px)<input required type="number" min="24" max="144" step="1" data-overlay-field="size" value="${item.size}"></label>
                <label>서체<select data-overlay-field="font">${fontOptions(item.font)}</select></label>
                <label>시작(초)<input required type="number" min="0" max="60" step="0.01" data-overlay-field="start" value="${item.start}"></label>
                <label>끝(초)<input required type="number" min="0" max="60" step="0.01" data-overlay-field="end" value="${item.end}"></label>
                <label class="check"><input type="checkbox" data-overlay-field="fixed" ${item.fixed?'checked':''}>완성 시점 고정</label>
            </div>
            <details data-overlay-style ${selectedOverlay===index?'open':''}>
                <summary>글귀 ${index+1} 스타일</summary>
                <div class="overlay-controls">
                    <label>글자색<input type="color" data-overlay-style-field="color" value="${style.color}"></label>
                    <label>외곽선 색<input type="color" data-overlay-style-field="outline_color" value="${style.outline_color}"></label>
                    <label>외곽선 두께(px)<input type="number" min="0" max="20" step="1" data-overlay-style-field="outline_width" value="${style.outline_width}"></label>
                    <label>배경색<input type="color" data-overlay-style-field="background_color" value="${style.background_color}"></label>
                    <label>배경 불투명도(%)<input type="number" min="0" max="100" step="1" data-overlay-style-field="background_opacity" value="${style.background_opacity}"></label>
                </div>
            </details>
            <button type="button" class="outline" data-overlay-seek>시작 시점 보기</button>
            <button type="button" class="outline" data-overlay-remove>삭제</button>
        </div>`;}).join('');
    $('#overlay-add').disabled=overlayItems.length>=40;
    $('#overlay-playhead').hidden=!overlayItems.length;
    overlayLayer.replaceChildren(...overlayItems.map((_,index)=>{
        const label=document.createElement('button');
        label.type='button';
        label.className='preview-overlay';
        label.dataset.overlay=index;
        label.title='클릭하여 스타일 편집 · 끌어서 이동';
        label.append(document.createElement('span'));
        return label;
    }));
}

function updateOverlayPreview(){
    const duration=overlayDuration();
    overlayTime.max=duration;
    const time=Number(overlayTime.value);
    $('#overlay-time-label').textContent=time.toFixed(2)+'초';
    const ratio=overlayLayer.clientWidth/1080;
    const locked=$('#production-fields').disabled;
    overlayItems.forEach((item,index)=>{
        const label=overlayLayer.children[index];
        const text=item.text.trim();
        const size=Math.max(24,Math.min(144,item.size));
        label.hidden=!text||time<item.start||time>=Math.min(item.end||duration,duration);
        label.disabled=locked;
        const style={...OVERLAY_STYLE,...item.style};
        const lines=wrapWords(text,Math.round(918/size));
        label.firstElementChild.textContent=lines.join('\n');
        label.setAttribute('aria-label',`글귀 ${index+1} 스타일 편집 · ${text}`);
        const padding=Math.ceil(size*.16)+style.outline_width;
        const background=style.background_opacity>0&&lines.length>0;
        Object.assign(label.style,{left:`${item.x*100}%`,top:`${item.y*100}%`,
            fontSize:`${size*ratio}px`,fontFamily:previewFont(item.font)+'Malgun Gothic, sans-serif',
            fontWeight:item.font?'normal':'bold',
            width:background?`${(Math.max(...lines.map(line=>Array.from(line).length))*size+2*padding)*ratio}px`:'',
            height:background?`${(Math.round(lines.length*size*1.2)+2*padding)*ratio}px`:'',
            backgroundColor:background?`color-mix(in srgb, ${style.background_color} ${style.background_opacity}%, transparent)`:'transparent'});
        Object.assign(label.firstElementChild.style,{color:style.color,
            webkitTextStroke:`${2*style.outline_width*ratio}px ${style.outline_color}`});
    });
}

function seekOverlayPreview(time){
    overlayTime.value=time;
    overlayVideo.pause();
    if(clipItems.length&&overlayVideo.readyState){
        seekOutputTime(Number(overlayTime.value));
        updateOverlayPreview();
        return;
    }
    let sourceTime=Number($('#production-form').elements.start.value)+Number(overlayTime.value);
    const narration=mixerVoice();
    if(narration){
        const segment=narration.segments.find(s=>s.start<=time&&time<s.end)||narration.segments.at(-1);
        const scene=overlayDocument.analysis?.scenes.find(s=>s.id===segment?.scene_id);
        if(scene)sourceTime=Math.min(scene.start+Math.max(0,time-segment.start),scene.end);
    }
    if(overlayVideo.readyState)overlayVideo.currentTime=Math.min(sourceTime,overlayVideo.duration);
    updateOverlayPreview();
}

$('#overlay-add').onclick=()=>{
    if(overlayItems.length>=40)return;
    const time=Number(overlayTime.value);
    const start=time<overlayDuration()?time:0;
    overlayItems.push({text:'새 글귀',x:.5,y:.5,size:72,font:'',start,end:0});
    renderOverlayEditor();
    seekOverlayPreview(start);
    markDirty();
    overlayList.querySelector('[data-overlay-row]:last-child input').select();
};
overlayList.addEventListener('input',event=>{
    const field=event.target.dataset.overlayField;
    const styleField=event.target.dataset.overlayStyleField;
    const row=event.target.closest('[data-overlay-row]');
    if((!field&&!styleField)||!row)return;
    const item=overlayItems[row.dataset.overlayRow];
    if(field==='fixed'){if(event.target.checked)item.fixed=true;else delete item.fixed;return;}
    if(styleField){
        const numeric=event.target.type==='number';
        const value=numeric?event.target.valueAsNumber:event.target.value;
        if(numeric&&(!Number.isInteger(value)||!event.target.validity.valid))return;
        item.style={...OVERLAY_STYLE,...item.style,[styleField]:value};
        selectedOverlay=Number(row.dataset.overlayRow);
        updateOverlayPreview();
        return;
    }
    item[field]=event.target.type==='number'?Number(event.target.value):event.target.value;
});
overlayList.addEventListener('click',event=>{
    const row=event.target.closest('[data-overlay-row]');
    if(!row)return;
    const index=Number(row.dataset.overlayRow);
    if(event.target.closest('[data-overlay-seek]'))seekOverlayPreview(overlayItems[index].start);
    if(event.target.closest('[data-overlay-remove]')){
        overlayItems.splice(index,1);
        if(selectedOverlay>=index)selectedOverlay=selectedOverlay===index?-1:selectedOverlay-1;
        renderOverlayEditor();
        markDirty();
    }
});
overlayTime.addEventListener('input',()=>seekOverlayPreview(Number(overlayTime.value)));
function syncOverlayTime(){
    if(!overlayItems.length||mixerVoice())return;
    overlayTime.max=overlayDuration();
    overlayTime.value=cueTime();
    updateOverlayPreview();
}
overlayVideo.addEventListener('timeupdate',syncOverlayTime);
overlayVideo.addEventListener('seeked',syncOverlayTime);
new ResizeObserver(updateOverlayPreview).observe(overlayLayer);

function moveOverlay(item,x,y){
    item.x=Math.round(Math.max(0,Math.min(1,x))*10000)/10000;
    item.y=Math.round(Math.max(0,Math.min(1,y))*10000)/10000;
    updateOverlayPreview();
}
function openOverlayStyle(index){
    const details=overlayList.querySelector(`[data-overlay-row="${index}"] [data-overlay-style]`);
    if(!details)return;
    selectedOverlay=index;
    details.open=true;
    details.scrollIntoView({block:'nearest'});
    details.querySelector('summary').focus({preventScroll:true});
}
overlayLayer.addEventListener('click',event=>{
    const label=event.target.closest('[data-overlay]');
    if(label&&!label.disabled&&(!label.dataset.dragged||event.detail===0))openOverlayStyle(Number(label.dataset.overlay));
});
overlayLayer.addEventListener('pointerdown',event=>{
    const label=event.target.closest('[data-overlay]');
    if(!label||label.disabled||event.button!==0||!event.isPrimary)return;
    event.preventDefault();
    label.focus({preventScroll:true});
    overlayVideo.pause();
    const item=overlayItems[label.dataset.overlay];
    const {x,y}=item;
    const {clientX,clientY,pointerId}=event;
    delete label.dataset.dragged;
    const {width,height}=overlayLayer.getBoundingClientRect();
    label.setPointerCapture(pointerId);
    const move=event=>{
        if(event.pointerId===pointerId&&(label.dataset.dragged||Math.hypot(event.clientX-clientX,event.clientY-clientY)>4)){
            label.dataset.dragged='1';
            moveOverlay(item,x+(event.clientX-clientX)/width,y+(event.clientY-clientY)/height);
        }
    };
    label.addEventListener('pointermove',move);
    label.addEventListener('lostpointercapture',()=>{
        label.removeEventListener('pointermove',move);
        if(item.x!==x||item.y!==y)markDirty();
    },{once:true});
});
overlayLayer.addEventListener('keydown',event=>{
    const label=event.target.closest('[data-overlay]');
    const direction={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
    if(!label||label.disabled||!direction)return;
    event.preventDefault();
    const item=overlayItems[label.dataset.overlay];
    const step=event.shiftKey?10:1;
    moveOverlay(item,item.x+direction[0]*step/1080,item.y+direction[1]*step/1920);
    markDirty();
});

// Manual cuts retain their explicit order; repeated source ranges are valid.
let clipItems=[],clipIndex=0,clipPlaying=false;
const clipPicture=$('#clip-picture');
const clipFraming=$('#clip-framing');
const frameDefaults={mode:'default',zoom:1,x:0,y:0};
const gradeDefaults={brightness:0,contrast:0,saturation:0,warmth:0};
const gradePresets={original:gradeDefaults,
    vivid:{brightness:3,contrast:18,saturation:24,warmth:0},
    warm:{brightness:4,contrast:4,saturation:10,warmth:9},
    mono:{brightness:0,contrast:0,saturation:-100,warmth:0}};
const gradeEditor=$('#clip-color');
let gradeBefore=false,appliedGrade='';
let lastFramedClip;

function syncGradeFields(grade){
    for(const input of gradeEditor.querySelectorAll('[data-grade-field]')){
        const field=input.dataset.gradeField;
        input.value=grade[field];
        const value=grade[field];
        $('#grade-'+field).textContent=value>0?`+${value}`:value;
        const progress=50+50*value/Number(input.max);
        input.style.setProperty('--grade-from',`${Math.min(50,progress)}%`);
        input.style.setProperty('--grade-to',`${Math.max(50,progress)}%`);
    }
    for(const button of gradeEditor.querySelectorAll('[data-grade-preset]')){
        const preset=gradePresets[button.dataset.gradePreset];
        button.setAttribute('aria-pressed',String(Object.keys(gradeDefaults).every(key=>preset[key]===grade[key])));
    }
    const compare=$('#grade-compare');
    compare.setAttribute('aria-pressed',String(gradeBefore));
    compare.disabled=!clipItems[clipIndex]?.grade;
    $('#grade-reset').disabled=compare.disabled;
    $('#grade-preview-badge').hidden=!gradeBefore||compare.disabled;
}

function previewGrade(clip){
    const grade=clip?.grade;
    const key=grade&&!gradeBefore?JSON.stringify(grade):'';
    if(key===appliedGrade)return;
    appliedGrade=key;
    if(!key){overlayVideo.style.filter='';return;}
    const c=1+grade.contrast/100,s=1+grade.saturation/100;
    const brightness=grade.brightness/100,warmth=grade.warmth/100;
    const offset=128/255*(1-c)+brightness;
    $('#grade-tone-matrix').setAttribute('values',
        `${c} 0 0 0 ${offset+warmth} 0 ${c} 0 0 ${offset} 0 0 ${c} 0 ${offset-warmth} 0 0 0 1 0`);
    const weights=[.2126,.7152,.0722];
    const rows=weights.map((_,i)=>weights.map((weight,j)=>weight*(1-s)+(i===j?s:0)).join(' ')+' 0 0');
    $('#grade-saturation-matrix').setAttribute('values',rows.join(' ')+' 0 0 0 1 0');
    overlayVideo.style.filter='url("#clip-grade-filter")';
}

function syncFramingFields(frame){
    for(const input of clipFraming.querySelectorAll('[data-frame-field]'))input.value=frame[input.dataset.frameField];
    for(const button of clipFraming.querySelectorAll('[data-frame-mode]')){
        button.setAttribute('aria-pressed',String(button.dataset.frameMode===frame.mode));
    }
}

function previewClipFraming(){
    const clip=clipItems[clipIndex],video=overlayVideo;
    const frame=clip?.framing||frameDefaults;
    const form=$('#production-form');
    clipFraming.hidden=!clip;
    $('.phone-preview').classList.toggle('manual-clip',Boolean(clip));
    video.controls=!clip;
    if(clip!==lastFramedClip){
        lastFramedClip=clip;
        gradeBefore=false;
        syncFramingFields(frame);
        syncGradeFields({...gradeDefaults,...clip?.grade});
    }
    previewGrade(clip);
    $('#clip-framing-label').textContent=clip?`클립 ${clipIndex+1} · 원본 ${clip.start}–${clip.end}초`:'';
    $('#grade-clip-label').textContent=clip?`클립 ${clipIndex+1}`:'';
    $('#clip-list').querySelectorAll('.clip-row').forEach((row,index)=>{
        const selected=Boolean(clip)&&index===clipIndex;
        row.classList.toggle('selected',selected);
        row.querySelector('[data-clip-action="seek"]').setAttribute('aria-current',String(selected));
    });
    const transform=form.elements.mirror.checked?'scaleX(-1)':'none';
    if(!clip||!video.videoWidth){
        Object.assign(video.style,{width:'',height:'',left:'',top:'',transform});
        return;
    }
    const layout=form.elements.layout.value,area=layout==='fill'?1470:1050;
    const fit=frame.mode==='fit'||(frame.mode==='default'&&layout==='fit');
    const scale=(fit?Math.min:Math.max)(1080/video.videoWidth,area/video.videoHeight)*frame.zoom;
    const width=Math.max(2,Math.round(video.videoWidth*scale));
    const height=Math.max(2,Math.round(video.videoHeight*scale));
    Object.assign(video.style,{
        width:`${100*width/1080}%`,height:`${100*height/area}%`,
        left:`${100*(Math.floor((1080-width)/2)+frame.x)/1080}%`,
        top:`${100*(Math.floor((area-height)/2)+frame.y)/area}%`,transform
    });
}

function changeGrade(grade){
    const clip=clipItems[clipIndex];
    if(!clip)return;
    if(Object.keys(gradeDefaults).every(key=>grade[key]===gradeDefaults[key]))delete clip.grade;
    else clip.grade={...grade};
    gradeBefore=false;
    $('#preview-frame').hidden=true;
    syncGradeFields(grade);
    previewGrade(clip);
}

gradeEditor.addEventListener('input',event=>{
    const field=event.target.dataset.gradeField;
    if(!field||!clipItems[clipIndex])return;
    stopClipPreview();
    changeGrade({...gradeDefaults,...clipItems[clipIndex].grade,[field]:Number(event.target.value)});
});
gradeEditor.addEventListener('click',event=>{
    const preset=event.target.closest('[data-grade-preset]')?.dataset.gradePreset;
    if(preset||event.target.id==='grade-reset'){
        stopClipPreview();
        changeGrade(gradePresets[preset]||gradeDefaults);
        markDirty();
    }
});
$('#grade-compare').onclick=()=>{
    if(!clipItems[clipIndex])return;
    $('#preview-frame').hidden=true;
    if(!mixVideo.hidden){
        const time=mixVideo.currentTime;
        invalidateMixPreview();
        seekTimeline(time);
    }
    gradeBefore=!gradeBefore;
    syncGradeFields({...gradeDefaults,...clipItems[clipIndex].grade});
    previewGrade(clipItems[clipIndex]);
};

clipFraming.addEventListener('click',event=>{
    const mode=event.target.closest('[data-frame-mode]')?.dataset.frameMode;
    const clip=clipItems[clipIndex];
    if(!mode||!clip)return;
    stopClipPreview();
    if(mode==='default')delete clip.framing;
    else clip.framing={...frameDefaults,mode};
    syncFramingFields(clip.framing||frameDefaults);
    markDirty();
});
clipFraming.addEventListener('input',event=>{
    const input=event.target,field=input.dataset.frameField,clip=clipItems[clipIndex];
    if(!field||!clip||!input.validity.valid)return;
    stopClipPreview();
    const frame={...(clip.framing||frameDefaults),[field]:Number(input.value)};
    if(Object.keys(frameDefaults).every(key=>frame[key]===frameDefaults[key]))delete clip.framing;
    else clip.framing=frame;
    if(typeof previewClipFraming==='function')previewClipFraming();
});
clipFraming.addEventListener('change',event=>{
    const input=event.target,field=input.dataset.frameField;
    if(field&&!input.validity.valid)input.value=(clipItems[clipIndex]?.framing||frameDefaults)[field];
});

clipPicture.addEventListener('pointerdown',event=>{
    const clip=clipItems[clipIndex];
    if(!clip||event.button!==0||!event.isPrimary||$('#production-fields').disabled)return;
    event.preventDefault();
    stopClipPreview();
    const frame={...(clip.framing||frameDefaults)};
    const {pointerId,clientX,clientY}=event;
    const {width,height}=clipPicture.getBoundingClientRect();
    const area=$('#production-form').elements.layout.value==='fill'?1470:1050;
    clipPicture.setPointerCapture(pointerId);
    const move=event=>{
        if(event.pointerId!==pointerId)return;
        const x=Math.max(-1080,Math.min(1080,Math.round(frame.x+(event.clientX-clientX)*1080/width)));
        const y=Math.max(-1470,Math.min(1470,Math.round(frame.y+(event.clientY-clientY)*area/height)));
        if(x===(clip.framing||frameDefaults).x&&y===(clip.framing||frameDefaults).y)return;
        clip.framing={...frame,x,y};
        syncFramingFields(clip.framing);
        previewClipFraming();
    };
    clipPicture.addEventListener('pointermove',move);
    clipPicture.addEventListener('lostpointercapture',()=>{
        clipPicture.removeEventListener('pointermove',move);
        if((clip.framing||frameDefaults).x===frame.x&&(clip.framing||frameDefaults).y===frame.y){
            if(Object.keys(frameDefaults).every(key=>frame[key]===frameDefaults[key]))delete clip.framing;
            return;
        }
        markDirty();
    },{once:true});
});
overlayVideo.addEventListener('loadedmetadata',()=>{
    if(pendingPreviewTime!==null){
        const time=pendingPreviewTime;
        pendingPreviewTime=null;
        restorePreviewTime(time);
    }else if(clipItems.length)seekOutputTime(clipOffset(clipIndex));
    if(typeof previewClipFraming==='function')previewClipFraming();
});
$('#clip-check-frame').onclick=checkProductionFrame;
function clipTotal(){return clipItems.reduce((sum,c)=>sum+c.end-c.start,0);}
function clipOffset(index){return clipItems.slice(0,index).reduce((sum,c)=>sum+c.end-c.start,0);}
function clipOutputTime(){if(!clipItems.length)return 0;clipIndex=Math.max(0,Math.min(clipIndex,clipItems.length-1));const c=clipItems[clipIndex];return clipOffset(clipIndex)+Math.max(0,Math.min(($('#production-source-preview').currentTime||0)-c.start,c.end-c.start));}
function seekOutputTime(time){const video=$('#production-source-preview');if(!clipItems.length){video.currentTime=(Number($('#production-form').elements.start.value)||0)+Math.max(0,time);return;}let remain=Math.max(0,Math.min(time,clipTotal()));clipIndex=clipItems.length-1;for(let i=0;i<clipItems.length;i++){const length=clipItems[i].end-clipItems[i].start;if(remain<length||i===clipItems.length-1){clipIndex=i;video.currentTime=clipItems[i].start+Math.min(remain,length);break;}remain-=length;}updateClipClock();}
function validateClipTimeline(includeQuotes=true){const duration=currentProduction()?.document.source?.duration||0;if(clipItems.length>40||clipItems.some(c=>!Number.isFinite(c.start)||!Number.isFinite(c.end)||c.start<0||c.end-c.start<0.099999||c.end>duration+0.001)||clipTotal()>60.001)throw Error('클립은 원본 안에서 0.1초 이상, 총 60초 이내로 설정하세요.');if(!includeQuotes)return;}
function loadClipTimeline(doc){clipPlaying=false;gradeBefore=false;clipItems=(doc.clips||[]).map(c=>({...c}));clipIndex=0;renderClips();if(clipItems.length&&overlayVideo.readyState)seekOutputTime(0);}
function renderClips(){let at=0;$('#clip-list').innerHTML=clipItems.map((c,i)=>{const start=at;at+=c.end-c.start;return `<article class="clip-row"><b>클립 ${i+1} · 완성 ${start.toFixed(2)}–${at.toFixed(2)}초</b><div class="clip-fields"><label>원본 시작<input type="number" min="0" step="0.001" data-clip="${i}" data-part="start" value="${c.start}"></label><label>원본 끝<input type="number" min="0" step="0.001" data-clip="${i}" data-part="end" value="${c.end}"></label></div><div class="workflow-actions"><button type="button" data-clip-action="seek" data-index="${i}">장면 보기</button><button type="button" data-clip-action="first" data-index="${i}" ${i===0?'disabled':''}>맨 앞으로</button><button type="button" data-clip-action="up" data-index="${i}" ${i===0?'disabled':''}>↑ 앞</button><button type="button" data-clip-action="down" data-index="${i}" ${i===clipItems.length-1?'disabled':''}>↓ 뒤</button><button type="button" data-clip-action="delete" data-index="${i}">삭제</button></div></article>`;}).join('');$('#clip-status').textContent=clipItems.length?`${clipItems.length}개 클립 · 합계 ${clipTotal().toFixed(2)}초 / 최대 60초`:'클립 편집 꺼짐 · 기존 시작/길이 구간을 사용합니다.';$('#clip-init').disabled=!!clipItems.length||!currentProduction()?.document.source;$('#clip-split').disabled=!clipItems.length||clipItems.length>=40;$('#clip-play').disabled=!clipItems.length;$('#clip-clear').disabled=!clipItems.length;$('#clip-playhead').disabled=!clipItems.length;$('#clip-playhead').max=clipItems.length?clipTotal():60;updateClipClock();}
function updateClipClock(){if(typeof previewClipFraming==='function')previewClipFraming();const time=cueTime();$('#clip-clock').textContent=time.toFixed(2)+'초';$('#clip-playhead').value=time;syncOverlayTime();updateOutputPlayhead();}
function stopClipPreview() {clipPlaying=false;$('#production-source-preview').pause();}
function changeClips(){stopClipPreview();clipIndex=Math.min(clipIndex,Math.max(clipItems.length-1,0));renderClips();renderCueTrack();renderAudioLanes();markDirty();}
$('#clip-init').onclick=()=>{const form=$('#production-form'),source=currentProduction()?.document.source;if(!source)return;const start=Number(form.elements.start.value)||0,end=Math.min(start+(Number(form.elements.duration.value)||60),source.duration);if(end-start<0.1){message('원본 안에서 0.1초 이상의 구간을 선택하세요.');return;}clipItems=[{start,end}];clipIndex=0;changeClips();seekOutputTime(0);};
$('#clip-split').onclick=()=>{if(!clipItems.length||clipItems.length>=40)return;const cut=Math.round($('#production-source-preview').currentTime*100)/100,c=clipItems[clipIndex];if(cut-c.start<0.099999||c.end-cut<0.099999){message('클립 안쪽에서 나누세요. 양쪽 모두 0.1초 이상이어야 합니다.');return;}clipItems.splice(clipIndex,1,{...c,end:cut},{...c,start:cut});changeClips();};
$('#clip-clear').onclick=()=>{clipItems=[];changeClips();message('클립 편집을 해제했습니다. 기존 시작/길이 구간을 사용합니다.');};
$('#clip-stop').onclick=()=>{
    stopClipPreview();
    if(mixRequest)invalidateMixPreview();
    else mixVideo.pause();
};
$('#clip-play').onclick=()=>{if(audioMix){playMixPreview();return;}try{validateClipTimeline();seekOutputTime(0);clipPlaying=true;$('#production-source-preview').play().catch(error=>{clipPlaying=false;message(error.message);});}catch(error){message(error.message);}};
$('#clip-playhead').oninput=e=>{
    const time=Number(e.target.value),mixVideo=$('#mix-preview-video');
    stopClipPreview();
    if(!mixVideo.hidden&&Number.isFinite(mixVideo.duration))mixVideo.currentTime=Math.min(time,mixVideo.duration);
    seekOutputTime(time);
    stylePreview();
};
$('#clip-list').addEventListener('change',event=>{
    const input=event.target,key=input.dataset.part;
    if(!key)return;
    const index=Number(input.dataset.clip),value=input.valueAsNumber;
    const clip=clipItems[index],source=currentProduction()?.document.source?.duration||0;
    const next={...clip,[key]:value};
    if(!Number.isFinite(value)||next.start<0||next.end>source||next.end-next.start<0.099999||
        clipTotal()-(clip.end-clip.start)+(next.end-next.start)>60.000001){
        input.value=clip[key];
        message('클립은 원본 안에서 0.1초 이상, 총 60초 이내로 설정하세요.');
        return;
    }
    commitClipTrim(index,next,key);
});
$('#clip-list').addEventListener('click',event=>{
    const button=event.target.closest('[data-clip-action]');
    if(!button)return;
    const index=Number(button.dataset.index),action=button.dataset.clipAction;
    if(action==='seek'){
        stopClipPreview();
        seekOutputTime(clipOffset(index));
        stylePreview();
        return;
    }
    const order=clipItems.map((_,i)=>i);
    order.splice(index,1);
    const target=action==='first'?0:action==='up'?index-1:index+1;
    if(action!=='delete'){
        if(target<0||target>=clipItems.length||target===index)return;
        order.splice(target,0,index);
    }
    applyClipOrder(order,action==='delete'?Math.min(index,Math.max(0,order.length-1)):target);
});

function applyClipOrder(order,selection){
    if(order.length&&!remapClipText(order))return;
    clipItems=order.map(i=>clipItems[i]);
    clipIndex=selection;
    changeClips();
    if(clipItems.length)seekOutputTime(clipOffset(clipIndex));
    stylePreview();
}

function remapClipText(order,replacement=null){
    const duration=clipTotal();
    const cuts=clipItems.map((clip,index)=>({start:clipOffset(index),length:clip.end-clip.start}));
    let offset=0;
    const spans=order.map(index=>{
        const {start,length}=cuts[index];
        const old=clipItems[index],next=replacement?.index===index?replacement.clip:old;
        const trimIn=next.start-old.start;
        const span={start:start+Math.max(0,trimIn),end:start+Math.min(length,next.end-old.start),
            shift:offset-start-trimIn};
        offset+=next.end-next.start;
        return span;
    });
    const round=time=>Math.round(time*100)/100;
    function moveItems(items,isCue=false){
        return items.flatMap((item,index)=>{
            if(item.fixed)return [item];
            const end=item.end||(isCue?items.find((next,i)=>i>index&&next.text.trim())?.start:0)||duration;
            const fragments=[];
            for(const span of spans){
                const start=round(Math.max(item.start,span.start)+span.shift);
                const endAt=round(Math.min(end,span.end)+span.shift);
                if(endAt<=start)continue;
                const previous=fragments.at(-1);
                if(previous?.end===start)previous.end=endAt;
                else fragments.push({...item,start,end:endAt});
            }
            const last=fragments.at(-1);
            if(last&&item.end===0&&last.end===round(offset))last.end=0;
            return fragments;
        }).sort((a,b)=>a.start-b.start);
    }
    const cues=moveItems(cueItems,true),overlays=moveItems(overlayItems);
    if(cues.length>40||overlays.length>40){
        message('클립 경계에서 나뉜 자막이나 글귀가 40개를 넘습니다.');
        return false;
    }
    selectedCues.clear();
    cueItems=cues;
    overlayItems=overlays;
    renderCues();
    renderOverlayEditor();
    return true;
}

function commitClipTrim(index,next,edge){
    const old=clipItems[index];
    if(old.start===next.start&&old.end===next.end)return;
    if(!remapClipText(clipItems.map((_,i)=>i),{index,clip:next})){renderClips();return;}
    clipItems[index]=next;
    clipIndex=index;
    changeClips();
    seekTimeline(clipOffset(index)+(edge==='end'?Math.max(0,next.end-next.start-0.034):0));
}
$('#production-source-preview').addEventListener('timeupdate',()=>{if(clipPlaying&&clipItems.length){const video=$('#production-source-preview'),current=clipItems[clipIndex];if(video.currentTime>=current.end-0.025){if(clipIndex+1<clipItems.length){clipIndex++;video.currentTime=clipItems[clipIndex].start;video.play().catch(()=>{clipPlaying=false;});}else{stopClipPreview();video.currentTime=current.end;}}}updateClipClock();});
$('#production-source-preview').addEventListener('seeking',()=>{if(!clipItems.length||clipPlaying||timelineDrag?.edge)return;const time=$('#production-source-preview').currentTime,current=clipItems[clipIndex];if(!current||time<current.start||time>current.end){const i=clipItems.findIndex(c=>time>=c.start&&time<c.end);if(i>=0)clipIndex=i;}updateClipClock();});
$('#production-source-preview').addEventListener('pause',()=>{clipPlaying=false;});

// Snapshots use the same model as the save payload, including drafts that cannot yet be saved.
const editForm=$('#production-form');
const editUndo=$('#production-undo'),editRedo=$('#production-redo');
const editHistory={current:null,past:[],future:[]};

function editFields(){
    return [...editForm.querySelectorAll('input[name],textarea[name],select[name]')]
        .filter(input=>input.name!=='save_template');
}

function captureEdit(){
    const fields=editFields().map(input=>input.matches('[type=checkbox],[type=radio]')?input.checked:input.value);
    return JSON.stringify({fields,clips:clipItems,cues:cueItems,overlays:overlayItems,blurs:blurBoxes,audioMix});
}

function syncEditButtons(pending=false){
    const locked=$('#production-fields').disabled;
    editUndo.disabled=$('#cue-undo').disabled=locked||(!editHistory.past.length&&!pending);
    editRedo.disabled=$('#cue-redo').disabled=locked||pending||!editHistory.future.length;
}

function rebaseEditHistory(sameProject,keepHistory){
    const current=captureEdit();
    if(!sameProject||!editHistory.current||(!keepHistory&&current!==editHistory.current)){
        editHistory.past=[];
        editHistory.future=[];
    }
    editHistory.current=current;
    syncEditButtons();
}

function recordEdit(){
    if(!editHistory.current)return;
    const current=captureEdit();
    if(current!==editHistory.current){
        editHistory.past.push(editHistory.current);
        if(editHistory.past.length>100)editHistory.past.shift();
        editHistory.current=current;
        editHistory.future=[];
    }
    syncEditButtons();
}

function stepEdit(redo){
    if(workflowBusy||$('#production-fields').disabled||activeEditPointers.size)return;
    recordEdit();
    const from=redo?editHistory.future:editHistory.past;
    const to=redo?editHistory.past:editHistory.future;
    if(!from.length)return;
    to.push(editHistory.current);
    editHistory.current=from.pop();
    applyEditSnapshot(JSON.parse(editHistory.current),mixVideo.hidden?cueTime():mixVideo.currentTime);
    syncEditButtons();
}

function applyEditSnapshot(snapshot,time){
    const fields=editFields();
    if(fields.length!==snapshot.fields.length)return;
    stopClipPreview();
    fields.forEach((input,index)=>{
        if(input.matches('[type=checkbox],[type=radio]'))input.checked=snapshot.fields[index];
        else input.value=snapshot.fields[index];
    });
    clipItems=snapshot.clips;
    const selection=cueItems.flatMap((cue,i)=>selectedCues.has(cue)?[i]:[]);
    selectedCues=new Set(selection.map(i=>snapshot.cues[i]).filter(Boolean));
    cueItems=snapshot.cues;
    overlayItems=snapshot.overlays;
    blurBoxes=snapshot.blurs;
    audioMix=snapshot.audioMix??null;
    renderBlurs();
    renderCues();
    renderOverlayEditor();
    renderClips();
    renderAudioMixer();
    restorePreviewTime(time);
    $('#preview-frame').hidden=true;
    editForm.dispatchEvent(new Event('input',{bubbles:true}));
    if(editForm.elements.script.value!==currentProduction()?.document.script){
        $('#script-status').textContent='대본을 수정했습니다. 현재 대본을 검토하고 다시 승인하세요.';
        $('#script-sentences').replaceChildren();
    }
    syncEditButtons();
}

function isEditField(input){
    return input.matches('input,textarea,select')&&
        !input.closest('.analysis-panel,#cue-editor,#caption-drafts,[data-cue-select]')&&
        !input.matches('#clip-playhead,[type=file],[name=save_template]');
}

const activeEditPointers=new Set();
const productionDialog=$('#production-dialog');
productionDialog.addEventListener('pointerdown',event=>{
    if(event.target.closest('#clip-picture,.preview-overlay,.blur-box,[data-timeline-drag],[data-audio-drag],input[type=range]')){
        activeEditPointers.add(event.pointerId);
    }
});
for(const type of ['pointerup','pointercancel']){
    window.addEventListener(type,event=>activeEditPointers.delete(event.pointerId));
}
productionDialog.addEventListener('close',()=>activeEditPointers.clear());
editForm.addEventListener('input',event=>{
    if(isEditField(event.target))syncEditButtons(captureEdit()!==editHistory.current);
});
editForm.addEventListener('change',event=>{
    if(isEditField(event.target))recordEdit();
});
editUndo.onclick=()=>stepEdit(false);
editRedo.onclick=()=>stepEdit(true);
document.addEventListener('keydown',event=>{
    if(!productionDialog.open||event.defaultPrevented||event.isComposing||event.altKey||
       !(event.ctrlKey||event.metaKey)||$('#production-fields').disabled)return;
    const target=event.target;
    if(target.isContentEditable||target.closest('textarea,input:is(:not([type]),[type=text],[type=search],[type=url],[type=email],[type=tel],[type=password],[type=number])'))return;
    const key=event.key.toLowerCase();
    if(key!=='z'&&!(key==='y'&&event.ctrlKey&&!event.shiftKey))return;
    event.preventDefault();
    stepEdit(key==='y'||event.shiftKey);
});

// Draft writes use a separate server endpoint and are ordered per project.
const recoveryDrafts=new Map();
const recoveryWrites=new Map();

async function loadRecoveryDrafts(){
    try{
        for(const draft of await request('/api/production/recovery')){
            if(!recoveryDrafts.has(draft.id))recoveryDrafts.set(draft.id,draft);
        }
    }catch{message('임시 저장본 목록을 불러오지 못했습니다.');}
}

function persistRecovery(id,draft,expected=null){
    const payload=draft||{id,revision:expected.revision,discard:true};
    const previous=recoveryWrites.get(id)||Promise.resolve();
    const write=previous.catch(()=>{}).then(()=>request('/api/production/recovery',payload));
    recoveryWrites.set(id,write);
    return write.finally(()=>{
        if(recoveryWrites.get(id)===write)recoveryWrites.delete(id);
    });
}
const recoveryNotice=$('#production-recovery');
const recoveryText=$('#production-recovery-text');
let savedRecoveryEdit=null, recoveryBase=null, ownedRecovery=null;
let applyingRecovery=false, pendingPreviewTime=null;

function restorePreviewTime(time){
    if(!currentProduction()?.document.source||!Number.isFinite(time))return;
    if(overlayVideo.readyState)seekOutputTime(Math.min(time,overlayDuration()));
    else pendingPreviewTime=time;
}

function readRecovery(project){
    if(!project)return null;
    const draft=recoveryDrafts.get(project.id);
    return draft?.source===(project.document.source?.filename||null)?draft:null;
}

function showRecovery(text,canDiscard=false){
    recoveryText.textContent=text;
    recoveryNotice.hidden=!text;
    $('#production-recovery-discard').hidden=!canDiscard;
}

function resetRecovery(){
    savedRecoveryEdit=captureEdit();
    recoveryBase=currentProduction().updated_at;
    ownedRecovery=null;
    pendingPreviewTime=null;
    showRecovery('');
}

function recoveryWriteFailed(id){
    if(activeProject===id)showRecovery('임시 저장에 실패했습니다. 변경사항을 직접 저장해 주세요.');
}

function removeRecovery(draft){
    if(!draft||recoveryDrafts.get(draft.id)!==draft)return;
    const persisted=persistRecovery(draft.id,null,draft);
    recoveryDrafts.delete(draft.id);
    return persisted.catch(error=>{
        if(!recoveryDrafts.has(draft.id))recoveryDrafts.set(draft.id,draft);
        throw error;
    });
}

function writeRecovery(){
    const project=currentProduction();
    if(applyingRecovery||!productionDialog.open||!project||$('#production-fields').disabled)return;
    const edit=captureEdit();
    const analysis=analysisDirty?readAnalysisEditor():null;
    try{
        const existing=readRecovery(project);
        if(edit===savedRecoveryEdit&&!analysis){
            if(existing&&existing===ownedRecovery)removeRecovery(existing)?.catch(()=>recoveryWriteFailed(project.id));
            ownedRecovery=null;
            showRecovery('');
            return;
        }
        const time=Math.round(cueTime()*100)/100;
        if(existing?.edit===edit&&JSON.stringify(existing.analysis)===JSON.stringify(analysis)&&
            existing.time===time&&existing.base===recoveryBase)return;
        const draft={
            id:project.id,source:project.document.source?.filename||null,base:recoveryBase,
            edit,analysis,time,at:Date.now(),revision:crypto.randomUUID()
        };
        const persisted=persistRecovery(project.id,draft);
        recoveryDrafts.set(project.id,draft);
        ownedRecovery=draft;
        showRecovery('변경사항을 임시 저장하는 중입니다.',true);
        persisted.then(()=>{
            if(productionDialog.open&&ownedRecovery===draft)showRecovery('변경사항을 임시 저장했습니다.',true);
        }).catch(()=>{
            if(recoveryDrafts.get(project.id)!==draft)return;
            recoveryDrafts.delete(project.id);
            recoveryWriteFailed(project.id);
        });
    }catch{recoveryWriteFailed(project.id);}
}

function restoreRecovery(){
    const project=currentProduction();
    const draft=readRecovery(project);
    if(!draft||project.status==='rendering')return;
    try{
        const edit=JSON.parse(draft.edit);
        if(edit.fields.length!==editFields().length)return;
        applyingRecovery=true;
        ownedRecovery=draft;
        recoveryBase=draft.base;
        if(draft.edit!==savedRecoveryEdit){
            applyEditSnapshot(edit,draft.time);
            recordEdit();
        }else restorePreviewTime(draft.time);
        if(draft.analysis){
            $('#analysis-scenes').innerHTML=draft.analysis.scenes.map(sceneEditor).join('');
            markAnalysisDirty();
        }
        showRecovery(draft.base===project.updated_at?'임시 저장한 작업을 복구했습니다.':
            '이 복구본을 만든 뒤 프로젝트가 저장되었습니다. 복구한 내용을 확인해 주세요.',true);
    }catch{
        showRecovery('임시 저장본을 읽을 수 없습니다.',true);
    }finally{applyingRecovery=false;}
}

async function completeRecoverySave(id,savedEdit,analysis){
    const project=productionState.projects.find(p=>p.id===id);
    if(!project)return;
    const draft=readRecovery(project);
    if(draft?.edit===savedEdit&&JSON.stringify(draft.analysis)===JSON.stringify(analysis)){
        await removeRecovery(draft);
    }
    if(activeProject===id){
        fillProduction(id,true);
        if(productionDialog.open)restoreRecovery();
    }
}

function openLatestRecovery(){
    if(document.querySelector('dialog[open]'))return;
    let latest=null;
    for(const project of productionState.projects){
        if(project.status==='rendering')continue;
        const draft=readRecovery(project);
        if(draft&&(!latest||draft.at>latest.at))latest={id:project.id,at:draft.at};
    }
    if(latest){
        switchView('projects');
        openProduction(latest.id);
    }
}

$('#production-recovery-discard').onclick=async()=>{
    if(!confirm('임시 저장한 편집을 버리고 마지막으로 저장한 프로젝트로 돌아갈까요?'))return;
    const id=activeProject;
    try{
        await removeRecovery(ownedRecovery);
        if(activeProject===id){
            fillProduction(id);
            if(productionDialog.open)restoreRecovery();
        }
    }catch{showRecovery('임시 저장본을 지울 수 없습니다.',true);}
};
editForm.addEventListener('input',writeRecovery);
editForm.addEventListener('change',writeRecovery);
overlayVideo.addEventListener('seeked',()=>{if(!clipPlaying&&ownedRecovery)writeRecovery();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)writeRecovery();});

// Uploaded clips live in output time. Source audio uses the current picture edit.
const mixerTracks={narration:'나레이션',music:'배경음악'};
const mixerPanel=$('#audio-mixer'),mixVideo=$('#mix-preview-video'),mixStatus=$('#mix-preview-status');
let audioMix=null,mixerSession=0,mixRequest=null,mixUrl='';
const pendingAudio=new Set();

function defaultAudioMix(){
    return {source_volume:100,narration:{volume:100,muted:false},music:{volume:35,muted:false},duck:{enabled:false,reduction:75}};
}

function invalidateMixPreview(){
    mixRequest?.abort();
    mixRequest=null;
    mixVideo.pause();
    if(mixUrl){
        mixVideo.removeAttribute('src');
        mixVideo.load();
        URL.revokeObjectURL(mixUrl);
        mixUrl='';
    }
    mixVideo.hidden=true;
    mixStatus.textContent='';
    mixerPanel.style.removeProperty('--mix-playhead');
}

function loadAudioMixer(doc){
    mixerSession++;
    pendingAudio.clear();
    invalidateMixPreview();
    audioMix=doc.audio_mix?structuredClone(doc.audio_mix):null;
    renderAudioMixer();
}

function mixerVoice(){
    const doc=currentProduction()?.document;
    if(clipItems.length||audioMix?.narration.clip||!doc?.narration)return null;
    if($('#production-form').elements.script.value!==doc.script)return null;
    if(doc.approved_hash&&doc.narration.approved_hash===doc.approved_hash||doc.narration.mixer_base_hash)return doc.narration;
    return null;
}

function mixerDuration(){
    const form=$('#production-form'),source=currentProduction()?.document.source;
    if(!source)return 0;
    return clipItems.length?clipTotal():mixerVoice()?.duration||
        Math.max(0,Math.min(Number(form.elements.duration.value),source.duration-Number(form.elements.start.value)));
}

function syncSourceVolume(volume){
    $('#source-volume-value').textContent=volume+'%';
    overlayVideo.volume=volume/100;
}

function renderAudioMixer(){
    const mix=audioMix||defaultAudioMix();
    const sourceVolume=mixerPanel.querySelector('[data-mixer-track="source"]');
    sourceVolume.value=mix.source_volume;
    syncSourceVolume(mix.source_volume);
    mixerPanel.querySelector('[data-duck="enabled"]').checked=mix.duck.enabled;
    mixerPanel.querySelector('[data-duck="reduction"]').value=mix.duck.reduction;
    $('#audio-tracks').innerHTML=Object.entries(mixerTracks).map(([key,title])=>{
        const track=mix[key],clip=track.clip;
        const numberField=(field,label,value,max)=>`<label>${label}<input data-mixer-track="${key}" data-mixer-field="${field}" type="number" min="0" max="${max}" step="any" value="${value}" required></label>`;
        return `<div class="mixer-row"><div class="mixer-controls"><b>${title}</b>
        ${numberField('volume','볼륨 (%)',track.volume,100)}
        <label class="check"><input data-mixer-track="${key}" data-mixer-field="muted" type="checkbox" ${track.muted?'checked':''}>음소거</label>
        <label class="mixer-file">${clip?'파일 바꾸기':'파일 추가'}<input data-audio-file="${key}" type="file" accept="audio/*,.m4a,.aac,.opus" ${pendingAudio.has(key)?'disabled':''}></label></div>
        <div class="mixer-lane" data-mixer-lane="${key}"></div>
        ${clip?`<div class="mixer-controls"><span class="mixer-filename">${esc(clip.name)}</span>
        ${numberField('at','배치 (초)',clip.at,60)}
        ${numberField('in','파일 시작 (초)',clip.in,clip.duration)}
        ${numberField('out','파일 끝 (초)',clip.out,clip.duration)}
        <button type="button" data-audio-remove="${key}">제거</button></div>`:''}</div>`;
    }).join('');
    renderAudioLanes();
}

function renderAudioLanes(){
    const duration=mixerDuration(), doc=currentProduction()?.document;
    const ruler=mixerPanel.querySelector('.mixer-ruler');
    ruler.innerHTML=Array.from({length:5},(_,i)=>`<span>${Number((duration*i/4).toFixed(2))}초</span>`).join('');
    $('#mix-preview-play').disabled=!duration;
    if(!clipItems.length){
        $('#clip-playhead').disabled=!duration;
        $('#clip-playhead').max=duration||60;
    }
    function bar(at,length,label,editable=false){
        if(!duration||at>=duration||length<=0)return '';
        const visible=Math.min(length,duration-at);
        return `<div class="mixer-bar" style="left:${at/duration*100}%;width:${visible/duration*100}%" title="${esc(label)}"${editable?' data-audio-drag="move"':''}>
        ${editable?'<i data-audio-drag="in"></i>':''}<span>${esc(label)}</span>${editable?'<i data-audio-drag="out"></i>':''}</div>`;
    }
    let offset=0;
    const voice=mixerVoice();
    const sourceSpans=clipItems.length?clipItems.map(c=>c.end-c.start):
        voice?voice.segments.map(s=>s.end-s.start):[duration];
    mixerPanel.querySelector('[data-mixer-lane="source"]').innerHTML=doc?.source?.audio?
        sourceSpans.map((length,i)=>{const html=bar(offset,length,`원음 ${i+1}`);offset+=length;return html;}).join(''):'';
    for(const key of Object.keys(mixerTracks)){
        const clip=audioMix?.[key].clip;
        mixerPanel.querySelector(`[data-mixer-lane="${key}"]`).innerHTML=clip?
            bar(clip.at,clip.out-clip.in,clip.name,true):key==='narration'&&voice?bar(0,voice.duration,'생성된 나레이션'):'';
    }
}

mixerPanel.addEventListener('input',event=>{
    const input=event.target, {mixerTrack:key,mixerField:field,duck}=input.dataset;
    if(!key&&!duck)return;
    input.setCustomValidity('');
    const value=input.type==='checkbox'?input.checked:input.valueAsNumber;
    if(input.type==='number'&&(!input.validity.valid||!Number.isFinite(value)))return;
    const mix=audioMix||defaultAudioMix();
    const clip=mix[key]?.clip;
    if(clip&&(field==='in'&&value>=clip.out||field==='out'&&value<=clip.in)){
        input.setCustomValidity('파일 끝은 시작보다 뒤여야 합니다.');
        return;
    }
    audioMix=mix;
    if(duck)mix.duck[duck]=value;
    else if(key==='source'){
        mix.source_volume=value;
        syncSourceVolume(value);
    }
    else if(field==='volume'||field==='muted')mix[key][field]=value;
    else clip[field]=value;
    renderAudioLanes();
});

mixerPanel.addEventListener('change',async event=>{
    const input=event.target,key=input.dataset.audioFile,file=input.files?.[0];
    if(!key||!file||pendingAudio.has(key))return;
    const project=activeProject,session=mixerSession;
    pendingAudio.add(key);
    input.disabled=true;
    mixStatus.textContent='오디오 파일을 확인하는 중…';
    try{
        const response=await fetch(`/api/production/audio-upload?id=${encodeURIComponent(project)}`,{method:'POST',body:file});
        const data=await response.json();
        if(!response.ok)throw Error(data.error||'오디오를 추가할 수 없습니다.');
        if(session!==mixerSession||!$('#production-dialog').open)return;
        pendingAudio.delete(key);
        audioMix??=defaultAudioMix();
        audioMix[key].clip={...data,name:file.name.slice(0,160),at:0,in:0,out:data.duration};
        renderAudioMixer();
        markDirty();
    }catch(error){
        if(session===mixerSession)mixStatus.textContent=error.message;
    }finally{
        if(session===mixerSession){
            pendingAudio.delete(key);
            const current=mixerPanel.querySelector(`[data-audio-file="${key}"]`);
            current.disabled=false;
            current.value='';
        }
    }
});

mixerPanel.addEventListener('click',event=>{
    const key=event.target.closest('[data-audio-remove]')?.dataset.audioRemove;
    if(!key)return;
    delete audioMix[key].clip;
    renderAudioMixer();
    markDirty();
});

mixerPanel.addEventListener('pointerdown',event=>{
    const handle=event.target.closest('[data-audio-drag]');
    if(!handle||event.button!==0||$('#production-fields').disabled)return;
    const lane=handle.closest('[data-mixer-lane]'),key=lane.dataset.mixerLane;
    const clip=audioMix[key].clip,initial={...clip},mode=handle.dataset.audioDrag;
    const duration=mixerDuration(),secondsPerPixel=duration/lane.clientWidth;
    const visibleEnd=Math.min(initial.out,initial.in+duration-initial.at);
    const x=event.clientX,pointer=event.pointerId;
    const bounded=(value,min,max)=>Math.round(Math.max(min,Math.min(max,value))*1000)/1000;
    event.preventDefault();
    lane.setPointerCapture(pointer);
    const move=e=>{
        if(e.pointerId!==pointer)return;
        const delta=(e.clientX-x)*secondsPerPixel;
        if(mode==='move')clip.at=bounded(initial.at+delta,0,Math.max(0,mixerDuration()-0.001));
        if(mode==='in'){
            const shift=bounded(delta,Math.max(-initial.at,-initial.in),initial.out-initial.in-0.001);
            clip.in=bounded(initial.in+shift,0,initial.out-0.001);
            clip.at=bounded(initial.at+shift,0,60);
        }
        if(mode==='out'&&delta!==0)clip.out=bounded(visibleEnd+delta,initial.in+0.001,initial.duration);
        renderAudioLanes();
    };
    lane.addEventListener('pointermove',move);
    lane.addEventListener('lostpointercapture',()=>{
        lane.removeEventListener('pointermove',move);
        renderAudioMixer();
        if(['at','in','out'].some(key=>clip[key]!==initial[key]))markDirty();
    },{once:true});
});

async function playMixPreview(){
    const button=$('#mix-preview-play');
    if(button.disabled||!$('#production-form').reportValidity())return;
    if(gradeBefore){
        gradeBefore=false;
        syncGradeFields({...gradeDefaults,...clipItems[clipIndex]?.grade});
        previewGrade(clipItems[clipIndex]);
    }
    if(pendingAudio.size){
        mixStatus.textContent='오디오 업로드가 끝난 뒤 재생하세요.';
        return;
    }
    if(mixUrl){
        mixVideo.currentTime=0;
        await mixVideo.play().catch(()=>{});
        return;
    }
    invalidateMixPreview();
    stopClipPreview();
    const controller=new AbortController();
    mixRequest=controller;
    mixStatus.textContent='오디오 믹스 미리보기를 만드는 중…';
    try{
        const {id,script,settings,clips,audio_mix}=productionPayload();
        const response=await fetch('/api/production/mix-preview',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({id,script,settings,clips,audio_mix}),signal:controller.signal
        });
        if(!response.ok)throw Error((await response.json()).error||'미리보기를 만들 수 없습니다.');
        const blob=await response.blob();
        if(controller.signal.aborted)return;
        mixUrl=URL.createObjectURL(blob);
        mixVideo.src=mixUrl;
        mixVideo.hidden=false;
        mixStatus.textContent='오디오 믹스 미리보기';
        await mixVideo.play().catch(()=>{});
    }catch(error){
        if(!controller.signal.aborted)mixStatus.textContent=error.message;
    }finally{
        if(mixRequest===controller)mixRequest=null;
    }
}

$('#mix-preview-play').onclick=playMixPreview;
mixVideo.addEventListener('play',stopClipPreview);
$('#production-source-preview').addEventListener('play',()=>mixVideo.pause());
mixVideo.addEventListener('timeupdate',()=>{
    if(mixVideo.hidden)return;
    const time=mixVideo.currentTime;
    $('#clip-clock').textContent=time.toFixed(2)+'초';
    $('#clip-playhead').value=time;
    updateOutputPlayhead();
    mixerPanel.style.setProperty('--mix-playhead',`${100*time/mixVideo.duration}%`);
});
$('#production-dialog').addEventListener('close',()=>{
    mixerSession++;
    invalidateMixPreview();
});

const outputTimeline=$('#output-timeline-grid');
let timelineMarkup='', timelineDrag=null;
const trimPreview=$('#timeline-trim-preview');
const waveformCache=new Map(),waveformPending=new Set();
$('#timeline-zoom').onchange=event=>{
    const scroll=outputTimeline.parentElement;
    const position=scroll.scrollLeft/Math.max(1,outputTimeline.offsetWidth);
    outputTimeline.style.width=100*Number(event.target.value)+'%';
    scroll.scrollLeft=position*outputTimeline.offsetWidth;
};

function waveformURL(kind,start,length,version=''){
    return '/api/production/waveform?'+new URLSearchParams({
        id:activeProject,kind,start:start.toFixed(3),duration:length.toFixed(3),v:version
    });
}

function drawWaveforms(){
    for(const svg of outputTimeline.querySelectorAll('[data-waveform]')){
        const url=svg.dataset.waveform,cached=waveformCache.get(url);
        if(cached){
            if(cached.path&&svg.dataset.loaded!=='1'){
                svg.querySelector('path').setAttribute('d',cached.path);
                svg.dataset.loaded='1';
            }
            continue;
        }
        if(waveformPending.has(url)||waveformPending.size>=2)continue;
        waveformPending.add(url);
        fetch(url).then(async response=>{
            if(!response.ok)throw Error('파형을 읽을 수 없습니다.');
            const {peaks,step,duration}=await response.json();
            const path=peaks.map(([low,high],i)=>{
                const x=Math.min(1000,1000*(i+.5)*step/duration).toFixed(2);
                return `M${x} ${(10-Math.min(9,high*18)).toFixed(2)}V${(10-Math.max(-9,low*18)).toFixed(2)}`;
            }).join('');
            waveformCache.set(url,{path});
        }).catch(()=>waveformCache.set(url,{})).finally(()=>{
            waveformPending.delete(url);
            drawWaveforms();
        });
    }
    // Only evict entries for ranges no longer in this timeline.
    if(waveformCache.size>128){
        const current=new Set([...outputTimeline.querySelectorAll('[data-waveform]')].map(svg=>svg.dataset.waveform));
        for(const url of waveformCache.keys())if(!current.has(url))waveformCache.delete(url);
    }
}

function automaticCaptionSpans(duration,voice){
    const ranges=voice?.segments||[{start:0,end:duration,text:editForm.elements.script.value}];
    return ranges.flatMap(range=>{
        const chunks=captionChunks(range.text);
        const step=(range.end-range.start)/chunks.length;
        return chunks.map((lines,i)=>({start:range.start+i*step,end:range.start+(i+1)*step,lines}));
    });
}

function outputPreviewTime(){
    if(!mixVideo.hidden)return mixVideo.currentTime;
    return mixerVoice()?Number(overlayTime.value):cueTime();
}

function updateOutputPlayhead(){
    const duration=mixerDuration();
    const time=Math.min(outputPreviewTime(),duration);
    outputTimeline.style.setProperty('--timeline-time',`${duration?100*time/duration:0}%`);
    const clock=outputTimeline.querySelector('output');
    if(clock)clock.value=time.toFixed(2)+'초';
}

function renderOutputTimeline(){
    const duration=mixerDuration();
    if(timelineDrag)return;
    const doc=currentProduction()?.document;
    const locked=$('#production-fields').disabled;
    const voice=mixerVoice();
    const audio=audioMix||defaultAudioMix();
    function row(name,bars,key=''){
        const levels=[];
        const segments=bars.filter(b=>b.start<duration&&b.end>b.start).sort((a,b)=>a.start-b.start).map(bar=>{
            const end=Math.min(bar.end,duration);
            let level=levels.findIndex(last=>last<=bar.start+0.000001);
            if(level<0)level=levels.length;
            levels[level]=end;
            const label=`${bar.label} · ${bar.start.toFixed(2)}–${end.toFixed(2)}초`;
            const drag=bar.index===undefined||locked?'':` data-timeline-drag="${key}" data-index="${bar.index}"`;
            const trim=key==='video'&&drag?`<span class="timeline-edge" data-trim-edge="start" title="시작 트리밍"></span><span class="timeline-edge" data-trim-edge="end" title="끝 트리밍"></span>`:'';
            const waveLength=Math.min(end-bar.start,bar.wave?.length??0);
            const wave=waveLength>=0.001?`<svg class="timeline-wave" aria-hidden="true" viewBox="0 0 1000 20" preserveAspectRatio="none"
                style="width:${100*waveLength/(end-bar.start)}%" data-waveform="${esc(waveformURL(bar.wave.kind,bar.wave.start,waveLength,bar.wave.version))}"><path/></svg>`:'';
            return `<button type="button" class="timeline-bar ${bar.quiet?'quiet':''}" data-time="${bar.start}"${drag}
                style="left:${100*bar.start/duration}%;width:${100*(end-bar.start)/duration}%;top:${level*25+2}px"
                title="${esc(label)}" aria-label="${esc(label)}">${wave}<span class="timeline-wave-label">${esc(bar.label)}</span>${trim}</button>`;
        });
        return `<span class="timeline-label">${name}</span><div class="timeline-lane lane-${key}" style="height:${Math.max(1,levels.length)*25+4}px">${segments.join('')}</div>`;
    }
    const video=clipItems.length?clipItems.map((clip,index)=>({
        start:clipOffset(index),end:clipOffset(index+1),label:`클립 ${index+1} · 원본 ${clip.start}–${clip.end}초`,index
    })):voice?voice.segments.map((segment,i)=>({...segment,label:`장면 ${i+1}`})):
        [{start:0,end:duration,label:'원본 선택 구간'}];
    const overlays=overlayItems.map((item,index)=>({
        start:item.start,end:item.end||duration,label:item.text,index
    })).filter(item=>item.label.trim());
    const cues=cueItems.filter(cue=>cue.text.trim()).sort((a,b)=>a.start-b.start);
    const captions=!editForm.elements.captions.checked?[]:cues.length?cues.map((cue,i)=>({
        start:cue.start,end:cueEnd(cue,i,cues,duration),label:cue.text.replaceAll('*','')
    })):automaticCaptionSpans(duration,voice).map(span=>({...span,label:span.lines.flat().map(([word])=>word).join(' ')}));
    const marks=Array.from({length:5},(_,i)=>{
        const time=duration*i/4;
        return `<button type="button" data-time="${time}" style="left:${i*25}%" aria-label="${Number(time.toFixed(2))}초로 이동">${Number(time.toFixed(2))}</button>`;
    }).join('');
    const sourceBars=doc?.source?.audio?video.map((bar,i)=>{
        const source=clipItems[i]||(voice?doc.analysis?.scenes.find(scene=>scene.id===voice.segments[i].scene_id):null);
        const start=source?.start??Number(editForm.elements.start.value);
        return {start:bar.start,end:bar.end,label:`원음 ${i+1}`,
            quiet:editForm.elements.mute.checked||!audio.source_volume,
            wave:{kind:'source',start,length:source?source.end-source.start:bar.end-bar.start,version:doc.source.filename}};
    }):[];
    const markup=duration?`<output aria-label="완성 영상 시점"></output><div class="timeline-ruler">${marks}</div>
        ${row('영상',video,'video')}${row('글귀',overlays,'overlay')}${row('자막',captions,'caption')}
        ${row('원음',sourceBars,'audio')}
        ${Object.entries({narration:'나레이션',music:'음악'}).map(([key,name])=>{
            const track=audio[key], clip=track.clip, quiet=track.muted||!track.volume;
            const bars=clip?[{start:clip.at,end:clip.at+clip.out-clip.in,label:clip.name||name,quiet,
                wave:{kind:clip.id,start:clip.in,length:clip.out-clip.in}}]:
                key==='narration'&&voice?[{start:0,end:voice.duration,label:'생성된 나레이션',quiet,
                    wave:{kind:'narration',start:0,length:voice.duration,version:voice.audio_file}}]:[];
            return row(name,bars,'audio');
        }).join('')}<div class="timeline-playhead" aria-hidden="true"></div>`:
        '<p class="hint timeline-empty">원본 영상을 올리면 트랙이 표시됩니다.</p>';
    if(markup!==timelineMarkup){
        outputTimeline.innerHTML=markup;
        timelineMarkup=markup;
        drawWaveforms();
    }
    updateOutputPlayhead();
}

function seekTimeline(time){
    const target=Math.max(0,Math.min(time,mixerDuration()));
    stopClipPreview();
    $('#preview-frame').hidden=true;
    if(!mixVideo.hidden&&Number.isFinite(mixVideo.duration)){
        mixVideo.pause();
        mixVideo.currentTime=Math.min(target,mixVideo.duration);
    }
    if(!overlayVideo.readyState)pendingPreviewTime=target;
    seekOverlayPreview(target);
    stylePreview();
}

outputTimeline.addEventListener('click',event=>{
    if(event.target.closest('[data-trim-edge]'))return;
    const button=event.target.closest('[data-time]');
    if(button){seekTimeline(Number(button.dataset.time));return;}
    const ruler=event.target.closest('.timeline-ruler');
    if(ruler){
        const rect=ruler.getBoundingClientRect();
        seekTimeline((event.clientX-rect.left)/rect.width*mixerDuration());
    }
});

outputTimeline.addEventListener('pointerdown',event=>{
    const bar=event.target.closest('[data-timeline-drag]');
    if(!bar||!event.isPrimary||event.button!==0||workflowBusy||timelineDrag)return;
    const rect=bar.parentElement.getBoundingClientRect();
    const edge=event.target.closest('[data-trim-edge]')?.dataset.trimEdge;
    timelineDrag={bar,key:bar.dataset.timelineDrag,index:Number(bar.dataset.index),
        startX:event.clientX,width:rect.width,duration:mixerDuration(),id:event.pointerId,edge};
    if(edge)event.preventDefault();
});
outputTimeline.addEventListener('pointermove',event=>{
    const drag=timelineDrag;
    if(!drag||drag.id!==event.pointerId)return;
    const dx=event.clientX-drag.startX;
    if(!drag.moved&&Math.abs(dx)<4)return;
    if(!drag.moved){
        drag.moved=true;
        outputTimeline.setPointerCapture(event.pointerId);
        stopClipPreview();
        mixVideo.pause();
        drag.bar.classList.add('moving');
        if(drag.edge){
            recordEdit();
            invalidateMixPreview();
            $('#preview-frame').hidden=true;
            drag.left=clipOffset(drag.index);
        }
    }
    if(drag.edge){
        const clip=clipItems[drag.index],oldLength=clip.end-clip.start;
        const source=currentProduction().document.source.duration;
        const maxLength=60-drag.duration+oldLength;
        const edge=drag.edge;
        const low=edge==='start'?Math.max(0,clip.end-maxLength):clip.start+0.1;
        const high=edge==='start'?clip.end-0.1:Math.min(source,clip.start+maxLength);
        const value=Math.round(Math.max(low,Math.min(high,clip[edge]+dx/drag.width*drag.duration))*1000)/1000;
        drag.next={...clip,[edge]:value};
        const length=drag.next.end-drag.next.start;
        drag.bar.style.left=`${100*(drag.left+(edge==='start'?value-clip.start:0))/drag.duration}%`;
        drag.bar.style.width=`${100*length/drag.duration}%`;
        clipIndex=drag.index;
        if(overlayVideo.readyState)overlayVideo.currentTime=edge==='start'?value:Math.max(clip.start,value-0.034);
        trimPreview.hidden=false;
        trimPreview.textContent=`원본 ${value.toFixed(2)}초 · 클립 ${length.toFixed(2)}초 · 전체 ${(drag.duration-oldLength+length).toFixed(2)}초`;
        return;
    }
    drag.bar.style.translate=`${dx}px`;
});
function finishTimelineDrag(event,commit){
    const drag=timelineDrag;
    if(!drag||drag.id!==event.pointerId)return;
    timelineDrag=null;
    drag.bar.classList.remove('moving');
    drag.bar.style.translate='';
    if(drag.edge){
        trimPreview.hidden=true;
        timelineMarkup='';
        if(commit&&drag.next&&!$('#production-fields').disabled&&!workflowBusy){
            commitClipTrim(drag.index,drag.next,drag.edge);
        }else if(drag.moved){
            seekTimeline(clipOffset(drag.index));
        }
        renderOutputTimeline();
        return;
    }
    if(!commit||!drag.moved||$('#production-fields').disabled||workflowBusy)return;
    const delta=(event.clientX-drag.startX)/drag.width*drag.duration;
    if(drag.key==='video'){
        const center=(clipOffset(drag.index)+clipOffset(drag.index+1))/2+delta;
        const order=clipItems.map((_,i)=>i).filter(i=>i!==drag.index);
        const target=order.filter(i=>center>(clipOffset(i)+clipOffset(i+1))/2).length;
        if(target===drag.index)return;
        order.splice(target,0,drag.index);
        applyClipOrder(order,target);
        return;
    }
    const item=overlayItems[drag.index];
    const length=Math.round((Math.min(item.end||drag.duration,drag.duration)-item.start)*100);
    if(length<=0)return;
    const limit=Math.floor(drag.duration*100+0.000001);
    const start=Math.max(0,Math.min(Math.round((item.start+delta)*100),limit-length));
    if(start/100===item.start)return;
    item.start=start/100;
    item.end=!item.end&&start+length===limit?0:(start+length)/100;
    renderOverlayEditor();
    seekTimeline(item.start);
    markDirty();
}
window.addEventListener('pointerup',event=>finishTimelineDrag(event,true));
window.addEventListener('pointercancel',event=>finishTimelineDrag(event,false));
outputTimeline.addEventListener('lostpointercapture',event=>finishTimelineDrag(event,false));

let shareImportRightsEdited=false,shareImportProject=null;
function shareImportPending(project){return ['queued','running'].includes(project?.document?.share_import?.status);}
function safeShareLink(value){try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)?url.href:null;}catch{return null;}}
function renderShareImport(project,fillRights=true){
  if(shareImportProject!==project.id){shareImportProject=project.id;shareImportRightsEdited=false;}
  const info=project.document.share_import,panel=$('#share-import-panel');panel.hidden=!info;
  if(!info)return;
  const labels={pending:'공유 링크 저장됨',queued:'가져오기 대기',running:'원본 가져오는 중',imported:'원본 가져오기 완료',manual:'직접 올린 원본 사용',error:'가져오기 실패',unsupported:'자동 가져오기 미지원',metadata_only:'영상 정보만 가져옴'};
  $('#share-import-status').textContent=(labels[info.status]||info.status||'상태 확인 중')+(info.message?' · '+info.message:'');
  const platform={Bilibili:'빌리빌리',Douyin:'도우인',Xiaohongshu:'샤오홍슈',Kuaishou:'콰이쇼우'}[info.platform]||info.platform;
  $('#share-import-details').textContent=[platform,info.title,info.author||info.author_id].filter(Boolean).join(' · ');
  $('#share-import-links').innerHTML=[['공유 링크',info.share_url],['원본 링크',info.original_url]].map(([label,url])=>{const href=safeShareLink(url);return href?`<a class="outline" target="_blank" rel="noopener noreferrer" href="${esc(href)}">${label}</a>`:'';}).join('');
  if(fillRights&&!shareImportRightsEdited)$('#share-import-rights').value=info.rights_note||'';
  $('#share-import-retry').disabled=shareImportPending(project)||['rendering','queued'].includes(project.status);
}
$('#share-import-rights').addEventListener('input',event=>{shareImportRightsEdited=true;event.stopPropagation();});
$('#share-import-retry').onclick=async()=>{
  const id=activeProject,rights_note=$('#share-import-rights').value.trim(),button=$('#share-import-retry');
  button.disabled=true;
  try{
    if(productionDirty||analysisDirty)await saveProduction();
    await request('/api/production/share-import',{id,rights_note});
    shareImportRightsEdited=false;
    await refreshProduction();
    if(activeProject===id)fillProduction(id);
    message('원본 가져오기를 요청했습니다.');
  }catch(error){message(error.message);const project=currentProduction();if(project)renderShareImport(project,false);}
};
