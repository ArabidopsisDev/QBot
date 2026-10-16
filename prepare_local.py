"""Configure the pinned isolated NapCat installation without exposing credentials."""
import hashlib
import json
import re
import secrets
from pathlib import Path
from qbot import ALLOWED_GROUPS

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor' / 'napcat' / 'napcat'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    with (ROOT / 'qq.txt').open(encoding='utf-8-sig') as credentials:
        account = credentials.readline().strip()
    if not re.fullmatch(r'[1-9][0-9]{4,12}', account):
        raise SystemExit('qq.txt first line must contain the account number')
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    path = runtime / 'connection.json'
    connection = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'self_id': account, 'allowed_groups': sorted(ALLOWED_GROUPS), 'token': secrets.token_hex(32)}
    if connection['self_id'] != account or set(connection.get('allowed_groups', [])) != ALLOWED_GROUPS:
        raise SystemExit('Existing configuration account/group mismatch')
    write_json(path, connection)
    token = connection['token']
    config = VENDOR / 'config'
    base = json.loads((config / 'napcat.json').read_text(encoding='utf-8'))
    base.update(fileLog=False, consoleLog=False, fileLogLevel='error', consoleLogLevel='error', autoTimeSync=False)
    write_json(config / 'napcat.json', base)
    write_json(config / f'napcat_{account}.json', base)
    web_path = config / 'webui.json'
    web = json.loads(web_path.read_text(encoding='utf-8')) if web_path.exists() else {}
    web.update(host='127.0.0.1', port=6099, token=web.get('token') or secrets.token_hex(32),
               autoLoginAccount=account, disableWebUI=False)
    write_json(web_path, web)
    write_json(config / f'onebot11_{account}.json', {
        'network': {
            'httpServers': [{'name':'qbot-api', 'enable':True, 'host':'127.0.0.1',
                'port':5701, 'enableCors':False, 'enableWebsocket':False,
                'messagePostFormat':'array', 'token':token, 'debug':False}],
            'httpClients': [{'name':'qbot-events', 'enable':True,
                'url':'http://127.0.0.1:5702/events', 'messagePostFormat':'array',
                'reportSelfMessage':False, 'token':token, 'debug':False}],
            'httpSseServers':[], 'websocketServers':[], 'websocketClients':[], 'plugins':[]},
        'musicSignUrl':'', 'enableLocalFile2Url':False, 'parseMultMsg':False,
        'imageDownloadProxy':''})
    module = VENDOR / 'napcat.mjs'
    source = module.read_text(encoding='utf-8')
    signature = '  async sendMsg(e, n, r = 1e4) {\n'
    guard = ('    // QBOT_FIXED_GROUP_GUARD: do not remove.\n'
             '    if (e.chatType !== Te.KCHATTYPEGROUP || !["312579123", "1050045834", "1006808801", "929618496"].includes(String(e.peerUid)))\n'
             '      throw new Error("QBot blocked a non-test-group send");\n')
    if 'QBOT_FIXED_GROUP_GUARD' not in source:
        if source.count(signature) != 1:
            raise SystemExit('Unrecognized NapCat build; refusing to patch or start')
        module.with_suffix('.mjs.original').write_text(source, encoding='utf-8')
        source = source.replace(signature, signature + guard)
        module.write_text(source, encoding='utf-8')
    elif signature + guard not in source:
        raise SystemExit('NapCat guard mismatch')
    write_json(runtime / 'integrity.json', {'version':'4.18.28',
        'napcat_sha256':hashlib.sha256(module.read_bytes()).hexdigest()})
    print('Local configuration ready. Password was not read. Message logs disabled. Fixed-group guard installed.')


if __name__ == '__main__':
    main()
