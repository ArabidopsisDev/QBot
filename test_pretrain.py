import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from pretrain import parse_export, train


class PretrainTests(unittest.TestCase):
    def test_tail_only_characters_no_cross_message_pairs_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'export.txt'
            texts = ['early']*8 + ['甲乙[图片]丙丁', 'xy']
            source.write_text('\n\n'.join(f'2026-01-01 00:00:{i:02d} User(12345)\n{text}'
                for i,text in enumerate(texts)), encoding='utf-8-sig')
            db = root / 'test.sqlite3'
            summary = train(source, db)
            self.assertEqual(summary['selected_records'], 2)
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(set(connection.execute('SELECT * FROM chars')),
                    {('甲','乙',1), ('丙','丁',1), ('x','y',1)})
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM qa').fetchone()[0], 0)
            self.assertTrue(train(source, db)['already_imported'])
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT SUM(w) FROM chars').fetchone()[0], 3)

    def test_multiline_and_sender_header_removed(self):
        records = parse_export('title\n2026-01-01 00:00:00 Name(12345)\na\nb\n\n')
        self.assertEqual(records, [('2026-01-01 00:00:00','12345','a\nb')])


if __name__ == '__main__':
    unittest.main()
