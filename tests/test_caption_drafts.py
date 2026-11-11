import unittest

import caption_drafts
import production


class CaptionDraftTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            'product': '밀폐용기',
            'reference': '이걸 한 손으로 열어 봤어요.',
            'reference_url': 'https://example.com/reference',
            'scene': '한 손으로 뚜껑을 들어 올려요.',
            'scene_url': 'https://example.com/source',
            'reviews': '열기 편해요.\n열기 편해요.\n정리하기 좋아요.\n세척은 번거로워요.',
            'review_url': 'https://example.com/reviews',
            'same_product': True,
        }

    def test_drafts_preserve_sources_and_display_review_attribution(self):
        lines = caption_drafts.build(self.data)
        self.assertEqual([line['evidence']['kind'] for line in lines],
                         ['reference', 'scene', 'review', 'review'])
        self.assertNotEqual(lines[0]['text'], self.data['reference'])
        self.assertIn('한 손', lines[0]['text'])
        self.assertEqual(lines[-1]['text'], '세척은 번거로워요.')
        self.assertEqual(lines[-1]['sub'], '구매 후기 발췌')
        cues = production.cues([{**line, 'start': i * 2, 'end': i * 2 + 2}
                                for i, line in enumerate(lines)])
        self.assertEqual([cue['evidence'] for cue in cues], [line['evidence'] for line in lines])

    def test_reviews_require_matching_product_and_link(self):
        with self.assertRaisesRegex(ValueError, '같은 모델'):
            caption_drafts.build({**self.data, 'same_product': False})
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            caption_drafts.build({**self.data, 'review_url': 'javascript:alert(1)'})
        self.assertEqual(len(caption_drafts.build({**self.data, 'reviews': '', 'same_product': False})), 2)

    def test_old_cues_keep_their_previous_shape(self):
        [cue] = production.cues([{'text': '기존 자막'}])
        self.assertNotIn('evidence', cue)
