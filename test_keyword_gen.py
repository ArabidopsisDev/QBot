"""Offline keyword-seeded generation: learned edges only, bans, echoes, harness."""
import argparse
import hashlib
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

import keyword_gen
import keyword_reply
from keyword_reply import END, START, Bounds, Contexts, KeywordGenerator
from qbot import Bot

CORPUS = ['今天考试好难啊。', '数学考试我也没复习！', '考试结束去吃火锅吧', '火锅好吃吗？',
          '明天还要考试吗', '我觉得数学挺难的。', '晚上一起吃火锅！', '复习不完了啊',
          '物理考试比数学难', '吃完火锅去复习']


def model(texts=CORPUS, bans=()):
    bot = Bot(self_id='900000')
    with bot.db:
        for text in texts:
            bot.learn_chars(text)
        for a, b in bans:
            bot.db.execute("INSERT INTO pair_bans VALUES (?,?,'t',0)", (a, b))
    return bot


class KeywordGenTests(unittest.TestCase):
    def generator(self, texts=CORPUS, bans=(), **kwargs):
        bot = model(texts, bans)
        self.addCleanup(bot.close)
        return KeywordGenerator(bot.db, bounds=Bounds((t, 1) for t in texts), **kwargs)

    def test_every_adjacent_pair_of_a_reply_is_a_learned_edge(self):
        gen = self.generator()
        rng = random.Random(3)
        produced = 0
        for prompt in ('今天考试好难', '想吃火锅', '数学怎么复习'):
            for _ in range(20):
                found = gen.reply(prompt, rng)
                if found is None:
                    continue
                produced += 1
                self.assertIn(found.seed, found.text)
                self.assertTrue(set(found.seed) <= set(prompt))
                for pair in zip(found.text, found.text[1:]):
                    self.assertIn(pair, gen.edges)
                self.assertLessEqual(len(found.text), keyword_reply.MAX_REPLY)
                self.assertGreaterEqual(len(found.text), keyword_reply.MIN_REPLY)
        self.assertGreater(produced, 0)

    def test_keywords_skip_punctuation_function_words_and_unknown_letters(self):
        gen = self.generator(stop_top=0)
        keywords = gen.keyword_chars('我的考试！？123 abc😀龘')
        self.assertIn('考', keywords)
        for character in '我的！？123 abc😀龘':
            self.assertNotIn(character, keywords)
        self.assertEqual(gen.seeds('。。。！'), [])
        self.assertIsNone(gen.reply('的了吗？', random.Random(1)))

    def test_two_character_seeds_must_be_learned_edges(self):
        gen = self.generator(stop_top=0)
        spans = [span for span, _ in gen.seeds('考试火锅')]
        self.assertIn('考试', spans)
        self.assertIn('火锅', spans)
        self.assertNotIn('试火', spans)

    def test_banned_pairs_never_leave_the_generator(self):
        gen = self.generator(bans=[('火', '锅')])
        rng = random.Random(5)
        for _ in range(100):
            found = gen.reply('火锅好吃', rng)
            if found:
                self.assertNotIn('火锅', found.text)

    def test_echoing_the_input_is_not_a_reply(self):
        gen = self.generator(texts=['考试好难'] * 5, stop_top=0)
        rng = random.Random(2)
        for _ in range(30):
            self.assertIsNone(gen.reply('考试好难', rng))

    def test_same_seed_same_reply(self):
        gen = self.generator()
        first = [gen.reply('考试好难', random.Random(9)) for _ in range(3)]
        self.assertEqual(len({f.text if f else None for f in first}), 1)

    def test_boundary_rates_come_from_one_corpus_and_are_smoothed(self):
        bounds = Bounds([('甲乙', 3), ('乙丙', 1)])
        self.assertEqual(bounds.messages, 4)
        self.assertAlmostEqual(bounds.global_rate, 4 / 8)
        self.assertAlmostEqual(bounds.p_start('甲'), (3 + 4 * .5) / (3 + 4))
        self.assertAlmostEqual(bounds.p_end('丙'), (1 + 4 * .5) / (1 + 4))
        self.assertAlmostEqual(bounds.p_end('甲'), (0 + 2) / (3 + 4))
        self.assertFalse(Bounds())

    def test_heuristic_boundaries_never_cross_terminal_punctuation_backward(self):
        bot = model(['好的。考试加油'] * 3)
        self.addCleanup(bot.close)
        gen = KeywordGenerator(bot.db, stop_top=0)
        rng = random.Random(4)
        for _ in range(50):
            text = gen.grow('考', rng)
            if text:
                self.assertFalse(text.startswith('。'))
                self.assertNotIn('好的。考', text)

    def test_longest_copy(self):
        self.assertEqual(keyword_reply.longest_copy('今天考试', '昨天考试\x00今天下雨'), 3)
        self.assertEqual(keyword_reply.longest_copy('龘', '甲乙'), 0)


class ContextTests(unittest.TestCase):
    def generator(self, texts, chars_texts=None, **kwargs):
        bot = model(chars_texts or texts)
        self.addCleanup(bot.close)
        sentences = [(t, 1) for t in texts]
        return KeywordGenerator(bot.db, bounds=Bounds(sentences),
                                contexts=Contexts(sentences, 3), **kwargs)

    def test_contexts_count_distinct_messages_with_boundaries(self):
        contexts = Contexts([('甲乙丙', 5), ('甲乙丙', 1), ('丁甲乙丁', 1)], 3)
        self.assertEqual(contexts.forward['甲乙'], {'丙': 1, '丁': 1})
        self.assertEqual(contexts.forward['乙丙'], {END: 1})
        self.assertEqual(contexts.backward['甲乙'], {START: 1, '丁': 1})
        self.assertEqual(contexts.forward['丁甲乙'], {'丁': 1})

    def test_witten_bell_mixes_supported_contexts_only(self):
        texts = ['甲乙丙', '甲乙丙', '戊乙丁']
        gen = self.generator(texts, min_support=2)
        base = gen._base('乙', True)
        self.assertAlmostEqual(base['丙'], (1 - gen.bounds.p_end('乙')) * 2 / 3)
        mixed = gen.distribution('甲乙', True)
        # 甲乙 seen in 2 distinct messages... but identical texts count once.
        self.assertEqual(mixed, base)
        gen = self.generator(['甲乙丙', '甲乙丙。', '戊乙丁'], min_support=2)
        base = gen._base('乙', True)
        mixed = gen.distribution('甲乙', True)
        self.assertAlmostEqual(mixed['丙'], (2 + 1 * base['丙']) / (2 + 1))
        self.assertAlmostEqual(mixed['丁'], base['丁'] / 3)
        self.assertAlmostEqual(sum(mixed.values()), 1.0)

    def test_long_contexts_never_revive_unlearned_or_deleted_pairs(self):
        sentences = ['甲乙丙丁', '甲乙丙丁', '甲乙丙戊']
        gen = self.generator(sentences * 2, chars_texts=['甲乙丙戊', '乙丙'])
        dist = gen.distribution('乙丙', True)
        self.assertNotIn('丁', dist)
        self.assertAlmostEqual(sum(dist.values()), 1.0)

    def test_round_two_replies_use_learned_edges_and_prefer_complete(self):
        gen = self.generator(CORPUS * 2)
        rng = random.Random(7)
        produced = 0
        for prompt in ('今天考试好难', '想吃火锅', '数学怎么复习'):
            for _ in range(10):
                found = gen.reply(prompt, rng)
                if found is None:
                    continue
                produced += 1
                self.assertIn(found.seed, found.text)
                self.assertNotIn(END, found.text)
                self.assertNotIn(START, found.text)
                for pair in zip(found.text, found.text[1:]):
                    self.assertIn(pair, gen.edges)
        self.assertGreater(produced, 0)

    def test_round_two_respects_pair_bans(self):
        bot = model(CORPUS * 2, bans=[('火', '锅')])
        self.addCleanup(bot.close)
        sentences = [(t, 1) for t in CORPUS * 2]
        gen = KeywordGenerator(bot.db, bounds=Bounds(sentences), contexts=Contexts(sentences))
        rng = random.Random(5)
        for _ in range(60):
            found = gen.reply('火锅好吃', rng)
            if found:
                self.assertNotIn('火锅', found.text)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_snapshot_leaves_the_source_untouched(self):
        source = self.tmp / 'live.sqlite3'
        bot = Bot(str(source), self_id='900000')
        with bot.db:
            bot.learn_chars('今天考试好难')
        bot.close()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        copy = self.tmp / 'out' / 'copy.sqlite3'
        keyword_gen.snapshot(source, copy)
        db = sqlite3.connect(copy)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM chars').fetchone()[0], 5)
        db.close()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_compare_on_an_export_writes_reports_without_touching_it(self):
        lines = []
        for index, text in enumerate(CORPUS * 4):
            lines.append(f'2026-01-01 10:{index // 60:02d}:{index % 60:02d} 群友(10000{index % 3})')
            lines.append(text)
            lines.append('')
        lines += ['2026-01-02 10:00:00 管家(2854196310)', '欢迎新人', '',
                  '2026-01-02 10:00:01 群友(100000)', '/model generate', '']
        export = self.tmp / 'export.txt'
        export.write_text('\n'.join(lines), encoding='utf-8')
        before = export.read_bytes()
        out = self.tmp / 'work'
        args = argparse.Namespace(out=str(out), export=export, db=None, holdout=.25,
                                  exclude=[], prompts=50, prompts_file=None, seed=1,
                                  attempts=16, blend=0.0, refine=.30, group='312579123',
                                  heuristic_bounds=False, order=3, min_support=2)
        keyword_gen.compare(args)
        self.assertEqual(export.read_bytes(), before)
        metrics = json.loads((out / 'metrics.json').read_text(encoding='utf-8'))
        self.assertTrue(metrics['held_out'])
        self.assertEqual(metrics['source']['messages'], 40)
        self.assertEqual(metrics['baseline']['prompts'], 10)
        # The live model continues the prompt's last character by construction.
        if metrics['baseline']['no_reply_rate'] < 1:
            self.assertEqual(metrics['baseline']['continuation_edge_rate'], 1.0)
        rows = (out / 'compare.tsv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 11)
        key = json.loads((out / 'answer_key.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(key['0']), ['baseline', 'keyword'])
        self.assertIn('complete_rate', metrics['keyword'])
        self.assertIn('keyword_v1', metrics)
        v1 = json.loads((out / 'answer_key_v1.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(v1['0']), ['keyword', 'keyword_v1'])
        blind = (out / 'blind_pairs.tsv').read_text(encoding='utf-8')
        self.assertNotIn('keyword', blind.split('\n', 1)[1])
        self.assertNotIn('欢迎新人', (out / 'compare.tsv').read_text(encoding='utf-8'))
        # A reader fills the last column; the tally maps sides back and keeps no text.
        filled = blind.splitlines()
        choices = ['left', 'right', 'same', 'both_bad', 'LEFT', 'oops', '']
        for i in range(1, len(filled)):
            filled[i] += choices[(i - 1) % len(choices)]
        (out / 'blind_pairs.tsv').write_text('\n'.join(filled), encoding='utf-8')
        keyword_gen.tally(argparse.Namespace(out=str(out)))
        summary = json.loads((out / 'tally.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['pairs'], 10)
        self.assertEqual((summary['judged'], summary['invalid'], summary['blank']), (8, 1, 1))
        wins = [key[str(i)][0] for i in (0, 4, 7)] + [key['1'][1], key['8'][1]]
        self.assertEqual(summary['keyword_wins'], wins.count('keyword'))
        self.assertEqual(summary['baseline_wins'], wins.count('baseline'))
        self.assertEqual((summary['same'], summary['both_bad']), (2, 1))
        self.assertNotIn('考', (out / 'tally.json').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
