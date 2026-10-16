"""Local, password-free administration dashboard for QBot.

Binds to 127.0.0.1 only. There is no authentication by design (the operator asked
for a local panel with no password), so the port must never be exposed: every
route is reachable by anything that can open a loopback socket.

Writes go through their own short-lived sqlite connections, so the dashboard can
never corrupt the bot's long-running connection; SQLite serialises them and a
busy write returns an error to the page instead of blocking the service.
"""
import json
import math
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import datastore
import moderation
import gates
import loop_cleanup
from quality import QualityLedger
from gate_review import GateReviewStore
from qbot import (GROUP_DATABASES, MAX_MANUAL_BAN, MAX_REASON, MIN_MANUAL_BAN, MODELS,
                  PRODUCTION_GROUP, QQ_RE, TEST_GROUP, THIRD_GROUP, FOURTH_GROUP, Bot, is_counted)

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / 'data'
PRE_ROOT = ROOT / 'pre'
WEB_ROOT = ROOT / 'web'
LOG_PATH = ROOT / 'runtime' / 'bot.log'
STOP_PATH = ROOT / 'runtime' / 'STOP'
RUNTIME = ROOT / 'runtime' / 'connection.json'
QUALITY_DB = DATA_ROOT / 'quality.sqlite3'
STICKER_DB = DATA_ROOT / 'stickers' / 'stickers.sqlite3'
SIGN_PATH = ROOT / 'res' / 'sign.png'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 5703
MAX_BODY = 65536
MAX_TEXT = 512
STARTED_AT = time.time()

GROUP_LABELS = {
    TEST_GROUP: '测试群 · Arabidopsis Thaliana',
    PRODUCTION_GROUP: '正式群 · USing main(1nt 2);',
    THIRD_GROUP: '第三群 · 山农神人会',
    FOURTH_GROUP: '第四群 · 929618496',
}


def self_id():
    try:
        return json.loads(RUNTIME.read_text(encoding='utf-8'))['self_id']
    except (OSError, KeyError, ValueError):
        return '3860000000'


def connect(group):
    """Yield a short-lived connection for one group, always closed afterwards.

    `with sqlite3.connect(...)` only wraps a transaction and would leak the
    handle, so the panel uses this context manager for every request.
    """
    @contextmanager
    def scope():
        db = sqlite3.connect(str(DATA_ROOT / GROUP_DATABASES[group]), timeout=5)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return scope()


def groups():
    return list(GROUP_DATABASES)


def group_or_error(value):
    return value if value in GROUP_DATABASES else None


# ---------------------------------------------------------------- statistics

def _scalar(db, sql, params=(), default=0):
    row = db.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else default


def summarize(db, group, now):
    """One row for the dashboard overview."""
    bans = _scalar(db, 'SELECT COUNT(*) FROM bans')
    has_whitelist = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whitelist'").fetchone() is not None
    whitelist = _scalar(db, 'SELECT COUNT(*) FROM whitelist WHERE until>?', (now,)) \
        if has_whitelist else 0
    violations = _scalar(db, 'SELECT COUNT(*) FROM violations')
    return {
        'group': group,
        'label': GROUP_LABELS.get(group, group),
        'model': _scalar(db, "SELECT value FROM settings WHERE key='model'", default='match'),
        'bans': bans,
        'whitelist': whitelist,
        'violations': violations,
        'qa_pairs': _scalar(db, 'SELECT COUNT(*) FROM qa'),
        'qa_weight': _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM qa'),
        'transitions': _scalar(db, 'SELECT COUNT(*) FROM chars'),
        'transition_weight': _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM chars'),
        # The optional trigram lookahead table (next_gen.md); 0 rows while the
        # blend has never been trained, and the panel shows counts only.
        'trigrams': _scalar(db, 'SELECT COUNT(*) FROM gen_trigrams')
                    if datastore.has_table(db, 'gen_trigrams') else 0,
        'trigram_weight': _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM gen_trigrams')
                          if datastore.has_table(db, 'gen_trigrams') else 0,
        'memories': _scalar(db, 'SELECT COUNT(*) FROM intel_memory'),
        'links': _scalar(db, 'SELECT COUNT(*) FROM intel_links'),
        'seen': _scalar(db, 'SELECT COUNT(*) FROM seen'),
        'bytes': (DATA_ROOT / GROUP_DATABASES[group]).stat().st_size
                 if (DATA_ROOT / GROUP_DATABASES[group]).is_file() else 0,
    }


def ban_rows(db, now):
    return [{
        'user_id': row[0], 'until': row[1], 'seconds': row[2], 'reason': row[3],
        'mode': row[4], 'created_at': row[5], 'remaining': max(0, int(row[1] - now)),
    } for row in db.execute(
        'SELECT user_id,until,seconds,reason,mode,created_at FROM bans ORDER BY created_at DESC')]


def whitelist_rows(db, now):
    return [{
        'user_id': row[0], 'until': row[1], 'seconds': row[2], 'granted_at': row[3],
        'remaining': max(0, int(row[1] - now)),
    } for row in db.execute(
        'SELECT user_id,until,seconds,granted_at FROM whitelist WHERE until>? '
        'ORDER BY granted_at', (now,))]


def violation_rows(db, limit=50):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='violation_log'"
                      ).fetchone():
        return []
    return [{'user_id': row[0], 'at': row[1], 'grade': row[2], 'term': row[3]}
            for row in db.execute(
                'SELECT user_id,at,grade,term FROM violation_log ORDER BY at DESC LIMIT ?',
                (limit,))]


def anomalies(rows, key='w', threshold=3.0, limit=15):
    """Robust outliers on log10(weight) using median and MAD.

    Weights in this model are heavy-tailed, so a median/MAD z-score on the log
    scale is far more meaningful than a mean/standard-deviation cut.
    """
    if len(rows) < 8:
        return {'high': [], 'low': [], 'median': None, 'mad': None}
    values = [math.log10(max(1, row[key])) for row in rows]
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in values)
    mad = deviations[len(deviations) // 2] or 1e-9
    high, low = [], []
    for row, value in zip(rows, values):
        z = 0.6745 * (value - median) / mad
        if z >= threshold:
            high.append(dict(row, z=round(z, 2)))
        elif z <= -threshold:
            low.append(dict(row, z=round(z, 2)))
    return {'high': high[:limit], 'low': low[:limit],
            'median': round(median, 3), 'mad': round(mad, 3)}


def attribution(db):
    """How much of the model carries a sender, so the QQ filter is honest."""
    total = _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM chars')
    attributed = _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM user_chars')
    return {'total_weight': total, 'attributed_weight': attributed,
            'coverage': round(attributed / total, 4) if total else 0.0,
            'senders': _scalar(db, 'SELECT COUNT(DISTINCT user_id) FROM user_chars')}


def sender_rows(db, limit=300):
    """Senders that own attributed transitions, heaviest first."""
    return [{'user_id': row[0], 'weight': row[1], 'transitions': row[2]}
            for row in db.execute('''SELECT user_id, SUM(w), COUNT(*) FROM user_chars
                GROUP BY user_id ORDER BY SUM(w) DESC LIMIT ?''', (limit,))]


def generate_stats(db, user=None, top=25):
    """Overall view of the generate model, optionally scoped to one sender.

    The charts report a CHINESE-ONLY view: letters, digits, ASCII symbols,
    whitespace and emoji are excluded from every statistic here, because they
    say nothing about how Chinese is written. `total_weight` still reports the
    model's real total, since that is the base the percentage-based weight
    adjustment works from.
    """
    if user:
        source = 'SELECT first,second,w FROM user_chars WHERE user_id=?'
        params = (user,)
    else:
        source = 'SELECT first,second,w FROM chars'
        params = ()
    every = [{'first': row[0], 'second': row[1], 'w': row[2]}
             for row in db.execute(source, params)]
    rows = [row for row in every
            if is_counted(row['first']) and is_counted(row['second'])]
    real_total = sum(row['w'] for row in every)
    total = sum(row['w'] for row in rows)
    for row in rows:
        row['share'] = round(row['w'] / total, 6) if total else 0.0
    rows.sort(key=lambda row: -row['w'])
    weights = [row['w'] for row in rows]
    histogram = {'bins': [], 'counts': []}
    if weights:
        lo, hi = math.log10(min(weights)), math.log10(max(weights))
        if hi - lo < 1e-9:
            hi = lo + 1.0
        edges = [lo + (hi - lo) * i / 14 for i in range(15)]
        counts = [0] * 14
        for weight in weights:
            position = min(13, int((math.log10(weight) - lo) / (hi - lo) * 14))
            counts[position] += 1
        histogram = {'bins': [round(edge, 3) for edge in edges], 'counts': counts}
    endings = len([row for row in rows if row['second'] == ''])
    return {
        'scope': 'user' if user else 'all',
        'user': user,
        'attribution': attribution(db),
        'transitions': len(rows), 'total_weight': real_total,
        'counted_weight': total, 'excluded_weight': real_total - total,
        'distinct_first': len({row['first'] for row in rows}),
        'distinct_second': len({row['second'] for row in rows}),
        'mean_weight': round(total / len(rows), 2) if rows else 0,
        'max_weight': max(weights, default=0),
        'endings': endings,
        'top': rows[:top], 'histogram': histogram, 'anomalies': anomalies(rows),
    }


def pair_detail(db, first, second):
    """Drill-down of one transition: rank, share and who produced it."""
    rows = [{'first': row[0], 'second': row[1], 'w': row[2]}
            for row in db.execute('SELECT first,second,w FROM chars')]
    total = sum(row['w'] for row in rows)
    weight = next((row['w'] for row in rows
                   if row['first'] == first and row['second'] == second), 0)
    ranked = sorted(rows, key=lambda row: -row['w'])
    rank = next((index + 1 for index, row in enumerate(ranked)
                 if row['first'] == first and row['second'] == second), None)
    contributors = [{'user_id': row[0], 'w': row[1]} for row in db.execute(
        'SELECT user_id,w FROM user_chars WHERE first=? AND second=? ORDER BY w DESC LIMIT 30',
        (first, second))]
    return {
        'first': first, 'second': second, 'weight': weight,
        'share': round(weight / total, 6) if total else 0.0,
        'rank': rank, 'total_transitions': len(rows), 'total_weight': total,
        'distinct_users': _scalar(db,
            'SELECT COUNT(*) FROM user_chars WHERE first=? AND second=?', (first, second)),
        'attributed_weight': sum(row['w'] for row in contributors),
        'contributors': contributors,
    }


def explore(db, char, limit=18, user=None):
    """Everything the charts need for one character.

    The aggregate table keeps non-Chinese rows for history, but generate() never
    walks them, so this view ignores them too: an exploration panel that showed
    pairs the model refuses to use would be misleading.

    With `user` set, the whole view is recomputed from that sender's own
    attributed transitions instead of the aggregate model.
    """
    if not isinstance(char, str) or not char or len(char) > 4:
        raise ValueError('请输入 1–4 个字符')
    if user:
        pairs = [(row[0], row[1], row[2]) for row in db.execute(
            'SELECT first,second,w FROM user_chars WHERE user_id=?', (user,))]
    else:
        pairs = [(first, second, w) for first, second, w in db.execute(
            'SELECT first,second,w FROM chars')
            if is_counted(first) and is_counted(second)]

    model_total = sum(w for _, _, w in pairs)
    weight = {(first, second): w for first, second, w in pairs}
    in_weight, out_weight = {}, {}
    for first, second, w in pairs:
        in_weight[second] = in_weight.get(second, 0) + w
        out_weight[first] = out_weight.get(first, 0) + w

    successors = sorted(((s, w) for f, s, w in pairs if f == char),
                        key=lambda item: -item[1])[:limit]
    predecessors = sorted(((f, w) for f, s, w in pairs if s == char),
                          key=lambda item: -item[1])[:limit]
    out_total = out_weight.get(char, 0)
    in_total = in_weight.get(char, 0)

    def enrich(rows, key, total, marginal_of):
        """Add each neighbour's model-wide share, plus its lift over that baseline.

        lift = P(neighbour | char) / P(neighbour). Around 1.0 means the pair is no
        more likely than the neighbour's overall frequency; well above 1.0 means
        the neighbour is genuinely characteristic of this character.
        """
        built = []
        for name, count in rows:
            marginal = marginal_of.get(name, 0)
            built.append({
                key: name, 'w': count,
                'share': round(count / total, 6) if total else 0.0,
                'marginal': marginal,
                'marginal_share': round(marginal / model_total, 6) if model_total else 0.0,
                'lift': (round((count / total) / (marginal / model_total), 3)
                         if total and marginal and model_total else 0.0),
            })
        return built

    successors = enrich(successors, 'second', out_total, in_weight)
    predecessors = enrich(predecessors, 'first', in_total, out_weight)
    # Heatmap: the character's own row first, then its predecessors, against its
    # successors. The centre row is the direct transition the panel is about.
    row_names = [char] + [row['first'] for row in predecessors]
    columns = [row['second'] for row in successors]
    values = [[weight.get((first, second), 0) for second in columns] for first in row_names]
    nodes = [{'id': char, 'kind': 'center', 'w': 0, 'share': 1.0}]
    links = []
    for row in predecessors:
        nodes.append({'id': row['first'], 'kind': 'prev', 'w': row['w'], 'share': row['share']})
        links.append({'source': row['first'], 'target': char, 'w': row['w'], 'kind': 'in'})
    for row in successors:
        nodes.append({'id': row['second'], 'kind': 'next', 'w': row['w'], 'share': row['share']})
        links.append({'source': char, 'target': row['second'], 'w': row['w'], 'kind': 'out'})
    entropy = -sum(row['share'] * math.log2(row['share'])
                   for row in successors if row['share'] > 0)
    cumulative, running, top80 = [], 0.0, None
    for index, row in enumerate(successors, start=1):
        running += row['share']
        cumulative.append(round(running, 6))
        if top80 is None and running >= 0.8:
            top80 = index
    return {
        'char': char, 'scope': 'user' if user else 'all', 'user': user,
        'out_total': out_total, 'in_total': in_total, 'model_total': model_total,
        'distribution_entropy': round(entropy, 4),
        # 2^H is the effective number of successors: 1 means fully deterministic.
        'effective_support': round(2 ** entropy, 3) if entropy else 0.0,
        'concentration': {'cumulative': cumulative, 'top80': top80,
                          'count': len(successors)},
        'successors': successors, 'predecessors': predecessors,
        'heatmap': {'rows': row_names, 'cols': columns, 'values': values},
        'graph': {'nodes': nodes, 'links': links},
        'anomalies': {'successors': anomalies(successors), 'predecessors': anomalies(predecessors)},
    }


def adjust_weights(db, pairs, percent, direction):
    """Raise or lower weights by a share of the model's current total weight."""
    total_before = _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM chars')
    delta = int(round(total_before * percent / 100.0))
    if direction == 'add' and delta < 1:
        delta = 1  # A non-zero request must actually move something.
    if delta < 1:
        raise ValueError('调整幅度不足以产生变化，请提高百分比')
    applied = deleted = 0
    for first, second in pairs:
        row = db.execute('SELECT w FROM chars WHERE first=? AND second=?',
                         (first, second)).fetchone()
        current = row[0] if row else 0
        if direction == 'add':
            updated = current + delta
            db.execute('''INSERT INTO chars VALUES (?,?,?)
                ON CONFLICT(first,second) DO UPDATE SET w=excluded.w''',
                (first, second, updated))
            applied += 1
        elif current == 0:
            continue
        elif current - delta <= 0:
            db.execute('DELETE FROM chars WHERE first=? AND second=?', (first, second))
            deleted += 1
        else:
            db.execute('UPDATE chars SET w=? WHERE first=? AND second=?',
                       (current - delta, first, second))
            applied += 1
    db.commit()
    total_after = _scalar(db, 'SELECT COALESCE(SUM(w),0) FROM chars')
    return {'applied': applied, 'deleted': deleted, 'delta': delta,
            'total_before': total_before, 'total_after': total_after}


def pairs_from_request(body):
    """Build the pair list from either a two-character form or a whole sentence."""
    mode = body.get('mode')
    direction = body.get('direction')
    if direction not in ('add', 'reduce'):
        raise ValueError('direction 必须是 add 或 reduce')
    try:
        percent = float(body.get('percent'))
    except (TypeError, ValueError):
        raise ValueError('percent 必须是数字')
    if not 0 < percent <= 100:
        raise ValueError('percent 必须在 0–100 之间')
    if mode == 'pair':
        first, second = body.get('first'), body.get('second')
        if not isinstance(first, str) or not isinstance(second, str):
            raise ValueError('请提供前后两字')
        first, second = first[:4], second[:4]
        if not first or not second:
            raise ValueError('请提供前后两字')
        return [(first, second)], percent, direction
    if mode == 'sentence':
        sentence = body.get('sentence')
        if not isinstance(sentence, str) or len(sentence) < 2:
            raise ValueError('请提供至少两个字符的句子')
        sentence = sentence[:MAX_TEXT]
        pairs = list(dict.fromkeys(zip(sentence, sentence[1:])))
        if not pairs:
            raise ValueError('句子中没有任何相邻两字组合')
        return pairs, percent, direction
    raise ValueError('mode 必须是 pair 或 sentence')


def non_chinese_rows(db):
    """Read-only statistic: transitions the dashboard excludes from its charts.

    That is anything involving a letter, digit, ASCII symbol, whitespace or
    emoji. Chinese punctuation IS counted, so it never shows up here. These rows
    stay in the table and the model still learns and walks them; excluding them
    is a reporting choice, and the panel offers no way to delete them.
    """
    rows = db.execute('SELECT first,second,w FROM chars').fetchall()
    bad = [row for row in rows if not (is_counted(row[0]) and is_counted(row[1]))]
    return {
        'rows': len(bad), 'weight': sum(row[2] for row in bad),
        'total_rows': len(rows), 'total_weight': sum(row[2] for row in rows),
        'weight_share': round(sum(row[2] for row in bad) / sum(row[2] for row in rows), 4)
                        if rows and sum(row[2] for row in rows) else 0.0,
        'samples': [{'first': row[0], 'second': row[1], 'w': row[2]} for row in bad[:24]],
    }


# --------------------------------------------------------------- loop sweep
#
# Memories are not a provenance ledger: model clearing, imports and memory
# pruning break correspondence with the character tables. Cleanup removes only
# proven repetitive memories and their matching QA / links, after SQLite backup.
# Anonymous chars / gen_trigrams / user_chars are never guessed or subtracted.

LOOP_SCAN_LIMIT = 5000      # sentences examined per request
LOOP_TEXT_LIMIT = 160       # characters analysed per sentence
LOOP_MIN_SCORE = 0.8        # dominant exact run; ordinary emphasis stays
LOOP_WIDTH_LIMIT = 24       # longest repeating block considered


def loop_profile(text):
    """Measure how repetitive a sentence is, without any fuzzy heuristic.

    Returns (score, width, repeats): the longest run of a block that repeats
    character-for-character, as a fraction of the sentence. score 1.0 means the
    whole sentence is one block repeated; 0.0 means nothing repeats.
    """
    if not isinstance(text, str):
        return 0.0, 0, 0
    text = text[:LOOP_TEXT_LIMIT]
    n = len(text)
    if n < 4:
        return 0.0, 0, 0
    best = (0.0, 0, 0)
    for width in range(1, min(n // 2, LOOP_WIDTH_LIMIT) + 1):
        start = 0
        while start + width <= n:
            block = text[start:start + width]
            repeats = 1
            cursor = start + width
            while text[cursor:cursor + width] == block and cursor + width <= n:
                repeats += 1
                cursor += width
            if repeats >= 2:
                score = width * repeats / n
                if score > best[0]:
                    best = (round(score, 4), width, repeats)
            start = cursor if repeats >= 2 else start + 1
    return best


def loop_rows(db, limit=LOOP_SCAN_LIMIT):
    """Strong repeat memories; excludes laughter, numbers, URLs and code."""
    return loop_cleanup.memory_rows(db, limit)[:200]


def loop_plan(db, ids, group):
    return loop_cleanup.plan_memory_cleanup(db, group, ids)


def loop_clean(db, ids, group, plan_token):
    return loop_cleanup.clean_memory(db, group, ids,
        DATA_ROOT/GROUP_DATABASES[group], DATA_ROOT/'backups'/'memory-clean', plan_token)


def pair_ban_rows(db):
    """The group's outbound pair bans, each with its current model weight.

    A weight of None means the pair is not in the model at all: generation can
    never form it, so the entry is harmless belt-and-braces.
    """
    rows = db.execute('SELECT first,second,reason,created_at FROM pair_bans '
                      'ORDER BY created_at DESC').fetchall()
    return [{'first': first, 'second': second, 'reason': reason,
             'created_at': created,
             'weight': _scalar(db, 'SELECT w FROM chars WHERE first=? AND second=?',
                               (first, second), None)}
            for first, second, reason, created in rows]


def add_pair_ban(db, first, second, reason, now):
    for value in (first, second):
        if not isinstance(value, str) or len(value.strip()) != 1:
            raise ValueError('前字与后字都必须恰好是 1 个字符')
    first, second = first.strip(), second.strip()
    reason = (reason or '').strip()[:MAX_REASON] or '管理员封禁'
    db.execute('''INSERT INTO pair_bans VALUES (?,?,?,?)
        ON CONFLICT(first,second) DO UPDATE SET reason=excluded.reason,
        created_at=excluded.created_at''', (first, second, reason, now))
    db.commit()
    return {'ok': True, 'first': first, 'second': second, 'reason': reason}


def remove_pair_ban(db, first, second):
    removed = db.execute('DELETE FROM pair_bans WHERE first=? AND second=?',
                         (first, second)).rowcount
    db.commit()
    return {'ok': True, 'removed': removed}


def dictionary():
    return {
        'grades': [{'grade': grade, 'label': moderation.GRADE_LABELS[grade],
                    'seconds': moderation.GRADE_SECONDS[grade],
                    'terms': [term for term, value in moderation.DICTIONARY.items()
                              if value == grade]} for grade in moderation.GRADES],
        'safe': list(moderation.SAFE),
        'escalation_window': moderation.ESCALATION_WINDOW,
    }


def gate_catalog():
    """Read-only operator view of deployed gate modes and review proposals."""
    cfg=gates.default_config()
    details=[
        ('00','封禁底层','有效封禁期间静默丢弃消息；不建报告，也不重复扣分。'),
        ('01','长消息','单条超过 66 字阻断训练和模型回复；疑似分拆只观察。'),
        ('02','敏感词汇','按词库分级；明确讨论或反对措辞建议零扣分、零封禁，仍保留门禁审查。处罚须人工批准。'),
        ('03','长英文与网址','英文长段或多个网址的候选检测；当前只观察。'),
        ('04','单条短重复','短片段重复至少 4 次；笑声、语气重复和明确引用建议零扣分，机械重复仍待审。当前只观察。'),
        ('05','五条消息同关键词','同一用户五条消息有重复关键词并高度相似；当前只观察。'),
        ('06','训练后补全测试','重复训练句后立刻发送前缀，或严格的特殊符号模板测试；当前只观察。'),
        ('07','发言及图片频率','高频发言或短时大量图片，正式启用后也永远静默。'),
    ]
    labels={
        '01a':'67–100 字','01b':'101–150 字','01c':'151–200 字',
        '01d':'201 字及以上','01s':'快速分拆嫌疑',
        '03a':'长英文段落','03b':'单条长网址','03c':'多个不同网址',
        '04a':'短片段重复 4–5 次','04b':'较长或更多次重复',
        '05a':'五条同关键词且部分近重复','05b':'五条高度近重复',
        '06a':'重复短句后前缀测试','06b':'特殊符号模板测试',
        '07a':'30 秒内第 8 条起','07b':'60 秒内第 5 张图起',
    }
    points={**gates.POINTS_01,**gates.POINTS_03,**gates.POINTS_04,
            **gates.POINTS_05,**gates.POINTS_06,**gates.POINTS_07}
    rows=[]
    for number,name,description in details:
        sub=[]
        if number=='00':
            sub=[{'code':'00','label':'已封禁用户','mode':'enforce',
                  'points':None,'ban_seconds':None}]
        elif number=='02':
            sub=[{'code':'02'+grade,'label':moderation.GRADE_LABELS[grade],
                  'mode':cfg['modes']['02'],'points':gates.POINTS_02[grade],
                  'ban_seconds':gates.BAN_02[grade],
                  'terms':[term for term,value in moderation.DICTIONARY.items()
                           if value==grade]}
                 for grade in moderation.GRADES]
        else:
            for code,mode in cfg['modes'].items():
                if code.startswith(number):
                    sub.append({'code':code,'label':labels.get(code,code),
                                'mode':mode,'points':points.get(code),
                                'ban_seconds':gates.BAN_06.get(code,0)})
        rows.append({'number':number,'name':name,'description':description,
                     'subtypes':sub,'notification':('始终静默' if number in ('00','07')
                        else '较严重且已核准规则才提示；当前默认关闭')})
    pending={}
    if QUALITY_DB.is_file():
        with closing(sqlite3.connect('file:'+QUALITY_DB.as_posix()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='gate_cases'").fetchone():
                pending={gate:count for gate,count in db.execute(
                    "SELECT primary_gate,COUNT(*) FROM gate_cases WHERE status IN ('pending','shadow','applying') GROUP BY primary_gate")}
    for row in rows:
        row['pending']=pending.get(row['number'],0)
    return {'version':cfg['version'],'proposal':cfg['proposal'],
            'gates':rows,'safe_terms':list(moderation.SAFE)}


def quality_state():
    if not QUALITY_DB.is_file():
        return {'status':'未初始化','users':[],'categories':[],
                'changes':[],'total_users':0,'total_events':0}
    db=sqlite3.connect('file:' + QUALITY_DB.as_posix() + '?mode=ro',uri=True)
    try:
        has_cases=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_cases'").fetchone())
        sources='SELECT user_id,COUNT(*) AS n FROM events GROUP BY user_id'
        if has_cases:
            sources+=' UNION ALL SELECT user_id,COUNT(*) AS n FROM gate_cases GROUP BY user_id'
        users=[{'user_id':row[0],'score':row[1],'version':row[2],
                'gate_events':row[3]} for row in db.execute('''SELECT u.user_id,u.score,
                    u.score_version,COALESCE(SUM(c.n),0) AS gate_events FROM users u
                    LEFT JOIN ('''+sources+''') c ON c.user_id=u.user_id
                    GROUP BY u.user_id ORDER BY gate_events DESC,u.user_id LIMIT 200''')]
        categories=[{'category':row[0],'count':row[1]} for row in db.execute(
            """SELECT CASE WHEN gate='02' THEN 'legacy_post_ban_notice:'||category
                WHEN gate='01' THEN 'legacy_01:'||category ELSE category END,
                COUNT(*) FROM events GROUP BY 1 ORDER BY 1""")]
        if has_cases:
            categories.extend({'category':row[0],'count':row[1]} for row in db.execute(
                'SELECT primary_category,COUNT(*) FROM gate_cases GROUP BY primary_category ORDER BY primary_category'))
        has_audit=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='score_audit'").fetchone()
        changes=([{'user_id':row[0],'old_score':row[1],'score':row[2],
                   'reason':row[3],'at':row[4]} for row in db.execute('''SELECT user_id,
                   old_score,new_score,reason,changed_at FROM score_audit
                   ORDER BY id DESC LIMIT 30''')] if has_audit else [])
        status = ('已启用' if _scalar(db,"SELECT COUNT(*) FROM users WHERE score_version!='uncalibrated'")
                  else '校准中')
        return {'status':status,'users':users,'categories':categories,
                'changes':changes,
                'total_users':_scalar(db,'SELECT COUNT(*) FROM users'),
                'total_events':(_scalar(db,'SELECT COUNT(*) FROM events')+
                    (_scalar(db,'SELECT COUNT(*) FROM gate_cases') if has_cases else 0))}
    finally:
        db.close()


def quality_user(user_id):
    if not isinstance(user_id,str) or not QQ_RE.fullmatch(user_id):
        raise ValueError('QQ 号格式错误')
    if not QUALITY_DB.is_file():
        return {'user_id':user_id,'score':100,'version':'uncalibrated',
                'events':[],'changes':[]}
    db=sqlite3.connect('file:' + QUALITY_DB.as_posix() + '?mode=ro',uri=True)
    try:
        row=db.execute('SELECT score,score_version FROM users WHERE user_id=?',
                       (user_id,)).fetchone()
        events=[{'group':r[0],'category':r[1],'at':r[2]} for r in db.execute(
            '''SELECT group_id,CASE WHEN gate='02' THEN 'legacy_post_ban_notice:'||category
                WHEN gate='01' THEN 'legacy_01:'||category ELSE category END,
                occurred_at FROM events WHERE user_id=?
               ORDER BY occurred_at DESC LIMIT 50''',(user_id,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_cases'").fetchone():
            events.extend({'group':r[0],'category':r[1],'at':r[2]} for r in db.execute(
                '''SELECT group_id,primary_category,occurred_at FROM gate_cases
                   WHERE user_id=? ORDER BY occurred_at DESC LIMIT 50''',(user_id,)))
            events=sorted(events,key=lambda item:item['at'],reverse=True)[:50]
        has_audit=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='score_audit'").fetchone()
        changes=([{'old_score':r[0],'score':r[1],'reason':r[2],'at':r[3]}
                  for r in db.execute('''SELECT old_score,new_score,reason,changed_at
                  FROM score_audit WHERE user_id=? ORDER BY id DESC LIMIT 50''',
                  (user_id,))] if has_audit else [])
        return {'user_id':user_id,'score':row[0] if row else 100,
                'version':row[1] if row else 'uncalibrated',
                'events':events,'changes':changes}
    finally:
        db.close()


def review_store():
    return GateReviewStore(QUALITY_DB, DATA_ROOT, SIGN_PATH)


def sticker_state():
    if not STICKER_DB.is_file():
        return []
    db=sqlite3.connect('file:' + STICKER_DB.as_posix() + '?mode=ro',uri=True)
    try:
        return [{'group':row[0],'assets':row[1],'observations':row[2]} for row in
                db.execute('''SELECT group_id,COUNT(*),COALESCE(SUM(seen_count),0)
                    FROM assets GROUP BY group_id ORDER BY group_id''')]
    finally:
        db.close()


def system_state():
    def size(path):
        return Path(path).stat().st_size if Path(path).is_file() else 0

    return {
        'pid': None, 'python': sys.version.split()[0],
        'started_at': STARTED_AT, 'uptime': int(time.time() - STARTED_AT),
        'stop': STOP_PATH.exists(),
        'log_bytes': size(LOG_PATH),
        'databases': [{'group': group, 'file': GROUP_DATABASES[group],
                       'bytes': size(DATA_ROOT / GROUP_DATABASES[group])}
                      for group in groups()],
        'quality':quality_state(),
        'stickers':sticker_state(),
    }


def log_tail(lines=120):
    if not LOG_PATH.is_file():
        return []
    with LOG_PATH.open('r', encoding='utf-8', errors='replace') as handle:
        return handle.read().splitlines()[-lines:]


# ------------------------------------------------------------------ requests

def in_group(model, group, user):
    return isinstance(user, str) and bool(QQ_RE.fullmatch(user))


def handle_api(method, path, query, body):
    """Return (status, payload) for every route the dashboard uses."""
    now = time.time()
    if path == '/api/overview':
        result = []
        for group in groups():
            with connect(group) as db:
                result.append(summarize(db, group, now))
        return 200, {'groups': result, 'now': now, 'models': list(MODELS)}

    if path == '/api/system':
        return 200, system_state()

    if path == '/api/quality':
        return 200, quality_state()

    if path == '/api/quality/user':
        try:
            return 200, quality_user(query.get('user_id'))
        except ValueError as error:
            return 400, {'error':str(error)}

    if path == '/api/quality/score' and method == 'POST':
        try:
            ledger=QualityLedger(QUALITY_DB)
            try:
                return 200, ledger.set_score(body.get('user_id'),body.get('score'),
                                             body.get('reason'))
            finally:
                ledger.close()
        except ValueError as error:
            return 400, {'error':str(error)}

    if path == '/api/gate-reports':
        try:
            return 200, {'reports':review_store().list_cases(query.get('status','pending'))}
        except ValueError as error:
            return 400, {'error':str(error)}

    if path == '/api/gate-report':
        try:
            report=review_store().get(query.get('report_id'))
            return (200,report) if report else (404,{'error':'报告不存在'})
        except ValueError as error:
            return 400, {'error':str(error)}

    if path == '/api/gate-report/approve' and method == 'POST':
        try:
            result=review_store().approve(body.get('report_id'),
                reviewer=body.get('reviewer'),points=body.get('points'),
                ban_seconds=body.get('ban_seconds',0),reason=body.get('reason'))
            return 200,result
        except ValueError as error:
            return 400,{'error':str(error)}

    if path == '/api/gate-report/reject' and method == 'POST':
        try:
            return 200,review_store().reject(body.get('report_id'))
        except ValueError as error:
            return 400,{'error':str(error)}

    if path == '/api/log':
        return 200, {'lines': log_tail()}

    if path == '/api/dictionary':
        return 200, dictionary()

    if path == '/api/gates':
        return 200, gate_catalog()

    if path == '/api/bans':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            return 200, {'group': group, 'bans': ban_rows(db, now),
                         'whitelist': whitelist_rows(db, now),
                         'violations': violation_rows(db)}

    if path == '/api/ban' and method == 'POST':
        group = group_or_error(body.get('group'))
        user = body.get('user_id')
        if group is None:
            return 400, {'error': '未知群'}
        if not in_group(None, group, user):
            return 400, {'error': 'QQ 号无效'}
        try:
            seconds = int(body.get('seconds'))
        except (TypeError, ValueError):
            return 400, {'error': '时长必须是整数秒'}
        if not MIN_MANUAL_BAN <= seconds <= MAX_MANUAL_BAN:
            return 400, {'error': f'时长必须在 {MIN_MANUAL_BAN}–{MAX_MANUAL_BAN} 秒之间'}
        reason = str(body.get('reason') or '后台封禁')[:MAX_REASON]
        with connect(group) as db:
            db.execute('''INSERT INTO bans VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET until=excluded.until,
                seconds=excluded.seconds, reason=excluded.reason,
                mode=excluded.mode, created_at=excluded.created_at''',
                (user, now + seconds, seconds, reason, '后台封禁', now))
            db.commit()
        return 200, {'ok': True, 'user_id': user, 'seconds': seconds}

    if path == '/api/unban' and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            removed = db.execute('DELETE FROM bans WHERE user_id=?',
                                 (body.get('user_id'),)).rowcount
            db.commit()
        return 200, {'ok': True, 'removed': removed}

    if path == '/api/dstx' and method == 'POST':
        released = []
        for group in groups():
            with connect(group) as db:
                rows = [row[0] for row in db.execute('SELECT user_id FROM bans')]
                if rows:
                    db.execute('DELETE FROM bans')
                    db.commit()
                released.extend(rows)
        return 200, {'ok': True, 'released': sorted(set(released))}

    if path == '/api/whitelist/grant' and method == 'POST':
        group = group_or_error(body.get('group'))
        user = body.get('user_id')
        if group is None:
            return 400, {'error': '未知群'}
        if not in_group(None, group, user):
            return 400, {'error': 'QQ 号无效'}
        try:
            seconds = int(body.get('seconds'))
        except (TypeError, ValueError):
            return 400, {'error': '时长必须是整数秒'}
        if not 30 <= seconds <= 86400:
            return 400, {'error': '白名单时长必须在 30–86400 秒之间'}
        with connect(group) as db:
            db.execute('INSERT OR REPLACE INTO whitelist VALUES (?,?,?,?)',
                       (user, now + seconds, seconds, now))
            db.execute('INSERT OR REPLACE INTO apply_log VALUES (?,?)', (user, now))
            db.commit()
        return 200, {'ok': True}

    if path == '/api/whitelist/revoke' and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            removed = db.execute('DELETE FROM whitelist WHERE user_id=?',
                                 (body.get('user_id'),)).rowcount
            db.commit()
        return 200, {'ok': True, 'removed': removed}

    if path == '/api/model' and method == 'POST':
        group = group_or_error(body.get('group'))
        model = body.get('model')
        if group is None:
            return 400, {'error': '未知群'}
        if model not in MODELS:
            return 400, {'error': '未知模型'}
        with connect(group) as db:
            db.execute("UPDATE settings SET value=? WHERE key='model'", (model,))
            db.commit()
        return 200, {'ok': True, 'model': model}

    if path == '/api/generate':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        user = query.get('user') or None
        with connect(group) as db:
            return 200, generate_stats(db, user=user)

    if path == '/api/generate/users':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            return 200, {'senders': sender_rows(db), 'attribution': attribution(db)}

    if path == '/api/pairbans':
        group = group_or_error(query.get('group') if method == 'GET' else body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        if method == 'GET':
            with connect(group) as db:
                return 200, {'group': group, 'bans': pair_ban_rows(db)}
        try:
            with connect(group) as db:
                return 200, add_pair_ban(db, body.get('first'), body.get('second'),
                                         body.get('reason'), now)
        except ValueError as error:
            return 400, {'error': str(error)}

    if path == '/api/pairbans/delete' and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            return 200, remove_pair_ban(db, body.get('first'), body.get('second'))

    if path == '/api/loops':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            repair_count = repair_chars = 0
            if datastore.has_table(db, 'repetition_repairs'):
                repair_count = _scalar(db, 'SELECT COUNT(*) FROM repetition_repairs')
                repair_chars = _scalar(db, 'SELECT COALESCE(SUM(removed),0) FROM repetition_repairs')
            return 200, {'group': group, 'loops': loop_rows(db),
                         'threshold': LOOP_MIN_SCORE, 'text_limit': 512,
                         'protected_tables': ['chars','gen_trigrams','user_chars'],
                         'training_repairs': repair_count, 'removed_characters': repair_chars,
                         'automatic_cleanup': {'interval_seconds': 300,
                                               'scan_limit': 5000, 'delete_limit': 20}}

    if path in ('/api/loops/preview', '/api/loops/clean') and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        raw = body.get('ids')
        if not isinstance(raw, list) or not raw:
            return 400, {'error': '请至少选择一条语句'}
        if any(type(value) is not int or value <= 0 for value in raw):
            return 400, {'error': 'id 必须是整数'}
        ids = raw
        with connect(group) as db:
            try:
                if path == '/api/loops/preview':
                    return 200, loop_plan(db, ids, group)
                return 200, loop_clean(db, ids, group, body.get('plan_token'))
            except (ValueError, OSError, sqlite3.Error) as error:
                return 400, {'error': str(error)}

    if path == '/api/generate/non_chinese':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            return 200, non_chinese_rows(db)

    if path == '/api/generate/pair':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        first, second = query.get('first', ''), query.get('second', '')
        if not first or not second or len(first) > 4 or len(second) > 4:
            return 400, {'error': '请提供前后两字'}
        with connect(group) as db:
            return 200, pair_detail(db, first, second)

    if path == '/api/generate/explore':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        try:
            with connect(group) as db:
                return 200, explore(db, query.get('char', ''), user=query.get('user') or None)
        except ValueError as error:
            return 400, {'error': str(error)}

    if path == '/api/generate/adjust' and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        try:
            pairs, percent, direction = pairs_from_request(body)
        except ValueError as error:
            return 400, {'error': str(error)}
        with connect(group) as db:
            try:
                report = adjust_weights(db, pairs, percent, direction)
            except ValueError as error:
                return 400, {'error': str(error)}
        return 200, {'ok': True, 'report': report}

    if path == '/api/match':
        group = group_or_error(query.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        with connect(group) as db:
            pairs = [{'q': row[0], 'a': row[1], 'w': row[2]} for row in db.execute(
                'SELECT q,a,w FROM qa ORDER BY w DESC LIMIT 200')]
            top = [{'a': row[0], 'w': row[1]} for row in db.execute(
                'SELECT a,SUM(w) w FROM qa GROUP BY a ORDER BY w DESC LIMIT 15')]
            return 200, {'pairs': pairs, 'top_answers': top,
                         'count': db.execute('SELECT COUNT(*) FROM qa').fetchone()[0],
                         'weight': db.execute('SELECT COALESCE(SUM(w),0) FROM qa').fetchone()[0],
                         'anomalies': anomalies(pairs)}

    if path == '/api/sources':
        try:
            return 200, {'sources': datastore.list_sources(PRE_ROOT)}
        except OSError:
            return 200, {'sources': []}

    if path == '/api/train' and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        reference = body.get('name')
        if reference == 'list':
            return 200, {'sources': datastore.list_sources(PRE_ROOT)}
        try:
            path = datastore.source_file_for(PRE_ROOT, reference)
        except ValueError as error:
            return 400, {'error': str(error)}
        bot = Bot(str(DATA_ROOT / GROUP_DATABASES[group]), self_id=self_id(),
                  group_id=group, pre_root=PRE_ROOT)
        try:
            report = bot.train_file(path)
            # train_file leaves committing to its caller: the live path runs
            # inside Bot.handle's transaction, so the panel must commit here or
            # closing the bot would silently throw the whole import away.
            bot.db.commit()
            return 200, report
        except (ValueError, OSError) as error:
            bot.db.rollback()
            return 400, {'error': str(error)}
        finally:
            bot.close()

    if path in ('/api/save', '/api/load') and method == 'POST':
        group = group_or_error(body.get('group'))
        if group is None:
            return 400, {'error': '未知群'}
        try:
            folder = datastore.folder_for(PRE_ROOT, body.get('name'))
        except ValueError as error:
            return 400, {'error': str(error)}
        bot = Bot(str(DATA_ROOT / GROUP_DATABASES[group]), self_id=self_id(),
                  group_id=group, pre_root=PRE_ROOT)
        try:
            if path == '/api/save':
                datastore.write_backup(folder, datastore.export_database(
                    bot.db, group_id=group, saved_at=now))
                return 200, {'ok': True, 'summary': datastore.summarize(bot.db)}
            report = datastore.import_database(bot.db, datastore.read_backup(folder))
            bot.db.commit()
            return 200, {'ok': True, 'report': report}
        except (ValueError, OSError) as error:
            return 400, {'error': str(error)}
        finally:
            bot.close()

    if path == '/api/clear' and method == 'POST':
        group = group_or_error(body.get('group'))
        model = body.get('model')
        if group is None:
            return 400, {'error': '未知群'}
        if model not in MODELS:
            return 400, {'error': '未知模型'}
        with connect(group) as db:
            if model == 'match':
                db.execute('DELETE FROM qa')
            elif model == 'generate':
                db.execute('DELETE FROM chars')
                if datastore.has_table(db, 'gen_trigrams'):
                    db.execute('DELETE FROM gen_trigrams')
                if datastore.has_pretraining(db):
                    db.execute('DELETE FROM pretraining')
            else:
                db.execute('DELETE FROM intel_memory')
            db.commit()
        return 200, {'ok': True}

    return 404, {'error': '未知接口'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'QBotAdmin'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, status, payload, content_type='application/json'):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        mime=(content_type if content_type in ('application/pdf','image/png')
              else content_type + '; charset=utf-8')
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ('/api/gate-report/pdf','/api/gate-report/pdf-info',
                           '/api/gate-report/pdf-page'):
            query={key:values[0] for key,values in parse_qs(parsed.query).items()}
            try:
                store=review_store()
                report=store.get(query.get('report_id'))
                if report is None:
                    return self._send(404,{'error':'报告不存在'})
                signature=(store.signature(report['signature_sha256'])
                           if report.get('status')=='approved' and
                           report.get('signature_sha256') else None)
                from gate_report_pdf import render_pdf, pdf_page_count, render_page_png
                pdf=render_pdf(report,signature_bytes=signature)
            except (ValueError,OSError,RuntimeError):
                return self._send(404,{'error':'报告 PDF 暂不可用'})
            if parsed.path == '/api/gate-report/pdf-info':
                return self._send(200,{'page_count':pdf_page_count(pdf),
                                        'report_id':report['report_id']})
            if parsed.path == '/api/gate-report/pdf-page':
                try:
                    page=int(query.get('page',''))
                    png=render_page_png(pdf,page)
                except (ValueError,TypeError):
                    return self._send(400,{'error':'PDF 页码无效'})
                return self._send(200,png,'image/png')
            self.send_response(200)
            self.send_header('Content-Type','application/pdf')
            self.send_header('Content-Disposition','inline; filename="gate-report.pdf"')
            self.send_header('Content-Length',str(len(pdf)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(pdf)
            return
        if parsed.path == '/api/gate-signature':
            query={key:values[0] for key,values in parse_qs(parsed.query).items()}
            try:
                raw=review_store().signature(query.get('sha256'))
            except (ValueError, OSError):
                return self._send(404,{'error':'签名不存在'})
            return self._send(200,raw,'image/png')
        if parsed.path in ('/', '/index.html'):
            page = WEB_ROOT / 'admin.html'
            if not page.is_file():
                return self._send(500, {'error': '缺少 web/admin.html'})
            return self._send(200, page.read_bytes(), 'text/html')
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            status, payload = handle_api('GET', parsed.path, query, {})
        except Exception as error:  # Never leak a stack trace into the page.
            status, payload = 500, {'error': f'{type(error).__name__}: {error}'}
        self._send(status, payload)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if not 0 <= length <= MAX_BODY:
            return self._send(413, {'error': '请求体过大'})
        raw = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            return self._send(400, {'error': '请求体必须是 JSON'})
        if not isinstance(body, dict):
            return self._send(400, {'error': '请求体必须是 JSON 对象'})
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            status, payload = handle_api('POST', parsed.path, query, body)
        except Exception as error:
            status, payload = 500, {'error': f'{type(error).__name__}: {error}'}
        self._send(status, payload)


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f'Admin dashboard: http://{host}:{port}/ (loopback only, no password)', flush=True)
    server.serve_forever()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='QBot 本地管理后台（仅监听 127.0.0.1）')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if args.host != DEFAULT_HOST:
        raise SystemExit('Only the loopback address is allowed')
    serve(args.host, args.port)


if __name__ == '__main__':
    main()
