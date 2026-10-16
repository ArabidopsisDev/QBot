"""Clear the application-gating state in every group: violations and the daily
application log. It deliberately does NOT touch active bans or active whitelist
grants.

Usage:
    python -X utf8 reset_apply_state.py            # dry run, shows what would go
    python -X utf8 reset_apply_state.py --apply    # actually clear
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

import qbot

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
CLEARED = ('violations', 'violation_log', 'apply_log', 'apply_attempts')
KEPT = ('bans', 'whitelist')


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='perform the deletion')
    args = parser.parse_args()

    print('模式：' + ('执行清理' if args.apply else '只读盘点（加 --apply 才会真正删除）'))
    print()
    total = 0
    for group, filename in qbot.GROUP_DATABASES.items():
        path = DATA / filename
        if not path.is_file():
            print(f'{group}  {filename}: 文件不存在，跳过')
            continue
        db = sqlite3.connect(str(path), timeout=10)
        try:
            print(f'{group}  {filename}')
            for table in CLEARED:
                if not table_exists(db, table):
                    print(f'    {table:18} 表不存在')
                    continue
                count = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                total += count
                print(f'    {table:18} {count} 行' + ('  -> 清空' if args.apply else ''))
            for table in KEPT:
                if table_exists(db, table):
                    count = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    print(f'    {table:18} {count} 行  （保留，不动）')
            if args.apply:
                with db:
                    for table in CLEARED:
                        if table_exists(db, table):
                            db.execute(f'DELETE FROM {table}')
                after = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in CLEARED if table_exists(db, table)}
                print(f'    清理后：{after}')
        finally:
            db.close()
    print()
    print(f'合计涉及 {total} 行。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
