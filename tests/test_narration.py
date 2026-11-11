import io,tempfile,wave
from pathlib import Path
from unittest.mock import patch
import unittest
import app,production,narration

def wav_bytes(seconds=.2):
 data=io.BytesIO()
 with wave.open(data,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\x00\x00'*round(seconds*24000))
 return data.getvalue()

class VoiceTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=patch.object(app,'DB',Path(self.temp.name)/'db');self.root=patch.object(production,'PROJECT_ROOT',Path(self.temp.name)/'projects');self.db.start();self.root.start();app.init()
  c=production.create_channel({'name':'voice'});self.ident=production.create_project({'title':'voice','channel_id':c});p=production.project(self.ident);p['document']['source']={'filename':'source','duration':8,'audio':True};p['document']['script']='첫 장면입니다. 다음 장면입니다.';p['document']['settings']['duration']=4;production.store(p);production.approve(self.ident)
 def tearDown(self):self.root.stop();self.db.stop();self.temp.cleanup()
 def upload(self):
  body=wav_bytes();return narration.upload(self.ident,io.BytesIO(body),len(body))
 def test_unapproved_cannot_prepare(self):
  production.update_project({'id':self.ident,'script':'새로운 대본'})
  with self.assertRaises(ValueError):self.upload()
 def test_upload_is_not_claimed_as_aligned(self):
  ident=self.upload();a=narration.get(ident)
  self.assertEqual(a['data']['timing'],'manual_estimate');self.assertIsNone(production.project(self.ident)['document'].get('narration_id'))
 def test_activation_requires_confirmation_and_clears_render_approval(self):
  ident=self.upload();a=narration.get(ident)
  with self.assertRaises(ValueError):narration.activate(ident,a['data']['cues'],False)
  narration.activate(ident,a['data']['cues'],True);doc=production.project(self.ident)['document'];self.assertEqual(doc['narration_id'],ident);self.assertIsNone(doc['approved_hash'])
 def test_script_edit_invalidates_voice(self):
  ident=self.upload();a=narration.get(ident);narration.activate(ident,a['data']['cues'],True)
  production.update_project({'id':self.ident,'script':'바뀐 대본'})
  with self.assertRaises(ValueError):narration.for_render(production.project(self.ident))
  self.assertTrue(narration.state(self.ident)['narration']['stale'])
 def test_long_voice_is_not_cut(self):
  p=production.project(self.ident);p['document']['settings']['duration']=1;production.store(p);production.approve(self.ident)
  body=wav_bytes(2);ident=narration.upload(self.ident,io.BytesIO(body),len(body))
  with self.assertRaises(ValueError):narration.activate(ident,narration.get(ident)['data']['cues'],True)
 def test_file_change_is_rejected(self):
  ident=self.upload();narration.file(ident).write_bytes(b'changed')
  with self.assertRaises(ValueError):narration.file(ident)
 def test_measured_sentence_cues_from_real_wav_parts(self):
  a=narration.create(production.project(self.ident),'tts');narration.LOCK.acquire()
  with patch.object(narration,'speech',return_value=wav_bytes(.2)):narration.generate(a,production.project(self.ident),['첫 장면입니다.','다음 장면입니다.'])
  result=narration.get(a['id']);self.assertEqual(result['status'],'ready');self.assertEqual(result['data']['timing'],'measured_sentences');self.assertAlmostEqual(result['data']['cues'][1]['start'],.2);self.assertAlmostEqual(result['data']['duration'],.4)
 def test_bad_or_overlapping_cues_rejected(self):
  ident=self.upload()
  with self.assertRaises(ValueError):narration.activate(ident,[{'text':'one','start':0,'end':.15},{'text':'two','start':.1,'end':.2}],True)

 def test_caption_sized_chunks_preserve_script_without_highlight_markup(self):
  script='지금 *중요한 장면*을 살펴봅니다. 다음 장면에서는 다른 일이 일어납니다.'
  chunks=narration.chunks(script)
  self.assertEqual(''.join(chunks).replace(' ',''),script.replace('*','').replace(' ',''))
  self.assertTrue(all(len(c)<=60 for c in chunks))
