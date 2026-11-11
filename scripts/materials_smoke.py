"""Isolated browser check for the proven-material library; no external API calls."""
import json,os,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app,production
from playwright.sync_api import sync_playwright
BASE='http://127.0.0.1:8003'
with tempfile.TemporaryDirectory() as directory:
 app.DB=Path(directory)/'db';app.init()
 production.create_channel({'name':'재가공 채널'})
 for ident,views in [('low',100000),('hit',3000000)]:
  app.save_video({'id':ident,'snippet':{'title':ident,'channelTitle':'creator','publishedAt':app.dt.datetime.now(app.UTC).isoformat()},'statistics':{'viewCount':str(views),'likeCount':'100','commentCount':'2'}},app.dt.datetime.now(app.UTC).isoformat())
 server=subprocess.Popen(['python3','app.py'],cwd=ROOT,env={**os.environ,'MFLOW_DB':str(app.DB),'MFLOW_PROJECT_ROOT':str(Path(directory)/'projects'),'PORT':'8003'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  for attempt in range(50):
   try:urllib.request.urlopen(BASE+'/api/state',timeout=1).close();break
   except OSError:time.sleep(.1)
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1360,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(BASE,wait_until='networkidle');page.wait_for_selector('.video');page.locator('#material-collect').click()
   page.wait_for_function("document.querySelector('#saved-count').textContent==='1'");page.locator('[data-view="saved"]').click();assert page.locator('.video').count()==1
   page.locator('[data-research="hit"]').click();page.locator('#research [name="status"]').select_option('verified');page.locator('#research [name="notes"]').fill('원본 조사 메모');page.get_by_role('button',name='추적 노트 저장',exact=True).click();page.wait_for_selector('#research-dialog',state='hidden')
   page.locator('[data-research="hit"]').click();page.wait_for_function("document.querySelector('#material-form [name=rights]').value==='unknown'")
   page.locator('#material-form [name="hypothesis"]').fill('첫 장면의 호기심 <img src=x onerror=alert(1)>');page.locator('#material-form [name="angle"]').fill('실제 원리를 설명하는 새로운 해석');page.locator('#material-form [name="rights"]').select_option('permission');page.locator('#material-form [name="evidence"]').fill('제작자와 사용 조건 확인 기록');page.locator('#material-form button').click()
   page.wait_for_function("document.querySelector('.material-card-note').textContent.includes('기록 완료')")
   page.locator('#source-to-project').click();page.wait_for_selector('#new-project-dialog[open]');page.locator('#new-project-form [name="title"]').fill('재해석 버전 A');page.locator('#new-project-form button.primary').click();page.wait_for_selector('#production-dialog[open]')
   assert '새로운 해석' in page.locator('#production-form [name="context"]').input_value();assert page.locator('#production-material-brief img').count()==0
   page.locator('[data-close="production-dialog"]').click();page.locator('[data-view="saved"]').click();page.locator('[data-research="hit"]').click()
   page.wait_for_function("document.querySelector('#material-outcome [name=project_id]').options.length===1")
   page.locator('#material-outcome [name="url"]').fill('https://www.youtube.com/shorts/reprocessed');page.locator('#material-outcome [name="views"]').fill('12345');page.locator('#material-outcome [name="note"]').fill('해설 버전 첫 성과');page.locator('#material-outcome button').click()
   page.wait_for_function("document.querySelector('#material-history').textContent.includes('12,345')")
   page.locator('#close-research').click();page.locator('#material-ready-filter').select_option('ready');assert page.locator('.video').count()==1
   page.locator('#material-rights-filter').select_option('unknown');assert page.locator('.video').count()==0;page.locator('#material-rights-filter').select_option('permission');assert page.locator('.video').count()==1
   page.screenshot(path='/tmp/mflow-materials-desktop.png',full_page=True)
   page.reload(wait_until='networkidle');page.locator('[data-view="saved"]').click();page.wait_for_selector('.video');assert '실제 원리' in page.locator('.material-card-note').inner_text()
   page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='/tmp/mflow-materials-mobile.png',full_page=True)
   assert not errors,errors
   with urllib.request.urlopen(BASE+'/api/materials/state') as r:material=json.load(r)['materials']['hit']
   assert material['baseline']['views']==3000000 and material['outcomes'][0]['views']==12345 and material['outcomes'][0]['likes'] is None
   browser.close();print('PASS: threshold collection, source notes preserved, idea/rights records, safe rendering, project brief, linked outcome, filters, persistence and mobile')
 finally:server.terminate();server.wait(timeout=5)
