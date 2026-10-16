"""Export, import and text-import a group's training data.

Names given to /save, /load and /train are validated against a strict pattern
before touching the filesystem, so a name can never escape the pre folder.
"""
import hashlib
import json
import re
import unicodedata
from pathlib import Path

# Letters, digits, underscore, hyphen and CJK only: no separators, no dots.
NAME_RE = re.compile(r'[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff-]{0,31}')
RESERVED = frozenset({'con', 'prn', 'aux', 'nul'} |
                     {f'com{i}' for i in range(1, 10)} |
                     {f'lpt{i}' for i in range(1, 10)})
DOCUMENT = 'data.json'
FORMAT_VERSION = 1

HEADER = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*?)[(<]([^()<>]+)[)>]$')
PLACEHOLDER = re.compile(r'\[(?:图片|表情|动画表情|视频|语音|文件|红包|转账|不支持的消息|QQ表情)\]')

TABLES = {
    'qa': ('INSERT OR REPLACE INTO qa VALUES (?,?,?)', 3),
    'chars': ('INSERT OR REPLACE INTO chars VALUES (?,?,?)', 3),
    'gen_trigrams': ('INSERT OR REPLACE INTO gen_trigrams VALUES (?,?,?,?)', 4),
    'intel_memory': ('INSERT OR REPLACE INTO intel_memory VALUES (?,?,?,?)', 4),
    'intel_terms': ('INSERT OR REPLACE INTO intel_terms VALUES (?,?,?)', 3),
    'intel_links': ('INSERT OR REPLACE INTO intel_links VALUES (?,?,?,?)', 4),
}
# Restored in this order; gen_trigrams sits next to the table it derives from.
RESTORE_ORDER = ('qa', 'chars', 'gen_trigrams', 'intel_memory', 'intel_terms', 'intel_links')
PRETRAINING = ('INSERT OR REPLACE INTO pretraining VALUES (?,?)', 2)
GEN_TRIGRAMS_KEY = 'gen_trigrams'


def plain_name(value, *, strip_txt=False):
    """Validate one folder or file name; reject anything that could escape."""
    if not isinstance(value, str):
        raise ValueError('名称无效')
    candidate = value.strip()
    if strip_txt and candidate.lower().endswith('.txt'):
        candidate = candidate[:-4]
    if not NAME_RE.fullmatch(candidate) or candidate.lower() in RESERVED:
        raise ValueError('名称无效：只允许中英文、数字、下划线和连字符，长度 1-32')
    return candidate


def folder_for(root, name):
    """Resolve pre/<name>, refusing anything that is not a plain folder name."""
    return Path(root) / plain_name(name)


def text_folder_for(root):
    return Path(root) / 'text'


def source_file_for(root, reference):
    """Resolve a /train reference to its .txt file.

    Accepts the nested layout `文件夹/文件名` and, for files sitting directly in
    pre/text, the flat `文件名` form. A typed .txt suffix is tolerated.
    """
    if not isinstance(reference, str):
        raise ValueError('名称无效')
    parts = reference.strip().split('/')
    if len(parts) == 1:
        return text_folder_for(root) / (plain_name(parts[0], strip_txt=True) + '.txt')
    if len(parts) == 2:
        return (text_folder_for(root) / plain_name(parts[0])
                / (plain_name(parts[1], strip_txt=True) + '.txt'))
    raise ValueError('名称无效：最多只能有一层文件夹，例如 文件夹/文件')


def _visible(entry):
    """Hide only a trailing .txt; any other extension stays visible on disk."""
    return entry.name[:-4] if entry.suffix.lower() == '.txt' else entry.name


def list_sources(root):
    """Return the pre/text tree as display lines.

    Each folder line is followed by its own file names prefixed with '- ';
    files sitting directly in pre/text are listed last as bare names.
    """
    base = text_folder_for(root)
    entries = sorted(base.iterdir(), key=lambda entry: entry.name)
    lines = []
    for entry in entries:
        if not entry.is_dir():
            continue
        files = sorted(_visible(child) for child in entry.iterdir() if child.is_file())
        if not files:
            continue
        lines.append(entry.name)
        lines.extend(f'- {name}' for name in files)
    lines.extend(_visible(entry) for entry in entries if entry.is_file())
    return lines


def has_table(db, name):
    """True when the schema already carries `name` (older databases may not)."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def has_pretraining(db):
    return has_table(db, 'pretraining')


def _rows(db, sql):
    return [list(row) for row in db.execute(sql)]


def export_database(db, *, group_id, saved_at):
    return {
        'version': FORMAT_VERSION,
        'group_id': str(group_id),
        'saved_at': float(saved_at),
        'qa': _rows(db, 'SELECT q,a,w FROM qa ORDER BY q,a'),
        'chars': _rows(db, 'SELECT first,second,w FROM chars ORDER BY first,second'),
        GEN_TRIGRAMS_KEY: _rows(db, 'SELECT older,first,second,w FROM gen_trigrams '
                                    'ORDER BY older,first,second')
                          if has_table(db, GEN_TRIGRAMS_KEY) else [],
        'intel_memory': _rows(db, 'SELECT id,text,w,last_seen FROM intel_memory ORDER BY id'),
        'intel_terms': _rows(db, 'SELECT term,memory_id,weight FROM intel_terms ORDER BY memory_id,term'),
        'intel_links': _rows(db, 'SELECT qid,aid,w,last_seen FROM intel_links ORDER BY qid,aid'),
        'pretraining': _rows(db, 'SELECT id,summary FROM pretraining') if has_pretraining(db) else [],
    }


def write_backup(folder, payload):
    folder.mkdir(parents=True, exist_ok=True)
    document = folder / DOCUMENT
    document.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return document


def read_backup(folder):
    document = Path(folder) / DOCUMENT
    if not document.is_file():
        raise ValueError(f'未找到备份文件 {DOCUMENT}')
    try:
        payload = json.loads(document.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('备份文件无法解析') from error
    if not isinstance(payload, dict) or payload.get('version') != FORMAT_VERSION:
        raise ValueError('备份文件版本不受支持')
    return payload


def _validated(payload, key, width=None):
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f'备份文件字段 {key} 无效')
    if width is None:
        width = TABLES[key][1]
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            raise ValueError(f'备份文件字段 {key} 记录无效')
        if key == GEN_TRIGRAMS_KEY:
            older, first, second, weight = row
            if any(not isinstance(character, str) or len(character) != 1
                   for character in (older, first, second)):
                raise ValueError('备份文件字段 gen_trigrams 的字符必须是单个字符')
            if type(weight) is not int or weight <= 0:
                raise ValueError('备份文件字段 gen_trigrams 的权重必须是正整数')
    return rows


def clear_training_data(db):
    """Drop learned data only; bans, model mode and dedup history are preserved."""
    db.execute('DELETE FROM intel_links')
    db.execute('DELETE FROM intel_terms')
    db.execute('DELETE FROM intel_memory')
    db.execute('DELETE FROM qa')
    db.execute('DELETE FROM chars')
    if has_table(db, GEN_TRIGRAMS_KEY):
        db.execute('DELETE FROM gen_trigrams')
    if has_pretraining(db):
        db.execute('DELETE FROM pretraining')


def import_database(db, payload):
    """Replace every learned table with the backup contents.

    Everything is validated before the first delete, so a malformed document
    can never leave a half-restored model behind. Backups written before the
    trigram table existed simply carry no `gen_trigrams` field and restore it
    as empty; the format version is deliberately unchanged so they still load.
    """
    counts = {key: _validated(payload, key) for key in TABLES}
    pretraining = (_validated(payload, 'pretraining', PRETRAINING[1])
                   if payload.get('pretraining') else [])
    if not has_pretraining(db):
        db.execute('CREATE TABLE IF NOT EXISTS pretraining (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
    clear_training_data(db)
    for key in RESTORE_ORDER:
        statement = TABLES[key][0]
        db.executemany(statement, counts[key])
    db.executemany(PRETRAINING[0], pretraining)
    return {
        'qa': len(counts['qa']), 'chars': len(counts['chars']),
        GEN_TRIGRAMS_KEY: len(counts[GEN_TRIGRAMS_KEY]),
        'intel_memory': len(counts['intel_memory']), 'intel_terms': len(counts['intel_terms']),
        'intel_links': len(counts['intel_links']), 'pretraining': len(pretraining),
    }


def summarize(db):
    """Row counts for the feedback message after /save."""
    rows = db.execute('SELECT (SELECT COUNT(*) FROM qa),(SELECT COUNT(*) FROM chars),'
                      '(SELECT COUNT(*) FROM intel_memory)').fetchone()
    return {'qa': rows[0], 'chars': rows[1], 'intel_memory': rows[2]}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_export(text):
    """Parse a QQ text export into ordered (timestamp, user_id, message) records.

    A file without recognized headers is treated as one message per non-empty line.
    """
    records = []
    current = None
    for line in text.splitlines():
        match = HEADER.fullmatch(line)
        if match:
            if current is not None:
                records.append((*current[:2], '\n'.join(current[2]).strip('\n')))
            timestamp, _, user_id = match.groups()
            current = [timestamp, user_id, []]
        elif current is not None:
            current[2].append(line)
        elif line.strip():
            records.append(('', '', line.strip()))
    if current is not None:
        records.append((*current[:2], '\n'.join(current[2]).strip('\n')))
    return [record for record in records if record[2].strip()]


def read_text_source(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'未找到文件 {path.parent.name}/{path.name}，发送 /train list 查看可用数据集')
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('gb18030')
    records = parse_export(text)
    if not records:
        raise ValueError('文件中没有可训练的内容')
    return records, hashlib.sha256(raw).hexdigest()


def normalize_text(value):
    return unicodedata.normalize('NFKC', value)
