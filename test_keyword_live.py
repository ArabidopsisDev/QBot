"""Keyword replies inside Bot.handle: opt-in per group, fresh-edge and ban checks."""
import random
import unittest
from unittest.mock import Mock

import live
import qbot
from qbot import ALLOWED_GROUPS, THIRD_GROUP, Bot, keyword_probability

CORPUS = ['今天考试好难啊。', '数学考试我也没复习！', '考试结束去吃火锅吧', '火锅好吃吗？',
          '明天还要考试吗', '我觉得数学挺难的。', '晚上一起吃火锅！', '复习不完了啊',
          '物理考试比数学难', '吃完火锅去复习']


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class KeywordLiveTests(unittest.TestCase):
    def bot(self, probability, **kwargs):
        self.serial = getattr(self, 'serial', 0)
        bot = Bot(self_id='900000', group_id=THIRD_GROUP, rng=random.Random(1),
                  keyword_reply_probability=probability, **kwargs)
        self.addCleanup(bot.close)
        with bot.db:
            bot.db.execute("UPDATE settings SET value='generate' WHERE key='model'")
        for text in CORPUS * 3:
            self.send(bot, text)
        return bot

    def send(self, bot, text):
        self.serial += 1
        return bot.handle(dict(post_type='message', message_type='group', group_id=THIRD_GROUP,
                               self_id='900000', user_id='800001', message_id=str(self.serial),
                               time=self.serial, message=[{'type': 'text', 'data': {'text': text}}]))

    @staticmethod
    def text(reply):
        return reply['params']['message'][0]['data']['text'] if reply else None

    def test_zero_probability_never_touches_the_keyword_rng(self):
        bot = self.bot(0.0, keyword_rng=Mock())
        peer = Bot(self_id='900000', group_id=THIRD_GROUP, rng=random.Random(1))
        self.addCleanup(peer.close)
        with peer.db:
            peer.db.execute("UPDATE settings SET value='generate' WHERE key='model'")
        self.serial = 0
        for text in CORPUS * 3:
            self.send(peer, text)
        for prompt in ('今天考试好难', '想吃火锅', '数学怎么复习'):
            self.assertEqual(self.text(self.send(bot, prompt)), self.text(self.send(peer, prompt)))
        bot.keyword_rng.random.assert_not_called()

    def test_keyword_replies_use_current_edges_and_skip_training_copies(self):
        bot = self.bot(1.0, keyword_rng=random.Random(4))
        memories = {row[0] for row in bot.db.execute('SELECT text FROM intel_memory')}
        produced = 0
        for prompt in ('今天考试好难', '想吃火锅', '数学怎么复习') * 5:
            reply = self.send(bot, prompt)
            if reply is None:
                continue
            produced += 1
            self.assertTrue(reply['model_generated'])
            text = self.text(reply)
            self.assertNotIn(text, memories)
            for first, second in zip(text, text[1:]):
                self.assertTrue(bot.db.execute('SELECT 1 FROM chars WHERE first=? AND second=?',
                                               (first, second)).fetchone())
        self.assertGreater(produced, 0)

    def test_edges_deleted_after_caching_never_leave(self):
        bot = self.bot(1.0, keyword_rng=random.Random(2))
        bot.keyword_reply('火锅好吃')  # Builds and caches the model.
        with bot.db:
            bot.db.execute("DELETE FROM chars WHERE first='火' AND second='锅'")
        for _ in range(40):
            text = bot.keyword_reply('火锅好吃')
            if text:
                self.assertNotIn('火锅', text)

    def test_pair_bans_added_after_caching_drop_the_reply(self):
        bot = self.bot(1.0, keyword_rng=random.Random(3))
        bot.keyword_reply('火锅好吃')
        with bot.db:
            bot.db.execute("INSERT INTO pair_bans VALUES ('火','锅','t',0)")
        for _ in range(20):
            text = self.text(self.send(bot, '火锅好吃'))
            if text:
                self.assertNotIn('火锅', text)

    def test_model_refreshes_after_the_interval(self):
        clock = Clock()
        bot = self.bot(1.0, keyword_rng=random.Random(5), clock=clock)
        bot.keyword_reply('考试')
        first = bot._keyword_model
        clock.now += qbot.KEYWORD_REFRESH - 1
        bot.keyword_reply('考试')
        self.assertIs(bot._keyword_model, first)
        clock.now += 2
        bot.keyword_reply('考试')
        self.assertIsNot(bot._keyword_model, first)
        clock.now -= 500  # A wall clock stepping back also forces a rebuild.
        current = bot._keyword_model
        bot.keyword_reply('考试')
        self.assertIsNot(bot._keyword_model, current)

    def test_other_models_ignore_the_keyword_branch(self):
        for model in ('match', 'intelligence'):
            bot = self.bot(1.0, keyword_rng=random.Random(7))
            with bot.db:
                bot.db.execute("UPDATE settings SET value=? WHERE key='model'", (model,))
            bot.keyword_rng = Mock()
            self.send(bot, '今天考试好难')
            bot.keyword_rng.random.assert_not_called()

    def test_cleared_generate_data_silences_keyword_replies(self):
        bot = self.bot(1.0, keyword_rng=random.Random(6))
        bot.keyword_reply('考试')
        with bot.db:
            bot.db.execute('DELETE FROM chars')
        self.assertIsNone(bot.keyword_reply('今天考试好难'))

    def test_probability_validation(self):
        for value in (-0.1, 1.5, float('nan'), True, '0.3', None):
            with self.assertRaises(ValueError):
                keyword_probability(value)
        self.assertEqual(keyword_probability(1), 1.0)
        with self.assertRaises(ValueError):
            Bot(self_id='900000', keyword_reply_probability=2)

    def test_live_enables_only_the_third_group(self):
        self.assertEqual(set(live.KEYWORD_REPLY_PROBABILITY), {THIRD_GROUP})
        for group, value in live.KEYWORD_REPLY_PROBABILITY.items():
            self.assertIn(group, ALLOWED_GROUPS)
            self.assertEqual(keyword_probability(value), .30)


if __name__ == '__main__':
    unittest.main()
