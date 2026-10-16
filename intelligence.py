"""Small, online, retrieval-conditioned character model. No external dependencies."""
from collections import Counter, defaultdict
import math
import re
import unicodedata

MAX_TEXT = 512
MAX_FEATURES = 96
MAX_MEMORIES = 5000
MAX_LINKS = 20000
ORDER = 4
STOP_CHARS = set('了的呢啊呀吗吧哦嗯是在我你他她它这那也都就和与')
# Deliberately limited lexical hints, not a general sentiment/semantic classifier.
STATES = {
    '没有坏':1, '没坏':1, '不坏':1, '不好':-1, '不正常':-1,
    '没成功':-1, '不成功':-1, '未解决':-1, '没解决':-1,
    '不开心':-1, '不难过':1, '没失败':1, '不失败':1,
    '恢复':1, '正常':1, '成功':1, '开心':1, '解决':1, '修好':1,
    '故障':-1, '异常':-1, '失败':-1, '难过':-1, '报错':-1, '崩溃':-1,
    '好':1, '坏':-1,
}
STATE_RE = re.compile('|'.join(re.escape(s) for s in sorted(STATES,key=len,reverse=True)))


def normalize(text):
    return ''.join(ch for ch in unicodedata.normalize('NFKC',text).lower() if ch.isalnum())


def polarity(text):
    score = sum(STATES[m.group()] for m in STATE_RE.finditer(normalize(text)))
    return (score > 0) - (score < 0)


def features(text):
    text = normalize(text[:MAX_TEXT])
    values = Counter()
    for width,weight in ((1,.25),(2,1.0),(3,1.25)):
        counts = Counter(text[i:i+width] for i in range(len(text)-width+1))
        for token,count in counts.items():
            if width == 1 and token in STOP_CHARS:
                continue
            values[token] = weight * (1 + math.log(count))
    sign = polarity(text)
    if sign:
        values[f'@state:{sign}'] = 2.0
    return dict(values.most_common(MAX_FEATURES))


def similarity(left, right):
    if not left or not right:
        return 0.0
    dot = sum(w*right.get(term,0) for term,w in left.items())
    cosine = dot / math.sqrt(sum(w*w for w in left.values())*sum(w*w for w in right.values()))
    coverage = sum(w for term,w in left.items() if term in right)/sum(left.values())
    return .65*cosine + .35*coverage


def build_graph(samples):
    graph = defaultdict(Counter)
    for text,weight in samples:
        if not text:
            continue
        graph[''][text[0]] += weight
        for pos in range(1,len(text)+1):
            following = text[pos] if pos < len(text) else ''  # Explicit end-of-message.
            for width in range(1,min(ORDER,pos)+1):
                graph[text[pos-width:pos]][following] += weight
    return graph


def draw(rows,rng):
    ticket = rng.random()*sum(rows.values())
    for value,weight in sorted(rows.items()):
        ticket -= weight
        if ticket < 0:
            return value
    return next(reversed(rows))


def walk(graph,rng,seed=None):
    history = seed[-ORDER:] if seed else ''
    result = ''
    target = rng.randint(3,50)
    ended = False
    while len(result) < target:
        rows = None
        if not history:
            rows = graph.get('')
        else:
            # A continuation must have >=2 chars of input context initially.
            # Never fall back from 好了 / 坏了 directly to their shared 了.
            minimum = 2 if seed is not None and not result else 1
            for width in range(min(ORDER,len(history)),minimum-1,-1):
                rows = graph.get(history[-width:])
                if rows:
                    break
        if not rows:
            break
        following = draw(rows,rng)
        if not following:
            ended = True
            break
        result += following
        history = (history+following)[-ORDER:]
    return (result,ended) if len(result)>=3 else (None,False)


class Intelligence:
    def __init__(self,db,rng):
        self.db,self.rng = db,rng
        self.previous = None
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS intel_memory (
                id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE,
                w INTEGER NOT NULL CHECK(w>0), last_seen REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS intel_recent ON intel_memory(last_seen);
            CREATE TABLE IF NOT EXISTS intel_terms (
                term TEXT NOT NULL, memory_id INTEGER NOT NULL REFERENCES intel_memory(id) ON DELETE CASCADE,
                weight REAL NOT NULL, PRIMARY KEY(term,memory_id));
            CREATE INDEX IF NOT EXISTS intel_terms_memory ON intel_terms(memory_id);
            CREATE TABLE IF NOT EXISTS intel_links (
                qid INTEGER NOT NULL REFERENCES intel_memory(id) ON DELETE CASCADE,
                aid INTEGER NOT NULL REFERENCES intel_memory(id) ON DELETE CASCADE,
                w INTEGER NOT NULL CHECK(w>0), last_seen REAL NOT NULL, PRIMARY KEY(qid,aid));
            CREATE INDEX IF NOT EXISTS intel_links_answer ON intel_links(aid);
            CREATE INDEX IF NOT EXISTS intel_links_recent ON intel_links(last_seen);
        ''')

    def reset_context(self):
        self.previous = None

    def clear(self):
        self.db.execute('DELETE FROM intel_memory')  # Cascades to links and postings.
        self.reset_context()

    def observe(self,text,timestamp):
        # Bound per-message work without altering match/generate's larger limit.
        if len(text)>MAX_TEXT:
            self.reset_context()
            return
        row = self.db.execute('SELECT id FROM intel_memory WHERE text=?',(text,)).fetchone()
        if row:
            mid = row[0]
            self.db.execute('UPDATE intel_memory SET w=w+1,last_seen=? WHERE id=?',(timestamp,mid))
        else:
            mid = self.db.execute('INSERT INTO intel_memory(text,w,last_seen) VALUES (?,1,?)',
                                  (text,timestamp)).lastrowid
            self.db.executemany('INSERT INTO intel_terms VALUES (?,?,?)',
                                ((term,mid,w) for term,w in features(text).items()))
        if self.previous:
            previous_text,previous_time = self.previous
            if 0 <= timestamp-previous_time <= 180:
                q = self.db.execute('SELECT id FROM intel_memory WHERE text=?',(previous_text,)).fetchone()
                if q:
                    self.db.execute('''INSERT INTO intel_links VALUES (?,?,1,?)
                        ON CONFLICT(qid,aid) DO UPDATE SET w=w+1,last_seen=excluded.last_seen''',
                        (q[0],mid,timestamp))
        self.previous = (text,timestamp)
        count = self.db.execute('SELECT COUNT(*) FROM intel_memory').fetchone()[0]
        if count>MAX_MEMORIES:
            self.db.execute('DELETE FROM intel_memory WHERE id IN (SELECT id FROM intel_memory ORDER BY last_seen,id LIMIT ?)',
                            (count-MAX_MEMORIES,))
        count = self.db.execute('SELECT COUNT(*) FROM intel_links').fetchone()[0]
        if count>MAX_LINKS:
            self.db.execute('DELETE FROM intel_links WHERE rowid IN (SELECT rowid FROM intel_links ORDER BY last_seen,rowid LIMIT ?)',
                            (count-MAX_LINKS,))

    def retrieve(self,query,*,linked):
        query_features = features(query)
        if not query_features:
            return []
        if linked:
            exact = self.db.execute('''SELECT id,text,w FROM intel_memory m WHERE text=?
                AND EXISTS (SELECT 1 FROM intel_links l WHERE l.qid=m.id)''',(query,)).fetchone()
            if exact:
                return [(1.0,*exact)]
        # Probe rare lexical features first; a frequent state label must not cause
        # a scan of every positive/negative sentence. Bound postings per feature.
        terms = [term for term in query_features if not term.startswith('@')]
        if not terms:
            return []
        placeholders = ','.join('?' for _ in terms)
        rare = self.db.execute(f'''SELECT term,COUNT(*) AS frequency FROM intel_terms
            WHERE term IN ({placeholders}) GROUP BY term ORDER BY frequency,term LIMIT 8''',terms).fetchall()
        overlap = Counter()
        link_filter = 'AND EXISTS (SELECT 1 FROM intel_links l WHERE l.qid=t.memory_id)' if linked else ''
        for term,_ in rare:
            for mid,weight in self.db.execute(f'''SELECT memory_id,weight FROM intel_terms t
                WHERE term=? {link_filter} ORDER BY memory_id DESC LIMIT 256''',(term,)):
                overlap[mid] += weight*query_features[term]
        rows = overlap.most_common(40)
        ranked = []
        sign = polarity(query)
        for mid,_ in rows:
            text,count = self.db.execute('SELECT text,w FROM intel_memory WHERE id=?',(mid,)).fetchone()
            if sign and polarity(text)==-sign:
                continue
            other = features(text)
            # Shared filler characters alone are not a context match.
            if len(normalize(query))>1 and not any(len(t)>=2 and not t.startswith('@') and t in other for t in query_features):
                continue
            score = 1.0 if normalize(query)==normalize(text) else similarity(query_features,other)
            if score>=.33:
                ranked.append((score,mid,text,count))
        return sorted(ranked,reverse=True)[:12]

    def reply(self,query):
        if not query or len(query)>MAX_TEXT:
            return None
        answers = Counter()
        contexts = self.retrieve(query,linked=True)
        # Exact known contexts should dominate merely related prompts.
        if contexts:
            threshold = max(.33,contexts[0][0]*.8)
            for score,mid,_,_ in contexts:
                if score < threshold:
                    continue
                for text,count in self.db.execute('''SELECT m.text,l.w FROM intel_links l
                    JOIN intel_memory m ON m.id=l.aid WHERE l.qid=? ORDER BY l.w DESC,l.aid LIMIT 12''',(mid,)):
                    if 3 <= len(text) <= MAX_TEXT:
                        answers[text] += score**3 * math.sqrt(count)
        samples = answers.most_common(24)
        seed = None
        if not samples:
            # No known answers: try grounded continuation of a multi-character suffix.
            samples = [(text,score**3*math.sqrt(count)) for score,_,text,count in self.retrieve(query,linked=False)]
            seed = query.rstrip()
        if not samples:
            return None
        graph = build_graph(samples)
        candidates = {}
        sample_features = [(features(text),weight) for text,weight in samples]
        for _ in range(8):
            text,ended = walk(graph,self.rng,seed)
            if text is None or normalize(text)==normalize(query):
                continue
            # Reject obvious sampling loops; valid prose is still probabilistic.
            if re.search(r'(.{1,4})\1\1',text):
                continue
            value = features(text)
            score = max(similarity(value,other)*math.log1p(w) for other,w in sample_features)
            score += .1*ended
            candidates[text] = max(candidates.get(text,-1),score)
        return max(candidates,key=candidates.get) if candidates else None
