"""QBot engine, plus an offline console. Live transport lives in live.py."""
from __future__ import annotations

import argparse
import math
import random
import re
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import datastore
from generation_refine import (GeneratedPath, MAX_CANDIDATES, natural_stop,
    structural_issue, validate_refine_probability, weighted_pick)
from repetition import strong_repeat_repair
from intelligence import Intelligence
from keyword_reply import Bounds as KeywordBounds, Contexts as KeywordContexts, KeywordGenerator
from moderation import ESCALATION_WINDOW, classify as classify_sexual, grade_reason, human_duration
from quality import QualityLedger, ban_grade, length_gate
from gates import GateHistory, default_config as gate_default_config, evaluate as evaluate_gates, normalize_event as gate_normalize_event, record_for as gate_record_for

TEST_GROUP = "312579123"
PRODUCTION_GROUP = "1050045834"
THIRD_GROUP = "1006808801"
FOURTH_GROUP = "929618496"
ALLOWED_GROUPS = frozenset((TEST_GROUP, PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP))
CLEAR_ADMIN = "575586491"
MODELS = ('match', 'generate', 'intelligence')
MAX_TEXT = 10000
# Seconds of clock skew tolerated before a message counts as out of order.
TIMESTAMP_TOLERANCE = 300.0
PRE_ROOT = Path(__file__).resolve().parent / 'pre'
QQ_RE = re.compile(r'[1-9][0-9]{4,12}')
MIN_MANUAL_BAN = 1
MAX_MANUAL_BAN = 31536000
MAX_REASON = 40
ADMIN_COMMANDS = frozenset(('/ban', '/unban', '/save', '/load', '/train', '/dstx'))
# Optional two-character lookahead for generate (see next_gen.md). 0.0 keeps the
# old single-character model byte-for-byte, including its random draws.
BLEND_MAX_LIMIT = 0.20
BLEND_CONFIG_KEY = 'generate_blend_max'
GEN_TRIGRAMS_DDL = """
    CREATE TABLE IF NOT EXISTS gen_trigrams (
        older TEXT NOT NULL, first TEXT NOT NULL, second TEXT NOT NULL,
        w INTEGER NOT NULL CHECK(w > 0),
        PRIMARY KEY(older, first, second));"""

# One database per group; live.py builds its bots from this single mapping.
GROUP_DATABASES = {
    TEST_GROUP: 'live.sqlite3',
    PRODUCTION_GROUP: 'production.sqlite3',
    THIRD_GROUP: 'third.sqlite3',
    FOURTH_GROUP: 'fourth.sqlite3',
}
# /apply: a random grant between 30 seconds and one hour, at most three at once.
APPLY_MIN_SECONDS = 30
APPLY_MAX_SECONDS = 3600
APPLY_LIMIT = 3
APPLY_WINDOW = 86400        # One successful grant per account per day.
VIOLATION_WINDOW = 86400    # An automatic ban blocks applications for a day.
APPLY_QUIET = 60            # Repeat attempts inside this window are ignored silently.
EXPIRY_NOTICE = '白名单资格已到期，已移出白名单。'
# Keyword-seeded generate replies (keyword_reply.py). The in-memory model is
# rebuilt from chars + intel_memory at most this often; every reply is still
# re-checked against the current chars and pair bans before it is returned.
KEYWORD_REFRESH = 60.0


def keyword_probability(value):
    """Validate a group's share of generate replies taken by the keyword path."""
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError('keyword_reply_probability 必须是 0.0–1.0 之间的有限数值')
    return float(value)


def identifier(value):
    if type(value) is int:
        return str(value)
    return value if isinstance(value, str) and value else None


def qq_number(value):
    """Accept only a bare QQ account number typed as one token."""
    return value if isinstance(value, str) and QQ_RE.fullmatch(value) else None


def blend_ceiling(value):
    """Validate one group's trigram blend weight; 0.0 disables the blend."""
    if type(value) not in (int, float) or not math.isfinite(value) \
            or not 0.0 <= value <= BLEND_MAX_LIMIT:
        raise ValueError(f'generate_blend_max 必须是 0.0–{BLEND_MAX_LIMIT:g} 之间的有限数值')
    return float(value)


def blend_limits(config):
    """Per-group blend ceilings from the connection file, defaulting to 0.0.

    Only the three authorized groups may appear; anything else, and any value
    that is not a finite number inside 0.0–0.20, is refused outright so a typo
    can neither enable the blend by surprise nor widen the send whitelist.
    """
    if not isinstance(config, dict):
        raise ValueError('连接配置无效')
    raw = config.get(BLEND_CONFIG_KEY, {})
    if not isinstance(raw, dict):
        raise ValueError(f'{BLEND_CONFIG_KEY} 必须是按群配置的字典')
    limits = {group: 0.0 for group in ALLOWED_GROUPS}
    for group, value in raw.items():
        if group not in ALLOWED_GROUPS:
            raise ValueError(f'{BLEND_CONFIG_KEY} 含未授权群号')
        limits[group] = blend_ceiling(value)
    return limits


def trigram_counts(text):
    """Three-character windows of one message: len(text)-2 of them.

    The character scope is exactly `learn_chars`' -- every adjacent character,
    punctuation, letters and emoji included -- and no window ever crosses a
    message, an attachment placeholder, a sender, a group or a date boundary.
    """
    counts = Counter()
    counts.update(zip(text, text[1:], text[2:]))
    return counts



def export_time(value):
    """Convert a QQ export header timestamp to epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').timestamp()
    except ValueError:
        return None


# The generate model learns every adjacent pair, punctuation included, so replies
# read naturally. `is_counted` exists for the DASHBOARD only: the panel's charts
# and statistics deliberately ignore letters, digits, ASCII symbols, whitespace
# and emoji, while still counting Chinese punctuation.
IDEOGRAPH = re.compile('[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
COUNTED = re.compile(
    '[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'          # ideographs
    '\u3001\u3002\u3008-\u3011\u3014-\u301b'             # 、。〈〉《》「」『』【】〔〕〖〗
    '\uff01\uff08\uff09\uff0c\uff1a\uff1b\uff1f'         # ！（），：；？
    '\u2018\u2019\u201c\u201d\u2026\u2014\u00b7\uff5e'   # ‘’“”…—·～
    ']')


def is_chinese(character):
    """True for a Chinese ideograph (not punctuation)."""
    return bool(IDEOGRAPH.fullmatch(character))


def is_counted(character):
    """True for a character the dashboard counts: 汉字 or 中文标点."""
    return bool(COUNTED.fullmatch(character))


def cjk_pairs(text):
    """Adjacent pairs of counted characters (dashboard helpers and pretraining)."""
    return [(first, second) for first, second in zip(text, text[1:])
            if is_counted(first) and is_counted(second)]


def group_reply(group_id, text, *, exempt=False):
    """Construct a literal-text reply for an explicitly authorized group."""
    if identifier(group_id) not in ALLOWED_GROUPS:
        raise PermissionError("Destination is not the authorized test group")
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
        raise ValueError("Invalid reply text")
    reply = {"action": "send_group_msg", "params": {
        "group_id": int(group_id),
        "message": [{"type": "text", "data": {"text": text}}],
    }}
    if exempt:
        reply['exempt'] = True
    return reply


def human_seconds(seconds):
    """Render a whitelist duration the way the group notice shows it."""
    seconds = max(0, int(seconds))
    return f'{seconds // 60}分{seconds % 60}秒'


def expired_whitelist(db, now):
    """Expired grants in a raw connection, oldest first.

    Nothing is removed here: only the expiry watcher deletes a row, and only
    after the notice for it actually went out. Otherwise a message arriving in
    the same tick could remove the row and silently swallow the notice.
    """
    return [row[0] for row in db.execute(
        'SELECT user_id FROM whitelist WHERE until<=? ORDER BY until,user_id', (now,))]


class Bot:
    def __init__(self, database=":memory:", *, self_id, group_id=TEST_GROUP, rng=None,
                 clock=time.time, pre_root=PRE_ROOT, generate_blend_max=0.0,
                 generate_refine_probability=0.0, refine_rng=None,
                 keyword_reply_probability=0.0, keyword_rng=None,
                 quality=None):
        self.group_id = identifier(group_id)
        if self.group_id not in ALLOWED_GROUPS:
            raise PermissionError('Unauthorized group')
        self.self_id = identifier(self_id)
        if not self.self_id:
            raise ValueError("self_id is required")
        self.rng = rng if rng is not None else random.Random()
        self.quality = quality if quality is not None else QualityLedger(':memory:',clock=clock)
        self._owns_quality = quality is None
        self.clock = clock
        self.pre_root = Path(pre_root)
        self.generate_blend_max = blend_ceiling(generate_blend_max)
        self.generate_refine_probability=validate_refine_probability(generate_refine_probability)
        self.refine_rng=refine_rng if refine_rng is not None else random.Random()
        self.keyword_reply_probability = keyword_probability(keyword_reply_probability)
        self.keyword_rng = keyword_rng if keyword_rng is not None else random.Random()
        self._keyword_model = None
        self._keyword_built = None
        self.db = sqlite3.connect(database)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS qa (
                q TEXT NOT NULL, a TEXT NOT NULL, w INTEGER NOT NULL CHECK(w > 0),
                PRIMARY KEY(q, a));
            CREATE TABLE IF NOT EXISTS chars (
                first TEXT NOT NULL, second TEXT NOT NULL,
                w INTEGER NOT NULL CHECK(w > 0), PRIMARY KEY(first, second));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES ('model', 'match');
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bans (
                user_id TEXT PRIMARY KEY,
                until REAL NOT NULL,
                seconds INTEGER NOT NULL CHECK(seconds > 0),
                reason TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id TEXT PRIMARY KEY,
                until REAL NOT NULL,
                seconds INTEGER NOT NULL CHECK(seconds > 0),
                granted_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS violations (
                user_id TEXT PRIMARY KEY,
                last_at REAL NOT NULL,
                count INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS violation_log (
                user_id TEXT NOT NULL,
                at REAL NOT NULL,
                grade TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(user_id, at));
            CREATE TABLE IF NOT EXISTS user_chars (
                user_id TEXT NOT NULL,
                first TEXT NOT NULL,
                second TEXT NOT NULL,
                w INTEGER NOT NULL CHECK(w > 0),
                PRIMARY KEY(user_id, first, second));
            CREATE INDEX IF NOT EXISTS user_chars_pair ON user_chars(first, second);
            CREATE INDEX IF NOT EXISTS user_chars_user ON user_chars(user_id);
            CREATE TABLE IF NOT EXISTS apply_log (
                user_id TEXT PRIMARY KEY,
                granted_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS apply_attempts (
                user_id TEXT PRIMARY KEY,
                last_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pair_bans (
                first TEXT NOT NULL,
                second TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(first, second));
            CREATE TABLE IF NOT EXISTS repetition_repairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, at REAL NOT NULL,
                removed INTEGER NOT NULL CHECK(removed>0));
        """ + GEN_TRIGRAMS_DDL)
        self.intelligence = Intelligence(self.db, self.rng)
        self.previous = None
        self.last_time = None
        self.peers = {}
        self.gate_history = GateHistory()
        self.gate_config = gate_default_config()
        self.last_gate_decision = None

    def attach_peers(self, mapping):
        """Register sibling group bots so /dstx can reach every group's bans."""
        self.peers = {group: bot for group, bot in mapping.items() if bot is not self}

    def close(self):
        self.db.close()
        if self._owns_quality:
            self.quality.close()

    @property
    def model(self):
        return self.db.execute("SELECT value FROM settings WHERE key='model'").fetchone()[0]

    def pick(self, rows):
        if not rows:
            return None
        ticket = self.rng.randrange(sum(weight for _, weight in rows))
        for value, weight in rows:
            if ticket < weight:
                return value
            ticket -= weight
        raise AssertionError("Invalid weights")

    # ---- moderation ---------------------------------------------------

    def active_ban(self, user_id, now):
        """Return the live ban for a sender, dropping an expired one."""
        row = self.db.execute(
            'SELECT until,seconds,reason,mode,created_at FROM bans WHERE user_id=?', (user_id,)).fetchone()
        if row is None:
            return None
        if row[0] <= now:
            self.db.execute('DELETE FROM bans WHERE user_id=?', (user_id,))
            return None
        return {'until': row[0], 'seconds': row[1], 'reason': row[2],
                'mode': row[3], 'created_at': row[4]}

    def ban(self, user_id, seconds, reason, mode, now):
        seconds = int(seconds)
        self.db.execute('''INSERT INTO bans VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET until=excluded.until, seconds=excluded.seconds,
                reason=excluded.reason, mode=excluded.mode, created_at=excluded.created_at''',
            (user_id, now + seconds, seconds, reason, mode, now))
        return {'until': now + seconds, 'seconds': seconds, 'reason': reason,
                'mode': mode, 'created_at': now}

    def unban(self, user_id):
        return bool(self.db.execute('DELETE FROM bans WHERE user_id=?', (user_id,)).rowcount)

    def banned_users(self):
        return {row[0] for row in self.db.execute('SELECT user_id FROM bans')}

    def ban_notice(self, user_id, record):
        return (f'{user_id}（QQ号），您已被封禁{human_duration(record["seconds"])}，'
                f'原因：{record["reason"]}，方式：{record["mode"]}')

    # ---- /apply whitelist ---------------------------------------------

    def active_whitelist(self, now):
        """Live grants for this group, oldest first.

        Expired rows are ignored rather than deleted: the expiry watcher owns
        their removal, so that every grant produces exactly one notice.
        """
        return [(row[0], row[1]) for row in self.db.execute(
            'SELECT user_id,until FROM whitelist WHERE until>? ORDER BY granted_at,user_id',
            (now,))]

    def whitelisted(self, user_id, now):
        """True while the sender may speak without being screened or banned."""
        row = self.db.execute('SELECT until FROM whitelist WHERE user_id=?',
                              (user_id,)).fetchone()
        return row is not None and row[0] > now

    def violation(self, user_id, now):
        """True when this account was automatically banned within the last day."""
        row = self.db.execute('SELECT last_at FROM violations WHERE user_id=?', (user_id,)).fetchone()
        return row is not None and now - row[0] < VIOLATION_WINDOW

    def record_violation(self, user_id, now, grade='', term=''):
        """Remember a violation for the /apply gate and for escalation."""
        self.db.execute('''INSERT INTO violations VALUES (?,?,1)
            ON CONFLICT(user_id) DO UPDATE SET last_at=excluded.last_at, count=count+1''',
            (user_id, now))
        self.db.execute('INSERT OR REPLACE INTO violation_log VALUES (?,?,?,?)',
                        (user_id, now, grade, term))

    def violation_count(self, user_id, now, window=ESCALATION_WINDOW):
        """How many violations this account earned inside the escalation window."""
        return self.db.execute('SELECT COUNT(*) FROM violation_log WHERE user_id=? AND at>?',
                               (user_id, now - window)).fetchone()[0]

    def apply_quiet(self, user_id, now):
        """True when this sender must be ignored to keep notices from flooding."""
        row = self.db.execute('SELECT last_at FROM apply_attempts WHERE user_id=?',
                              (user_id,)).fetchone()
        if row is not None and now - row[0] < APPLY_QUIET:
            return True
        self.db.execute('INSERT OR REPLACE INTO apply_attempts VALUES (?,?)', (user_id, now))
        return False

    def apply_reason(self, user_id, now):
        """Return the refusal text, or None when the application may proceed."""
        head = f'{user_id}（QQ号），'
        granted = self.db.execute('SELECT granted_at FROM apply_log WHERE user_id=?',
                                  (user_id,)).fetchone()
        if granted is not None and now - granted[0] < APPLY_WINDOW:
            return head + '您今天已成功申请过，无法发放第二次白名单资格'
        if self.violation(user_id, now):
            return head + '由于最近24小时内有违规行为，暂无法发放资格'
        if len(self.active_whitelist(now)) >= APPLY_LIMIT:
            return head + '由于白名单列表已满，暂无法发放资格'
        return None

    def apply_notice(self, user_id, seconds, now):
        """Success text plus the current list, each grant with its remaining time."""
        lines = [f'{user_id}（QQ号），经自动审核，已成功发放{human_seconds(seconds)}白名单资格',
                 '当前白名单列表：']
        for index, (granted, until) in enumerate(self.active_whitelist(now), start=1):
            lines.append(f'{index}. {granted}（QQ号），剩余{human_seconds(until - now)}')
        return '\n'.join(lines)

    def grant_whitelist(self, user_id, now):
        """Grant one random-length exemption and remember it for the daily limit."""
        seconds = self.rng.randint(APPLY_MIN_SECONDS, APPLY_MAX_SECONDS)
        self.db.execute('INSERT OR REPLACE INTO whitelist VALUES (?,?,?,?)',
                        (user_id, now + seconds, seconds, now))
        self.db.execute('INSERT OR REPLACE INTO apply_log VALUES (?,?)', (user_id, now))
        return seconds

    def _apply(self, user, now):
        """Handle /apply; every notice bypasses the shared send interval."""
        if self.apply_quiet(user, now):
            return None
        refusal = self.apply_reason(user, now)
        if refusal is not None:
            return self._reply(refusal, exempt=True)
        seconds = self.grant_whitelist(user, now)
        return self._reply(self.apply_notice(user, seconds, now), exempt=True)

    # ---- learning -----------------------------------------------------

    def learn_pair(self, previous, text, timestamp):
        """Record one previous->current transition when the two are close in time."""
        if previous is None:
            return 0
        previous_text, previous_time = previous
        if not 0 <= timestamp - previous_time <= 180:
            return 0
        self.db.execute('''INSERT INTO qa VALUES (?, ?, 1)
            ON CONFLICT(q,a) DO UPDATE SET w=w+1''', (previous_text, text))
        return 1

    def learn_chars(self, text, user_id=None):
        """Learn in-message transitions, attributed to one sender when known.

        Every adjacent pair is learned -- punctuation and symbols included --
        because that is what makes a reply read like a sentence. The dashboard
        filters these rows for display, which is a reporting choice only.
        """
        rows = list(zip(text, text[1:]))
        if not rows:
            return 0
        self.db.executemany('''INSERT INTO chars VALUES (?, ?, 1)
            ON CONFLICT(first,second) DO UPDATE SET w=w+1''', rows)
        if user_id:
            self.db.executemany('''INSERT INTO user_chars VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id,first,second) DO UPDATE SET w=w+1''',
                ((user_id, first, second) for first, second in rows))
        return len(rows)

    def learn_trigrams(self, text):
        """Learn the in-message three-character windows of the same message.

        Called right after `learn_chars`, so the two tables share one message's
        guards, one transaction and one character scope. A message of m
        characters contributes exactly m-2 windows (0 for m<3); nothing is ever
        joined across messages, attachments, senders, groups or dates.
        """
        windows = list(zip(text, text[1:], text[2:]))
        if not windows:
            return 0
        self.db.executemany('''INSERT INTO gen_trigrams VALUES (?, ?, ?, 1)
            ON CONFLICT(older,first,second) DO UPDATE SET w=w+1''', windows)
        return len(windows)

    def train_file(self, path):
        """Learn every model from a text export, adding to existing data."""
        records, source_hash = datastore.read_text_source(path)
        blocked = self.banned_users()
        base = self.clock()
        pairs, transitions, attributed = Counter(), Counter(), Counter()
        trigrams = Counter()
        ordered, previous, trained, skipped = [], None, 0, 0
        self.intelligence.reset_context()
        for index, (stamp, sender, raw) in enumerate(records):
            if sender and (sender == self.self_id or sender in blocked):
                skipped += 1
                continue
            cleaned_chunks=[strong_repeat_repair(chunk.strip())[0]
                            for chunk in datastore.PLACEHOLDER.split(raw) if chunk.strip()]
            message = ''.join(cleaned_chunks)
            if not message or message.split(maxsplit=1)[0].startswith('/'):
                skipped += 1
                continue
            timestamp = export_time(stamp)
            if timestamp is None:
                timestamp = base + index * 0.001
            if previous is not None and 0 <= timestamp - previous[1] <= 180:
                pairs[(previous[0], message)] += 1
            # Split on attachments so bigrams never span a placeholder.
            for chunk in cleaned_chunks:
                found = list(zip(chunk, chunk[1:]))
                transitions.update(found)
                # Same split, same scope: a window never spans two chunks.
                trigrams.update(trigram_counts(chunk))
                if sender:
                    attributed.update((sender, first, second) for first, second in found)
            ordered.append((message, timestamp))
            previous = (message, timestamp)
            trained += 1
        if pairs:
            self.db.executemany('''INSERT INTO qa VALUES (?, ?, ?)
                ON CONFLICT(q,a) DO UPDATE SET w=w+excluded.w''',
                ((q, a, w) for (q, a), w in pairs.items()))
        if transitions:
            self.db.executemany('''INSERT INTO chars VALUES (?, ?, ?)
                ON CONFLICT(first,second) DO UPDATE SET w=w+excluded.w''',
                ((a, b, w) for (a, b), w in transitions.items()))
        if trigrams:
            self.db.executemany('''INSERT INTO gen_trigrams VALUES (?, ?, ?, ?)
                ON CONFLICT(older,first,second) DO UPDATE SET w=w+excluded.w''',
                ((a, b, c, w) for (a, b, c), w in trigrams.items()))
        if attributed:
            self.db.executemany('''INSERT INTO user_chars VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id,first,second) DO UPDATE SET w=w+excluded.w''',
                ((user_id, a, b, w) for (user_id, a, b), w in attributed.items()))
        for message, timestamp in ordered:
            self.intelligence.observe(message, timestamp)
        self.previous = None
        self.intelligence.reset_context()
        return {'records': len(records), 'trained': trained, 'skipped': skipped,
                'pairs': len(pairs), 'transitions': len(transitions),
                'trigrams': len(trigrams),
                'attributed': len(attributed), 'source_hash': source_hash}

    # ---- admin commands -----------------------------------------------

    def _reply(self, text, *, exempt=False):
        return group_reply(self.group_id, text, exempt=exempt)

    def admin_command(self, command, text, user):
        """Only the authenticated OneBot sender id may run these."""
        if user != CLEAR_ADMIN:
            return self._reply(f'无权限：仅 QQ {CLEAR_ADMIN} 可以使用 {command}。')
        args = text.strip().split()
        if command == '/ban':
            return self._ban(args)
        if command == '/unban':
            return self._unban(args)
        if command == '/save':
            return self._save(args)
        if command == '/load':
            return self._load(args)
        if command == '/dstx':
            return self._dstx(args)
        return self._train(args)

    def _lift_bans(self, target):
        """Clear one group's ban list and return the released QQ numbers."""
        rows = [row[0] for row in target.db.execute('SELECT user_id FROM bans')]
        if not rows:
            return []
        target.db.execute('DELETE FROM bans')
        if target is not self:
            # Our own transaction is committed by the caller's with-block.
            target.db.commit()
        return rows

    def _dstx(self, args):
        """Lift every ban in every group; the admin only, and only with no arguments."""
        if len(args) != 1:
            return self._reply('用法：/dstx（无需参数，解除所有群的封禁）')
        released = []
        for target in [self] + [peer for peer in self.peers.values() if peer is not self]:
            try:
                released.extend(self._lift_bans(target))
            except sqlite3.Error:
                continue  # One unreadable group must not block the others.
        header = '大赦天下！解除封禁名单：'
        unique = sorted(set(released),
                        key=lambda value: (int(value) if value.isdigit() else 0, value))
        if not unique:
            return self._reply(f'{header}\n（当前没有封禁记录）')
        lines = [[f'{index}. {value}'] for index, value in enumerate(unique, start=1)]
        return self._reply(self._bounded(header, lines, '个封禁记录'))

    def _ban(self, args):
        usage = f'用法：/ban QQ号 秒数（{MIN_MANUAL_BAN}-{MAX_MANUAL_BAN}），可选原因'
        if len(args) not in (3, 4):
            return self._reply(usage)
        target = qq_number(args[1])
        if target is None:
            return self._reply(usage)
        if target == CLEAR_ADMIN:
            return self._reply(f'已取消。不能封禁管理员账号（{CLEAR_ADMIN}）。')
        try:
            seconds = int(args[2])
        except ValueError:
            return self._reply(usage)
        if not MIN_MANUAL_BAN <= seconds <= MAX_MANUAL_BAN:
            return self._reply(usage)
        reason = args[3][:MAX_REASON] if len(args) == 4 else '管理员封禁'
        record = self.ban(target, seconds, reason, '手动封禁', self.clock())
        return self._reply('已封禁。' + self.ban_notice(target, record))

    def _unban(self, args):
        usage = '用法：/unban QQ号'
        if len(args) != 2:
            return self._reply(usage)
        target = qq_number(args[1])
        if target is None:
            return self._reply(usage)
        if self.unban(target):
            return self._reply(f'已解除。{target}（QQ号）已移出封禁列表。')
        return self._reply(f'已解除。{target}（QQ号）不在封禁列表中。')

    def _save(self, args):
        usage = '用法：/save 名称（中英文、数字、下划线、连字符，1-32 字）'
        if len(args) != 2:
            return self._reply(usage)
        try:
            folder = datastore.folder_for(self.pre_root, args[1])
            payload = datastore.export_database(self.db, group_id=self.group_id,
                                                 saved_at=self.clock())
            datastore.write_backup(folder, payload)
        except (ValueError, OSError) as error:
            return self._reply(f'保存失败：{error}')
        stats = datastore.summarize(self.db)
        return self._reply(f'已保存。名称：{folder.name}；问答 {stats["qa"]} 条，'
                           f'字符转移 {stats["chars"]} 条，'
                           f'intelligence 记忆 {stats["intel_memory"]} 条。')

    def _load(self, args):
        usage = '用法：/load 名称（读取 pre 目录下同名文件夹的备份）'
        if len(args) != 2:
            return self._reply(usage)
        try:
            folder = datastore.folder_for(self.pre_root, args[1])
            payload = datastore.read_backup(folder)
            stats = datastore.import_database(self.db, payload)
        except (ValueError, OSError) as error:
            return self._reply(f'加载失败：{error}')
        self.previous = None
        self.intelligence.reset_context()
        return self._reply(f'已加载。名称：{folder.name}；问答 {stats["qa"]} 条，'
                           f'字符转移 {stats["chars"]} 条，'
                           f'intelligence 记忆 {stats["intel_memory"]} 条。')

    def _train(self, args):
        usage = '用法：/train 文件夹/文件名（读取 pre/text/文件夹/文件名.txt）；/train list 列出全部数据集'
        if len(args) != 2:
            return self._reply(usage)
        # "list" is a reserved sub-command, never a file name to train.
        if args[1] == 'list':
            return self._list_sources()
        try:
            path = datastore.source_file_for(self.pre_root, args[1])
            stats = self.train_file(path)
        except (ValueError, OSError) as error:
            return self._reply(f'训练失败：{error}')
        memories = self.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0]
        return self._reply(f'已训练。名称：{args[1]}；共 {stats["records"]} 条记录，'
                           f'训练 {stats["trained"]} 条消息，跳过 {stats["skipped"]} 条，'
                           f'问答 {stats["pairs"]} 组，字符转移 {stats["transitions"]} 组，'
                           f'intelligence 记忆 {memories} 条。')

    def _bounded(self, header, blocks, unit):
        """Join a header with whole blocks, dropping any that would overflow."""
        listing, shown = [header], 0
        for block in blocks:
            if len('\n'.join(listing + block)) > MAX_TEXT - 64:
                break
            listing.extend(block)
            shown += 1
        text = '\n'.join(listing)
        if shown < len(blocks):
            text += f'\n…共 {len(blocks)} {unit}，仅列出前 {shown} 个。'
        return text

    def _list_sources(self):
        """List the pre/text datasets, one name per line, hiding a .txt suffix."""
        try:
            names = datastore.list_sources(self.pre_root)
        except OSError:
            return self._reply('列出失败：无法读取 pre/text 目录。')
        header = '可训练数据集：'
        if not names:
            return self._reply(f'{header}\n（pre/text 目录为空，没有可训练的文件。）')
        # A folder line and the '- ' file lines under it are kept together.
        blocks = []
        for line in names:
            if line.startswith('- ') and blocks:
                blocks[-1].append(line)
            else:
                blocks.append([line])
        return self._reply(self._bounded(header, blocks, '个数据集'))

    def generate(self, seed):
        first=self._generate_path(seed)
        if first.text is None or first.blocked or self.generate_refine_probability<=0:
            return first.text
        if self.refine_rng.random()>=self.generate_refine_probability:
            return first.text
        stopped=natural_stop(first.text,first.target)
        if not structural_issue(stopped):
            return stopped
        candidates=[]
        for _ in range(MAX_CANDIDATES-1):
            path=self._generate_path(seed,rng=self.refine_rng)
            if path.blocked or path.text is None:
                continue
            text=natural_stop(path.text,path.target)
            if not structural_issue(text):
                candidates.append(text)
        return self.refine_rng.choice(candidates) if candidates else first.text

    def keyword_reply(self, text):
        """A keyword-seeded reply to the whole message, or None (stay silent).

        Unlike `generate`, the walk does not continue the message's last
        character: it grows a sentence through an informative keyword of the
        message (see keyword_reply.py). Its randomness comes only from
        `keyword_rng`. The cached model may lag behind by KEYWORD_REFRESH
        seconds, so the reply is dropped if any of its adjacent pairs is no
        longer a stored `chars` edge -- deleted or cleared edges never leave.
        """
        now = self.clock()
        if (self._keyword_model is None or self._keyword_built is None
                or not 0 <= now - self._keyword_built < KEYWORD_REFRESH):
            memories = self.db.execute(
                'SELECT text, w FROM intel_memory ORDER BY id').fetchall()
            self._keyword_model = KeywordGenerator(
                self.db, bounds=KeywordBounds(memories), contexts=KeywordContexts(memories),
                corpus=[text for text, _ in memories])
            self._keyword_built = now
        found = self._keyword_model.reply(text, self.keyword_rng)
        if found is None:
            return None
        pairs = list(zip(found.text, found.text[1:]))
        for first, second in pairs:
            if not self.db.execute('SELECT 1 FROM chars WHERE first=? AND second=?',
                                   (first, second)).fetchone():
                return None
        return found.text

    def _generate_path(self, seed, *, rng=None):
        """Walk the transition graph from the tail of the message.

        `seed` is the hidden starting context: its last character starts the
        walk, and when the hint carries two characters the pair also seeds the
        optional trigram blend. Neither seed character is ever appended to the
        reply -- they are context only.

        Every stored transition is eligible, punctuation included, so replies
        come out punctuated instead of as one unbroken run of characters. When
        the walk forms a banned pair, generation is cut off at that point and the
        whole reply is dropped -- the operator's pair ban lives on the output
        path, so such a pair can never leave the bot.
        """
        context = seed[-2:] if isinstance(seed, str) else str(seed)[-2:]
        draw_rng=self.rng if rng is None else rng
        target = draw_rng.randint(3, 50)
        current = context[-1:]
        result = ''
        banned = self.banned_pairs()
        while len(result) < target:
            rows = self.db.execute(
                'SELECT second, w FROM chars WHERE first=? ORDER BY second',
                (current,)).fetchall()
            blended = self.blend_candidates(context, rows)
            candidates=blended if blended is not None else rows
            following = (self.pick(candidates) if rng is None
                         else weighted_pick(candidates,draw_rng))
            if following is None:
                break
            if banned and (current[-1:], following) in banned:
                return GeneratedPath(None,target,blocked=True)
            result += following
            current = following
            context = (context + following)[-2:]
        return GeneratedPath(result if len(result)>=3 else None,target)

    def blend_candidates(self, context, rows):
        """Mixed single/trigram candidate weights, or None to use `rows` as-is.

        Each candidate keeps at least (1 - generate_blend_max) of its original
        single-character probability, because P(c) = (1-λ)·P₁(c) + λ·P₂(c) with
        λ = generate_blend_max · N/(N+4) ≤ generate_blend_max ≤ 0.20. P₂ is
        renormalised over the successors `chars` already allows, so a trigram
        row left behind by a manual weight edit or a loop sweep can never bring
        a deleted edge back. Returning None preserves the original pick(rows)
        path -- same probabilities and same random draws -- which is what makes
        the default (0.0) bit-for-bit identical to the old model.
        """
        ceiling = self.generate_blend_max
        if ceiling <= 0.0 or len(context) < 2 or not rows:
            return None
        seen = self.db.execute(
            'SELECT second, w FROM gen_trigrams WHERE older=? AND first=?',
            (context[-2], context[-1])).fetchall()
        allowed = {second for second, _ in rows}
        survivor = [(second, w) for second, w in seen if second in allowed]
        evidence = sum(w for _, w in survivor)
        if evidence <= 0:
            return None
        # Use exact integer mixture weights. A fixed decimal scale can round a
        # rare but valid single-character successor down to zero.
        numerator, denominator = Fraction(str(ceiling)).as_integer_ratio()
        base_factor = denominator * (evidence + 4) - numerator * evidence
        base = sum(w for _, w in rows)
        trigram_factor = numerator * base
        weight = {second: w for second, w in survivor}
        blended = []
        for second, w in rows:
            blended.append((second, base_factor * w + trigram_factor * weight.get(second, 0)))
        return blended

    def banned_pairs(self):
        """Outbound-forbidden adjacent character pairs for this group."""
        return {(row[0], row[1]) for row in self.db.execute('SELECT first,second FROM pair_bans')}

    def breaking_pair(self, text, banned=None):
        """The first forbidden adjacent pair inside `text`, or None."""
        if banned is None:
            banned = self.banned_pairs()
        if not banned:
            return None
        for pair in zip(text, text[1:]):
            if pair in banned:
                return pair
        return None

    def handle(self, event):
        """Accept a synthetic/previously isolated OneBot-shaped event.

        These checks protect the engine, NOT the upstream QQ client's reception.
        """
        self.last_gate_decision = None
        if not isinstance(event, dict):
            return None
        # Check routing metadata before looking at message content.
        if (event.get('post_type') != 'message'
                or event.get('message_type') != 'group'
                or identifier(event.get('group_id')) != self.group_id
                or identifier(event.get('self_id')) != self.self_id):
            return None
        user = identifier(event.get('user_id'))
        if not user or user == self.self_id:
            return None
        message_id = identifier(event.get('message_id'))
        timestamp = event.get('time')
        if (not message_id or type(timestamp) not in (int, float)
                or not math.isfinite(timestamp) or timestamp < 0):
            return None
        if (self.last_time is not None
                and timestamp < self.last_time - TIMESTAMP_TOLERANCE):
            return None
        # Gate 00 is a read-only hard stop: no report, notice or quality event.
        with self.db:
            active = self.active_ban(user, self.clock())
            if active is not None:
                if not self.db.execute('INSERT OR IGNORE INTO seen VALUES (?)',
                                       (message_id,)).rowcount:
                    return None
                if self.last_time is None or timestamp > self.last_time:
                    self.last_time = timestamp
                self.previous = None
                self.intelligence.reset_context()
                envelope=gate_normalize_event(event,group_id=self.group_id,self_id=self.self_id)
                if envelope is not None:
                    self.last_gate_decision=evaluate_gates(envelope,now=self.clock(),
                        history=self.gate_history,config=self.gate_config,banned=True)
                return None
        envelope=gate_normalize_event(event,group_id=self.group_id,self_id=self.self_id)
        if envelope is None:
            return None
        with self.db:
            if not self.db.execute('INSERT OR IGNORE INTO seen VALUES (?)',
                                   (message_id,)).rowcount:
                return None
            if self.last_time is None or timestamp > self.last_time:
                self.last_time = timestamp
            now=self.clock()
            decision=evaluate_gates(envelope,now=now,history=self.gate_history,
                config=self.gate_config,screening_exempt=self.whitelisted(user,now),
                prior_violations=self.violation_count(user,now))
            self.last_gate_decision=decision
            if decision.case is not None:
                self.quality.score(user)
                self.quality.record_case(decision.case)
            self.gate_history.add(gate_record_for(envelope,time=timestamp,
                                  blocked=decision.blocked),now=timestamp)
            for finding in decision.findings:
                self.gate_history.add_hit(self.group_id,user,finding.category,timestamp)
            if decision.blocked:
                self.previous=None
                self.intelligence.reset_context()
                if decision.notice and self.quality.claim_notice(decision.notice):
                    reply=group_reply(self.group_id,decision.notice['template'])
                    reply['gate_notice']=decision.notice
                    return reply
                return None
        # Require text segments; never parse raw CQ codes, attachments or quotes.
        segments = event.get('message')
        if not isinstance(segments, list) or not segments:
            return None
        pieces = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get('type') != 'text':
                self.previous = None
                self.intelligence.reset_context()
                return None
            data = segment.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('text'), str):
                return None
            pieces.append(data['text'])
        text = ''.join(pieces)
        if not text:
            self.previous = None
            self.intelligence.reset_context()
            return None
        # Tolerate clock skew between QQ's server time and this host. A single
        # event carrying a slightly future timestamp (NapCat falls back to local
        # Date.now() when it cannot read server time, and this host's clock runs
        # minutes fast) used to block every following message until real time
        # caught up. Replays are still caught by the `seen` table below.
        with self.db:
            # Keep a monotonic high-water mark: accepting a slightly older message
            # must not drag the watermark back and widen the tolerance window.
            if self.last_time is None or timestamp > self.last_time:
                self.last_time = timestamp
            now = self.clock()
            head = text.strip().split(maxsplit=1)[0:1]
            if head == ['/score']:
                if text.strip() != '/score':
                    return group_reply(self.group_id,'用法：/score')
                queried=self.quality.query_score(user,self.group_id,message_id)
                reply=group_reply(self.group_id,queried['text'])
                if queried['claim']:
                    reply['score_query']=queried['claim']
                return reply
            if head == ['/clear']:
                # Only the authenticated OneBot sender id grants permission.
                # Never learn commands, including rejected or malformed ones.
                if user != CLEAR_ADMIN:
                    return group_reply(self.group_id, '无权限：仅 QQ 575586491 可以使用 /clear。')
                args = text.strip().split()
                if len(args) != 2 or args[1] not in MODELS:
                    return group_reply(self.group_id, '用法：/clear match、/clear generate 或 /clear intelligence')
                if args[1] == 'match':
                    self.db.execute('DELETE FROM qa')
                    self.previous = None
                    answer = '已清空本群 match 模型数据和问答上下文。'
                elif args[1] == 'generate':
                    self.db.execute('DELETE FROM chars')
                    self.db.execute('DELETE FROM gen_trigrams')
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pretraining'").fetchone():
                        self.db.execute('DELETE FROM pretraining')
                    answer = '已清空本群 generate 模型数据（含预训练数据）。'
                else:
                    self.intelligence.clear()
                    answer = '已清空本群 intelligence 模型数据和上下文。'
                # Keep mode and deduplication history: replayed clear commands must
                # not wipe messages learned after the original command.
                return group_reply(self.group_id, answer)
            if text.strip().split(maxsplit=1)[0:1] == ['/model']:
                args = text.strip().split()
                if len(args) != 2 or args[1] not in MODELS:
                    answer = '用法：/model match、/model generate 或 /model intelligence'
                else:
                    self.db.execute("UPDATE settings SET value=? WHERE key='model'", (args[1],))
                    answer = f'已切换到 {args[1]} 模型'
                return group_reply(self.group_id, answer)
            if head and head[0] in ADMIN_COMMANDS:
                return self.admin_command(head[0], text, user)
            # /apply is open to every member and is never screened or learned.
            if head == ['/apply']:
                return self._apply(user, now)
            # Gate 02 is review-only until its calibrated policy is approved.
            # Gate 01 has already been handled by the unified pipeline above.
            score = self.quality.score(user)
            train_allowed = self.quality.sample(score)
            reply_allowed = self.quality.sample(score)
            # All three models learn eligible messages; mode selects only the reply.
            if train_allowed:
                training_text,repairs=strong_repeat_repair(text)
                if repairs:
                    self.db.execute('''INSERT INTO repetition_repairs(user_id,at,removed)
                        VALUES (?,?,?)''',(user,timestamp,sum(r['removed'] for r in repairs)))
                self.learn_pair(self.previous, training_text, timestamp)
                self.previous = (training_text, timestamp)
                self.learn_chars(training_text, user)
                self.learn_trigrams(training_text)
                self.intelligence.observe(training_text, timestamp)
            else:
                self.previous = None
                self.intelligence.reset_context()
            if not reply_allowed:
                return None
            if self.model == 'match':
                answer = self.pick(self.db.execute(
                    'SELECT a,w FROM qa WHERE q=? ORDER BY a', (text,)).fetchall())
            elif self.model == 'generate':
                # A configured share of replies answers the whole message
                # through a keyword instead of continuing its tail. At 0.0 the
                # keyword RNG is never touched and generate is unchanged.
                if (self.keyword_reply_probability > 0
                        and self.keyword_rng.random() < self.keyword_reply_probability):
                    answer = self.keyword_reply(text)
                else:
                    # The last two characters seed the walk and its trigram
                    # blend; only the last one can be the walk's starting node.
                    answer = self.generate(text[-2:])
            else:
                answer = self.intelligence.reply(text)
        if answer and self.breaking_pair(answer) is not None:
            # The operator banned this pair on the OUTBOUND side. Drop the whole
            # reply instead of sending a truncated fragment. Only model-generated
            # text is filtered: system notices such as ban or /apply replies are
            # fixed strings and must never be swallowed by a pair ban.
            return None
        if not answer:
            return None
        reply = group_reply(self.group_id, answer)
        reply['model_generated'] = True
        return reply


def main():
    parser = argparse.ArgumentParser(description='QBot 本地离线终端；不会连接 QQ')
    parser.add_argument('--db', type=Path, default=Path(__file__).parent / 'data' / 'offline.sqlite3')
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    bot = Bot(str(args.db), self_id='offline-bot')
    print(f'离线模拟群 {TEST_GROUP}，当前模型 {bot.model}。输入 /exit 退出。')
    print('支持 /model match、/model generate、/model intelligence；所有输入仅在本地处理。')
    try:
        while True:
            value = input('你> ')
            if value == '/exit':
                break
            result = bot.handle({'post_type': 'message', 'message_type': 'group',
                'group_id': TEST_GROUP, 'self_id': 'offline-bot', 'user_id': 'offline-user',
                'message_id': uuid.uuid4().hex, 'time': time.time(),
                'message': [{'type': 'text', 'data': {'text': value}}]})
            print('机器人> ' + result['params']['message'][0]['data']['text'] if result
                  else '（已学习，暂无可用回复）')
    except (EOFError, KeyboardInterrupt):
        print('\n已退出。')
    finally:
        bot.close()


if __name__ == '__main__':
    main()
