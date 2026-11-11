import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import app
import manual_timeline
import production


def ffmpeg(*args):
    return subprocess.run(['ffmpeg','-v','error','-nostdin','-y',*map(str,args)],
                          check=True,capture_output=True,timeout=60).stdout


class ClipGradeTests(unittest.TestCase):
    def test_grade_validation_preserves_legacy_and_approval(self):
        clip={'start':0,'end':2}
        self.assertEqual(manual_timeline.clips([{**clip,'grade':{}}],2),[clip])
        with self.assertRaises(ValueError):
            manual_timeline.clips([{**clip,'grade':{'contrast':float('nan')}}],2)
        with self.assertRaises(ValueError):
            manual_timeline.clips([{**clip,'grade':{'saturation':True}}],2)
        graded=manual_timeline.clips([{**clip,'grade':{'brightness':12,'saturation':-100}}],2)
        self.assertEqual(graded[0]['grade'],{'brightness':12,'contrast':0,'saturation':-100,'warmth':0})
        self.assertNotEqual(production.fingerprint({'clips':[clip]}),production.fingerprint({'clips':graded}))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                         'FFmpeg and production font required')
    def test_cropped_mirrored_grade_matches_exact_preview_and_preserves_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(app,'DB',root/'check.sqlite'),patch.object(production,'PROJECT_ROOT',root/'projects'):
                app.init()
                channel=production.create_channel({'name':'보정 검증','language':'ko'})
                ident=production.create_project({'title':'보정 검증','channel_id':channel})
                source=root/'stripes.mp4'
                ffmpeg('-f','lavfi','-i','color=0x204090:s=320x240:r=30:d=3',
                       '-vf','drawbox=x=0:y=0:w=160:h=240:color=0x982525:t=fill',
                       '-c:v','libx264',source)
                content=source.read_bytes()
                production.upload(ident,io.BytesIO(content),len(content))
                frame={'mode':'fill','zoom':1.18,'x':32,'y':-18}
                first={'start':0,'end':1.5,'framing':frame,
                       'grade':{'brightness':9,'contrast':8,'saturation':14,'warmth':9}}
                second={'start':1.5,'end':3,'framing':frame}
                overlay={'text':'글귀','start':0,'end':0,'x':0.5,'y':0.60,'size':72,'font':'',
                         'style':{'background_color':'#22ee44','background_opacity':100}}
                production.update_project({'id':ident,'clips':[first,second],'overlays':[overlay],
                    'cues':[{'text':'자막','start':0,'end':0}],
                    'settings':{**production.default_settings(),'layout':'fill','mirror':True}})
                self.assertEqual(production.project(ident)['document']['clips'][0],first)
                preview=root/'frame.png'
                preview.write_bytes(production.preview_frame(ident,0.5))
                production.approve(ident)
                production.start_render(ident)
                self.assertTrue(production.process_next_render())
                final=production.asset(ident,'output')

                def rgb(path,time,x,y):
                    return ffmpeg('-ss',time,'-i',path,'-frames:v',1,'-vf',
                                  f'crop=2:2:iw*{x}:ih*{y},format=rgb24','-f','rawvideo','-')[:3]
                for x in (.25,.75):
                    before=rgb(preview,0,x,.45)
                    after=rgb(final,.5,x,.45)
                    self.assertTrue(all(abs(a-b)<=8 for a,b in zip(before,after)),(before,after))
                left,right=rgb(final,.5,.25,.45),rgb(final,.5,.75,.45)
                self.assertGreater(left[2],left[0])
                self.assertGreater(right[0],right[2])
                self.assertGreater(sum(abs(a-b) for a,b in zip(left,rgb(final,2,.25,.45))),15)
                # Opaque green text background remains the same above both graded and original footage.
                a,b=rgb(final,.5,.435,.60),rgb(final,2,.435,.60)
                self.assertGreater(a[1],180)
                self.assertTrue(all(abs(i-j)<=6 for i,j in zip(a,b)),(a,b))
