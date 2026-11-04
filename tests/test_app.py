import datetime as dt
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import app

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=patch.object(app,'DB',Path(self.tmp.name)/'test.db');self.db.start();app.init()
    def tearDown(self):
        self.db.stop();self.tmp.cleanup()
    def test_stats_and_growth_baseline(self):
        item={'id':'abc','snippet':{'title':'test','channelTitle':'creator','publishedAt':'2026-11-01T00:00:00Z'},'statistics':{'viewCount':'10000','likeCount':'400','commentCount':'100'}}
        app.save_video(item,'2026-11-02T00:00:00+00:00')
        item['statistics']['viewCount']='12000'
        app.save_video(item,'2026-11-02T02:00:00+00:00')
        app.save_video(item,'2026-11-02T02:01:00+00:00')
        with app.connect() as con: row=con.execute('SELECT * FROM videos').fetchone()
        result=app.metrics(row,dt.datetime(2026,11,3,tzinfo=app.UTC))
        self.assertEqual(result['engagement'],4.17)
        self.assertEqual(row['previous_views'],10000)
        self.assertEqual(row['previous_at'],'2026-11-02T00:00:00+00:00')
        self.assertGreater(result['growth_per_hour'],990)
    def test_hidden_likes_not_treated_as_zero(self):
        item={'id':'hidden','snippet':{'title':'test','channelTitle':'creator','publishedAt':'2026-11-01T00:00:00Z'},'statistics':{'viewCount':'10000','commentCount':'5'}}
        app.save_video(item,'2026-11-02T00:00:00+00:00')
        with app.connect() as con: row=con.execute('SELECT * FROM videos').fetchone()
        self.assertIsNone(app.metrics(row)['engagement'])
    def test_scan_deduplicates_results_and_records_success(self):
        def api(endpoint,params):
            if endpoint=='search': return {'items':[{'id':{'videoId':'one'}}]}
            return {'items':[{'id':'one','snippet':{'title':'动物','channelTitle':'creator','publishedAt':dt.datetime.now(app.UTC).isoformat()},'statistics':{'viewCount':'25000','likeCount':'500','commentCount':'50'}}]}
        with patch.dict('os.environ',{'YOUTUBE_API_KEY':'test-only'}),patch.object(app,'youtube',side_effect=api): app.scan()
        result=app.state()
        self.assertEqual(len(result['videos']),1)
        self.assertEqual(result['runs'][0]['status'],'success')
        self.assertIsNone(result['videos'][0]['growth_per_hour'])
    def test_missing_key_is_actionable_and_logged(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(ValueError,'YOUTUBE_API_KEY'):app.scan()
        self.assertEqual(app.state()['runs'][0]['status'],'error')


class ChannelTests(unittest.TestCase):
    setUp = DiscoveryTests.setUp
    tearDown = DiscoveryTests.tearDown
    def test_channel_median_cache_and_hidden_subscribers(self):
        calls=[]
        def api(endpoint,params):
            calls.append(endpoint)
            if endpoint=='channels':return {'items':[{'contentDetails':{'relatedPlaylists':{'uploads':'uploads'}},'statistics':{'hiddenSubscriberCount':True}}]}
            if endpoint=='playlistItems':return {'items':[{'contentDetails':{'videoId':str(i)}} for i in range(3)]}
            return {'items':[{'statistics':{'viewCount':str(n)}} for n in (100,200,999999)]}
        with patch.dict('os.environ',{'YOUTUBE_API_KEY':'test'}),patch.object(app,'youtube',side_effect=api):
            app.analyze_channel('channel');app.analyze_channel('channel')
        with app.connect() as con:row=con.execute('SELECT * FROM channels').fetchone()
        self.assertEqual(row['median_views'],200)
        self.assertEqual(row['sample_count'],3)
        self.assertIsNone(row['subscribers'])
        self.assertEqual(calls,['channels','playlistItems','videos'])
    def test_saved_items_survive_discovery_filters(self):
        item={'id':'old','snippet':{'title':'old','channelTitle':'channel','publishedAt':'2020-01-01T00:00:00Z'},'statistics':{'viewCount':'1'}}
        app.save_video(item,dt.datetime.now(app.UTC).isoformat())
        with app.connect() as con:con.execute("INSERT INTO research(video_id,saved) VALUES ('old',1)")
        result=app.state()
        self.assertEqual(result['videos'],[])
        self.assertEqual(result['all_videos'][0]['id'],'old')
        self.assertEqual(result['research']['old']['saved'],1)

class ServerTests(unittest.TestCase):
    setUp_db = DiscoveryTests.setUp
    def setUp(self):
        self.setUp_db()
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();DiscoveryTests.tearDown(self)
    def request(self,method,path,body=None,host=None):
        con=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        headers={'Content-Type':'application/json'}
        if host:headers['Host']=host
        con.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        response=con.getresponse();data=response.read();con.close()
        return response.status,data
    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.request('GET','/api/state',host='attacker.example:8000')[0],403)
        self.assertEqual(self.request('POST','/api/scan',{},host='attacker.example')[0],403)
        self.assertEqual(self.request('GET','/api/state',host='localhost:8000')[0],200)
    def test_unexpected_error_returns_json_500(self):
        with patch.object(app,'state',side_effect=RuntimeError('boom')),patch('traceback.print_exc'):
            status,data=self.request('GET','/api/state')
        self.assertEqual(status,500);self.assertIn('error',json.loads(data))
    def test_settings_missing_or_non_numeric_fields_get_clear_message(self):
        status,data=self.request('POST','/api/settings',{'queries':'cats'})
        self.assertEqual(status,400);self.assertIn('모두 입력',json.loads(data)['error'])
        status,data=self.request('POST','/api/settings',{'queries':'cats','days':'abc','min_views':'0','hour_kst':'9','schedule':'false'})
        self.assertEqual(status,400);self.assertIn('숫자',json.loads(data)['error'])
    def test_connections_are_closed_after_use(self):
        with app.connect() as con: con.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError): con.execute('SELECT 1')

if __name__ == "__main__": unittest.main()
