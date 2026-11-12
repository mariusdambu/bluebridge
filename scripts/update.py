"""Update the audited v2.4 install with a private snapshot and selective rollback."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

from backup import backup, verify

BASE = Path(__file__).resolve().parents[1]
CORE = Path('/opt/bluebridge')
CONFIG = Path('/etc/bluebridge/config.json')
DASH = Path('/opt/family-plex/bluebridge_dashboard.py')
FILES = {CORE / name: BASE / 'src' / name for name in ('core.py','controller.py','player.py')}
FILES[DASH] = BASE / 'src/bluebridge_dashboard.py'
for unit in ('bluebridge','bluealsa-aplay','bluealsa'):
    dropin = Path('/etc/systemd/system') / (unit+'.service.d') / '90-bluebridge-recovery.conf'
    FILES[dropin] = BASE / 'systemd/v2.4.1' / ('restart-limit.conf' if unit=='bluealsa' else 'watchdog.conf')
AUDIO = ('bluealsa.service', 'bluealsa-aplay.service', 'bluebridge.service')
UNITS = (*AUDIO, 'plexserver-dashboard.service')

def run(*args):
    subprocess.run(args, check=True, timeout=60)

def install(path, data, mode, uid=0, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.chown(tmp, uid, gid)
            tmp.chmod(mode)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

def activate():
    run('systemctl','daemon-reload')
    run('systemd-analyze','--man=no','verify',*AUDIO)
    run('systemctl','restart',*UNITS)

def rollback(archive):
    verify(archive)
    with tarfile.open(archive) as tar:
        members = {m.name:m for m in tar.getmembers()}
        for path in (*FILES, CONFIG):
            member = members.get(str(path).lstrip('/'))
            if member and member.isfile():
                install(path, tar.extractfile(member).read(), member.mode, member.uid, member.gid)
            elif member is None and path in FILES and path.suffix == '.conf':
                path.unlink(missing_ok=True)
            else:
                raise RuntimeError('Missing original: '+str(path))
    activate()

def update():
    state = json.loads(Path('/run/bluebridge/state.json').read_text())
    if state.get('clients') or state.get('stream_active') or not 0 <= time.time()-state['updated'] < 12:
        raise RuntimeError('Wait for BlueBridge to be idle and observable')
    settings = json.loads(CONFIG.read_text())
    if not settings.get('preferred_adapter'):
        original = (CORE/'controller.py').read_text()
        old = re.search(r"^PREFERRED_ADAPTER = '([0-9A-F:]{17})'",original,re.M)
        if not old:
            raise RuntimeError('Set preferred_adapter from the previous installation before upgrading')
        settings['preferred_adapter'] = old[1]
    payload = {path:source.read_bytes() for path,source in FILES.items()}
    for path,data in payload.items():
        if path.suffix=='.py':
            compile(data,str(path),'exec')
    archive = backup('/root/bluebridge-recovery')
    print('Rollback archive: '+str(archive),flush=True)
    try:
        info = CONFIG.stat()
        install(CONFIG, (json.dumps(settings,indent=2)+'\n').encode(), info.st_mode & 0o777, info.st_uid, info.st_gid)
        for path,data in payload.items():
            install(path,data,0o644)
        activate()
        for _ in range(12):
            time.sleep(2)
            current = json.loads(Path('/run/bluebridge/state.json').read_text()) if Path('/run/bluebridge/state.json').is_file() else {}
            if (current.get('service',{}).get('state')=='online'
                    and current.get('pairing',{}).get('open')
                    and current.get('bluetooth_adapter',{}).get('label')==state.get('bluetooth_adapter',{}).get('label')):
                run('systemctl','is-active',*UNITS)
                return
        raise RuntimeError('BlueBridge readiness check did not pass')
    except Exception:
        rollback(archive)
        raise

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Root required')
    if args.rollback:
        rollback(args.rollback)
    else:
        update()
