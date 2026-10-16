"""Gate pipeline v2: normalization, gate 00 and gates 01-07.

Everything in this module is pure and offline:

* no QQ / NapCat API, no network, no logging, no database access;
* every entry point takes an explicit clock value (`now`) and an explicit,
  bounded `GateHistory`, so the same inputs always produce the same
  `GateDecision` (deterministic offline replay and unit tests);
* side effects (writing a case, banning, sending) belong to the callers
  (`qbot.Bot`, `quality.QualityLedger`, `gate_review`, `live.Sender`).

Contract summary (see next/gate_pipeline_v2.md):

* 00 (an active ban) short-circuits everything: no findings, no case, no
  stored text, no output.
* Findings for 01-07 are collected in parallel; one message yields at most
  one case holding every finding.
* `mode` is `enforce` (blocks training/replies and clears model context) or
  `shadow` (records candidate evidence only, never changes old behaviour).
* Notification is an independent policy, `silent` by default: a hit is never
  equal to a group message.
* Nickname / group card / at-display-name fields are metadata and are never
  part of the text that gets scanned.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# ---------------------------------------------------------------- constants

RULE_VERSION = 'gates-v2.1-record-review'
PROPOSAL_VERSION = 'proposal-v1'
SCHEMA_VERSION = 1
MAX_EVIDENCE_TEXT = 262144          # one OneBot event body, 256 KiB
COOLDOWN_SECONDS = 86400.0          # non-01 notice cooldown, (group,user,gate,cat)

# Primary-gate and notification priority (06 > 02 > 01 > 03 > 04, then 05/07).
GATE_PRIORITY = ('06', '02', '01', '03', '04', '05', '07')

# Pre-registered proposal values (proposal-v1). Conservative review *suggestions*
# only: nothing here is ever applied without an explicit human approval.
POINTS_01 = {'01a': 1, '01b': 2, '01c': 3, '01d': 4}
POINTS_02 = {'Ia': 1, 'Ib': 2, 'Ic': 3,
             'IIa': 4, 'IIb': 5, 'IIc': 6,
             'IIIa': 7, 'IIIb': 8, 'IIIc': 9,
             'IVa': 10, 'IVb': 12, 'Va': 15, 'Vb': 18}
BAN_02 = {'Ia': 0, 'Ib': 0, 'Ic': 0, 'IIa': 0, 'IIb': 0, 'IIc': 0,
          'IIIa': 30, 'IIIb': 30, 'IIIc': 30,
          'IVa': 60, 'IVb': 90, 'Va': 120, 'Vb': 180}
POINTS_03 = {'03a': 0, '03b': 0, '03c': 1}
POINTS_04 = {'04a': 0, '04b': 1}
POINTS_05 = {'05a': 1, '05b': 2}
POINTS_06 = {'06a': 12, '06b': 20}
BAN_06 = {'06a': 30, '06b': 60}
POINTS_07 = {'07a': 0, '07b': 0}

IMAGE_TYPES = frozenset(('image', 'face', 'sface', 'bface', 'marketFace', 'market_face'))
NON_TEXT_TYPES = frozenset(('image', 'face', 'sface', 'bface', 'marketFace', 'market_face',
                            'record', 'video', 'reply', 'forward', 'node', 'json', 'xml',
                            'markdown', 'file', 'dice', 'rps'))

# Commands the engine recognises. A message whose first token is one of these is
# a command: commands never train and only gate 07 (frequency) may look at them.
COMMANDS = frozenset(('/clear', '/model', '/score', '/apply', '/ban', '/unban',
                      '/save', '/load', '/train', '/dstx'))

SENTENCE_END = tuple('。！？!?…．.；;')
CJK_RANGES = (('\u3400', '\u4dbf'), ('\u4e00', '\u9fff'), ('\uf900', '\ufaff'))
ASCII_SPECIAL = frozenset('!@#$%^&*()[]{}<>+=~`|\\/:;"\'?,._-')
EXPRESSIVE_REPEAT = frozenset('哈呵嘿嘻喵啊嗯哦等')

URL_RE = re.compile(r'(?i)(?:https?://|www\.)[^\s<>"\'\u3000\uff08\uff09\u3010\u3011<>]+')
TRAILING_PUNCT = '.,;:!?)]}>\'"\u3002\uff01\uff1f\uff0c\u3001\uff1b\uff1a\u3009\u3011\u300b'
ENGLISH_RUN_RE = re.compile(r'[A-Za-z]+')
ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z']+")
FENCE_RE = re.compile(r'```.*?```', re.S)
INLINE_CODE_RE = re.compile(r'`[^`]*`')

# Common-topic words that must never become a 05 keyword on their own.
STOPWORDS_ZH = frozenset((
    '我们', '你们', '他们', '今天', '明天', '昨天', '现在', '时候', '什么', '怎么',
    '这个', '那个', '就是', '还是', '不是', '没有', '可以', '因为', '所以', '但是',
    '如果', '知道', '觉得', '需要', '大家', '问题', '时间', '学习', '老师', '同学',
    '群主', '管理员', '消息', '内容', '一下', '怎么了', '东西', '地方', '开始',
    '已经', '还有', '一个', '自己', '这样', '那样', '为什么', '怎么样', '真的',
))
STOPWORDS_EN = frozenset((
    'the', 'and', 'that', 'this', 'with', 'have', 'has', 'had', 'for', 'not',
    'are', 'was', 'were', 'you', 'your', 'but', 'his', 'her', 'its', 'our',
    'their', 'what', 'when', 'where', 'which', 'while', 'will', 'would', 'could',
    'should', 'can', 'may', 'might', 'about', 'into', 'from', 'than', 'then',
    'them', 'they', 'these', 'those', 'there', 'here', 'been', 'being', 'does',
    'doing', 'done', 'just', 'also', 'very', 'more', 'most', 'some', 'such',
))

DEFAULT_MODES = {
    '00': 'enforce',
    '01a': 'enforce', '01b': 'enforce', '01c': 'enforce', '01d': 'enforce',
    '01s': 'shadow',
    '02': 'enforce',
    '03a': 'shadow', '03b': 'shadow', '03c': 'shadow',
    '04a': 'shadow', '04b': 'shadow',
    '05a': 'shadow', '05b': 'shadow',
    '06a': 'shadow', '06b': 'shadow',
    '07a': 'shadow', '07b': 'shadow',
}

# Notification thresholds (3.1). `enabled` is False for every gate by default:
# a hint may only be considered after the rule itself is enforce *and* the
# offline false-positive evaluation + human review approved it.
DEFAULT_NOTIFY = {
    '01': {'enabled': False, 'window': 300.0, 'need': 3, 'category': '01d'},
    '01s': {'enabled': False},
    '02': {'enabled': False, 'min_grade': 'IVa'},
    '03': {'enabled': False, 'window': 600.0, 'need': 2, 'category': '03c'},
    '04': {'enabled': False, 'min_repeats': 10, 'min_run_chars': 30},
    '05': {'enabled': False},
    '06': {'enabled': False},
    '07': {'enabled': False},
    '00': {'enabled': False},
}

NOTICE_TEMPLATES = {
    '01': '提示：过长的消息未纳入训练，请精简后再发送。',
    '01s': '提示：拆分发送的长消息未纳入训练，请一次性完整发送。',
    '02': '提示：该消息未纳入训练，请注意发言规范。',
    '03': '提示：链接较多的消息未纳入训练。',
    '04': '提示：重复内容未纳入训练，请勿刷屏。',
    '06': '提示：重复测试内容未纳入训练，请勿刷屏。',
}


def default_config():
    """Fresh versioned config; callers may override single entries."""
    return {
        'version': RULE_VERSION,
        'proposal': PROPOSAL_VERSION,
        'modes': dict(DEFAULT_MODES),
        'notify': {key: dict(value) for key, value in DEFAULT_NOTIFY.items()},
        'history_limit': 20,
        'history_max_age': 180.0,
        'commands': frozenset(COMMANDS),
        'stopwords_zh': STOPWORDS_ZH,
        'stopwords_en': STOPWORDS_EN,
    }


def mode_of(config, rule):
    return (config or default_config())['modes'].get(rule, 'shadow')


# ------------------------------------------------------------------- shapes

class EventEnvelope:
    """One authorized-group message, split into author text vs metadata.

    Only `text` segments' `data.text` is author text. `nickname`, `card`,
    `title` and every at-segment display name are metadata: they are never
    concatenated into the scanned text, never counted for length and never
    scanned for keywords.
    """
    __slots__ = ('group_id', 'user_id', 'self_id', 'message_id', 'timestamp',
                 'text', 'text_parts', 'pure_text', 'image_count', 'other_media',
                 'has_at', 'has_quote', 'nickname', 'card', 'title',
                 'is_command', 'nickname_spans')

    def __init__(self, *, group_id, user_id, self_id, message_id, timestamp,
                 text='', text_parts=(), pure_text=False, image_count=0,
                 other_media=0, has_at=False, has_quote=False, nickname='',
                 card='', title='', is_command=False, nickname_spans=()):
        self.group_id = group_id
        self.user_id = user_id
        self.self_id = self_id
        self.message_id = message_id
        self.timestamp = timestamp
        self.text = text
        self.text_parts = tuple(text_parts)
        self.pure_text = pure_text
        self.image_count = image_count
        self.other_media = other_media
        self.has_at = has_at
        self.has_quote = has_quote
        self.nickname = nickname or ''
        self.card = card or ''
        self.title = title or ''
        self.is_command = is_command
        self.nickname_spans = tuple(nickname_spans)

    @property
    def has_media(self):
        return bool(self.image_count or self.other_media)

    @property
    def empty(self):
        return not self.text.strip()

    @property
    def author_label(self):
        """Display-only author name for the report (never scanned)."""
        return self.nickname or self.card or ''


class MessageRecord:
    """One already-accepted sender message kept in the bounded ring buffer."""
    __slots__ = ('message_id', 'group_id', 'user_id', 'time', 'text', 'pure_text',
                 'is_command', 'image_count', 'has_at', 'has_quote', 'blocked')

    def __init__(self, *, message_id, group_id, user_id, time, text, pure_text,
                 is_command, image_count, has_at, has_quote, blocked):
        self.message_id = message_id
        self.group_id = group_id
        self.user_id = user_id
        self.time = time
        self.text = text
        self.pure_text = pure_text
        self.is_command = is_command
        self.image_count = image_count
        self.has_at = has_at
        self.has_quote = has_quote
        self.blocked = blocked

    @classmethod
    def from_envelope(cls, env, time, blocked=False):
        return cls(message_id=env.message_id, group_id=env.group_id,
                   user_id=env.user_id, time=time, text=env.text,
                   pure_text=env.pure_text, is_command=env.is_command,
                   image_count=env.image_count, has_at=env.has_at,
                   has_quote=env.has_quote, blocked=blocked)


class GateHistory:
    """Bounded, per-process candidate buffer: last N messages per sender+group,
    at most `max_age` seconds old, plus timestamp-only hit counters.

    Cold start after a restart simply loses candidates -- the design rule is
    "rather miss a detection than produce a false positive".
    """
    def __init__(self, records=(), hits=(), *, limit=20, max_age=180.0):
        self.limit = int(limit)
        self.max_age = float(max_age)
        self.records = list(records)
        self.hits = {}
        for group, user, category, at in hits:
            self.hits.setdefault((group, user, category), []).append(float(at))

    def prune(self, now):
        floor = now - self.max_age
        self.records = [r for r in self.records if floor <= r.time <= now]
        hit_floor = now - max(self.max_age, 600.0)
        for key in list(self.hits):
            self.hits[key] = [t for t in self.hits[key] if hit_floor <= t <= now]
            if not self.hits[key]:
                del self.hits[key]

    def add(self, record, now=None):
        if now is not None:
            self.prune(now)
        if any(item.group_id == record.group_id and item.message_id == record.message_id
               for item in self.records):
            return
        self.records.append(record)
        # Per sender+group ring buffer of at most `limit` messages.
        kept, counts = [], {}
        for item in reversed(self.records):
            key = (item.group_id, item.user_id)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] <= self.limit:
                kept.append(item)
        self.records = list(reversed(kept))

    def add_hit(self, group, user, category, at):
        key = (group, user, category)
        bucket = self.hits.setdefault(key, [])
        bucket.append(float(at))
        bucket.sort()

    def recent(self, group, user, now, window=None):
        """Same sender, same group, inside the window, in time order."""
        span = self.max_age if window is None else float(window)
        low, high = now - span, now
        rows = [r for r in self.records
                if r.group_id == group and r.user_id == user and low <= r.time <= high]
        rows.sort(key=lambda r: (r.time, r.message_id))
        return rows

    def prior_hits(self, group, user, category, now, window):
        return [t for t in self.hits.get((group, user, category), [])
                if now - window <= t <= now]


class GateFinding:
    """One gate's evidence for one message."""
    __slots__ = ('gate', 'category', 'mode', 'confidence', 'evidence',
                 'suggested_points', 'suggested_ban_seconds', 'flags', 'primary')

    def __init__(self, *, gate, category, mode, confidence, evidence,
                 suggested_points, suggested_ban_seconds=0, flags=(), primary=False):
        self.gate = gate
        self.category = category
        self.mode = mode
        self.confidence = confidence
        self.evidence = dict(evidence)
        self.suggested_points = int(suggested_points)
        self.suggested_ban_seconds = int(suggested_ban_seconds)
        self.flags = list(flags)
        self.primary = bool(primary)

    @property
    def label(self):
        return f'{self.gate}{self.category}' if self.category.startswith(self.gate) \
            else self.category

    def to_dict(self):
        return {'gate': self.gate, 'category': self.category, 'mode': self.mode,
                'confidence': self.confidence, 'evidence': self.evidence,
                'suggested_points': self.suggested_points,
                'suggested_ban_seconds': self.suggested_ban_seconds,
                'flags': list(self.flags), 'primary': self.primary}


class GateDecision:
    """The deterministic result of one message's pipeline run."""
    __slots__ = ('blocked', 'reason', 'findings', 'case', 'notice', 'silent',
                 'train_allowed', 'reply_allowed', 'ban_applied')

    def __init__(self, *, blocked, reason, findings, case=None, notice=None,
                 silent=True, train_allowed=True, reply_allowed=True,
                 ban_applied=False):
        self.blocked = bool(blocked)
        self.reason = reason
        self.findings = list(findings)
        self.case = case
        self.notice = notice          # None == silent; {'template','claim','gate'}
        self.silent = bool(silent)
        self.train_allowed = bool(train_allowed)
        self.reply_allowed = bool(reply_allowed)
        self.ban_applied = bool(ban_applied)

    def finding(self, gate):
        for item in self.findings:
            if item.gate == gate:
                return item
        return None

    @property
    def categories(self):
        return [item.category for item in self.findings]

    @property
    def mode(self):
        return 'enforce' if any(item.mode == 'enforce' for item in self.findings) \
            else 'shadow'


# ------------------------------------------------------------- normalization

def _is_cjk(char):
    return any(low <= char <= high for low, high in CJK_RANGES)


def _nickname_spans(text, *names):
    """Positions of the sender's own nickname inside the author text.

    The text is never trimmed by similarity: a user can type somebody else's
    name. The spans only let a match that falls *inside* the sender's own
    nickname be skipped, and they force the "insufficient evidence" flag.
    """
    spans = []
    for name in dict.fromkeys(n for n in names if isinstance(n, str) and len(n.strip()) >= 2):
        needle = name.strip()
        start = text.find(needle)
        while start >= 0:
            spans.append((start, start + len(needle)))
            start = text.find(needle, start + 1)
    return tuple(sorted(spans))


def normalize_event(event, *, group_id, self_id, commands=None):
    """Build an EventEnvelope from a OneBot-shaped dict, or None if unusable.

    `raw_message` is deliberately ignored: it contains CQ codes and must never
    be used for detection. Only `message[].data.text` is read, verbatim.
    """
    if not isinstance(event, dict):
        return None
    segments = event.get('message')
    if not isinstance(segments, list) or not segments:
        return None
    parts, images, others, at_seen, quote_seen = [], 0, 0, False, False
    pure_text = True
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        kind = segment.get('type')
        if not isinstance(kind, str):
            return None
        if kind == 'text':
            data = segment.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('text'), str):
                return None
            parts.append(data['text'])
            continue
        if kind == 'at':
            at_seen = True
            continue
        if kind in ('reply', 'quote'):
            quote_seen = True
            pure_text = False
            continue
        pure_text = False
        if kind in IMAGE_TYPES:
            images += 1
        else:
            others += 1

    text = ''.join(parts)
    sender = event.get('sender')
    if not isinstance(sender, dict):
        sender = {}
    nickname = sender.get('nickname') if isinstance(sender.get('nickname'), str) else ''
    card = sender.get('card') if isinstance(sender.get('card'), str) else ''
    title = sender.get('title') if isinstance(sender.get('title'), str) else ''
    if not isinstance(card, str):
        card = ''
    if not isinstance(title, str):
        title = ''

    known = COMMANDS if commands is None else frozenset(commands)
    head = text.strip().split(maxsplit=1)[:1]
    is_command = bool(head) and head[0] in known

    try:
        timestamp = float(event.get('time'))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= timestamp < float('inf'):
        return None
    return EventEnvelope(
        group_id=str(group_id), user_id=str(event.get('user_id')),
        self_id=str(self_id), message_id=str(event.get('message_id')),
        timestamp=timestamp,
        text=text, text_parts=tuple(parts), pure_text=pure_text,
        image_count=images, other_media=others, has_at=at_seen, has_quote=quote_seen,
        nickname=nickname, card=card, title=title, is_command=is_command,
        nickname_spans=_nickname_spans(text, nickname, card))


def record_for(env, *, time, blocked=False):
    return MessageRecord.from_envelope(env, time, blocked=blocked)


# ------------------------------------------------------------------ helpers

def length_bucket(text):
    """01a/b/c/d by Unicode code points; the historical 66 boundary stands."""
    size = len(text)
    if size <= 66:
        return None
    if size <= 100:
        return '01a'
    if size <= 150:
        return '01b'
    if size <= 200:
        return return_bucket_01c() if False else '01c'
    return '01d'


def _clean_urls(text):
    """Text with every URL removed, plus the URL list itself."""
    urls = [match.group(0).rstrip(TRAILING_PUNCT) for match in URL_RE.finditer(text)]
    return URL_RE.sub(' ', text), urls


def _longest_letters(run_text):
    best = 0
    for match in ENGLISH_RUN_RE.finditer(run_text):
        if len(match.group(0)) > best:
            best = len(match.group(0))
    return best


def _visible(text):
    return [ch for ch in text if not ch.isspace()]


def _spans_in(text, needle):
    spans, start = [], text.find(needle)
    while start >= 0:
        spans.append((start, start + len(needle)))
        start = text.find(needle, start + 1)
    return spans


def _inside(spans, position):
    return any(low <= position < high for low, high in spans)


def _covered(spans, start, end):
    return any(low <= start and end <= high for low, high in spans)


def _quoted(text, start, end):
    """True when [start, end) sits inside 「」/“”/() style quotation marks."""
    before, after = text[:start], text[end:]
    pairs = (('「', '」'), ('『', '』'), ('“', '”'), ('‘', '’'),
             ('(', ')'), ('（', '）'), ('【', '】'), ('《', '》'))
    for open_ch, close_ch in pairs:
        if before.rfind(open_ch) > before.rfind(close_ch) and close_ch in after:
            return True
    return False


def _bigrams(text):
    compact = ''.join(ch for ch in text if not ch.isspace())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _jaccard(first, second):
    if not first or not second:
        return 0.0
    union = len(first | second)
    return (len(first & second) / union) if union else 0.0


def _skeleton(text):
    return re.sub(r'[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]+', '_', text)


def _inside_code(text, start, end):
    for match in FENCE_RE.finditer(text):
        if match.start() <= start and end <= match.end():
            return True
    for match in INLINE_CODE_RE.finditer(text):
        if match.start() <= start and end <= match.end():
            return True
    return False


# ------------------------------------------------------------------ gate 01

def _gate_01(env, history, cfg, now):
    findings = []
    category = length_bucket(env.text)
    if category is not None:
        findings.append(GateFinding(
            gate='01', category=category, mode=mode_of(cfg, category),
            confidence='exact-length-v1',
            evidence={'code_points': len(env.text)},
            suggested_points=POINTS_01[category]))
    split = _gate_01s(env, history, cfg, now)
    if split is not None:
        findings.append(split)
    return findings


def _gate_01s(env, history, cfg, now):
    """Split-message suspicion: shadow only, strict evidence, never a penalty."""
    if env.is_command or env.has_media or env.has_at or env.has_quote:
        return None
    candidates = [r for r in history.recent(env.group_id, env.user_id, now, 12.0)
                  if r.pure_text and not r.is_command and not r.blocked
                  and not r.has_at and not r.image_count and not r.has_quote
                  and 5 <= len(r.text) <= 66]
    candidates = [r for r in candidates if r.text.strip()]
    current_ok = (not env.is_command and env.pure_text and not env.has_media
                  and not env.has_at and not env.has_quote
                  and 5 <= len(env.text) <= 66 and bool(env.text.strip()))
    if not current_ok:
        return None
    if candidates and candidates[-1].message_id == env.message_id:
        fragments = candidates[-4:]
    else:
        fragments = (candidates + [_current_record(env, now)])[-4:]
    if len(fragments) < 4:
        return None
    texts = [item.text for item in fragments]
    times = [item.time for item in fragments]
    if len(set(texts)) < 2:
        return None                                   # plain repetition, not a split
    if sum(len(text) for text in texts) <= 100:
        return None
    if times[-1] - times[0] > 12.0:
        return None
    if any(times[index + 1] - times[index] > 2.0 for index in range(len(times) - 1)):
        return None
    if any(index and times[index] < times[index - 1] for index in range(len(times))):
        return None                                   # out-of-order never proves it
    unfinished = sum(1 for text in texts
                     if text.rstrip() and text.rstrip()[-1] not in SENTENCE_END)
    if unfinished < 3:
        return None                                   # normal sentences end their line
    merged = ''.join(texts)
    bucket = length_bucket(merged) or '01a'
    points = min(8, POINTS_01[bucket] * 2)
    return GateFinding(
        gate='01', category='01s', mode=mode_of(cfg, '01s'),
        confidence='strict-split-v1',
        evidence={'fragments': texts,
                  'deltas': [round(times[i + 1] - times[i], 3)
                             for i in range(len(times) - 1)],
                  'merged_length': len(merged), 'merged_bucket': bucket},
        suggested_points=points,
        flags=['suspected_only'])


def _current_record(env, now):
    return MessageRecord.from_envelope(env, now, blocked=False)


# ------------------------------------------------------------------ gate 02

def _gate_02(env, cfg, prior_violations):
    """Sensitive-vocabulary gate. The nickname/at fields are never scanned."""
    if env.is_command:
        return []
    from moderation import GRADE_INDEX, GRADE_SECONDS, GRADES, match as dict_match
    found = dict_match(env.text)
    if found is None:
        return []
    base, term = found
    spans = _spans_in(env.text, term)
    flags = []
    if env.nickname_spans and spans:
        if all(_inside(env.nickname_spans, start) for start, _ in spans):
            return []                       # the hit IS the sender's nickname: skip
        flags.append('insufficient_evidence')
    if not spans:
        match_mode = 'normalized'
        position = None
        flags.append('insufficient_evidence')
    else:
        match_mode = 'literal'
        position = spans[0]
        if _quoted(env.text, *spans[0]) or env.has_at or env.has_quote:
            flags.append('possible_false_positive')
    # The export contains explicit disapproval/discussion of a listed term.
    # This is review evidence, never an exemption from the training gate.
    discussed=False
    for start,end in spans:
        before=env.text[max(0,start-18):start]
        after=env.text[end:end+12]
        if (re.search(r'(?:(?:不喜欢|不支持|反对|禁止|拒绝|抵制)(?:这种|这类|别人说|有人说|说|发|讲|听到|看见)?|不要(?:说|发|讲))[“「]?$',before)
                or re.match(r'(?:是什么意思|的意思是什么|这个词的含义|一词的含义)',after)):
            discussed=True
    if discussed:
        flags.append('explicit_discussion_or_disapproval')
    step = max(0, int(prior_violations))
    escalated = GRADES[min(len(GRADES) - 1, GRADE_INDEX[base] + step)]
    evidence = {
        'term': term,
        'base_grade': base,
        'prior_violations': step,
        'escalated_grade': escalated,
        'existing_seconds': GRADE_SECONDS[escalated],
        'match_mode': match_mode,
        'span': list(spans[0]) if spans else None,
        'position': position,
        'quoted': bool(spans) and _quoted(env.text, *spans[0]),
        'at_present': env.has_at,
        'discussion_context': discussed,
    }
    return [GateFinding(
        gate='02', category='02' + base, mode=mode_of(cfg, '02'),
        confidence='dictionary-term-v1', evidence=evidence,
        suggested_points=0 if discussed else POINTS_02[base],
        suggested_ban_seconds=0 if discussed else BAN_02[base],
        flags=flags)]


# ------------------------------------------------------------------ gate 03

def _gate_03(env, cfg, now, history):
    if env.is_command or not env.text:
        return []
    findings = []
    stripped, urls = _clean_urls(env.text)
    visible = _visible(stripped)
    letters = _longest_letters(stripped)
    ratio = (sum(1 for ch in stripped if ch.isascii() and ch.isalpha()) / len(visible)
             if visible else 0.0)
    if letters >= 40 and ratio >= 0.70:
        findings.append(GateFinding(
            gate='03', category='03a', mode=mode_of(cfg, '03a'),
            confidence='url-parse-v1',
            evidence={'longest_run': letters, 'letter_ratio': round(ratio, 4),
                      'visible_chars': len(visible)},
            suggested_points=POINTS_03['03a'],
            flags=['possible_false_positive']))
    for url in urls:
        if len(url) >= 60:
            findings.append(GateFinding(
                gate='03', category='03b', mode=mode_of(cfg, '03b'),
                confidence='url-parse-v1',
                evidence={'url_length': len(url), 'url': url[:120]},
                suggested_points=POINTS_03['03b']))
            break
    http_urls = [u for u in urls if u.lower().startswith(('http://', 'https://'))]
    distinct = list(dict.fromkeys(u.rstrip('/').lower() for u in http_urls))
    if len(distinct) >= 3:
        prior = [t for t in history.prior_hits(env.group_id, env.user_id, '03c',
                                               now, 600.0)]
        findings.append(GateFinding(
            gate='03', category='03c', mode=mode_of(cfg, '03c'),
            confidence='url-parse-v1',
            evidence={'urls': distinct[:8], 'distinct': len(distinct),
                      'prior_10min': len(prior)},
            suggested_points=POINTS_03['03c']))
    return findings


# ------------------------------------------------------------------ gate 04

def _repeat_run(text):
    """Shortest segment repeating >=4 times inside one message, or None."""
    size = len(text)
    for width in range(1, min(6, size // 4) + 1):
        index = 0
        while index + 4 * width <= size:
            segment = text[index:index + width]
            repeats = 1
            while (index + (repeats + 1) * width <= size
                    and text[index + repeats * width:index + (repeats + 1) * width] == segment):
                repeats += 1
            if repeats >= 4:
                return segment, repeats, index
            index += 1
    return None


def _gate_04(env, cfg):
    if env.is_command or not env.text:
        return []
    text = env.text
    size = len(text)
    index = 0
    while index + 4 <= size:
        run = _repeat_run(text[index:])
        if run is None:
            return []
        segment, repeats, offset = run
        start, end = index + offset, index + offset + len(segment) * repeats
        index = end
        if _inside_code(text, start, end):
            continue
        if any(_covered([span], start, end) for span in env.nickname_spans):
            continue
        url_spans = [m.span() for m in URL_RE.finditer(text)]
        if any(_covered([span], start, end) for span in url_spans):
            continue
        if not any(_is_cjk(ch) or ch.isalpha() for ch in segment):
            continue                      # digits / symbol sequences are not repetition
        if len(segment) > 6:
            continue
        if repeats >= 6 or len(segment) >= 3:
            category = '04b'
        elif repeats >= 4 and len(segment) <= 2:
            category = '04a'
        else:
            continue
        expressive=set(segment)<=EXPRESSIVE_REPEAT
        quoted=_quoted(text,start,end)
        flags=['possible_false_positive']
        if expressive:
            flags.append('expressive_repetition')
        if quoted:
            flags.append('quoted_repetition')
        return [GateFinding(
            gate='04', category=category, mode=mode_of(cfg, category),
            confidence='exact-repeat-v1',
            evidence={'segment': segment, 'repeats': repeats, 'span': [start, end],
                      'run_chars': len(segment) * repeats},
            suggested_points=0 if expressive or quoted else POINTS_04[category],
            flags=flags)]
    return []


# ------------------------------------------------------------------ gate 05

def _keyword(texts, cfg):
    """A shared keyword of >=3 hanzi or >=5 letters; None means give up."""
    stopwords_zh = cfg['stopwords_zh']
    stopwords_en = cfg['stopwords_en']
    base = texts[0][:300]
    for width in range(8, 2, -1):
        found = None
        for index in range(0, len(base) - width + 1):
            gram = base[index:index + width]
            if not all(_is_cjk(ch) for ch in gram) or gram in stopwords_zh:
                continue
            if all(gram in other for other in texts[1:]):
                found = gram
                break
        if found:
            return found
    best = None
    for token in ENGLISH_TOKEN_RE.finditer(base):
        word = token.group(0).strip("'").lower()
        if len([ch for ch in word if ch.isalpha()]) < 5 or word in stopwords_en:
            continue
        if all(word in other.lower() for other in texts[1:]):
            if best is None or len(word) > len(best):
                best = word
    return best


def _gate_05(env, history, cfg, now):
    if env.is_command or not env.pure_text or env.has_media or env.empty:
        return []
    rows = [r for r in history.recent(env.group_id, env.user_id, now, 180.0)
            if r.pure_text and not r.is_command and not r.blocked
            and not r.image_count and r.text.strip()]
    rows = rows[-4:] + [_current_record(env, now)]
    if len(rows) < 5:
        return []
    texts = [row.text for row in rows]
    times = [row.time for row in rows]
    if times[-1] - times[0] > 180.0 or any(times[i] < times[i - 1]
                                            for i in range(1, len(times))):
        return []
    keyword = _keyword(texts, cfg)
    if not keyword:
        return []                                  # no credible extraction: give up
    if not all(keyword in text.lower() for text in texts):
        return []
    grams = [_bigrams(text) for text in texts]
    edges = [[1.0 if _jaccard(grams[i], grams[j]) >= 0.7 else 0.0
              for j in range(5)] for i in range(5)]
    # Largest near-duplicate cluster (connected component over the >=0.7 edges).
    seen, clusters = set(), []
    for seed in range(5):
        if seed in seen:
            continue
        stack, component = [seed], []
        seen.add(seed)
        while stack:
            node = stack.pop()
            component.append(node)
            for other in range(5):
                if other not in seen and edges[node][other]:
                    seen.add(other)
                    stack.append(other)
        clusters.append(component)
    biggest = max(len(c) for c in clusters)
    category = '05b' if biggest >= 5 else ('05a' if biggest >= 3 else None)
    if category is None:
        return []
    positions = {str(index): [list(span) for span in _spans_in(text, keyword)]
                 for index, text in enumerate(texts)}
    return [GateFinding(
        gate='05', category=category, mode=mode_of(cfg, category),
        confidence='keyword-5of5-v1',
        evidence={'keyword': keyword, 'positions': positions,
                  'cluster_size': biggest,
                  'jaccard': [[round(_jaccard(grams[i], grams[j]), 3)
                               for j in range(5)] for i in range(5)],
                  'deltas': [round(times[i + 1] - times[i], 3)
                             for i in range(len(times) - 1)]},
        suggested_points=POINTS_05[category],
        flags=['suspected_only'])]


# ------------------------------------------------------------------ gate 06

def _gate_06(env, history, cfg, now):
    """Strict combined evidence only: never a lone `{` or a lone character."""
    if env.is_command or not env.pure_text or env.has_media or env.empty:
        return []
    rows = [r for r in history.recent(env.group_id, env.user_id, now, 60.0)
            if r.pure_text and not r.is_command and not r.blocked and r.text.strip()]
    text = env.text
    findings = []

    # 06a: >=3 identical training sentences, then their 1-2 char prefix alone.
    if 1 <= len(text) <= 2:
        by_text = {}
        for row in rows:
            by_text.setdefault(row.text, []).append(row)
        for sentence, group in sorted(by_text.items(), key=lambda item: item[0]):
            if len(sentence) < 4 or len(group) < 3:
                continue
            if not sentence.startswith(text):
                continue
            chosen = group[-3:]
            times = [row.time for row in chosen] + [now]
            if max(times) - min(times) > 60.0:
                continue
            findings.append(GateFinding(
                gate='06', category='06a', mode=mode_of(cfg, '06a'),
                confidence='strict-composition-v1',
                evidence={'template': sentence, 'repeats': len(group),
                          'message_ids': [row.message_id for row in chosen],
                          'deltas': [round(now - row.time, 3) for row in chosen],
                          'prefix': text, 'prefix_match': True},
                suggested_points=POINTS_06['06a'],
                suggested_ban_seconds=BAN_06['06a'],
                flags=['suspected_only']))
            break

    # 06b: >=3 similar symbol templates, then that symbol sent on its own.
    if len(text) == 1 and text in ASCII_SPECIAL:
        matched = [row for row in rows
                   if len(row.text) >= 2 and row.text.startswith(text)
                   and any(ch in ASCII_SPECIAL for ch in row.text)
                   and _skeleton(row.text) != '_']
        if not matched:
            return findings
        distinct_skeleton = _skeleton(matched[-1].text)
        candidates = [row for row in matched if _skeleton(row.text) == distinct_skeleton]
        if len(candidates) >= 3:
            chosen = candidates[-3:]
            times = [row.time for row in chosen] + [now]
            if max(times) - min(times) <= 60.0:
                findings.append(GateFinding(
                    gate='06', category='06b', mode=mode_of(cfg, '06b'),
                    confidence='strict-composition-v1',
                    evidence={'templates': [row.text for row in chosen],
                              'repeats': len(candidates),
                              'message_ids': [row.message_id for row in chosen],
                              'deltas': [round(now - row.time, 3) for row in chosen],
                              'symbol': text, 'prefix_match': True},
                    suggested_points=POINTS_06['06b'],
                    suggested_ban_seconds=BAN_06['06b'],
                    flags=['suspected_only']))
    return findings


# ------------------------------------------------------------------ gate 07

def _gate_07(env, history, cfg, now):
    findings = []
    window = history.recent(env.group_id, env.user_id, env.timestamp, 30.0)
    window = [row for row in window if row.time <= env.timestamp]
    count = len(window) + 1
    if count >= 8:
        findings.append(GateFinding(
            gate='07', category='07a', mode=mode_of(cfg, '07a'),
            confidence='count-window-v1',
            evidence={'window': 30.0, 'messages': count,
                      'message_ids': [row.message_id for row in window[-7:]]},
            suggested_points=POINTS_07['07a']))
    images = [row for row in history.recent(env.group_id, env.user_id,
                                            env.timestamp, 60.0)
              if row.time <= env.timestamp]
    image_count = sum(row.image_count for row in images) + env.image_count
    if image_count >= 5:
        findings.append(GateFinding(
            gate='07', category='07b', mode=mode_of(cfg, '07b'),
            confidence='count-window-v1',
            evidence={'window': 60.0, 'images': image_count,
                      'message_ids': [row.message_id for row in images if row.image_count]},
            suggested_points=POINTS_07['07b']))
    return findings


# ------------------------------------------------------------- notification

def _priority(finding):
    try:
        return GATE_PRIORITY.index(finding.gate)
    except ValueError:
        return len(GATE_PRIORITY)


def notification(env, findings, *, now, history, cfg, ban_applied=False):
    """Independent severity policy: 'silent' (default) or one fixed hint.

    A hit is never itself a group message. 00 and 07 are always silent, the
    hint text never contains the nickname, the matched word, the original
    message, a score or any detection detail.
    """
    notify = cfg['notify']
    enforce = [item for item in findings if item.mode == 'enforce']
    if not enforce:
        return None                                    # shadow never notifies
    candidates = []
    for item in sorted(enforce, key=_priority):
        if item.gate == '00' or item.gate == '07':
            continue
        rule = notify.get(item.gate) or notify.get(item.category)
        if not rule or not rule.get('enabled'):
            continue
        if item.gate == '01' and item.category == '01s':
            if mode_of(cfg, '01s') != 'enforce':
                continue
            candidates.append(item)
            continue
        if item.gate == '01':
            window, need = rule.get('window', 300.0), rule.get('need', 3)
            hits = history.prior_hits(env.group_id, env.user_id,
                                      rule.get('category', '01d'), now, window)
            total = len(hits) + (1 if item.category == rule.get('category', '01d') else 0)
            if item.category == rule.get('category', '01d') and total >= need:
                candidates.append(item)
            continue
        if item.gate == '02':
            if ban_applied:
                continue                               # a fresh ban overrides hints
            grades = ('Ia', 'Ib', 'Ic', 'IIa', 'IIb', 'IIc', 'IIIa', 'IIIb', 'IIIc',
                      'IVa', 'IVb', 'Va', 'Vb')
            if grades.index(item.category[2:]) < grades.index(rule.get('min_grade', 'IVa')):
                continue
            if 'possible_false_positive' in item.flags or 'insufficient_evidence' in item.flags:
                continue
            candidates.append(item)
            continue
        if item.gate == '03':
            if item.category != rule.get('category', '03c'):
                continue                               # 03a/03b always silent
            hits = history.prior_hits(env.group_id, env.user_id, '03c',
                                      now, rule.get('window', 600.0))
            if len(hits) + 1 >= rule.get('need', 2):
                candidates.append(item)
            continue
        if item.gate == '04':
            if 'expressive_repetition' in item.flags or 'quoted_repetition' in item.flags:
                continue
            if item.category != '04b':
                continue
            if (item.evidence.get('repeats', 0) >= rule.get('min_repeats', 10)
                    and item.evidence.get('run_chars', 0) >= rule.get('min_run_chars', 30)):
                candidates.append(item)
            continue
        if item.gate in ('05', '06'):
            continue                                   # never before calibration
    if not candidates:
        return None
    chosen = candidates[0]
    template = NOTICE_TEMPLATES.get(chosen.gate)
    if not template:
        return None
    if chosen.gate == '01' and chosen.category != '01s':
        claim = 'md5:' + hashlib.md5(env.text.encode('utf-8')).hexdigest()
    else:
        claim = (f'cooldown:{env.group_id}:{env.user_id}:'
                 f'{chosen.gate}:{chosen.category}')
    return {'gate': chosen.gate, 'category': chosen.category,
            'template': template, 'claim': claim,
            'cooldown': 0.0 if claim.startswith('md5:') else COOLDOWN_SECONDS}


# ---------------------------------------------------------------- evaluate

def evaluate(env, *, now, history=None, config=None, banned=False,
             screening_exempt=False, prior_violations=0, ban_applied=False):
    """Deterministic pipeline for one normalized event.

    `banned` (gate 00) wins over everything: no findings, no case, no output.
    Gates 01-07 are collected in parallel, then one single case is built.
    """
    cfg = config or default_config()
    history = history if history is not None else GateHistory()
    if banned:
        return GateDecision(blocked=True, reason='00', findings=[],
                            case=None, notice=None, silent=True,
                            train_allowed=False, reply_allowed=False)

    findings = []
    if env.is_command:
        findings += _gate_07(env, history, cfg, now)   # only frequency may limit commands
    else:
        findings += _gate_01(env, history, cfg, now)
        if not screening_exempt:
            findings += _gate_02(env, cfg, prior_violations)
        findings += _gate_03(env, cfg, now, history)
        findings += _gate_04(env, cfg)
        findings += _gate_05(env, history, cfg, now)
        findings += _gate_06(env, history, cfg, now)
        findings += _gate_07(env, history, cfg, now)

    findings.sort(key=lambda item: (_priority(item), item.category))
    if findings:
        findings[0].primary = True

    enforce = [item for item in findings if item.mode == 'enforce']
    blocked = bool(enforce) or bool(ban_applied)
    notice = notification(env, findings, now=now, history=history, cfg=cfg,
                          ban_applied=ban_applied)
    case = build_case(env, findings, now=now, config=cfg) if findings else None
    if ban_applied:
        reason = 'policy-ban' if not enforce else 'gate+policy-ban'
    elif enforce:
        reason = 'gate'
    elif findings:
        reason = 'shadow'
    else:
        reason = 'command' if env.is_command else 'clean'
    return GateDecision(
        blocked=blocked, reason=reason, findings=findings, case=case,
        notice=notice, silent=notice is None,
        train_allowed=not blocked, reply_allowed=not blocked,
        ban_applied=ban_applied)


# -------------------------------------------------------------- case build

def report_id_for(group_id, message_id):
    raw = f'gates-v2|{group_id}|{message_id}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]


def build_case(env, findings, *, now, config=None):
    """One message -> at most one case holding every finding (deduplicated)."""
    if not findings:
        return None
    cfg = config or default_config()
    seen, unique = set(), []
    for item in findings:
        key = (item.gate, item.category)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    primary = unique[0]
    uncertain = any('insufficient_evidence' in item.flags for item in unique) \
        or bool(env.nickname_spans)
    text = env.text
    if len(text) > MAX_EVIDENCE_TEXT:
        text = text[:MAX_EVIDENCE_TEXT]
    mode = 'enforce' if any(item.mode == 'enforce' for item in unique) else 'shadow'
    return {
        'report_id': report_id_for(env.group_id, env.message_id),
        'group_id': env.group_id,
        'user_id': env.user_id,
        'message_id': env.message_id,
        'occurred_at': float(env.timestamp),
        'received_at': float(now),
        'primary_gate': primary.gate,
        'primary_category': primary.category,
        'labels': [item.category for item in unique],
        'mode': mode,
        'status': 'pending' if mode=='enforce' else 'shadow',
        'rule_version': cfg['version'],
        'proposal': cfg['proposal'],
        'text': text,
        'nickname': env.author_label,
        'uncertain': bool(uncertain),
        'suggested_points': primary.suggested_points,
        'suggested_ban_seconds': primary.suggested_ban_seconds,
        'findings': [item.to_dict() for item in unique],
    }
