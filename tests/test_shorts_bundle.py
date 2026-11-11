import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

import app
import production
import shorts_bundle
import workflow


class ShortsBundleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for module, name, value in (
            (app, 'DB', self.root/'test.sqlite'),
            (production, 'PROJECT_ROOT', self.root/'projects'),
        ):
            change = patch.object(module,name,value)
            change.start()
            self.addCleanup(change.stop)
        app.init()
        self.channels = [production.create_channel({'name':f'계정 {i+1}'}) for i in range(3)]
        self.parent = production.create_project({'title':'원본 영상','channel_id':self.channels[0]})
        self.data = {
            'source_project_id':self.parent, 'product':'밀폐용기', 'option':'뚜껑 포함 1 L',
            'same_option':True, 'product_url':'https://example.com/item',
            'review_url':'https://example.com/reviews',
            'positive_review':'뚜껑 열기가 편해요.', 'concern_review':'세척할 때 분리가 번거로워요.',
            'variants':{angle:{'channel_id':self.channels[i],'start':i*4,'end':(i+1)*4,
                               'observed':observed} for i,(angle,observed) in enumerate(
                zip(shorts_bundle.ANGLES,('한 손으로 뚜껑을 들어요.','용기를 선반에 놓아요.','뚜껑을 분리해요.')))},
        }

    def test_invalid_assignment_does_not_create_any_projects(self):
        with patch.object(production,'probe',return_value={'duration':12,'width':160,'height':90,'audio':False}):
            production.upload(self.parent,io.BytesIO(b'source'),6)
        self.data['variants']['guide']['channel_id']=self.channels[0]
        with self.assertRaisesRegex(ValueError,'서로 다른 제작 채널'):
            shorts_bundle.create(self.data)
        self.assertEqual([p['id'] for p in production.state()['projects']],[self.parent])

    def test_recommendations_require_confirmed_facts_and_choose_distinct_openings(self):
        with patch.object(production,'probe',return_value={'duration':12,'width':160,'height':90,'audio':False}):
            production.upload(self.parent,io.BytesIO(b'source'),6)
        scenes = [
            {'start':0,'end':4,'facts':['한 손으로 뚜껑을 열어요.']},
            {'start':4,'end':8,'facts':['용기를 선반에 놓아요.']},
            {'start':8,'end':12,'facts':['뚜껑을 분리해 세척해요.'],'inferences':['한 손으로도 열어 쓸 것 같아요.']},
        ]
        workflow.save_analysis(self.parent,{'scenes':scenes})
        self.data['positive_review']='뚜껑을 열기 편해요.'
        self.assertFalse(shorts_bundle.recommend(self.data)['confirmed'])
        workflow.confirm_analysis(self.parent)
        result=shorts_bundle.recommend(self.data)
        self.assertTrue(result['confirmed'])
        self.assertEqual([option['start'] for option in result['angles']['review']],[0])
        self.assertEqual([option['start'] for option in result['angles']['guide']],[8])
        chosen={angle:next(item for item in options if item['preferred'])
                for angle,options in result['angles'].items()}
        self.assertEqual([item['start'] for item in chosen.values()],[4,0,8])
        for angle,item in chosen.items():
            self.data['variants'][angle].update(item)
        bundle=shorts_bundle.create(self.data)
        for angle,ident in zip(shorts_bundle.ANGLES,bundle['projects']):
            doc=production.project(ident)['document']
            self.assertEqual(doc['cues'][0]['text'],chosen[angle]['hook'])
            self.assertEqual(doc['clips'][0]['start'],chosen[angle]['start'])
        self.data['concern_review']='가방에 넣고 다니기 어려워요.'
        self.assertEqual(shorts_bundle.recommend(self.data)['angles']['guide'],[])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                         'FFmpeg, FFprobe and the production font are required')
    def test_bundle_automation_aligns_captions_and_renders_all_channels_once(self):
        source=self.root/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=160x90:r=30:d=12',
                        '-c:v','libx264',str(source)],check=True,capture_output=True,timeout=30)
        with source.open('rb') as stream:
            production.upload(self.parent,stream,source.stat().st_size)
        for i,channel in enumerate(self.channels):
            shorts_bundle.save_narration_profile(channel,{'voice':f'한국어 음성 {i+1}','rate':i,
                                                         'closing':f'마무리 {i+1}'})
        bundle=shorts_bundle.create(self.data)

        class TestVoice:
            name='test-tone'
            mock=False

            def __init__(self, voice, rate):
                self.rate=rate

            def synthesize(self, text, output):
                with wave.open(str(output),'wb') as audio:
                    audio.setparams((1,2,8000,0,'NONE','not compressed'))
                    audio.writeframes((b'\x00\x18\x00\xe8')*(2000-200*self.rate))

        with patch.object(shorts_bundle.narration,'WindowsOneCore',TestVoice):
            self.assertEqual(shorts_bundle.queue_automation(bundle['id']),{'queued':3})
            self.assertEqual(shorts_bundle.queue_automation(bundle['id']),{'queued':0})
            production.recover_renders()
            for _ in range(3):
                self.assertTrue(production.process_next_render())
        self.assertEqual(shorts_bundle.queue_automation(bundle['id']),{'queued':0})
        for i,ident in enumerate(bundle['projects']):
            project=production.project(ident)
            self.assertEqual(project['status'],'completed',project['document']['message'])
            doc=project['document']
            self.assertEqual(doc['bundle_narration']['profile']['voice'],f'한국어 음성 {i+1}')
            self.assertEqual(doc['cues'][-1]['text'],f'마무리 {i+1}')
            self.assertEqual(doc['cues'][0]['end'],round(doc['bundle_narration']['segments'][0]['end'],2))
            with zipfile.ZipFile(production.asset(ident,'archive')) as archive:
                manifest=json.loads(archive.read('project.json'))
                self.assertGreater(manifest['bundle_audio_qa']['peak'],0)
                self.assertIn(f'마무리 {i+1}',archive.read('captions.srt').decode())

        first=bundle['projects'][0]
        production.update_project({'id':first,'clips':[{'start':0,'end':1}]})

        class LongVoice(TestVoice):
            def synthesize(self, text, output):
                with wave.open(str(output),'wb') as audio:
                    audio.setparams((1,2,8000,0,'NONE','not compressed'))
                    audio.writeframes(b'\x00\x10'*16000)

        with patch.object(shorts_bundle.narration,'WindowsOneCore',LongVoice):
            self.assertEqual(shorts_bundle.queue_automation(bundle['id']),{'queued':1})
            self.assertTrue(production.process_next_render())
        failed=production.project(first)
        self.assertEqual(failed['status'],'error')
        self.assertIn('컷을 늘리거나 가장 긴 문장을 줄이세요',failed['document']['message'])
        self.assertEqual(failed['document']['clips'],[{'start':0,'end':1}])
        with self.assertRaises(ValueError):
            production.asset(first,'output')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                         'FFmpeg, FFprobe and the production font are required')
    def test_three_distinct_cuts_render_with_account_specific_posting_files(self):
        source=self.root/'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=160x90:r=30:d=12',
                        '-vf',"drawbox=c=red:t=fill:enable='lt(t,4)',drawbox=c=lime:t=fill:enable='between(t,4,7.99)'",
                        '-c:v','libx264',str(source)],check=True,capture_output=True,timeout=30)
        with source.open('rb') as stream:
            production.upload(self.parent,stream,source.stat().st_size)
        before=production.project(self.parent)
        bundle=shorts_bundle.create(self.data)
        self.assertEqual(production.project(self.parent),before)
        for index, ident in enumerate(bundle['projects']):
            with self.subTest(index=index):
                project=production.project(ident)
                self.assertEqual(project['channel_id'],self.channels[index])
                self.assertEqual(project['document']['shorts_bundle']['id'],bundle['id'])
                self.assertEqual(project['document']['clips'][0]['start'],index*4)
                production.approve(ident)
                production.start_render(ident)
                self.assertTrue(production.process_next_render())
                self.assertEqual(production.project(ident)['status'],'completed')
                output=production.asset(ident,'output')
                pixel=subprocess.check_output(['ffmpeg','-v','error','-ss','1','-i',str(output),
                    '-vf','format=rgb24,crop=1:1:400:720','-frames:v','1','-f','rawvideo','-'])
                self.assertGreater(pixel[index],200)
                with zipfile.ZipFile(production.asset(ident,'archive')) as archive:
                    posting=json.loads(archive.read('project.json'))['posting']
                    self.assertEqual(posting['channel'],f'계정 {index+1}')
                    self.assertEqual(posting['title'],project['title'])
                    self.assertIn('https://example.com/item',archive.read('posting.txt').decode())
                    captions=archive.read('captions.srt').decode()
                    self.assertIn(shorts_bundle.ANGLES[posting['angle']][1],captions)
                    self.assertEqual('구매 후기 발췌' in captions,index>0)
