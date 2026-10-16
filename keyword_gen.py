"""Offline prototype: keyword-seeded two-way generation (MegaHAL-style reply).

The live generate model starts from the last character of the incoming
message, so its output reads as the rest of the user's sentence. This module
instead picks an informative keyword from the message, grows a sentence in
both directions from it -- forward to a message end, backward to a message
start -- and keeps the candidate whose keywords carry the most information.

Nothing here is wired into live.py or qbot.handle. The module never sends
anything, never opens a network connection and never writes the production
database: `compare` and `reply` work on a SQLite backup snapshot, or on a model
trained from a text export into a fresh database in the work directory.

    python -B -X utf8 keyword_gen.py compare --db data\\production.sqlite3 --group 1050045834
    python -B -X utf8 keyword_gen.py compare --export pre\\text\\群\\导出.txt --holdout 0.1
    python -B -X utf8 keyword_gen.py reply --db data\\third.sqlite3 --group 1006808801 "今天考试好难"

The backward model needs no new table: a bigram count is a joint count, so
P(prev | cur) = w(prev, cur) / sum_x w(x, cur) comes straight from `chars`.
Only message boundaries are new, and they are estimated from whole messages
(intel_memory, or the export itself) as self-consistent start/end rates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import statistics
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import datastore
from generation_refine import structural_issue, weighted_pick
from intelligence import STOP_CHARS
from qbot import ALLOWED_GROUPS, TEST_GROUP, Bot, trigram_counts
from repetition import strong_repeat_repair

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'runtime' / 'keyword_gen_work'
MIN_REPLY = 3
MAX_REPLY = 50
ATTEMPTS = 64          # Candidates per reply; MegaHAL uses a time budget instead.
TOP_SEEDS = 6          # Only the most informative input spans may seed a walk.
STOP_TOP = 24          # The most frequent letters in the model never act as keywords.
PRIOR = 4.0            # Pseudo-count pulling sparse boundary rates to the global rate.
TERMINAL = frozenset('。！？!?…～~\n')
# Heuristic boundaries, used only when no whole-message evidence is available:
# a geometric length with this per-step stop chance, plus terminal punctuation.
HEURISTIC_HAZARD = 1 / 12
GROUP_ASSISTANT = '2854196310'
# Round 2: longest character context used by the walks, and the number of
# distinct messages a context needs before it may shift probabilities.
ORDER = 3
MIN_SUPPORT = 2
END, START = '\x03', '\x02'  # Boundary symbols; never emitted, never in chars.


def word_char(character):
    """Letters only: punctuation, digits, spaces and emoji never become keywords."""
    return unicodedata.category(character).startswith('L')


class Bounds:
    """Per-character message start/end rates from one corpus of whole messages.

    For a character c, every occurrence either opens its message or has a
    predecessor inside it, so starts(c)/occurrences(c) is the chance that a
    backward walk standing on c should stop. Ends are symmetric. Both counts
    must come from the SAME messages; mixing them with `chars` totals from a
    different period would bias every rate.
    """
    def __init__(self, texts=()):
        self.starts, self.ends, self.occurrences = Counter(), Counter(), Counter()
        self.lengths = Counter()
        for text, weight in texts:
            if not text:
                continue
            self.lengths[len(text)] += weight
            self.starts[text[0]] += weight
            self.ends[text[-1]] += weight
            for character in text:
                self.occurrences[character] += weight
        total = sum(self.occurrences.values())
        self.messages = sum(self.starts.values())
        self.global_rate = self.messages / total if total else 0.0

    def __bool__(self):
        return self.messages > 0

    def _rate(self, counts, character):
        return ((counts[character] + PRIOR * self.global_rate)
                / (self.occurrences[character] + PRIOR))

    def p_start(self, character):
        return self._rate(self.starts, character)

    def p_end(self, character):
        return self._rate(self.ends, character)

    def draw_length(self, rng):
        """A message length from the same corpus, clipped to the reply limits."""
        rows = sorted(self.lengths.items())
        ticket = rng.random() * sum(w for _, w in rows)
        for length, w in rows:
            ticket -= w
            if ticket < 0:
                return min(MAX_REPLY, max(MIN_REPLY, length))
        return min(MAX_REPLY, max(MIN_REPLY, rows[-1][0]))


class Contexts:
    """2..order character contexts of whole messages, both directions (round 2).

    `forward[h][x]` counts how many DISTINCT messages have x (or END) right
    after the context h; `backward[h][x]` how many have x (or START) right
    before it. Counting messages instead of occurrences keeps one person's
    repeated line from looking like broad evidence. Built from the same whole
    messages as `Bounds`, so the boundary symbols line up with the rates.
    """
    def __init__(self, texts=(), order=ORDER):
        self.order = order
        self.forward, self.backward = defaultdict(Counter), defaultdict(Counter)
        for text in sorted({text for text, _ in texts if text}):
            ahead, behind = set(), set()
            for index in range(len(text) + 1):
                following = text[index] if index < len(text) else END
                preceding = text[index - 1] if index > 0 else START
                for width in range(2, order + 1):
                    if index >= width:
                        ahead.add((text[index - width:index], following))
                    if index + width <= len(text):
                        behind.add((text[index:index + width], preceding))
            for context, value in ahead:
                self.forward[context][value] += 1
            for context, value in behind:
                self.backward[context][value] += 1

    def __bool__(self):
        return bool(self.forward)


@dataclass(frozen=True)
class Candidate:
    text: str
    seed: str
    score: float
    complete: bool = True


class KeywordGenerator:
    """Read-only view of one group's `chars` (and pair bans) held in memory.

    With `contexts`, walks use Witten-Bell interpolation from the longest
    supported 2..order character context down to round 1's `chars` +
    `Bounds` distribution; without it they are round 1, draw for draw.
    """

    def __init__(self, db, *, bounds=None, stop_top=STOP_TOP, attempts=ATTEMPTS,
                 contexts=None, min_support=MIN_SUPPORT):
        self.forward, self.backward = defaultdict(list), defaultdict(list)
        self.out_total, self.in_total = Counter(), Counter()
        self.edges = {}
        for first, second, w in db.execute(
                'SELECT first, second, w FROM chars ORDER BY first, second'):
            self.forward[first].append((second, w))
            self.backward[second].append((first, w))
            self.out_total[first] += w
            self.in_total[second] += w
            self.edges[(first, second)] = w
        self.banned = set()
        if datastore.has_table(db, 'pair_bans'):
            self.banned = {(a, b) for a, b in db.execute('SELECT first, second FROM pair_bans')}
        self.bounds = bounds if bounds is not None else Bounds()
        self.attempts = attempts
        self.contexts = contexts if contexts else None
        self.min_support = min_support
        self._distributions = {}  # The model is read-only, so each context is computed once.
        # Occurrences of c: every in-message use has a predecessor or opens the
        # message; without boundary data the larger side is the best estimate.
        frequency = Counter()
        for character in sorted(set(self.out_total) | set(self.in_total)):
            frequency[character] = max(self.out_total[character], self.in_total[character], 1)
        self.frequency = frequency
        self.total = sum(frequency.values()) or 1
        # Sorted ties: set order changes with PYTHONHASHSEED, the stop list must not.
        letters = [c for c, _ in sorted(frequency.items(), key=lambda item: (-item[1], item[0]))
                   if word_char(c)]
        # At most an eighth of the alphabet, so a small young group model keeps keywords.
        self.stop = set(STOP_CHARS) | set(letters[:min(stop_top, len(letters) // 8)])

    # ---- keywords ----------------------------------------------------------

    def surprise(self, character):
        return -math.log2(self.frequency[character] / self.total)

    def transition_surprise(self, first, second):
        w = self.edges.get((first, second))
        return -math.log2(w / self.out_total[first]) if w else None

    def keyword_chars(self, text):
        return {c for c in text if word_char(c) and c not in self.stop and c in self.frequency}

    def seeds(self, text):
        """Informative 1-2 character spans of the input that the model knows.

        A two-character span must be a learned edge, so the walk starts on a
        real local phrase. Scores are surprise in bits; only the top few spans
        are kept, and the caller samples among them in proportion to score.
        """
        scored = {}
        keywords = self.keyword_chars(text)
        for index, character in enumerate(text):
            if character not in keywords:
                continue
            scored[character] = max(scored.get(character, 0.0), self.surprise(character))
            if index + 1 < len(text):
                following = text[index + 1]
                step = self.transition_surprise(character, following)
                if step is not None and word_char(following):
                    span = character + following
                    scored[span] = max(scored.get(span, 0.0), self.surprise(character) + step)
        ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:TOP_SEEDS]

    # ---- walks -------------------------------------------------------------

    def _forward(self, current, budget, rng):
        out = ''
        while len(out) < budget:
            if self.bounds:
                if rng.random() < self.bounds.p_end(current):
                    break
            elif current in TERMINAL or rng.random() < HEURISTIC_HAZARD:
                break
            rows = self.forward.get(current)
            if not rows:
                break
            current = weighted_pick(rows, rng)
            out += current
        return out

    def _backward(self, current, budget, rng):
        out = ''
        while len(out) < budget:
            if self.bounds and rng.random() < self.bounds.p_start(current):
                break
            if not self.bounds and rng.random() < HEURISTIC_HAZARD:
                break
            rows = self.backward.get(current)
            if not rows:
                break
            previous = weighted_pick(rows, rng)
            if not self.bounds and previous in TERMINAL:
                break  # Heuristic: a sentence end just before us is a message start.
            out = previous + out
            current = previous
        return out

    def _base(self, character, forward):
        """Round 1's one-character distribution, with the boundary as a symbol.

        Its support is exactly the learned `chars` edges plus the boundary, so
        nothing above it can revive a deleted or never-learned pair.
        """
        rows = (self.forward if forward else self.backward).get(character, ())
        boundary = END if forward else START
        if not rows:
            return {boundary: 1.0}
        if self.bounds:
            stop = self.bounds.p_end(character) if forward else self.bounds.p_start(character)
        else:
            stop = HEURISTIC_HAZARD
        total = sum(w for _, w in rows)
        dist = {value: (1 - stop) * w / total for value, w in rows}
        dist[boundary] = stop
        return dist

    def distribution(self, context, forward):
        """P(next | context) forward or P(previous | context) backward.

        `context` is the text on the walk's side of the next symbol, nearest
        character last (forward) or first (backward). Witten-Bell mixing:
        P_k = (c(h,x) + T(h) P_{k-1}) / (N(h) + T(h)), using only contexts seen
        in at least `min_support` distinct messages, and only counts for
        symbols the base distribution allows.
        """
        cached = self._distributions.get((context, forward))
        if cached is not None:
            return cached
        dist = self._base(context[-1] if forward else context[0], forward)
        if self.contexts is None:
            return dist
        table = self.contexts.forward if forward else self.contexts.backward
        for width in range(2, min(self.contexts.order, len(context)) + 1):
            h = context[-width:] if forward else context[:width]
            counts = {x: c for x, c in table.get(h, {}).items() if x in dist}
            n = sum(counts.values())
            if n < self.min_support:
                break  # Longer contexts are at most this frequent.
            t = len(counts)
            dist = {x: (counts.get(x, 0) + t * p) / (n + t) for x, p in dist.items()}
        self._distributions[(context, forward)] = dist
        return dist

    def _walk(self, text, budget, forward, rng):
        """Extend `text` on one side; returns (added, reached_boundary)."""
        added = ''
        boundary = END if forward else START
        while len(added) < budget:
            whole = text + added if forward else added + text
            context = whole[-self.contexts.order:] if forward else whole[:self.contexts.order]
            dist = self.distribution(context, forward)
            ticket = rng.random() * sum(dist.values())
            choice = boundary
            for value in sorted(dist):
                ticket -= dist[value]
                if ticket < 0:
                    choice = value
                    break
            if choice == boundary:
                return added, True
            added = added + choice if forward else choice + added
        return added, False

    def grow_contexts(self, seed, rng):
        """Round 2: (text, complete) through `seed`, or None if too short or unsafe."""
        budget = MAX_REPLY - len(seed)
        if rng.random() < .5:
            after, ended = self._walk(seed, budget, True, rng)
            before, started = self._walk(seed + after, budget - len(after), False, rng)
        else:
            before, started = self._walk(seed, budget, False, rng)
            after, ended = self._walk(before + seed, budget - len(before), True, rng)
        text = before + seed + after
        if not self._acceptable(text):
            return None
        return text, ended and started

    def _acceptable(self, text):
        if len(text) < MIN_REPLY or len(text) > MAX_REPLY:
            return False
        if self.banned and any(pair in self.banned for pair in zip(text, text[1:])):
            return False  # Same outbound rule as Bot: a banned pair drops the reply.
        return not structural_issue(text)

    def grow(self, seed, rng):
        """One sentence through `seed`, or None when it is too short or unsafe."""
        budget = MAX_REPLY - len(seed)
        # Randomise which side may use the larger share of the length cap.
        if rng.random() < .5:
            after = self._forward(seed[-1], budget, rng)
            before = self._backward(seed[0], budget - len(after), rng)
        else:
            before = self._backward(seed[0], budget, rng)
            after = self._forward(seed[-1], budget - len(before), rng)
        text = before + seed + after
        return text if self._acceptable(text) else None

    # ---- scoring -----------------------------------------------------------

    def score(self, text, keywords):
        """MegaHAL-style information of the input keywords inside a reply.

        Each keyword occurrence adds its surprise under the forward model
        (given the previous character) and the backward model (given the next
        one). Long replies are damped the way megahal.c's evaluate_reply does,
        so a candidate cannot win merely by repeating keywords.
        """
        entropy, count = 0.0, 0
        for index, character in enumerate(text):
            if character not in keywords:
                continue
            if index > 0:
                w = self.edges.get((text[index - 1], character))
                if w:
                    entropy -= math.log2(w / self.out_total[text[index - 1]])
                    count += 1
            if index + 1 < len(text):
                w = self.edges.get((character, text[index + 1]))
                if w:
                    entropy -= math.log2(w / self.in_total[text[index + 1]])
                    count += 1
        if count >= 8:
            entropy /= math.sqrt(count - 1)
        if count >= 16:
            entropy /= count
        return entropy

    def reply(self, text, rng):
        """Best candidate for one incoming message, or None when there is none.

        No keyword, or no acceptable sentence, means silence -- never a fall
        back to continuing the user's tail, which is the behaviour being fixed.
        """
        seeds = self.seeds(text)
        if not seeds:
            return None
        keywords = self.keyword_chars(text)
        stripped = text.strip()
        best = None
        # Round 2: a keyword usually sits mid-sentence and each side stops with
        # a memoryless rate, so raw candidates run about twice a real message,
        # and keyword scoring favours long ones further. Rank by closeness to
        # a length drawn from real messages first, in 4-character bands.
        target = (self.bounds.draw_length(rng)
                  if self.contexts is not None and self.bounds.lengths else None)
        for _ in range(self.attempts):
            ticket = rng.random() * sum(score for _, score in seeds)
            seed = seeds[-1][0]
            for span, score in seeds:
                ticket -= score
                if ticket < 0:
                    seed = span
                    break
            if self.contexts is None:
                candidate, complete = self.grow(seed, rng), True
            else:
                grown = self.grow_contexts(seed, rng)
                candidate, complete = grown if grown else (None, False)
            if candidate is None or candidate in stripped:
                continue  # Echoing (part of) the input is not a reply.
            scored = Candidate(candidate, seed, self.score(candidate, keywords), complete)
            # Round 2 prefers sentences that reached a real start and end over
            # ones cut by the length cap; round 1 marks every candidate complete.
            rank = self._rank(scored, target)
            if best is None or rank > self._rank(best, target):
                best = scored
        return best

    @staticmethod
    def _rank(candidate, target):
        band = 0 if target is None else -(abs(len(candidate.text) - target) // 4)
        return candidate.complete, band, candidate.score, candidate.text


# ---- offline harness -------------------------------------------------------

def snapshot(source, destination):
    """Copy a live database through the backup API, opening the source read-only."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def export_messages(path, excluded=()):
    """Cleaned messages of one export in time order, as Bot.train_file sees them.

    Returns (message, chunks) pairs: chunks are the attachment-split pieces
    the character models learn from; a message start/end is a chunk edge.
    """
    records, _ = datastore.read_text_source(path)
    records = sorted(enumerate(records), key=lambda item: (item[1][0], item[0]))
    excluded = set(excluded) | {GROUP_ASSISTANT}
    messages = []
    for _, (_, sender, raw) in records:
        if sender and sender in excluded:
            continue
        chunks = [strong_repeat_repair(chunk.strip())[0]
                  for chunk in datastore.PLACEHOLDER.split(raw) if chunk.strip()]
        message = ''.join(chunks)
        if not message or message.split(maxsplit=1)[0].startswith('/'):
            continue
        messages.append((message, chunks))
    return messages


def train_database(destination, messages):
    """A fresh generate model (chars + gen_trigrams) from already-cleaned chunks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    Bot(str(destination), self_id='keyword-gen-offline').close()
    pairs, windows = Counter(), Counter()
    for _, chunks in messages:
        for chunk in chunks:
            pairs.update(zip(chunk, chunk[1:]))
            windows.update(trigram_counts(chunk))
    db = sqlite3.connect(destination)
    try:
        with db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)',
                           ((a, b, w) for (a, b), w in pairs.items()))
            db.executemany('INSERT INTO gen_trigrams VALUES (?,?,?,?)',
                           ((a, b, c, w) for (a, b, c), w in windows.items()))
    finally:
        db.close()


def memory_texts(db):
    if not datastore.has_table(db, 'intel_memory'):
        return []
    return [(text, w) for text, w in db.execute('SELECT text, w FROM intel_memory ORDER BY id')]


def longest_copy(text, corpus):
    """Longest substring of `text` that appears verbatim in the training corpus."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if any(text[i:i + middle] in corpus for i in range(len(text) - middle + 1)):
            low = middle
        else:
            high = middle - 1
    return low


def summarize(rows, key, generator, corpus, whole):
    replies = [row[key] for row in rows]
    spoken = [(row['prompt'], reply) for row, reply in zip(rows, replies) if reply]
    lengths = [len(reply) for _, reply in spoken]
    copies = [longest_copy(reply, corpus) for _, reply in spoken] if corpus else []
    # Continuation: the reply's first character is a learned successor of the
    # prompt's last one -- true by construction for the live generate model.
    continuation = sum(1 for prompt, reply in spoken
                       if (prompt[-1], reply[0]) in generator.edges)
    overlap = sum(1 for prompt, reply in spoken
                  if generator.keyword_chars(prompt) & set(reply))
    latency = [row[key + '_ms'] for row in rows]
    count = len(spoken) or 1
    return {
        'prompts': len(rows),
        'no_reply_rate': round(1 - len(spoken) / len(rows), 4) if rows else None,
        'length_median': statistics.median(lengths) if lengths else None,
        'length_mean': round(statistics.fmean(lengths), 2) if lengths else None,
        'continuation_edge_rate': round(continuation / count, 4),
        'keyword_overlap_rate': round(overlap / count, 4),
        'whole_message_copy_rate': round(sum(r in whole for _, r in spoken) / count, 4),
        'longest_copy_median': statistics.median(copies) if copies else None,
        'longest_copy_ratio_mean': (round(statistics.fmean(
            c / len(r) for c, (_, r) in zip(copies, spoken)), 4) if copies else None),
        'distinct_reply_rate': round(len({r for _, r in spoken}) / count, 4),
        'latency_ms_median': round(statistics.median(latency), 2) if latency else None,
        'latency_ms_p95': (round(sorted(latency)[max(0, math.ceil(len(latency) * .95) - 1)], 2)
                           if latency else None),
    }


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_prompts(args, messages, db):
    if args.prompts_file:
        lines = Path(args.prompts_file).read_text(encoding='utf-8-sig').splitlines()
        return [line for line in lines if line.strip()], 'file', True
    if messages is not None:
        split = len(messages) - max(1, math.ceil(len(messages) * args.holdout))
        held = [message for message, _ in messages[split:]]
        return held, 'export_holdout', True
    # Memories were trained on: prompts drawn from them are NOT held out.
    texts = [text for text, _ in memory_texts(db)]
    return texts, 'intel_memory_in_training', False


def compare(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model = out / 'model.sqlite3'
    messages = None
    if args.export:
        messages = export_messages(args.export, args.exclude)
        if len(messages) < 20:
            raise SystemExit('导出中的有效消息少于 20 条，无法比较')
        split = len(messages) - max(1, math.ceil(len(messages) * args.holdout))
        train_database(model, messages[:split])
        training = messages[:split]
        # Chunks, not messages: `chars` never links across an attachment, so a
        # chunk edge is exactly where a walk has no in-message neighbour.
        sentences = [(chunk, 1) for _, chunks in training for chunk in chunks]
        whole = {message for message, _ in training}
        source = {'export': str(args.export), 'sha256': digest(args.export),
                  'messages': len(messages), 'trained': split}
    else:
        snapshot(args.db, model)
        source = {'db': str(args.db), 'snapshot_sha256': digest(model)}
    db = sqlite3.connect(model)
    try:
        if messages is None:
            sentences = memory_texts(db)
            whole = {text for text, _ in sentences}
        bounds = Bounds() if args.heuristic_bounds else Bounds(sentences)
        contexts = Contexts(sentences, args.order) if args.order > 1 else None
        round_one = KeywordGenerator(db, bounds=bounds, attempts=args.attempts)
        generator = KeywordGenerator(db, bounds=bounds, attempts=args.attempts,
                                     contexts=contexts, min_support=args.min_support)
        prompts, prompt_source, held_out = load_prompts(args, messages, db)
    finally:
        db.close()
    prompts = [p for p in prompts if p.strip()]
    picker = random.Random(args.seed)
    if len(prompts) > args.prompts:
        prompts = picker.sample(prompts, args.prompts)
    if not prompts:
        raise SystemExit('没有可用的测试输入')
    corpus = '\x00'.join(sorted(whole))
    bot = Bot(str(model), self_id='keyword-gen-offline', group_id=args.group,
              generate_blend_max=args.blend, generate_refine_probability=args.refine)
    rows = []
    try:
        for index, prompt in enumerate(prompts):
            seed = args.seed * 100003 + index
            bot.rng, bot.refine_rng = random.Random(seed), random.Random(seed ^ 0x5bd1e995)
            started = time.perf_counter()
            baseline = bot.generate(prompt[-2:])  # Exactly what Bot.handle passes.
            if baseline and bot.breaking_pair(baseline) is not None:
                baseline = None
            middle = time.perf_counter()
            first = round_one.reply(prompt, random.Random(seed))
            between = time.perf_counter()
            found = generator.reply(prompt, random.Random(seed))
            finished = time.perf_counter()
            rows.append({'prompt': prompt, 'baseline': baseline,
                         'keyword_v1': first.text if first else None,
                         'keyword': found.text if found else None,
                         'seed_span': found.seed if found else None,
                         'complete': found.complete if found else None,
                         'baseline_ms': (middle - started) * 1000,
                         'keyword_v1_ms': (between - middle) * 1000,
                         'keyword_ms': (finished - between) * 1000})
    finally:
        bot.close()
    spoken = [row for row in rows if row['keyword']]
    metrics = {
        'source': source, 'prompt_source': prompt_source, 'held_out': held_out,
        'boundaries': ('whole_messages' if bounds else 'heuristic'),
        'boundary_messages': bounds.messages,
        'params': {'seed': args.seed, 'attempts': args.attempts, 'blend': args.blend,
                   'refine': args.refine, 'group': args.group, 'stop_top': STOP_TOP,
                   'order': args.order, 'min_support': args.min_support},
        'code_sha256': digest(__file__),
        'baseline': summarize(rows, 'baseline', generator, corpus, whole),
        'keyword_v1': summarize(rows, 'keyword_v1', generator, corpus, whole),
        'keyword': dict(summarize(rows, 'keyword', generator, corpus, whole),
                        complete_rate=(round(sum(r['complete'] for r in spoken) / len(spoken), 4)
                                       if spoken else None)),
    }
    with open(out / 'compare.tsv', 'w', encoding='utf-8', newline='') as handle:
        handle.write('index\tprompt\tbaseline\tkeyword_v1\tkeyword\tseed_span\n')
        for index, row in enumerate(rows):
            cells = [str(index)] + [clean_cell(row[k]) for k in
                                    ('prompt', 'baseline', 'keyword_v1', 'keyword', 'seed_span')]
            handle.write('\t'.join(cells) + '\n')
    write_blind(out, rows, random.Random(args.seed + 1))
    write_blind(out, rows, random.Random(args.seed + 2), opponent='keyword_v1', name='v1')
    (out / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    print(json.dumps({k: metrics[k] for k in ('prompt_source', 'held_out', 'boundaries',
                                              'baseline', 'keyword_v1', 'keyword')},
                     ensure_ascii=False, indent=2))
    print(f'输出目录：{out}')


def clean_cell(value):
    return '' if value is None else value.replace('\t', ' ').replace('\r', ' ').replace('\n', '⏎')


def blind_names(name):
    suffix = f'_{name}' if name else ''
    return f'blind_pairs{suffix}.tsv', f'answer_key{suffix}.json', f'tally{suffix}.json'


def write_blind(out, rows, rng, *, opponent='baseline', name=''):
    """Left/right-shuffled pairs for a reader; the key lives in its own file."""
    pairs, answers, _ = blind_names(name)
    key = {}
    with open(out / pairs, 'w', encoding='utf-8', newline='') as handle:
        handle.write('index\tprompt\tleft\tright\tchoice(left/right/same/both_bad)\n')
        for index, row in enumerate(rows):
            sides = [opponent, 'keyword']
            rng.shuffle(sides)
            key[index] = sides
            handle.write('\t'.join([str(index), clean_cell(row['prompt']),
                                    clean_cell(row[sides[0]]) or '（无回复）',
                                    clean_cell(row[sides[1]]) or '（无回复）', '']) + '\n')
    (out / answers).write_text(json.dumps(key, indent=1), encoding='utf-8')


CHOICES = ('left', 'right', 'same', 'both_bad')


def tally(args):
    """Aggregate a filled blind_pairs.tsv into counts only -- no chat text."""
    out = Path(args.out)
    pairs, answers, result = blind_names(getattr(args, 'name', ''))
    key = json.loads((out / answers).read_text(encoding='utf-8'))
    opponent = next((side for sides in key.values() for side in sides if side != 'keyword'),
                    'baseline')
    counts, blank, invalid = Counter(), 0, 0
    lines = (out / pairs).read_text(encoding='utf-8-sig').splitlines()[1:]
    for line in lines:
        cells = line.split('\t')
        choice = cells[4].strip().lower() if len(cells) > 4 else ''
        if not choice:
            blank += 1
        elif choice not in CHOICES or cells[0] not in key:
            invalid += 1
        elif choice in ('left', 'right'):
            counts[key[cells[0]][0 if choice == 'left' else 1] + '_wins'] += 1
        else:
            counts[choice] += 1
    judged = sum(counts.values())
    summary = {'opponent': opponent, 'pairs': len(lines), 'judged': judged, 'blank': blank,
               'invalid': invalid,
               **{name: counts[name] for name in
                  ('keyword_wins', opponent + '_wins', 'same', 'both_bad')},
               'keyword_net_win_rate': (round((counts['keyword_wins'] - counts[opponent + '_wins'])
                                              / judged, 4) if judged else None)}
    (out / result).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


def interactive(args):
    out = Path(args.out)
    model = out / 'reply-snapshot.sqlite3'
    snapshot(args.db, model)
    db = sqlite3.connect(model)
    try:
        memories = memory_texts(db)
        bounds = Bounds() if args.heuristic_bounds else Bounds(memories)
        contexts = Contexts(memories, args.order) if args.order > 1 else None
        generator = KeywordGenerator(db, bounds=bounds, attempts=args.attempts,
                                     contexts=contexts, min_support=args.min_support)
    finally:
        db.close()
    bot = Bot(str(model), self_id='keyword-gen-offline', group_id=args.group,
              generate_blend_max=args.blend, generate_refine_probability=args.refine)
    rng = random.Random(args.seed)
    try:
        texts = args.text or iter(lambda: input('> '), '')
        for text in texts:
            print('关键字候选：', ' '.join(f'{s}({v:.1f})' for s, v in generator.seeds(text)) or '无')
            for _ in range(args.n):
                found = generator.reply(text, rng)
                print('  keyword :', found.text if found else '（无回复）',
                      f'[{found.seed}]' if found else '')
                print('  baseline:', bot.generate(text[-2:]) or '（无回复）')
    except EOFError:
        pass
    finally:
        bot.close()


def main():
    parser = argparse.ArgumentParser(description='关键字双向生成离线原型；不连接 QQ、不写生产库')
    sub = parser.add_subparsers(dest='command', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--group', default=TEST_GROUP, choices=sorted(ALLOWED_GROUPS),
                        help='仅用于构造离线 Bot 做基线对比')
    common.add_argument('--blend', type=float, default=0.0, help='基线 generate_blend_max')
    common.add_argument('--refine', type=float, default=.30, help='基线 refine 概率（线上 0.30）')
    common.add_argument('--attempts', type=int, default=ATTEMPTS)
    common.add_argument('--seed', type=int, default=1)
    common.add_argument('--heuristic-bounds', action='store_true',
                        help='不用整句边界统计，改用标点与几何长度')
    common.add_argument('--order', type=int, default=ORDER, choices=(1, 2, 3, 4),
                        help='第二轮最长字符上下文；1 等于第一轮原型')
    common.add_argument('--min-support', type=int, default=MIN_SUPPORT,
                        help='上下文至少出现在多少条不同消息中才参与')
    common.add_argument('--out', default=str(WORK))
    run = sub.add_parser('compare', parents=[common], help='基线与关键字生成的批量对比')
    data = run.add_mutually_exclusive_group(required=True)
    data.add_argument('--db', type=Path, help='群数据库；只读快照后使用')
    data.add_argument('--export', type=Path, help='QQ 文本导出；按时间切分训练/留出')
    run.add_argument('--holdout', type=float, default=.1)
    run.add_argument('--prompts', type=int, default=200)
    run.add_argument('--prompts-file', type=Path, help='每行一条测试输入')
    run.add_argument('--exclude', nargs='*', default=[], help='排除的发送者 QQ（如机器人自身）')
    reply = sub.add_parser('reply', parents=[common], help='对单条输入试生成')
    reply.add_argument('--db', type=Path, required=True)
    reply.add_argument('--n', type=int, default=3)
    reply.add_argument('text', nargs='*')
    count = sub.add_parser('tally', help='汇总已填写的 blind_pairs.tsv，只输出计数')
    count.add_argument('--out', default=str(WORK))
    count.add_argument('--name', default='', help='留空为对基线；v1 为对第一轮原型')
    args = parser.parse_args()
    if args.command == 'compare':
        if not 0 < args.holdout < 1:
            parser.error('--holdout 必须在 0 与 1 之间')
        compare(args)
    elif args.command == 'tally':
        tally(args)
    else:
        interactive(args)


if __name__ == '__main__':
    main()
