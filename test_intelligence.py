import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligence import Intelligence, features, polarity
from qbot import Bot, CLEAR_ADMIN, TEST_GROUP, PRODUCTION_GROUP


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.bot=Bot(self_id='900000',rng=random.Random(37))
        self.addCleanup(self.bot.close)
        self.serial=0

    def event(self,text,**changes):
        self.serial+=1
        result=dict(post_type='message',message_type='group',group_id=TEST_GROUP,
            self_id='900000',user_id='human',time=self.serial,message_id=str(self.serial),
            message=[{'type':'text','data':{'text':text}}])
        result.update(changes)
        return result

    def teach_pair(self,question,answer):
        with self.bot.db:
            self.bot.intelligence.reset_context()
            self.bot.intelligence.observe(question,10)
            self.bot.intelligence.observe(answer,11)

    def test_good_bad_contexts_with_shared_last_character(self):
        self.teach_pair('好了','那就继续用吧')
        self.teach_pair('坏了','先检查再维修')
        for _ in range(20):
            good=self.bot.intelligence.reply('好了')
            bad=self.bot.intelligence.reply('坏了')
            self.assertTrue(good and '那就继续用吧'.startswith(good),good)
            self.assertTrue(bad and '先检查再维修'.startswith(bad),bad)

    def test_negation_and_related_whole_sentence(self):
        self.teach_pair('电脑已经好了','那就继续使用')
        self.teach_pair('电脑已经坏了','先检查电源线')
        for query in ('电脑又坏了','电脑不好了'):
            for _ in range(5):
                result=self.bot.intelligence.reply(query)
                self.assertTrue(result and '先检查电源线'.startswith(result),result)
        self.assertEqual(polarity('不好'),-1)
        self.assertEqual(polarity('没有坏'),1)
        self.assertEqual(polarity('没成功'),-1)
        self.assertEqual(polarity('成功'),1)

    def test_suffix_continuation_never_backs_off_to_shared_single_character(self):
        with self.bot.db:
            self.bot.intelligence.observe('好了就继续工作',1)
            self.bot.intelligence.reset_context()
            self.bot.intelligence.observe('坏了先检查电源',2)
        good=self.bot.intelligence.reply('好了')
        bad=self.bot.intelligence.reply('坏了')
        self.assertTrue(good and '就继续工作'.startswith(good),good)
        self.assertTrue(bad and '先检查电源'.startswith(bad),bad)
        self.assertIsNone(self.bot.intelligence.reply('了'))
        self.assertIsNone(self.bot.intelligence.reply('量子香蕉'))

    def test_all_three_modes_train_all_three_models(self):
        for mode in ('match','generate','intelligence'):
            bot=Bot(self_id='900000')
            try:
                bot.handle(self.event('/model '+mode))
                bot.handle(self.event('甲乙'))
                last=self.event('丙丁')
                bot.handle(last)
                bot.handle(last)
                bot.handle(self.event('bot response',user_id='900000'))
                self.assertEqual(bot.db.execute('SELECT * FROM qa').fetchall(),[('甲乙','丙丁',1)])
                self.assertEqual(set(bot.db.execute('SELECT * FROM chars')),{('甲','乙',1),('丙','丁',1)})
                self.assertEqual(bot.db.execute('SELECT SUM(w) FROM intel_memory').fetchone()[0],2)
                self.assertEqual(bot.db.execute('SELECT SUM(w) FROM intel_links').fetchone()[0],1)
                self.assertEqual(bot.model,mode)
            finally:
                bot.close()

    def test_mode_only_selects_reply(self):
        self.teach_pair('好了','那就继续使用')
        self.bot.handle(self.event('/model intelligence'))
        result=self.bot.handle(self.event('好了'))
        self.assertTrue('那就继续使用'.startswith(result['params']['message'][0]['data']['text']))
        self.assertEqual(result['params']['group_id'],int(TEST_GROUP))

    def test_timeout_and_other_groups_never_enter_intelligence(self):
        self.bot.handle(self.event('a',time=1))
        self.bot.handle(self.event('b',time=182))
        self.bot.handle(self.event('c',time=183,group_id='OTHER'))
        self.bot.handle(self.event('private',time=184,message_type='private'))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_links').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],2)

    def test_clear_permission_and_independent_contexts(self):
        self.bot.handle(self.event('a'))
        self.bot.handle(self.event('/clear intelligence',user_id='not-admin'))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],1)
        self.bot.handle(self.event('/clear match',user_id=CLEAR_ADMIN))
        self.bot.handle(self.event('b'))
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM qa').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_links').fetchone()[0],1)
        clear=self.event('/clear intelligence',user_id=CLEAR_ADMIN)
        self.bot.handle(clear)
        for table in ('intel_memory','intel_terms','intel_links'):
            self.assertEqual(self.bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
        self.bot.handle(self.event('c'))
        self.bot.handle(clear)
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa').fetchall(),[('b','c',1)])
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_links').fetchone()[0],0)
        self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],1)

    def test_storage_caps_and_cascades(self):
        with patch('intelligence.MAX_MEMORIES',4),patch('intelligence.MAX_LINKS',3):
            for i in range(10):
                self.bot.handle(self.event(f'text{i}'))
        self.assertLessEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],4)
        self.assertLessEqual(self.bot.db.execute('SELECT COUNT(*) FROM intel_links').fetchone()[0],3)
        self.assertEqual(self.bot.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertLessEqual(len(features('abcdefghij'*1000)),96)

    def test_restart_persistence_and_group_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'test.sqlite3')
            bot=Bot(path,self_id='900000')
            bot.handle(self.event('/model intelligence'))
            bot.handle(self.event('好了'))
            bot.handle(self.event('那就继续工作'))
            bot.close()
            bot=Bot(path,self_id='900000')
            production=Bot(self_id='900000',group_id=PRODUCTION_GROUP)
            try:
                self.assertEqual(bot.model,'intelligence')
                self.assertIsNone(bot.intelligence.previous)
                self.assertIsNotNone(bot.intelligence.reply('好了'))
                self.assertIsNone(production.intelligence.reply('好了'))
            finally:
                bot.close()
                production.close()


if __name__=='__main__':
    unittest.main()
