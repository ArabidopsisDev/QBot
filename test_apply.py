import random
import unittest

import qbot
from qbot import (APPLY_LIMIT, APPLY_MAX_SECONDS, APPLY_MIN_SECONDS, APPLY_QUIET,
                  APPLY_WINDOW, VIOLATION_WINDOW, Bot, CLEAR_ADMIN, TEST_GROUP,
                  human_seconds)


class ApplyRng(random.Random):
    """Deterministic grant length; every other Random method stays available."""

    def randint(self, low, high):
        assert (low, high) == (APPLY_MIN_SECONDS, APPLY_MAX_SECONDS)
        return 90


class Clock:
    def __init__(self, now=10000.0):
        self.now = now

    def __call__(self):
        return self.now


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bot = Bot(self_id='900000', clock=self.clock, rng=ApplyRng(7))
        self.addCleanup(self.bot.close)
        self.sequence = 0

    def event(self, text, user='800001'):
        self.sequence += 1
        return dict(post_type='message', message_type='group', group_id=TEST_GROUP,
            self_id='900000', user_id=user, message_id=str(self.sequence),
            time=float(self.sequence), message=[{'type':'text','data':{'text':text}}])

    def apply(self, user='800001'):
        return self.bot.handle(self.event('/apply', user))

    def collect(self, text, user='800001'):
        return self.bot.handle(self.event(text, user))

    def text_of(self, result):
        return result['params']['message'][0]['data']['text'] if result else None

    def count(self, table):
        return self.bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_any_member_may_apply_and_receives_a_random_grant(self):
        result = self.apply('800001')
        self.assertTrue(result['exempt'])
        self.assertEqual(self.text_of(result),
            '800001（QQ号），经自动审核，已成功发放1分30秒白名单资格\n'
            '当前白名单列表：\n1. 800001（QQ号），剩余1分30秒')
        self.assertEqual(self.count('whitelist'), 1)
        self.assertEqual(self.count('apply_log'), 1)

    def test_grant_length_stays_inside_the_configured_range(self):
        bot = Bot(self_id='900000', clock=self.clock)
        self.addCleanup(bot.close)
        for _ in range(200):
            seconds = bot.grant_whitelist('800009', self.clock.now)
            self.assertGreaterEqual(seconds, APPLY_MIN_SECONDS)
            self.assertLessEqual(seconds, APPLY_MAX_SECONDS)

    def test_list_shows_every_active_grant_with_its_remaining_time(self):
        self.apply('800001')
        self.clock.now += 10
        result = self.apply('800002')
        self.assertEqual(self.text_of(result),
            '800002（QQ号），经自动审核，已成功发放1分30秒白名单资格\n'
            '当前白名单列表：\n'
            '1. 800001（QQ号），剩余1分20秒\n'
            '2. 800002（QQ号），剩余1分30秒')

    def test_fourth_applicant_is_refused_while_the_list_is_full(self):
        for user in ('800001', '800002', '800003'):
            self.assertIn('已成功发放', self.text_of(self.apply(user)))
        self.assertEqual(self.count('whitelist'), APPLY_LIMIT)
        result = self.apply('800004')
        self.assertTrue(result['exempt'])
        self.assertEqual(self.text_of(result),
                         '800004（QQ号），由于白名单列表已满，暂无法发放资格')
        self.assertEqual(self.count('whitelist'), APPLY_LIMIT)

    def test_second_success_inside_a_day_is_refused(self):
        self.assertIn('已成功发放', self.text_of(self.apply('800001')))
        self.clock.now += APPLY_QUIET + 1
        self.assertEqual(self.text_of(self.apply('800001')),
            '800001（QQ号），您今天已成功申请过，无法发放第二次白名单资格')

    def test_success_is_possible_again_after_a_day(self):
        self.assertIn('已成功发放', self.text_of(self.apply('800001')))
        self.clock.now += APPLY_WINDOW + 1
        self.assertIn('已成功发放', self.text_of(self.apply('800001')))

    def test_unapproved_02_report_does_not_block_an_application(self):
        self.assertIsNone(self.collect('色情', '800001'))
        self.assertEqual(self.count('bans'),0)
        self.assertIn('已成功发放',self.text_of(self.apply('800001')))

    def test_the_violation_window_expires(self):
        self.collect('色情', '800001')
        self.clock.now += VIOLATION_WINDOW + 61
        self.assertIn('已成功发放', self.text_of(self.apply('800001')))

    def test_a_whitelisted_sender_is_never_screened_or_banned(self):
        self.apply('800001')
        self.assertIsNone(self.collect('你几把谁', '800001'))
        self.assertEqual(self.count('bans'), 0)
        self.assertEqual(self.count('violations'), 0)
        self.assertGreater(self.count('chars'), 0)  # Still learned, just not screened.

    def test_screening_resumes_once_the_grant_expires(self):
        self.apply('800001')
        self.clock.now += 91
        self.assertFalse(self.bot.whitelisted('800001', self.clock.now))
        self.assertIsNone(self.collect('你几把谁', '800001'))
        self.assertEqual(self.count('bans'), 0)
        # The expired row survives until the watcher announces and drops it.
        self.assertEqual(self.count('whitelist'), 1)
        self.assertEqual(self.bot.active_whitelist(self.clock.now), [])

    def test_repeat_attempts_are_ignored_silently(self):
        self.apply('800001')
        self.clock.now += 1
        self.assertIsNone(self.apply('800001'))
        self.clock.now += APPLY_QUIET + 1
        self.assertIsNotNone(self.apply('800001'))

    def test_apply_is_never_learned(self):
        self.apply('800001')
        self.assertEqual(self.count('chars'), 0)
        self.assertEqual(self.count('qa'), 0)
        self.assertEqual(self.count('intel_memory'), 0)

    def test_only_apply_notices_are_marked_exempt(self):
        self.assertTrue(self.apply('800001')['exempt'])
        admin = self.bot.handle(self.event('/model match', CLEAR_ADMIN))
        self.assertNotIn('exempt', admin)

    def test_expired_helper_only_reports_and_never_removes(self):
        self.apply('800001')
        self.assertEqual(qbot.expired_whitelist(self.bot.db, self.clock.now), [])
        self.assertEqual(qbot.expired_whitelist(self.bot.db, self.clock.now + 91), ['800001'])
        # Removal is the watcher's job, and only after the notice went out.
        self.assertEqual(qbot.expired_whitelist(self.bot.db, self.clock.now + 91), ['800001'])
        self.assertEqual(self.count('whitelist'), 1)
        self.assertEqual(self.bot.active_whitelist(self.clock.now + 91), [])

    def test_duration_formatting(self):
        self.assertEqual(human_seconds(0), '0分0秒')
        self.assertEqual(human_seconds(90), '1分30秒')
        self.assertEqual(human_seconds(3600), '60分0秒')


if __name__ == '__main__':
    unittest.main()
