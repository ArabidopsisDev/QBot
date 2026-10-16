"""Explicit Windows launcher test with isolated, offline dummy processes only."""
import json
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    # Do not interfere with an existing real service or unrelated listeners.
    for port in (5701,5702,6099):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1',port))
            except OSError:
                raise SystemExit('Required port occupied; offline launcher test skipped')
    parent=(ROOT/'runtime').resolve()
    with tempfile.TemporaryDirectory(prefix='QBot launcher test ',dir=parent) as folder:
        root=Path(folder).resolve()
        assert root.parent==parent  # All recursive cleanup stays inside runtime.
        (root/'runtime').mkdir()
        (root/'vendor/napcat').mkdir(parents=True)
        for name in ('start-live.ps1','stop-live.ps1','status-live.ps1','service-common.ps1'):
            shutil.copy2(ROOT/name,root/name)
        shutil.copy2(ROOT/'vendor/napcat/node.exe',root/'vendor/napcat/node.exe')
        (root/'check_ready.py').write_text("print('Offline fixture preflight')\n",encoding='utf-8')
        (root/'login_status.py').write_text("print('Offline fixture: isLogin=true coreReady=true')\n",encoding='utf-8')
        guard=root/'test_napcat_guard.cjs'
        guard.write_text('process.exit(0);\n',encoding='utf-8')
        (root/'runtime/STOP').touch()
        marker=root/'learning-data.keep'
        marker.write_bytes(b'preserve me')
        (root/'live.py').write_text('''import socket
from pathlib import Path
stop=Path(__file__).parent/'runtime/STOP'
with socket.socket() as server:
    server.bind(('127.0.0.1',5702))
    server.listen()
    server.settimeout(.2)
    while not stop.exists():
        try:
            connection,_=server.accept()
            connection.close()
        except TimeoutError:
            pass
''',encoding='utf-8')
        entry=root/'vendor/napcat/index.js'
        entry.write_text('''const net=require('net');
const child=require('child_process');
if(process.argv.includes('worker')) {
  net.createServer(s=>s.end()).listen(6099,'127.0.0.1');
} else {
  child.fork(__filename,['worker'],{stdio:'ignore',windowsHide:true});
  net.createServer(s=>s.end()).listen(5701,'127.0.0.1');
}
''',encoding='utf-8')
        def run(name,*args,ok=True):
            result=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy',
                'Bypass','-File',str(root/name),*args],capture_output=True,text=True,
                errors='replace',timeout=90)
            if (result.returncode==0)!=ok:
                raise AssertionError(result.stdout+'\n'+result.stderr)
            return result.stdout
        try:
            assert 'CHECK ONLY' in run('start-live.ps1','-CheckOnly')
            assert (root/'runtime/STOP').exists()
            guard.write_text('process.exit(1);\n',encoding='utf-8')
            run('start-live.ps1',ok=False)
            assert (root/'runtime/STOP').exists()
            guard.write_text('process.exit(0);\n',encoding='utf-8')
            run('start-live.ps1')
            assert not (root/'runtime/STOP').exists()
            before=json.loads((root/'runtime/processes.json').read_text(encoding='utf-8-sig'))
            assert 'Already running' in run('start-live.ps1')
            after=json.loads((root/'runtime/processes.json').read_text(encoding='utf-8-sig'))
            assert before==after
            assert 'local processes are running' in run('status-live.ps1')
            run('stop-live.ps1')
            assert 'STOPPED' in run('status-live.ps1')
            run('stop-live.ps1')
            assert marker.read_bytes()==b'preserve me'
            entry.write_text('process.exit(1);\n',encoding='utf-8')
            run('start-live.ps1',ok=False)
            assert (root/'runtime/STOP').exists()
            assert 'STOPPED' in run('status-live.ps1')
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1',5702))
                occupied.listen()
                run('start-live.ps1',ok=False)
                assert occupied.getsockname()[1]==5702
            print('PASS: check-only, failed preflight, start, duplicate prevention, status, stop, repeat stop, data preservation, failed-start cleanup, occupied port.')
        finally:
            run('stop-live.ps1')
            assert 'STOPPED' in run('status-live.ps1')
            assert root.resolve().parent==parent
    print('Only offline dummy processes were used. Real QQ service remains stopped.')


if __name__=='__main__':
    main()
