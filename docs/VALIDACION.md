# Validación de v2.4.1

El propietario confirmó el funcionamiento general de v2.4.1 en pruebas manuales. Pasaron 50 pruebas en Linux y una comprobación del renderizado JavaScript. Para reproducirlas usa `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`. Las pruebas Linux de ALSA y sockets se omiten en macOS.

| Escenario | Cobertura y evidencia |
|---|---|
| Retirada y reconexión del TP-Link | Detección por identidad y selección periódica. Retirada registrada en v2.4. Ciclo completo específico de v2.4.1 sin registrar. |
| Conmutación a ASUS | Validada en v2.4. El teléfono puede necesitar seleccionar el otro emparejamiento con el mismo alias. |
| Retorno a TP-Link | Se prioriza al quedar libre la conexión de respaldo. Retorno con un cliente conectado al ASUS sin prueba específica registrada. |
| Pérdida y retorno de Scarlett | Detección por identidad USB/ALSA y arranque del PCM correspondiente. Cambios registrados en v2.4. |
| Realtek y retorno a Scarlett | Selección de ambos caminos registrada en v2.4. Realtek observado en v2.4.1. Ciclo físico con niveles distintos sin registro. |
| Fallo temporal de BlueALSA | systemd reintenta con límite. Fallo inyectado durante audio pendiente. |
| Reproductor bloqueado o sin audio | Watchdog de supervisor y recuperación limitada del PCM si hay XRUN o puntero detenido con transporte activo. Lógica comprobada. Fallo real durante reproducción pendiente. Silencio y fallos de altavoces pueden resultar indistinguibles. |
| Pérdida de conexión Bluetooth | El puesto queda libre al caer el enlace. La reconexión la inicia el cliente y puede requerir selección manual. Pérdida inesperada sin prueba específica. |
| Reinicio de servicios | Botón probado en v2.4. Arranque controlado de las unidades comprobado en v2.4.1. Puede requerir reconexión manual. |
| Arranque completo | Journal confirma inicio automático en el arranque del 12/11/2026 y estado operativo posterior. Reproducción inmediatamente después del arranque y recuperación de un corte prolongado sin registro. |
| Conexiones simultáneas | Política de un cliente y exclusión del segundo conservadas. Prueba física simultánea sin registro. |
| Reemparejar tras olvidar | Validado en v2.4 con ambas radios. Se conserva `JustWorksRepairing = always`. |

## Volumen y dashboard

El slider permite un ajuste de 1 %. Se conserva la curva de amplitud y la separación del volumen Bluetooth. Los 256 pasos del control ALSA cuantizan la ganancia entre −60 y 0 dB, por lo que varios porcentajes altos pueden compartir un paso y los cambios a niveles bajos pueden percibirse más. La precisión resulta adecuada para el ajuste doméstico. El diseño guarda los niveles por ID y reaplica el correspondiente al cambiar de salida. Las pruebas verifican independencia y persistencia en el JSON. La Scarlett utiliza el mismo mecanismo cuando el control softvol pasa la verificación. No consta una medición específica en Scarlett ni un ciclo físico con valores distintos.

Realtek confirmó un cambio en 127 ms desde la petición al backend hasta la lectura ALSA, con la preferencia original restaurada después. La latencia audible no se midió. La tarjeta muestra la salida seleccionada y su slider. Los estados secundarios y la desconexión quedan en un desplegable inicialmente cerrado. Los controles sin capacidad confirmada se deshabilitan. El estado activo exige adaptador y salida preparados, con estado reciente del reproductor. El indicador de flujo digital no certifica que se oigan los altavoces.

La verificación de los hashes del respaldo y su contenido acredita integridad. Falta probar una restauración completa en otro sistema.
