"""Gate boundaries, notice deduplication and future score sampling, offline."""
import hashlib
import hmac
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import admin
import live
from qbot import Bot, PRODUCTION_GROUP, TEST_GROUP
from quality import QualityLedger, ban_grade, length_gate


class SequenceRng:
    def __init__(self,values): self.values=iter(values)
    def randrange(self,limit):
        assert limit==100
        return next(self.values)


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.quality=QualityLedger(Path(self.temp.name)/'quality.sqlite3',clock=lambda:1000.0)
        self.addCleanup(self.quality.close)
        self.bot=Bot(self_id='900000',clock=lambda:1000.0,quality=self.quality)
        self.addCleanup(self.bot.close)
        self.serial=0

    def event(self,text,*,group=TEST_GROUP,user='800000'):
        self.serial+=1
        return dict(post_type='message',message_type='group',group_id=group,
            self_id='900000',user_id=user,message_id=str(self.serial),time=float(self.serial),
            message=[{'type':'text','data':{'text':text}}])

    def test_length_boundaries(self):
        expected={66:None,67:'01a',100:'01a',101:'01b',150:'01b',
                  151:'01c',200:'01c',201:'01d',10000:'01d',10001:'01d'}
        for count,category in expected.items():
            with self.subTest(count=count):
                self.assertEqual(length_gate('甲'*count),category)

    def test_01_blocks_learning_and_records_one_case_per_message_silently(self):
        text='甲'*67
        first=self.bot.handle(self.event(text))
        self.assertIsNone(first)
        self.assertIsNone(self.bot.handle(self.event(text,user='800001')))
        other=Bot(self_id='900000',group_id=PRODUCTION_GROUP,
                  clock=lambda:1000.0,quality=self.quality)
        self.addCleanup(other.close)
        self.assertIsNone(other.handle(self.event(text,group=PRODUCTION_GROUP)))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM qa').fetchone()[0],0)
        self.assertIsNone(self.bot.previous)
        self.assertEqual(self.quality.db.execute(
            "SELECT COUNT(*) FROM gate_cases WHERE primary_gate='01'").fetchone()[0],3)
        self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM notice_claims').fetchone()[0],0)

    def test_over_reply_limit_still_records_01d(self):
        result=self.bot.handle(self.event('甲'*10001))
        self.assertIsNone(result)
        self.assertEqual(self.quality.db.execute('SELECT primary_category FROM gate_cases').fetchone()[0],
                         '01d')

    def test_01_notice_defaults_to_silent(self):
        for _ in range(3):
            self.assertIsNone(self.bot.handle(self.event('乙'*101)))
        self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM notice_claims').fetchone()[0],0)

    def test_00_never_records_or_replies_to_banned_messages(self):
        with self.bot.db:
            self.bot.ban('800000',60,'手动封禁','手动封禁',1000.0)
        first=self.bot.handle(self.event('你好'))
        self.assertIsNone(first)
        self.assertIsNone(self.bot.handle(self.event('再次发言')))
        with self.bot.db:
            self.bot.ban('800000',60,'再次手动封禁','手动封禁',1000.5)
        renewed=self.bot.handle(self.event('新一次封禁后发言'))
        self.assertIsNone(renewed)
        self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM gate_cases').fetchone()[0],0)
        self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],0)

    def test_00_blocks_transport_and_sticker_capture_offline(self):
        with self.bot.db:
            self.bot.ban('800000',60,'审核封禁','审核封禁',1000.0)
        sender=Mock()
        stickers=Mock()
        token='offline-secret'
        server=HTTPServer(('127.0.0.1',0),live.make_handler(
            {TEST_GROUP:self.bot},sender,token,stickers))
        try:
            payload=self.event('test')
            payload['message']=[{'type':'image','data':{'file':'x.jpg',
                'sub_type':1,'summary':'[动画表情]'}}]
            body=json.dumps(payload).encode()
            signature='sha1='+hmac.new(token.encode(),body,hashlib.sha1).hexdigest()
            status=[]
            def request():
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                conn.request('POST','/events',body=body,
                             headers={'x-signature':signature})
                response=conn.getresponse(); status.append(response.status)
                response.read(); conn.close()
            thread=threading.Thread(target=request,daemon=True); thread.start()
            with patch.object(live,'STOP_FILE',Path(self.temp.name)/'STOP'):
                server.handle_request()
            thread.join()
            self.assertEqual(status,[200])
            sender.send.assert_not_called()
            stickers.capture.assert_not_called()
            self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM gate_cases').fetchone()[0],0)
        finally:
            server.server_close()

    def test_sensitive_word_is_02_report_without_auto_ban(self):
        first=self.bot.handle(self.event('色情'))
        self.assertIsNone(first)
        self.assertEqual(self.quality.db.execute('SELECT primary_category FROM gate_cases').fetchone()[0],
                         '02Ia')
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM bans').fetchone()[0],0)
        self.assertEqual(self.quality.score('800000'),100)

    def test_first_post_ban_sticker_is_silent_00(self):
        with self.bot.db:
            self.bot.ban('800000',60,'手动封禁','手动封禁',1000.0)
        image=self.event('unused')
        image['message']=[{'type':'image','data':{'file':'sticker.jpg'}}]
        result=self.bot.handle(image)
        self.assertIsNone(result)
        self.assertIsNone(self.bot.handle(self.event('随后发言')))

    def test_train_and_reply_draws_are_independent_when_calibrated(self):
        self.quality.score('800000')
        with self.quality.db:
            self.quality.db.execute("UPDATE users SET score=50,score_version='test' WHERE user_id='800000'")
        self.quality.rng=SequenceRng([20,80,80,20])
        with self.bot.db:
            self.bot.db.execute("UPDATE settings SET value='generate' WHERE key='model'")
        with patch.object(self.bot,'generate',return_value='测试生成文本'):
            self.assertIsNone(self.bot.handle(self.event('甲乙丙')))
            self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],2)
            second=self.bot.handle(self.event('丁戊己'))
        self.assertEqual(second['params']['message'][0]['data']['text'],'测试生成文本')
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],2)
        self.assertIsNone(self.bot.previous)

    def test_backup_is_readable_without_raw_message_contents(self):
        self.bot.handle(self.event('甲'*67))
        target=Path(self.temp.name)/'backup.sqlite3'
        self.quality.backup(target)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT score FROM users').fetchone()[0],100)
            self.assertEqual(db.execute('SELECT primary_category FROM gate_cases').fetchone()[0],'01a')

    def test_manual_score_is_audited_and_visible_to_live_sampling(self):
        with patch.object(admin,'QUALITY_DB',Path(self.quality.path)):
            status,_=admin.handle_api('POST','/api/quality/score',{},
                {'user_id':'800000','score':50,'reason':''})
            self.assertEqual(status,400)
            status,result=admin.handle_api('POST','/api/quality/score',{},
                {'user_id':'800000','score':50,'reason':'人工复核'})
            self.assertEqual(status,200)
            self.assertTrue(result['changed'])
            self.assertEqual(self.quality.score('800000'),50)
            status,detail=admin.handle_api('GET','/api/quality/user',
                {'user_id':'800000'}, {})
            self.assertEqual(status,200)
            self.assertEqual(detail['score'],50)
            self.assertEqual(detail['changes'][0]['reason'],'人工复核')
            status,summary=admin.handle_api('GET','/api/quality',{}, {})
            self.assertEqual(status,200)
            self.assertEqual(summary['total_users'],1)
            self.assertEqual(summary['changes'][0]['old_score'],100)
            status,repeat=admin.handle_api('POST','/api/quality/score',{},
                {'user_id':'800000','score':50,'reason':'重复请求'})
            self.assertEqual(status,200)
            self.assertFalse(repeat['changed'])
            self.assertEqual(self.quality.db.execute(
                'SELECT COUNT(*) FROM score_audit').fetchone()[0],1)

    def test_live_rate_limit_skip_releases_notice_for_a_future_attempt(self):
        sender=Mock()
        sender.send.return_value=False
        token='offline-secret'
        server=HTTPServer(('127.0.0.1',0),live.make_handler(
            {TEST_GROUP:self.bot},sender,token))
        try:
            payload=self.event('甲'*67)
            body=json.dumps(payload).encode()
            signature='sha1='+hmac.new(token.encode(),body,hashlib.sha1).hexdigest()
            status=[]
            def request():
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                connection.request('POST','/events',body=body,
                                   headers={'x-signature':signature})
                response=connection.getresponse()
                status.append(response.status)
                response.read()
                connection.close()
            thread=threading.Thread(target=request,daemon=True)
            thread.start()
            with patch.object(live,'STOP_FILE',Path(self.temp.name)/'STOP'):
                server.handle_request()  # Bot's SQLite connection stays on its owning thread.
            thread.join()
            self.assertEqual(status,[200])
            self.assertEqual(self.quality.db.execute('SELECT COUNT(*) FROM notice_claims').fetchone()[0],0)
            self.assertIsNone(self.bot.handle(self.event('甲'*67)))
        finally:
            server.server_close()

    def test_dashboard_quality_summary_is_read_only_and_separate(self):
        self.bot.handle(self.event('甲'*67))
        with patch.object(admin,'QUALITY_DB',Path(self.quality.path)):
            state=admin.quality_state()
        self.assertEqual(state['status'],'校准中')
        self.assertEqual(state['users'][0]['score'],100)
        self.assertEqual(state['categories'],[{'category':'01a','count':1}])
        self.assertEqual(state['total_events'],1)


if __name__=='__main__':
    unittest.main()
