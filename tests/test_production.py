import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import app
import production

class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=patch.object(app,'DB',Path(self.temp.name)/'db.sqlite');self.db.start();self.root=patch.object(production,'PROJECT_ROOT',Path(self.temp.name)/'projects');self.root.start();app.init()
        self.channel=production.create_channel({'name':'테스트 채널','language':'ko'})
        self.ident=production.create_project({'title':'테스트 프로젝트','channel_id':self.channel})
    def tearDown(self):self.root.stop();self.db.stop();self.temp.cleanup()
    def add_source(self):
        with patch.object(production,'probe',return_value={'duration':20,'width':1920,'height':1080,'audio':True}):production.upload(self.ident,io.BytesIO(b'source'),6)
    def test_existing_main_narration_is_checked_and_invalidated(self):
        self.add_source()
        production.update_project({'id': self.ident, 'script': '원래 대본'})
        project = production.project(self.ident)
        doc = project['document']
        voice_id = 'b' * 32
        doc['narration_id'] = voice_id
        production.store(project)
        voice = production.PROJECT_ROOT / self.ident / 'narration' / voice_id / 'voice.wav'
        voice.parent.mkdir(parents=True)
        voice.write_bytes(b'legacy WAV')
        payload = {'script': doc['script'], 'source': doc['source'],
                   'start': doc['settings']['start'], 'duration': doc['settings']['duration']}
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        data = {'duration': 1.0, 'sha256': hashlib.sha256(voice.read_bytes()).hexdigest()}
        with app.connect() as con:
            con.execute('CREATE TABLE narrations (id, project_id, signature, status, data)')
            con.execute('INSERT INTO narrations VALUES (?, ?, ?, ?, ?)',
                        (voice_id, self.ident, signature, 'ready', json.dumps(data)))
        self.assertEqual(production.legacy_narration(project, doc, 2), (voice.resolve(), 1.0))
        voice.write_bytes(b'changed WAV')
        with self.assertRaises(ValueError):
            production.legacy_narration(project, doc, 2)
        production.update_project({'id': self.ident, 'script': '바뀐 대본'})
        self.assertNotIn('narration_id', production.project(self.ident)['document'])

    def test_approval_required_and_changes_revoke_approval(self):
        with self.assertRaises(ValueError):production.start_render(self.ident)
        self.add_source();production.update_project({'id':self.ident,'script':'사실을 확인한 새 대본'})
        production.approve(self.ident)
        self.assertEqual(production.project(self.ident)['status'],'approved')
        self.assertIsNotNone(production.project(self.ident)['document']['approved_hash'])
        production.update_project({'id':self.ident,'headline':'변경한 제목'})
        self.assertIsNone(production.project(self.ident)['document']['approved_hash'])
        with self.assertRaises(ValueError):production.start_render(self.ident)
    def test_template_applies_to_next_project(self):
        cfg={**production.default_settings(),'style':'news','duration':30,'mirror':True}
        production.update_project({'id':self.ident,'settings':cfg,'save_template':True})
        next_id=production.create_project({'title':'다음 영상','channel_id':self.channel})
        self.assertEqual(production.project(next_id)['document']['settings'],cfg)
    def test_bad_upload_preserves_previous_source(self):
        self.add_source();old=production.project(self.ident)['document']['source']['filename']
        with patch.object(production,'probe',side_effect=ValueError('invalid')):
            with self.assertRaises(ValueError):production.upload(self.ident,io.BytesIO(b'bad'),3)
        self.assertEqual(production.project(self.ident)['document']['source']['filename'],old)
        self.assertEqual(len(list((production.PROJECT_ROOT/self.ident).iterdir())),1)
    def test_nonfinite_settings_rejected(self):
        for key,value in [('duration',float('nan')),('start',float('inf')),('duration',1000),('mirror','true')]:
            with self.subTest(key=key),self.assertRaises(ValueError):production.settings({key:value})
    def test_layout_setting(self):
        self.assertEqual(production.settings({})['layout'],'band')
        self.assertEqual(production.settings({'layout':'fit'})['layout'],'fit')
        with self.assertRaises(ValueError):production.settings({'layout':'stretch'})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and Path(production.FONT).is_file(),
                         'needs FFmpeg and a render font')
    def test_clip_framing_survives_reordering_and_matches_rendered_frames(self):
        source = Path(self.temp.name) / 'landscape.mp4'
        subprocess.run([
            'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=640x360:r=30:d=2',
            '-vf', "drawbox=x=320:w=320:h=360:c=blue:t=fill,"
                   "drawbox=w=320:h=360:c=lime:t=fill:enable='gte(t,1)',"
                   "drawbox=x=320:w=320:h=360:c=yellow:t=fill:enable='gte(t,1)'",
            '-pix_fmt', 'yuv420p', str(source),
        ], check=True, capture_output=True)
        with source.open('rb') as reader:
            production.upload(self.ident, reader, source.stat().st_size)
        first = {'start': 0, 'end': 1, 'framing': {'mode': 'fill', 'zoom': 1.2, 'x': 350, 'y': -30}}
        second = {'start': 1, 'end': 2, 'framing': {'mode': 'fit', 'zoom': 1.1, 'x': -180, 'y': 140}}
        production.update_project({'id': self.ident, 'clips': [first, second]})
        saved = production.project(self.ident)['document']['clips']
        production.update_project({'id': self.ident, 'clips': saved[::-1]})
        self.assertEqual(production.project(self.ident)['document']['clips'], [second, first])
        production.approve(self.ident)
        approved=production.project(self.ident)
        self.assertFalse(production.update_project({'id':self.ident,'clips':[second,first]}))
        self.assertEqual(production.project(self.ident),approved)
        with patch.object(production, 'state', return_value={'render_available': True}):
            production.start_render(self.ident)
        self.assertTrue(production.process_next_render())
        self.assertEqual(production.project(self.ident)['status'], 'completed')
        output = production.asset(self.ident, 'output')
        self.assertEqual(production.probe(output)['duration'], 2)

        def pixel(args, x, y):
            return subprocess.check_output([
                'ffmpeg', '-v', 'error', *args, '-vf', f'format=rgb24,crop=1:1:{x}:{y}',
                '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-',
            ])

        # In output order the centered subjects are yellow, then red.
        # At y=720 the shifted fit clip is still in its black border.
        for time, center, upper in ((0.5, (255, 255, 0), (0, 0, 0)),
                                    (1.5, (255, 0, 0), (255, 0, 0))):
            preview = Path(self.temp.name) / f'preview-{time}.png'
            preview.write_bytes(production.preview_frame(self.ident, time))
            for y, expected in ((900, center), (720, upper)):
                samples = (
                    pixel(['-i', str(preview)], 270, y // 2),
                    pixel(['-ss', str(time), '-i', str(output)], 540, y),
                )
                for sample in samples:
                    self.assertEqual(len(sample), 3)
                    self.assertTrue(all(abs(a-b) < 15 for a,b in zip(sample, expected)), (time, y, sample))

        completed=production.project(self.ident)
        archive=production.asset(self.ident,'archive')
        draft_preview=Path(self.temp.name)/'unsaved.png'
        draft_preview.write_bytes(production.preview_frame(self.ident,0.5,{'clips':[first,second]}))
        self.assertGreater(pixel(['-i',str(draft_preview)],270,450)[0],240)
        self.assertLess(pixel(['-i',str(draft_preview)],270,450)[1],15)
        self.assertFalse(production.update_project({'id':self.ident,'clips':[second,first],
            'cues':[],'overlays':[],'audio_mix':None,'credit_on':True}))
        self.assertEqual(production.project(self.ident),completed)
        self.assertEqual(production.asset(self.ident,'output'),output)
        self.assertEqual(production.asset(self.ident,'archive'),archive)

        for change in ({'cues':[{'text':'새 자막','start':0}]},
                       {'clips':[first,second]}, {'audio_mix':{'source_volume':50}}):
            with self.subTest(change=change):
                self.assertTrue(production.update_project({'id':self.ident,**change}))
                updated=production.project(self.ident)
                self.assertIsNone(updated['document']['approved_hash'])
                self.assertNotIn('output',updated['document'])
                self.assertNotIn('archive',updated['document'])
                production.approve(self.ident)
                self.assertEqual(production.project(self.ident)['status'],'approved')
    def test_layout_keeps_video_clear_of_title_band(self):
        area=production.VIDEO_BOTTOM-production.TITLE_HEIGHT
        band,fill,fit=(','.join(production.layout_filters(name)) for name in ('band','fill','fit'))
        self.assertIn(f'crop=1080:{area}',band);self.assertIn(f'pad=1080:1920:0:{production.TITLE_HEIGHT}:black',band)
        self.assertIn(f'pad=1080:1920:0:{production.TITLE_HEIGHT}',fill)
        self.assertIn(f'scale=1080:{area}:force_original_aspect_ratio=decrease',fit)
    def test_subtitles_two_color_title_and_highlight(self):
        style=production.STYLES['basic']
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,srt=production.subtitles({'headline':'상단 문구 첫 줄 둘째 줄은 미완결','script':'나레이션 *예시 문장* 입니다. 다음 문장'},style,10,True)
        title=next(line for line in ass.splitlines() if ',Title,' in line and line.startswith('Dialogue'))
        self.assertIn(production.ass_color(style['title'])+'}상단 문구 첫 줄',title)
        self.assertIn(production.ass_color(style['title2'])+'}둘째 줄은 미완결',title)
        self.assertIn(production.ass_color(style['highlight'])+'}예시',ass)
        self.assertEqual(ass.count(',Caption,,'),2)
        self.assertNotIn('*',srt)
        self.assertIn('나레이션 예시 문장',srt)
    def test_subtitle_text_cannot_inject_ass_tags(self):
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,_=production.subtitles({'headline':'{\\pos(0,0)}제목','script':'대본'},production.STYLES['basic'],5,False)
        self.assertNotIn('{\\pos(0,0)}',ass)
        self.assertNotIn(',Caption,,',ass)
    def test_timed_overlays_persist_and_render_independently_of_captions(self):
        self.add_source()
        item={'text':r'{\pos(0,0)} 글귀','x':.25,'y':.75,'size':64,'start':1.25,'end':4,'font':''}
        production.update_project({'id':self.ident,'script':'대본','overlays':[item]})
        production.approve(self.ident)
        doc=production.project(self.ident)['document']
        self.assertEqual(doc['overlays'],[item])
        self.assertNotEqual(production.fingerprint(doc),production.fingerprint({**doc,'overlays':[]}))
        legacy={key:value for key,value in doc.items() if key!='overlays'}
        self.assertEqual(production.fingerprint(legacy),production.fingerprint({**legacy,'overlays':[]}))
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,srt=production.subtitles(doc,production.STYLES['basic'],3,False)
        self.assertIn(r'Dialogue: 3,0:00:01.25,0:00:03.00,Caption,,0,0,0,,{\an5\pos(270,1440)\fnTest Font\fs64\b1',ass)
        self.assertIn('｛＼pos(0,0)｝ 글귀',ass)
        self.assertNotIn('글귀',srt)
        for key,bad in [('x',float('nan')),('size',200),('end',1),('font','../font.ttf')]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                production.update_project({'id':self.ident,'overlays':[{**item,key:bad}]})
        self.assertEqual(production.project(self.ident)['document']['approved_hash'],doc['approved_hash'])
    def test_custom_fonts_from_fonts_folder(self):
        fonts=Path(self.temp.name)/'fonts';fonts.mkdir();(fonts/'Display.ttf').write_bytes(b'x');(fonts/'notes.txt').write_text('x')
        with patch.object(production,'FONTS_DIR',fonts),patch.object(production,'font_info',return_value=('Display Font',400)):
            self.assertEqual(production.available_fonts(),[{'file':'Display.ttf','name':'Display Font'}])
            self.assertEqual(production.settings({'title_font':'Display.ttf'})['title_font'],'Display.ttf')
            for bad in ('Missing.ttf','../db.sqlite','notes.txt',3):
                with self.subTest(bad=bad),self.assertRaises(ValueError):production.settings({'caption_font':bad})
            ass,_=production.subtitles({'headline':'제목','script':'대본'},production.STYLES['basic'],5,True,fonts/'Display.ttf',fonts/'Display.ttf')
        self.assertIn('Style: Title,Display Font,',ass)
        self.assertIn('&H00000000&,0,0,0,0,100',ass)  # regular-weight display font is not fake-bolded
    def test_comment_cards_and_credit(self):
        cards=production.comments([{'name':'봄날','when':'15시간 전','text':' 와  반응 미쳤다 ','at':'3'},{'text':'','at':0},{'name':'','when':'','text':'처음엔 몰랐어요','at':0}])
        self.assertEqual([c['text'] for c in cards],['처음엔 몰랐어요','와 반응 미쳤다'])
        for bad in ([{'text':'가'*81}],[{'text':'a','at':'x'}],[{'text':'a','at':99}],'nope'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):production.comments(bad)
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,_=production.subtitles({'headline':'제목','script':'대본','credit':'에이티즈 - Drip','comments':cards},production.STYLES['basic'],10,True)
        self.assertIn(',Credit,,',ass);self.assertIn('에이티즈 - Drip',ass)
        self.assertIn('0:00:00.00,0:00:03.00,Comment,,',ass);self.assertNotIn('0:00:00.00,0:00:03.00,CommentName',ass)  # first card has no name line
        self.assertIn('@봄날 · 15시간 전',ass)
        self.assertEqual(ass.count(',Card,,'),2)
    def test_credit_and_comments_keep_old_approvals(self):
        doc={'source':{'filename':'a.mp4'},'script':'s','headline':'h','settings':production.default_settings()}
        self.assertEqual(production.fingerprint(doc),production.fingerprint({**doc,'credit':'','comments':[]}))
        self.assertNotEqual(production.fingerprint(doc),production.fingerprint({**doc,'credit':'출처'}))
    def test_credit_can_be_switched_off(self):
        doc={'headline':'제목','script':'대본','credit':'출처. 채널','credit_on':False}
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,_=production.subtitles(doc,production.STYLES['basic'],10,True)
        self.assertNotIn(',Credit,,',ass)
        base={'source':{'filename':'a.mp4'},'script':'s','headline':'h','settings':production.default_settings(),'credit':'출처'}
        self.assertEqual(production.fingerprint(base),production.fingerprint({**base,'credit_on':True}))
        self.assertNotEqual(production.fingerprint(base),production.fingerprint({**base,'credit_on':False}))
        production.update_project({'id':self.ident,'credit':'출처. 채널','credit_on':False})
        doc=production.project(self.ident)['document']
        self.assertEqual((doc['credit'],doc['credit_on']),('출처. 채널',False))  # text kept for switching back on
    def test_position_offsets(self):
        doc={'headline':'제목','script':'대본','credit':'출처','comments':[{'name':'a','when':'','text':'댓글','at':0}]}
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            base,_=production.subtitles(doc,production.STYLES['basic'],10,True)
            moved,_=production.subtitles(doc,production.STYLES['basic'],10,True,offsets={'title_y':10,'credit_y':-10,'caption_y':-100,'comment_y':20})
        px=production.px
        def y(ass,style):
            import re
            return int(re.search(r','+style+r',,0,0,0,,\{\\(?:an7\\)?pos\(\d+,(\d+)\)',ass).group(1))
        self.assertEqual(y(moved,'Title')-y(base,'Title'),px(10))
        self.assertEqual(y(moved,'Credit')-y(base,'Credit'),px(-10))
        self.assertEqual(y(moved,'Caption')-y(base,'Caption'),px(-100))
        self.assertEqual(y(moved,'Card')-y(base,'Card'),px(20))
        cfg=production.settings({'title_y':'15','comment_y':-40.4})
        self.assertEqual((cfg['title_y'],cfg['comment_y'],cfg['caption_y']),(15,-40,0))
        for bad in (301,-301,'x',float('nan')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):production.settings({'caption_y':bad})
    def test_blur_boxes(self):
        boxes=production.blurs([{'x':0.05,'y':0.6,'w':2,'h':0.1},{'x':0.5,'y':0.5,'w':0.001,'h':0.001},{'x':0,'y':0,'w':0.5,'h':0.1,'start':1,'end':3,'mode':'blur'}])
        self.assertEqual([b['mode'] for b in boxes],['erase','blur'])
        self.assertEqual(len(boxes),2)  # the tiny one is dropped
        self.assertAlmostEqual(boxes[0]['w'],0.95)  # clamped to the frame
        for bad in ('x',[{'x':'a'}],[{'x':0,'y':0,'w':0.5,'h':0.5,'start':3,'end':2}],[{'x':0,'y':0,'w':0.5,'h':0.5}]*7,[{'x':0,'y':0,'w':0.5,'h':0.5,'mode':'pixelate'}]):
            with self.subTest(bad=bad),self.assertRaises(ValueError):production.blurs(bad)
        graph=production.blur_graph('scale=1080:1920',boxes,5)
        self.assertEqual(graph.count('delogo='),1);self.assertEqual(graph.count('boxblur='),1);self.assertEqual(graph.count('alphamerge'),2)
        self.assertIn("enable='between(t,1.0,3.0)'",graph);self.assertIn("enable='between(t,0.0,6)'",graph)
        self.assertTrue(graph.endswith('[v2]null'))
        self.assertEqual(production.blur_graph('scale=1080:1920',[],5),'scale=1080:1920')
        base={'source':{'filename':'a.mp4'},'script':'s','headline':'h','settings':production.default_settings()}
        self.assertEqual(production.fingerprint(base),production.fingerprint({**base,'blurs':[]}))
        self.assertNotEqual(production.fingerprint(base),production.fingerprint({**base,'blurs':boxes}))
        production.update_project({'id':self.ident,'blurs':boxes})
        self.assertEqual(production.project(self.ident)['document']['blurs'],boxes)
    @unittest.skipUnless(shutil.which('ffmpeg'),'needs ffmpeg')
    def test_blur_graph_runs_in_ffmpeg(self):
        boxes=production.blurs([{'x':0.05,'y':0.6,'w':0.9,'h':0.1},{'x':0.1,'y':0.3,'w':0.3,'h':0.05,'start':0.2,'end':0.6,'mode':'blur'},{'x':0,'y':0,'w':1,'h':0.02},{'x':0.9,'y':0.97,'w':0.1,'h':0.03,'mode':'blur'}])  # edge boxes too
        graph=production.blur_graph(','.join(production.layout_filters('band')),boxes,1)+',drawbox=x=0:y=0:w=iw:h=10:color=black:t=fill'
        result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=640x360:rate=10','-t','1','-vf',graph,'-f','null','-'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    @unittest.skipUnless(shutil.which('ffmpeg'),'needs ffmpeg')
    def test_preview_frame_png(self):
        source=Path(self.temp.name)/'src.mp4'
        subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','testsrc=size=320x568:rate=10','-t','2','-pix_fmt','yuv420p',str(source)],check=True)
        with source.open('rb') as f:production.upload(self.ident,f,source.stat().st_size)
        production.update_project({'id':self.ident,'blurs':[{'x':0.1,'y':0.6,'w':0.8,'h':0.05}]})
        png=production.preview_frame(self.ident,'1')
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertTrue(production.preview_frame(self.ident,'nan').startswith(b'\x89PNG'))
    def test_caption_timeline(self):
        items=production.cues([{'text':'둘째','start':3,'pos':'middle','look':'box','y':10},{'text':' 첫 *줄* ','sub':'first line','start':0,'pos':'top','look':'accent'},{'text':'','start':5}])
        self.assertEqual([c['text'] for c in items],['첫 *줄*','둘째'])
        for bad in ([{'text':'a','start':70}],[{'text':'a','start':3,'end':2}],[{'text':'a','pos':'left'}],[{'text':'a','look':'neon'}],[{'text':'가'*61}],'x'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):production.cues(bad)
        doc={'headline':'제목','script':'이 대본은 쓰이지 않아요','cues':items}
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,srt=production.subtitles(doc,production.STYLES['basic'],10,True)
            hidden,_=production.subtitles(doc,production.STYLES['basic'],10,False)
        self.assertIn('0:00:00.00,0:00:03.00,CaptionAccent,,0,0,0,,{\\an8\\pos(540,510)}',ass)
        self.assertIn(f'0:00:03.00,0:00:11.00,CaptionBox,,0,0,0,,{{\\an5\\pos(540,{975+production.px(10)})}}둘째',ass)
        self.assertIn('first line',ass);self.assertNotIn('쓰이지',ass)
        self.assertIn('첫 줄\nfirst line',srt);self.assertIn('00:00:03,000 --> 00:00:10,000',srt)
        self.assertNotIn('CaptionBox,,',hidden)
        base={'source':{'filename':'a.mp4'},'script':'s','headline':'h','settings':production.default_settings()}
        self.assertEqual(production.fingerprint(base),production.fingerprint({**base,'cues':[]}))
        self.assertNotEqual(production.fingerprint(base),production.fingerprint({**base,'cues':items}))
    def test_caption_style_overrides_survive_save_and_keep_outline_with_background(self):
        self.add_source()
        style={'size':82,'color':'#00ff00','outline':8,'outline_color':'#ff00ff','background':'#112233'}
        items=[{'text':'First','start':0.2,'end':1.13,'pos':'middle','style':style},
               {'text':'Second','start':1.13,'end':2,'pos':'top'}]
        production.update_project({'id':self.ident,'cues':items})
        doc=production.project(self.ident)['document']
        self.assertEqual(doc['cues'][0]['style'],style)
        self.assertNotIn('style',doc['cues'][1])
        with patch.object(production,'font_info',return_value=('Test Font',700)):
            ass,srt=production.subtitles(doc,production.STYLES['basic'],4,True)
        self.assertIn('Style: Cue0,Test Font,82,&H0000ff00&,&H000000FF&,&H00ff00ff&,&H00332211&',ass)
        self.assertIn(',4,8,6,2,0,0,0,1',ass.split('Style: Cue0,')[1].splitlines()[0])
        self.assertIn('0:00:00.20,0:00:01.13,Cue0',ass)
        self.assertIn('00:00:01,130 --> 00:00:02,000\nSecond',srt)
        for invalid in ({'size':145},{'outline':True},{'background':'red'},{'font':'../missing.ttf'}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                production.cues([{'text':'First','style':invalid}])

    def test_rendering_project_cannot_be_edited(self):
        p=production.project(self.ident);p['status']='rendering';production.store(p)
        with self.assertRaises(ValueError):production.update_project({'id':self.ident,'script':'edit'})
        with self.assertRaises(ValueError):production.upload(self.ident,io.BytesIO(b'new'),3)
    def test_start_beyond_source_cannot_be_approved(self):
        self.add_source();production.update_project({'id':self.ident,'script':'대본','settings':{'start':25}})
        with self.assertRaises(ValueError):production.approve(self.ident)

    def test_discovery_notes_transfer_to_project(self):
        app.save_video({'id':'candidate','snippet':{'title':'source','channelTitle':'author','publishedAt':'2026-11-01T00:00:00Z'},'statistics':{}},'2026-11-04T00:00:00+00:00')
        with app.connect() as con:con.execute("INSERT INTO research(video_id,notes,attribution) VALUES ('candidate','실제 맥락과 새 해석','@author')")
        ident=production.create_project({'title':'제작으로 연결','channel_id':self.channel,'source_video_id':'candidate'})
        doc=production.project(ident)['document']
        self.assertEqual(doc['source_url'],'https://www.youtube.com/watch?v=candidate')
        self.assertIn('실제 맥락과 새 해석',doc['context'])
        self.assertEqual(doc['script'],'')
        self.assertIsNone(doc['approved_hash'])

    def test_letters_mode_needs_opencv(self):
        self.assertEqual(production.blurs([{'x':0.1,'y':0.6,'w':0.8,'h':0.05,'mode':'text'}])[0]['mode'],'text')
        with patch.object(production,'letters_ready',return_value=False),self.assertRaisesRegex(ValueError,'install_windows'):
            production.erase_letters({'mode':'frame'})
        self.assertIn('letters_ready',production.state());self.assertIn('names',production.state()['gpu'])
    @unittest.skipUnless(shutil.which('ffmpeg') and os.environ.get('MFLOW_TEST_CV_PYTHON'),'needs ffmpeg and a Python with OpenCV (MFLOW_TEST_CV_PYTHON)')
    def test_letters_mode_renders(self):
        source=Path(self.temp.name)/'src.mp4'
        subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','testsrc=size=540x960:rate=30','-f','lavfi','-i','sine','-t','2','-vf',"drawbox=x=100:y=600:w=340:h=40:color=white:t=fill",'-pix_fmt','yuv420p','-shortest',str(source)],check=True)
        with source.open('rb') as f:production.upload(self.ident,f,source.stat().st_size)
        cfg={**production.default_settings(),'duration':2,'layout':'fit'}
        production.update_project({'id':self.ident,'script':'대본','settings':cfg,'blurs':[{'x':0.1,'y':0.55,'w':0.8,'h':0.1,'mode':'text'},{'x':0.1,'y':0.3,'w':0.3,'h':0.05,'mode':'blur'}]})
        production.approve(self.ident)
        with patch.object(production,'MEDIA_PYTHON',Path(os.environ['MFLOW_TEST_CV_PYTHON'])),patch.dict(production._TOOL_CACHE,clear=True):
            self.assertTrue(production.preview_frame(self.ident,'1').startswith(b'\x89PNG'))
            p=production.project(self.ident);production.LOCK.acquire();production.render(p)
        p=production.project(self.ident)
        self.assertEqual(p['status'],'completed',p['document']['message'])
        info=production.probe(production.PROJECT_ROOT/self.ident/p['document']['output'])
        self.assertEqual((info['width'],info['height'],info['audio']),(1080,1920,True))
    AI_STUB=("import argparse,json,os,subprocess,sys\n"
             "a=argparse.ArgumentParser();a.add_argument('-i');a.add_argument('-o');a.add_argument('-c',action='append',nargs=4,type=int);a.add_argument('--inpaint-mode')\n"
             "args=a.parse_args();assert os.path.isfile('backend/main.py')\n"
             "open(os.environ['STUB_LOG'],'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
             "if os.environ.get('STUB_NO_TEXT'):print('No subtitles detected. Check file: x');sys.exit(1)\n"
             "draw=','.join('drawbox=x=%d:y=%d:w=%d:h=%d:color=gray:t=fill'%(c[2],c[0],c[3]-c[2],c[1]-c[0]) for c in args.c)\n"
             "subprocess.run(['ffmpeg','-loglevel','error','-y','-i',args.i,'-vf',draw]+(['-frames:v','1'] if args.o.endswith('.png') else ['-pix_fmt','yuv420p'])+[args.o],check=True)\n")
    def ai_stub(self):
        """A stand-in for video-subtitle-remover's command line, unpacked like data/vsr/<package>/backend/main.py."""
        root=Path(self.temp.name)/'vsr';(root/'pkg/backend').mkdir(parents=True);(root/'pkg/backend/main.py').write_text(self.AI_STUB,encoding='utf-8')
        return patch.object(production,'AI_ROOT',root),patch.dict(os.environ,{'MFLOW_VSR_PYTHON':sys.executable,'STUB_LOG':str(Path(self.temp.name)/'stub_args.json')}),patch.dict(production._TOOL_CACHE,clear=True)
    def test_ai_mode_needs_tool(self):
        self.assertEqual(production.blurs([{'x':0.1,'y':0.6,'w':0.8,'h':0.05,'mode':'ai'}])[0]['mode'],'ai')
        with patch.object(production,'AI_ROOT',Path(self.temp.name)/'none'),patch.dict(production._TOOL_CACHE,clear=True):
            self.assertIsNone(production.ai_eraser());self.assertFalse(production.state()['ai_ready'])
            with self.assertRaisesRegex(ValueError,'VSR'):production.erase_ai('a.mp4','b.mp4',[])
    @unittest.skipUnless(shutil.which('ffmpeg') and Path(production.FONT).is_file(),'needs ffmpeg and a Korean font')
    def test_ai_mode_renders(self):
        source=Path(self.temp.name)/'src.mp4'
        subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','testsrc=size=540x960:rate=30','-f','lavfi','-i','sine','-t','2','-pix_fmt','yuv420p','-shortest',str(source)],check=True)
        with source.open('rb') as f:production.upload(self.ident,f,source.stat().st_size)
        cfg={**production.default_settings(),'duration':2,'layout':'fit'}
        production.update_project({'id':self.ident,'script':'대본','settings':cfg,'blurs':[{'x':0.1,'y':0.55,'w':0.8,'h':0.1,'mode':'ai','start':0.5,'end':1.5},{'x':0.1,'y':0.3,'w':0.3,'h':0.05,'mode':'blur'}]})
        production.approve(self.ident)
        root,env,cache=self.ai_stub()
        with root,env,cache:
            self.assertTrue(production.state()['ai_ready'])
            png=production.preview_frame(self.ident,'1');self.assertTrue(png.startswith(b'\x89PNG'))
            self.assertTrue(production.preview_frame(self.ident,'1.8').startswith(b'\x89PNG'))  # box not active: tool not called
            p=production.project(self.ident);production.LOCK.acquire();production.render(p)
        p=production.project(self.ident)
        self.assertEqual(p['status'],'completed',p['document']['message'])
        run=(production.PROJECT_ROOT/self.ident/p['document']['output']).parent
        info=production.probe(run/'short.mp4')
        self.assertEqual((info['width'],info['height'],info['audio']),(1080,1920,True))
        self.assertEqual(production.probe(run/'laid.mp4')['width'],1080)
        calls=[json.loads(line) for line in (Path(self.temp.name)/'stub_args.json').read_text().splitlines()]
        self.assertEqual(len(calls),2)  # preview at 1s and the render; not the preview at 1.8s
        self.assertTrue(calls[0][calls[0].index('-i')+1].endswith('.png'));call=calls[1]
        self.assertTrue(call[call.index('-i')+1].startswith(str(Path(self.temp.name)/'vsr')))  # staged inside the tool folder
        self.assertEqual([x.name for x in (Path(self.temp.name)/'vsr').iterdir()],['pkg'])  # staging cleaned up
        self.assertEqual(call[call.index('-c')+1:],['1056','1248','108','972'])
        self.assertEqual(call[call.index('--inpaint-mode')+1],'sttn-det')
    @unittest.skipUnless(shutil.which('ffmpeg'),'needs ffmpeg')
    def test_ai_mode_without_text_keeps_video(self):
        source=Path(self.temp.name)/'in.mp4';output=Path(self.temp.name)/'out.mp4'
        subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','testsrc=size=108x192:rate=10','-t','1','-pix_fmt','yuv420p',str(source)],check=True)
        root,env,cache=self.ai_stub()
        with root,env,cache,patch.dict(os.environ,{'STUB_NO_TEXT':'1'}):
            production.erase_ai(source,output,[{'x':0,'y':0,'w':1,'h':0.1}])
        self.assertEqual(output.read_bytes(),source.read_bytes())
    def test_unexpected_render_error_is_recoverable(self):
        self.add_source();production.update_project({'id':self.ident,'script':'대본'});production.approve(self.ident)
        with patch.object(production,'state',return_value={'render_available':True}):
            production.start_render(self.ident)
        with patch.object(production,'STYLES',{}),patch('traceback.print_exc'):production.process_next_render()
        self.assertEqual(production.project(self.ident)['status'],'error')
        self.assertFalse(production.LOCK.locked())
        self.assertEqual([x for x in (production.PROJECT_ROOT/self.ident).iterdir() if x.is_dir()],[])
        production.update_project({'id':self.ident,'script':'수정한 대본'})

    def test_queue_deduplicates_and_claims_projects_in_order(self):
        self.add_source()
        production.update_project({'id':self.ident,'script':'대본'})
        production.approve(self.ident)
        other=production.create_project({'title':'두 번째','channel_id':self.channel})
        with patch.object(production,'probe',return_value={'duration':20,'width':640,'height':480,'audio':False}):
            production.upload(other,io.BytesIO(b'source'),6)
        production.update_project({'id':other,'script':'두 번째 대본'})
        production.approve(other)
        with patch.object(production,'state',return_value={'render_available':True}):
            first=production.start_render(self.ident)
            self.assertEqual(production.start_render(self.ident)['job_id'],first['job_id'])
            production.start_render(other)
        calls=[]
        def finish(project):
            calls.append(project['id'])
            project['status']='completed'
            production.store(project)
            production.LOCK.release()
        with patch.object(production,'render',side_effect=finish):
            self.assertTrue(production.process_next_render())
            self.assertTrue(production.process_next_render())
            self.assertFalse(production.process_next_render())
        self.assertEqual(calls,[self.ident,other])
        with app.connect() as con:
            jobs=con.execute('SELECT status,attempts FROM production_render_jobs').fetchall()
        self.assertEqual([tuple(job) for job in jobs],[('completed',1),('completed',1)])

    def test_restart_requeues_interrupted_render_and_bounds_recovery(self):
        self.add_source()
        production.update_project({'id':self.ident,'script':'대본'})
        production.approve(self.ident)
        with patch.object(production,'state',return_value={'render_available':True}):
            job=production.start_render(self.ident)
            with app.connect() as con:
                con.execute("UPDATE production_render_jobs SET status='running',attempts=1")
            app.init()
            production.recover_renders()
            self.assertEqual(production.start_render(self.ident)['job_id'],job['job_id'])
            self.assertEqual(production.start_render(self.ident)['status'],'queued')
            with app.connect() as con:
                con.execute("UPDATE production_render_jobs SET status='running',attempts=?", (production.MAX_RENDER_ATTEMPTS,))
            production.recover_renders()
            self.assertEqual(production.project(self.ident)['status'],'error')
            retry=production.start_render(self.ident)
            self.assertNotEqual(retry['job_id'],job['job_id'])
            self.assertEqual(retry['status'],'queued')
    def test_headline_must_fit_three_lines(self):
        with self.assertRaises(ValueError):production.update_project({'id':self.ident,'headline':'가'*55})
        production.update_project({'id':self.ident,'headline':'가'*54})
        long_id=production.create_project({'title':'긴 제목 '*20,'channel_id':self.channel})
        production.update_project({'id':long_id,'script':'대본'})
    def test_upload_does_not_block_other_edits(self):
        def slow_probe(path):
            self.assertFalse(production.CONFIG_LOCK._is_owned())
            return {'duration':20,'width':1920,'height':1080,'audio':True}
        with patch.object(production,'probe',side_effect=slow_probe):production.upload(self.ident,io.BytesIO(b'source'),6)
    def test_filter_path_escapes_windows_paths(self):
        self.assertEqual(production.filter_path('D:\\프로젝트\\font.ttf'),"'D\\:/프로젝트/font.ttf'")
        with self.assertRaises(ValueError):production.filter_path("/tmp/it's")
    def test_probe_decodes_output_as_utf8(self):
        out='{"format":{"duration":"3","format_name":"mov,mp4"},"streams":[{"codec_type":"video","width":640,"height":360}]}'
        with patch.object(production.subprocess,'run',return_value=production.subprocess.CompletedProcess([],0,stdout=out)) as run:
            self.assertEqual(production.probe('x')['width'],640)
        self.assertEqual(run.call_args.kwargs['encoding'],'utf-8')

