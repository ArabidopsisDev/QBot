"""Read local login state or explicitly refresh its QR code; never sends QQ messages."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-qr', action='store_true')
    args = parser.parse_args()
    config = json.loads((ROOT / 'vendor/napcat/napcat/config/webui.json').read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def post(path, body, credential=None):
        headers = {'Content-Type':'application/json'}
        if credential:
            headers['Authorization'] = 'Bearer ' + credential
        request = urllib.request.Request('http://127.0.0.1:6099/api/' + path,
            data=json.dumps(body).encode(), headers=headers)
        with opener.open(request, timeout=5) as response:
            return json.load(response)
    login = post('auth/login', {'hash':hashlib.sha256((config['token']+'.napcat').encode()).hexdigest()})
    credential = login['data']['Credential']
    if args.refresh_qr:
        result = post('QQLogin/RefreshQRcode', {}, credential)
        print('QR refresh requested; result code:', result.get('code'))
    result = post('QQLogin/CheckLoginStatus', {}, credential)
    data = result.get('data', {})
    print(json.dumps({key:data.get(key) for key in
        ('isLogin','isOffline','loginPhase','qrLoginAccepted','coreReady')}, ensure_ascii=False))
    if data.get('loginError'):
        print('QQ reports a login error; inspect the local login UI.')
    if data.get('isLogin'):
        expected = json.loads((ROOT / 'runtime/connection.json').read_text(encoding='utf-8'))['self_id']
        actual = post('QQLogin/GetQQLoginInfo', {}, credential).get('data', {}).get('uin')
        print('Account matches configured bot:', str(actual) == expected)


if __name__ == '__main__':
    main()
