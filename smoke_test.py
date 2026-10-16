"""Explicit live smoke test: one text message, only in the authorized test group."""
import json
import urllib.request
from live import CONFIG, Sender
from qbot import TEST_GROUP, group_reply


def main():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def read(action, params):
        if action not in ('get_login_info', 'get_group_info'):
            raise PermissionError('Unsupported read action')
        request = urllib.request.Request('http://127.0.0.1:5701/' + action,
            data=json.dumps(params).encode(), headers={'Content-Type':'application/json',
                'Authorization':'Bearer ' + config['token']})
        with opener.open(request, timeout=10) as response:
            result = json.load(response)
        if result.get('status') != 'ok' or result.get('retcode') != 0:
            raise RuntimeError('Local QQ API check failed')
        return result['data']
    info = read('get_login_info', {})
    if str(info['user_id']) != config['self_id']:
        raise RuntimeError('Unexpected logged-in account; send refused')
    group = read('get_group_info', {'group_id':int(TEST_GROUP)})
    if str(group['group_id']) != TEST_GROUP:
        raise RuntimeError('Unexpected target group; send refused')
    sent = Sender(config['token']).send(group_reply(TEST_GROUP,
        'QBot 测试群连接正常。测试群 312579123、正式群 1050045834、群 1006808801、群 929618496 已启用，其余群和私聊禁止发送。支持 /model match 和 /model generate。'))
    print('Single authorized-group smoke message sent:', sent)


if __name__ == '__main__':
    main()
