"""BlueBridge output discovery. Strictly local ALSA/sysfs, never selects HDMI/DP."""
import pathlib
import re
from core import text, scarlett

DIGITAL = re.compile(r'(?i)(?:hdmi|display[ -]?port|\bdp\b|iec958|s[/. -]?pdif|\bdigital\b)')
SAFE_ID = re.compile(r'^[A-Za-z0-9_]{1,16}$')


def _properties(path):
    result = {}
    for line in text(path).splitlines():
        if ':' in line:
            key, val = line.split(':', 1)
            result[key.strip().lower()] = val.strip()
    return result


def _cards(sysroot):
    root = pathlib.Path(sysroot)
    result = []
    for item in (root / 'class/sound').glob('card[0-9]*'):
        if item.name[4:].isdigit():
            result.append((int(item.name[4:]), text(item / 'id'), item.resolve()))
    return sorted(result)


def _usb_ancestor(card_path, sysroot):
    # The USB parent owns vendor/product/serial. Do not confuse the PCI USB
    # host controller or other USB devices with the sound interface itself.
    root = pathlib.Path(sysroot).resolve()
    for candidate in (card_path, *card_path.parents):
        if candidate == root or root not in candidate.parents:
            break
        if text(candidate / 'idVendor') and text(candidate / 'idProduct'):
            return candidate
    return None


def _playbacks(number, devroot, procroot):
    result = []
    root = pathlib.Path(devroot) / 'snd'
    for item in root.glob(f'pcmC{number}D*p'):
        match = re.fullmatch(rf'pcmC{number}D([0-9]+)p', item.name)
        if not match:
            continue
        device = int(match.group(1))
        info = _properties(pathlib.Path(procroot) / 'asound' / f'card{number}' / f'pcm{device}p' / 'info')
        name = info.get('name', '') or info.get('id', '')
        result.append((device, name))
    return sorted(result)


def _result(id, label, kind, priority, connected=False, ready=False,
            card=None, device=None, card_id=None, generation=None, reason=''):
    pcm = None
    if ready and SAFE_ID.fullmatch(card_id or '') and device is not None:
        pcm = f'plughw:CARD={card_id},DEV={device}'
    return dict(id=id, label=label, kind=kind, priority=priority,
                connected=connected, ready=bool(ready and pcm), card=card,
                device=device, card_id=card_id, generation=generation,
                pcm=pcm, reason=reason)


def _usb_presence(spec, cards, sysroot, devroot, procroot):
    root = pathlib.Path(sysroot)
    matches = [item for item in (root / 'bus/usb/devices').glob('*')
               if text(item / 'idVendor').lower() == spec['vendor'].lower()
               and text(item / 'idProduct').lower() == spec['product'].lower()
               and (not spec.get('serial') or text(item / 'serial') == spec['serial'])]
    if not matches:
        return None
    label = spec['label']
    if len(matches) != 1:
        return _result(spec['id'], label, 'professional', spec['priority'], connected=True,
                       reason='Interfaz USB ambigua: hay más de una coincidencia')
    usb_path = matches[0].resolve()
    attached = [(number, card_id) for number, card_id, target in cards
                if usb_path in target.parents and SAFE_ID.fullmatch(card_id)]
    if len(attached) != 1:
        return _result(spec['id'], label, 'professional', spec['priority'], connected=True,
                       reason='Interfaz USB detectada; esperando tarjeta ALSA')
    number, card_id = attached[0]
    available = [(dev, name) for dev, name in _playbacks(number, devroot, procroot)
                 if not DIGITAL.search(name)]
    dev_wanted = spec.get('pcm_device')
    if dev_wanted is not None:
        available = [(d, n) for d, n in available if d == dev_wanted]
    if len(available) != 1:
        return _result(spec['id'], label, 'professional', spec['priority'], connected=True,
                       card=number, card_id=card_id,
                       reason='No hay un único PCM de reproducción autorizado')
    dev, _ = available[0]
    return _result(spec['id'], label, 'professional', spec['priority'], connected=True,
                   ready=True, card=number, card_id=card_id, device=dev,
                   generation=text(matches[0] / 'devnum'))


def _integrated_analog(cards, sysroot, devroot, procroot):
    possibilities = []
    for number, card_id, target in cards:
        if not SAFE_ID.fullmatch(card_id) or DIGITAL.search(card_id):
            continue
        if _usb_ancestor(target, sysroot):
            continue
        infos = _playbacks(number, devroot, procroot)
        for device, name in infos:
            # Require affirmative analog evidence: not merely "not HDMI".
            if 'analog' not in name.lower() or DIGITAL.search(name):
                continue
            possibilities.append(_result('integrated', f'Integrada · {name}', 'analog', -100,
                                         connected=True, ready=True, card=number, device=device,
                                         card_id=card_id))
    if not possibilities:
        return _result('integrated', 'Audio integrado', 'analog', -100,
                       reason='No se detectó una salida analógica integrada válida')
    # Prefer the known built-in HDA ID; otherwise choose stable lexical ID,
    # never PCI/ALSA enumeration order.
    return sorted(possibilities, key=lambda c:(c['card_id'] != 'Generic',
                                                c['card_id'], c['device']))[0]


def _profiles(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 16:
        raise ValueError('professional_usb debe ser una lista de máximo 16 tarjetas')
    ids = {'scarlett', 'integrated'}
    profiles = []
    for profile in raw:
        if not isinstance(profile, dict):
            raise ValueError('Perfil USB no válido')
        id = profile.get('id')
        if not isinstance(id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', id) or id in ids:
            raise ValueError('ID de perfil USB duplicado o no válido')
        ids.add(id)
        vendor, product = profile.get('vendor'), profile.get('product')
        if any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-fA-F]{4}', x)
               for x in (vendor, product)):
            raise ValueError('USB vendor/product deben ser hexadecimales de 4 caracteres')
        priority = profile.get('priority', 90)
        if type(priority) is not int or not 1 <= priority <= 1000:
            raise ValueError('Prioridad USB fuera de rango')
        pcm_device = profile.get('pcm_device')
        if pcm_device is not None and (type(pcm_device) is not int or not 0 <= pcm_device <= 31):
            raise ValueError('pcm_device debe estar entre 0 y 31')
        serial = profile.get('serial')
        if serial is not None and (not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', serial)):
            raise ValueError('Número de serie no válido')
        label = profile.get('label', id)
        if not isinstance(label, str) or not 1 <= len(label) <= 60:
            raise ValueError('Nombre del perfil no válido')
        profiles.append(dict(id=id, label=label, vendor=vendor, product=product,
                             serial=serial, priority=priority, pcm_device=pcm_device))
    return profiles


def discover_outputs(settings, other_profiles=None, sysroot='/sys', devroot='/dev', procroot='/proc'):
    """Return (selected output, connected candidates sorted by configured priority).

    A professional USB device whose physical USB identity is present continues to
    reserve its priority while ALSA enumerates; that prevents jumping briefly to
    the integrated sound card. Digital/HDMI/DP output is never a candidate.
    """
    cards = _cards(sysroot)
    professional = []
    scarlett_hw = scarlett(settings['scarlett'], sysroot=sysroot, devroot=devroot)
    if scarlett_hw['connected']:
        cardnum = scarlett_hw['card']
        ready = scarlett_hw['recognized'] and any(
            d == 0 and not DIGITAL.search(name)
            for d, name in _playbacks(cardnum, devroot, procroot))
        professional.append(_result('scarlett', 'Scarlett Solo', 'professional', 100,
                                    connected=True, ready=ready, card=cardnum,
                                    card_id='BlueBridge', device=0,
                                    generation=scarlett_hw.get('generation'),
                                    reason='' if ready else 'Scarlett presente; esperando ALSA'))
    for profile in _profiles(other_profiles):
        output = _usb_presence(profile, cards, sysroot, devroot, procroot)
        if output:
            professional.append(output)
    integrated = _integrated_analog(cards, sysroot, devroot, procroot)
    if professional:
        professional.sort(key=lambda c: (-c['priority'], c['id']))
        candidates = professional + ([integrated] if integrated['connected'] else [])
        return professional[0], candidates
    return integrated, [integrated] if integrated['connected'] else []


def select_output(settings, other_profiles=None, sysroot='/sys', devroot='/dev', procroot='/proc'):
    """Compatibility API for the hardware selector."""
    selected, _ = discover_outputs(settings, other_profiles, sysroot, devroot, procroot)
    return selected


def dashboard_candidates(candidates, selected_id, in_use=False):
    """Sanitized, single-winner display model; status is not device control.

    'En uso' is only shown if the controller confirmed live PCM progression.
    While playback is paused the device is merely 'Seleccionada', not 'En uso'.
    """
    entries = []
    for item in candidates[:17]:
        if not item.get('connected'):
            continue
        selected = item.get('id') == selected_id
        ready = bool(item.get('ready'))
        status = ('En uso' if in_use else 'Seleccionada') if selected and ready else (
                 'Esperando ALSA' if selected else 'En espera')
        entries.append(dict(id=str(item.get('id', ''))[:32],
                            label=str(item.get('label', 'Dispositivo'))[:90],
                            priority=int(item.get('priority', -100)),
                            selected=selected, in_use=bool(selected and in_use and ready),
                            state=status))
    return entries
