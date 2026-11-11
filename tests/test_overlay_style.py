import unittest
from unittest.mock import patch

import production


class OverlayStyleTests(unittest.TestCase):
    def test_legacy_overlay_retains_style_and_approval_identity(self):
        overlay={'text':'기존 글귀','x':0.4,'y':0.6,'size':72,'start':0,'end':0,'font':''}
        self.assertEqual(production.text_overlays([overlay]),[overlay])
        with patch.object(production,'ass_font',return_value=('Test Font',1)):
            events=list(production.overlay_events([overlay],3))
        self.assertEqual(len(events),1)
        self.assertIn(r'\c&H00FFFFFF&\3c&H00000000&\bord3',events[0])

    def test_two_distinct_styles_and_translucent_box_are_serialized(self):
        base={'text':'테스트','x':0.3,'y':0.4,'size':72,'start':0,'end':2,'font':''}
        items=production.text_overlays([
            {**base,'style':{'color':'#FF0000','outline_color':'#00FF00','outline_width':8,
                             'background_color':'#0000FF','background_opacity':50}},
            {**base,'x':0.7,'style':{'color':'#FFFF00','outline_width':0}},
        ])
        self.assertEqual(items[0]['style']['color'],'#ff0000')
        with patch.object(production,'ass_font',return_value=('Test Font',1)):
            box,first,second=production.overlay_events(items,3)
        self.assertIn(r'\1c&H00FF0000&\1a&H7F&',box)
        self.assertIn(r'\pos(324,768)',first)
        self.assertIn(r'\c&H000000FF&\3c&H0000FF00&\bord8',first)
        self.assertIn(r'\pos(756,768)',second)
        self.assertIn(r'\c&H0000FFFF&\3c&H00000000&\bord0',second)

    def test_rejects_unsafe_colors_and_invalid_dimensions(self):
        for value in ({'color':'red'},{'outline_color':r'#ff00ff}\bord0'},
                      {'outline_width':True},{'outline_width':21},{'background_opacity':float('nan')}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                production.overlay_style(value)
