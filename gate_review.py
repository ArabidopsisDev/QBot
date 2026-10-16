"""Local gate-report review, with idempotent score and optional group ban writes."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from qbot import GROUP_DATABASES
from quality import QualityLedger

REPORT_RE = re.compile(r'[0-9a-f]{32}\Z')
MAX_REVIEW_BAN = 3600


class GateReviewStore:
    def __init__(self, quality_path, data_root, sign_path, *, clock=time.time):
        self.quality_path=Path(quality_path)
        self.data_root=Path(data_root)
        self.sign_path=Path(sign_path)
        self.clock=clock
        self.signature_dir=self.data_root/'review_signatures'
        ledger=QualityLedger(self.quality_path)
        ledger.close()

    def _connect(self):
        db=sqlite3.connect(self.quality_path,timeout=5)
        db.row_factory=sqlite3.Row
        return db

    def _report_id(self,report_id):
        if not isinstance(report_id,str) or not REPORT_RE.fullmatch(report_id):
            raise ValueError('报告编号无效')
        return report_id

    def list_cases(self,status='pending',limit=100):
        if status not in ('pending','applying','approved','shadow'):
            raise ValueError('状态无效')
        with closing(self._connect()) as db:
            if status=='pending':
                rows=db.execute('''SELECT report_id,group_id,user_id,occurred_at,
                    primary_gate,primary_category,mode,status FROM gate_cases
                    WHERE status IN ('pending','shadow')
                    ORDER BY received_at DESC LIMIT ?''',(min(limit,200),))
            elif status=='shadow':
                rows=db.execute('''SELECT report_id,group_id,user_id,occurred_at,
                    primary_gate,primary_category,mode,status FROM gate_cases
                    WHERE mode='shadow' AND status IN ('pending','shadow')
                    ORDER BY received_at DESC LIMIT ?''',(min(limit,200),))
            else:
                rows=db.execute('''SELECT report_id,group_id,user_id,occurred_at,
                    primary_gate,primary_category,mode,status FROM gate_cases
                    WHERE status=? ORDER BY received_at DESC LIMIT ?''',(status,min(limit,200)))
            return [dict(row) for row in rows]

    def get(self,report_id):
        report_id=self._report_id(report_id)
        with closing(self._connect()) as db:
            row=db.execute('SELECT * FROM gate_cases WHERE report_id=?',
                           (report_id,)).fetchone()
            if row is None:
                return None
            result=json.loads(row['payload'])
            result['status']=row['status']
            result['review']=(json.loads(row['review_payload'])
                              if row['review_payload'] else None)
            result['reviewer']=row['reviewer']
            result['reviewed_at']=row['reviewed_at']
            result['signature_sha256']=row['signature_sha256']
            return result

    def reject(self,report_id):
        report_id=self._report_id(report_id)
        with closing(self._connect()) as db:
            row=db.execute('SELECT status FROM gate_cases WHERE report_id=?',
                           (report_id,)).fetchone()
            if row is None:
                raise ValueError('报告不存在')
            if row['status'] not in ('pending','shadow'):
                raise ValueError('报告已进入处理流程，不能驳回')
            db.execute('DELETE FROM gate_cases WHERE report_id=?',(report_id,))
            db.commit()
        return {'deleted':True,'report_id':report_id}

    def _snapshot_signature(self):
        raw=self.sign_path.read_bytes()
        if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw)>2_000_000:
            raise ValueError('签名图片不是有效的 PNG')
        digest=hashlib.sha256(raw).hexdigest()
        self.signature_dir.mkdir(parents=True,exist_ok=True)
        destination=self.signature_dir/(digest+'.png')
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('签名快照完整性错误')
        if not destination.exists():
            destination.write_bytes(raw)
        return digest

    def _apply_ban_once(self,report_id,group,user,seconds,decided_at):
        if seconds<=0:
            return
        filename=GROUP_DATABASES.get(group)
        if filename is None:
            raise ValueError('报告群号不在白名单')
        path=self.data_root/filename
        db=sqlite3.connect(path,timeout=5)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS review_bans (
                report_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                applied_at REAL NOT NULL, until REAL NOT NULL)''')
            claimed=db.execute('''INSERT OR IGNORE INTO review_bans VALUES (?,?,?,?)''',
                               (report_id,user,decided_at,decided_at+seconds)).rowcount
            if claimed and decided_at+seconds>self.clock():
                existing=db.execute('SELECT until FROM bans WHERE user_id=?',(user,)).fetchone()
                if existing is None or existing[0]<decided_at+seconds:
                    db.execute('''INSERT INTO bans VALUES (?,?,?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET until=excluded.until,
                        seconds=excluded.seconds,reason=excluded.reason,
                        mode=excluded.mode,created_at=excluded.created_at''',
                        (user,decided_at+seconds,seconds,
                         '门禁报告 '+report_id,'审核封禁',decided_at))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def approve(self,report_id,*,reviewer,points,ban_seconds=0,reason='按报告建议'):
        report_id=self._report_id(report_id)
        if not isinstance(reviewer,str) or not 1<=len(reviewer.strip())<=60:
            raise ValueError('请填写审核人')
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=200:
            raise ValueError('请填写处理原因')
        if type(points) is not int or not 0<=points<=100:
            raise ValueError('扣分必须是 0–100 的整数')
        if type(ban_seconds) is not int or not 0<=ban_seconds<=MAX_REVIEW_BAN:
            raise ValueError('封禁时间超出范围')
        reviewer,reason=reviewer.strip(),reason.strip()
        decision={'reviewer':reviewer,'points':points,'ban_seconds':ban_seconds,
                  'reason':reason}
        db=self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM gate_cases WHERE report_id=?',
                           (report_id,)).fetchone()
            if row is None:
                raise ValueError('报告不存在')
            if row['status']=='approved':
                existing=json.loads(row['review_payload'])
                if any(existing.get(k)!=v for k,v in decision.items()):
                    raise ValueError('报告已由另一处理意见批准')
                db.commit()
                return self.get(report_id)
            payload=json.loads(row['payload'])
            if row['status']=='applying':
                existing=json.loads(row['review_payload'])
                if any(existing.get(k)!=v for k,v in decision.items()):
                    raise ValueError('报告正在按另一处理意见执行')
                signature=existing['signature_sha256']
                signature_path=self.signature_dir/(signature+'.png')
                if (not signature_path.is_file() or
                    hashlib.sha256(signature_path.read_bytes()).hexdigest()!=signature):
                    raise RuntimeError('审核签名快照丢失或损坏')
                decided_at=row['reviewed_at']
            elif row['status'] in ('pending','shadow'):
                if ban_seconds and not any(f.get('gate') in ('02','06')
                                       for f in payload.get('findings',[])):
                    raise ValueError('此门禁不允许封禁')
                decided_at=self.clock()
                signature=self._snapshot_signature()
                decision['signature_sha256']=signature
                db.execute('''UPDATE gate_cases SET status='applying',
                    review_payload=?,reviewed_at=?,reviewer=?,signature_sha256=?
                    WHERE report_id=?''',(json.dumps(decision,ensure_ascii=False),
                    decided_at,reviewer,signature,report_id))
            else:
                raise ValueError('报告状态无效')
            db.commit()
            self._apply_ban_once(report_id,row['group_id'],row['user_id'],
                                 ban_seconds,decided_at)
            db.execute('BEGIN IMMEDIATE')
            exists=db.execute('SELECT 1 FROM review_decisions WHERE report_id=?',
                              (report_id,)).fetchone()
            if not exists:
                now=self.clock()
                db.execute('INSERT OR IGNORE INTO users(user_id,updated_at) VALUES (?,?)',
                           (row['user_id'],now))
                old=db.execute('SELECT score FROM users WHERE user_id=?',
                               (row['user_id'],)).fetchone()[0]
                new=max(0,old-points)
                db.execute("UPDATE users SET score=?,score_version='review-v1',updated_at=? WHERE user_id=?",
                           (new,now,row['user_id']))
                db.execute('''INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?,?,?)''',
                           (report_id,row['user_id'],old,new,points,ban_seconds,
                            reviewer,reason,decided_at))
                db.execute('''INSERT INTO score_audit
                    (user_id,old_score,new_score,reason,changed_at) VALUES (?,?,?,?,?)''',
                    (row['user_id'],old,new,'门禁报告 '+report_id+'：'+reason,now))
            db.execute("UPDATE gate_cases SET status='approved' WHERE report_id=?",
                       (report_id,))
            db.commit()
            return self.get(report_id)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def signature(self,digest):
        if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
            raise ValueError('签名编号无效')
        with closing(self._connect()) as db:
            allowed=db.execute('''SELECT 1 FROM gate_cases WHERE status='approved'
                AND signature_sha256=? LIMIT 1''',(digest,)).fetchone()
        if not allowed:
            raise ValueError('签名尚未归入已批准报告')
        path=self.signature_dir/(digest+'.png')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise RuntimeError('签名快照完整性错误')
        return raw
