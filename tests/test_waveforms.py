from array import array
from pathlib import Path
import shutil
import tempfile
import unittest
import wave

import production
import waveforms


class WaveformTests(unittest.TestCase):
    def test_range_validation(self):
        for start,duration in [(-1,1),(0,0),(0,61),(float('inf'),1),(0,float('nan'))]:
            with self.subTest(start=start,duration=duration), self.assertRaises(ValueError):
                waveforms.peaks('unused','wav',start,duration)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
    def test_window_and_bin_times_match_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'tone.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setparams((1,2,8000,0,'NONE','not compressed'))
                wav.writeframes(array('h',[0]*4000+[12000,-12000]*2000+[0]*4000).tobytes())
            data=waveforms.peaks(path,'wav',0.4,0.8)
        self.assertEqual(data['step'],0.02)
        self.assertEqual(len(data['peaks']),40)
        self.assertEqual(data['peaks'][2],[0,0])
        self.assertGreater(data['peaks'][8][1],0.3)
        self.assertEqual(data['peaks'][32],[0,0])

    def test_fixed_text_timing_persists_without_changing_legacy_records(self):
        cue={'text':'고정 자막','start':1,'end':2}
        overlay={**cue,'x':0.5,'y':0.5,'size':72,'font':''}
        for normalize,item in ((production.cues,cue),(production.text_overlays,overlay)):
            self.assertNotIn('fixed',normalize([item])[0])
            self.assertTrue(normalize([{**item,'fixed':True}])[0]['fixed'])
