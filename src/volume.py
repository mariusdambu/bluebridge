"""BlueBridge private per-card attenuation layer (NOT Bluetooth AVRCP volume).

The user-facing dashboard owns persistent preferences. This module is consumed
by the unprivileged audio player. No global ALSA configuration is modified.
"""
import ctypes
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

SETTINGS = Path('/etc/bluebridge/volumes.json')
PROFILE_REGISTRY = Path('/etc/bluebridge/professional_usb.json')
MIXER_NAME = 'BlueBridge Base Volume'
# One fixed local PCM name, redefined only when the hardware output changes.
PCM_NAME = 'bb_volume'
MIXER_STEPS = 255
# softvol registers a raw ALSA control, not necessarily a simple mixer control.
RAW_CONTROL = 'iface=MIXER,name=\'BlueBridge Base Volume\''
SAFE_ID = re.compile(r'^[A-Za-z0-9_]{1,16}$')
PROFILE_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


def known_cards(profile_path=PROFILE_REGISTRY):
    cards = {'scarlett': 'Scarlett Solo', 'integrated': 'Audio integrado'}
    try:
        data = json.loads(Path(profile_path).read_text())
        profiles = data.get('professional_usb', [])
        if not isinstance(profiles, list) or len(profiles) > 16:
            return cards
        for profile in profiles:
            if isinstance(profile, dict):
                key, label = profile.get('id'), profile.get('label')
                if (isinstance(key, str) and PROFILE_ID.fullmatch(key)
                        and key not in cards and isinstance(label, str)
                        and 1 <= len(label) <= 60):
                    cards[key] = label
    except (OSError, ValueError, TypeError):
        pass
    return cards


def default_level(card_id):
    # Existing Scarlett/Realtek: 0 dB preserves pre-upgrade loudness.
    # New professionally registered interfaces start at an attenuated level.
    return 100 if card_id in ('scarlett', 'integrated') else 70


def levels_from(data, allowed):
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('levels'), dict):
        raise ValueError('Preferencias de volumen corruptas o no reconocidas')
    values = {key: default_level(key) for key in allowed}
    for key, level in data['levels'].items():
        if key in values:
            if type(level) is not int or not 0 <= level <= 100:
                raise ValueError('Nivel guardado inválido para '+key)
            values[key] = level
    return values


def load_levels(path=SETTINGS, allowed=None):
    allowed = known_cards() if allowed is None else allowed
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('No es posible leer preferencias de volumen; no se inicia audio') from exc
    return levels_from(data, allowed)


def validate_level(card_id, level, allowed):
    if not isinstance(card_id, str) or card_id not in allowed:
        raise ValueError('Tarjeta de audio desconocida o no autorizada')
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError('El volumen debe ser un entero entre 0 y 100')


def render_alsa_config(output):
    """Use the ALSA 'softvol' playback plugin and a control ON THE SAME card.

    A player-only ALSA_CONFIG_PATH isolates this PCM from Plex/OMV/other apps.
    No untrusted text is interpolated: card_id validated and PCM numeric device.
    """
    card_id = output.get('card_id')
    device, card = output.get('device'), output.get('card')
    if (not isinstance(card_id, str) or not SAFE_ID.fullmatch(card_id)
            or type(device) is not int or not 0 <= device <= 31
            or type(card) is not int or not 0 <= card <= 1024):
        raise ValueError('Identidad ALSA de salida no válida')
    return (f'</usr/share/alsa/alsa.conf>\n'
            f'pcm.{PCM_NAME} {{\n'
            f'    type softvol\n'
            f'    slave.pcm "plughw:CARD={card_id},DEV={device}"\n'
            f'    control {{ name "{MIXER_NAME}" card {card} }}\n'
            f'    min_dB -60.0\n'
            f'    max_dB 0.0\n'
            f'    resolution 256\n'
            f'}}\n')


def raw_level(percent):
    """A dashboard percent means approximate *linear amplitude*, not dB percent.

    100 -> 0 dB, 50 -> -6 dB, 10 -> -20 dB. 0 -> ALSA's
    minimum softvol step (near silence); physical output is never boosted.
    """
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError('Nivel inválido')
    if percent == 0:
        return 0
    attenuation = 20.0 * math.log10(percent / 100.0)
    return max(0, min(MIXER_STEPS, round((attenuation + 60.0) / 60.0 * MIXER_STEPS)))


def _run(cmd, env=None):
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=4, check=False)


def prime_pcm(config_path):
    """Open and close the PCM before playback so the ALSA mixer element exists.

    Run in a fresh process to prevent cached ALSA configs after device changes.
    This never starts a stream or sends audio.
    """
    os.environ['ALSA_CONFIG_PATH'] = str(config_path)
    lib = ctypes.CDLL('libasound.so.2')
    lib.snd_pcm_open.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lib.snd_pcm_open.restype = ctypes.c_int
    lib.snd_pcm_set_params.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
                                       ctypes.c_uint]
    lib.snd_pcm_set_params.restype = ctypes.c_int
    lib.snd_pcm_close.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_close.restype = ctypes.c_int
    pcm = ctypes.c_void_p()
    # ALSA softvol creates its mixer control on hw_params, not merely on open.
    # S16_LE (2), RW_INTERLEAVED (3), stereo, 48 kHz. No audio is played.
    rc = lib.snd_pcm_open(ctypes.byref(pcm), PCM_NAME.encode(), 0, 0)
    if rc < 0:
        raise RuntimeError(f'No se puede abrir PCM digital (ALSA {rc})')
    try:
        rc = lib.snd_pcm_set_params(pcm, 2, 3, 2, 48000, 1, 250000)
        if rc < 0:
            raise RuntimeError(f'El dispositivo no admite preparar volumen digital (ALSA {rc})')
    finally:
        lib.snd_pcm_close(pcm)


def read_raw_control(card):
    """Read the exact private ALSA softvol control; never address Master/Front.

    ALSA softvol may expose this only through amixer cget/cset, not sget/sset.
    Check type/range/permissions to avoid touching an unrelated control.
    """
    if type(card) is not int or not 0 <= card <= 1024:
        raise ValueError('Tarjeta ALSA inválida')
    result = _run(['/usr/bin/amixer', '-c', str(card), 'cget', RAW_CONTROL])
    if result.returncode:
        raise RuntimeError('Control digital ausente: ' +
                           (result.stderr or result.stdout)[-230:])
    value = result.stdout
    heading = re.search(r"(?m)^numid=\d+,iface=MIXER,name='([^']+)'", value)
    shape = re.search(r'(?m)^\s*; type=INTEGER,access=([^,]+),values=(\d+),min=(\d+),max=(\d+)', value)
    current = re.search(r'(?m)^\s*:\s*values=([0-9]+(?:,[0-9]+)*)\s*$', value)
    if (not heading or heading.group(1) != MIXER_NAME or not shape or not current
            or not shape.group(1).startswith('rw')
            or not 1 <= int(shape.group(2)) <= 8
            or int(shape.group(3)) != 0 or int(shape.group(4)) != MIXER_STEPS):
        raise RuntimeError('Control digital con formato/rango inesperado; no se ajusta')
    channels = [int(v) for v in current.group(1).split(',')]
    if len(channels) != int(shape.group(2)) or any(not 0 <= v <= MIXER_STEPS for v in channels):
        raise RuntimeError('Lectura ALSA del volumen digital inconsistente')
    return channels


def create_and_prime(output, runtime_dir=Path('/run/bluebridge-player')):
    """Prepare isolated PCM, then verify the raw control actually exists."""
    config = Path(runtime_dir) / 'alsa-volume.conf'
    temp = config.with_suffix('.tmp')
    temp.write_text(render_alsa_config(output))
    temp.chmod(0o600)
    temp.replace(config)
    env = os.environ.copy()
    env['ALSA_CONFIG_PATH'] = str(config)
    result = _run([sys.executable, str(Path(__file__).resolve()), '--prime', str(config)], env=env)
    if result.returncode:
        raise RuntimeError('No se pudo preparar el volumen ALSA: ' + (result.stderr or result.stdout)[-250:])
    read_raw_control(output['card'])
    return config, env


def apply_level(card, percent):
    """Set & read back raw ALSA softvol. No player restart or hardware mixer."""
    value = raw_level(percent)
    read_raw_control(card)
    result = _run(['/usr/bin/amixer', '-c', str(card), 'cset', RAW_CONTROL, str(value)])
    if result.returncode:
        raise RuntimeError('No se pudo aplicar volumen base: ' + (result.stderr or result.stdout)[-230:])
    channels = read_raw_control(card)
    if not all(level == value for level in channels):
        raise RuntimeError('ALSA no confirmó el volumen base solicitado')
    return True


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--prime':
        raise SystemExit('Solo disponible para el supervisor local de BlueBridge')
    prime_pcm(Path(sys.argv[2]))
