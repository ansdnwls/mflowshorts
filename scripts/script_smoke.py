"""Real browser/upload/FFmpeg smoke test, isolated from user projects."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
import io
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:8002'
with tempfile.TemporaryDirectory() as directory:
    work=Path(directory);source=work/'source.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','3','-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac',str(source)],check=True)
    server=subprocess.Popen(['python3','app.py'],cwd=ROOT,env={**{k:v for k,v in os.environ.items() if k!='MFLOW_AI_KEY'},'MFLOW_DB':str(work/'db.sqlite'),'MFLOW_PROJECT_ROOT':str(work/'projects'),'PORT':'8002'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for attempt in range(50):
            try:urllib.request.urlopen(BASE+'/api/production/state',timeout=1).close();break
            except OSError:time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1360,'height':1000});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(BASE,wait_until='networkidle');page.locator('[data-view="channels"]').click()
            page.locator('#channel-form [name="name"]').fill('연동 검증 채널')
            page.locator('#channel-form button').click();page.wait_for_function("document.querySelector('#channel-grid').textContent.includes('연동 검증 채널')")
            page.locator('[data-view="projects"]').click();page.locator('#new-project').click()
            page.locator('#new-project-form [name="title"]').fill('원본으로 새 이야기 만들기')
            page.locator('#new-project-form button.primary').click();page.wait_for_selector('#production-dialog[open]')
            page.locator('#production-upload').set_input_files(source)
            page.wait_for_function("document.querySelector('#upload-status').textContent.includes('640×360')")
            page.locator('#scene-prepare').click()
            page.wait_for_selector('.scene-frame-grid img',timeout=60000)
            assert page.locator('.scene-frame-grid img').count()==2
            page.wait_for_function("[...document.querySelectorAll('.scene-frame-grid img')].every(i=>i.naturalWidth>0)")
            assert page.locator('#scene-analyze').is_disabled()
            page.locator('[data-scene-field="observed"]').first.fill('색상 패턴이 움직이는 테스트 화면이다.')
            page.locator('#scene-confirmed').check();page.locator('#scene-save').click()
            page.wait_for_function("document.querySelector('#scene-status').textContent.includes('검토 완료')")
            with urllib.request.urlopen(BASE+'/api/production/state') as r: scene_project=json.load(r)['projects'][0]['id']
            with urllib.request.urlopen(BASE+'/api/scenes/state?id='+scene_project) as r: analysis=json.load(r)['analysis']
            assert analysis['data']['has_audio'] and analysis['data']['confirmed']
            assert analysis['data']['frames'][0]['at']==0 and analysis['data']['frames'][1]['at']==2
            print('PASS: real frame/audio extraction, frame serving, manual review and persisted confirmation; AI button disabled without credentials')
            page.locator('#draft-local').click()
            page.wait_for_selector('[data-beat] textarea',timeout=30000)
            assert '색상 패턴' in page.locator('[data-beat] textarea').first.input_value()
            assert '로컬 사실 정리 틀' in page.locator('#draft-results').inner_text()
            assert page.locator('#draft-ai').is_disabled()
            page.locator('[data-beat] textarea').first.fill('색상 패턴이 움직이고 있습니다.')
            page.locator('#draft-confirm').check();page.locator('#draft-apply').click()
            page.wait_for_function("document.querySelector('#draft-status').textContent.includes('반영했습니다')")
            assert page.locator('#production-form [name="script"]').input_value()=='색상 패턴이 움직이고 있습니다.'
            assert page.locator('#production-render').is_disabled()
            with urllib.request.urlopen(BASE+'/api/production/state') as r: applied=json.load(r)['projects'][0]['document']
            assert applied['cues'][0]['start']==0 and applied['approved_hash'] is None
            print('PASS: local draft from reviewed facts, evidence display, edited draft, explicit apply, production cues, render approval revoked')

            page.locator('#production-form [name="headline"]').fill('새로운 해석: 100% 확인')
            page.locator('#production-form [name="script"]').fill('직접 확인한 장면을 새로운 맥락으로 설명합니다. 자막과 원음이 함께 나오는 테스트입니다.')
            page.locator('#production-form [name="duration"]').fill('2')
            page.locator('#production-form [name="style"][value="news"]').check()
            page.locator('#production-form [name="save_template"]').check()
            page.locator('#production-approve').click()
            page.wait_for_function("!document.querySelector('#production-render').disabled")
            # Any new edit disables rendering until approved again.
            page.locator('#production-form [name="headline"]').fill('확인하고 만드는 새로운 이야기')
            assert page.locator('#production-render').is_disabled()
            page.locator('#production-approve').click();page.wait_for_function("!document.querySelector('#production-render').disabled")
            page.locator('#production-render').click()
            page.wait_for_selector('#production-downloads a',timeout=60000)
            assert page.locator('#finished-preview').is_visible()
            with urllib.request.urlopen(BASE+'/api/production/state') as response:state=json.load(response)
            project=state['projects'][0];ident=project['id']
            assert project['status']=='completed'
            assert state['channels'][0]['template']['style']=='news'
            assert project['document']['render_info']['width']==1080 and project['document']['render_info']['height']==1920
            assert project['document']['render_info']['audio']
            asset=BASE+'/api/production/asset?id='+ident+'&kind=output'
            with urllib.request.urlopen(urllib.request.Request(asset,headers={'Range':'bytes=0-9'})) as response:
                assert response.status==206 and len(response.read())==10
                assert response.headers['Content-Range'].startswith('bytes 0-9/')
            with urllib.request.urlopen(BASE+'/api/production/asset?id='+ident+'&kind=archive') as response:archive_bytes=response.read()
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                assert set(archive.namelist())=={'short.mp4','captions.srt','project.json'}
            page.screenshot(path='/tmp/mflow-production-desktop.png',full_page=True)
            page.locator('[data-close="production-dialog"]').click();page.screenshot(path='/tmp/mflow-projects-desktop.png',full_page=True)
            page.reload(wait_until='networkidle');page.locator('[data-view="projects"]').click();page.wait_for_selector('[data-open-project]')
            page.locator('[data-open-project]').click();assert page.locator('#production-downloads a').count()==2
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path='/tmp/mflow-production-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert page.locator('#production-dialog').evaluate('(el)=>el.scrollWidth <= el.clientWidth+1')
            assert not errors,errors
            browser.close()
            print('PASS: channel, template, project, upload, preview, approval invalidation, real FFmpeg render, MP4/ZIP, range requests, persistence, mobile, no JS errors')
    finally:server.terminate();server.wait(timeout=5)
