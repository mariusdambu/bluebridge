# Installation and Integration

Audio flows through BlueZ -> BlueALSA A2DP Sink -> `bluealsa-aplay` -> ALSA PCM -> analog output. The player supervisor manages output switching. The controller manages the two adapters, enforces the single-client policy, and publishes local runtime state. The dashboard reads that state and uses a local Unix socket for bounded operations. Audio playback works without the dashboard.

This release was tested on Debian 13 with BlueZ 5.82, BlueALSA 4.3.1, Python 3, systemd, and ALSA 1.2.14. Required Debian packages: `bluez`, `bluez-alsa-utils`, `alsa-utils`, `python3`, `python3-dbus`, `python3-gi`. The TP-Link RTL8761BU adapter uses `firmware-realtek`. Check the kernel log for ASUS BCM20702A1 firmware matching its USB ID, obtain firmware from an authorized source, and store it on the system or in your private backup.

These instructions cover a fresh installation. To upgrade an existing v2.4 deployment on Family Plex Server, run `scripts/update.py` from a private directory on the NAS containing this repository tree. The script requires idle audio, preserves configuration, and creates a rollback snapshot.

1. Inspect both adapters with `bluetoothctl list` and the Scarlett with `lsusb` and `aplay -l`. Record both controller addresses and the USB serial number in a **private** copy of `config/receiver.example.json`. For a single-adapter setup, use the same address in both fields.
2. Create the system user `bluebridge` with primary group `audio`, no login shell, and no home directory. Install the modules from `src/` into `/opt/bluebridge/` owned by `root`, with directory mode `0755` and file mode `0644`. Also install `scripts/backup.py` in that path.
3. Install the private JSON as `/etc/bluebridge/config.json` and a copy of `config/volumes.example.json` as `/etc/bluebridge/volumes.json`. Use directory `root:audio` `0750` and files `root:audio` `0640`. Start with low physical levels on your amplifiers.
4. Fill in the USB serial number in `config/71-bluebridge.rules.example` and install the rule as `/etc/udev/rules.d/71-bluebridge.rules`. Reload rules with `udevadm control --reload-rules` and reconnect only the Scarlett. Verify that its ALSA card ID is `BlueBridge`.
5. Install `systemd/v2.4.1/bluebridge.service` into `/etc/systemd/system/`. Copy `bluealsa.conf` and `bluealsa-aplay.conf` as `bluebridge.conf` inside `bluealsa.service.d/` and `bluealsa-aplay.service.d/` respectively. Install `watchdog.conf` as `90-bluebridge-recovery.conf` in the drop-in directories of `bluebridge.service` and `bluealsa-aplay.service`, and `restart-limit.conf` under that same filename in `bluealsa.service.d/`.
6. If open family re-pairing is desired, set `JustWorksRepairing = always` under `[General]` in `/etc/bluetooth/main.conf`. This is a global BlueZ setting: it simplifies re-pairing and allows pairing attempts from nearby devices. Load this configuration when starting Bluetooth on a fresh install.
7. Run `systemctl daemon-reload`, validate with `systemd-analyze --man=no verify bluebridge.service bluealsa.service bluealsa-aplay.service`, and run `systemctl enable --now bluetooth.service bluealsa.service bluealsa-aplay.service bluebridge.service`. Check `systemctl status bluebridge bluealsa bluealsa-aplay` and the state file `/run/bluebridge/state.json`. Pair and test output from a phone before raising speaker volume to normal listening levels.

## Authenticated Dashboard

On Family Plex Server, install `src/bluebridge_dashboard.py` into `/opt/family-plex/`. The host dashboard imports the module, embeds `card(csrf)` in the main page, includes `bluebridge: status()` in its live status JSON, and passes that object to `window.updateBlueBridge` during its existing refresh cycle. Keep the visual classes `card`, `pill`, and `button`.

The `POST /api/bluebridge` handler must preserve the dashboard's session, network, `Host`, and `Origin` checks, verify CSRF, and enforce request body size limits before calling `operate(data)`. The JSON response returns `ok` and `message`. The process requires the administrative identity of the existing dashboard for bounded systemd and preference operations. Never expose this route as a public or unauthenticated API.

The initial view displays service status, the restart button, the active Bluetooth adapter, and the selected sound card with its base volume slider. Connected clients, manual disconnect controls, and secondary states remain inside a collapsible details section closed by default.

The slider batches updates during dragging. The player receives a local Unix datagram wakeup and applies the level to the private ALSA softvol control with readback verification. The gain curve maps `100%` to `0 dB`, `50%` near `-6 dB`, and `10%` near `-20 dB`. Each card stores its own preference in the shared levels JSON. Steps are quantized by softvol, making perceived changes steeper at very low percentages.
