let sceneState=null,sceneProject=null,sceneDirty=false;
const sceneFields={observed:'화면에서 확인한 사실',speech:'해당 구간 대사 (근거가 있을 때만)',screen_text:'화면 문자·워터마크·아이디',uncertain:'추정·불확실한 내용 (사실과 구분)'};
let sceneRequest=0;
async function loadScenes(id,force=false){
  if(sceneProject!==id){sceneState=null;sceneDirty=false;sceneProject=id;}
  const requestId=++sceneRequest;
  const result=await request('/api/scenes/state?id='+encodeURIComponent(id));
  if(sceneProject!==id||requestId!==sceneRequest)return;
  sceneState=result;
  const analysis=result.analysis;
  const busy=analysis&&['preparing','analyzing'].includes(analysis.status);
  if(analysis?.stale||force)sceneDirty=false;
  $('#scene-prepare').disabled=!!busy;
  $('#scene-analyze').disabled=busy||!result.ai_configured||!analysis||analysis.stale||!analysis.data.frames.length;
  $('#scene-apply').disabled=sceneDirty||analysis?.status!=='reviewed'||analysis.stale||!analysis.data.confirmed;
  $('#scene-status').textContent=(analysis?.message||'먼저 원본을 업로드하고 프레임을 추출하세요.')+
    (analysis?.stale?' 원본 또는 구간이 변경됐습니다. 다시 추출해야 합니다.':'')+
    (!result.ai_configured?' AI 연결에는 환경변수 설정이 필요합니다. 직접 검토·입력은 가능합니다.':'');
  if(!sceneDirty)renderScenes(analysis);
}
window.loadScenes=loadScenes;
function renderScenes(a){$('#scene-results').innerHTML=!a?'':`<p class="hint">원본 ${a.data.start}~${(a.data.start+a.data.duration).toFixed(2)}초 · ${a.data.frames.length}장 · ${a.data.has_audio?'음성 추출됨':'음성 없음/미추출'} · ${a.data.confirmed&&!a.stale?'검토 완료':'검토 전'}${a.data.model?' · '+esc(a.data.model):''}</p><div class="scene-frame-grid">${a.data.frames.map(f=>`<figure><a href="/api/scenes/frame?id=${encodeURIComponent(a.id)}&index=${f.index}" target="_blank" rel="noopener"><img src="/api/scenes/frame?id=${encodeURIComponent(a.id)}&index=${f.index}" loading="lazy" alt="원본 ${f.at}초 프레임"></a><figcaption>${f.at}초</figcaption></figure>`).join('')}</div>${a.data.speech_segments?.length?`<details><summary>음성 인식 시간 구간</summary>${a.data.speech_segments.map(s=>`<p class="hint">${s.start}~${s.end}초 · ${esc(s.text)}</p>`).join('')}</details>`:''}<label>전체 음성 전사 · ASR 오인식 검토 필요<textarea id="scene-transcript" maxlength="12000" rows="3">${esc(a.data.transcript)}</textarea></label><div class="scene-segments">${a.data.segments.map((s,i)=>`<article data-scene-row="${i}"><b>${s.start}~${s.end}초</b>${Object.entries(sceneFields).map(([key,label])=>`<label>${label}<textarea data-scene-field="${key}" maxlength="1000" rows="2">${esc(s[key])}</textarea></label>`).join('')}</article>`).join('')}</div><label class="check"><input type="checkbox" id="scene-confirmed" ${a.data.confirmed&&!a.stale?'checked':''}>사실·대사·추정을 직접 확인했습니다</label><button type="button" id="scene-save" class="outline" ${a.stale||['preparing','analyzing'].includes(a.status)?'disabled':''}>분석 검토 저장</button>`;}
async function startScenes(action){try{const id=activeProject;if(!id)return;if(productionDirty)await saveProduction();await request('/api/scenes/start',{project_id:id,action,interval:Number($('#scene-interval').value)});sceneDirty=false;await loadScenes(id,true);}catch(e){message(e.message);}}
$('#scene-prepare').onclick=()=>startScenes('prepare');$('#scene-analyze').onclick=()=>startScenes('analyze');
$('#scene-results').addEventListener('input',e=>{sceneDirty=true;$('#scene-apply').disabled=true;if(e.target.id!=='scene-confirmed'&&$('#scene-confirmed'))$('#scene-confirmed').checked=false;});
$('#scene-results').addEventListener('click',async event=>{
  if(event.target.id!=='scene-save')return;
  if(productionDirty||analysisDirty){message('제작 설정과 분석의 변경사항을 먼저 저장하세요.');return;}
  const id=sceneProject;
  const analysis=sceneState.analysis;
  const segments=[...document.querySelectorAll('[data-scene-row]')].map(row=>{
    const segment={...analysis.data.segments[Number(row.dataset.sceneRow)]};
    row.querySelectorAll('[data-scene-field]').forEach(input=>segment[input.dataset.sceneField]=input.value);
    return segment;
  });
  event.target.disabled=true;
  try{
    await request('/api/scenes/review',{project_id:id,segments,transcript:$('#scene-transcript').value,
      confirmed:$('#scene-confirmed').checked});
    await refreshProduction();
    if(id===activeProject&&$('#production-dialog').open){
      if(!productionDirty&&!analysisDirty)fillProduction(id,true);
      await loadScenes(id,true);
      message('분석 검토를 저장했습니다.');
    }
  }catch(error){message(error.message);}
  finally{event.target.disabled=false;}
});
setInterval(()=>{if($('#production-dialog').open&&sceneProject){const a=sceneState?.analysis;if(a&&['preparing','analyzing'].includes(a.status))loadScenes(sceneProject).catch(e=>message(e.message));}},2000);

$('#scene-panel').addEventListener('input',e=>e.stopPropagation());
$('#scene-panel').addEventListener('change',e=>e.stopPropagation());

$('#scene-apply').onclick=async()=>{
  const id=sceneProject;
  if(id!==activeProject||sceneDirty)return;
  if(productionDirty||analysisDirty){message('제작 설정과 분석의 변경사항을 먼저 저장하세요.');return;}
  try{
    const result=await request('/api/scenes/apply',{project_id:id});
    await refreshProduction();
    if(id===activeProject&&$('#production-dialog').open&&!productionDirty&&!analysisDirty)fillProduction(id,true);
    message(result.message);
  }catch(error){message(error.message);}
};
