import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import app
import comment_picker
import production

NOW = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)

def thread(text, likes=0, name='@someone_cool', published='2026-11-04T00:00:00Z', replies=0):
    return {'snippet': {'totalReplyCount': replies, 'topLevelComment': {'snippet': {
        'textDisplay': text, 'authorDisplayName': name, 'likeCount': likes, 'publishedAt': published}}}}

class PickerTests(unittest.TestCase):
    def test_youtube_ids(self):
        for url in ('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3', 'https://youtu.be/dQw4w9WgXcQ',
                    'https://www.youtube.com/shorts/dQw4w9WgXcQ', 'https://m.youtube.com/watch?v=dQw4w9WgXcQ'):
            self.assertEqual(comment_picker.youtube_id(url), 'dQw4w9WgXcQ')
        for url in ('https://www.tiktok.com/@a/video/1', 'https://evil.example/watch?v=dQw4w9WgXcQ', '', None, 'https://www.youtube.com/watch?v=short'):
            self.assertEqual(comment_picker.youtube_id(url), '')
    def test_most_liked_first(self):
        result = comment_picker.from_youtube([thread('왜 그런 거지?', 5), thread('just a normal comment', 900), thread('진짜 웃기다 ㅋㅋ', 50)], NOW)
        self.assertEqual([c['likes'] for c in result['comments']], [900, 50, 5])
    def test_labels(self):
        self.assertEqual(comment_picker.time_ago('2026-11-04T00:00:00Z', NOW), '12시간 전')
        self.assertEqual(comment_picker.time_ago('2026-10-31T12:00:00Z', NOW), '4일 전')
        self.assertEqual(comment_picker.time_ago('2025-01-01T00:00:00Z', NOW), '1년 전')
        self.assertEqual(comment_picker.mask('@coolguy123'), '@co***')
        self.assertEqual(comment_picker.mask(''), '@viewer***')
        self.assertTrue(comment_picker.is_korean('이거 진짜 실화임? lol'))
        self.assertFalse(comment_picker.is_korean('wait what just happened 😂'))
    def test_ranking_filters_and_korean_split(self):
        items = [thread('nice', 999), thread('check https://spam.example now', 500), thread('😂😂😂', 300),
                 thread('왜 마지막에 웃는 거임?? ㅋㅋㅋ', 40), thread('wait why did he do that?', 900),
                 thread('this is a very long comment ' * 5, 5000), thread('왜 마지막에 웃는 거임?? ㅋㅋㅋ', 40)]
        result = comment_picker.from_youtube(items, NOW)
        texts = [c['text'] for c in result['comments']]
        self.assertEqual(len(texts), 3)  # spam, emoji-only, too-short and duplicate dropped
        self.assertEqual(result['korean'], 1)
        long = next(c for c in result['comments'] if c['likes'] == 5000)
        self.assertFalse(long['fits'])
        korean = next(c for c in result['comments'] if c['korean'])
        self.assertEqual((korean['name'], korean['when']), ('@so***', '12시간 전'))
        self.assertTrue(result['viral_kr'])  # 1 of 3 is Korean
        self.assertFalse(comment_picker.from_youtube([thread('wait what happened here?')] * 1 + [thread(f'english comment number {i}') for i in range(9)], NOW)['viral_kr'])

class SourceCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(app, 'DB', Path(self.temp.name)/'db.sqlite'); self.db.start()
        self.root = patch.object(production, 'PROJECT_ROOT', Path(self.temp.name)/'projects'); self.root.start()
        app.init()
        channel = production.create_channel({'name': '채널', 'language': 'ko'})
        self.ident = production.create_project({'title': '프로젝트', 'channel_id': channel})
    def tearDown(self): self.root.stop(); self.db.stop(); self.temp.cleanup()
    def test_requires_youtube_source_and_key(self):
        with self.assertRaisesRegex(ValueError, 'YouTube'): app.source_comments(self.ident)
        production.update_project({'id': self.ident, 'source_url': 'https://www.youtube.com/shorts/dQw4w9WgXcQ'})
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, 'YOUTUBE_API_KEY'): app.source_comments(self.ident)
    def test_fetches_two_pages_of_relevant_comments(self):
        production.update_project({'id': self.ident, 'source_url': 'https://youtu.be/dQw4w9WgXcQ'})
        calls = []
        def api(endpoint, params):
            calls.append((endpoint, dict(params)))
            if 'pageToken' in params: return {'items': [thread('두번째 페이지 댓글 ㅋㅋ', 3)]}
            return {'items': [thread('첫 페이지 댓글 진짜?', 10)], 'nextPageToken': 'next'}
        with patch.dict('os.environ', {'YOUTUBE_API_KEY': 'test'}), patch.object(app, 'youtube', side_effect=api):
            result = app.source_comments(self.ident)
        self.assertEqual([c[0] for c in calls], ['commentThreads', 'commentThreads'])
        self.assertEqual(calls[0][1]['videoId'], 'dQw4w9WgXcQ'); self.assertEqual(calls[0][1]['order'], 'relevance')
        self.assertEqual(result['total'], 2); self.assertEqual(result['korean'], 2)
        self.assertEqual(production.project(self.ident)['document']['comments'], [])  # nothing stored until picked
    def test_comments_disabled_message(self):
        body = json.dumps({'error': {'errors': [{'reason': 'commentsDisabled'}]}}).encode()
        error = urllib.error.HTTPError('https://x', 403, 'Forbidden', {}, io.BytesIO(body))
        with patch.dict('os.environ', {'YOUTUBE_API_KEY': 'test'}), patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(ValueError, '댓글이 꺼져'): app.youtube('commentThreads', {})
    def test_comment_indicator_in_project_state(self):
        item = {'id': 'dQw4w9WgXcQ', 'snippet': {'title': 't', 'channelTitle': 'c', 'publishedAt': '2026-11-01T00:00:00Z'}, 'statistics': {'viewCount': '100', 'commentCount': '42'}}
        app.save_video(item, '2026-11-02T00:00:00+00:00')
        channel = production.state()['channels'][0]['id']
        ident = production.create_project({'title': '발굴 영상', 'channel_id': channel, 'source_video_id': 'dQw4w9WgXcQ'})
        info = next(p for p in production.state()['projects'] if p['id'] == ident)['comment_info']
        self.assertEqual(info, {'count': 42, 'auto': True, 'available': True})
        plain = next(p for p in production.state()['projects'] if p['id'] == self.ident)['comment_info']
        self.assertFalse(plain['available'])

if __name__ == '__main__': unittest.main()
