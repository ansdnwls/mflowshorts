import unittest

import manual_timeline as timeline


class ManualTimelineTests(unittest.TestCase):
    def test_highlight_first_maps_output_to_reordered_source(self):
        cuts=timeline.clips([{'start':5,'end':7},{'start':1,'end':2}],10)
        self.assertEqual(timeline.duration(cuts),3)
        self.assertEqual([timeline.source_time(cuts,t) for t in (0,1.5,2,2.5)], [5,6.5,1,1.5])

    def test_invalid_cuts_do_not_make_a_render_timeline(self):
        for cut in ({'start':0,'end':0.05},{'start':2,'end':1},
                    {'start':0,'end':11},{'start':float('nan'),'end':3}):
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                timeline.clips([cut],10)
        with self.assertRaises(ValueError):
            timeline.clips([{'start':0,'end':31}]*2,40)

    def test_quote_track_is_ordered_and_disallows_overlap(self):
        later={'text':'두 번째','start':2,'end':0}
        earlier={'text':'첫 번째','start':0,'end':2}
        self.assertEqual(timeline.quotes([later,earlier]),[earlier,later])
        with self.assertRaisesRegex(ValueError,'겹칩니다'):
            timeline.quotes([later,{**earlier,'end':3}])
