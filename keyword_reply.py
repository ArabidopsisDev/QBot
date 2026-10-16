"""Keyword-seeded two-way reply generation (MegaHAL-style), shared engine.

The plain generate model starts from the last character of the incoming
message, so its output reads as the rest of the user's sentence. This engine
picks an informative keyword from the message, grows a sentence in both
directions from it -- forward to a message end, backward to a message start --
with 1-3 character contexts, and keeps the candidate that reached real
boundaries, sits near a real message length and carries the most keyword
information. Every adjacent pair it can emit is a learned `chars` edge.

Used live by `Bot.keyword_reply` (qbot.py) and offline by keyword_gen.py.
It only reads the database it is given; it never sends or writes anything.
"""
from __future__ import annotations

import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

import datastore
from generation_refine import structural_issue, weighted_pick
from intelligence import STOP_CHARS

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
# Longest character context used by the walks, and the number of distinct
# messages a context needs before it may shift probabilities.
ORDER = 3
MIN_SUPPORT = 2
END, START = '\x03', '\x02'  # Boundary symbols; never emitted, never in chars.
# Copy guard: a reply may not equal a training message, nor take at least this
# share of itself verbatim from one (checked from this length up).
COPY_RATIO = .8
COPY_CHECK_MIN = 6


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
                 contexts=None, min_support=MIN_SUPPORT, corpus=None):
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
        # Copy guard source: the whole training messages, when known.
        texts = sorted({text for text in (corpus or ()) if text})
        self.copy_set, self.copy_text = set(texts), '\x00'.join(texts)
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
        found = []
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
            found.append(Candidate(candidate, seed, self.score(candidate, keywords), complete))
        # Round 2 prefers sentences that reached a real start and end over ones
        # cut by the length cap; round 1 marks every candidate complete. The
        # best candidate that is not a copy wins; the guard draws no randomness.
        found.sort(key=lambda candidate: self._rank(candidate, target), reverse=True)
        return next((candidate for candidate in found if not self.copied(candidate.text)), None)

    def copied(self, text):
        """True when `text` is a training message, or mostly lifted from one."""
        if text in self.copy_set:
            return True
        if len(text) < COPY_CHECK_MIN or not self.copy_text:
            return False
        return longest_copy(text, self.copy_text) >= math.ceil(COPY_RATIO * len(text))

    @staticmethod
    def _rank(candidate, target):
        band = 0 if target is None else -(abs(len(candidate.text) - target) // 4)
        return candidate.complete, band, candidate.score, candidate.text
