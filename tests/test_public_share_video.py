from email.message import Message
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import media_worker
import public_share_video as public
import share_import


class PublicShareTests(unittest.TestCase):
    def test_only_exact_post_provides_metadata(self):
        payload={'aweme_detail':{'aweme_id':'123','desc':'공개 영상','video':{'duration':3200},
                                 'author':{'nickname':'작성자','sec_uid':'public-id'}}}
        self.assertIsNone(public.post_info('Douyin','456',payload))
        info=public.post_info('Douyin','123',payload)
        self.assertEqual((info['title'],info['author'],info['duration']),('공개 영상','작성자',3.2))
        self.assertFalse(public.post_info('Xiaohongshu','item',
                            {'note':{'noteId':'item','type':'normal'}})['is_video'])

    def test_player_urls_reject_local_hosts_credentials_and_placeholders(self):
        self.assertTrue(public.media_url('Douyin','https://v5-dy-ov-experiment.zjcdn.com/video'))
        for url in ('https://127.0.0.1/video','https://zjcdn.com.evil.test/file',
                    'https://user@v1.zjcdn.com/file','https://v1.zjcdn.com:8443/file',
                    'https://lf-douyin-pc-web.douyinstatic.com/placeholder.mp4','blob:https://www.douyin.com/123'):
            with self.subTest(url=url):
                self.assertFalse(public.media_url('Douyin',url))
        self.assertEqual(share_import.classify('https://www.rednote.com/explore/6411cf99000000001300b6d9')[0],
                         'Xiaohongshu')

    def test_metadata_request_never_downloads_and_media_failure_retains_title(self):
        details=({'title':'공개 제목','author':'작성자','is_video':True},('media','agent','page'),'')
        task={'platform':'Douyin','download':False,'directory':'unused'}
        with patch.object(public,'browser_details',return_value=details), \
                patch.object(public,'download',side_effect=ValueError('파일 접근 거절')) as download:
            result=media_worker.public_share_fallback(task,'page',{'status':'error'})
            self.assertEqual((result['status'],result['title']),('metadata_only','공개 제목'))
            download.assert_not_called()
            result=media_worker.public_share_fallback({**task,'download':True},'page',{'status':'error'})
        self.assertEqual((result['status'],result['title'],result['message']),
                         ('error','공개 제목','파일 접근 거절'))

    def test_partial_media_response_is_not_saved_as_a_complete_file(self):
        media=('https://v5.zjcdn.com/file','agent','https://www.douyin.com/video/123')
        headers=Message()
        headers['Content-Type']='video/mp4'
        headers['Content-Length']='4'
        headers['Content-Range']='bytes 0-3/100'
        class Response(io.BytesIO):
            status=206
        response=Response(b'test')
        response.headers=headers
        opener=Mock()
        opener.open.return_value=response
        with tempfile.TemporaryDirectory() as folder, patch.object(public.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'전체 영상'):
                public.download('Douyin',media,folder)
            self.assertFalse((Path(folder)/'video.mp4').exists())
