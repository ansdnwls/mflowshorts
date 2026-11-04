import sys,json,tempfile,subprocess,os,time,urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from playwright.sync_api import sync_playwright
# A temporary database keeps test notes and sample videos out of the user workspace data.
with tempfile.TemporaryDirectory() as folder:
 app.DB=Path(folder)/'test.db';app.init()
 for i,views in enumerate([120000,3000000]):
  app.save_video({'id':f'fixture{i}','snippet':{'title':f'Fixture {i} <img src=x onerror=alert(1)>','channelTitle':'creator','channelId':'channel','publishedAt':app.dt.datetime.now(app.UTC).isoformat(),'description':'credit @original_creator'},'statistics':{'viewCount':str(views),'likeCount':'500','commentCount':'20'}},app.dt.datetime.now(app.UTC).isoformat())
 server=subprocess.Popen(['python3','app.py'],cwd=ROOT,env={**os.environ,'MFLOW_DB':str(app.DB),'PORT':'8001'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  for _ in range(50):
   try:urllib.request.urlopen('http://127.0.0.1:8001/api/state',timeout=1).close();break
   except OSError:time.sleep(.1)
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox']);page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto('http://127.0.0.1:8001',wait_until='networkidle');page.wait_for_selector('.video')
   assert page.locator('.video img').count()==0
   page.locator('[data-save="fixture0"]').click();page.wait_for_function("document.querySelector('#saved-count').textContent === '1'")
   page.locator('[data-view="saved"]').click();assert page.locator('.video').count()==1
   page.locator('[data-research="fixture0"]').click()
   assert '@original_creator' in page.locator('#description').inner_text()
   page.locator('#research [name="source_url"]').fill('https://www.tiktok.com/@original_creator/video/123')
   page.locator('#research [name="status"]').select_option('candidate')
   page.locator('#research [name="notes"]').fill('=HYPERLINK("bad")')
   page.get_by_role('button',name='추적 노트 저장',exact=True).click();page.wait_for_selector('#research-dialog',state='hidden')
   page.reload(wait_until='networkidle');page.locator('[data-view="saved"]').click();assert page.locator('.video').count()==1
   page.locator('[data-research="fixture0"]').click();assert page.locator('#research [name="notes"]').input_value()=='=HYPERLINK("bad")';page.locator('#close-research').click()
   csv=urllib.request.urlopen('http://127.0.0.1:8001/api/export').read().decode('utf-8-sig');assert "'=HYPERLINK" in csv
   page.locator('[data-save="fixture0"]').click();page.wait_for_function("document.querySelector('#saved-count').textContent === '0'")
   assert not errors,errors
   print('ISOLATED browser passed: safe title rendering, favorite/unfavorite, saved persistence, attribution extraction, source notes, CSV formula escaping')
   browser.close()
 finally:server.terminate();server.wait(timeout=5)
