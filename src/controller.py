"""BlueZ pairing agent, bounded recovery and cached state for the existing UI."""
import json
import logging
import re
import os
import pathlib
import signal
import socketserver
import threading
import time
import sys
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from core import PairingWindow, atomic_json, read_json, scarlett, pcm_status, field, watchdog
from audio_select import dashboard_candidates

PROPS = 'org.freedesktop.DBus.Properties'
OBJECTS = 'org.freedesktop.DBus.ObjectManager'
ADAPTER = 'org.bluez.Adapter1'
DEVICE = 'org.bluez.Device1'
AGENT = 'org.bluez.Agent1'
AUDIO = {'0000110a-0000-1000-8000-00805f9b34fb',
         '0000110b-0000-1000-8000-00805f9b34fb',
         '0000110c-0000-1000-8000-00805f9b34fb',
         '0000110e-0000-1000-8000-00805f9b34fb'}
STATE = '/run/bluebridge/state.json'
SOCKET = '/run/bluebridge/control.sock'
AUTH = '/var/lib/bluebridge/authorized.json'
# BlueBridge dual-adapter v2.4: match physical controller addresses, not hci indices.
def preferred_adapter(config):
    return str(config.get('preferred_adapter') or config.get('adapter') or '').upper()


def choose_audio_owner(previous, connected_paths):
    """Keep the incumbent until it goes away; never evict a playing client."""
    if previous in connected_paths:
        return previous
    return connected_paths[0] if connected_paths else None


def service_health(units, bluetooth_ready, fresh, player):
    if any(v == 'failed' for v in units.values()):
        return field('Error', 'offline')
    if any(v in ('activating', 'deactivating') for v in units.values()):
        return field('Recuperando', 'warning')
    if not all(v == 'active' for v in units.values()):
        return field('Detenido', 'parked')
    if not bluetooth_ready:
        return field('Bluetooth no disponible', 'warning')
    if not fresh:
        return field('Estado de audio desconocido', 'warning')
    if player.get('error'):
        return field('Error de reproducción', 'offline')
    if not player.get('output_ready') or not player.get('pid'):
        return field('Salida de audio no disponible', 'warning')
    return field('Activo', 'online')


class Rejected(dbus.DBusException):
    _dbus_error_name = 'org.bluez.Error.Rejected'


class Agent(dbus.service.Object):
    def __init__(self, controller):
        self.c = controller
        super().__init__(controller.bus, '/org/bluebridge/agent')

    def check(self, sender):
        if sender != self.c.owner:
            raise Rejected('Caller is not BlueZ')

    @dbus.service.method(AGENT, in_signature='', out_signature='', sender_keyword='sender')
    def Release(self, sender=None):
        self.check(sender)
        self.c.registered = False
        self.c.close()

    @dbus.service.method(AGENT, in_signature='', out_signature='', sender_keyword='sender')
    def Cancel(self, sender=None):
        self.check(sender)
        self.c.cancel_pending()

    @dbus.service.method(AGENT, in_signature='ou', out_signature='',
                         async_callbacks=('reply', 'error'), sender_keyword='sender')
    def RequestConfirmation(self, device, passkey, reply, error, sender=None):
        self.check(sender)
        try:
            self.c.validate_pair_device(str(device))
            reply()
        except Exception as exc:
            error(Rejected(str(exc)))

    @dbus.service.method(AGENT, in_signature='o', out_signature='',
                         async_callbacks=('reply', 'error'), sender_keyword='sender')
    def RequestAuthorization(self, device, reply, error, sender=None):
        self.check(sender)
        try:
            self.c.validate_pair_device(str(device))
            reply()
        except Exception as exc:
            error(Rejected(str(exc)))

    @dbus.service.method(AGENT, in_signature='os', out_signature='', sender_keyword='sender')
    def AuthorizeService(self, device, uuid, sender=None):
        self.check(sender)
        props = self.c.device(str(device))
        address = str(props.get('Address', ''))
        if (str(uuid).lower() not in AUDIO or
                str(props.get('Adapter')) != self.c.adapter or
                not (bool(props.get('Paired')) or
                     self.c.accepted.get(address, 0) > time.monotonic())):
            raise Rejected('Only authorized audio devices are allowed')

    @dbus.service.method(AGENT, in_signature='o', out_signature='s', sender_keyword='sender')
    def RequestPinCode(self, device, sender=None):
        self.check(sender)
        raise Rejected('Legacy PIN pairing is not supported')

    @dbus.service.method(AGENT, in_signature='o', out_signature='u', sender_keyword='sender')
    def RequestPasskey(self, device, sender=None):
        self.check(sender)
        raise Rejected('Use numeric comparison on the phone')

    @dbus.service.method(AGENT, in_signature='ouq', out_signature='', sender_keyword='sender')
    def DisplayPasskey(self, device, passkey, entered, sender=None):
        self.check(sender)
        raise Rejected('Use numeric comparison on the phone')

    @dbus.service.method(AGENT, in_signature='os', out_signature='', sender_keyword='sender')
    def DisplayPinCode(self, device, pincode, sender=None):
        self.check(sender)
        raise Rejected('Legacy PIN pairing is not supported')


class Controller:
    def __init__(self):
        self.config = read_json('/etc/bluebridge/config.json')
        if not self.config:
            raise RuntimeError('Missing private configuration')
        self.bus = dbus.SystemBus()
        self.owner = None
        self.adapter = None
        self.registered = False
        self.pairing_available = False
        # Only one paired audio client holds the receiver at a time.
        self.audio_owner = None
        self.disconnect_retry = {}
        self.adapter_cooldown = {}
        self.power_waiting = {}
        self.agent = Agent(self)
        self.window = PairingWindow()
        self.callbacks = None
        self.accepted = {}
        saved = read_json(AUTH, {})
        self.allowed = set(saved.get('devices', []))
        self.last = saved.get('last')
        self.previous_pointer = None
        self.last_error = ''
        self.state = {}
        self.bus.add_signal_receiver(self.owner_changed, signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus', arg0='org.bluez')

    def owner_changed(self, name, old, new):
        self.cancel_pending()
        self.window.close()
        self.accepted.clear()
        self.owner = None
        self.adapter = None
        self.registered = False
        self.pairing_available = False
        self.audio_owner = None
        self.disconnect_retry.clear()

    def properties(self, path):
        return dbus.Interface(self.bus.get_object('org.bluez', path), PROPS)

    def device(self, path):
        return self.properties(path).GetAll(DEVICE, timeout=2)

    def set_adapter(self, key, value):
        if self.adapter:
            self.properties(self.adapter).Set(ADAPTER, key, value, timeout=2)

    def cancel_pending(self):
        self.window.pending = None
        if self.callbacks:
            _, error = self.callbacks
            self.callbacks = None
            try:
                error(Rejected('Pairing rejected or expired'))
            except dbus.DBusException:
                pass

    def close(self):
        self.pairing_available = False
        self.cancel_pending()
        self.window.close()
        self.accepted.clear()
        for key in ('Discoverable', 'Pairable'):
            try:
                self.set_adapter(key, dbus.Boolean(False))
            except dbus.DBusException:
                pass

    def validate_pair_device(self, path):
        props = self.device(path)
        if (not self.adapter or str(props.get('Adapter')) != self.adapter or
                not str(props.get('Address', ''))):
            raise Rejected('Dispositivo Bluetooth inesperado')
        self.accepted[str(props['Address'])] = time.monotonic()+120
        return props

    def request(self, path, passkey, reply, error):
        try:
            props = self.device(path)
            if str(props.get('Adapter')) != self.adapter:
                raise ValueError('Unexpected adapter')
            address = str(props['Address'])
            self.window.request(address, str(props.get('Alias', address)), passkey)
            self.callbacks = (reply, error)
            self.publish_pending()
        except (ValueError, dbus.DBusException) as exc:
            error(Rejected(str(exc)))

    def save(self):
        atomic_json(AUTH, dict(devices=sorted(self.allowed), last=self.last))

    def publish_pending(self):
        self.state['pairing'] = dict(open=bool(self.pairing_available),
            remaining=0, pending=None)
        self.state['updated'] = time.time()
        atomic_json(STATE, self.state)

    def command(self, data):
        if data.get('action') == 'open':
            return dict(ok=True, message='Emparejamiento automático habilitado.')
        if data.get('action') == 'disconnect':
            # No blocklist, no unpair, no trust changes: disconnect this client only.
            address = data.get('address')
            if not isinstance(address, str) or not re.fullmatch(
                    r'[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}', address):
                raise ValueError('Dirección Bluetooth no válida')
            if not self.adapter:
                raise ValueError('No hay receptor Bluetooth activo')
            objects = dbus.Interface(self.bus.get_object('org.bluez', '/'), OBJECTS).GetManagedObjects(timeout=2)
            devices = [(str(path), d[DEVICE]) for path, d in objects.items()
                       if DEVICE in d and str(d[DEVICE].get('Adapter')) == self.adapter
                       and str(d[DEVICE].get('Address', '')).upper() == address.upper()
                       and bool(d[DEVICE].get('Connected'))]
            if len(devices) != 1:
                raise ValueError('El dispositivo ya no está conectado')
            path = devices[0][0]
            dbus.Interface(self.bus.get_object('org.bluez', path), DEVICE).Disconnect(
                reply_handler=lambda *_: logging.info('Desconexión manual solicitada: %s', address),
                error_handler=lambda err: logging.warning('No se pudo desconectar: %s', err),
                timeout=3)
            return dict(ok=True, message='Desconexión solicitada (sin bloquear ni desemparejar)')
        raise ValueError('Acción de BlueBridge no permitida')

    def unit(self, name):
        try:
            manager = dbus.Interface(self.bus.get_object('org.freedesktop.systemd1',
                '/org/freedesktop/systemd1'), 'org.freedesktop.systemd1.Manager')
            path = manager.GetUnit(name, timeout=1)
            return str(dbus.Interface(self.bus.get_object('org.freedesktop.systemd1', path),
                PROPS).Get('org.freedesktop.systemd1.Unit', 'ActiveState', timeout=1))
        except dbus.DBusException:
            return 'unknown'

    def select_adapter(self, objects, now):
        """Prefer TP-Link; preserve an ongoing fallback connection until it ends."""
        adapters = {str(v[ADAPTER].get('Address', '')).upper(): str(path)
                    for path, v in objects.items() if ADAPTER in v and str(v[ADAPTER].get('Address', '')).upper()
                    in {preferred_adapter(self.config), str(self.config['adapter']).upper()}}
        if self.adapter and self.adapter in adapters.values():
            if any(DEVICE in v and str(v[DEVICE].get('Adapter')) == self.adapter
                   and bool(v[DEVICE].get('Connected')) for v in objects.values()):
                return self.adapter, adapters
        ordered = tuple(dict.fromkeys((preferred_adapter(self.config), str(self.config['adapter']).upper())))
        for mac in ordered:
            path = adapters.get(mac)
            if not path or now < self.adapter_cooldown.get(mac, 0):
                continue
            try:
                props = self.properties(path)
                powered = bool(props.Get(ADAPTER, 'Powered', timeout=2))
                if not powered:
                    # Allow hardware time to initialize; retry backup on timeout.
                    first = self.power_waiting.setdefault(mac, now)
                    if now - first >= 10:
                        self.adapter_cooldown[mac] = now + 30
                        self.power_waiting.pop(mac, None)
                        logging.warning('Bluetooth %s no enciende: respaldo durante 30s', mac)
                        continue
                    props.Set(ADAPTER, 'Powered', dbus.Boolean(True), timeout=2)
                else:
                    self.power_waiting.pop(mac, None)
                    self.adapter_cooldown.pop(mac, None)
                return path, adapters
            except dbus.DBusException as exc:
                self.adapter_cooldown[mac] = now + 30
                self.power_waiting.pop(mac, None)
                logging.warning('Adaptador Bluetooth no disponible %s: %s', mac, exc)
        return None, adapters

    def park_other_adapters(self, adapters, selected):
        """Only the selected USB radio can accept clients; keep unplugged adapters untouched."""
        for path in adapters.values():
            if path == selected:
                continue
            try:
                props = self.properties(path)
                current = props.GetAll(ADAPTER, timeout=2)
                if bool(current.get('Discoverable')):
                    props.Set(ADAPTER, 'Discoverable', dbus.Boolean(False), timeout=2)
                if bool(current.get('Pairable')):
                    props.Set(ADAPTER, 'Pairable', dbus.Boolean(False), timeout=2)
                if bool(current.get('Powered')):
                    props.Set(ADAPTER, 'Powered', dbus.Boolean(False), timeout=2)
            except dbus.DBusException as exc:
                logging.warning('No se pudo aparcar radio secundaria: %s', exc)

    def tick(self):
        try:
            now = time.monotonic()
            if self.window.pending and now >= self.window.pending['deadline']:
                self.cancel_pending()
            if self.window.until and not self.window.remaining():
                self.close()
            objects = dbus.Interface(self.bus.get_object('org.bluez', '/'), OBJECTS).GetManagedObjects(timeout=2)
            owner = str(self.bus.get_name_owner('org.bluez'))
            adapter, adapters = self.select_adapter(objects, now)
            if self.owner != owner:
                self.registered = False
                self.owner = owner
            if not self.registered:
                manager = dbus.Interface(self.bus.get_object('org.bluez', '/org/bluez'), 'org.bluez.AgentManager1')
                try:
                    manager.RegisterAgent('/org/bluebridge/agent', 'NoInputNoOutput', timeout=2)
                except dbus.DBusException as exc:
                    if exc.get_dbus_name() != 'org.bluez.Error.AlreadyExists':
                        raise
                manager.RequestDefaultAgent('/org/bluebridge/agent', timeout=2)
                self.registered = True
            if adapter != self.adapter:
                self.close()
                self.adapter = adapter
                self.audio_owner = None
                self.disconnect_retry.clear()
                if adapter:
                    self.close()
                    self.set_adapter('Alias', dbus.String('Yamaha HS4 Wireless'))
                    self.set_adapter('PairableTimeout', dbus.UInt32(0))
                    self.set_adapter('DiscoverableTimeout', dbus.UInt32(0))
                    self.set_adapter('Powered', dbus.Boolean(True))
            self.park_other_adapters(adapters, self.adapter)
            ap = self.properties(self.adapter).GetAll(ADAPTER, timeout=2) if self.adapter else {}
            # Re-enable discoverability and incoming pairing if another process
            # or a BlueZ restart resets them. No writes to unrelated adapters.
            if self.adapter and ap.get('Powered'):
                if int(ap.get('PairableTimeout', -1)) != 0:
                    self.set_adapter('PairableTimeout', dbus.UInt32(0))
                if int(ap.get('DiscoverableTimeout', -1)) != 0:
                    self.set_adapter('DiscoverableTimeout', dbus.UInt32(0))
                if not ap.get('Pairable'):
                    self.set_adapter('Pairable', dbus.Boolean(True))
                if not ap.get('Discoverable'):
                    self.set_adapter('Discoverable', dbus.Boolean(True))
            self.pairing_available = bool(self.adapter and self.registered and
                                          ap.get('Powered') and ap.get('Pairable') and
                                          ap.get('Discoverable'))
            devices = {str(p): d[DEVICE] for p,d in objects.items() if DEVICE in d
                       and str(d[DEVICE].get('Adapter')) == self.adapter}
            for path, props in devices.items():
                address = str(props.get('Address', ''))
                if props.get('Paired') and address not in self.allowed:
                    # Persist only after BlueZ confirms pairing succeeded.
                    self.allowed.add(address)
                    self.last = address
                    self.save()
                if address in self.allowed and props.get('Paired') and not props.get('Trusted'):
                    self.properties(path).Set(DEVICE, 'Trusted', dbus.Boolean(True), timeout=2)
            connected = [(p,d) for p,d in devices.items() if d.get('Connected')]
            trusted_connected = [(p,d) for p,d in connected if str(d.get('Address')) in self.allowed]
            # Audio exclusivity: keep the existing connected client; ask BlueZ to
            # disconnect any later contender. This does not unpair the device.
            # Rate-limit requests so a stubborn phone cannot flood D-Bus.
            paths = [p for p, _ in trusted_connected]
            self.audio_owner = choose_audio_owner(self.audio_owner, paths)
            for path in paths:
                if path == self.audio_owner or now < self.disconnect_retry.get(path, 0):
                    continue
                self.disconnect_retry[path] = now + 10
                logging.warning('Segundo cliente Bluetooth: liberando %s', path)
                try:
                    dbus.Interface(self.bus.get_object('org.bluez', path), DEVICE).Disconnect(
                        reply_handler=lambda *_: None,
                        error_handler=lambda exc: logging.warning('No se pudo liberar cliente: %s', exc),
                        timeout=3)
                except dbus.DBusException as exc:
                    logging.warning('Desconexión Bluetooth no disponible: %s', exc)
            self.disconnect_retry = {p:t for p,t in self.disconnect_retry.items()
                                     if p in paths}
            if len(trusted_connected) == 1:
                address = str(trusted_connected[0][1]['Address'])
                if address != self.last:
                    self.last = address
                    self.save()
            hardware = scarlett(self.config['scarlett'])
            player = read_json('/run/bluebridge-player/state.json', {})
            fresh = 0 <= time.time()-player.get('updated', 0) < 8
            # Connections are initiated by the phone/tablet/PC, never by this
            # receiver: an explicit device-side disconnect must remain honored.
            # BlueZ may still reconnect on its own; we do not call ConnectProfile.
            transports = [d['org.bluez.MediaTransport1'] for d in objects.values()
                          if 'org.bluez.MediaTransport1' in d
                          and str(d['org.bluez.MediaTransport1'].get('Device')) in devices]
            active = [t for t in transports if str(t.get('State')) == 'active']
            # The playback process is the sole authority for choosing a sound
            # card. The controller only displays its fresh, verified state.
            output_id = player.get('output_id') if fresh else None
            output_label = str(player.get('output_label') or 'Sin salida')[:90] if fresh else 'Desconocida'
            output_card = player.get('card') if fresh and player.get('output_ready') else None
            output_dev = player.get('device', 0) if fresh else 0
            pcm = {k:v.strip() for k,v in pcm_status(output_card, device=output_dev).items()}
            player_pid = str(player.get('pid', ''))
            owner_tid = str(pcm.get('owner_pid', ''))
            owned = (fresh and player_pid.isdigit() and owner_tid.isdigit()
                     and os.path.isdir(f'/proc/{player_pid}/task/{owner_tid}'))
            pointer = (output_id, output_card, output_dev, player.get('pid'), pcm.get('hw_ptr'))
            advancing = (owned and pcm.get('state') == 'RUNNING' and pcm.get('hw_ptr') is not None
                         and self.previous_pointer is not None and pointer != self.previous_pointer
                         and pointer[:4] == self.previous_pointer[:4])
            self.previous_pointer = pointer
            units = {n:self.unit(n) for n in ('bluealsa.service','bluealsa-aplay.service','bluetooth.service')}
            audio_error = player.get('error') if fresh else ''
            if pcm.get('state') in ('XRUN', 'DISCONNECTED', 'SUSPENDED'):
                audio_error = 'Salida ALSA: '+pcm['state']
            bt_ready = bool(self.adapter and ap.get('Powered') and self.registered
                and self.bus.name_has_owner('org.bluealsa')
                and '0000110b-0000-1000-8000-00805f9b34fb' in ap.get('UUIDs', []))
            output = field('Sin salida de audio', 'parked')
            if fresh and player.get('output_connected') and not player.get('output_ready'):
                output = field('Esperando ALSA · '+output_label, 'warning')
            elif fresh and player.get('output_ready'):
                output = field('Preparada · sin flujo', 'warning') if player.get('pid') else field('Esperando reproductor')
                if audio_error:
                    output = field('Error · '+str(audio_error)[:120], 'offline')
                elif advancing:
                    output = field('Reproduciendo · flujo digital', 'online')
            reproduction = field('En espera', 'parked')
            if active:
                reproduction = field('Activa · flujo digital', 'online') if advancing else field('Desconocida · sin confirmar salida')
            elif advancing:
                reproduction = field('Desconocida · transporte no confirmado')
            detected_cards = dashboard_candidates(player.get('audio_candidates', [])
                if fresh and isinstance(player.get('audio_candidates'), list) else [],
                output_id, in_use=advancing)
            selected_audio = field(output_label, 'online' if fresh and player.get('output_ready')
                                   else 'warning' if fresh and player.get('output_connected')
                                   else 'parked')
            names = [str(d.get('Alias', 'Dispositivo'))[:100] for _,d in connected]
            clients = [dict(name=str(d.get('Alias') or d.get('Name') or 'Dispositivo')[:100],
                            address=str(d.get('Address', '')).upper())
                       for _, d in connected][:8]
            selected_addr = next((mac for mac, path in adapters.items()
                                  if path == self.adapter), None)
            adapter_name = ('TP-Link UB500 Plus' if selected_addr == preferred_adapter(self.config)
                            else 'ASUS USB-BT400' if selected_addr else 'No disponible')
            self.state = dict(updated=time.time(),
                service=service_health(units, bt_ready, fresh, player),
                bluetooth=field('Disponible','online') if bt_ready else
                    field('Desconectado','parked') if not self.adapter else field('Error · perfil no disponible','offline'),
                scarlett=field('Conectada','online') if hardware['connected'] else field('Desconectada','parked'),
                recognized=hardware['recognized'], audio_device=selected_audio,
                audio_devices=detected_cards, output=output, reproduction=reproduction,
                stream_active=bool(active),
                client=field('Conectado','online') if names else field('Sin conexión','parked'),
                bluetooth_adapter=field(adapter_name, 'online' if selected_addr else 'parked'),
                bluetooth_adapters=[dict(label=('TP-Link UB500 Plus'
                    if mac == preferred_adapter(self.config) else 'ASUS USB-BT400'),
                    selected=path == self.adapter,
                    powered=bool(objects.get(path, {}).get(ADAPTER, {}).get('Powered')))
                    for mac,path in adapters.items()],
                clients=clients,
                device=', '.join(names) if names else '—', units=units,
                detail='Salida actual: '+output_label+'. El flujo digital no confirma volumen físico ni monitores encendidos.')
            self.publish_pending()
            self.last_error = ''
        except Exception as exc:
            message = str(exc)[:200]
            if message != self.last_error:
                logging.warning('Estado temporalmente no disponible: %s', message)
                self.last_error = message
            # Close on an observation failure; the next healthy tick restores
            # the established family pairing policy.
            self.close()
            self.state = dict(updated=time.time(), error='Estado no disponible: '+message,
                              service=field('Error', 'offline'))
            atomic_json(STATE, self.state)
        watchdog()
        return True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        raw = self.rfile.readline(4097)
        if len(raw)>4096:
            return
        done = threading.Event()
        result = {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Petición no válida')
        except (ValueError, TypeError):
            return
        def dispatch():
            try:
                result.update(self.server.controller.command(data))
            except Exception as exc:
                result.update(ok=False, message=str(exc)[:200])
            done.set()
            return False
        GLib.idle_add(dispatch)
        if not done.wait(8):
            result = dict(ok=False, message='Control ocupado; comprueba el estado.')
        self.wfile.write((json.dumps(result)+'\n').encode())


def close_only():
    bus = dbus.SystemBus()
    config = read_json('/etc/bluebridge/config.json', {})
    objects = dbus.Interface(bus.get_object('org.bluez','/'), OBJECTS).GetManagedObjects(timeout=2)
    addresses = {preferred_adapter(config), str(config.get('adapter', '')).upper()}
    for path, data in objects.items():
        if ADAPTER in data and str(data[ADAPTER].get('Address', '')).upper() in addresses:
            props = dbus.Interface(bus.get_object('org.bluez',path), PROPS)
            for key in ('Discoverable','Pairable'):
                props.Set(ADAPTER,key,dbus.Boolean(False),timeout=2)


def main():
    DBusGMainLoop(set_as_default=True)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if '--close' in sys.argv:
        try:
            close_only()
        except dbus.DBusException:
            pass
        return
    controller = Controller()
    pathlib.Path(SOCKET).unlink(missing_ok=True)
    server = socketserver.UnixStreamServer(SOCKET, ControlHandler)
    server.controller = controller
    os.chmod(SOCKET, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    loop = GLib.MainLoop()
    def shutdown(*_):
        controller.close()
        loop.quit()
        return False
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown)
    controller.tick()
    GLib.timeout_add_seconds(2, controller.tick)
    try:
        loop.run()
    finally:
        controller.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
