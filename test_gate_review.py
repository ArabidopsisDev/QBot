import io
import sqlite3
from contextlib import closing
import threading
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import admin
import gates
from pypdf import PdfReader
from gate_review import GateReviewStore
from qbot import Bot, TEST_GROUP
from quality import QualityLedger


class GateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bot=Bot(str(self.root/'live.sqlite3'),self_id='900000')
        self.addCleanup(self.bot.close)
        self.path=self.root/'quality.sqlite3'
        self.ledger=QualityLedger(self.path,clock=lambda:1000.0)
        self.addCleanup(self.ledger.close)
        self.store=GateReviewStore(self.path,self.root,
            Path(__file__).parent/'res'/'sign.png',clock=lambda:1000.0)

    def case(self,mid='1',gate='02',status='pending'):
        report={
            'report_id':gates.report_id_for(TEST_GROUP,mid),
            'group_id':TEST_GROUP,'user_id':'800000','message_id':mid,
            'occurred_at':1000.0,'received_at':1000.0,
            'primary_gate':gate,'primary_category':gate+'IVa',
            'mode':'shadow' if status=='shadow' else 'enforce',
            'status':status,'rule_version':'gates-v2.0',
            'proposal':'proposal-v1','text':'测试内容','nickname':'测试者',
            'uncertain':False,'labels':[gate+'IVa'],
            'suggested_points':10,'suggested_ban_seconds':30,
            'findings':[{'gate':gate,'category':gate+'IVa','evidence':{'term':'测试'}}],
        }
        self.assertTrue(self.ledger.record_case(report))
        return report['report_id']

    def test_approval_is_once_only_with_signature_and_ban(self):
        report_id=self.case()
        self.assertEqual(len(self.store.list_cases()),1)
        result=self.store.approve(report_id,reviewer='管理员',points=10,
                                  ban_seconds=30,reason='人工确认')
        self.assertEqual(result['status'],'approved')
        self.assertTrue(self.store.signature(result['signature_sha256']))
        self.assertEqual(self.ledger.score('800000'),90)
        with closing(sqlite3.connect(self.root/'live.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT seconds FROM bans WHERE user_id=?',
                                        ('800000',)).fetchone()[0],30)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_bans').fetchone()[0],1)
        again=self.store.approve(report_id,reviewer='管理员',points=10,
                                 ban_seconds=30,reason='人工确认')
        self.assertEqual(again['status'],'approved')
        self.assertEqual(self.ledger.score('800000'),90)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM review_decisions')
                         .fetchone()[0],1)
        with self.assertRaises(ValueError):
            self.store.approve(report_id,reviewer='其他人',points=20,
                               ban_seconds=30,reason='重新处理')

    def test_reject_deletes_report_without_score_or_ban(self):
        report_id=self.case()
        self.assertTrue(self.store.reject(report_id)['deleted'])
        self.assertIsNone(self.store.get(report_id))
        self.assertEqual(self.ledger.score('800000'),100)
        with closing(sqlite3.connect(self.root/'live.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bans').fetchone()[0],0)

    def test_shadow_finding_can_only_deduct_after_explicit_human_approval(self):
        report_id=self.case(status='shadow')
        self.assertEqual(self.ledger.score('800000'),100)
        self.assertEqual(self.store.list_cases()[0]['report_id'],report_id)
        self.assertEqual(self.store.list_cases('shadow')[0]['report_id'],report_id)
        result=self.store.approve(report_id,reviewer='管理员',points=1,reason='人工复核')
        self.assertEqual(result['status'],'approved')
        self.assertEqual(self.ledger.score('800000'),99)

    def test_retry_after_ban_commit_does_not_extend_ban_or_double_deduct(self):
        report_id=self.case()
        original=self.store._apply_ban_once
        calls=[0]
        def crash_after_ban(*args):
            original(*args)
            calls[0]+=1
            if calls[0]==1:
                raise RuntimeError('simulated crash after ban commit')
        with patch.object(self.store,'_apply_ban_once',side_effect=crash_after_ban):
            with self.assertRaises(RuntimeError):
                self.store.approve(report_id,reviewer='管理员',points=10,
                                   ban_seconds=30,reason='人工确认')
        self.assertEqual(self.store.get(report_id)['status'],'applying')
        self.assertEqual(self.ledger.score('800000'),100)
        self.store.sign_path=self.root/'missing-sign.png'  # retry uses pinned snapshot
        self.store.approve(report_id,reviewer='管理员',points=10,
                           ban_seconds=30,reason='人工确认')
        self.assertEqual(self.ledger.score('800000'),90)
        with closing(sqlite3.connect(self.root/'live.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_bans').fetchone()[0],1)

    def test_http_pdf_preview_uses_report_without_json(self):
        report_id=self.case()
        with patch.object(admin,'QUALITY_DB',self.path), \
             patch.object(admin,'DATA_ROOT',self.root), \
             patch.object(admin,'SIGN_PATH',Path(__file__).parent/'res'/'sign.png'):
            server=ThreadingHTTPServer(('127.0.0.1',0),admin.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/api/gate-report/pdf?report_id={report_id}'
                with urllib.request.urlopen(url,timeout=10) as response:
                    self.assertEqual(response.headers['Content-Type'],'application/pdf')
                    pdf=response.read()
                info_url=f'http://127.0.0.1:{server.server_port}/api/gate-report/pdf-info?report_id={report_id}'
                with urllib.request.urlopen(info_url,timeout=10) as response:
                    import json
                    self.assertGreaterEqual(json.load(response)['page_count'],1)
                image_url=f'http://127.0.0.1:{server.server_port}/api/gate-report/pdf-page?report_id={report_id}&page=1'
                with urllib.request.urlopen(image_url,timeout=10) as response:
                    self.assertEqual(response.headers['Content-Type'],'image/png')
                    self.assertTrue(response.read(8).startswith(b'\x89PNG'))
                rendered='\n'.join(page.extract_text()
                                   for page in PdfReader(io.BytesIO(pdf)).pages)
                self.assertIn('门禁触发报告',rendered)
                self.assertIn('测试内容',rendered)
                self.assertNotIn('"evidence"',rendered)
            finally:
                server.shutdown(); thread.join(); server.server_close()

    def test_admin_review_routes_validate_and_return_report(self):
        report_id=self.case()
        with patch.object(admin,'QUALITY_DB',self.path), \
             patch.object(admin,'DATA_ROOT',self.root), \
             patch.object(admin,'SIGN_PATH',Path(__file__).parent/'res'/'sign.png'):
            status,payload=admin.handle_api('GET','/api/gate-reports',{'status':'pending'}, {})
            self.assertEqual(status,200)
            self.assertEqual(payload['reports'][0]['report_id'],report_id)
            status,payload=admin.handle_api('GET','/api/gate-report',
                                            {'report_id':report_id}, {})
            self.assertEqual(status,200)
            self.assertEqual(payload['text'],'测试内容')
            status,_=admin.handle_api('POST','/api/gate-report/approve',{},
                {'report_id':report_id,'reviewer':'管理员','points':10,
                 'ban_seconds':30,'reason':'人工确认'})
            self.assertEqual(status,200)


if __name__=='__main__':
    unittest.main()
