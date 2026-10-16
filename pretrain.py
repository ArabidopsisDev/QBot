"""Import the chronological final 20% of QQ text-export records into chars only."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from qbot import Bot, trigram_counts

ROOT = Path(__file__).resolve().parent
HEADER = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*?)[(<]([^()<>]+)[)>]$')
PLACEHOLDER = re.compile(r'\[(?:图片|表情|动画表情|视频|语音|文件|红包|转账|不支持的消息|QQ表情)\]')


def parse_export(text):
    records = []
    current = None
    for line in text.splitlines():
        match = HEADER.fullmatch(line)
        if match:
            if current is not None:
                records.append((*current[:2], '\n'.join(current[2]).strip('\n')))
            timestamp, name, user_id = match.groups()
            datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
            current = [timestamp, user_id, []]
        elif current is not None:
            current[2].append(line)
    if current is not None:
        records.append((*current[:2], '\n'.join(current[2]).strip('\n')))
    return records


def train(source, database, excluded=()):
    paths = sorted(source.glob('*.txt')) if source.is_dir() else [source]
    if not paths:
        raise ValueError('No text exports found')
    records, source_hashes = [], []
    for path in paths:
        raw = path.read_bytes()
        source_hashes.append(hashlib.sha256(raw).hexdigest())
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('gb18030')
        records.extend(parse_export(text))
    if not records:
        raise ValueError('No QQ message records recognized; no data imported')
    records.sort(key=lambda record: record[0])
    count = math.ceil(len(records) * .2)
    selected = records[-count:]
    excluded = set(excluded) | {'2854196310'}  # QQ group assistant is not human training data.
    counts, trained, attributed = Counter(), 0, Counter()
    trigrams = Counter()
    for _, sender, text in selected:
        if sender in excluded or text.strip().split(maxsplit=1)[0:1] in (['/model'], ['/clear']):
            continue
        pairs = Counter()
        windows = Counter()
        for chunk in PLACEHOLDER.split(text):
            pairs.update(zip(chunk, chunk[1:]))  # Punctuation included, as in live learning.
            # Same split and scope, so a window never spans an attachment.
            windows.update(trigram_counts(chunk))
        if pairs:
            counts.update(pairs)
            trigrams.update(windows)
            if sender:
                # Counter keys are (sender, first, second) and the value is the
                # weight: update() with an iterable would count whole tuples.
                attributed.update({(sender, a, b): w for (a, b), w in pairs.items()})
            trained += 1
    identity = hashlib.sha256(json.dumps({'sources':source_hashes, 'fraction':.2,
        'excluded':sorted(excluded), 'parser_version':1}, sort_keys=True).encode()).hexdigest()
    database.parent.mkdir(parents=True, exist_ok=True)
    Bot(str(database), self_id='schema-only').close()
    db = sqlite3.connect(database, timeout=30)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS pretraining (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            existing = db.execute('SELECT summary FROM pretraining WHERE id=?', (identity,)).fetchone()
            if existing:
                return dict(json.loads(existing[0]), already_imported=True)
            before_qa = db.execute('SELECT COUNT(*), COALESCE(SUM(w),0) FROM qa').fetchone()
            db.executemany('''INSERT INTO chars VALUES (?, ?, ?)
                ON CONFLICT(first,second) DO UPDATE SET w=w+excluded.w''',
                ((a,b,w) for (a,b),w in counts.items()))
            if trigrams:
                db.executemany('''INSERT INTO gen_trigrams VALUES (?, ?, ?, ?)
                    ON CONFLICT(older,first,second) DO UPDATE SET w=w+excluded.w''',
                    ((a,b,c,w) for (a,b,c),w in trigrams.items()))
            if attributed:
                db.executemany('''INSERT INTO user_chars VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id,first,second) DO UPDATE SET w=w+excluded.w''',
                    ((sender,a,b,w) for (sender,a,b),w in attributed.items()))
            assert db.execute('SELECT COUNT(*), COALESCE(SUM(w),0) FROM qa').fetchone() == before_qa
            summary = {'total_records':len(records), 'selected_records':count,
                'trained_records':trained, 'first_timestamp':selected[0][0],
                'last_timestamp':selected[-1][0], 'unique_transitions':len(counts),
                'added_transition_weight':sum(counts.values()), 'qa_unchanged':True,
                'unique_trigrams':len(trigrams),
                'added_trigram_weight':sum(trigrams.values()),
                'source_hashes':source_hashes}
            db.execute('INSERT INTO pretraining VALUES (?,?)', (identity,json.dumps(summary)))
        return dict(summary, already_imported=False)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'pre')
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'live.sqlite3')
    args = parser.parse_args()
    connection = ROOT / 'runtime' / 'connection.json'
    excluded = [json.loads(connection.read_text(encoding='utf-8'))['self_id']] if connection.exists() else []
    summary = train(args.source, args.db, excluded)
    destination = ROOT / 'data' / 'pretraining-report.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
