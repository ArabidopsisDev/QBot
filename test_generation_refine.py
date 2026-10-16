"""Bounded generation changes, prefix-only stopping and baseline compatibility."""
import random
import unittest
from unittest.mock import Mock, patch

from generation_refine import GeneratedPath, natural_stop, structural_issue
from qbot import Bot


class RefineTests(unittest.TestCase):
    def bot(self,**kwargs):
        bot=Bot(self_id='900000',**kwargs)
        self.addCleanup(bot.close)
        return bot

    def test_stop_keeps_the_last_complete_late_prefix_only(self):
        text='前面说完了。接下来也说完了！半截'
        stopped=natural_stop(text,len(text))
        self.assertTrue(text.startswith(stopped))
        self.assertTrue(stopped.endswith('！'))
        self.assertGreaterEqual(len(stopped),len(text)*.75)
        self.assertEqual(natural_stop('你好。剩下很长很长没有停点',15),
                         '你好。剩下很长很长没有停点')

    def test_urls_code_and_unclosed_quotes_are_not_stop_points(self):
        for text in ('网址 https://example.org/abc!?x=1',
                     '片段 `hello!?`还没说完','“这是未闭合的话！继续',
                     '这是 3.14159 还有数字'):
            self.assertEqual(natural_stop(text,len(text),fraction=.5),text)
        text='他说“这句话已经说完了！”尾巴'
        self.assertEqual(natural_stop(text,len(text),fraction=.5),'他说“这句话已经说完了！”')

    def test_ellipsis_and_emoji_sequences_are_not_split(self):
        text='原来是这样……后面'
        self.assertEqual(natural_stop(text,len(text),fraction=.5),'原来是这样……')
        text='这是什么…未完'
        self.assertEqual(natural_stop(text,len(text),fraction=.5),text)
        text='说完了吗！\u200d🤷未完'
        self.assertEqual(natural_stop(text,len(text),fraction=.5),text)

    def test_loop_checks_preserve_laughter_and_normal_sentences(self):
        self.assertTrue(structural_issue('甲乙丙'*8))
        self.assertTrue(structural_issue('！'*10))
        self.assertTrue(structural_issue('“这是没合上的话'))
        for text in ('哈哈哈哈哈哈哈哈哈哈哈','喵喵喵喵','今天天气不错！',
                     '👩\u200d💻👩\u200d💻',"that's fine",'https://a.example/(query)'):
            self.assertFalse(structural_issue(text),text)

    def test_zero_switch_does_not_touch_extra_rng_or_change_base_rng(self):
        bot=self.bot(rng=random.Random(10),generate_refine_probability=0,
                     refine_rng=Mock())
        peer=self.bot(rng=random.Random(10))
        for b in (bot,peer):
            with b.db:
                b.learn_chars('甲乙丙丁。甲乙丙戊！')
                b.learn_trigrams('甲乙丙丁。甲乙丙戊！')
        for _ in range(30):
            self.assertEqual(bot.generate('甲'),peer._generate_path('甲').text)
            self.assertEqual(bot.rng.getstate(),peer.rng.getstate())
        bot.refine_rng.random.assert_not_called()

    def test_enabled_branch_uses_at_most_three_paths_without_scoring(self):
        rng=Mock()
        rng.random.return_value=0
        rng.choice.side_effect=lambda values:values[0]
        bot=self.bot(generate_refine_probability=.3,refine_rng=rng)
        paths=[GeneratedPath('甲乙丙'*8,24),GeneratedPath('今天真不错！',7),
               GeneratedPath('明天一起走！',7)]
        with patch.object(bot,'_generate_path',side_effect=paths) as generate:
            self.assertEqual(bot.generate('甲'),'今天真不错！')
            self.assertEqual(generate.call_count,3)

    def test_banned_base_path_is_not_salvaged_by_a_safe_prefix(self):
        bot=self.bot(generate_refine_probability=.3,refine_rng=Mock())
        with patch.object(bot,'_generate_path',return_value=GeneratedPath(None,30,True)) as gen:
            self.assertIsNone(bot.generate('甲'))
            self.assertEqual(gen.call_count,1)
        bot.refine_rng.random.assert_not_called()

    def test_three_bad_paths_fall_back_to_original_without_more_retries(self):
        rng=Mock(); rng.random.return_value=0
        bot=self.bot(generate_refine_probability=.3,refine_rng=rng)
        path=GeneratedPath('甲乙丙'*8,24)
        with patch.object(bot,'_generate_path',return_value=path) as gen:
            self.assertEqual(bot.generate('甲'),path.text)
            self.assertEqual(gen.call_count,3)

    def test_training_repairs_only_strong_spans_in_all_models(self):
        bot=self.bot()
        raw='前言'+('甲乙丙丁戊己'*4)+'结尾'
        expected='前言甲乙丙丁戊己结尾'
        event={'post_type':'message','message_type':'group','group_id':bot.group_id,
               'self_id':bot.self_id,'user_id':'800000','message_id':'1','time':1000,
               'message':[{'type':'text','data':{'text':raw}}]}
        bot.handle(event)
        self.assertIsNotNone(bot.db.execute('SELECT 1 FROM intel_memory WHERE text=?',
                                           (expected,)).fetchone())
        self.assertIsNone(bot.db.execute('SELECT 1 FROM intel_memory WHERE text=?',
                                        (raw,)).fetchone())
        self.assertEqual(bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0],len(expected)-1)
        self.assertEqual(bot.db.execute('SELECT SUM(w) FROM gen_trigrams').fetchone()[0],len(expected)-2)
        self.assertEqual(bot.db.execute('SELECT SUM(removed) FROM repetition_repairs').fetchone()[0],18)
        self.assertEqual(bot.quality.score('800000'),100)

    def test_quality_training_skip_does_not_write_repair_audit(self):
        bot=self.bot()
        bot.quality.set_score('800000',0,'离线测试')
        event={'post_type':'message','message_type':'group','group_id':bot.group_id,
               'self_id':bot.self_id,'user_id':'800000','message_id':'1','time':1000,
               'message':[{'type':'text','data':{'text':'甲乙丙丁戊己'*4}}]}
        bot.handle(event)
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM repetition_repairs').fetchone()[0],0)
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM chars').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
