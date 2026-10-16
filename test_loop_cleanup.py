import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qbot import Bot, TEST_GROUP
from loop_cleanup import autoclean_memory, clean_memory, plan_memory_cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root/'model.sqlite3'
        self.bot = Bot(str(self.path), group_id=TEST_GROUP, self_id='900000')
        self.addCleanup(self.bot.close)
        self.db = self.bot.db

    def seed(self, text):
        self.bot.learn_chars(text, '700001')
        self.bot.learn_trigrams(text)
        self.bot.intelligence.observe(text, 1)
        self.db.commit()
        return self.db.execute('SELECT id FROM intel_memory WHERE text=?',(text,)).fetchone()[0]

    def test_auto_cleanup_caps_twenty_and_protects_all_weights(self):
        for index in range(25):
            self.seed(chr(0x4e00+index)+'甲乙丙丁戊己'*4)
        before = {table:self.db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                  for table in ('chars','gen_trigrams','user_chars')}
        result = autoclean_memory(self.db, TEST_GROUP, self.path, self.root/'backups', 1000)
        self.assertEqual(result['sentences_removed'], 20)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0], 5)
        for table, rows in before.items():
            self.assertEqual(self.db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall(), rows)
        with closing(sqlite3.connect(result['backup'])) as backup:
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0], 25)

    def test_backup_failure_and_wrong_path_never_delete(self):
        self.seed('甲乙丙丁戊己'*4)
        with patch('loop_cleanup.backup_database',side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):
                autoclean_memory(self.db,TEST_GROUP,self.path,self.root/'backups')
        with self.assertRaises(ValueError):
            autoclean_memory(self.db,TEST_GROUP,self.root/'wrong.sqlite3',self.root/'backups')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],1)

    def test_changed_related_qa_invalidates_preview(self):
        mid = self.seed('甲乙丙丁戊己'*4)
        plan = plan_memory_cleanup(self.db, TEST_GROUP, [mid])
        self.db.execute('INSERT INTO qa VALUES (?,?,1)',('正常问题','甲乙丙丁戊己'*4)); self.db.commit()
        with self.assertRaises(ValueError):
            clean_memory(self.db,TEST_GROUP,[mid],self.path,self.root/'backups',plan['plan_token'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0],1)

    def test_active_training_transaction_is_rejected(self):
        self.seed('甲乙丙丁戊己'*4)
        self.db.execute("INSERT INTO qa VALUES ('甲','乙',1)")
        with self.assertRaises(ValueError):
            autoclean_memory(self.db,TEST_GROUP,self.path,self.root/'backups')
        self.db.rollback()

    def test_no_match_or_zero_limit_does_not_create_backup(self):
        self.seed('哈哈'*20)
        for limit in (0,20):
            result=autoclean_memory(self.db,TEST_GROUP,self.path,self.root/'backups',limit)
            self.assertEqual(result['sentences_removed'],0)
        self.assertFalse((self.root/'backups').exists())


if __name__ == '__main__':
    unittest.main()
