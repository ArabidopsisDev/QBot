"""Conservative QQ sticker cache and weak context-based image selection.

Only explicit sticker segments are admitted. Cached files and the association
index live under data/stickers; ordinary pictures are never downloaded here.
"""
from __future__ import annotations

import hashlib
import queue
import random
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from intelligence import features, similarity
from qbot import ALLOWED_GROUPS, identifier

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_ASSETS_PER_GROUP = 200
MAX_QUEUE = 24
KEEP_SECONDS = 7 * 86400
CONTEXT_SECONDS = 60
SEND_COOLDOWN = 180
IMAGE_DEADLINE = 25
ALLOWED_HOSTS = frozenset(('multimedia.nt.qq.com.cn', 'gxh.vip.qq.com'))


class StickerSubtypeRejected(RuntimeError):
    """NapCat explicitly rejected sticker styling; plain-image fallback is safe."""


def sticker_segment(event):
    """Accept only a standalone NapCat sticker, never a regular image."""
    if not isinstance(event, dict) or event.get('post_type') != 'message' \
            or event.get('message_type') != 'group' \
            or identifier(event.get('group_id')) not in ALLOWED_GROUPS:
        return None
    segments = event.get('message')
    if not isinstance(segments, list) or len(segments) != 1:
        return None
    segment = segments[0]
    if not isinstance(segment, dict) or segment.get('type') != 'image':
        return None
    data = segment.get('data')
    if not isinstance(data, dict):
        return None
    market = bool(data.get('emoji_id') and data.get('emoji_package_id'))
    custom = (str(data.get('sub_type')) == '1' and
              data.get('summary') == '[动画表情]')
    if not (market or custom):
        return None
    size = data.get('file_size')
    if size not in (None, ''):
        try:
            if not 0 < int(size) <= MAX_IMAGE_BYTES:
                return None
        except (ValueError, TypeError):
            return None
    url = data.get('url')
    if not isinstance(url, str) or not url_allowed(url):
        return None
    return {'url':url, 'kind':'market' if market else 'custom',
            'summary':str(data.get('summary') or '')[:80] if market else ''}


def url_allowed(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname in ALLOWED_HOSTS
                and parts.username is None and parts.password is None
                and parts.port in (None, 443) and bool(parts.path))
    except ValueError:
        return False


def image_format(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return 'jpg'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    return None


def download_sticker(url):
    if not url_allowed(url):
        raise ValueError('Unapproved sticker source')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent':'QBot-sticker-cache/1'})
    with opener.open(request, timeout=10) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_IMAGE_BYTES:
            raise ValueError('Sticker is too large')
        raw = response.read(MAX_IMAGE_BYTES + 1)
    if not raw or len(raw) > MAX_IMAGE_BYTES or image_format(raw) is None:
        raise ValueError('Not a bounded supported image')
    return raw


class StickerLibrary:
    def __init__(self, root, *, clock=time.time, rng=None, fetch=download_sticker):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'stickers.sqlite3'
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        self.fetch = fetch
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS assets (
                    group_id TEXT NOT NULL, sha TEXT NOT NULL, ext TEXT NOT NULL,
                    kind TEXT NOT NULL, summary TEXT NOT NULL,
                    seen_count INTEGER NOT NULL, first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL, last_sent REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(group_id,sha));
                CREATE TABLE IF NOT EXISTS terms (
                    group_id TEXT NOT NULL, sha TEXT NOT NULL, term TEXT NOT NULL,
                    w REAL NOT NULL, PRIMARY KEY(group_id,sha,term));
                CREATE INDEX IF NOT EXISTS terms_lookup ON terms(group_id,term);
                CREATE TABLE IF NOT EXISTS events (
                    group_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at REAL NOT NULL,
                    PRIMARY KEY(group_id,message_id));
            ''')

    def connect(self):
        @contextmanager
        def session():
            db = sqlite3.connect(self.database, timeout=2)
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        return session()

    def asset_path(self, group, sha, ext):
        if group not in ALLOWED_GROUPS or len(sha) != 64 \
                or any(ch not in '0123456789abcdef' for ch in sha) \
                or ext not in ('png','gif','jpg','webp'):
            raise ValueError('Invalid cached sticker identity')
        folder = (self.root / group).resolve()
        if folder.parent != self.root:
            raise ValueError('Sticker path escaped cache')
        return folder / f'{sha}.{ext}'

    def _prune(self, db, group, now):
        db.execute('DELETE FROM events WHERE seen_at<?', (now-KEEP_SECONDS,))
        expired = db.execute('SELECT sha,ext FROM assets WHERE group_id=? AND last_seen<?',
                             (group,now-KEEP_SECONDS)).fetchall()
        count = db.execute('SELECT COUNT(*) FROM assets WHERE group_id=?',(group,)).fetchone()[0]
        excess = max(0,count-len(expired)-MAX_ASSETS_PER_GROUP)
        if excess:
            expired += db.execute('''SELECT sha,ext FROM assets WHERE group_id=? AND last_seen>=?
                ORDER BY last_seen,sha LIMIT ?''',(group,now-KEEP_SECONDS,excess)).fetchall()
        for sha,ext in expired:
            db.execute('DELETE FROM terms WHERE group_id=? AND sha=?',(group,sha))
            db.execute('DELETE FROM assets WHERE group_id=? AND sha=?',(group,sha))
            self.asset_path(group,sha,ext).unlink(missing_ok=True)

    def capture(self, event, previous=None):
        spec = sticker_segment(event)
        if spec is None:
            return False
        group = identifier(event['group_id'])
        message_id = identifier(event.get('message_id'))
        stamp = event.get('time')
        if not message_id or type(stamp) not in (int,float):
            return False
        with self.connect() as db:
            if db.execute('SELECT 1 FROM events WHERE group_id=? AND message_id=?',
                          (group,message_id)).fetchone():
                return False
        raw = self.fetch(spec['url'])
        ext = image_format(raw)
        if ext is None or not 0 < len(raw) <= MAX_IMAGE_BYTES:
            return False
        sha = hashlib.sha256(raw).hexdigest()
        path = self.asset_path(group,sha,ext)
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():
            try:
                with path.open('xb') as file:
                    file.write(raw)
            except FileExistsError:
                pass
        now = self.clock()
        terms = {}
        if previous is not None:
            text, previous_time = previous
            if isinstance(text,str) and 0 <= stamp-previous_time <= CONTEXT_SECONDS:
                terms.update(features(text))
        if spec['summary']:
            for term,w in features(spec['summary']).items():
                terms[term] = terms.get(term,0) + 2*w
        with self.connect() as db:
            if not db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                              (group,message_id,now)).rowcount:
                return False
            db.execute('''INSERT INTO assets VALUES (?,?,?,?,?,1,?,?,0)
                ON CONFLICT(group_id,sha) DO UPDATE SET
                seen_count=seen_count+1,last_seen=excluded.last_seen''',
                (group,sha,ext,spec['kind'],spec['summary'],now,now))
            db.executemany('''INSERT INTO terms VALUES (?,?,?,?)
                ON CONFLICT(group_id,sha,term) DO UPDATE SET w=w+excluded.w''',
                ((group,sha,term,w) for term,w in terms.items()))
            self._prune(db,group,now)
        return True

    def choose(self, group, prompt, previous=None, generated=None):
        if group not in ALLOWED_GROUPS or not isinstance(prompt,str) or not prompt:
            return None
        prompt_terms = features(prompt)
        reply_terms = features(generated) if isinstance(generated,str) else {}
        context_terms = (features(previous[0]) if previous and
                         isinstance(previous[0],str) else {})
        if not any(len(term)>=2 and not term.startswith('@')
                   for term in (*prompt_terms,*reply_terms)):
            return None
        now = self.clock()
        candidates = []
        with self.connect() as db:
            if db.execute('SELECT 1 FROM assets WHERE group_id=? AND last_sent>0 AND last_sent>? LIMIT 1',
                          (group,now-SEND_COOLDOWN)).fetchone():
                return None
            rows = db.execute('''SELECT sha,ext,kind,summary,seen_count,last_sent FROM assets
                WHERE group_id=? AND last_seen>=? ORDER BY last_seen DESC LIMIT ?''',
                (group,now-KEEP_SECONDS,MAX_ASSETS_PER_GROUP)).fetchall()
            for sha,ext,kind,summary,seen_count,last_sent in rows:
                terms = dict(db.execute('SELECT term,w FROM terms WHERE group_id=? AND sha=?',
                                        (group,sha)))
                if not terms:
                    continue
                reply_shared = [term for term in reply_terms if term in terms and
                                len(term)>=2 and not term.startswith('@')]
                prompt_shared = [term for term in prompt_terms if term in terms and
                                 len(term)>=2 and not term.startswith('@')]
                reply_score = similarity(reply_terms,terms) if reply_shared else 0.0
                prompt_score = similarity(prompt_terms,terms) if prompt_shared else 0.0
                context_score = (similarity(context_terms,terms) if context_terms else 0.0)
                if seen_count < 2 and kind != 'market':
                    # One custom use is weak evidence. Require a substantial
                    # generated-text match; never activate it from the prompt alone.
                    if not any(len(term)>=3 for term in reply_shared) or reply_score < .52:
                        continue
                    chance = .14
                elif reply_score >= .30:
                    chance = .30 if (reply_score >= .48 or
                                      any(term in prompt_shared for term in reply_shared)) else .25
                elif prompt_score >= .38:
                    chance = .16
                else:
                    continue
                rank = (reply_score + .30*prompt_score + .08*context_score -
                        (.08 if last_sent and now-last_sent < 600 else 0.0))
                candidates.append((rank,chance,sha,ext))
            if not candidates:
                return None
            _,chance,sha,ext = max(candidates,key=lambda item:(item[0],item[2]))
            if self.rng.random() >= chance:
                return None
            path = self.asset_path(group,sha,ext)
            if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                return None
            db.execute('UPDATE assets SET last_sent=? WHERE group_id=? AND sha=?',
                       (now,group,sha))
        return path


class StickerService:
    """Small bounded worker for downloads and delayed ordinary-image sends."""
    def __init__(self, library, sender, *, stop_file, logger=lambda _:None):
        self.library = library
        self.sender = sender
        self.stop_file = Path(stop_file)
        self.logger = logger
        self.jobs = queue.Queue(MAX_QUEUE)
        self.worker = threading.Thread(target=self._run,daemon=True,name='qbot-stickers')
        self.worker.start()

    def capture(self,event,previous=None):
        if sticker_segment(event) is None:
            return False
        try:
            self.jobs.put_nowait(('capture',event,previous))
            return True
        except queue.Full:
            return False

    def after_text(self,group,prompt,previous=None,generated=None):
        try:
            path = self.library.choose(group,prompt,previous,generated)
            if path is None:
                return False
            self.jobs.put_nowait(('send',group,path,time.monotonic()+IMAGE_DEADLINE))
            return True
        except (queue.Full,sqlite3.Error,OSError,ValueError):
            return False

    def _run(self):
        while not self.stop_file.exists():
            try:
                job = self.jobs.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                if job[0]=='capture':
                    if self.library.capture(job[1],job[2]):
                        self.logger('Authorized sticker cached.')
                else:
                    _,group,path,deadline=job
                    prefer_sticker = True
                    while time.monotonic()<deadline and not self.stop_file.exists():
                        try:
                            sent = self.sender.send_image(
                                group,path,prefer_sticker=prefer_sticker)
                        except StickerSubtypeRejected:
                            prefer_sticker = False
                            time.sleep(.5)
                            continue
                        if sent:
                            self.logger('Authorized sticker image sent.')
                            break
                        time.sleep(.5)  # No request was sent if the rate limit said no.
            except Exception:
                # Download or unknown send state is not retried.
                self.logger('Sticker operation failed or was uncertain; not retried.')
            finally:
                self.jobs.task_done()
