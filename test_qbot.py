import copy
import random
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from qbot import Bot, TEST_GROUP, PRODUCTION_GROUP, group_reply


class BotTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(self_id='900000', rng=random.Random(123))
        self.serial = 0
        self.addCleanup(self.bot.close)

    def event(self, text, timestamp=None, **changes):
        self.serial += 1
        event = dict(post_type='message', message_type='group', group_id=TEST_GROUP,
                     self_id='900000', user_id='800000', message_id=str(self.serial),
                     time=self.serial if timestamp is None else timestamp,
                     message=[{'type': 'text', 'data': {'text': text}}])
        event.update(changes)
        return event

    def send(self, text, timestamp=None, **changes):
        return self.bot.handle(self.event(text, timestamp, **changes))

    def test_adjacent_pairs(self):
        for value in ('1', '2', '3'):
            self.send(value)
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa ORDER BY q').fetchall(),
                         [('1', '2', 1), ('2', '3', 1)])

    def test_timeout_boundary(self):
        self.send('a', 0)
        self.send('b', 180)
        self.send('c', 361)
        self.send('d', 362)
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa ORDER BY q').fetchall(),
                         [('a', 'b', 1), ('c', 'd', 1)])

    def test_multiple_answers_counts_and_lookup(self):
        for value in ('q', 'a', 'q', 'a', 'q', 'b'):
            self.send(value)
        self.assertEqual(self.bot.db.execute('SELECT a,w FROM qa WHERE q=? ORDER BY a',
                                             ('q',)).fetchall(), [('a', 2), ('b', 1)])
        reply = self.send('q')
        self.assertIn(reply['params']['message'][0]['data']['text'], ('a', 'b'))

    def test_weight_intervals_exact(self):
        class Ticket:
            def __init__(self, n): self.n = n
            def randrange(self, total):
                assert total == 4
                return self.n
        actual = []
        for n in range(4):
            self.bot.rng = Ticket(n)
            actual.append(self.bot.pick([('a', 1), ('b', 3)]))
        self.assertEqual(actual, ['a', 'b', 'b', 'b'])

    def test_out_of_scope_before_content(self):
        # Fabricated metadata only: never connects to any real other group.
        class NoContent(dict):
            def get(self, key, *args):
                if key == 'message': raise AssertionError('Read out-of-scope content')
                return super().get(key, *args)
        for patch in ({'group_id': 'NOT-A-REAL-GROUP'}, {'message_type': 'private'},
                      {'post_type': 'notice'}, {'self_id': 'unexpected'}):
            self.assertIsNone(self.bot.handle(NoContent(self.event('unused', **patch))))
        for table in ('qa', 'chars', 'seen'):
            self.assertEqual(self.bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_self_and_duplicate_not_learned(self):
        self.send('a')
        event = self.event('b')
        self.bot.handle(event)
        self.bot.handle(copy.deepcopy(event))
        self.send('self-reply', user_id='900000')
        self.send('sent-echo', post_type='message_sent')
        self.send('c')
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa ORDER BY q').fetchall(),
                         [('a', 'b', 1), ('b', 'c', 1)])

    def test_command_exclusion_and_context_preserved_across_switch(self):
        self.send('事先知道')
        self.send('/model generate')
        self.send('甲乙丙丁')
        self.assertEqual(self.bot.model, 'generate')
        self.send('/model match')
        self.send('事后追认')
        self.assertEqual(set(self.bot.db.execute('SELECT * FROM qa')),
                         {('事先知道','甲乙丙丁',1), ('甲乙丙丁','事后追认',1)})
        self.assertEqual(self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0], 9)
        self.send('/model bad')
        self.assertEqual(self.bot.model, 'match')

    def test_generate_learns_every_adjacent_pair(self):
        self.send('/model generate')
        self.send('a!中🙂a')
        self.send('XY')
        self.send('你好，世界！')
        pairs = set(self.bot.db.execute('SELECT * FROM chars').fetchall())
        # The model learns punctuation too -- otherwise replies come out as one
        # unbroken run of characters. Filtering is a dashboard concern only.
        self.assertEqual(pairs, {
            ('a', '!', 1), ('!', '中', 1), ('中', '🙂', 1), ('🙂', 'a', 1),
            ('X', 'Y', 1),
            ('你', '好', 1), ('好', '，', 1), ('，', '世', 1), ('世', '界', 1), ('界', '！', 1)})
        before = self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0]
        self.send('ignored', user_id='900000')
        self.assertEqual(before, self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0])

    def test_generate_length_limits_and_seed(self):
        self.send('/model generate')
        # Seed the graph directly: strong repeated user messages are now
        # compacted before training, independently of generation length limits.
        with self.bot.db:
            self.bot.learn_chars('甲乙丙' * 10)
        for length in (3, 50):
            class FixedLength:
                def randint(self, low, high):
                    assert (low, high) == (3, 50)
                    return length
                def randrange(self, total): return 0
            self.bot.rng = FixedLength()
            result = self.bot.generate('乙')
            self.assertEqual(len(result), length)
            self.assertTrue(result.startswith('丙'))
            self.assertEqual(result, ('丙甲乙' * 25)[:length])

    def test_dead_end_no_fabrication(self):
        self.send('/model generate')
        self.assertIsNone(self.send('你好'))
        self.assertIsNone(self.bot.generate('未'))

    def test_send_guard_and_literal_cq(self):
        with self.assertRaises(PermissionError): group_reply('NOT-A-REAL-GROUP', 'x')
        result = group_reply(TEST_GROUP, '[CQ:at,qq=all]')
        self.assertEqual(result['params']['group_id'], 312579123)
        self.assertEqual(result['params']['message'][0]['type'], 'text')

    def test_persistence_and_restart_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.sqlite3')
            bot = Bot(path, self_id='900000')
            bot.handle(self.event('你'))
            bot.handle(self.event('好'))
            bot.handle(self.event('/model generate'))
            event = self.event('你好你')
            bot.handle(event)
            bot.close()
            bot = Bot(path, self_id='900000')
            try:
                self.assertEqual(bot.model, 'generate')
                self.assertEqual(set(bot.db.execute('SELECT * FROM qa')),
                                 {('你','好',1), ('好','你好你',1)})
                self.assertIsNone(bot.previous)
                self.assertIsNone(bot.handle(event))
                self.assertEqual(bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0], 2)
            finally:
                bot.close()

    def test_invalid_and_out_of_order(self):
        from qbot import TIMESTAMP_TOLERANCE
        self.send('a', 1000)
        # Far older than the skew tolerance: a stale replay must still be refused.
        self.send('late', 1000 - TIMESTAMP_TOLERANCE - 1)
        self.send('invalid', float('nan'))
        self.send('invalid', message_id=None)
        self.send('b', 1001)
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa').fetchall(), [('a','b',1)])

    def test_generate_walks_punctuation_transitions(self):
        # Punctuation is learned and walked like any other character, so a reply
        # can contain 。，！？ instead of being one unbroken run of characters.
        with self.bot.db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('你', '好', 5), ('好', '，', 5), ('，', '你', 5)])

        class AlwaysFirst:
            def randint(self, low, high): return 3
            def randrange(self, total): return 0
        self.bot.rng = AlwaysFirst()
        self.assertEqual(self.bot.generate('你'), '好，你')
        self.assertIsNone(self.bot.generate('?'))   # Nothing follows an unseen char.

    def test_generate_mode_replies_when_the_message_ends_in_punctuation(self):
        bot = Bot(self_id='900000')
        self.addCleanup(bot.close)
        bot.handle(self.event('/model generate'))
        # A corpus in which 。 recurs, so a message ending in 。 has a continuation.
        bot.handle(self.event('甲乙，丙丁。甲乙，丙丁。'))

        class Fixed:
            def randint(self, low, high): return 3
            def randrange(self, total): return 0
        bot.rng = Fixed()
        result = bot.handle(self.event('丙丁。'))
        self.assertIsNotNone(result)
        self.assertEqual(result['params']['message'][0]['data']['text'], '甲乙，')
        # A character the corpus never saw has no continuation, so no reply.
        self.assertIsNone(bot.handle(self.event('甲乙？')))

    def test_clock_skew_does_not_block_later_messages(self):
        from qbot import TIMESTAMP_TOLERANCE
        bot = Bot(self_id='900000')
        self.addCleanup(bot.close)

        def learned():
            return bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0]

        # One event carrying a local-clock (future) timestamp used to silence the
        # group until real time caught up, because every later message looked old.
        bot.handle(self.event('未来的消息', time=1_000_000))
        self.assertEqual(bot.last_time, 1_000_000)
        before = learned()
        bot.handle(self.event('甲乙', time=1_000_000 - 120))
        self.assertGreater(learned(), before)          # slight skew: accepted
        # A genuinely stale message is still refused.
        stale = learned()
        bot.handle(self.event('丙丁', time=1_000_000 - TIMESTAMP_TOLERANCE - 60))
        self.assertEqual(learned(), stale)

    def test_pair_ban_cuts_generation_and_drops_the_reply(self):
        self.send('/model generate')
        self.send('甲乙丙')
        self.send('丁戊己庚辛')
        with self.bot.db as db:
            db.execute("INSERT INTO pair_bans VALUES ('乙','丙','测试',0)")

        class AlwaysFirst:
            def randint(self, low, high): return 3
            def randrange(self, total): return 0
        self.bot.rng = AlwaysFirst()
        # 甲→乙→丙 命中封禁对：生成在此截断，整条回复被丢弃
        self.assertIsNone(self.bot.generate('甲'))
        # 另一条不相干的链照常生成，说明拦截不影响其它内容
        self.assertEqual(self.bot.generate('丁'), '戊己庚')

    def test_pair_ban_suppresses_a_match_answer(self):
        bot = Bot(self_id='900000')
        self.addCleanup(bot.close)
        bot.handle(self.event('甲'))
        bot.handle(self.event('乙丙'))
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM qa').fetchone()[0], 1)
        with bot.db as db:
            db.execute("INSERT INTO pair_bans VALUES ('乙','丙','测试',0)")
        # match 会答「乙丙」，其中含被禁的 乙→丙 ⇒ 整条不发送
        self.assertIsNone(bot.handle(self.event('甲')))

    def test_pair_ban_suppresses_an_intelligence_answer(self):
        bot = Bot(self_id='900000')
        self.addCleanup(bot.close)
        bot.handle(self.event('/model intelligence'))
        bot.handle(self.event('今天天气不错'))
        bot.handle(self.event('今天天气不错'))
        with bot.db as db:
            db.execute("INSERT INTO pair_bans VALUES ('今','天','测试',0)")
        self.assertIsNone(bot.handle(self.event('今天天气不错')))

    def test_pair_ban_never_swallows_system_replies(self):
        bot = Bot(self_id='900000')
        self.addCleanup(bot.close)
        with bot.db as db:
            db.execute("INSERT INTO pair_bans VALUES ('已','切','测试',0)")
        # 系统固定提示不受出站封禁对影响，否则命令反馈会被静默吞掉
        result = bot.handle(self.event('/model generate'))
        self.assertIsNotNone(result)
        self.assertIn('已切换', result['params']['message'][0]['data']['text'])

    def test_group_models_and_learning_are_isolated(self):
        production = Bot(self_id='900000', group_id=PRODUCTION_GROUP)
        try:
            self.send('/model generate')
            self.assertEqual(self.bot.model, 'generate')
            self.assertEqual(production.model, 'match')
            event = self.event('production text', group_id=PRODUCTION_GROUP)
            self.assertIsNone(self.bot.handle(event))
            production.handle(event)
            production.handle(self.event('answer', group_id=PRODUCTION_GROUP))
            self.assertEqual(production.db.execute('SELECT * FROM qa').fetchall(),
                             [('production text','answer',1)])
            self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM qa').fetchone()[0], 0)
            self.assertEqual(group_reply(PRODUCTION_GROUP,'reply')['params']['group_id'],1050045834)
        finally:
            production.close()

    def test_fourth_group_learns_in_its_own_database(self):
        from qbot import FOURTH_GROUP, GROUP_DATABASES
        self.assertEqual(GROUP_DATABASES[FOURTH_GROUP], 'fourth.sqlite3')
        with closing(Bot(self_id='900000', group_id=FOURTH_GROUP)) as fourth:
            event = self.event('new group question', group_id=FOURTH_GROUP)
            self.assertIsNone(self.bot.handle(event))
            fourth.handle(event)
            fourth.handle(self.event('new group answer', group_id=FOURTH_GROUP))
            self.assertEqual(fourth.db.execute('SELECT q,a FROM qa').fetchall(),
                             [('new group question', 'new group answer')])
            self.assertEqual(self.bot.db.execute('SELECT COUNT(*) FROM qa').fetchone()[0], 0)
            self.assertEqual(group_reply(FOURTH_GROUP, 'reply')['params']['group_id'], 929618496)

    def test_both_modes_learn_both_models_without_duplicate_or_bot_training(self):
        from unittest.mock import patch
        for mode in ('match','generate'):
            with self.subTest(mode=mode):
                bot = Bot(self_id='900000')
                try:
                    bot.handle(self.event('/model '+mode))
                    with patch.object(bot,'generate',return_value='生成回复'):
                        bot.handle(self.event('甲乙'))
                        middle=self.event('丙丁')
                        bot.handle(middle)
                        bot.handle(copy.deepcopy(middle))
                        bot.handle(self.event('ignored bot text',user_id='900000'))
                        reply=bot.handle(self.event('甲乙'))
                    self.assertEqual(set(bot.db.execute('SELECT * FROM qa')),
                                     {('甲乙','丙丁',1),('丙丁','甲乙',1)})
                    self.assertEqual(set(bot.db.execute('SELECT * FROM chars')),
                                     {('甲','乙',2),('丙','丁',1)})
                    self.assertEqual(reply['params']['message'][0]['data']['text'],
                                     '丙丁' if mode=='match' else '生成回复')
                finally:
                    bot.close()

    def test_generate_mode_match_learning_still_respects_three_minutes(self):
        self.send('/model generate',0)
        self.send('甲乙',1)
        self.send('bot reply',100,user_id='900000')
        self.send('丙丁',182)
        self.send('戊己',183)
        self.assertEqual(self.bot.db.execute('SELECT * FROM qa').fetchall(),[('丙丁','戊己',1)])
        self.assertEqual(self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0],3)


if __name__ == '__main__':
    unittest.main()
