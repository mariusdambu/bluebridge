"""BlueBridge v2.3.2: verified digital level with safe Bluetooth-only fallback."""
import logging
from collections import deque
import pathlib
import signal
import subprocess
import time
import os
import socket
from core import read_json, atomic_json, pcm_status, watchdog
from audio_select import discover_outputs
from volume import (load_levels, default_level, create_and_prime, apply_level)

RUN = '/run/bluebridge-player/state.json'
PROFILES = '/etc/bluebridge/professional_usb.json'
running = True


class ScarlettFallback:
    """Allow ALSA to initialize, then hold analog audio until USB is stable."""

    INITIAL_GRACE = 6
    READY_STABILITY = 4

    def __init__(self):
        self.on_realtek = False
        self.wait_since = None
        self.ready_since = None
        self.ready_identity = None

    def choose(self, preferred, candidates, now):
        realtek = next((card for card in candidates
                        if card['id'] == 'integrated' and card['ready']), None)
        if preferred['id'] != 'scarlett' or realtek is None:
            self.on_realtek = preferred['id'] == 'integrated' and preferred['ready']
            self.wait_since = self.ready_since = self.ready_identity = None
            return preferred

        if preferred['ready']:
            if not self.on_realtek and self.wait_since is None:
                return preferred
            identity = (preferred['card'], preferred['device'], preferred['generation'])
            if identity != self.ready_identity:
                self.ready_identity = identity
                self.ready_since = now
            if now - self.ready_since >= self.READY_STABILITY:
                self.on_realtek = False
                self.wait_since = self.ready_since = self.ready_identity = None
                return preferred
        else:
            self.ready_since = self.ready_identity = None
            if self.wait_since is None:
                self.wait_since = now

        grace_expired = self.wait_since is not None and now - self.wait_since >= self.INITIAL_GRACE
        if self.on_realtek or grace_expired:
            self.on_realtek = True
            return realtek
        if preferred['ready']:
            return dict(preferred, ready=False, pcm=None, reason='Estabilizando Scarlett en ALSA')
        return preferred


class VolumeWakeup:
    """Event-driven volume wakeup; two-second polling remains the fallback."""
    def __init__(self, path='/run/bluebridge-player/volume.sock'):
        self.path = pathlib.Path(path)
        self.path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(self.path))
        self.path.chmod(0o620)
        self.sock.settimeout(2)

    def wait(self):
        try:
            self.sock.recv(64)
        except socket.timeout:
            pass

    def close(self):
        self.sock.close()
        self.path.unlink(missing_ok=True)


class StallDetector:
    """A stopped hardware pointer, never musical silence or client inactivity."""
    def __init__(self):
        self.previous = None

    def observe(self, identity, pid, pcm):
        owner = str(pcm.get('owner_pid', ''))
        owned = bool(pid and owner.isdigit() and os.path.isdir(f'/proc/{pid}/task/{owner}'))
        current = (identity, pid, pcm.get('hw_ptr'))
        stuck = bool(owned and pcm.get('state') == 'RUNNING' and current[-1] is not None
                     and current == self.previous)
        self.previous = current if owned and pcm.get('state') == 'RUNNING' else None
        return stuck


class XrunRecovery:
    """Bounded, conservative recovery only for prolonged XRUN during active A2DP."""
    def __init__(self):
        self.since = None
        self.attempts = deque()

    def observe(self, now, xrun, streaming):
        if not (xrun and streaming):
            self.since = None
            return False
        if self.since is None:
            self.since = now
            return False
        if now - self.since < 16:
            return False
        while self.attempts and now - self.attempts[0] > 600:
            self.attempts.popleft()
        if len(self.attempts) >= 2 or (self.attempts and now - self.attempts[-1] < 90):
            return False
        self.attempts.append(now)
        self.since = None
        return True


def active_transport(adapter_address=None):
    """Check BlueZ directly: private controller state is root-only by design."""
    try:
        import dbus
        bus = dbus.SystemBus()
        manager = dbus.Interface(bus.get_object('org.bluez', '/', introspect=False),
                                 'org.freedesktop.DBus.ObjectManager')
        objects = manager.GetManagedObjects(timeout=2)
        adapters = [str(path) for path, data in objects.items()
                    if 'org.bluez.Adapter1' in data
                    and ((adapter_address is None and data['org.bluez.Adapter1'].get('Powered'))
                         or str(data['org.bluez.Adapter1'].get('Address')) == adapter_address)]
        if len(adapters) != 1:
            return False
        devices = {str(path) for path, data in objects.items()
                   if 'org.bluez.Device1' in data
                   and str(data['org.bluez.Device1'].get('Adapter')) == adapters[0]}
        return any('org.bluez.MediaTransport1' in data
                   and str(data['org.bluez.MediaTransport1'].get('Device')) in devices
                   and str(data['org.bluez.MediaTransport1'].get('State')) == 'active'
                   for data in objects.values())
    except Exception as exc:
        logging.debug('BlueZ state unavailable for recovery: %s', exc)
        return False


def load_profiles(path=PROFILES):
    """Reload USB profile registry automatically, without restarting services."""
    file = pathlib.Path(path)
    if not file.exists():
        return []
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValueError('professional_usb.json no es un objeto JSON válido')
    return document.get('professional_usb', [])


def stop(*_):
    global running
    running = False


def terminate(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    settings = read_json('/etc/bluebridge/config.json')
    if not settings or not settings.get('scarlett') or not settings.get('adapter'):
        raise RuntimeError('Missing BlueBridge private configuration')
    child = None
    selected = None
    retry_at = 0
    delay = 5
    started = 0
    error = ''
    output_policy = ScarlettFallback()
    recovery = XrunRecovery()
    stalled = StallDetector()
    wakeup = VolumeWakeup()
    profiles = []
    profile_error = ''
    volume_config = None
    volume_env = None
    volume_applied = None
    volume_error = ''
    volume_retry_at = 0
    fallback_only = False
    volume_supported = None
    playback_mode = 'waiting'
    try:
        while running:
            try:
                profiles = load_profiles()
                preferred, candidates = discover_outputs(settings, profiles)
                current = output_policy.choose(preferred, candidates, time.monotonic())
                profile_error = ''
            except (ValueError, TypeError, KeyError, OSError) as exc:
                # Never replace a working device with an unvalidated new profile.
                profile_error = f'Configuración de interfaz USB no válida: {exc}'
                logging.error(profile_error)
                candidates = []
                current = dict(id='none', label='Configuración no válida',
                               kind='none', connected=False, ready=False,
                               card=None, device=None, card_id=None, pcm=None,
                               generation=None, reason=profile_error)
            identity = (current['id'], current['card'], current['card_id'],
                        current['device'], current['generation']) if current['ready'] else None
            if child and (identity != selected or child.poll() is not None):
                if child.poll() is None:
                    terminate(child)
                else:
                    error = f'El reproductor terminó con código {child.returncode}.'
                    logging.warning(error)
                    # Opening the verified mixer alone is not sufficient: the
                    # real BlueALSA playback PCM must also remain running.
                    if volume_supported is True and time.monotonic() - started < 10:
                        fallback_only = True
                        volume_supported = False
                        volume_applied = None
                        volume_error = ('El PCM de volumen terminó al arrancar; '
                                        'Bluetooth directo, control base no disponible')
                        retry_at = time.monotonic() + 2
                    else:
                        retry_at = time.monotonic() + delay
                        delay = min(60, delay * 2)
                child = None
            if identity != selected:
                recovery.since = None
                volume_config = None
                volume_env = None
                volume_applied = None
                volume_error = ''
                volume_retry_at = 0
                fallback_only = False
                volume_supported = None
                playback_mode = 'waiting'
                selected = identity
                retry_at = 0
                delay = 5
                error = ''
                logging.info('Salida seleccionada: %s (%s)', current['label'],
                             current.get('pcm') or current.get('reason', 'esperando'))
            try:
                levels = load_levels()
                requested_volume = levels.get(current.get('id'), default_level(current.get('id')))
                settings_ok = True
            except (ValueError, RuntimeError, OSError) as exc:
                requested_volume = None
                settings_ok = False
                volume_error = str(exc)[:230]
                error = volume_error
                logging.warning('Audio detenido por configuración de volumen: %s', volume_error)
                if child:
                    terminate(child)
                    child = None
                retry_at = time.monotonic() + 5
            if identity and settings_ok and child is None and time.monotonic() >= retry_at:
                try:
                    if not fallback_only:
                        try:
                            # Check real ALSA capability before presenting a slider.
                            volume_config, volume_env = create_and_prime(current)
                            apply_level(current['card'], requested_volume)
                            volume_applied = requested_volume
                            volume_error = ''
                            volume_supported = True
                        except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
                            # A failed base control never blocks family playback.
                            fallback_only = True
                            volume_supported = False
                            volume_applied = None
                            volume_error = str(exc)[:230]
                            logging.warning('Volumen base no compatible con %s: %s',
                                            current['label'], volume_error)
                    output_pcm = 'bb_volume' if volume_supported is True else current['pcm']
                    env = volume_env if volume_supported is True else None
                    playback_mode = 'digital' if volume_supported is True else 'bluetooth_only'
                    # Preserve the legacy v2.2 route and volume behavior if
                    # the private softvol PCM cannot be verified. The digital
                    # mode uses independent BlueALSA source volume.
                    options = ['/usr/bin/bluealsa-aplay', '--loglevel=warning']
                    if volume_supported is True:
                        options.append('--volume=software')
                    options += ['-D', output_pcm, '--single-audio']
                    child = subprocess.Popen(options, env=env)
                    started = time.monotonic()
                    error = ''
                    logging.info('Reproductor iniciado: %s; volumen base %s; tarjeta ALSA %s',
                                 current['label'],
                                 f'{requested_volume}%' if volume_supported else 'no disponible',
                                 current['card'])
                except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
                    error = str(exc)[:230]
                    logging.warning('No se pudo iniciar reproductor: %s', error)
                    retry_at = time.monotonic() + delay
                    delay = min(60, delay * 2)
            if (identity and settings_ok and child and child.poll() is None
                    and volume_supported is True and volume_applied != requested_volume
                    and time.monotonic() >= volume_retry_at):
                try:
                    apply_level(current['card'], requested_volume)
                    volume_applied = requested_volume
                    volume_error = ''
                    logging.info('Volumen base aplicado a %s: %s%%', current['id'], requested_volume)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                    volume_error = str(exc)[:230]
                    logging.warning('No se pudo aplicar cambio de volumen: %s', volume_error)
                    # Do not claim that the slider is functional when amixer
                    # cannot change the active control. Keep the audio alive.
                    volume_supported = False
                    volume_retry_at = time.monotonic() + 10
            if child and time.monotonic() - started > 60:
                delay = 5
            pcm = pcm_status(current['card'], device=current['device']) if identity else {}
            stuck = stalled.observe(identity, child.pid if child else None, pcm)
            unhealthy = pcm.get('state') == 'XRUN' or stuck
            streaming = bool(identity and child and child.poll() is None
                             and unhealthy and active_transport())
            if recovery.observe(time.monotonic(), unhealthy, streaming):
                logging.warning('PCM sin avance o XRUN persistente: reinicio limitado del reproductor BlueALSA')
                terminate(child)
                child = None
                retry_at = time.monotonic() + 2
                error = 'Recuperando salida de audio sin avance o XRUN persistente'
            atomic_json(RUN, dict(updated=time.time(), pid=child.pid if child else None,
                card=current.get('card'), device=current.get('device'),
                recognized=current.get('ready', False),
                output_id=current.get('id'), output_label=current.get('label'),
                output_kind=current.get('kind'), output_connected=current.get('connected', False),
                output_ready=current.get('ready', False),
                output_reason=current.get('reason', ''),
                audio_candidates=[dict(id=c['id'], label=c['label'],
                    priority=c['priority'], connected=c['connected'],
                    ready=c['ready']) for c in candidates],
                volume_base_requested=requested_volume,
                volume_base_applied=volume_applied if (identity and child and child.poll() is None
                                                      and volume_supported is True) else None,
                volume_supported=(volume_supported if identity and child and child.poll() is None else None),
                volume_mode=playback_mode if identity and child and child.poll() is None else 'waiting',
                volume_error=volume_error,
                error=profile_error or error), 0o640)
            watchdog()
            wakeup.wait()
    finally:
        wakeup.close()
        terminate(child)
        atomic_json(RUN, dict(updated=time.time(), pid=None, output_id='none',
                              error='Detenido'), 0o640)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()
