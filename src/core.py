"""Small, testable hardware and authorization primitives. No network operations."""
import json
import os
import pathlib
import secrets
import socket
import time


def read_json(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path, value, mode=0o600):
    path = pathlib.Path(path)
    temp = path.with_suffix('.tmp')
    with open(temp, 'w') as f:
        os.chmod(temp, mode)
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def text(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return ''


def scarlett(config, sysroot='/sys', devroot='/dev'):
    """Resolve only the configured USB identity; never use ALSA's default card."""
    root = pathlib.Path(sysroot)
    matches = [p for p in (root / 'bus/usb/devices').glob('*')
               if text(p/'idVendor') == config['vendor']
               and text(p/'idProduct') == config['product']
               and text(p/'serial') == config['serial']]
    result = dict(connected=bool(matches), recognized=False, card=None, device=None)
    if len(matches) != 1:
        return result
    usb = matches[0].resolve()
    cards = []
    for card in (root/'class/sound').glob('card[0-9]*'):
        if usb in card.resolve().parents and card.name[4:].isdigit():
            cards.append(int(card.name[4:]))
    if len(cards) == 1:
        number = cards[0]
        node = pathlib.Path(devroot)/'snd'/f'pcmC{number}D0p'
        result.update(card=number, device=str(node), recognized=node.exists()
                      and text(root/'class/sound'/f'card{number}'/'id') == 'BlueBridge',
                      generation=text(matches[0]/'devnum'))
    return result


def pcm_status(card, procroot='/proc', device=0):
    if card is None:
        return {}
    content = text(pathlib.Path(procroot)/f'asound/card{card}/pcm{device}p/sub0/status')
    result = {}
    for line in content.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            result[key.strip()] = value.strip()
    return result


class PairingWindow:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.until = 0
        self.pending = None

    def remaining(self):
        return max(0, int(self.until - self.clock() + 0.999))

    def open(self):
        if self.pending:
            raise ValueError('Resuelve primero la solicitud pendiente.')
        self.until = self.clock() + 120

    def close(self):
        self.until = 0
        pending, self.pending = self.pending, None
        return pending

    def request(self, address, name, passkey=None):
        if not self.remaining() or self.pending:
            raise ValueError('Emparejamiento cerrado u otra solicitud pendiente.')
        self.pending = dict(id=secrets.token_hex(16), address=address,
                            name=name[:100], passkey=passkey,
                            deadline=min(self.until, self.clock()+60))
        return self.pending

    def decide(self, request_id):
        pending = self.pending
        if (not pending or not secrets.compare_digest(str(request_id), pending['id'])
                or self.clock() >= pending['deadline'] or not self.remaining()):
            raise ValueError('Solicitud caducada; vuelve a emparejar.')
        self.pending = None
        return pending


def field(label, state='warning'):
    return dict(label=label, state=state)


def watchdog():
    """Heartbeat for systemd only; not an audio/activity or connection timer."""
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.1)
            sock.sendto(b'WATCHDOG=1', address)
    except OSError:
        pass
