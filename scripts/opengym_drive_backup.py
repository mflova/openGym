#!/usr/bin/env python3
"""Create and verify OpenGym project backups in an app-owned Google Drive folder.

The Google Drive OAuth grant is reused from Home Assistant's configured Google Drive
integration. The MCP server is deliberately not involved: it is read-only and only
answers questions from ./data.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import aiohttp
except ModuleNotFoundError:
    aiohttp = None

PROJECT_DIR = Path("/home/mflova/git/openGym")
STATE_DIR = Path("/home/mflova/backups/opengym")
HA_CONFIG_DIR = Path("/home/mflova/docker/homeassistant/config")
SERVICE_UNIT = Path("/home/mflova/.config/systemd/user/opengym-drive-backup.service")
TIMER_UNIT = Path("/home/mflova/.config/systemd/user/opengym-drive-backup.timer")
CONTAINER = "opengym-api-1"
RETENTION = 3
INSTANCE_ID = "opengym-rpi"
FOLDER_NAME = "OpenGym Backups"
FOLDER_PROPERTY = "opengym_backup_folder"
FILE_PROPERTY = "opengym_backup"
EXCLUDED_NAMES = {".git", "node_modules", "dist", "coverage", ".cache", "tmp", "backups"}
AUTH_CONFIG = HA_CONFIG_DIR / ".storage" / "core.config_entries"
APPLICATION_CREDENTIALS = HA_CONFIG_DIR / ".storage" / "application_credentials"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, sort_keys=True), flush=True)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    return file_digest(path, "sha256")


def md5(path: Path) -> str:
    return file_digest(path, "md5")


def staged_path(path: Path, root: Path) -> str:
    return f"project/{path.relative_to(root).as_posix()}"


def staged_dir_path(path: Path, root: Path) -> str:
    return f"{staged_path(path, root)}/"


def is_excluded_tree(path: Path) -> bool:
    return path.name in EXCLUDED_NAMES and path.is_dir()


def copy_project_tree(source: Path, target: Path) -> list[str]:
    excluded: list[str] = []
    target.mkdir(parents=True, exist_ok=True)

    def copy_member(member: Path, destination: Path) -> None:
        if is_excluded_tree(member):
            excluded.append(staged_dir_path(member, source))
            return
        if member.name == "data" and member.parent == source:
            return
        if member.is_symlink():
            destination.symlink_to(member.readlink())
        elif member.is_dir():
            destination.mkdir()
            for child in sorted(member.iterdir(), key=lambda item: item.name):
                copy_member(child, destination / child.name)
        else:
            shutil.copy2(member, destination, follow_symlinks=False)

    for member in sorted(source.iterdir(), key=lambda item: item.name):
        copy_member(member, target / member.name)
    return sorted(excluded)


def remove_excluded_trees(root: Path, project_root: Path) -> list[str]:
    excluded: list[str] = []
    for member in sorted(root.rglob("*"), key=lambda item: item.relative_to(project_root).as_posix(), reverse=True):
        if not is_excluded_tree(member):
            continue
        excluded.append(staged_dir_path(member, project_root))
        if member.is_symlink():
            member.unlink()
        else:
            shutil.rmtree(member)
    return sorted(excluded)


def state_file_digests(data_stage: Path, project_root: Path) -> dict[str, str]:
    return {
        staged_path(path, project_root): sha256(path)
        for path in sorted(data_stage.glob("state-*.json"), key=lambda item: item.name)
        if path.is_file() and not path.is_symlink()
    }


def write_manifest(manifest: Path, timestamp: str, excluded: Iterable[str], state_digests: dict[str, str]) -> None:
    manifest.write_text(json.dumps({
        "format": 2,
        "created_utc": timestamp,
        "contract": "Complete OpenGym project snapshot; project data is in project/data/.",
        "included": [
            "project/ (excluding generated/dependency trees by reserved directory name at any depth)",
            "systemd/opengym-drive-backup.service",
            "systemd/opengym-drive-backup.timer",
        ],
        "excluded": sorted(set(excluded)),
        "state_file_sha256": state_digests,
        "mcp": "The MCP server is read-only and is included as source; it is not used to create this backup.",
        "restore": "Restore project/ to /home/mflova/git/openGym, restore systemd units, then docker compose up -d.",
    }, indent=2, sort_keys=True) + "\n")


def archive_snapshot(timestamp: str) -> tuple[Path, Path, str]:
    if not PROJECT_DIR.is_dir() or not (PROJECT_DIR / "docker-compose.yml").is_file():
        raise RuntimeError("OpenGym project or docker-compose.yml is missing")
    if not (PROJECT_DIR / "data").is_dir():
        raise RuntimeError("OpenGym data directory is missing")
    for unit in (SERVICE_UNIT, TIMER_UNIT):
        if not unit.is_file():
            raise RuntimeError(f"required systemd unit is missing: {unit}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    archive = STATE_DIR / f"opengym-project-{timestamp}.tar.zst"
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")
    with tempfile.TemporaryDirectory(prefix="opengym-snapshot-", dir=STATE_DIR) as temp_name:
        stage = Path(temp_name)
        payload = stage / "project"
        manifest = stage / "OPENGYM_BACKUP_MANIFEST.json"
        excluded = copy_project_tree(PROJECT_DIR, payload)

        # The API container owns data/secret and data/vapid.json (mode 0600). Docker
        # copies the mounted data as the service user without making those files
        # world-readable or requiring an interactive sudo password.
        data_stage = payload / "data"
        data_stage.mkdir()
        run(["docker", "cp", f"{CONTAINER}:/data/.", str(data_stage)])
        excluded.extend(remove_excluded_trees(data_stage, payload))
        state_digests = state_file_digests(data_stage, payload)

        systemd_stage = stage / "systemd"
        systemd_stage.mkdir()
        shutil.copy2(SERVICE_UNIT, systemd_stage / SERVICE_UNIT.name)
        shutil.copy2(TIMER_UNIT, systemd_stage / TIMER_UNIT.name)
        write_manifest(manifest, timestamp, excluded, state_digests)
        run(["tar", "--zstd", "--create", "--file", str(archive), "--directory", str(stage), "project", "systemd", manifest.name])

    required = {
        "project/docker-compose.yml",
        "project/data/db.json",
        "project/mcp/README.md",
        "systemd/opengym-drive-backup.service",
        "systemd/opengym-drive-backup.timer",
        "OPENGYM_BACKUP_MANIFEST.json",
    }
    listing = set(run(["tar", "--zstd", "--list", "--file", str(archive)]).stdout.splitlines())
    missing = sorted(required - listing)
    if missing:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"archive required paths are missing: {missing}")
    digest = sha256(archive)
    checksum_path.write_text(f"{digest}  {archive.name}\n")
    if sha256(archive) != digest:
        raise RuntimeError("archive checksum verification failed")
    return archive, checksum_path, digest


def load_drive_credentials() -> tuple[str, str, str]:
    entries = json.loads(AUTH_CONFIG.read_text())["data"]["entries"]
    entry = next(item for item in entries if item["domain"] == "google_drive")
    token = entry["data"]["token"]
    implementation = entry["data"].get("auth_implementation")
    applications = json.loads(APPLICATION_CREDENTIALS.read_text())["data"]["items"]
    application = next((item for item in applications if item.get("id") == implementation), None)
    if application is None:
        application = next(item for item in applications if item.get("domain") == "google_drive")
    return token["refresh_token"], application["client_id"], application["client_secret"]


def clean(value: Any) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or ""))[:180]


def safe_error_detail(error: Exception) -> str:
    detail = clean(error)
    detail = re.sub(r"https://www\.googleapis\.com/drive/v3/files/[^ ?]+", f"{DRIVE_FILES_URL}/[redacted]", detail)
    detail = re.sub(r"https://www\.googleapis\.com/upload/drive/v3/files[^ ]*", "https://www.googleapis.com/upload/drive/v3/files[redacted]", detail)
    detail = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[redacted]", detail, flags=re.IGNORECASE)
    return detail


def safe_file(item: dict[str, Any]) -> dict[str, Any]:
    props = item.get("appProperties", {})
    return {
        "name": clean(item.get("name")),
        "size": int(item.get("size", 0)),
        "created": item.get("createdTime"),
        "checksum": clean(props.get("sha256", "")),
        "parents_count": len(item.get("parents", [])),
    }


def validate_remote_backup(item: dict[str, Any], folder_id: str, size: int, sha256_digest: str, md5_digest: str) -> None:
    if item.get("appProperties", {}).get("sha256") != sha256_digest:
        raise RuntimeError("remote SHA-256 metadata does not match local archive")
    if folder_id not in item.get("parents", []):
        raise RuntimeError("remote backup is not in the OpenGym Drive folder")
    if int(item.get("size", 0)) != size:
        raise RuntimeError("remote backup size does not match local archive")
    remote_md5 = item.get("md5Checksum")
    if remote_md5 and remote_md5.lower() != md5_digest:
        raise RuntimeError("remote MD5 checksum does not match local archive")


class DriveClient:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}"}

    async def list_files(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token = None
        while True:
            params: dict[str, str] = {
                "q": query,
                "orderBy": "createdTime desc",
                "fields": "nextPageToken,files(id,name,mimeType,size,createdTime,parents,appProperties,md5Checksum)",
            }
            if page_token:
                params["pageToken"] = page_token
            async with self.session.get(DRIVE_FILES_URL, params=params, headers=self.headers) as response:
                response.raise_for_status()
                data = await response.json()
            files.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return files

    async def folder(self, create: bool) -> dict[str, Any]:
        query = (
            "mimeType='application/vnd.google-apps.folder' and "
            f"appProperties has {{ key='{FOLDER_PROPERTY}' and value='true' }} and "
            f"appProperties has {{ key='instance_id' and value='{INSTANCE_ID}' }} and trashed=false"
        )
        found = await self.list_files(query)
        if len(found) > 1:
            raise RuntimeError(f"OpenGym backup folder selection found {len(found)} app-owned folders")
        if found:
            return found[0]
        if not create:
            raise RuntimeError("OpenGym backup folder is absent")
        body = {
            "name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {FOLDER_PROPERTY: "true", "instance_id": INSTANCE_ID, "backup_layout": "v1"},
        }
        async with self.session.post(
            DRIVE_FILES_URL,
            params={"fields": "id,name,mimeType,parents,appProperties,md5Checksum"},
            headers={**self.headers, "Content-Type": "application/json"},
            data=json.dumps(body),
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def backups(self) -> list[dict[str, Any]]:
        query = (
            f"appProperties has {{ key='{FILE_PROPERTY}' and value='true' }} and "
            f"appProperties has {{ key='instance_id' and value='{INSTANCE_ID}' }} and trashed=false"
        )
        return await self.list_files(query)

    async def range_proof(self, item: dict[str, Any]) -> None:
        if int(item.get("size", 0)) == 0:
            raise RuntimeError("remote backup is empty")
        async with self.session.get(
            f"{DRIVE_FILES_URL}/{item['id']}",
            params={"alt": "media"},
            headers={**self.headers, "Range": "bytes=0-511"},
        ) as response:
            if response.status not in (200, 206):
                raise RuntimeError(f"authenticated range read failed: HTTP {response.status}")
            if not await response.content.read(1):
                raise RuntimeError("authenticated range read was empty")

    async def upload(self, archive: Path, digest: str, folder: dict[str, Any]) -> dict[str, Any]:
        size = archive.stat().st_size
        md5_digest = md5(archive)
        metadata = {
            "name": archive.name,
            "mimeType": "application/zstd",
            "parents": [folder["id"]],
            "appProperties": {
                FILE_PROPERTY: "true",
                "instance_id": INSTANCE_ID,
                "backup_kind": "full-project",
                "sha256": digest,
                "format": "tar.zst",
            },
        }
        init_headers = {
            **self.headers,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/zstd",
            "X-Upload-Content-Length": str(size),
        }
        async with self.session.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "resumable", "fields": "id,name,size,createdTime,parents,appProperties,md5Checksum"},
            headers=init_headers,
            data=json.dumps(metadata),
        ) as response:
            response.raise_for_status()
            upload_url = response.headers["Location"]
        with archive.open("rb") as payload:
            async with self.session.put(
                upload_url,
                headers={**self.headers, "Content-Type": "application/zstd", "Content-Length": str(size)},
                data=payload,
            ) as response:
                response.raise_for_status()
                uploaded = await response.json()
        async with self.session.get(
            f"{DRIVE_FILES_URL}/{uploaded['id']}",
            params={"fields": "id,name,size,createdTime,parents,appProperties,md5Checksum"},
            headers=self.headers,
        ) as response:
            response.raise_for_status()
            confirmed = await response.json()
        validate_remote_backup(confirmed, folder["id"], size, digest, md5_digest)
        await self.range_proof(confirmed)
        return confirmed

    async def delete(self, item: dict[str, Any]) -> None:
        async with self.session.delete(f"{DRIVE_FILES_URL}/{item['id']}", headers=self.headers) as response:
            response.raise_for_status()

    async def prune_and_verify(self, folder: dict[str, Any]) -> dict[str, Any]:
        items = await self.backups()
        if any(folder["id"] not in item.get("parents", []) for item in items):
            raise RuntimeError("an OpenGym backup is outside the app-owned Drive folder")
        for old in items[RETENTION:]:
            await self.delete(old)
        after = await self.backups()
        if len(after) > RETENTION:
            raise RuntimeError(f"OpenGym Drive retention exceeds {RETENTION}")
        for item in after:
            await self.range_proof(item)
        return {
            "count": len(after),
            "files": [safe_file(item) for item in after],
            "folder_name": FOLDER_NAME,
            "range_reads_verified": len(after),
        }


async def access_token() -> str:
    if aiohttp is None:
        raise RuntimeError("aiohttp is required for Google Drive access")
    refresh_token, client_id, client_secret = load_drive_credentials()
    timeout = aiohttp.ClientTimeout(total=120, connect=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        ) as response:
            response.raise_for_status()
            return (await response.json())["access_token"]


async def drive_run(archive: Path | None, digest: str | None, verify_only: bool) -> dict[str, Any]:
    if aiohttp is None:
        raise RuntimeError("aiohttp is required for Google Drive access")
    token = await access_token()
    timeout = aiohttp.ClientTimeout(total=4 * 3600, connect=120, sock_connect=120, sock_read=4 * 3600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = DriveClient(session, token)
        folder = await client.folder(create=not verify_only)
        if verify_only:
            items = await client.backups()
            if len(items) > RETENTION:
                raise RuntimeError(f"OpenGym Drive retention exceeds {RETENTION}")
            if any(folder["id"] not in item.get("parents", []) for item in items):
                raise RuntimeError("an OpenGym backup is outside the app-owned Drive folder")
            for item in items:
                await client.range_proof(item)
            return {
                "state": "HEALTHY",
                "folder_name": FOLDER_NAME,
                "remote_verified": True,
                "count": len(items),
                "files": [safe_file(x) for x in items],
                "range_reads_verified": len(items),
            }
        assert archive is not None and digest is not None
        confirmed = await client.upload(archive, digest, folder)
        retention = await client.prune_and_verify(folder)
        return {"uploaded": safe_file(confirmed), **retention}


def prune_local() -> None:
    archives = sorted(STATE_DIR.glob("opengym-project-*.tar.zst"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in archives[RETENTION:]:
        old.unlink(missing_ok=True)
        old.with_suffix(old.suffix + ".sha256").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--run", action="store_true")
    actions.add_argument("--create-only", action="store_true")
    actions.add_argument("--verify-drive", action="store_true")
    args = parser.parse_args()
    if args.verify_drive:
        print(json.dumps(asyncio.run(drive_run(None, None, True)), sort_keys=True))
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive, checksum, digest = archive_snapshot(timestamp)
    emit("archive_verified", archive=archive.name, size=archive.stat().st_size, checksum=digest, checksum_file=checksum.name)
    if args.create_only:
        return
    result = asyncio.run(drive_run(archive, digest, False))
    prune_local()
    emit("drive_backup_verified", **result)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("failed", error=type(error).__name__, detail=safe_error_detail(error))
        raise
