"""Graded screening for sexual vocabulary.

Design rules:

* Only multi-character terms (plus the two unambiguously vulgar characters 屄
  and 肏) are matched. Generalized characters that carry ordinary meanings --
  草, 吊, 逼, 屌 -- are never matched on their own, because 草稿/操场/草莓,
  吊车/吊销, 牛逼/逼近 and 屌丝 are everyday words.
* Five levels, each split into sub-grades, thirteen steps in total. The ladder
  runs 1 minute -> 24 hours in roughly doubling steps, so escalation is gradual
  instead of jumping straight to a day.
* Repeat offenders are escalated one step per prior violation inside
  ESCALATION_WINDOW.
* Formal, academic and legal wording (色情, 淫秽, 性欲 ...) sits at level I on
  purpose: it can turn up in ordinary class discussion, so a slip costs a
  minute or two rather than a day.
"""
import re
import unicodedata

# Ordered lightest to heaviest. Every level carries sub-grades.
GRADES = ('Ia', 'Ib', 'Ic',
          'IIa', 'IIb', 'IIc',
          'IIIa', 'IIIb', 'IIIc',
          'IVa', 'IVb',
          'Va', 'Vb')
GRADE_INDEX = {grade: index for index, grade in enumerate(GRADES)}
GRADE_LABELS = {
    'Ia': '轻微', 'Ib': '轻微', 'Ic': '轻微',
    'IIa': '较轻', 'IIb': '较轻', 'IIc': '较轻',
    'IIIa': '中等', 'IIIb': '中等', 'IIIc': '中等',
    'IVa': '严重', 'IVb': '严重',
    'Va': '极严重', 'Vb': '极严重',
}
# 1 / 3 / 5 min · 10 / 20 / 30 min · 1 / 2 / 3 h · 6 / 12 h · 18 / 24 h
GRADE_SECONDS = {
    'Ia': 60, 'Ib': 180, 'Ic': 300,
    'IIa': 600, 'IIb': 1200, 'IIc': 1800,
    'IIIa': 3600, 'IIIb': 7200, 'IIIc': 10800,
    'IVa': 21600, 'IVb': 43200,
    'Va': 64800, 'Vb': 86400,
}
MIN_AUTO_BAN = GRADE_SECONDS[GRADES[0]]
MAX_AUTO_BAN = GRADE_SECONDS[GRADES[-1]]
ESCALATION_WINDOW = 30 * 86400

DICTIONARY = {
    # I -- 口头失当：课堂、法律或正式文本里也可能出现，误伤成本压到最低。
    '色情': 'Ia', '淫秽': 'Ia', '情色': 'Ia', '情欲': 'Ia', '性欲': 'Ia',
    '性幻想': 'Ia', '荤段子': 'Ia', '开黄腔': 'Ia', '黄色笑话': 'Ia', '黄腔': 'Ia',
    '咸湿': 'Ib', '色鬼': 'Ib', '骚味': 'Ib', '骚气': 'Ib', '裸照': 'Ib',
    '发骚': 'Ic', '裸聊': 'Ic', '黄片': 'Ic',
    # II -- 低俗用语：明确的性相关侮辱或骚扰措辞。
    '骚货': 'IIa', '骚婊': 'IIa', '婊子': 'IIa',
    '妓女': 'IIb', '卖淫': 'IIb', '嫖客': 'IIb',
    '嫖娼': 'IIc', '一夜情': 'IIc', '炮友': 'IIc', '约炮': 'IIc',
    # III -- 明显色情：露骨的性行为或性器官描述。
    '自慰': 'IIIa', '手淫': 'IIIa', '淫乱': 'IIIa',
    '淫荡': 'IIIb', '淫叫': 'IIIb', '淫水': 'IIIb',
    '打飞机': 'IIIc', '撸管': 'IIIc', '淫穴': 'IIIc', '肉棒': 'IIIc',
    # IV -- 露骨色情：粗俗的性器官与性行为俗称。
    '精液': 'IVa', '射精': 'IVa', '龟头': 'IVa', '阴蒂': 'IVa',
    '舔阴': 'IVa', '阴道': 'IVa', '阴茎': 'IVa',
    '几把': 'IVb', '鸡巴': 'IVb', '鸡芭': 'IVb', '鸡吧': 'IVb', '屄': 'IVb',
    '骚逼': 'IVb', '骚屄': 'IVb', '操逼': 'IVb', '舔逼': 'IVb', '舔屌': 'IVb',
    # V -- 极端内容：最露骨，或涉及性暴力的表述。
    '肏': 'Va', '口交': 'Va', '肛交': 'Va', '做爱': 'Va', '性交': 'Va',
    '强奸': 'Vb', '轮奸': 'Vb', '鸡奸': 'Vb', '兽交': 'Vb', '恋童': 'Vb',
}

# Innocent compounds are blanked before matching, longest first. 把 is also a
# measure word, so "好几把椅子" must never fire on 几把.
SAFE = (
    '好几把', '十几把', '几十把', '几百把', '上千把', '一万把',
    '几把剪刀', '几把钥匙',
    '几把刀', '几把枪', '几把椅', '几把伞', '几把刷', '几把勺', '几把剪',
    '几把尺', '几把扇', '几把锁', '几把筷', '几把锄', '几把锯', '几把锤',
    '屌丝', '屌爆', '牛逼', '傻逼', '装逼', '逼近', '逼真', '逼供', '逼问',
    '吊儿郎当', '吊车', '吊销', '吊坠', '吊带', '吊顶', '吊桥', '吊唁',
    '吊装', '吊兰', '吊灯', '吊床', '吊环', '吊牌', '吊针', '吊瓶',
)
_ORDERED_SAFE = tuple(sorted(SAFE, key=len, reverse=True))

SEPARATORS = re.compile(r"[\s\u3000\-_*~·|/\\.,，。、!！?？;；:：'\"“”‘’()（）\[\]【】]+")


def normalize(text):
    """Fold width/case and drop separators so spaced-out spellings still match."""
    return SEPARATORS.sub('', unicodedata.normalize('NFKC', text).lower())


def _blank_safe(folded):
    for word in _ORDERED_SAFE:
        if word in folded:
            folded = folded.replace(word, '\u3000')
    return folded


def match(text):
    """Return (base_grade, term) for the heaviest match in `text`, else None."""
    if not isinstance(text, str) or not text:
        return None
    folded = _blank_safe(normalize(text))
    best = None
    for term, grade in DICTIONARY.items():
        if term in folded and (best is None or GRADE_INDEX[grade] > GRADE_INDEX[best[0]]):
            best = (grade, term)
    return best


def escalate(grade, prior_violations=0):
    """Raise a base grade one step per prior violation, capped at Vb."""
    try:
        step = max(0, int(prior_violations))
    except (TypeError, ValueError):
        step = 0
    return GRADES[min(len(GRADES) - 1, GRADE_INDEX[grade] + step)]


def classify(text, prior_violations=0):
    """Return (grade, term, seconds) for the penalty `text` earns, else None."""
    found = match(text)
    if found is None:
        return None
    base, term = found
    grade = escalate(base, prior_violations)
    return grade, term, GRADE_SECONDS[grade]


def level_of(grade):
    """The roman level ('I'..'V') a sub-grade belongs to."""
    return grade.rstrip('abc')


def grade_reason(grade, term):
    """The reason string used in the ban notice."""
    return f'色情词汇「{term}」（{grade}级·{GRADE_LABELS[grade]}）'


def scan(text):
    """Compatibility helper: (seconds, label, term) using the base grade."""
    found = match(text)
    if found is None:
        return None
    grade, term = found
    return GRADE_SECONDS[grade], GRADE_LABELS[grade], term


def human_duration(seconds):
    """Render a ban length the way the group notice shows it."""
    if seconds >= 86400 and seconds % 86400 == 0 and seconds != 86400:
        return f'{seconds // 86400} 天'
    if seconds >= 3600 and seconds % 3600 == 0:
        return f'{seconds // 3600} 小时'
    if seconds >= 60 and seconds % 60 == 0:
        return f'{seconds // 60} 分钟'
    return f'{seconds} 秒'


def grade_table():
    """Documentation rows: (grade, label, seconds, example terms)."""
    return [(grade, GRADE_LABELS[grade], GRADE_SECONDS[grade],
             [term for term, value in DICTIONARY.items() if value == grade])
            for grade in GRADES]
