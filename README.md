# BlueBridge

Bluetooth A2DP audio receiver for Debian 13 with BlueZ, BlueALSA, and ALSA. Runs under systemd and can be integrated as a card inside an authenticated dashboard.

The controller selects the primary Bluetooth adapter and keeps the secondary adapter in reserve. The player routes audio to the Scarlett Solo when available and falls back to the integrated analog output if unplugged or if ALSA initialization stalls. HDMI and DisplayPort outputs are excluded. Base volume is stored per card while the phone regulates its own level independently.

- [Installation and integration](docs/INSTALLATION.md)
- [Backup, recovery, and rollback](docs/RECOVERY.md)
- [Validation and release scope](docs/VALIDATION.md)

On your phone, select **Yamaha HS4 Wireless** and play from your usual audio app. A single client holds the audio connection at a time. To switch sources, disconnect the active client from its Bluetooth settings or from the dashboard's advanced controls.

The current stable release is **v2.4.2** (with `v2.4.1` preserved intact as the previous stable baseline). The validation matrix distinguishes confirmed usage from failure scenarios that still lack dedicated testing. Distributed under the MIT License.
