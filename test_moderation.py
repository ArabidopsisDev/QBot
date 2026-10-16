import unittest

import moderation
from moderation import (GRADES, GRADE_SECONDS, MAX_AUTO_BAN, MIN_AUTO_BAN,
                        classify, escalate, grade_reason, human_duration, level_of, match)


class DictionaryTests(unittest.TestCase):
    def test_grade_ladder_is_complete_and_monotonic(self):
        self.assertEqual(list(GRADES), [
            'Ia', 'Ib', 'Ic', 'IIa', 'IIb', 'IIc', 'IIIa', 'IIIb', 'IIIc',
            'IVa', 'IVb', 'Va', 'Vb'])
        self.assertEqual(MIN_AUTO_BAN, 60)
        self.assertEqual(MAX_AUTO_BAN, 86400)
        durations = [GRADE_SECONDS[grade] for grade in GRADES]
        self.assertEqual(durations, sorted(durations))
        self.assertEqual(len(set(durations)), len(durations))
        for grade in GRADES:
            with self.subTest(grade=grade):
                self.assertIn(grade, moderation.GRADE_LABELS)

    def test_every_level_has_at_least_two_sub_grades(self):
        by_level = {}
        for grade in GRADES:
            by_level.setdefault(level_of(grade), []).append(grade)
        self.assertEqual(sorted(by_level), ['I', 'II', 'III', 'IV', 'V'])
        for level, members in by_level.items():
            with self.subTest(level=level):
                self.assertGreaterEqual(len(members), 2)
                self.assertEqual(members, [g for g in GRADES if level_of(g) == level])

    def test_every_term_is_multi_character_or_unambiguously_vulgar(self):
        allowed_single = {'屄', '肏'}
        for term in moderation.DICTIONARY:
            with self.subTest(term=term):
                self.assertTrue(len(term) > 1 or term in allowed_single)

    def test_every_grade_holds_at_least_one_term(self):
        for grade in GRADES:
            with self.subTest(grade=grade):
                self.assertTrue([term for term, value in moderation.DICTIONARY.items()
                                 if value == grade])

    def test_generalized_characters_are_never_matched(self):
        for text in ('草稿还没写完', '去操场跑步', '草莓很好吃', '起草文件', '草药',
                     '吊车作业注意安全', '驾照被吊销了', '吊坠挺好看', '吊兰浇水',
                     '吊灯坏了', '这个方案牛逼', '真是个傻逼', '屌丝逆袭',
                     '逼近截止日期', '逼真得像照片'):
            with self.subTest(text=text):
                self.assertIsNone(match(text))

    def test_measure_word_phrase_never_fires(self):
        for text in ('买了好几把椅子', '桌上有十几把刀', '借了几把伞',
                     '带了几把剪刀', '发了几把钥匙', '几十把梳子'):
            with self.subTest(text=text):
                self.assertIsNone(match(text))

    def test_heaviest_match_wins(self):
        self.assertEqual(match('你几把谁啊'), ('IVb', '几把'))
        self.assertEqual(match('说鸡巴呢'), ('IVb', '鸡巴'))
        self.assertEqual(match('几把 强奸 哪个重'), ('Vb', '强奸'))

    def test_spaced_and_full_width_spellings_still_match(self):
        for text in ('几 把', '几　把', '几-把', '鸡 巴'):
            with self.subTest(text=text):
                self.assertIsNotNone(match(text))

    def test_invalid_input_is_ignored(self):
        for value in (None, '', 5, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(match(value))


class ClassifyTests(unittest.TestCase):
    def test_samples_span_the_whole_ladder(self):
        samples = {'色情': 'Ia', '咸湿': 'Ib', '裸聊': 'Ic',
                   '骚货': 'IIa', '妓女': 'IIb', '约炮': 'IIc',
                   '自慰': 'IIIa', '淫荡': 'IIIb', '撸管': 'IIIc',
                   '龟头': 'IVa', '鸡巴': 'IVb',
                   '口交': 'Va', '强奸': 'Vb'}
        for text, grade in samples.items():
            with self.subTest(text=text):
                found = classify(text)
                self.assertEqual(found[0], grade)
                self.assertEqual(found[2], GRADE_SECONDS[grade])

    def test_prior_violations_escalate_one_step_each(self):
        self.assertEqual(escalate('Ia', 0), 'Ia')
        self.assertEqual(escalate('Ia', 1), 'Ib')
        self.assertEqual(escalate('Ia', 2), 'Ic')
        self.assertEqual(escalate('Ia', 3), 'IIa')
        self.assertEqual(escalate('IVb', 1), 'Va')
        self.assertEqual(escalate('Vb', 5), 'Vb')          # Capped at Vb.
        self.assertEqual(escalate('Ia', None), 'Ia')       # Defensive.
        self.assertEqual(escalate('Ia', -3), 'Ia')

    def test_escalation_walks_the_ladder_in_order(self):
        seen = [escalate('Ia', step) for step in range(len(GRADES) + 3)]
        self.assertEqual(seen[:len(GRADES)], list(GRADES))
        self.assertEqual(seen[len(GRADES):], ['Vb'] * 3)

    def test_classify_applies_the_escalated_duration(self):
        self.assertEqual(classify('色情', 0)[2], 60)
        self.assertEqual(classify('色情', 1)[2], 180)
        self.assertEqual(classify('色情', 2)[2], 300)
        self.assertEqual(classify('色情', 3)[2], 600)
        self.assertEqual(classify('强奸', 3)[2], GRADE_SECONDS['Vb'])

    def test_reason_names_the_grade(self):
        self.assertEqual(grade_reason('IVb', '几把'), '色情词汇「几把」（IVb级·严重）')
        self.assertEqual(grade_reason('Ia', '色情'), '色情词汇「色情」（Ia级·轻微）')


class DurationTests(unittest.TestCase):
    def test_duration_rendering(self):
        self.assertEqual(human_duration(60), '1 分钟')
        self.assertEqual(human_duration(180), '3 分钟')
        self.assertEqual(human_duration(600), '10 分钟')
        self.assertEqual(human_duration(3600), '1 小时')
        self.assertEqual(human_duration(21600), '6 小时')
        self.assertEqual(human_duration(43200), '12 小时')
        self.assertEqual(human_duration(64800), '18 小时')
        self.assertEqual(human_duration(86400), '24 小时')
        self.assertEqual(human_duration(90), '90 秒')

    def test_documentation_table_covers_every_grade(self):
        rows = moderation.grade_table()
        self.assertEqual([row[0] for row in rows], list(GRADES))
        for grade, label, seconds, terms in rows:
            with self.subTest(grade=grade):
                self.assertTrue(terms, f'{grade} has no terms')
                self.assertEqual(seconds, GRADE_SECONDS[grade])
                self.assertTrue(label)


if __name__ == '__main__':
    unittest.main()
