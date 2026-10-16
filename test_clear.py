import tempfile
import unittest
from pathlib import Path

from qbot import Bot, CLEAR_ADMIN, TEST_GROUP, PRODUCTION_GROUP


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(self_id='900000')
        self.addCleanup(self.bot.close)
        self.sequence = 0
        self.seed(self.bot)

    def seed(self, bot):
        with bot.db:
            bot.db.execute("INSERT INTO qa VALUES ('q','a',2)")
            bot.db.execute("INSERT INTO chars VALUES ('a','b',3)")
            bot.db.execute('CREATE TABLE pretraining (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            bot.db.execute("INSERT INTO pretraining VALUES ('import','{}')")

    def event(self, text, **changes):
        self.sequence += 1
        event = dict(post_type='message', message_type='group', group_id=TEST_GROUP,
            self_id='900000', user_id=CLEAR_ADMIN, message_id=str(self.sequence),
            time=self.sequence, message=[{'type':'text','data':{'text':text}}])
        event.update(changes)
        return event

    def count(self, table, bot=None):
        return (bot or self.bot).db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_only_real_sender_id_can_clear(self):
        for user in ('other',None,True,'575586491 ',575586491.0):
            for model in ('match','generate'):
                self.bot.handle(self.event('/clear '+model,user_id=user,
                    sender={'user_id':575586491,'nickname':'575586491'}))
                self.assertEqual(self.count('qa'),1)
                self.assertEqual(self.count('chars'),1)
                self.assertEqual(self.count('pretraining'),1)

    def test_clear_match_resets_context_only(self):
        self.bot.previous = ('before clear',0)
        result = self.bot.handle(self.event('/clear match', user_id=575586491))
        self.assertEqual(result['params']['group_id'],int(TEST_GROUP))
        self.assertEqual(self.count('qa'),0)
        self.assertEqual(self.count('chars'),1)
        self.assertEqual(self.count('pretraining'),1)
        self.assertEqual(self.bot.model,'match')
        self.assertIsNone(self.bot.previous)
        self.bot.handle(self.event('after clear'))
        self.assertEqual(self.count('qa'),0)

    def test_clear_generate_includes_pretraining_preserves_other_model(self):
        self.bot.previous=('match context',0)
        self.bot.handle(self.event('/clear generate'))
        self.assertEqual(self.count('chars'),0)
        self.assertEqual(self.count('pretraining'),0)
        self.assertEqual(self.count('qa'),1)
        self.assertEqual(self.bot.previous,('match context',0))
        self.assertEqual(self.bot.model,'match')

    def test_invalid_commands_never_learn_or_clear(self):
        for command in ('/clear','/clear all','/clear match extra','/clear generate; DELETE'):
            self.bot.handle(self.event(command))
        self.assertEqual(self.count('qa'),1)
        self.assertEqual(self.count('chars'),1)
        self.assertEqual(self.count('pretraining'),1)

    def test_non_whitelisted_group_and_private_rejected(self):
        for changes in ({'group_id':'OTHER'}, {'message_type':'private'}):
            self.assertIsNone(self.bot.handle(self.event('/clear match',**changes)))
        self.assertEqual(self.count('qa'),1)
        self.assertEqual(self.count('seen'),0)

    def test_production_clear_does_not_touch_test_group(self):
        production=Bot(self_id='900000',group_id=PRODUCTION_GROUP)
        try:
            self.seed(production)
            production.handle(self.event('/clear generate',group_id=PRODUCTION_GROUP))
            self.assertEqual(self.count('chars',production),0)
            self.assertEqual(self.count('chars'),1)
            self.assertEqual(self.count('qa'),1)
        finally:
            production.close()

    def test_replayed_clear_after_restart_does_not_erase_new_learning(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'test.sqlite3')
            bot=Bot(path,self_id='900000')
            self.seed(bot)
            event=self.event('/clear generate')
            bot.handle(event)
            with bot.db:
                bot.db.execute("INSERT INTO chars VALUES ('x','y',1)")
            bot.close()
            bot=Bot(path,self_id='900000')
            try:
                self.assertIsNone(bot.handle(event))
                self.assertEqual(self.count('chars',bot),1)
            finally:
                bot.close()

    def test_generate_mode_rejected_command_not_trained(self):
        self.bot.handle(self.event('/model generate'))
        self.bot.handle(self.event('/clear generate',user_id='other'))
        self.assertEqual(self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0],3)
        self.bot.handle(self.event('/clear generate'))
        self.assertEqual(self.bot.model,'generate')
        self.assertEqual(self.count('chars'),0)


if __name__ == '__main__':
    unittest.main()
