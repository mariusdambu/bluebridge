#!/usr/bin/env python3
"""Private, atomic BlueBridge snapshots. No service stops and no network I/O."""
import argparse
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import time

ROOTS = (
 '/opt/bluebridge', '/etc/bluebridge', '/var/lib/bluebridge',
 '/var/lib/bluetooth', '/var/lib/bluealsa', '/var/lib/alsa/asound.state',
 '/etc/bluetooth', '/etc/default/bluez-alsa', '/etc/asound.conf',
 '/etc/alsa/conf.d/20-bluealsa.conf', '/etc/udev/rules.d/71-bluebridge.rules',
 '/etc/systemd/system/bluebridge.service', '/etc/systemd/system/bluebridge.service.d',
 '/etc/systemd/system/bluealsa.service.d', '/etc/systemd/system/bluealsa-aplay.service.d',
 '/etc/systemd/system/bluetooth.service.d',
 '/opt/family-plex/bluebridge_dashboard.py', '/opt/family-plex/legacy_backup.py',
 '/opt/plexserver-dashboard/dashboard.py',
 '/usr/lib/firmware/brcm/BCM20702A1-0b05-17cb.hcd',
 '/usr/lib/firmware/rtl_bt/rtl8761bu_fw.bin',
 '/usr/lib/firmware/rtl_bt/rtl8761bu_config.bin',
)
MANIFEST = '_bluebridge_manifest.json'


def inventory(roots=ROOTS):
    entries={}
    for name in roots:
        root=Path(name)
        if not root.exists() and not root.is_symlink(): continue
        paths=[root] + (list(root.rglob('*')) if root.is_dir() and not root.is_symlink() else [])
        for p in paths:
            if '__pycache__' in p.parts or p.name.startswith('._') or p.suffix=='.pyc': continue
            s=p.lstat()
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)): continue
            entries[str(p)]=(s.st_mode,s.st_uid,s.st_gid,s.st_size,s.st_mtime_ns,s.st_ino)
    return entries


def stable_capture(roots=ROOTS):
    for attempt in range(3):
        before=inventory(roots)
        data={}
        for name,info in before.items():
            if stat.S_ISREG(info[0]):
                if info[3]>16*1024*1024: raise RuntimeError('Unexpected large file: '+name)
                data[name]=Path(name).read_bytes()
            elif stat.S_ISLNK(info[0]): data[name]=os.readlink(name)
        if inventory(roots)==before:
            return before,data
        time.sleep(0.5)
    raise RuntimeError('Configuration is changing; retry after pairing/settings finish')


def metadata():
    packages=subprocess.run(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n',
        'bluez','bluez-alsa-utils','libasound2-plugin-bluez','alsa-utils','python3','python3-dbus',
        'python3-gi','firmware-realtek','bluez-firmware'],capture_output=True,text=True).stdout
    units={name:subprocess.run(['systemctl','is-enabled',name],capture_output=True,text=True).stdout.strip()
           for name in ['bluetooth.service','bluealsa.service','bluealsa-aplay.service','bluebridge.service']}
    return dict(packages=packages,units=units,kernel=os.uname().release)


def verify(archive):
    archive=Path(archive)
    expected=Path(str(archive)+'.sha256').read_text().split()[0]
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        raise RuntimeError('Archive checksum mismatch')
    with tarfile.open(archive,'r:gz') as tar:
        manifest=json.load(tar.extractfile(MANIFEST))
        members={m.name:m for m in tar.getmembers()}
        if len(members)!=len(tar.getmembers()): raise RuntimeError('Duplicate archive paths')
        for name,digest in manifest['sha256'].items():
            m=members.get(name.lstrip('/'))
            if not m or not m.isfile(): raise RuntimeError('Missing regular file: '+name)
            if hashlib.sha256(tar.extractfile(m).read()).hexdigest()!=digest:
                raise RuntimeError('File checksum mismatch: '+name)
        for name,target in manifest['symlinks'].items():
            m=members.get(name.lstrip('/'))
            if not m or not m.issym() or m.linkname!=target: raise RuntimeError('Symlink mismatch')
        for m in members.values():
            if m.name.startswith('/') or '..' in Path(m.name).parts: raise RuntimeError('Unsafe member')
    return manifest


def backup(destination):
    if os.geteuid()!=0: raise RuntimeError('Root required for pairing keys')
    destination=Path(destination)
    if destination.is_symlink(): raise RuntimeError('Destination must not be a symlink')
    destination.mkdir(mode=0o700,parents=True,exist_ok=True)
    if destination.stat().st_uid!=0: raise RuntimeError('Destination must be root-owned')
    destination.chmod(0o700)
    os.umask(0o077)
    with open('/run/lock/bluebridge-backup.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        entries,data=stable_capture()
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive=destination/('bluebridge-'+stamp+'.tar.gz')
        temp=destination/('.'+archive.name+'.tmp')
        manifest=dict(format=1,created_utc=stamp,roots=list(ROOTS),
            present=list(entries),sha256={n:hashlib.sha256(b).hexdigest() for n,b in data.items() if isinstance(b,bytes)},
            symlinks={n:b for n,b in data.items() if isinstance(b,str)},
            consistency='All included paths unchanged across capture; services not stopped',**metadata())
        try:
            with tarfile.open(temp,'w:gz') as tar:
                for name,info in sorted(entries.items()):
                    m=tarfile.TarInfo(name.lstrip('/'))
                    m.mode=stat.S_IMODE(info[0]);m.uid=info[1];m.gid=info[2];m.mtime=info[4]/1e9
                    if stat.S_ISDIR(info[0]): m.type=tarfile.DIRTYPE;tar.addfile(m)
                    elif stat.S_ISLNK(info[0]): m.type=tarfile.SYMTYPE;m.linkname=data[name];tar.addfile(m)
                    else:
                        m.size=len(data[name]);tar.addfile(m,io.BytesIO(data[name]))
                payload=json.dumps(manifest,indent=2).encode()
                m=tarfile.TarInfo(MANIFEST);m.mode=0o600;m.size=len(payload);tar.addfile(m,io.BytesIO(payload))
            with open(temp,'rb') as f: os.fsync(f.fileno())
            temp.replace(archive)
            Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
            verify(archive)
            # Retain all successful snapshots. Cleanup is a separate reviewed act.
            latest=destination/'.latest.json.tmp'
            latest.write_text(json.dumps(dict(archive=archive.name,verified_utc=stamp)))
            latest.replace(destination/'latest.json')
            fd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally: temp.unlink(missing_ok=True)
        return archive


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--destination',type=Path)
    mode.add_argument('--verify',type=Path)
    args=parser.parse_args()
    if args.verify:
        value=verify(args.verify);print('Verified',len(value['sha256']),'files')
    else: print(backup(args.destination))
