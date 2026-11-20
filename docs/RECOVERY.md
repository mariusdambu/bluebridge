# Backup, Recovery, and Rollback

Run as `root`: `python3 /opt/bluebridge/backup.py --destination /private/backup/bluebridge` on a mounted and verified backup target. Verify with `python3 /opt/bluebridge/backup.py --verify ARCHIVE.tar.gz`. Keep the archive and its `.sha256` sidecar together. The embedded manifest records Debian package versions, unit enablement states, present paths, and per-file SHA-256 checksums. Capture retries automatically if files change while being read.

On Family Plex Server, `scripts/attach_backup.py` generates a scoped integration patch for the `legacy_backup.py` wrapper. Running the same script with `--dashboard` integrates the combined backup status into the host dashboard. Review both diffs before installing them. The BlueBridge snapshot runs after application writers are restored and stores archives under `server-config/bluebridge` on the verified USB backup drive. `latest.json` in that directory points to the latest verified BlueBridge archive. Both the "Create backup now" button and the weekly timer invoke the same systemd unit. "OK" reflects BlueBridge verification as well. If only the BlueBridge step fails, the card displays a warning and technical details remain in the systemd journal, while the Plex mirror marker keeps tracking its own backup status.

The archive includes code, configuration, installed Bluetooth firmware, udev rules, systemd units, volume levels, dashboard modules, and private BlueZ, BlueALSA, and ALSA state. Keep it restricted to `root`. The recorded package list helps reproduce system dependencies; the archive is not a full system image.

## Reinstallation

1. Verify the archive and extract it as `root` into a private staging directory outside live paths. Inspect the manifest and recreate packages, system user, and group according to the installation guide.
2. Restore code, systemd units, udev rule, and configuration selectively. Adjust ownership if UID/GID changed. Integrate the dashboard module into the current dashboard version while preserving its authentication controls. Avoid overwriting unrelated dashboard or backup wrapper changes.
3. Restore Bluetooth pairings with BlueZ and BlueALSA stopped, and only if each adapter hardware identity matches. Preserve original file permissions. A replacement Bluetooth adapter typically requires pairing again. Review firmware and ALSA selection before re-enabling services.
4. Start the services and verify alias, playback, volume levels, and fallback behavior. Checksum verification alone only proves archive integrity.

## Rolling Back to v2.4

From the deployment tree, run as `root`: `python3 scripts/update.py --rollback PREVIOUS_ARCHIVE.tar.gz` while no audio is playing. This restores code, adapter selection config, the dashboard module, and affected systemd drop-ins. Current volume levels and BlueZ pairing keys are preserved. The backup hook integration can be reverted by restoring only `/opt/family-plex/legacy_backup.py` from a snapshot prior to integration, verifying no unrelated newer edits exist.

Inspect logs with `journalctl -u bluebridge -u bluealsa -u bluealsa-aplay`. If the five-restarts-in-five-minutes limit is hit, fix the underlying issue, run `systemctl reset-failed` on the affected units, and start them again.

## Retention and Monitoring

Check `latest.json` and verify the referenced archive against its `.sha256` checksum, comparing its timestamp with the last weekly run. BlueBridge snapshot errors are logged as `BlueBridge private snapshot failed` in the `plexserver-backup.service` journal, and `latest.json` retains the previous valid archive.

Current retention keeps all snapshots. At roughly 130 KiB per snapshot, weekly growth is minimal. If archive size grows, keep at least the eight most recent verified snapshots plus one monthly snapshot for the past six months, verifying the target of `latest.json` before pruning older archive/checksum pairs. Keep local rollback archives separate from routine rotation.
