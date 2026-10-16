import hashlib
import json
from pathlib import Path
from qbot import ALLOWED_GROUPS

if ALLOWED_GROUPS != frozenset(('312579123', '1050045834', '1006808801', '929618496')):
    raise SystemExit('Send whitelist differs from the four explicitly authorized groups')

import moderation  # noqa: E402  (checked here so a broken screen blocks startup)

if moderation.match('几把') != ('IVb', '几把'):
    raise SystemExit('Sexual-vocabulary dictionary is missing or altered')
if len(moderation.GRADES) != 13 or moderation.GRADE_SECONDS[moderation.GRADES[-1]] != 86400:
    raise SystemExit('Ban grade scale is altered')
if moderation.match('好几把椅子') is not None or moderation.match('这个方案牛逼') is not None:
    raise SystemExit('Innocent-word exclusions are broken')
if any(moderation.match(word) is not None
       for word in ('草稿', '操场', '草莓', '吊车', '吊销', '吊坠', '吊兰')):
    raise SystemExit('Generalized characters must never be matched on their own')

root = Path(__file__).resolve().parent
vendor = root / 'vendor' / 'napcat' / 'napcat'
if (vendor / 'config' / 'napcat-update.json').exists():
    raise SystemExit('Pending NapCat update could replace the send guard; startup refused')
connection = json.loads((root / 'runtime' / 'connection.json').read_text(encoding='utf-8'))
integrity = json.loads((root / 'runtime' / 'integrity.json').read_text(encoding='utf-8'))
assert set(connection['allowed_groups']) == ALLOWED_GROUPS
assert hashlib.sha256((vendor / 'napcat.mjs').read_bytes()).hexdigest() == integrity['napcat_sha256'], 'NapCat build changed'
assert 'QBOT_FIXED_GROUP_GUARD' in (vendor / 'napcat.mjs').read_text(encoding='utf-8')
assert all((vendor.parent / name).is_file() for name in ('crypto.dll', 'ssl.dll', 'wrapper.node'))
web = json.loads((vendor / 'config' / 'webui.json').read_text(encoding='utf-8'))
assert web['host'] == '127.0.0.1' and web['port'] == 6099
assert len(web.get('token', '')) >= 32 and web['token'] != 'napcat', 'Avoid automatic WebUI token messages'
for name in ('napcat.json', f"napcat_{connection['self_id']}.json"):
    logging = json.loads((vendor / 'config' / name).read_text(encoding='utf-8'))
    assert logging['fileLog'] is False and logging['consoleLog'] is False
config = json.loads((vendor / 'config' / f"onebot11_{connection['self_id']}.json").read_text(encoding='utf-8'))
network = config['network']
assert len(network['httpServers']) == len(network['httpClients']) == 1
api, events = network['httpServers'][0], network['httpClients'][0]
assert api['host'] == '127.0.0.1' and api['port'] == 5701 and api['token'] == connection['token']
assert not api['enableWebsocket'] and not api['debug']
assert events['url'] == 'http://127.0.0.1:5702/events' and events['token'] == connection['token']
assert not events['debug'] and events['messagePostFormat'] == 'array'
assert not any(network[key] for key in ('httpSseServers', 'websocketServers', 'websocketClients', 'plugins'))
assert not list((vendor / 'plugins').glob('*/main.mjs')), 'Unexpected plugin'
print('Verified pinned NapCat build, fixed-group guard, loopback-only configuration.')
