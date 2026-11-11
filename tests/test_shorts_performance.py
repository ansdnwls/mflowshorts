from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import production
import shorts_performance


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database=patch.object(app,'DB',Path(directory.name)/'db.sqlite')
        database.start()
        self.addCleanup(database.stop)
        app.init()
        self.channels=[production.create_channel({'name':name}) for name in ('A','B')]

    def record(self, day, views, *, channel=0, platform='youtube', angle='demo', bundle=None, clicks=None):
        ident=production.create_project({'title':f'영상 {day}','channel_id':self.channels[channel]})
        p=production.project(ident)
        p['document']['shorts_bundle']={'id':bundle or ident,'angle':angle}
        production.store(p)
        urls={'youtube':'https://youtu.be/example1234','tiktok':'https://www.tiktok.com/@fixture/video/123'}
        shorts_performance.save({'id':ident,'platform':platform,'url':urls[platform],
                                 'posted_on':f'2025-01-{day:02}','views':views,'clicks':clicks})
        return ident

    def test_baseline_uses_only_earlier_records_in_same_channel_and_platform(self):
        first=self.record(1,100)
        self.record(2,200)
        self.record(3,1000,channel=1)
        self.record(4,1000,platform='tiktok')
        self.record(5,300)
        self.record(6,1000,bundle='current')
        self.record(7,1000)
        selected=self.record(7,500,bundle='current',clicks=0)
        self.record(8,1000)
        records=shorts_performance.snapshot()['records']
        self.assertIsNone(records[first]['view_ratio'])
        self.assertIsNone(records[first]['clicks'])
        self.assertEqual(records[selected]['baseline_count'],3)
        self.assertEqual(records[selected]['view_ratio'],2.5)
        self.assertEqual(records[selected]['clicks'],0)

    def test_suggestion_compares_normalized_angles_and_requires_samples(self):
        for channel,baseline in ((0,100),(1,1000)):
            for day in (1,2,3):self.record(day,baseline,channel=channel)
        self.record(4,200,angle='review')
        self.record(5,250,angle='review')
        self.record(4,1100,channel=1,angle='guide')
        self.assertIsNone(shorts_performance.snapshot()['suggested'])
        self.record(5,1200,channel=1,angle='guide')
        self.assertEqual(shorts_performance.snapshot()['suggested']['angle'],'review')

    def test_rejects_mismatched_link_and_keeps_previous_values(self):
        ident=self.record(1,120)
        with self.assertRaisesRegex(ValueError,'HTTPS 링크'):
            shorts_performance.save({'id':ident,'platform':'tiktok','url':'https://youtu.be/example1234',
                                     'posted_on':'2025-01-01','views':0})
        self.assertEqual(shorts_performance.snapshot()['records'][ident]['views'],120)
