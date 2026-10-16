"""Small, deterministic helpers for stopping and screening generated paths."""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

MAX_REFINE_PROBABILITY = .30
MIN_STOP_FRACTION = .75
MAX_CANDIDATES = 3
LAUGHTER = frozenset('哈呵嘿嘻喵啊嗯哦')
PUNCTUATION = frozenset('。！？!?，,;；:：.…_-=~')
OPENERS = {'(':')','[':']','{':'}','（':'）','【':'】','《':'》',
           '〈':'〉','「':'」','『':'』','“':'”','‘':'’'}
CLOSERS = frozenset(OPENERS.values())
URL = re.compile(r'(?:https?://|www\.)[^\s]+',re.I)
CODE = re.compile(r'```[\s\S]*?(?:```|$)|`[^`\n]*(?:`|$)')


@dataclass(frozen=True)
class GeneratedPath:
    text: str | None
    target: int
    blocked: bool = False


def validate_refine_probability(value):
    if (type(value) not in (int,float) or not math.isfinite(value)
            or not 0<=value<=MAX_REFINE_PROBABILITY):
        raise ValueError('生成优化概率须为 0–0.30 的有限数值')
    return float(value)


def weighted_pick(rows,rng):
    if not rows:
        return None
    ticket=rng.randrange(sum(weight for _,weight in rows))
    for value,weight in rows:
        ticket-=weight
        if ticket<0:
            return value
    raise AssertionError('Invalid generation weights')


def _protected_spans(text):
    return [match.span() for pattern in (URL,CODE) for match in pattern.finditer(text)]


def _apostrophe(text,index):
    return (text[index]=="'" and 0<index<len(text)-1 and
            text[index-1].isascii() and text[index-1].isalpha() and
            text[index+1].isascii() and text[index+1].isalpha())


def _attached_character(character):
    return (character=='\u200d' or unicodedata.category(character) in ('Mn','Mc','Me')
            or '\U0001f3fb'<=character<='\U0001f3ff')


def _scan(text):
    """Yield (position, closed, sentence_tail) while respecting code and URLs."""
    stack=[]
    spans=_protected_spans(text)
    tail=False
    mismatched=False
    for index,character in enumerate(text):
        if any(start<=index<end for start,end in spans):
            tail=False
            yield index+1,False,False
            continue
        closed_quote=False
        if character in ('"',"'") and not _apostrophe(text,index):
            if stack and stack[-1]==character:
                stack.pop(); closed_quote=True
            else:
                stack.append(character)
        elif character in OPENERS:
            stack.append(OPENERS[character])
        elif character in CLOSERS:
            if stack and stack[-1]==character:
                stack.pop(); closed_quote=True
            else:
                mismatched=True
        terminal=character in '。！？!?'
        if character=='…':
            # A full even-length Chinese ellipsis can close; odd partial runs cannot.
            run=1
            before=index-1
            while before>=0 and text[before]=='…':
                run+=1; before-=1
            terminal=run>=2 and run%2==0 and (index+1==len(text) or text[index+1]!='…')
        if terminal:
            tail=True
        elif not closed_quote:
            tail=False
        attached=(index+1<len(text) and _attached_character(text[index+1]))
        # Do not cut within expressive punctuation or an ellipsis run.
        continuing=(index+1<len(text) and text[index+1] in '。！？!?…')
        yield index+1,not stack and not mismatched,tail and not attached and not continuing


def natural_stop(text,target,fraction=MIN_STOP_FRACTION):
    if not text or len(text)<3:
        return text
    minimum=max(3,math.ceil(target*fraction))
    choices=[position for position,closed,tail in _scan(text)
             if closed and tail and minimum<=position<=target]
    return text[:choices[-1]] if choices else text


def structural_issue(text):
    """Only mechanical loops and definite paired-delimiter defects are screened."""
    if not text:
        return True
    size=len(text)
    spans=_protected_spans(text)
    for width in range(1,min(4,size//4)+1):
        for start in range(size-width*4+1):
            block=text[start:start+width]
            if len(set(block))==1 and block[0] in LAUGHTER:
                continue
            end=start+width
            repeats=1
            while text[end:end+width]==block:
                repeats+=1; end+=width
            run=end-start
            if (repeats>=4 and run>=12 and run/size>=.8 and
                    not any(a<=start and end<=b for a,b in spans)):
                return True
    if size>=8:
        punctuation=sum(character in PUNCTUATION for character in text)
        if punctuation/size>=.8 and re.search(r'([。！？!?，,;；:：.…_\-=~])\1{3,}',text):
            return True
    last=None
    for last in _scan(text):
        pass
    return bool(last and not last[1] and not any(end==len(text) for _,end in spans))
