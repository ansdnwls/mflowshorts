import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import app
import media_worker

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=patch.object(app,'DB',Path(self.temp.name)/'test.db');self.db.start()
        self.root=patch.object(app,'MEDIA_ROOT',Path(self.temp.name)/'downloads');self.root.start();app.init()
    def tearDown(self):self.root.stop();self.db.stop();self.temp.cleanup()
    def test_platform_urls_reject_local_urls_and_wrong_paths(self):
        for url in ['http://127.0.0.1','https://www.tiktok.com.evil.test/@a/video/123','https://www.tiktok.com@127.0.0.1/a','https://www.tiktok.com:444/@a/video/123','https://www.tiktok.com/@a','https://www.douyin.com/video/123']:
            with self.subTest(url=url),self.assertRaises(ValueError):app.validate_media_task({'platform':'TikTok','action':'inspect','url':url})
        self.assertEqual(app.validate_media_task({'platform':'TikTok','action':'inspect','url':'https://www.tiktok.com/@a/video/123?tracking=x'})['url'],'https://www.tiktok.com/@a/video/123')
    def test_unavailable_views_not_zero_and_platform_ids_do_not_collide(self):
        data={'platform':'Douyin','external_id':'123','url':'https://www.douyin.com/video/123','title':'鱼','likes':200,'comments':5,'views':None,'published':None}
        key=app.save_external_video(data)
        self.assertEqual(key,'douyin:123')
        result=app.state()['all_videos'][0]
        for field in ('views','engagement','vph','views_per_day','age_days'):self.assertIsNone(result[field])
        app.save_external_video({**data,'likes':300})
        self.assertEqual(len(app.state()['all_videos']),1)
        self.assertEqual(app.state()['all_videos'][0]['likes'],300)
    def test_worker_policy_denial_has_specific_code_and_no_credentials(self):
        with patch('urllib.request.urlopen',side_effect=urllib.error.URLError('Tunnel connection failed: 403 Forbidden')):
            with self.assertRaises(media_worker.MediaError) as error:media_worker.preflight('https://www.tiktok.com/')
        self.assertEqual(error.exception.code,'network_blocked')
    def insert_job(self):
        now=dt.datetime.now(app.UTC).isoformat()
        with app.connect() as con:con.execute('INSERT INTO media_jobs VALUES (?,?,?,?,?,?,?,?,?)',('job','TikTok','inspect','queued','','','{}',now,now))
    def test_job_import_success_persists_metadata(self):
        self.insert_job()
        output={'ok':True,'result':{'videos':[{'platform':'TikTok','external_id':'123','url':'https://www.tiktok.com/@a/video/123','title':'test','views':100,'likes':2,'published':None}]}}
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(output))):app.run_media_job('job',{'platform':'TikTok','action':'inspect'})
        state=app.state();self.assertEqual(state['media_jobs'][0]['status'],'success');self.assertEqual(state['all_videos'][0]['platform'],'TikTok')
    def test_job_denial_is_not_reported_as_empty_success(self):
        self.insert_job()
        output={'ok':False,'code':'network_blocked','message':'네트워크 차단'}
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(output))):app.run_media_job('job',{'platform':'TikTok','action':'inspect'})
        job=app.state()['media_jobs'][0];self.assertEqual(job['status'],'error');self.assertEqual(job['code'],'network_blocked');self.assertEqual(app.state()['all_videos'],[])
    def test_download_timeout_cleans_partial_files(self):
        self.insert_job()
        directory=app.MEDIA_ROOT/'job';directory.mkdir(parents=True);(directory/'video.part').write_bytes(b'partial')
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('worker',240)):app.run_media_job('job',{'platform':'TikTok','action':'download'})
        self.assertFalse(directory.exists());self.assertEqual(app.state()['media_jobs'][0]['status'],'error')
