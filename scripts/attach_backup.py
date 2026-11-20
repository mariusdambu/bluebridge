"""Integrate BlueBridge with the existing Family Plex backup and dashboard."""
import argparse
from pathlib import Path

ANCHOR = "        status = pathlib.Path('/var/lib/plexserver-backup/status.env').read_text()"
OLD_HOOK = """        # BlueBridge snapshot runs after the application writers are restored.
        try:
            mounted(root, config['backup_uuid'])
            subprocess.run(['/usr/bin/python3', '/opt/bluebridge/backup.py',
                            '--destination', str(destination / 'server-config/bluebridge')],
                           check=True, timeout=120)
        except Exception as error:
            print('BlueBridge private snapshot failed: ' + str(error), file=sys.stderr)
"""
SYNC = "        subprocess.run(['sync', '-f', str(destination)], check=True)\n"
FINAL_STATUS = """        # Keep last-success.json as the Plex mirror marker. Publish the
        # combined dashboard result only after the mirror has been committed.
        updated = dt.datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')
        if bluebridge_ok:
            state, label = 'ok', 'Correcto'
            detail = 'Copia completada, incluido BlueBridge verificado.'
        else:
            state, label = 'warn', 'BlueBridge no respaldado'
            detail = ('Copia del resto completada. BlueBridge no se pudo respaldar o verificar. '
                      'Consulta el registro de plexserver-backup.service.')
        status_path = pathlib.Path('/var/lib/plexserver-backup/status.env')
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(f'state={state}\\nlabel={label}\\ndetail={detail}\\n'
                             f'updated={updated}\\nbluebridge={"ok" if bluebridge_ok else "error"}\\n')
        temporary.replace(status_path)
"""


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('Unexpected layout near ' + old[:75])
    return source.replace(old, new, 1)


def integrate(source):
    if FINAL_STATUS in source:
        return source
    if OLD_HOOK not in source:
        source = replace_once(source, ANCHOR, OLD_HOOK + ANCHOR)
    hook = OLD_HOOK.replace('        try:\n', '        bluebridge_ok = False\n        try:\n', 1)
    hook = hook.replace('        except Exception', '            bluebridge_ok = True\n        except Exception', 1)
    source = replace_once(source, OLD_HOOK, hook)
    source = replace_once(source, SYNC, SYNC + FINAL_STATUS)
    compile(source, 'legacy_backup.py', 'exec')
    return source


def integrate_dashboard(source):
    if 'def backup_running():' in source and "status.get(\"bluebridge\")" in source:
        return source
    check = 'run(["systemctl", "is-active", "plexserver-backup.service"], timeout=1) == "active"'
    if source.count(check) != 3:
        raise ValueError('Unexpected backup activity checks')
    source = source.replace(check, 'backup_running()')
    source = replace_once(source, "running = subprocess.run(['systemctl', 'is-active', '--quiet', 'plexserver-backup.service'], timeout=3).returncode == 0", 'running = backup_running()')
    source = replace_once(source, 'def backup_status():', '''def backup_running():
    # A oneshot service remains activating for the duration of ExecStart.
    return run(["systemctl", "show", "-p", "ActiveState", "--value",
                "plexserver-backup.service"], timeout=1) in {"activating", "active"}


def backup_status():''')
    source = replace_once(source, '    status = read_status_file()\n    usb = usb_status()', '''    status = read_status_file()
    if status.get("state") == "error" or (
            (status.get("state"), status.get("bluebridge")) in {("ok", "ok"), ("warn", "error")}):
        return {key: status.get(key, "") for key in ("state", "label", "detail")}
    usb = usb_status()''')
    source = replace_once(source, '        "state": "ok" if ok else "warn",\n        "label": "Correcto" if ok else "Revisar",\n        "detail": started or last_line,', '''        "state": "warn",
        "label": "BlueBridge sin verificar" if ok else "Revisar",
        "detail": ("La copia anterior del NAS no registra el resultado de BlueBridge."
                   if ok else started or last_line),''')
    compile(source, 'dashboard.py', 'exec')
    return source


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashboard', action='store_true')
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    patch = integrate_dashboard if args.dashboard else integrate
    print(patch(args.source.read_text()), end='')
