import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import app
import media_worker
import production
import project_import
import share_import


class ShareLinkTests(unittest.TestCase):
    def test_platforms_and_share_text(self):
        cases=[
            ('https://www.bilibili.com/video/BV13x41117TL/?p=2&spm_id_from=1','Bilibili',False),
            ('https://b23.tv/BV13x41117TL','Bilibili',True),
            ('https://www.douyin.com/video/6961737553342991651','Douyin',False),
            ('https://v.douyin.com/uD7JHhJ-0oo/','Douyin',True),
            ('https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=a','Xiaohongshu',False),
            ('https://xhslink.com/a/abc123','Xiaohongshu',True),
            ('https://www.kuaishou.com/short-video/abc123','Kuaishou',False),
            ('https://v.kuaishou.com/abc123','Kuaishou',True),
        ]
        for url,platform,short in cases:
            with self.subTest(url=url):
                detected,normalized,is_short=share_import.from_share('复制此链接，打开应用 '+url+'。')
                self.assertEqual((detected,is_short),(platform,short))
                self.assertNotIn('spm_id_from',normalized)
        self.assertEqual(share_import.classify('https://b23.tv/BV13x41117TL/')[1],
                         'https://b23.tv/BV13x41117TL')

    def test_rejects_non_posts_and_untrusted_redirects(self):
        for url in ('https://b23.tv.evil.test/test','https://b23.tv@127.0.0.1/test',
                    'https://www.bilibili.com:443/video/BV13x41117TL','https://www.douyin.com/user/abc',
                    'https://b23.tv/%2e%2e/test','https://www.xiaohongshu.com/user/profile/abc'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                share_import.from_share(url)
        redirect=Mock()
        redirect.open.side_effect=HTTPError('https://b23.tv/abc/',302,'Found',
                                            {'Location':'http://127.0.0.1:8020/'},io.BytesIO())
        with self.assertRaises(ValueError):
            share_import.resolve('Bilibili','https://b23.tv/abc',redirect)
        self.assertEqual(redirect.open.call_count,1)

    def test_short_link_resolves_and_normalizes_canonical_page(self):
        redirect=Mock()
        redirect.open.side_effect=HTTPError('https://v.douyin.com/abc/',302,'Found',
                                            {'Location':'https://www.iesdouyin.com/share/video/6961737553342991651/?region=CN'},io.BytesIO())
        self.assertEqual(share_import.resolve('Douyin','https://v.douyin.com/abc',redirect),
                         'https://www.douyin.com/video/6961737553342991651')

    def test_xiaohongshu_photo_never_downloads(self):
        downloader=Mock()
        downloader.__enter__=Mock(return_value=downloader)
        downloader.__exit__=Mock(return_value=False)
        downloader.extract_info.return_value={'title':'공개 사진 제목','formats':[{'ext':'jpg'}]}
        module=types.SimpleNamespace(YoutubeDL=Mock(return_value=downloader))
        with patch.dict(sys.modules,{'yt_dlp':module}):
            result=media_worker.import_share({'platform':'Xiaohongshu','download':True,
                'url':'https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9'})
        self.assertEqual(result['status'],'unsupported')
        self.assertEqual(result['title'],'공개 사진 제목')
        self.assertEqual(downloader.extract_info.call_count,1)
        self.assertFalse(downloader.extract_info.call_args.kwargs['download'])


class ProjectShareTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        for target,attribute,path in ((app,'DB',root/'db.sqlite'),(production,'PROJECT_ROOT',root/'projects')):
            changed=patch.object(target,attribute,path)
            changed.start()
            self.addCleanup(changed.stop)
        app.init()
        self.channel=production.create_channel({'name':'공유 테스트','language':'ko'})

    def create(self,link,note=''):
        record=project_import.source_record(link,note)
        ident=production.create_project({'title':'가져오기','channel_id':self.channel},record)
        return ident

    def test_unavailable_import_keeps_project_available_for_manual_upload(self):
        ident=self.create('https://www.kuaishou.com/short-video/abc123','직접 촬영')
        record=project_import.start(ident,'unused',Path(sys.executable).with_name('missing-media-python.exe'))
        self.assertEqual(record['status'],'error')
        with patch.object(production,'probe',return_value={'duration':4,'width':320,'height':240,'audio':True}):
            production.upload(ident,io.BytesIO(b'video'),5)
        doc=production.project(ident)['document']
        self.assertEqual(doc['share_import']['status'],'manual')
        self.assertEqual(doc['share_import']['rights_note'],'직접 촬영')
        self.assertEqual(production.asset(ident,'source').read_bytes(),b'video')

    def test_worker_attaches_video_and_retains_rights_and_canonical_metadata(self):
        ident=self.create('https://b23.tv/abc123','CC BY 3.0')
        def worker(args,**kwargs):
            task=json.loads(kwargs['input'])
            if task['download']:
                (Path(task['directory'])/'video.mp4').write_bytes(b'fixture')
            return subprocess.CompletedProcess(args,0,json.dumps({'ok':True,'result':{
                'status':'downloaded' if task['download'] else 'metadata_only',
                'filename':'video.mp4','title':'영상 제목','author':'게시자',
                'original_url':'https://www.bilibili.com/video/BV13x41117TL'}}))
        info={'duration':4,'width':320,'height':240,'audio':True}
        with patch.object(project_import.subprocess,'run',side_effect=worker), patch.object(production,'probe',return_value=info), \
                patch.object(project_import.threading,'Thread',side_effect=lambda target,args,daemon:types.SimpleNamespace(start=lambda:target(*args))):
            project_import.start(ident,'worker',sys.executable)
        p=production.project(ident)
        self.assertEqual(p['status'],'source_ready')
        record=p['document']['share_import']
        self.assertEqual((record['status'],record['title'],record['author'],record['rights_note']),
                         ('imported','영상 제목','게시자','CC BY 3.0'))
        self.assertEqual(p['document']['source_url'],record['original_url'])
        self.assertEqual(production.asset(ident,'source').read_bytes(),b'fixture')

    def test_late_worker_does_not_replace_manual_file(self):
        ident=self.create('https://b23.tv/abc123','직접 촬영')
        p=production.project(ident)
        p['document']['share_import'].update(job_id='job',status='running')
        production.store(p)
        info={'duration':4,'width':320,'height':240,'audio':True}
        with patch.object(production,'probe',return_value=info):
            production.upload(ident,io.BytesIO(b'manual'),6)
        path=production.PROJECT_ROOT/ident/'fixture.mp4'
        path.write_bytes(b'late download')
        project_import.update(ident,'job',{'status':'downloaded','title':'가져온 제목'},(path,info))
        self.assertEqual(production.asset(ident,'source').read_bytes(),b'manual')
        self.assertEqual(production.project(ident)['document']['share_import']['title'],'가져온 제목')

    def test_download_timeout_keeps_verified_metadata(self):
        ident=self.create('https://b23.tv/abc123','CC BY 3.0')
        metadata={'status':'metadata_only','title':'확인한 제목','author':'확인한 작성자',
                  'original_url':'https://www.bilibili.com/video/BV13x41117TL'}
        with (patch.object(project_import,'worker_result',side_effect=[metadata,subprocess.TimeoutExpired('worker',240)]),
              patch.object(project_import.threading,'Thread',
                           side_effect=lambda target,args,daemon:types.SimpleNamespace(start=lambda:target(*args)))):
            project_import.start(ident,'worker',sys.executable)
        doc=production.project(ident)['document']
        self.assertIsNone(doc['source'])
        self.assertEqual(doc['share_import']['status'],'error')
        self.assertEqual(doc['share_import']['title'],metadata['title'])
        self.assertEqual(doc['source_url'],metadata['original_url'])
