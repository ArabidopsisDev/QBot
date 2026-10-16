import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import datastore
import qbot
from qbot import Bot, CLEAR_ADMIN, TEST_GROUP


class DatastoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bot = Bot(self_id='900000', pre_root=self.root)
        self.addCleanup(self.bot.close)
        self.sequence = 0

    def event(self, text, user=CLEAR_ADMIN):
        self.sequence += 1
        return dict(post_type='message', message_type='group', group_id=TEST_GROUP,
            self_id='900000', user_id=user, message_id=str(self.sequence),
            time=self.sequence, message=[{'type':'text','data':{'text':text}}])

    def say(self, text, user=CLEAR_ADMIN):
        result = self.bot.handle(self.event(text, user))
        return result['params']['message'][0]['data']['text'] if result else None

    def count(self, table):
        return self.bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def snapshot(self):
        db = self.bot.db
        return {
            'qa': db.execute('SELECT q,a,w FROM qa ORDER BY q,a').fetchall(),
            'chars': db.execute('SELECT first,second,w FROM chars ORDER BY first,second').fetchall(),
            'intel_memory': db.execute('SELECT id,text,w,last_seen FROM intel_memory ORDER BY id').fetchall(),
            'intel_terms': db.execute('SELECT term,memory_id,weight FROM intel_terms ORDER BY memory_id,term').fetchall(),
            'intel_links': db.execute('SELECT qid,aid,w,last_seen FROM intel_links ORDER BY qid,aid').fetchall(),
        }

    def write_source(self, reference, content):
        path = datastore.source_file_for(self.root, reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_folder_for_rejects_escaping_names(self):
        for name in ('../evil', '..', 'a/b', 'a\\b', 'con', 'COM1', 'x' * 33, '',
                     'a.b', 'a b', 'a"b', None, 5):
            with self.subTest(name=name), self.assertRaises(ValueError):
                datastore.folder_for(self.root, name)
        self.assertEqual(datastore.folder_for(self.root, '正常-名称_1').name, '正常-名称_1')
        self.assertEqual(datastore.folder_for(self.root, 'ok').parent, self.root)

    def test_save_then_load_round_trip(self):
        self.say('你好啊')
        self.say('今天天气不错')
        before = self.snapshot()
        self.assertIn('已保存', self.say('/save 我的备份'))
        self.assertTrue((self.root / '我的备份' / 'data.json').is_file())
        self.say('/clear match')
        self.say('/clear generate')
        self.say('/clear intelligence')
        self.assertNotEqual(self.snapshot(), before)
        self.assertIn('已加载', self.say('/load 我的备份'))
        self.assertEqual(self.snapshot(), before)

    def test_load_replaces_rather_than_merges(self):
        self.say('甲消息')
        self.say('乙消息')
        self.say('/save 基线')
        saved = self.snapshot()
        self.say('丙消息')
        self.say('丁消息')
        self.assertNotEqual(self.snapshot(), saved)
        self.say('/load 基线')
        self.assertEqual(self.snapshot(), saved)

    def test_load_reports_missing_folder(self):
        self.assertIn('加载失败', self.say('/load 不存在'))

    def test_save_rejects_bad_name(self):
        for name in ('..', 'a/b', 'x' * 33):
            with self.subTest(name=name):
                self.assertIn('保存失败', self.say(f'/save {name}'))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_train_reads_text_folder_and_keeps_existing_data(self):
        self.say('原有内容')
        self.write_source('样例',
            '2024-01-01 10:00:00 甲(111111)\n你好世界\n'
            '2024-01-01 10:00:10 乙(222222)\n今天天气不错\n'
            '2024-01-01 10:00:20 甲(111111)\n确实挺好的\n')
        answer = self.say('/train 样例')
        self.assertIn('已训练', answer)
        self.assertIn('训练 3 条消息', answer)
        for question, reply in (('你好世界', '今天天气不错'), ('今天天气不错', '确实挺好的')):
            with self.subTest(question=question):
                row = self.bot.db.execute('SELECT w FROM qa WHERE q=? AND a=?',
                                          (question, reply)).fetchone()
                self.assertEqual(row, (1,))
        self.assertIsNotNone(
            self.bot.db.execute("SELECT w FROM chars WHERE first='原'").fetchone())

    def test_train_is_additive_on_a_second_run(self):
        self.write_source('增量', '普通文本行一\n普通文本行二\n')
        self.say('/train 增量')
        first = self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0]
        self.say('/train 增量')
        self.assertGreater(self.bot.db.execute('SELECT SUM(w) FROM chars').fetchone()[0], first)

    def test_train_reports_missing_file(self):
        self.assertIn('训练失败', self.say('/train 不存在'))

    def test_train_list_shows_folders_with_their_files(self):
        for reference in ('alpha/one', 'alpha/two', 'beta/three'):
            self.write_source(reference, '内容')
        (self.root / 'text' / 'gamma').mkdir()
        self.assertEqual(self.say('/train list'),
                         '可训练数据集：\nalpha\n- one\n- two\nbeta\n- three')

    def test_train_list_lists_loose_files_last(self):
        self.write_source('alpha/one', '内容')
        self.write_source('loose', '内容')
        self.assertEqual(self.say('/train list'),
                         '可训练数据集：\nalpha\n- one\nloose')

    def test_train_list_hides_only_the_txt_suffix(self):
        self.write_source('alpha/one', '内容')
        (self.root / 'text' / 'alpha' / 'notes.md').write_text('x', encoding='utf-8')
        (self.root / 'text' / 'alpha' / 'nested').mkdir()
        self.assertEqual(self.say('/train list'),
                         '可训练数据集：\nalpha\n- notes.md\n- one')

    def test_train_list_is_a_subcommand_not_a_file_name(self):
        self.write_source('list', '普通文本行一\n普通文本行二\n')
        self.assertEqual(self.say('/train list'), '可训练数据集：\nlist')
        self.assertEqual(self.count('qa'), 0)
        self.assertEqual(self.count('chars'), 0)

    def test_nested_file_named_list_is_still_trainable(self):
        self.write_source('alpha/list', '普通文本行一\n普通文本行二\n')
        self.assertIn('已训练', self.say('/train alpha/list'))
        self.assertGreater(self.count('chars'), 0)

    def test_train_list_reports_an_empty_folder(self):
        (self.root / 'text').mkdir()
        self.assertEqual(self.say('/train list'),
                         '可训练数据集：\n（pre/text 目录为空，没有可训练的文件。）')

    def test_train_list_reports_a_missing_folder(self):
        self.assertIn('列出失败', self.say('/train list'))

    def test_train_list_rejects_extra_arguments(self):
        self.assertIn('用法', self.say('/train list extra'))

    def test_train_list_stays_within_the_reply_length_limit(self):
        for index in range(40):
            self.write_source(f'文件夹{index:04d}/文件', '内容')
        with patch.object(qbot, 'MAX_TEXT', 120):
            answer = self.say('/train list')
        self.assertLessEqual(len(answer), 120)
        self.assertIn('仅列出前', answer)
        # A folder line is never left dangling without its file lines.
        lines = answer.split('\n')
        self.assertTrue(lines[-1].startswith('…共'))
        self.assertTrue(lines[-2].startswith('- '))

    def test_train_skips_commands_and_own_messages(self):
        self.write_source('过滤',
            '2024-01-01 10:00:00 甲(111111)\n/model generate\n'
            '2024-01-01 10:00:05 机器人(900000)\n我是机器人\n'
            '2024-01-01 10:00:10 乙(222222)\n正常内容\n')
        answer = self.say('/train 过滤')
        self.assertIn('训练 1 条消息', answer)
        self.assertIn('跳过 2 条', answer)
        self.assertEqual(self.count('qa'), 0)


if __name__ == '__main__':
    unittest.main()
