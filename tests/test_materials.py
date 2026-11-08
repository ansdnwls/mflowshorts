import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
import app,production,material_library as materials

class MaterialTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=patch.object(app,'DB',Path(self.temp.name)/'test.db');self.db.start();app.init()
  for ident,views in [('big',2000000),('small',100),('other',500000)]:
   app.save_video({'id':ident,'snippet':{'title':ident,'channelTitle':'creator','publishedAt':'2026-11-01T00:00:00Z','channelId':'channel'},'statistics':{'viewCount':str(views),'likeCount':'10','commentCount':'1'}},'2026-11-02T00:00:00+00:00')
 def tearDown(self):self.db.stop();self.temp.cleanup()
 def test_collect_real_thresholds_preserves_research(self):
  with app.connect() as con:con.execute("INSERT INTO research(video_id,notes,status) VALUES ('big','preserve','verified')")
  self.assertEqual(materials.collect(['big','small']),['big'])
  with app.connect() as con:r=con.execute("SELECT * FROM research WHERE video_id='big'").fetchone()
  self.assertEqual(r['notes'],'preserve');self.assertEqual(r['status'],'verified');self.assertEqual(r['saved'],1)
 def test_channel_relative_performance_is_independent_of_absolute_views(self):
  with app.connect() as con:con.execute('INSERT INTO channels VALUES (?,?,?,?,?)',('channel',10,50,100,'2026-11-02T00:00:00+00:00'))
  self.assertEqual(materials.collect(['small']),['small'])
 def test_baseline_not_replaced_by_later_stats(self):
  materials.collect(['big'])
  with app.connect() as con:con.execute("UPDATE videos SET views=3000000 WHERE id='big'")
  materials.save({'video_id':'big','angle':'다른 해석'})
  self.assertEqual(materials.state()['materials']['big']['baseline']['views'],2000000)
 def test_ready_requires_source_verification_permission_evidence_and_angle(self):
  materials.save({'video_id':'big','rights':'permission','angle':'해설','evidence':'제작자 확인'})
  self.assertFalse(materials.state()['materials']['big']['ready'])
  with app.connect() as con:con.execute("UPDATE research SET status='verified' WHERE video_id='big'")
  self.assertTrue(materials.state()['materials']['big']['ready'])
  materials.save({'video_id':'big','rights':'unknown','angle':'해설','evidence':'문의'})
  self.assertFalse(materials.state()['materials']['big']['ready'])
 def test_common_source_is_manual_and_does_not_verify_ownership(self):
  for ident in ['big','other']:materials.save({'video_id':ident,'group_url':'https://www.tiktok.com/@a/video/123#x'})
  s=materials.state()['materials'];self.assertEqual(s['big']['same_source'],['other']);self.assertFalse(s['big']['ready'])
  with self.assertRaises(ValueError):materials.save({'video_id':'small','group_url':'javascript:alert(1)'})
 def test_new_project_keeps_full_brief_and_outcome_is_linked(self):
  materials.save({'video_id':'big','hypothesis':'호기심','angle':'새로운 설명'})
  channel=production.create_channel({'name':'channel'});project=production.create_project({'title':'version A','channel_id':channel,'source_video_id':'big'})
  doc=production.project(project)['document'];self.assertEqual(doc['material_brief']['angle'],'새로운 설명');self.assertIn('호기심',doc['context'])
  materials.outcome({'video_id':'big','project_id':project,'url':'https://www.youtube.com/shorts/output','views':0,'likes':''})
  s=materials.state()['materials']['big'];self.assertEqual(s['projects'][0]['id'],project);self.assertEqual(s['outcomes'][0]['views'],0);self.assertIsNone(s['outcomes'][0]['likes'])
  with self.assertRaises(ValueError):materials.outcome({'video_id':'small','project_id':project,'url':'https://example.com/output'})
 def test_bad_metrics_rejected(self):
  for value in [-1,'nan',1.5,True]:
   with self.subTest(value=value),self.assertRaises(ValueError):materials.number(value)
  with self.assertRaises(ValueError):materials.collect(['big'],min_ratio=float('nan'))
