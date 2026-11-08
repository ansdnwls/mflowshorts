import json
from unittest.mock import patch
import unittest
import app,production,scene_analysis as scenes,script_drafts as drafts
import test_scenes

class ScriptTests(unittest.TestCase):
 setUp=test_scenes.SceneTests.setUp
 tearDown=test_scenes.SceneTests.tearDown
 def confirm(self):
  a=scenes.get(self.ident);scenes.review(self.project,a['data']['segments'],'',True)
 def generate(self,kind='local',response=None):
  self.confirm()
  with patch.object(drafts.threading,'Thread') as thread,patch.dict('os.environ',{'MFLOW_AI_KEY':'test-only'}):
   ident=drafts.start(self.project,kind)
  args=thread.call_args.kwargs['args']
  if response is None:drafts.worker(*args)
  else:
   with patch.object(scenes,'api',return_value=response):drafts.worker(*args)
  return drafts.get(ident)
 def test_unconfirmed_analysis_cannot_generate(self):
  with self.assertRaises(ValueError):drafts.start(self.project,'local')
 def test_local_outline_copies_fact_and_does_not_touch_production(self):
  d=self.generate();self.assertEqual(d['status'],'draft');self.assertEqual(d['data']['beats'][0]['narration'],'사람이 걷는다')
  self.assertNotIn('목적은 불명확',json.dumps(d['data'],ensure_ascii=False));self.assertEqual(production.project(self.project)['document']['script'],'')
 def test_explicit_apply_shifts_times_and_retains_old_writing(self):
  production.update_project({'id':self.project,'script':'이전 대본'})
  d=self.generate()
  with self.assertRaises(ValueError):drafts.apply(d['id'],False)
  drafts.apply(d['id'],True);p=production.project(self.project)
  self.assertEqual(p['document']['cues'][0]['start'],0);self.assertEqual(p['document']['cues'][0]['end'],2)
  self.assertEqual(p['document']['script'],'사람이 걷는다');self.assertIsNone(p['document']['approved_hash'])
  self.assertEqual(drafts.get(d['id'])['data']['previous_production']['script'],'이전 대본')
 def test_production_edit_prevents_overwrite(self):
  d=self.generate();production.update_project({'id':self.project,'script':'다른 편집'})
  with self.assertRaises(ValueError):drafts.apply(d['id'],True)
  self.assertEqual(production.project(self.project)['document']['script'],'다른 편집')
 def test_review_change_invalidates_draft(self):
  d=self.generate();a=scenes.get(self.ident);a['data']['segments'][0]['observed']='사람이 멈춘다';scenes.review(self.project,a['data']['segments'],'',True)
  with self.assertRaises(ValueError):drafts.apply(d['id'],True)
 def test_ai_response_and_time_evidence_validation(self):
  data={'headline':'걷는 사람','beats':[{'scene_index':0,'start':2,'end':4,'narration':'사람이 걷고 있습니다.','role':'hook'}]}
  d=self.generate('ai',{'choices':[{'message':{'content':json.dumps(data)}}]});self.assertEqual(d['status'],'draft')
  for field,value in [('start',0),('end',9),('scene_index',99),('narration','가'*61)]:
   changed=json.loads(json.dumps(data));changed['beats'][0][field]=value
   with self.assertRaises(ValueError):drafts.normalize(changed,scenes.get(self.ident))
 def test_failed_generation_does_not_change_existing_script(self):
  production.update_project({'id':self.project,'script':'보존할 대본'})
  d=self.generate('ai',{'choices':[]});self.assertEqual(d['status'],'error')
  self.assertEqual(production.project(self.project)['document']['script'],'보존할 대본')
