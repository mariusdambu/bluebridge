"""BlueBridge v2.3.2 dashboard; verified per-card controls, safe grey fallback.

The pre-existing host authenticates requests and verifies CSRF before operate().
There is no new HTTP endpoint, network listener, or card selector.
"""
import json
import re
import socket
import subprocess
import threading
import os
from pathlib import Path
import pwd
import grp
import stat
import tempfile
import time

STATE = Path('/run/bluebridge/state.json')
PLAYER = Path('/run/bluebridge-player/state.json')
PREFS = Path('/etc/bluebridge/volumes.json')
PROFILES = Path('/etc/bluebridge/professional_usb.json')
# BlueBridge restart button v2.4r1
_restart_lock = threading.Lock()
_restart_last = 0.0
_volume_lock = threading.Lock()


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _allowed():
    """Only configured sound card IDs; no client-supplied names/ALSA commands."""
    result = {'scarlett': ('Scarlett Solo', 100),
              'integrated': ('Audio integrado', -100)}
    profiles = _read_json(PROFILES, {})
    if not isinstance(profiles, dict) or not isinstance(profiles.get('professional_usb', []), list):
        return result
    import re
    for item in profiles.get('professional_usb', [])[:16]:
        if not isinstance(item, dict):
            continue
        key, label, priority = item.get('id'), item.get('label'), item.get('priority', 90)
        if (isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', key)
                and key not in result and isinstance(label, str)
                and 1 <= len(label) <= 60 and type(priority) is int and 1 <= priority <= 1000):
            result[key] = (label, priority)
    return result


def _default(key):
    # Existing card paths start at unity gain; other pro cards are attenuated.
    return 100 if key in ('scarlett', 'integrated') else 70


def _levels(allowed):
    raw = _read_json(PREFS, {})
    levels = {key: _default(key) for key in allowed}
    if isinstance(raw, dict) and raw.get('version') == 1 and isinstance(raw.get('levels'), dict):
        for key, value in raw['levels'].items():
            if key in levels and type(value) is int and 0 <= value <= 100:
                levels[key] = value
    return levels


def status():
    try:
        value = _read_json(STATE)
        if not isinstance(value, dict) or not 0 <= time.time()-value['updated'] < 12:
            raise ValueError('stale')
        allowed = _allowed()
        levels = _levels(allowed)
        cards = value.get('audio_devices') or []
        if not isinstance(cards, list):
            cards = []
        present = {}
        for c in cards:
            if isinstance(c, dict) and c.get('id') in allowed:
                # Controller's dashboard_candidates() emits only *connected*
                # devices, but deliberately omits the connected/ready keys.
                # Do not mistake an omitted key for a disconnected card.
                present[c['id']] = dict(c, connected=True)
        # Show also the registered cards when physically unplugged, to allow
        # setting their volume now for the NEXT time they are connected.
        for key, (label, priority) in allowed.items():
            if key not in present:
                present[key] = dict(id=key, label=label, priority=priority,
                                    connected=False, ready=False, selected=False,
                                    in_use=False, state='No conectada')
        player = _read_json(PLAYER, {})
        fresh = (isinstance(player, dict) and 0 <= time.time()-player.get('updated', 0) < 9)
        selected = player.get('output_id') if fresh else None
        for key, c in present.items():
            c['base_volume'] = levels[key]
            is_current = bool(fresh and key == selected and player.get('output_ready'))
            if is_current:
                c['selected'] = True
                c['ready'] = True
                c['connected'] = True
            verified = (is_current and player.get('volume_supported') is True
                        and player.get('volume_mode') == 'digital'
                        and player.get('pid') is not None
                        and not player.get('volume_error'))
            c['volume_control'] = ('available' if verified else
                                   'unsupported' if is_current and player.get('volume_supported') is False
                                   else 'unverified')
            c['base_applied'] = (player.get('volume_base_applied') == levels[key]
                                 if verified else None)
            if is_current and player.get('volume_error'):
                c['volume_error'] = str(player['volume_error'])[:160]
        value['audio_devices'] = sorted(present.values(), key=lambda c: (not c.get('connected',False), -c['priority'], c['id']))
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return dict(error='Estado no disponible o desactualizado',
                    service=dict(label='Desconocido', state='warning'))


def operate(data):
    # Serialize read-modify-write so simultaneous tabs cannot lose another
    # card's level. Never execute a client-supplied command or process ID.
    if isinstance(data, dict) and data.get('action') == 'set_volume':
        with _volume_lock:
            return _operate(data)
    return _operate(data)


def _operate(data):
    """Invoked behind the dashboard's existing login + CSRF check."""
    if not isinstance(data, dict) or set(data)-{'action','csrf','id','level','address'}:
        raise ValueError('Petición no válida')
    if data.get('action') == 'restart_service':
        # No client-selected commands, service names, or shell parameters.
        if set(data) != {'action', 'csrf'}:
            raise ValueError('Peticion de reinicio no valida')
        if os.geteuid() != 0:
            raise RuntimeError('El dashboard no tiene permisos para reiniciar BlueBridge')
        global _restart_last
        if not _restart_lock.acquire(blocking=False):
            raise RuntimeError('Ya hay un reinicio de BlueBridge en curso')
        try:
            now = time.monotonic()
            if now - _restart_last < 30:
                raise RuntimeError('Reinicio reciente: espera antes de repetirlo')
            _restart_last = now  # Prevent repeated clicks, even if systemd reports an error.
            cmd = ['/usr/bin/systemctl', '--no-block', 'restart',
                   'bluealsa.service', 'bluealsa-aplay.service', 'bluebridge.service']
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError('No se pudo solicitar el reinicio de BlueBridge') from exc
            if result.returncode != 0:
                raise RuntimeError('systemd ha rechazado el reinicio de BlueBridge')
            # --no-block queues the jobs; do not claim they have finished.
            return dict(ok=True, message='Reinicio solicitado; comprueba el estado en unos segundos')
        finally:
            _restart_lock.release()
    if data.get('action') == 'disconnect':
        address = data.get('address')
        if (set(data) - {'csrf','action','address'} or not isinstance(address, str)
                or not re.fullmatch(r'[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}', address)):
            raise ValueError('Dispositivo Bluetooth no válido')
        if os.geteuid() != 0:
            raise RuntimeError('El dashboard no tiene permisos para desconectar dispositivos')
        # Existing root-only local socket; do not expose an unauthenticated HTTP endpoint.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(9)
            sock.connect('/run/bluebridge/control.sock')
            sock.sendall((json.dumps({'action':'disconnect','address':address})+'\n').encode())
            reply = bytearray()
            while len(reply) < 4096:
                part = sock.recv(4096 - len(reply))
                if not part:
                    break
                reply.extend(part)
                if b'\n' in reply:
                    break
        result = json.loads(reply.split(b'\n', 1)[0])
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError(str(result.get('message', 'No se pudo desconectar'))[:160])
        return result
    if data.get('action') != 'set_volume' or 'address' in data:
        raise ValueError('Acción de BlueBridge no permitida')
    card_id, level = data.get('id'), data.get('level')
    allowed = _allowed()
    if not isinstance(card_id, str) or card_id not in allowed:
        raise ValueError('Tarjeta desconocida')
    if type(level) is not int or not 0 <= level <= 100:
        raise ValueError('Volumen fuera de rango (0–100)')
    if os.geteuid() != 0:
        raise RuntimeError('El dashboard no tiene permisos para guardar preferencias de audio')
    # The frontend is advisory; the API independently enforces capability.
    current = status()
    available = any(c.get('id') == card_id and c.get('volume_control') == 'available'
                    for c in current.get('audio_devices', []))
    if not available:
        raise RuntimeError('Control de volumen no verificado o no disponible para esta tarjeta')
    if PREFS.is_symlink() or PREFS.parent.is_symlink():
        raise RuntimeError('Configuración de audio inesperada')
    if not PREFS.parent.is_dir():
        raise RuntimeError('Directorio de configuración BlueBridge ausente')
    owner = PREFS.parent.stat()
    if owner.st_uid != 0 or bool(owner.st_mode & 0o022):
        raise RuntimeError('Permisos inseguros de configuración BlueBridge')
    levels = _levels(allowed)
    levels[card_id] = level
    content = (json.dumps({'version':1,'levels':levels}, ensure_ascii=False,
                          sort_keys=True) + '\n').encode()
    fd, tmp = tempfile.mkstemp(prefix='.bbvol-', dir=str(PREFS.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chown(tmp, 0, grp.getgrnam('audio').gr_gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, PREFS)
        dirfd = os.open(str(PREFS.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wake:
            wake.settimeout(0.05)
            wake.sendto(b'volume', '/run/bluebridge-player/volume.sock')
    except OSError:
        pass  # Polling remains available if upgrading the player independently.
    return dict(ok=True, message='Volumen base guardado', id=card_id, level=level)


def card(csrf):
    rows = ''.join('<div style="display:flex;justify-content:space-between;gap:12px;margin:8px 0">'
        '<span>'+label+'</span><span id="bb-'+key+'" class="pill warning">Comprobando</span></div>'
        for key,label in [('bluetooth','Bluetooth'),('scarlett','Scarlett Solo'),
                         ('output','Salida de audio'),
                         ('client','Cliente Bluetooth'),('reproduction','Reproducción'),
                         ('pairing','Emparejamiento')])
    body = '''<section aria-label="BlueBridge"><article class="card" id="bluebridge-card">
<div class="card-head"><div><h2>BlueBridge</h2><p>Yamaha HS4 Wireless</p></div><div style="display:flex;align-items:center;gap:8px;justify-content:flex-end;flex-wrap:wrap"><span id="bb-service" class="pill warning">Comprobando</span><button class="button" type="button" id="bb-restart">Reiniciar servicio</button></div></div>
<div class="meta" id="bb-restart-status" role="status" aria-live="polite"></div>
''' + '''<div style="display:flex;justify-content:space-between;gap:12px;margin:10px 0"><span>Adaptador Bluetooth</span><span id="bb-bluetooth_adapter" class="pill warning">Comprobando</span></div>
<p class="meta"><strong>Tarjetas disponibles</strong></p>
<div id="bb-audio-devices" aria-live="polite">Comprobando salida de sonido...</div>
<details id="bb-advanced" style="margin-top:14px"><summary style="cursor:pointer">Información y controles avanzados</summary>
''' + rows + '''<p id="bb-other-adapters"></p><p>Dispositivo: <strong id="bb-device">—</strong></p>
<div id="bb-clients" aria-live="polite"></div>
<p class="meta" id="bb-detail">El flujo digital no confirma el volumen físico ni los monitores encendidos.</p>
<p class="meta"><strong>Otras tarjetas · orden de prioridad</strong></p>
<div id="bb-other-devices"></div>
<p class="meta">El volumen base es independiente del móvil. Los controles no verificados permanecen deshabilitados.</p>
</details>
</article></section><script>
(() => {
 const csrf = __BB_CSRF__;
 const keys = ['service','bluetooth','bluetooth_adapter','scarlett','output','client','reproduction','pairing'];
 const changes = new Map();
 const requests = new Set();
 const queues = new Map();
 function queueVolume(id, level, note, control, flush=false) {
   let q=queues.get(id);
   if(!q) { q={pending:null,running:false,timer:null}; queues.set(id,q); }
   q.pending={level,note,control};
   if(q.running || q.timer) return;
   q.timer=window.setTimeout(()=>{ q.timer=null; sendVolume(id,q); },flush?0:180);
 }
 async function sendVolume(id,q) {
   if(q.running || !q.pending) return;
   const {level,note}=q.pending; q.pending=null; q.running=true; requests.add(id);
   note.textContent='Aplicando...';
   try {
     const response=await fetch('/api/bluebridge',{method:'POST',credentials:'same-origin',
       headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action:'set_volume',id,level})});
     const body=await response.json();
     if(!response.ok || body.ok!==true) throw new Error(body.message || 'Error al guardar');
     if(changes.get(id)===level) changes.delete(id);
     note.textContent='Guardado · '+level+'%';
   } catch(error) {
     note.textContent='No guardado: '+String(error.message||error).slice(0,100);
   } finally {
     q.running=false; requests.delete(id);
     if(q.pending) q.timer=window.setTimeout(()=>{q.timer=null;sendVolume(id,q);},180);
   }
 }
 function renderAudioDevices(devices, rootId) {
   const root = document.getElementById(rootId);
   if (!root) return;
   // Keep sliders in place while interacting; polling must not steal drag focus.
   if (root.contains(document.activeElement) || root.querySelector('input[type="range"]:active') || requests.size) return;
   root.replaceChildren();
   if (!Array.isArray(devices) || devices.length === 0) {
     root.textContent = rootId==='bb-audio-devices' ? 'Salida de audio no disponible.' : 'Sin otras tarjetas registradas.';
     return;
   }
   let connectedRank = 0;
   for (const d of devices.slice(0,18)) {
     if (!d || typeof d.id !== 'string') continue;
     const wrap = document.createElement('div');
     wrap.style.cssText = 'padding:9px 0;border-bottom:1px solid var(--border,#7773)';
     const top = document.createElement('div');
     top.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:10px';
     const label = document.createElement('span');
     label.textContent = (rootId==='bb-audio-devices' ? '' : ((d.connected ? 'Prioridad ' + (++connectedRank) : 'Desconectada') + ' · ')) + (d.label || 'Tarjeta');
     const state = document.createElement('span');
     state.className = 'pill ' + (d.in_use ? 'online' : d.selected ? 'warning' : 'parked');
     state.textContent = d.state || 'No conectada';
     top.append(label, state);
     const bottom = document.createElement('div');
     bottom.style.cssText = 'display:flex;align-items:center;gap:10px;margin-top:7px';
     const slider = document.createElement('input');
     slider.type = 'range';slider.min='0';slider.max='100';slider.step='1';
     slider.value = String(changes.has(d.id) ? changes.get(d.id) : (d.base_volume ?? 100));
     const adjustable = d.volume_control === 'available';
     slider.disabled = !adjustable;
     slider.style.cssText = 'flex:1;min-width:60px;accent-color:var(--accent,#60a5fa);' +
         (adjustable ? '' : 'opacity:0.38;filter:grayscale(1);cursor:not-allowed;');
     slider.setAttribute('aria-label','Volumen base de '+(d.label || d.id) +
       (adjustable ? '' : ' (control no disponible)'));
     const number = document.createElement('strong');
     number.style.minWidth = '48px';number.textContent = slider.value + '%';
     slider.addEventListener('input', () => {changes.set(d.id,Number(slider.value));number.textContent=slider.value+'%'; if(adjustable) queueVolume(d.id,Number(slider.value),note,slider);});
     const note = document.createElement('small');
     note.className='meta';
     note.style.cssText='display:block;margin-top:4px';
     if (d.volume_control === 'unsupported') note.textContent =
       'Control no compatible · solo volumen Bluetooth' +
       (d.volume_error ? ' ('+d.volume_error+')' : '');
     else if (!adjustable) note.textContent = d.connected ?
       'Pendiente de comprobar al utilizar la tarjeta' : 'No conectada · control sin verificar';
     else if (d.base_applied === false) note.textContent='Pendiente de aplicar';
     else note.textContent='Volumen base disponible · Bluetooth independiente';
     if (adjustable) slider.addEventListener('change', () => queueVolume(d.id, Number(slider.value), note, slider, true));
     bottom.append(slider,number);
     wrap.append(top,bottom,note);
     root.append(wrap);
   }
 }
 let restartBusy = false;
 async function restartBlueBridge() {
   if (restartBusy) return;
   if (!window.confirm('¿Reiniciar BlueBridge? Se interrumpira temporalmente la musica Bluetooth.')) return;
   const button = document.getElementById('bb-restart');
   const status = document.getElementById('bb-restart-status');
   if (!button || !status) return;
   restartBusy = true;
   button.disabled = true;
   status.textContent = 'Solicitando reinicio...';
   try {
     const response = await fetch('/api/bluebridge', {
       method:'POST', credentials:'same-origin',
       headers:{'Content-Type':'application/json'},
       body:JSON.stringify({csrf,action:'restart_service'})
     });
     const result = await response.json();
     if (!response.ok || result.ok !== true) {
       throw new Error(result.message || 'No se pudo reiniciar BlueBridge');
     }
     status.textContent = result.message || 'Reinicio solicitado; comprueba el estado.';
   } catch(error) {
     status.textContent = 'Error: ' + String(error.message || error).slice(0,120);
   } finally {
     restartBusy = false;
     button.disabled = false;
   }
 }
 document.getElementById('bb-restart')?.addEventListener('click', restartBlueBridge);
 let disconnectBusy = false;
 async function disconnectClient(address, name, btn, message) {
   if (!window.confirm('¿Desconectar ' + name + '? Podrá volver a conectarse cuando quiera.')) return;
   disconnectBusy = true;
   btn.disabled = true;
   message.textContent = 'Desconectando...';
   try {
     const response = await fetch('/api/bluebridge', {
       method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
       body:JSON.stringify({csrf,action:'disconnect',address})
     });
     const result = await response.json();
     if (!response.ok || result.ok !== true) throw new Error(result.message || 'Error al desconectar');
     message.textContent = 'Desconexión solicitada';
   } catch(error) {
     message.textContent = 'No desconectado: ' + String(error.message || error).slice(0,100);
   } finally {
     disconnectBusy = false;
     btn.disabled = false;
   }
 }
 function renderClients(clients) {
   const root=document.getElementById('bb-clients');
   if (!root || disconnectBusy) return;
   root.replaceChildren();
   if (!Array.isArray(clients) || !clients.length) return;
   for (const c of clients.slice(0,8)) {
     if (!c || typeof c.address!=='string' || !/^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$/.test(c.address)) continue;
     const wrap=document.createElement('div');
     wrap.style.cssText='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0';
     const label=document.createElement('span');
     label.textContent=(c.name || 'Dispositivo') + ' · ' + c.address;
     const btn=document.createElement('button');
     btn.type='button';btn.className='button';btn.textContent='Desconectar';
     const message=document.createElement('small');message.className='meta';
     btn.addEventListener('click',()=>disconnectClient(c.address,c.name||c.address,btn,message));
     wrap.append(label,btn,message);
     root.append(wrap);
   }
 }
 function pill(key, value) {
   const el=document.getElementById('bb-'+key);
   if (!el) return;
   const state=['online','warning','parked','offline'].includes(value?.state)?value.state:'warning';
   el.className='pill '+state;el.textContent=value?.label||'Desconocido';
 }
 window.updateBlueBridge = (s) => {
   const valid=s&&!s.error&&Date.now()/1000-s.updated<15;
   if (!valid) {
     keys.forEach(k=>pill(k,{label:'Desconocido',state:'warning'}));
     document.getElementById('bb-device').textContent='—';
     renderAudioDevices(null,'bb-audio-devices');
     renderAudioDevices(null,'bb-other-devices');
     document.getElementById('bb-other-adapters').textContent='';
     renderClients(null);
     document.getElementById('bb-detail').textContent=s?.error||'Estado no disponible; esperando actualización.';
     return;
   }
   keys.filter(k=>k!=='pairing').forEach(k=>pill(k,s[k]));
   const pairing=s.pairing||{};
   pill('pairing',{label:pairing.open?'Disponible':'No disponible',state:pairing.open?'online':'warning'});
   const devices=Array.isArray(s.audio_devices)?s.audio_devices:[];
   renderAudioDevices(devices.filter(d=>d.selected),'bb-audio-devices');
   renderAudioDevices(devices.filter(d=>!d.selected),'bb-other-devices');
   const backups=(s.bluetooth_adapters||[]).filter(a=>!a.selected);
   document.getElementById('bb-other-adapters').textContent=backups.map(
     a=>a.label+' · '+(a.powered?'Encendido':'En reserva')).join(' · ');
   renderClients(s.clients);
   document.getElementById('bb-device').textContent=s.device||'—';
   document.getElementById('bb-detail').textContent=s.detail||'';
 };
})();
</script>'''
    # JS-safe literal, no injection of CSRF token into JS code or HTML.
    token = json.dumps(str(csrf)).replace('<','\\u003c').replace('>','\\u003e')
    return body.replace('__BB_CSRF__', token)
