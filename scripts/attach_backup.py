"""Attach the BlueBridge snapshot to the existing Family Plex backup."""
from pathlib import Path
import sys

ANCHOR = "        status = pathlib.Path('/var/lib/plexserver-backup/status.env').read_text()"
HOOK = """        # BlueBridge snapshot runs after the application writers are restored.
        try:
            mounted(root, config['backup_uuid'])
            subprocess.run(['/usr/bin/python3', '/opt/bluebridge/backup.py',
                            '--destination', str(destination / 'server-config/bluebridge')],
                           check=True, timeout=120)
        except Exception as error:
            print('BlueBridge private snapshot failed: ' + str(error), file=sys.stderr)
"""

def integrate(source):
    if HOOK in source:
        return source
    if source.count(ANCHOR) != 1:
        raise ValueError('Unexpected backup wrapper layout')
    patched = source.replace(ANCHOR, HOOK + ANCHOR, 1)
    compile(patched, 'legacy_backup.py', 'exec')
    return patched

if __name__ == '__main__':
    print(integrate(Path(sys.argv[1]).read_text()), end='')
