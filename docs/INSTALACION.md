# Instalación e integración

El audio recorre BlueZ → BlueALSA A2DP Sink → bluealsa-aplay → PCM ALSA → salida analógica. El supervisor del reproductor gestiona el cambio de salida. El controlador gobierna los dos adaptadores, aplica la política de un cliente y publica un estado local. El dashboard lee ese estado y usa un socket local para operaciones concretas. La reproducción funciona sin dashboard.

La versión se ha probado en Debian 13 con BlueZ 5.82, BlueALSA 4.3.1, Python 3, systemd y ALSA 1.2.14. Paquetes de Debian requeridos: `bluez`, `bluez-alsa-utils`, `alsa-utils`, `python3`, `python3-dbus`, `python3-gi`. El TP-Link RTL8761BU usa `firmware-realtek`. Comprueba en el registro del kernel el firmware del ASUS BCM20702A1 para su ID USB. Obtén firmware de una fuente autorizada y guárdalo en el sistema o en la copia privada.

Estas instrucciones son para una instalación nueva. Para actualizar el despliegue v2.4 de Family Plex Server, utiliza `scripts/update.py` en un directorio privado del NAS con el árbol de este repositorio. El script exige reposo, conserva la configuración y crea un punto de reversión.

1. Comprueba ambos adaptadores con `bluetoothctl list` y la Scarlett con `lsusb` y `aplay -l`. Anota las dos direcciones de controlador y la serie USB en una copia **privada** de `config/receiver.example.json`. En un montaje de un solo adaptador usa su dirección en ambos campos.
2. Crea el usuario de sistema `bluebridge` con grupo principal `audio`, sin shell ni directorio personal. Instala los módulos de `src/` en `/opt/bluebridge/` como root, con directorio 0755 y archivos 0644. Instala también `scripts/backup.py` en esa ruta.
3. Instala el JSON privado como `/etc/bluebridge/config.json` y una copia de `config/volumes.example.json` como `/etc/bluebridge/volumes.json`. Usa directorio root:audio 0750 y archivos root:audio 0640. Empieza con niveles físicos bajos en los amplificadores.
4. Completa la serie en `config/71-bluebridge.rules.example` e instala la regla en `/etc/udev/rules.d/71-bluebridge.rules`. Recarga reglas con `udevadm control --reload-rules` y vuelve a conectar únicamente la Scarlett. Verifica que su ID ALSA es `BlueBridge`.
5. Instala `systemd/v2.4.1/bluebridge.service` en `/etc/systemd/system/`. Copia `bluealsa.conf` y `bluealsa-aplay.conf` como `bluebridge.conf` en los respectivos directorios `bluealsa.service.d/` y `bluealsa-aplay.service.d/`. Instala `watchdog.conf` como `90-bluebridge-recovery.conf` en los drop-ins de `bluebridge.service` y `bluealsa-aplay.service`, y `restart-limit.conf` con ese nombre en el de `bluealsa.service`.
6. Si se desea el reemparejamiento familiar abierto de esta instalación, establece `JustWorksRepairing = always` en la sección `[General]` de `/etc/bluetooth/main.conf`. Es una opción global de BlueZ. Facilita volver a emparejar y permite intentos de emparejamiento de equipos cercanos. Carga la configuración al iniciar Bluetooth en la instalación nueva.
7. Ejecuta `systemctl daemon-reload`, valida con `systemd-analyze --man=no verify bluebridge.service bluealsa.service bluealsa-aplay.service` y ejecuta `systemctl enable --now bluetooth.service bluealsa.service bluealsa-aplay.service bluebridge.service`. Comprueba `systemctl status bluebridge bluealsa bluealsa-aplay` y el estado en `/run/bluebridge/state.json`. Empareja y prueba la salida desde un teléfono antes de conectar los altavoces a volumen normal.

## Dashboard autenticado

En Family Plex Server, instala `src/bluebridge_dashboard.py` en `/opt/family-plex/`. El dashboard importa el módulo, inserta `card(csrf)` en la portada, agrega `bluebridge: status()` a su JSON de estado y pasa el campo a `window.updateBlueBridge` en el refresco existente. Mantén las clases visuales `card`, `pill` y `button`.

La acción `POST /api/bluebridge` debe conservar el control de sesión, red, Host y Origin del dashboard, comprobar CSRF y limitar el tamaño de la petición antes de llamar a `operate(data)`. La respuesta JSON usa `ok` y `message`. El proceso necesita la identidad administrativa del dashboard existente para las operaciones acotadas sobre systemd y preferencias. Nunca expongas la ruta como una API pública o sin autenticación.

La vista inicial muestra el servicio y el botón de reinicio, el adaptador seleccionado y la tarjeta de sonido seleccionada con su volumen. Los clientes, la desconexión y los estados secundarios quedan en el desplegable cerrado por defecto.

El slider envía valores agrupados durante el movimiento. El reproductor recibe un aviso local y aplica el nivel al control ALSA privado con lectura posterior. La curva conserva 100 % a 0 dB, 50 % cerca de −6 dB y 10 % cerca de −20 dB. Cada tarjeta conserva su preferencia en el mismo JSON de niveles. Los pasos están cuantizados por softvol y la sensación de cambio aumenta a porcentajes muy bajos.
