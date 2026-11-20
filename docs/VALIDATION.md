# Release Validation (v2.4.1 / v2.4.2)

General operation of v2.4.1 and the v2.4.2 player/backup updates was confirmed in live production usage. All 56 unit tests pass on Linux (54 pass and 2 Linux-only ALSA/socket tests are skipped on macOS), along with JavaScript rendering verification. Reproduce with `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests`.

| Scenario | Coverage and evidence |
|---|---|
| TP-Link unplug and reconnect | Identity-based detection and periodic selection. Unplug recorded in v2.4; full v2.4.1+ cycle not separately logged. |
| Failover to ASUS adapter | Validated in v2.4. Phones may require selecting the second pairing sharing the same alias. |
| Return to TP-Link | Prioritized once the fallback connection becomes free. Return with an active client on ASUS not separately recorded. |
| Scarlett loss and return | USB/ALSA identity detection and PCM launch. Live switch from Scarlett Solo (`card 2`) to integrated analog (`card 1`) confirmed in v2.4.2 production logs. |
| Realtek and return to Scarlett | Both paths recorded in v2.4; Realtek and Scarlett-to-Realtek transition verified in v2.4.2. Full multi-level physical cycle not separately recorded. |
| Temporary BlueALSA failure | systemd retries with bounded restart limits. Injected failure during active audio still pending. |
| Stalled or silent player | Supervisor watchdog and bounded PCM recovery on XRUN or stalled pointer with active transport. Logic unit-tested; live fault injection during playback pending. |
| Bluetooth connection drop | Slot is freed when link drops. Reconnection is client-initiated and may require manual selection. Unexpected drop not separately logged. |
| Service restart | Dashboard restart button validated in v2.4; controlled unit startup verified in v2.4.1/v2.4.2. May require manual phone reconnect. |
| Full cold boot | Journal confirms automatic service startup and operational state. Immediate post-boot playback and long power-outage recovery not separately logged. |
| Simultaneous connections | Single-client policy and second-client disconnect preserved. Simultaneous physical test not separately recorded. |
| Re-pair after forgetting | Validated in v2.4 with both adapters. `JustWorksRepairing = always` preserved. |

## Volume and Dashboard

The slider provides 1% granularity while preserving the amplitude curve and separation from Bluetooth phone volume. The 256 ALSA softvol steps quantize gain across `-60 dB` to `0 dB`, so several high percentages may share a step and adjustments at very low percentages feel steeper. Levels persist per card ID in JSON and are reapplied on output switch. Automated tests verify independence and persistence. Scarlett uses the same mechanism when the softvol control passes verification; dedicated audible latency measurements on Scarlett remain pending.

On Realtek, a backend-to-ALSA readback adjustment completed in 127 ms while preserving the existing preference file. Audible latency was not measured. The card displays the active output and its slider; secondary statuses and client disconnect controls stay in a collapsed details section. Unverified controls remain disabled. "Active" status requires a ready adapter, a ready output, and fresh player state. Digital stream activity does not verify powered speakers or physical gain knobs.

Backup archive checksum and manifest verification confirms integrity; full bare-metal restoration onto a second system remains pending.

## v2.4.2 — Scarlett ALSA Initialization Fallback

If the Scarlett appears on USB but its ALSA PCM is slow to become ready, BlueBridge grants up to six seconds from first detecting the delay before falling back to integrated analog audio (Realtek). If Realtek is already active, it continues playing uninterrupted while waiting. If Scarlett is absent, Realtek is selected immediately. After waiting or while on Realtek, the Scarlett PCM must remain continuously ready for four seconds before switching back. Switching follows the existing polling cycle and may cause a brief audio interruption.

All edge cases—including intermittent ALSA availability and HDMI exclusion—were verified via simulated USB/ALSA tests (56 tests passing) and deployed to production alongside the combined backup status integration.
