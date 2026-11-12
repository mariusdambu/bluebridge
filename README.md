# BlueBridge

Receptor Bluetooth A2DP para Debian 13 con BlueZ, BlueALSA y ALSA. Funciona con systemd y puede integrarse como tarjeta en un dashboard autenticado.

El controlador selecciona el adaptador principal y mantiene el secundario en reserva. El reproductor utiliza la Scarlett Solo cuando está disponible y pasa a la salida analógica integrada si se retira. Las salidas HDMI y DisplayPort quedan excluidas. El volumen base se guarda por tarjeta y el teléfono regula su propio nivel.

- [Instalación e integración](docs/INSTALACION.md)
- [Copia, restauración y rollback](docs/RECUPERACION.md)
- [Pruebas y alcance de la versión](docs/VALIDACION.md)

En el teléfono, selecciona **Yamaha HS4 Wireless** y reproduce desde la aplicación habitual. Un solo cliente conserva la conexión de audio. Para cambiar de fuente, desconéctalo desde sus ajustes Bluetooth o desde los controles avanzados del dashboard.

La versión estable de referencia es **v2.4.1**. La matriz de validación distingue el uso confirmado de los escenarios de fallo que aún carecen de prueba específica. El proyecto se distribuye con licencia MIT.
