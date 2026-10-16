"""Backed-up memory cleanup; anonymous character counts are never subtracted."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from repetition import dominant_repeat

SCAN_LIMIT = 5000
DELETE_LIMIT = 20


def memory_rows(db, limit=SCAN_LIMIT):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intel_memory'").fetchone():
        return []
    found = []
    for memory_id, text, weight, last_seen in db.execute(
            'SELECT id,text,w,last_seen FROM intel_memory ORDER BY w DESC,id LIMIT ?',
            (min(max(int(limit), 1), SCAN_LIMIT),)):
        finding = dominant_repeat(text)
        if finding:
            found.append({'id': memory_id, 'text': text, 'w': weight,
                          'last_seen': last_seen, 'score': finding['coverage'],
                          'width': finding['width'], 'repeats': finding['repeats'],
                          'length': len(text), 'finding': finding})
    return sorted(found, key=lambda row: (-row['score'], -row['w'], row['id']))


def plan_memory_cleanup(db, group, ids):
    ids = sorted(set(ids))
    if not ids or any(type(value) is not int or value <= 0 for value in ids):
        raise ValueError('请选择有效的记忆编号')
    if len(ids) > 200:
        raise ValueError('一次最多选择 200 条记忆')
    placeholders = ','.join('?' for _ in ids)
    rows = db.execute('SELECT id,text,w,last_seen FROM intel_memory WHERE id IN ('+
                      placeholders+') ORDER BY id', tuple(ids)).fetchall()
    if len(rows) != len(ids):
        raise ValueError('所选语句已变化或不存在，请重新预览')
    sentences = []
    for memory_id, text, weight, last_seen in rows:
        finding = dominant_repeat(text)
        if finding is None:
            raise ValueError('所选语句不满足强重复条件，请刷新列表')
        sentences.append({'id': memory_id, 'text': text, 'w': weight,
                          'last_seen': last_seen, 'length': len(text)})
    texts = [row['text'] for row in sentences]
    marks = ','.join('?' for _ in texts)
    qa = db.execute('SELECT q,a,w FROM qa WHERE q IN ('+marks+
                    ') OR a IN ('+marks+') ORDER BY q,a', tuple(texts)*2).fetchall()
    links = db.execute('SELECT qid,aid,w,last_seen FROM intel_links WHERE qid IN ('+
        placeholders+') OR aid IN ('+placeholders+') ORDER BY qid,aid', tuple(ids)*2).fetchall()
    terms = db.execute('SELECT term,memory_id,weight FROM intel_terms WHERE memory_id IN ('+
                       placeholders+') ORDER BY term,memory_id', tuple(ids)).fetchall()
    # A plan is bound to its group and every row that will be deleted.
    payload = json.dumps({'group': str(group), 'sentences': sentences,
                          'qa': qa, 'links': links, 'terms': terms},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    token = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return {'group': str(group), 'plan_token': token, 'sentences': sentences,
            'qa_rows': len(qa), 'protected_tables': ['chars','gen_trigrams','user_chars'],
            'changes': [], 'deleted_pairs': 0, 'reduced_pairs': 0,
            'weight_removed': 0,
            'weight_before': db.execute('SELECT COALESCE(SUM(w),0) FROM chars').fetchone()[0]}


def backup_database(db, group, database_path, backup_root):
    """Back up through a separate read connection before taking the write lock.

    Using backup() on the caller inside its own write transaction can deadlock.
    A live path must be the same database opened by the supplied connection.
    """
    actual = db.execute('PRAGMA database_list').fetchone()[2]
    if not actual:
        # In-memory connections are used only by offline callers/tests.
        raise ValueError('清理需要可备份的数据库文件')
    source_path = Path(database_path).resolve()
    if Path(actual).resolve() != source_path:
        raise ValueError('备份数据库路径与当前连接不一致')
    if db.in_transaction:
        raise ValueError('清理前必须完成当前训练事务')
    root = Path(backup_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_group = str(group)
    if not safe_group.isdigit():
        raise ValueError('群编号无效')
    name = 'memory-clean-'+safe_group+'-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3'
    destination = root/name
    with closing(sqlite3.connect(source_path.as_uri()+'?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(destination)) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise OSError('清理备份完整性检查失败')
    return str(destination)


def _delete_memory(db, plan):
    ids = [row['id'] for row in plan['sentences']]
    texts = [row['text'] for row in plan['sentences']]
    marks = ','.join('?' for _ in ids)
    db.execute('DELETE FROM intel_terms WHERE memory_id IN ('+marks+')', tuple(ids))
    db.execute('DELETE FROM intel_links WHERE qid IN ('+marks+') OR aid IN ('+marks+')', tuple(ids)*2)
    db.execute('DELETE FROM intel_memory WHERE id IN ('+marks+')', tuple(ids))
    text_marks = ','.join('?' for _ in texts)
    removed = db.execute('DELETE FROM qa WHERE q IN ('+text_marks+') OR a IN ('+text_marks+')',
                         tuple(texts)*2).rowcount
    return {'sentences_removed': len(ids), 'qa_rows_removed': removed,
            'pairs_deleted': 0, 'pairs_reduced': 0, 'weight_removed': 0,
            'weight_after': db.execute('SELECT COALESCE(SUM(w),0) FROM chars').fetchone()[0],
            'protected_tables': plan['protected_tables']}


def clean_memory(db, group, ids, database_path, backup_root, plan_token):
    initial = plan_memory_cleanup(db, group, ids)
    if not isinstance(plan_token, str) or initial['plan_token'] != plan_token:
        raise ValueError('预览已失效或目标群不一致，请重新预览')
    backup = backup_database(db, group, database_path, backup_root)
    try:
        db.execute('BEGIN IMMEDIATE')
        current = plan_memory_cleanup(db, group, ids)
        if current['plan_token'] != plan_token:
            raise ValueError('预览后数据已变化，请重新预览')
        result = _delete_memory(db, current)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return dict(result, backup=backup, group=str(group))


def autoclean_memory(db, group, database_path, backup_root, limit=DELETE_LIMIT):
    """Delete <=20 dominant strong-repeat memories, safely and with a backup."""
    limit = min(max(int(limit), 0), DELETE_LIMIT)
    if not limit:
        return {'sentences_removed': 0, 'group': str(group)}
    rows = memory_rows(db)[:limit]
    if not rows:
        return {'sentences_removed': 0, 'group': str(group)}
    ids = [row['id'] for row in rows]
    plan = plan_memory_cleanup(db, group, ids)
    return clean_memory(db, group, ids, database_path, backup_root, plan['plan_token'])
