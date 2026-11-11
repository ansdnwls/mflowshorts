import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import production
import publishing


class PublishingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        for change in (patch.object(app,'DB',root/'db.sqlite'),
                       patch.object(production,'PROJECT_ROOT',root/'projects'),
                       patch.dict('os.environ',{'MFLOW_YOUTUBE_MODE':'mock'})):
            change.start()
            self.addCleanup(change.stop)
        app.init()
        self.channels=[production.create_channel({'name':f'Account {i}'}) for i in range(3)]
        self.ids=[]
        for i,channel in enumerate(self.channels):
            ident=production.create_project({'title':f'쇼츠 {i}','channel_id':channel})
            p=production.project(ident)
            p['status']='completed'
            p['document'].update(shorts_bundle={'id':'bundle','description':'상품 정보'},output='short.mp4')
            folder=production.PROJECT_ROOT/ident
            folder.mkdir(parents=True)
            (folder/'short.mp4').write_bytes(f'MP4 fixture {i}'.encode())
            production.store(p)
            self.ids.append(ident)
        self.payload={'projects':self.ids,'bundle_id':'bundle','confirmed':True,'made_for_kids':False}

    def test_mock_uploads_all_three_once(self):
        with patch.object(publishing,'youtube_upload') as upload:
            self.assertEqual(publishing.enqueue(self.payload),{'queued':3})
            self.assertEqual(publishing.enqueue(self.payload),{'queued':0})
            self.assertEqual([publishing.process_one() for _ in range(4)],[True,True,True,False])
            upload.assert_not_called()
        jobs=publishing.snapshot()['jobs']
        self.assertEqual(len({job['video_id'] for job in jobs.values()}),3)
        self.assertEqual({job['status'] for job in jobs.values()},{'completed'})
        self.assertEqual(publishing.enqueue(self.payload),{'queued':0})

    def test_schedule_cancel_and_changed_file(self):
        future=dt.datetime.fromtimestamp(publishing.utc_now()+600,dt.timezone.utc)
        publishing.enqueue({**self.payload,'scheduled_at':future.isoformat()})
        self.assertFalse(publishing.process_one())
        publishing.cancel(self.ids[0])
        production.asset(self.ids[1],'output').write_bytes(b'different file')
        with patch.object(publishing,'utc_now',return_value=future.timestamp()+1):
            while publishing.process_one():pass
        jobs=publishing.snapshot()['jobs']
        self.assertEqual([jobs[ident]['status'] for ident in self.ids],['cancelled','failed','completed'])

    def test_uncertain_real_upload_is_never_retried(self):
        with patch.object(publishing,'mode',return_value='youtube'), \
                patch.object(publishing,'account',return_value=('channel','token')), \
                patch.object(publishing,'youtube_upload',side_effect=publishing.UploadUncertain) as upload:
            payload={**self.payload,'projects':self.ids[:1]}
            self.assertEqual(publishing.enqueue(payload),{'queued':1})
            self.assertTrue(publishing.process_one())
            self.assertEqual(publishing.snapshot()['jobs'][self.ids[0]]['status'],'needs_check')
            self.assertEqual(publishing.enqueue(payload),{'queued':0})
            self.assertFalse(publishing.process_one())
            upload.assert_called_once()

    def test_youtube_adapter_refuses_mismatched_channel_before_sending_video(self):
        with patch.object(publishing,'account',return_value=('expected','refresh')), \
                patch.dict('os.environ',{'MFLOW_YOUTUBE_CLIENT_ID':'fixture','MFLOW_YOUTUBE_CLIENT_SECRET':'fixture'}), \
                patch.object(publishing,'google',side_effect=[
                    (200,None,b'{"access_token":"fixture"}'),
                    (200,None,json.dumps({'items':[{'id':'different'}]}).encode()),
                ]) as google:
            with self.assertRaisesRegex(ValueError,'일치하지 않습니다'):
                publishing.youtube_upload({'channel':self.channels[0],'target':'expected'},
                                          production.asset(self.ids[0],'output'))
            self.assertEqual(google.call_count,2)

    def test_youtube_adapter_uses_resumable_session_and_bound_identity(self):
        def transport(method, host, path, body=None, headers=None):
            if path=='/token':
                return 200,None,b'{"access_token":"fixture"}'
            if path.startswith('/youtube/v3/channels'):
                return 200,None,b'{"items":[{"id":"expected"}]}'
            if method=='POST':
                self.assertEqual(json.loads(body)['status']['privacyStatus'],'private')
                return 200,'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=fixture',b''
            self.assertEqual((method,host),('PUT','www.googleapis.com'))
            self.assertEqual(body.read(),b'MP4 fixture 0')
            return 201,None,b'{"id":"example1234","snippet":{"channelId":"expected"}}'

        payload={'channel':self.channels[0],'target':'expected','title':'쇼츠','description':'상품',
                 'privacy':'private','kids':False}
        with patch.object(publishing,'account',return_value=('expected','refresh')), \
                patch.dict('os.environ',{'MFLOW_YOUTUBE_CLIENT_ID':'fixture','MFLOW_YOUTUBE_CLIENT_SECRET':'fixture'}), \
                patch.object(publishing,'google',side_effect=transport):
            self.assertEqual(publishing.youtube_upload(payload,production.asset(self.ids[0],'output')),
                             'example1234')
