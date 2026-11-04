import io
import shutil
import subprocess
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
        boxes=production.blurs([{'x':0.05,'y':0.6,'w':2,'h':0.1},{'x':0.5,'y':0.5,'w':0.001,'h':0.001},{'x':0,'y':0,'w':0.5,'h':0.1,'start':1,'end':3}])
        self.assertEqual(len(boxes),2)  # the tiny one is dropped
        self.assertAlmostEqual(boxes[0]['w'],0.95)  # clamped to the frame
        for bad in ('x',[{'x':'a'}],[{'x':0,'y':0,'w':0.5,'h':0.5,'start':3,'end':2}],[{'x':0,'y':0,'w':0.5,'h':0.5}]*7):
            with self.subTest(bad=bad),self.assertRaises(ValueError):production.blurs(bad)
        graph=production.blur_graph('scale=1080:1920',boxes,5)
        self.assertEqual(graph.count('boxblur='),2)
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
        boxes=production.blurs([{'x':0.05,'y':0.6,'w':0.9,'h':0.1},{'x':0.1,'y':0.3,'w':0.3,'h':0.05,'start':0.2,'end':0.6}])
        graph=production.blur_graph(','.join(production.layout_filters('band')),boxes,1)+',drawbox=x=0:y=0:w=iw:h=10:color=black:t=fill'
        result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=640x360:rate=10','-t','1','-vf',graph,'-f','null','-'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
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

    def test_unexpected_render_error_is_recoverable(self):
        self.add_source();production.update_project({'id':self.ident,'script':'대본'});production.approve(self.ident)
        with patch.object(production,'state',return_value={'render_available':True}),patch.object(production.threading,'Thread') as thread:
            production.start_render(self.ident)
        p=thread.call_args.kwargs['args'][0]
        with patch.object(production,'STYLES',{}),patch('traceback.print_exc'):production.render(p)
        self.assertEqual(production.project(self.ident)['status'],'error')
        self.assertFalse(production.LOCK.locked())
        self.assertEqual([x for x in (production.PROJECT_ROOT/self.ident).iterdir() if x.is_dir()],[])
        production.update_project({'id':self.ident,'script':'수정한 대본'})
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

