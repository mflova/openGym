# OpenGym backups

OpenGym backups use the Google Drive OAuth grant already configured in Home Assistant. The optional OpenGym MCP server is not part of the backup mechanism: it is a local, read-only query bridge over `./data`.

## Schedule and retention

- `opengym-drive-backup.timer` is enabled as a user timer and runs because user linger is enabled.
- The timer uses a three-day cadence anchored on timer activation (`OnActiveSec=3d`) and re-arms after each completed oneshot (`OnUnitInactiveSec=3d`).
- `Persistent=true` catches up a missed run after the user systemd manager or Raspberry Pi returns; it does not rely on a boot-only timer.
- The Drive folder is app-owned and named `OpenGym Backups`.
- The newest 3 verified Drive archives are retained; local archives under `/home/mflova/backups/opengym` are also retained up to 3.
- The archive is not client-side encrypted. Google Drive provides TLS in transit and provider-managed encryption at rest. The archive contains OpenGym's `data/secret`, so anyone who can read the archive can impersonate an OpenGym session.

## Coverage

The archive includes the complete OpenGym project: `data/` (profiles, passkeys, session secret, VAPID keys, subscriptions, invites and per-user workout state), `.env`, Compose files, source, documentation, media, and the read-only `mcp/` source. The exact systemd units and a manifest are included for restore.

Generated or reproducible trees excluded from the archive are `.git/`, `node_modules/`, `dist/`, `coverage/`, `.cache/`, `tmp/`, and `backups/` wherever those directory names appear in the staged project tree, including nested paths such as `mcp/node_modules/`. Required source files such as `mcp/README.md` remain included. Exercise media is included because it is part of this self-hosted project, even though it can also be re-downloaded from the upstream dataset.

`OPENGYM_BACKUP_MANIFEST.json` is written after the staged project and data copy are prepared. It records the actual excluded staged paths and non-secret SHA-256 digests for each staged `project/data/state-*.json` file; it does not include file contents, tokens, credentials, Drive IDs, or other secrets.

This does not back up the Raspberry Pi operating system, Docker images, unrelated Compose projects/volumes, or host files outside OpenGym.

## Inspection

```bash
systemctl --user list-timers opengym-drive-backup.timer --all
journalctl --user -u opengym-drive-backup.service --no-pager
python3 /home/mflova/git/openGym/scripts/opengym_drive_backup.py --verify-drive
```

## Restore

1. Install Linux/Docker on the replacement host and restore the OpenGym project to `/home/mflova/git/openGym`.
2. Extract the newest `opengym-project-*.tar.zst` archive so its `project/` contents replace that project directory and its `systemd/` contents replace the user units.
3. Run `systemctl --user daemon-reload` and enable the timer.
4. Start OpenGym with `docker compose up -d` from the project directory.
5. Verify the profiles and training history before allowing users to sign in.

The archive is created and uploaded by `/home/mflova/git/openGym/scripts/opengym_drive_backup.py`; it validates required paths, SHA-256 metadata, Drive `md5Checksum` when Google provides it, Drive folder parentage, retention, and authenticated range reads.
