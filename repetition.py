"""Exact, conservative repeat repair. Pure text work; no model weights change."""
import re
import unicodedata

MAX_SCAN_TEXT = 10000
MAX_BLOCK = 12
EXPRESSIVE = frozenset('哈呵嘿嘻喵啊嗯哦')
URL = re.compile(r'(?i)(?:https?://|www\.)\S+')
CODE = re.compile(r'```|`[^`]*`|[{}]|(?:^|\n)\s*(?:def |class |import |#include|SELECT |function\b)')


def strong_repeat_findings(text):
    """Return only exact, long runs; ordinary laughter, URLs and code survive.

    A 2-12 character block needs at least four copies covering 24 characters.
    A non-expressive single letter needs 16 copies. Short emphasis is untouched.
    Positions refer to the original text and findings never overlap.
    """
    if (not isinstance(text, str) or len(text) > MAX_SCAN_TEXT or
            URL.search(text) or CODE.search(text)):
        return []
    found, start, n = [], 0, len(text)
    while start < n:
        best = None
        for width in range(1, min(MAX_BLOCK, (n-start)//4) + 1):
            block = text[start:start+width]
            # Numbers, spacing, punctuation and symbolic data are not sentences.
            if (any(char.isdigit() or not unicodedata.category(char).startswith('L')
                    for char in block) or set(block) <= EXPRESSIVE):
                continue
            # A longer spelling of a single-character run must not bypass its
            # special handling, especially 哈哈哈哈 / 喵喵喵喵.
            if width > 1 and len(set(block)) == 1:
                continue
            cursor, repeats = start + width, 1
            while cursor + width <= n and text[cursor:cursor+width] == block:
                cursor += width
                repeats += 1
            span = cursor-start
            if (width == 1 and repeats >= 16) or (width > 1 and repeats >= 4 and span >= 24):
                candidate = {'start': start, 'end': cursor, 'block': block,
                             'width': width, 'repeats': repeats, 'span': span,
                             'removed': span-width}
                if best is None or (span, -width) > (best['span'], -best['width']):
                    best = candidate
        if best is None:
            start += 1
        else:
            found.append(best)
            start = best['end']
    return found


def strong_repeat_repair(text):
    """Keep the first copy of each strong run and all surrounding text."""
    findings = strong_repeat_findings(text)
    if not findings:
        return text, []
    pieces, cursor = [], 0
    for item in findings:
        pieces.extend((text[cursor:item['start']], item['block']))
        cursor = item['end']
    pieces.append(text[cursor:])
    return ''.join(pieces), findings


def dominant_repeat(text):
    """An automatic historical deletion needs one strong run covering >=80%."""
    findings = strong_repeat_findings(text)
    if not findings:
        return None
    best = max(findings, key=lambda item: item['span'])
    # Even a long run inside ordinary prose is not an automatic whole-row delete.
    if best['span']/len(text) < 0.8:
        return None
    return dict(best, coverage=round(best['span']/len(text), 4))
