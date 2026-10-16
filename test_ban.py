import unittest

from qbot import ADMIN_COMMANDS, Bot, CLEAR_ADMIN, TEST_GROUP


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class BanTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bot = Bot(self_id='900000', clock=self.clock)
        self.addCleanup(self.bot.close)
        self.sequence = 0

    def event(self, text, user=CLEAR_ADMIN, **changes):
        self.sequence += 1
        event = dict(post_type='message', message_type='group', group_id=TEST_GROUP,
            self_id='900000', user_id=user, message_id=str(self.sequence),
            time=self.sequence, message=[{'type':'text','data':{'text':text}}])
        event.update(changes)
        return event

    def say(self, text, user=CLEAR_ADMIN):
        result = self.bot.handle(self.event(text, user))
        return result['params']['message'][0]['data']['text'] if result else None

    def count(self, table):
        return self.bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def ban_row(self, user='123456789'):
        return self.bot.db.execute('SELECT until,seconds,mode FROM bans WHERE user_id=?',
                                   (user,)).fetchone()

    def test_new_commands_are_admin_only(self):
        commands = ('/ban 123456789 60', '/unban 123456789', '/save demo',
                    '/load demo', '/train demo', '/train list', '/dstx')
        self.assertEqual(set(ADMIN_COMMANDS),
                         {'/ban', '/unban', '/save', '/load', '/train', '/dstx'})
        for command in commands:
            for user in ('900001', '575586491 ', '5755864910'):
                with self.subTest(command=command, user=user):
                    self.assertIn('无权限', self.say(command, user))
        for user in (None, 575586491.0):
            for command in commands:
                with self.subTest(command=command, user=user):
                    self.assertIsNone(self.say(command, user))
        self.assertEqual(self.count('bans'), 0)

    def test_manual_ban_stops_training_and_replies(self):
        self.say('你好')
        answer = self.say('/ban 123456789 60 测试原因')
        self.assertIn('已封禁', answer)
        self.assertIn('手动封禁', answer)
        self.assertIn('测试原因', answer)
        self.assertEqual(self.count('bans'), 1)
        learned = (self.count('qa'), self.count('chars'))
        self.assertIsNone(self.say('机器人你好呀', user='123456789'))
        self.assertIsNone(self.say('第二条被封禁消息', user='123456789'))
        self.assertEqual((self.count('qa'), self.count('chars')), learned)

    def test_unban_restores_replies(self):
        self.say('你好')
        self.say('/ban 123456789 60')
        self.say('在吗', user='123456789')
        before = self.count('qa')
        self.assertIn('已移出封禁列表', self.say('/unban 123456789'))
        self.assertEqual(self.count('bans'), 0)
        self.say('晚上好', user='123456789')
        self.say('明天见', user='123456789')
        self.assertGreater(self.count('qa'), before)

    def test_unban_reports_missing_entry(self):
        self.assertIn('不在封禁列表', self.say('/unban 123456789'))
        self.assertEqual(self.count('bans'), 0)

    def test_expired_ban_is_dropped(self):
        self.say('/ban 123456789 60')
        self.assertEqual(self.count('bans'), 1)
        self.clock.now += 61
        self.bot.handle(self.event('测试消息', user='123456789'))
        self.assertEqual(self.count('bans'), 0)

    def test_usage_errors_never_ban(self):
        for command in ('/ban', '/ban 123456789', '/ban abc 60', '/ban 123456789 abc',
                        '/ban 123456789 0', '/ban 123456789 31536001',
                        '/ban 123456789 60 原因 多余',
                        '/unban', '/unban abc', '/unban 123456789 extra'):
            with self.subTest(command=command):
                self.say(command)
        self.assertEqual(self.count('bans'), 0)

    def test_reason_is_truncated_not_rejected(self):
        self.say('/ban 123456789 60 ' + 'x' * 50)
        self.assertEqual(len(self.bot.db.execute('SELECT reason FROM bans').fetchone()[0]), 40)

    def test_admin_account_cannot_be_banned(self):
        self.assertIn('不能封禁管理员', self.say(f'/ban {CLEAR_ADMIN} 60'))
        self.assertEqual(self.count('bans'), 0)

    def test_automatic_ban_notice_format(self):
        result = self.bot.handle(self.event('你几把谁啊', user='123456789'))
        self.assertIsNone(result)
        self.assertIsNone(self.ban_row())
        self.assertEqual(self.bot.quality.db.execute(
            'SELECT primary_category FROM gate_cases').fetchone()[0],'02IVb')

    def test_automatic_ban_lightest_grade(self):
        self.say('色情', user='123456789')
        self.assertIsNone(self.ban_row())

    def test_unapproved_sensitive_reports_do_not_escalate_or_ban(self):
        self.say('色情', user='123456789')
        self.clock.now += 120
        self.say('色情', user='123456789')
        self.clock.now += 200
        self.say('色情', user='123456789')
        self.assertIsNone(self.ban_row())
        self.assertEqual(self.bot.violation_count('123456789', self.clock.now), 0)

    def test_automatic_ban_does_not_train_the_violation(self):
        before = (self.count('qa'), self.count('chars'))
        self.say('几把', user='123456789')
        self.assertEqual((self.count('qa'), self.count('chars')), before)

    def test_innocent_words_never_trigger_automatic_ban(self):
        for text in ('吊车作业要注意安全', '他上周把驾照吊销了', '这个方案确实牛逼',
                     '真是个傻逼', '屌丝也有春天', '吊坠挺好看的', '逼近交付日期'):
            with self.subTest(text=text):
                self.say(text, user='123456789')
        self.assertEqual(self.count('bans'), 0)
        self.assertGreater(self.count('chars'), 0)

    def test_admin_is_exempt_from_automatic_ban(self):
        self.say('几把', user=CLEAR_ADMIN)
        self.assertEqual(self.count('bans'), 0)

    def test_ban_is_per_group(self):
        from qbot import PRODUCTION_GROUP
        production = Bot(self_id='900000', group_id=PRODUCTION_GROUP, clock=self.clock)
        self.addCleanup(production.close)
        self.say('/ban 123456789 60')
        self.assertEqual(self.count('bans'), 1)
        self.assertEqual(production.db.execute('SELECT COUNT(*) FROM bans').fetchone()[0], 0)
        production.handle(self.event('晚上好', user='123456789', group_id=PRODUCTION_GROUP))
        learned = production.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0]
        self.assertGreater(learned, 0)

    def linked_bots(self):
        """Build the authorized group bots sharing one peer map, the way live.py does."""
        from qbot import PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP
        bots = {TEST_GROUP: self.bot}
        for group in (PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP):
            peer = Bot(self_id='900000', group_id=group, clock=self.clock)
            self.addCleanup(peer.close)
            bots[group] = peer
        for bot in bots.values():
            bot.attach_peers(bots)
        return bots

    def test_dstx_releases_every_banned_account_in_every_group(self):
        from qbot import PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP
        bots = self.linked_bots()
        self.say('/ban 111111111 60')
        self.say('/ban 333333333 60')
        # The same account banned in two groups must appear only once.
        for group in (PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP):
            bots[group].handle(self.event('/ban 222222222 60', group_id=group))
        self.assertEqual(self.say('/dstx'),
            '大赦天下！解除封禁名单：\n1. 111111111\n2. 222222222\n3. 333333333')
        for group, bot in bots.items():
            with self.subTest(group=group):
                self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM bans').fetchone()[0], 0)

    def test_dstx_sorts_numerically(self):
        self.say('/ban 999999999 60')
        self.say('/ban 1000000000 60')
        self.assertEqual(self.say('/dstx'),
            '大赦天下！解除封禁名单：\n1. 999999999\n2. 1000000000')
        self.assertEqual(self.count('bans'), 0)

    def test_dstx_reports_an_empty_list(self):
        self.assertEqual(self.say('/dstx'),
                         '大赦天下！解除封禁名单：\n（当前没有封禁记录）')

    def test_dstx_rejects_extra_arguments(self):
        self.say('/ban 111111111 60')
        self.assertIn('用法', self.say('/dstx now'))
        self.assertEqual(self.count('bans'), 1)

    def test_dstx_is_never_learned(self):
        before = self.count('chars')
        self.say('/dstx')
        self.assertEqual(self.count('chars'), before)


if __name__ == '__main__':
    unittest.main()
