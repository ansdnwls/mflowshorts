import io
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import unittest
import app,production,scene_analysis as scenes

class SceneTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=patch.object(app,'DB',Path(self.tmp.name)/'db');self.root=patch.object(production,'PROJECT_ROOT',Path(self.tmp.name)/'projects');self.db.start();self.root.start();app.init()
  c=production.create_channel({'name':'test'});self.project=production.create_project({'title':'test','channel_id':c});p=production.project(self.project);p['document']['source']={'filename':'source','duration':10,'audio':False};p['document']['settings'].update(start=2,duration=4);production.store(p)
  digest,a,b=scenes.signature(p);self.ident='test-analysis'
  data={'start':a,'duration':b,'frames':[{'index':0,'at':2,'end':4}],'segments':[{'start':2,'end':4,'observed':'사람이 걷는다','speech':'','screen_text':'','uncertain':'목적은 불명확'}],'transcript':'','confirmed':False}
  with app.connect() as con:con.execute('INSERT INTO scene_analyses VALUES (?,?,?,?,?,?,?)',(self.ident,self.project,digest,'prepared','',json.dumps(data),production.now()))
 def tearDown(self):self.root.stop();self.db.stop();self.tmp.cleanup()
 def test_review_stales_after_source_change(self):
  item=scenes.get(self.ident);scenes.review(self.project,item['data']['segments'],'',True);self.assertTrue(scenes.state(self.project)['analysis']['data']['confirmed'])
  p=production.project(self.project);p['document']['source']['filename']='new';production.store(p)
  self.assertTrue(scenes.state(self.project)['analysis']['stale'])
  with self.assertRaises(ValueError):scenes.review(self.project,item['data']['segments'],'',True)
 def test_bad_times_and_inference_only_cannot_confirm(self):
  data=scenes.get(self.ident)['data']
  for start,end in [(1,3),(2,9),(float('nan'),3),(3,2)]:
   with self.assertRaises(ValueError):scenes.validate_segments([{'start':start,'end':end}],data)
  with self.assertRaises(ValueError):scenes.review(self.project,[{'start':2,'end':4,'uncertain':'추정만'}],'',True)
 def test_ai_payload_and_response_schema(self):
  folder=scenes.directory(scenes.get(self.ident));folder.mkdir(parents=True);(folder/'frame-0.jpg').write_bytes(b'fixture')
  data=scenes.get(self.ident)['data'];response={'choices':[{'message':{'content':json.dumps({'segments':data['segments']})}}]}
  with patch.object(scenes,'api',return_value=response) as call:scenes.analyze(scenes.get(self.ident))
  payload=json.loads(call.call_args.args[1]);self.assertEqual(payload['response_format'],{'type':'json_object'})
  self.assertEqual(scenes.get(self.ident)['status'],'analyzed');self.assertFalse(scenes.get(self.ident)['data']['confirmed'])
 def test_api_failure_preserves_frames(self):
  item=scenes.get(self.ident)
  with patch.object(scenes,'analyze',side_effect=ValueError('API failed')):
   scenes.LOCK.acquire();scenes.worker(item,'analyze',production.project(self.project))
  self.assertEqual(scenes.get(self.ident)['status'],'error');self.assertEqual(len(scenes.get(self.ident)['data']['frames']),1);self.assertFalse(scenes.LOCK.locked())
 def test_edit_clears_confirmation(self):
  segments=scenes.get(self.ident)['data']['segments'];scenes.review(self.project,segments,'',True)
  segments[0]['observed']='다른 사실';scenes.review(self.project,segments,'',False)
  self.assertIsNone(scenes.get(self.ident)['data']['review_hash'])

 def test_reviewed_frames_feed_the_production_script(self):
  with self.assertRaises(ValueError):
   scenes.apply_review(self.project)
  item = scenes.get(self.ident)
  scenes.review(self.project, item['data']['segments'], '', True)
  analysis = scenes.apply_review(self.project)['analysis']
  self.assertTrue(analysis['confirmed'])
  self.assertEqual(analysis['scenes'][0]['facts'], ['사람이 걷는다'])
  self.assertEqual(analysis['scenes'][0]['inferences'], ['목적은 불명확'])

  def response(indices):
   return {'choices': [{'message': {'content': json.dumps({'indices': indices})}}]}

  with patch.object(scenes, 'api', return_value=response([0, 0, 1])):
   script = scenes.generate_ai_script(self.project)['script']
  self.assertEqual(script['provider'], 'openai_evidence')
  self.assertEqual([line['section'] for line in script['sentences']], ['hook', 'situation', 'ending'])
  self.assertEqual([line['kind'] for line in script['sentences']], ['fact', 'fact', 'inference'])
  scenes.apply_review(self.project)
  self.assertEqual(production.project(self.project)['document']['generated_script'], script)
  for invalid in ([1, 0, 0], [0, 0, 10], [0, 0, True]):
   with patch.object(scenes, 'api', return_value=response(invalid)), self.assertRaises(ValueError):
    scenes.generate_ai_script(self.project)
  self.assertEqual(production.project(self.project)['document']['generated_script'], script)
  item['data']['segments'][0]['observed'] = '사람이 멈춘다'
  scenes.review(self.project, item['data']['segments'], '', True)
  updated = production.project(self.project)['document']
  self.assertFalse(updated['analysis']['confirmed'])
  self.assertNotIn('generated_script', updated)
  scenes.apply_review(self.project)
  production.update_project({'id': self.project, 'settings': {**updated['settings'], 'start': 3}})
  self.assertNotIn('analysis', production.project(self.project)['document'])
  self.assertTrue(scenes.state(self.project)['analysis']['stale'])

 @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and FFprobe required')
 def test_real_frame_and_audio_extraction_feeds_local_script(self):
  source = Path(self.tmp.name) / 'scene.mp4'
  subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=24',
                  '-f', 'lavfi', '-i', 'sine=440', '-t', '3', '-c:v', 'libx264',
                  '-threads', '2', '-c:a', 'aac', str(source)],
                 check=True, capture_output=True, timeout=20)
  payload = source.read_bytes()
  production.upload(self.project, io.BytesIO(payload), len(payload))
  project = production.project(self.project)
  production.update_project({'id': self.project, 'settings': {**project['document']['settings'], 'start': 0}})
  ident = scenes.start(self.project, 'prepare', 2)
  deadline = time.monotonic() + 20
  item = scenes.get(ident)
  while item['status'] == 'preparing' and time.monotonic() < deadline:
   time.sleep(0.05)
   item = scenes.get(ident)
  self.assertEqual(item['status'], 'prepared', item['message'])
  self.assertTrue(item['data']['has_audio'])
  self.assertEqual([frame['at'] for frame in item['data']['frames']], [0, 2])
  self.assertTrue(scenes.frame(ident, 0).read_bytes().startswith(b'\xff\xd8'))
  item['data']['segments'][0]['observed'] = '색상 패턴이 움직인다'
  scenes.review(self.project, item['data']['segments'], '', True)
  scenes.apply_review(self.project)
  result = scenes.workflow.generate_script(self.project)['script']
  self.assertTrue(all(line['kind'] == 'fact' for line in result['sentences']))

 def test_audio_transcript_has_original_timestamps(self):
  item=scenes.get(self.ident);item['data']['has_audio']=True
  folder=scenes.directory(item);folder.mkdir(parents=True);(folder/'frame-0.jpg').write_bytes(b'fixture');(folder/'audio.wav').write_bytes(b'wav fixture')
  asr={'text':'hello','segments':[{'start':0.5,'end':1.2,'text':'hello'}]}
  vision={'choices':[{'message':{'content':json.dumps({'segments':item['data']['segments']})}}]}
  with patch.object(scenes,'api',side_effect=[asr,vision]) as call:scenes.analyze(item)
  self.assertIn(b'verbose_json',call.call_args_list[0].args[1])
  result=scenes.get(self.ident)['data'];self.assertEqual(result['speech_segments'][0]['start'],2.5);self.assertEqual(result['transcript'],'hello')
