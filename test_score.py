"""Offline /score quota, routing and calendar checks."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from qbot import Bot, TEST_GROUP, PRODUCTION_GROUP
from quality import QualityLedger, SHANGHAI, score_label, score_week


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,10,4,23,59,59,tzinfo=SHANGHAI).timestamp()
        self.ledger=QualityLedger(Path(self.temp.name)/'quality.sqlite3',clock=lambda:self.now)
        self.addCleanup(self.ledger.close)
        self.bot=Bot(self_id='900000',quality=self.ledger,clock=lambda:self.now)
        self.addCleanup(self.bot.close)
        self.serial=0

    def event(self,group=TEST_GROUP,user='800000',text='/score'):
        self.serial+=1
        return dict(post_type='message',message_type='group',group_id=group,
                    self_id='900000',user_id=user,message_id=str(self.serial),time=self.now,
                    message=[{'type':'text','data':{'text':text}}])

    def text(self,reply):
        return reply['params']['message'][0]['data']['text']

    def test_tier_boundaries(self):
        expected={100:'无可挑剔',99:'优秀',85:'优秀',84:'良好',70:'良好',
                  69:'中等',60:'中等',59:'较差',40:'较差',39:'很差',0:'很差'}
        for score,label in expected.items():
            self.assertEqual(score_label(score),label)

    def test_quota_is_global_per_qq_and_resets_at_shanghai_monday(self):
        first=self.bot.handle(self.event())
        self.assertEqual(self.text(first),'您的质量分为无可挑剔！\n您本周的查询额度剩余1次！')
        other=Bot(self_id='900000',group_id=PRODUCTION_GROUP,
                  quality=self.ledger,clock=lambda:self.now)
        self.addCleanup(other.close)
        second=other.handle(self.event(PRODUCTION_GROUP))
        self.assertIn('剩余0次',self.text(second))
        third=self.bot.handle(self.event())
        self.assertEqual(self.text(third),
            '您的额度已用完！\n重置时间：2026-10-05 00:00（北京时间）')
        self.now+=1
        self.assertIn('剩余1次',self.text(self.bot.handle(self.event())))
        self.assertIn('剩余1次',self.text(self.bot.handle(self.event(user='800001'))))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],0)

    def test_rate_skipped_query_releases_quota_unknown_send_keeps_it(self):
        first=self.bot.handle(self.event())
        self.ledger.settle_score_query(first['score_query'],sent=False)
        next_reply=self.bot.handle(self.event())
        self.assertIn('剩余1次',self.text(next_reply))
        self.ledger.settle_score_query(next_reply['score_query'],sent=False,uncertain=True)
        self.assertIn('剩余0次',self.text(self.bot.handle(self.event())))

    def test_banned_sender_and_other_groups_cannot_query(self):
        with self.bot.db:
            self.bot.ban('800000',60,'测试封禁','手动封禁',self.now)
        self.assertIsNone(self.bot.handle(self.event()))
        self.assertIsNone(self.bot.handle(self.event(group='999999')))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM score_queries').fetchone()[0],0)

    def test_replayed_message_does_not_consume_quota_again(self):
        event=self.event()
        self.assertIsNotNone(self.bot.handle(event))
        self.assertIsNone(self.bot.handle(event))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM score_queries').fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
