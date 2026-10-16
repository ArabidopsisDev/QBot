"""Loopback-only NapCat transport. Text sends restricted to the authorized groups."""
import hashlib
import hmac
import json
import math
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime
from sticker import (MAX_IMAGE_BYTES, StickerLibrary, StickerService,
                     StickerSubtypeRejected, image_format, sticker_segment)

from qbot import (ALLOWED_GROUPS, EXPIRY_NOTICE, GROUP_DATABASES, TEST_GROUP,
                  PRODUCTION_GROUP, THIRD_GROUP, Bot, blend_limits, expired_whitelist,
                  group_reply, identifier)
from quality import QualityLedger
from loop_cleanup import autoclean_memory

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'runtime' / 'connection.json'
STOP_FILE = ROOT / 'runtime' / 'STOP'
API_URL = 'http://127.0.0.1:5701/send_group_msg'
MAX_EVENT_BYTES = 262144
MIN_SEND_INTERVAL = 5.0
GENERATE_REFINE_PROBABILITY = .30
# Share of generate-mode replies answered by keyword_reply.py instead of
# continuing the message tail. Enabled for the third group only.
KEYWORD_REPLY_PROBABILITY = {THIRD_GROUP: .30}
AUTO_LOOP_CLEAN_INTERVAL = 300.0
# Restart announcements are switched off: the group asked not to receive update
# notices. `broadcast_restart` is kept (and unit tested) so the behaviour can be
# re-enabled by flipping this flag, without touching the transport again.
ANNOUNCE_RESTART = False
RESTART_NOTICE = (
    '更新公告\n'
    '1. 封禁改为五级十三档（Ia–Vb），1 分钟至 24 小时，'
    '30 天内累犯每次自动升级一档。\n'
    '2. 新增 /apply：任何人都可申请 30 秒–1 小时随机白名单。\n'
    '3. 新增 /dstx：一键解除所有群的封禁（仅管理员）。\n'
    '4. 管理后台 http://127.0.0.1:5703 ：数据大屏、封禁与白名单、'
    '训练数据可视化与权重调整。\n'
    '5. generate 只统计中文转移，英文、数字、标点不再计入，也不参与生成。')
RESTART_DEADLINE = 120.0
EXPIRY_INTERVAL = 2.0


def log(message, *, flush=True):
    """Timestamped log line.

    Without a timestamp a stall can only be dated from the log file's mtime,
    which made a past outage needlessly hard to diagnose. Only status text is
    ever logged; message bodies, paths and credentials never are.
    """
    print(f'{time.strftime("%m-%d %H:%M:%S")} {message}', flush=flush)


def describe(error):
    """One bounded line describing why a send failed (e.g. a mute)."""
    text = ' '.join(str(error).split())
    return text[:160]


class SendRateLimit:
    """Shared across groups, Sender instances and restarts on this host."""
    def __init__(self, path=ROOT / 'runtime' / 'send-rate.sqlite3', clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS send_rate (id INTEGER PRIMARY KEY CHECK(id=1), last_attempt REAL NOT NULL)')
            db.commit()
        finally:
            db.close()

    def acquire(self):
        db = sqlite3.connect(self.path, timeout=0)
        try:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            row = db.execute('SELECT last_attempt FROM send_rate WHERE id=1').fetchone()
            if row is not None:
                if now < row[0]:
                    # Monotonic clock reset after a host reboot: start a fresh cooldown.
                    db.execute('UPDATE send_rate SET last_attempt=? WHERE id=1', (now,))
                    db.commit()
                    return False
                if now - row[0] < MIN_SEND_INTERVAL:
                    return False
            db.execute('INSERT OR REPLACE INTO send_rate VALUES (1,?)', (now,))
            db.commit()  # Reserve before network I/O; failures also consume the slot.
            return True
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return False
            raise
        finally:
            db.close()


def event_time_seconds(value):
    if type(value) not in (int, float, str):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    # NapCat normally uses seconds, but its fallback uses Date.now() (milliseconds).
    return value / 1000 if value >= 100_000_000_000 else value


def read_event_body(headers, stream):
    """Decode bounded Content-Length or HTTP/1.1 chunked uploads (NapCat uses chunks)."""
    transfer = headers.get('Transfer-Encoding', '').strip().lower()
    length = headers.get('Content-Length')
    if transfer:
        if transfer != 'chunked' or length is not None:
            raise ValueError('Unsupported or ambiguous body framing')
        data = bytearray()
        while True:
            line = stream.readline(130)
            if len(line) > 128 or not line.endswith(b'\r\n'):
                raise ValueError('Invalid chunk header')
            size = int(line[:-2].split(b';', 1)[0], 16)
            if size < 0 or len(data) + size > MAX_EVENT_BYTES:
                raise ValueError('Invalid chunk length')
            if size == 0:
                trailer_size = 0
                while True:
                    trailer = stream.readline(8194)
                    trailer_size += len(trailer)
                    if not trailer.endswith(b'\r\n') or trailer_size > 8192:
                        raise ValueError('Invalid trailers')
                    if trailer == b'\r\n':
                        break
                if not data:
                    raise ValueError('Empty event')
                return bytes(data)
            chunk = stream.read(size)
            if len(chunk) != size or stream.read(2) != b'\r\n':
                raise ValueError('Truncated chunk')
            data.extend(chunk)
    size = int(length or '0')
    if not 0 < size <= MAX_EVENT_BYTES:
        raise ValueError('Invalid body length')
    body = stream.read(size)
    if len(body) != size:
        raise ValueError('Truncated event')
    return body


class Sender:
    def __init__(self, token, *, rate_limit=None):
        self.token = token
        self.rate_limit = rate_limit if rate_limit is not None else SendRateLimit()
        # No system proxy, redirect following, arbitrary URL or arbitrary action.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def send(self, result, *, exempt=False):
        """Send one validated reply. `exempt` skips the shared send interval.

        Only the /apply notices use it, so a grant, a refusal and an expiry
        notice are never delayed or dropped by the chat rate limiter.
        """
        if STOP_FILE.exists():
            return False
        if not isinstance(result, dict) or result.get('action') != 'send_group_msg':
            raise PermissionError('Only send_group_msg is permitted')
        params = result.get('params', {})
        if set(params) != {'group_id', 'message'} or identifier(params.get('group_id')) not in ALLOWED_GROUPS:
            raise PermissionError('Destination must be the authorized test group')
        message = params.get('message')
        if (not isinstance(message, list) or len(message) != 1
                or not isinstance(message[0], dict)
                or message[0].get('type') != 'text'
                or not isinstance(message[0].get('data'), dict)):
            raise PermissionError('Only literal text is permitted')
        # Rebuild the payload; never forward user-supplied API fields.
        safe = group_reply(params['group_id'], message[0]['data'].get('text'))['params']
        request = urllib.request.Request(API_URL,
            data=json.dumps(safe, ensure_ascii=False).encode('utf-8'), method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})
        if not exempt and not self.rate_limit.acquire():
            return False
        with self.opener.open(request, timeout=10) as response:
            status = json.load(response)
        if status.get('status') != 'ok' or status.get('retcode') != 0:
            raise RuntimeError('Send failed')
        return True

    def send_image(self, group_id, cached_path, *, prefer_sticker=True):
        """Prefer image subtype 1; use plain image only after explicit rejection."""
        if STOP_FILE.exists():
            return False
        group = identifier(group_id)
        if group not in ALLOWED_GROUPS:
            raise PermissionError('Image destination is not allowed')
        root = (ROOT / 'data' / 'stickers').resolve()
        path = Path(cached_path).resolve()
        if path.parent != root / group or path.suffix[1:] not in ('png','gif','jpg','webp'):
            raise PermissionError('Image is outside the managed sticker cache')
        raw = path.read_bytes()
        if (not 0 < len(raw) <= MAX_IMAGE_BYTES or
                image_format(raw) != path.suffix[1:] or
                hashlib.sha256(raw).hexdigest() != path.stem):
            raise PermissionError('Cached sticker failed integrity validation')
        data = {'file':str(path)}
        if prefer_sticker:
            data['sub_type'] = 1
        safe = {'group_id':int(group), 'message':[
            {'type':'image','data':data}]}
        request = urllib.request.Request(API_URL,
            data=json.dumps(safe,ensure_ascii=False).encode('utf-8'),method='POST',
            headers={'Content-Type':'application/json',
                     'Authorization':'Bearer ' + self.token})
        if not self.rate_limit.acquire():
            return False
        with self.opener.open(request,timeout=10) as response:
            status = json.load(response)
        if status.get('status') != 'ok' or status.get('retcode') != 0:
            if prefer_sticker:
                raise StickerSubtypeRejected('Sticker image subtype rejected')
            raise RuntimeError('Image send failed')
        return True


def broadcast_restart(sender, groups, *, deadline=RESTART_DEADLINE, clock=time.monotonic):
    """Announce the restart to every authorized group without blocking startup.

    NapCat may still be logging in, and the shared send interval gates each
    group, so every destination is retried until the deadline.
    """
    end = clock() + deadline
    for group in sorted(groups):
        while clock() < end:
            if STOP_FILE.exists():
                return
            try:
                if sender.send(group_reply(group, RESTART_NOTICE)):
                    log(f'Authorized group {group}: restart notice sent.', flush=True)
                    break
            except Exception:
                pass  # NapCat not ready yet; retry until the deadline.
            time.sleep(1.0)


def sweep_expiry(sender, connections, *, clock=time.time):
    """One pass: announce every expired grant, then forget only the delivered ones.

    `connections` maps a group id to a raw sqlite3 connection owned by the
    calling thread; returns the (group, user) pairs that were announced.
    """
    announced = []
    now = clock()
    for group, connection in connections.items():
        try:
            expired = expired_whitelist(connection, now)
        except sqlite3.Error:
            continue  # Retry on the next tick rather than dying.
        delivered = []
        for user_id in expired:
            try:
                notice = f'{user_id}（QQ号），{EXPIRY_NOTICE}'
                if sender.send(group_reply(group, notice), exempt=True):
                    delivered.append(user_id)
                    announced.append((group, user_id))
                    log(f'Authorized group {group}: whitelist expiry notice sent.', flush=True)
            except Exception:
                pass  # Keep the row so the next tick tries again.
        if delivered:
            try:
                with connection:
                    connection.executemany('DELETE FROM whitelist WHERE user_id=?',
                                           ((user_id,) for user_id in delivered))
            except sqlite3.Error:
                pass  # A duplicate notice is preferable to a swallowed one.
    return announced


def watch_expiry(sender, *, interval=EXPIRY_INTERVAL, clock=time.time):
    """Announce expired whitelist grants.

    Runs in its own thread, so it opens its own connection per group database:
    a sqlite3 connection must never be shared across threads.
    """
    connections = {}
    try:
        for group, filename in GROUP_DATABASES.items():
            connections[group] = sqlite3.connect(str(ROOT / 'data' / filename), timeout=5)
        while not STOP_FILE.exists():
            sweep_expiry(sender, connections, clock=clock)
            time.sleep(interval)
    finally:
        for connection in connections.values():
            connection.close()


def make_handler(bots, sender, token, stickers=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log event bodies, other conversations, credentials or request paths.

        def do_POST(self):
            self.connection.settimeout(5)
            if self.path != '/events':
                self.send_error(404)
                return
            try:
                body = read_event_body(self.headers, self.rfile)
                expected = 'sha1=' + hmac.new(token.encode(), body, hashlib.sha1).hexdigest()
                if not hmac.compare_digest(expected, self.headers.get('x-signature', '')):
                    log('Event authentication rejected; no message processed.', flush=True)
                    self.send_error(403)
                    return
                event = json.loads(body)
                if not isinstance(event, dict):
                    raise ValueError()
                # No quick operations in responses: they can target the originating chat.
                group_id = identifier(event.get('group_id'))
                bot = bots.get(group_id) if group_id in ALLOWED_GROUPS else None
                if (not STOP_FILE.exists() and event.get('post_type') == 'message'
                        and event.get('message_type') == 'group'
                        and bot is not None
                        and identifier(event.get('self_id')) == bot.self_id):
                    timestamp = event_time_seconds(event.get('time'))
                    if timestamp is not None:
                        event['time'] = timestamp
                        previous = bot.previous
                        before = bot.db.total_changes
                        result = bot.handle(event)
                        # Capture is a side effect: gate 00 and every enforced
                        # block must run before any image download or cache write.
                        if (stickers is not None and bot.last_gate_decision is not None
                                and not bot.last_gate_decision.blocked
                                and bot.active_ban(identifier(event.get('user_id')),
                                                   time.time()) is None
                                and sticker_segment(event) is not None):
                            stickers.capture(event,previous)
                        if result and result.get('quality_notice'):
                            log('Authorized group ' + group_id + ': ' +
                                result['quality_notice']['category'] +
                                ' gate blocked training.', flush=True)
                        if bot.db.total_changes > before:
                            log(f'Authorized group {group_id}: event learned/handled.', flush=True)
                        else:
                            log(f'Authorized group {group_id}: self, duplicate or unsupported event ignored.', flush=True)
                        if result:
                            try:
                                if identifier(result.get('params', {}).get('group_id')) != group_id:
                                    raise PermissionError('Cross-group response blocked')
                                user = identifier(event.get('user_id'))
                                if user and bot.active_ban(user,time.time()) is not None:
                                    if result.get('gate_notice'):
                                        bot.quality.settle_gate_notice(
                                            result['gate_notice'],sent=False)
                                    if result.get('score_query'):
                                        bot.quality.settle_score_query(result['score_query'],sent=False)
                                    log(f'Authorized group {group_id}: gate 00 suppressed outbound.',flush=True)
                                    self.send_response(200)
                                    self.send_header('Content-Type','application/json')
                                    self.end_headers()
                                    self.wfile.write(b'{}')
                                    return
                                sent = sender.send(result, exempt=bool(result.get('exempt')))
                                if result.get('quality_notice'):
                                    bot.quality.settle_notice(result['quality_notice'],sent=sent)
                                if result.get('gate_notice'):
                                    bot.quality.settle_gate_notice(result['gate_notice'],sent=sent)
                                if result.get('score_query'):
                                    bot.quality.settle_score_query(result['score_query'],sent=sent)
                                if sent:
                                    log(f'Authorized group {group_id}: reply sent.', flush=True)
                                    if stickers is not None and result.get('model_generated') is True:
                                        segments = event.get('message')
                                        if isinstance(segments,list) and segments and all(
                                                isinstance(s,dict) and s.get('type')=='text'
                                                and isinstance(s.get('data',{}).get('text'),str)
                                                for s in segments):
                                            prompt = ''.join(s['data']['text'] for s in segments)
                                            context = (previous if previous is not None and
                                                       0 <= timestamp-previous[1] <= 60 else None)
                                            outgoing = result.get('params',{}).get('message',[])
                                            generated = (outgoing[0].get('data',{}).get('text')
                                                         if len(outgoing)==1 and
                                                         isinstance(outgoing[0],dict) and
                                                         outgoing[0].get('type')=='text' else None)
                                            if isinstance(generated,str) and generated:
                                                stickers.after_text(group_id,prompt,context,generated)
                                else:
                                    log(f'Authorized group {group_id}: reply skipped (5-second limit or stopped).', flush=True)
                            except Exception as error:
                                if result.get('quality_notice'):
                                    bot.quality.settle_notice(result['quality_notice'],
                                                              sent=False,uncertain=True)
                                if result.get('gate_notice'):
                                    bot.quality.settle_gate_notice(result['gate_notice'],
                                        sent=False,uncertain=True)
                                if result.get('score_query'):
                                    bot.quality.settle_score_query(result['score_query'],
                                        sent=False,uncertain=True)
                                # No retry on unknown send state: avoid duplicate sends.
                                log('Send failed or uncertain; not retried: ' + describe(error))
                    else:
                        log(f'Authorized group {group_id}: invalid timestamp rejected.', flush=True)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')
            except (ValueError, TypeError, UnicodeError):
                self.send_error(400)
            except Exception:
                # A storage/logic fault stops sending instead of continuing uncontrolled.
                STOP_FILE.parent.mkdir(exist_ok=True)
                STOP_FILE.touch()
                self.send_error(503)
    return Handler


def sweep_repeated_memory(bots):
    """Run on the event-loop thread, after training commits; never sends QQ data."""
    for group,bot in bots.items():
        if STOP_FILE.exists():
            return
        if group not in ALLOWED_GROUPS or bot.group_id!=group:
            raise PermissionError('Cleanup group does not match the bot')
        try:
            report=autoclean_memory(bot.db,group,ROOT/'data'/GROUP_DATABASES[group],
                                   ROOT/'data'/'backups'/'memory-clean',limit=20)
            if report.get('sentences_removed'):
                bot.previous=None
                bot.intelligence.reset_context()
                log(f'Authorized group {group}: strong-repeat memories cleaned: '
                    f'{report["sentences_removed"]}; snapshot saved.',flush=True)
        except (ValueError,OSError,sqlite3.Error) as error:
            log('Strong-repeat cleanup deferred: '+type(error).__name__,flush=True)


def main():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    if set(config.get('allowed_groups', [])) != ALLOWED_GROUPS or len(config.get('token', '')) < 32:
        raise SystemExit('Invalid local connection configuration')
    if STOP_FILE.exists():
        raise SystemExit('STOP exists; live bot remains disabled')
    # Optional per-group trigram blend ceiling. It defaults to 0.0 for every
    # group, and this project never writes the key itself: enabling the new
    # probability is an operator decision made in connection.json.
    try:
        blend = blend_limits(config)
    except ValueError as error:
        raise SystemExit(f'Invalid generate blend configuration: {error}')
    (ROOT / 'data').mkdir(exist_ok=True)
    quality_path = ROOT / 'data' / 'quality.sqlite3'
    if quality_path.exists():
        backup_path = (ROOT / 'data' / 'backups' /
            ('quality-before-start-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3'))
        backup_path.parent.mkdir(parents=True,exist_ok=True)
        source = sqlite3.connect(quality_path)
        try:
            target = sqlite3.connect(backup_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    quality = QualityLedger(quality_path)
    bots = {
        group: Bot(str(ROOT / 'data' / filename), self_id=config['self_id'],
                   group_id=group, generate_blend_max=blend[group],
                   generate_refine_probability=GENERATE_REFINE_PROBABILITY,
                   keyword_reply_probability=KEYWORD_REPLY_PROBABILITY.get(group, 0.0),
                   quality=quality)
        for group, filename in GROUP_DATABASES.items()
    }
    for bot in bots.values():
        bot.attach_peers(bots)  # Lets /dstx lift bans in every group at once.
    sender = Sender(config['token'])
    library = StickerLibrary(ROOT / 'data' / 'stickers')
    stickers = StickerService(library,sender,stop_file=STOP_FILE,logger=log)
    server = HTTPServer(('127.0.0.1', 5702), make_handler(bots, sender, config['token'],stickers))
    server.timeout = 1
    log('QBot ready: test 312579123, production 1050045834, third 1006808801, fourth 929618496; all other destinations blocked.', flush=True)
    log(f'Global send interval: {MIN_SEND_INTERVAL:g} seconds; suppressed replies are not queued.', flush=True)
    log('Generate refinement: 30% branch, prefix-only stopping and at most 3 paths.',flush=True)
    log('Keyword replies: ' + (', '.join(f'{g} {p:.0%}' for g, p in
        sorted(KEYWORD_REPLY_PROBABILITY.items())) or 'off') + ' of generate replies.', flush=True)
    if ANNOUNCE_RESTART:
        threading.Thread(target=broadcast_restart, args=(sender, bots), daemon=True).start()
    threading.Thread(target=watch_expiry, args=(sender,), daemon=True).start()
    # The dashboard is deliberately NOT started here: it runs as its own process
    # (start-admin.cmd) so that panel work never restarts the bot and never
    # blackouts group messages.
    next_cleanup=time.monotonic()+1.0
    try:
        while not STOP_FILE.exists():
            server.handle_request()
            if time.monotonic()>=next_cleanup:
                sweep_repeated_memory(bots)
                next_cleanup=time.monotonic()+AUTO_LOOP_CLEAN_INTERVAL
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for bot in bots.values():
            bot.close()
        quality.close()


if __name__ == '__main__':
    main()
