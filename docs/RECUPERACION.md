# Copia, restauración y rollback

Ejecuta como root `python3 /opt/bluebridge/backup.py --destination /ruta/privada/bluebridge` en un destino de respaldo ya montado y comprobado. Verifica con `python3 /opt/bluebridge/backup.py --verify ARCHIVO.tar.gz`. Conserva el archivo y su `.sha256` juntos. El manifiesto incluye paquetes, estado de habilitación, rutas presentes y hashes de cada archivo. La captura se reintenta si detecta cambios mientras lee los archivos.

En Family Plex Server, `scripts/attach_backup.py` genera una modificación acotada para el envoltorio `legacy_backup.py`. El mismo script con `--dashboard` integra el estado combinado en el dashboard. Revisa ambas diferencias antes de instalarlas. El paso se ejecuta tras restaurar los escritores de aplicaciones y guarda la copia en `server-config/bluebridge` del USB verificado. El archivo `latest.json` de ese directorio identifica la última copia BlueBridge verificada. El botón «Crear copia ahora» y el temporizador semanal lanzan el mismo servicio. «Correcto» incluye la verificación de BlueBridge. Si falla solo ese paso, la tarjeta muestra «BlueBridge no respaldado» y el detalle técnico queda en el journal. El marcador del espejo de Plex sigue indicando el resultado de su propia copia.

El archivo incluye código, configuración, firmware instalado, reglas udev, unidades, niveles, módulos del dashboard y datos privados de BlueZ, BlueALSA y ALSA. Mantenlo con permisos de root. La lista de paquetes permite reproducir dependencias. El archivo no es una imagen del sistema.

## Reinstalación

1. Verifica la copia y extráela como root en un directorio de preparación privado, fuera de las rutas activas. Inspecciona el manifiesto y recrea los paquetes, el usuario y su grupo según la guía.
2. Restaura código, unidades, regla udev y configuración selectivamente. Adecua propietarios si cambió el UID o GID. Integra el módulo del dashboard en su versión actual y conserva su autenticación. Evita sobrescribir cambios del dashboard o del envoltorio del backup ajenos a BlueBridge.
3. Restaura emparejamientos con BlueZ y BlueALSA detenidos, únicamente si la identidad de cada adaptador coincide. Mantén los permisos originales. Un adaptador de reemplazo suele requerir emparejar de nuevo. Revisa el firmware y la selección ALSA antes de reactivar los servicios.
4. Arranca el componente y verifica alias, reproducción, niveles y fallback. Una verificación de hashes solo acredita integridad del archivo.

## Volver a v2.4

Desde el árbol que se usó para desplegar, ejecuta como root `python3 scripts/update.py --rollback ARCHIVO_PREVIO.tar.gz` cuando no haya audio en uso. Restaura el código, la configuración de selección, el módulo del dashboard y los drop-ins afectados. Los niveles actuales y las claves BlueZ se conservan. La integración del backup se puede revertir restaurando únicamente `/opt/family-plex/legacy_backup.py` desde una copia anterior a esa integración, comprobando que no tenga cambios posteriores ajenos.

Los logs se consultan con `journalctl -u bluebridge -u bluealsa -u bluealsa-aplay`. Tras agotar el límite de cinco arranques en cinco minutos, corrige la causa, ejecuta `systemctl reset-failed` para las unidades afectadas y vuelve a iniciarlas.

## Retención y seguimiento

Consulta `latest.json` y verifica el archivo indicado junto con su checksum. Compara su fecha con la última ejecución semanal. Un error del paso BlueBridge se registra como `BlueBridge private snapshot failed` en el journal de `plexserver-backup.service`. El puntero conserva la copia válida anterior.

La retención actual conserva todas las capturas. A unos 130 KiB por copia, el crecimiento semanal es pequeño. Si el tamaño aumenta, conserva como mínimo las ocho últimas capturas verificadas y una mensual de los últimos seis meses. Comprueba el archivo que señala `latest.json` antes de retirar cada par de archivo y checksum antiguo. El rollback local tiene su propia finalidad y permanece fuera de esa rotación.
