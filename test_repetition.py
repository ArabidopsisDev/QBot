import unittest

from repetition import dominant_repeat, strong_repeat_findings, strong_repeat_repair


class RepetitionTests(unittest.TestCase):
    def test_repair_keeps_one_copy_and_surrounding_prose(self):
        original = '开头'+('甲乙丙丁戊己'*4)+'后面的正常内容。'
        repaired, findings = strong_repeat_repair(original)
        self.assertEqual(repaired, '开头甲乙丙丁戊己后面的正常内容。')
        self.assertEqual(findings[0]['removed'], 18)
        self.assertIsNone(dominant_repeat(original))

    def test_multiple_runs_keep_their_original_order(self):
        original = '开头'+('甲乙丙丁'*6)+'中间'+('天地玄黄'*6)+'结尾'
        repaired, findings = strong_repeat_repair(original)
        self.assertEqual(repaired, '开头甲乙丙丁中间天地玄黄结尾')
        self.assertEqual(len(findings), 2)
        self.assertLess(findings[0]['end'], findings[1]['start'])

    def test_short_emphasis_laughter_numbers_urls_and_code_survive(self):
        examples = ('喵喵喵喵', '哈哈'*100, '啊'*100, '好的'*4, '别别别',
                    '0123'*10, 'https://example.com/'+'abcd'*20,
                    'www.example.com/'+('甲乙丙丁'*8),
                    '`'+('甲乙丙丁'*8)+'`', 'function(){'+('甲乙丙丁'*8)+'}')
        for text in examples:
            with self.subTest(text=text[:30]):
                self.assertEqual(strong_repeat_repair(text), (text, []))
                self.assertIsNone(dominant_repeat(text))

    def test_single_non_expressive_character_needs_sixteen_copies(self):
        self.assertEqual(strong_repeat_repair('的'*15), ('的'*15, []))
        self.assertEqual(strong_repeat_repair('的'*16)[0], '的')
        self.assertEqual(dominant_repeat('的'*16)['coverage'], 1.0)

    def test_block_needs_both_four_copies_and_24_characters(self):
        for text in ('甲乙丙丁戊己庚辛'*3, '甲乙丙丁戊'*4):
            self.assertEqual(strong_repeat_repair(text), (text, []))
        self.assertEqual(strong_repeat_repair('甲乙丙丁戊己'*4)[0], '甲乙丙丁戊己')
        self.assertEqual(strong_repeat_repair('甲乙'*12)[0], '甲乙')

    def test_scan_uses_full_sentence_and_has_a_hard_bound(self):
        prefix = ''.join(chr(0x5000+index) for index in range(160))
        text = prefix+'甲乙丙丁戊己'*4
        repaired, findings = strong_repeat_repair(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(repaired, prefix+'甲乙丙丁戊己')
        huge = '的'*10001
        self.assertEqual(strong_repeat_repair(huge), (huge, []))


if __name__ == '__main__':
    unittest.main()
