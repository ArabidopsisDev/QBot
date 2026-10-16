"""Versioned, auditable gate events and conservative user-quality scaffolding.

No uncalibrated penalty is applied: users currently remain at 100/100. The
same score will later gate training and replies independently once a reviewed
calibration supplies a non-100 value.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LONG_CLASSES = ((100,'01a'),(150,'01b'),(200,'01c'))
GRADE_RE = re.compile(r'\b(I{1,3}|IV|V)([abc])级')
VALID_02 = frozenset(('M','Ia','Ib','Ic','IIa','IIb','IIc',
                      'IIIa','IIIb','IIIc','IVa','IVb','Va','Vb'))
SHANGHAI = timezone(timedelta(hours=8))


def score_label(score):
    if type(score) is not int or not 0<=score<=100:
        raise ValueError('Invalid quality score')
    for minimum,label in ((100,'无可挑剔'),(85,'优秀'),(70,'良好'),
                          (60,'中等'),(40,'较差'),(0,'很差')):
        if score>=minimum:
            return label


def score_week(now):
    local=datetime.fromtimestamp(now,SHANGHAI)
    start=(local-timedelta(days=local.weekday())).replace(
        hour=0,minute=0,second=0,microsecond=0)
    return start.strftime('%Y-%m-%d'),start+timedelta(days=7)


def length_gate(text):
    """The user's 'greater than 66' boundary wins over the 66-100 label."""
    if not isinstance(text,str) or len(text)<=66:
        return None
    for limit,label in LONG_CLASSES:
        if len(text)<=limit:
            return label
    return '01d'


def ban_grade(group_db, user_id, record):
    if record['mode'] != '自动封禁':
        return 'M'
    when = record['created_at']
    row = group_db.execute('''SELECT grade,at FROM violation_log WHERE user_id=?
        ORDER BY ABS(at-?) LIMIT 1''',(user_id,when)).fetchone()
    if row and abs(row[1]-when)<1 and row[0] in VALID_02:
        return row[0]
    match = GRADE_RE.search(record['reason'])
    if match:
        grade=''.join(match.groups())
        if grade in VALID_02:
            return grade
    return 'M'  # Unknown old records get the conservative manual category.


class QualityLedger:
    def __init__(self, path=':memory:', *, clock=time.time, rng=None):
        self.path=str(path)
        if self.path!=':memory:':
            Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=5)
        self.clock=clock
        self.rng=rng if rng is not None else random.Random()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, score INTEGER NOT NULL DEFAULT 100
                CHECK(score BETWEEN 0 AND 100), score_version TEXT NOT NULL
                DEFAULT 'uncalibrated', updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                gate TEXT NOT NULL, category TEXT NOT NULL,
                user_id TEXT NOT NULL, content_md5 TEXT,
                ban_instance TEXT, occurred_at REAL NOT NULL,
                PRIMARY KEY(group_id,message_id,gate));
            CREATE INDEX IF NOT EXISTS quality_user_events ON events(user_id,occurred_at);
            CREATE TABLE IF NOT EXISTS notices01 (
                content_md5 TEXT PRIMARY KEY, state TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL);
            CREATE TABLE IF NOT EXISTS notices02 (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                ban_instance TEXT NOT NULL, category TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL, sent_at REAL,
                PRIMARY KEY(group_id,user_id,ban_instance));
            CREATE TABLE IF NOT EXISTS score_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, old_score INTEGER NOT NULL,
                new_score INTEGER NOT NULL, reason TEXT NOT NULL,
                changed_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS quality_score_audit_user
                ON score_audit(user_id,changed_at);
            CREATE TABLE IF NOT EXISTS gate_cases (
                report_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                user_id TEXT NOT NULL, message_id TEXT NOT NULL,
                occurred_at REAL NOT NULL, received_at REAL NOT NULL,
                primary_gate TEXT NOT NULL, primary_category TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, review_payload TEXT,
                reviewed_at REAL, reviewer TEXT, signature_sha256 TEXT,
                UNIQUE(group_id,message_id));
            CREATE INDEX IF NOT EXISTS gate_cases_queue
                ON gate_cases(status,received_at);
            CREATE TABLE IF NOT EXISTS review_decisions (
                report_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                old_score INTEGER NOT NULL, new_score INTEGER NOT NULL,
                points INTEGER NOT NULL, ban_seconds INTEGER NOT NULL,
                reviewer TEXT NOT NULL, reason TEXT NOT NULL,
                decided_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notice_claims (
                claim_key TEXT PRIMARY KEY, state TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL);
            CREATE TABLE IF NOT EXISTS score_queries (
                group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                user_id TEXT NOT NULL, week_start TEXT NOT NULL,
                state TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(group_id,message_id));
            CREATE INDEX IF NOT EXISTS score_queries_week ON score_queries(user_id,week_start);
        ''')

    def close(self):
        self.db.close()

    def score(self,user_id):
        now=self.clock()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO users(user_id,updated_at) VALUES (?,?)',
                            (user_id,now))
        return self.db.execute('SELECT score FROM users WHERE user_id=?',
                               (user_id,)).fetchone()[0]

    def sample(self,score):
        if type(score) is not int or not 0<=score<=100:
            raise ValueError('Invalid quality score')
        if score==100:
            return True  # No new RNG draw while calibration is disabled.
        if score==0:
            return False
        return self.rng.randrange(100)<score

    def query_score(self,user_id,group_id,message_id):
        now=self.clock()
        week,reset=score_week(now)
        score=self.score(user_id)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            used=self.db.execute('SELECT COUNT(*) FROM score_queries WHERE user_id=? AND week_start=?',
                                 (user_id,week)).fetchone()[0]
            if used>=2:
                self.db.commit()
                return {'text':'您的额度已用完！\n重置时间：'+reset.strftime('%Y-%m-%d %H:%M')+'（北京时间）',
                        'claim':None}
            self.db.execute("INSERT INTO score_queries VALUES (?,?,?,?,'pending',?)",
                            (group_id,str(message_id),user_id,week,now))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {'text':f'您的质量分为{score_label(score)}！\n您本周的查询额度剩余{1-used}次！',
                'claim':{'group':group_id,'message_id':str(message_id)}}

    def settle_score_query(self,claim,*,sent,uncertain=False):
        if not claim or uncertain:
            return
        with self.db:
            if sent:
                self.db.execute("UPDATE score_queries SET state='sent' WHERE group_id=? AND message_id=?",
                                (claim['group'],claim['message_id']))
            else:
                self.db.execute("DELETE FROM score_queries WHERE group_id=? AND message_id=? AND state='pending'",
                                (claim['group'],claim['message_id']))

    def set_score(self,user_id,score,reason):
        """An explicit operator override, separate from uncalibrated gate events."""
        if not isinstance(user_id,str) or not re.fullmatch(r'[1-9][0-9]{4,12}',user_id):
            raise ValueError('QQ 号格式错误')
        if type(score) is not int or not 0<=score<=100:
            raise ValueError('质量分必须是 0–100 的整数')
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=120:
            raise ValueError('请填写 1–120 字的调整原因')
        reason=reason.strip()
        now=self.clock()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO users(user_id,updated_at) VALUES (?,?)',
                            (user_id,now))
            old=self.db.execute('SELECT score FROM users WHERE user_id=?',
                                (user_id,)).fetchone()[0]
            if old==score:
                return {'user_id':user_id,'old_score':old,'score':score,'changed':False}
            self.db.execute('''UPDATE users SET score=?,score_version='manual-v1',
                updated_at=? WHERE user_id=?''',(score,now,user_id))
            self.db.execute('''INSERT INTO score_audit
                (user_id,old_score,new_score,reason,changed_at)
                VALUES (?,?,?,?,?)''',(user_id,old,score,reason,now))
        return {'user_id':user_id,'old_score':old,'score':score,'changed':True}

    def record_case(self,case):
        """Insert one report per message; 00 never calls this method."""
        if not isinstance(case,dict) or case.get('primary_gate') not in (
                '01','02','03','04','05','06','07'):
            raise ValueError('Invalid gate case')
        encoded=json.dumps(case,ensure_ascii=False,separators=(',',':'))
        with self.db:
            return bool(self.db.execute('''INSERT OR IGNORE INTO gate_cases
                (report_id,group_id,user_id,message_id,occurred_at,received_at,
                 primary_gate,primary_category,mode,status,payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (case['report_id'],case['group_id'],case['user_id'],case['message_id'],
                 case['occurred_at'],case['received_at'],case['primary_gate'],
                 case['primary_category'],case['mode'],case['status'],encoded)).rowcount)

    def claim_notice(self,claim,now=None):
        now=self.clock() if now is None else now
        key=claim['claim']
        with self.db:
            row=self.db.execute('SELECT state,sent_at FROM notice_claims WHERE claim_key=?',
                                (key,)).fetchone()
            if row is not None:
                if row[0]=='pending' or key.startswith('md5:'):
                    return False
                if now-row[1] < claim['cooldown']:
                    return False
            self.db.execute('''INSERT INTO notice_claims VALUES (?,'pending',?,NULL)
                ON CONFLICT(claim_key) DO UPDATE SET state='pending',created_at=excluded.created_at,
                sent_at=NULL''',(key,now))
        return True

    def settle_gate_notice(self,claim,*,sent,uncertain=False):
        if uncertain:
            return
        with self.db:
            if sent:
                self.db.execute("UPDATE notice_claims SET state='sent',sent_at=? WHERE claim_key=?",
                                (self.clock(),claim['claim']))
            else:
                self.db.execute("DELETE FROM notice_claims WHERE claim_key=? AND state='pending'",
                                (claim['claim'],))

    def gate01(self,user_id,group_id,message_id,text,category):
        digest=hashlib.md5(text.encode('utf-8')).hexdigest()
        now=self.clock()
        self.score(user_id)
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO events VALUES
                (?,?,?,?,?,?,NULL,?)''',
                (group_id,str(message_id),'01',category,user_id,digest,now))
            claimed=self.db.execute('''INSERT OR IGNORE INTO notices01
                VALUES (?,'pending',?,NULL)''',(digest,now)).rowcount
        return {'gate':'01','digest':digest,'category':category} if claimed else None

    def gate02(self,user_id,group_id,message_id,record,category):
        if category not in VALID_02:
            raise ValueError('Invalid ban grade')
        instance=format(float(record['created_at']),'.17g')
        now=self.clock()
        self.score(user_id)
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO events VALUES
                (?,?,?,?,?,NULL,?,?)''',
                (group_id,str(message_id),'02','02'+category,user_id,instance,now))
            claimed=self.db.execute('''INSERT OR IGNORE INTO notices02
                VALUES (?,?,?,?,'pending',?,NULL)''',
                (group_id,user_id,instance,'02'+category,now)).rowcount
        return {'gate':'02','group':group_id,'user':user_id,
                'instance':instance,'category':'02'+category} if claimed else None

    def settle_notice(self,notice,*,sent,uncertain=False):
        if not notice or uncertain:
            return
        now=self.clock()
        with self.db:
            if notice['gate']=='01':
                if sent:
                    self.db.execute("UPDATE notices01 SET state='sent',sent_at=? WHERE content_md5=?",
                                    (now,notice['digest']))
                else:
                    self.db.execute("DELETE FROM notices01 WHERE content_md5=? AND state='pending'",
                                    (notice['digest'],))
            elif notice['gate']=='02':
                key=(notice['group'],notice['user'],notice['instance'])
                if sent:
                    self.db.execute("""UPDATE notices02 SET state='sent',sent_at=?
                        WHERE group_id=? AND user_id=? AND ban_instance=?""",(now,*key))
                else:
                    self.db.execute("""DELETE FROM notices02 WHERE group_id=? AND user_id=?
                        AND ban_instance=? AND state='pending'""",key)

    def backup(self,destination):
        dest=Path(destination)
        dest.parent.mkdir(parents=True,exist_ok=True)
        out=sqlite3.connect(dest)
        try:
            self.db.backup(out)
            if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Quality backup failed integrity check')
        finally:
            out.close()
        return dest
