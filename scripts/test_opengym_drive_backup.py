#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("opengym_drive_backup.py")
SPEC = importlib.util.spec_from_file_location("opengym_drive_backup", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class FakeResponse:
    def __init__(self, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}
        self.status = 200

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, object]:
        return self.payload


class FakeDriveSession:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.puts: list[dict[str, object]] = []
        self.gets: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeResponse({}, {"Location": "https://upload.example/session"})

    def put(self, url: str, **kwargs: object) -> FakeResponse:
        self.puts.append({"url": url, **kwargs})
        return FakeResponse({"id": "uploaded-drive-id"})

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        return FakeResponse({
            "id": "uploaded-drive-id",
            "name": "opengym-project.tar.zst",
            "size": "7",
            "parents": ["folder"],
            "appProperties": {"sha256": hashlib.sha256(b"archive").hexdigest()},
            "md5Checksum": "wrong-md5",
        })


class BackupStagingTests(unittest.TestCase):
    def test_recursive_exclusion_keeps_required_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "source"
            project = Path(temp_name) / "stage" / "project"
            (root / "mcp" / "node_modules" / "pkg").mkdir(parents=True)
            (root / "mcp" / "README.md").write_text("mcp source\n")
            (root / "mcp" / "node_modules" / "pkg" / "index.js").write_text("generated\n")
            (root / "frontend" / "dist").mkdir(parents=True)
            (root / "frontend" / "dist" / "bundle.js").write_text("generated\n")
            (root / "data").mkdir()
            (root / "data" / "db.json").write_text("{}\n")

            excluded = backup.copy_project_tree(root, project)

            self.assertTrue((project / "mcp" / "README.md").is_file())
            self.assertFalse((project / "mcp" / "node_modules").exists())
            self.assertFalse((project / "frontend" / "dist").exists())
            self.assertFalse((project / "data").exists())
            self.assertEqual(excluded, ["project/frontend/dist/", "project/mcp/node_modules/"])

    def test_exclusion_name_files_are_preserved_in_project_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "source"
            project = Path(temp_name) / "stage" / "project"
            (root / "src").mkdir(parents=True)
            (root / "src" / "tmp").write_text("source file\n")
            (root / "src" / "dist").write_text("source file\n")
            (root / "src" / "coverage").write_text("source file\n")

            excluded = backup.copy_project_tree(root, project)

            self.assertEqual(excluded, [])
            self.assertEqual((project / "src" / "tmp").read_text(), "source file\n")
            self.assertEqual((project / "src" / "dist").read_text(), "source file\n")
            self.assertEqual((project / "src" / "coverage").read_text(), "source file\n")

    def test_symlinked_directories_are_preserved_without_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            root = temp / "source"
            project = temp / "stage" / "project"
            external = temp / "external"
            (root / "mcp").mkdir(parents=True)
            external.mkdir()
            (external / "node_modules").mkdir()
            (external / "node_modules" / "leak.js").write_text("generated\n")
            (root / "mcp" / "linked_source").symlink_to(external, target_is_directory=True)
            (root / "mcp" / "node_modules").symlink_to(external / "node_modules", target_is_directory=True)

            excluded = backup.copy_project_tree(root, project)

            self.assertTrue((project / "mcp" / "linked_source").is_symlink())
            self.assertEqual((project / "mcp" / "linked_source").readlink(), external)
            self.assertFalse((project / "mcp" / "node_modules").exists())
            self.assertEqual(excluded, ["project/mcp/node_modules/"])

    def test_manifest_records_truthful_exclusions_and_state_digests_without_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name) / "project"
            data = project / "data"
            manifest = Path(temp_name) / "OPENGYM_BACKUP_MANIFEST.json"
            (data / "tmp").mkdir(parents=True)
            (data / "tmp" / "cache.json").write_text("cache\n")
            (data / "state-alice.json").write_text('{"token":"do-not-copy-to-manifest"}\n')
            (data / "state-bob.json").write_text('{"drive_id":"file_123"}\n')
            (data / "db.json").write_text("{}\n")

            excluded = backup.remove_excluded_trees(data, project)
            state_digests = backup.state_file_digests(data, project)
            backup.write_manifest(manifest, "20260823T120000Z", excluded, state_digests)

            raw_manifest = manifest.read_text()
            parsed = json.loads(raw_manifest)
            expected_alice = hashlib.sha256((data / "state-alice.json").read_bytes()).hexdigest()
            expected_bob = hashlib.sha256((data / "state-bob.json").read_bytes()).hexdigest()

            self.assertEqual(parsed["excluded"], ["project/data/tmp/"])
            self.assertEqual(parsed["state_file_sha256"], {
                "project/data/state-alice.json": expected_alice,
                "project/data/state-bob.json": expected_bob,
            })
            self.assertNotIn("do-not-copy-to-manifest", raw_manifest)
            self.assertNotIn("file_123", raw_manifest)
            self.assertNotIn("token", raw_manifest)
            self.assertFalse((data / "tmp").exists())

    def test_exclusion_name_files_are_preserved_in_staged_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name) / "project"
            data = project / "data"
            data.mkdir(parents=True)
            (data / "tmp").write_text("source file\n")
            (data / "backups").write_text("source file\n")

            excluded = backup.remove_excluded_trees(data, project)

            self.assertEqual(excluded, [])
            self.assertEqual((data / "tmp").read_text(), "source file\n")
            self.assertEqual((data / "backups").read_text(), "source file\n")


class DriveValidationTests(unittest.TestCase):
    def test_safe_file_redacts_drive_id_and_app_properties_except_sha256(self) -> None:
        drive_id = "drive-file-id-123"
        app_secret = "app-property-secret-456"
        sha_digest = hashlib.sha256(b"archive").hexdigest()

        safe = backup.safe_file({
            "id": drive_id,
            "name": "opengym-project.tar.zst",
            "size": "7",
            "createdTime": "2026-08-23T12:00:00Z",
            "parents": ["folder", "another-folder"],
            "appProperties": {
                "sha256": sha_digest,
                "secret": app_secret,
                "drive_id": drive_id,
            },
        })

        raw_safe = json.dumps(safe, sort_keys=True)
        self.assertEqual(safe, {
            "name": "opengym-project.tar.zst",
            "size": 7,
            "created": "2026-08-23T12:00:00Z",
            "checksum": sha_digest,
            "parents_count": 2,
        })
        self.assertIn(sha_digest, raw_safe)
        self.assertNotIn(drive_id, raw_safe)
        self.assertNotIn(app_secret, raw_safe)
        self.assertNotIn("appProperties", raw_safe)

    def test_safe_error_detail_redacts_drive_urls_upload_query_and_bearer_token(self) -> None:
        drive_id = "fid123"
        upload_secret = "up789"
        token = "tok_ABC-123"
        detail = backup.safe_error_detail(RuntimeError(
            f"file https://www.googleapis.com/drive/v3/files/{drive_id}?x=1 "
            f"upload https://www.googleapis.com/upload/drive/v3/files?u={upload_secret} "
            f"auth Bearer {token}"
        ))

        self.assertIn("https://www.googleapis.com/drive/v3/files/[redacted]?x=1", detail)
        self.assertIn("https://www.googleapis.com/upload/drive/v3/files[redacted]", detail)
        self.assertIn("Bearer [redacted]", detail)
        self.assertNotIn(drive_id, detail)
        self.assertNotIn(upload_secret, detail)
        self.assertNotIn(token, detail)

    def test_upload_requests_and_validates_remote_md5_before_range_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            archive = Path(temp_name) / "opengym-project.tar.zst"
            archive.write_bytes(b"archive")
            session = FakeDriveSession()
            client = backup.DriveClient(session, "redacted-token")

            with self.assertRaisesRegex(RuntimeError, "remote MD5 checksum"):
                backup.asyncio.run(client.upload(archive, hashlib.sha256(b"archive").hexdigest(), {"id": "folder"}))

            self.assertIn("md5Checksum", session.posts[0]["params"]["fields"])
            self.assertIn("md5Checksum", session.gets[0]["params"]["fields"])
            self.assertEqual(len(session.gets), 1)

    def test_remote_md5_mismatch_fails_validation(self) -> None:
        item = {
            "name": "opengym-project.tar.zst",
            "size": "4",
            "parents": ["folder"],
            "appProperties": {"sha256": "local-sha"},
            "md5Checksum": "remote-md5",
        }

        with self.assertRaisesRegex(RuntimeError, "remote MD5 checksum"):
            backup.validate_remote_backup(item, "folder", 4, "local-sha", "local-md5")

    def test_absent_remote_md5_keeps_sha_size_parent_validation(self) -> None:
        item = {
            "name": "opengym-project.tar.zst",
            "size": "4",
            "parents": ["folder"],
            "appProperties": {"sha256": "local-sha"},
        }

        backup.validate_remote_backup(item, "folder", 4, "local-sha", "local-md5")

    def test_verify_drive_output_includes_healthy_state_and_preserves_fields(self) -> None:
        item = {
            "name": "opengym-project.tar.zst",
            "size": "7",
            "createdTime": "2026-08-23T12:00:00Z",
            "parents": ["folder"],
            "appProperties": {"sha256": hashlib.sha256(b"archive").hexdigest()},
        }

        class FakeAiohttp:
            class ClientTimeout:
                def __init__(self, **kwargs: object) -> None:
                    self.kwargs = kwargs

            class ClientSession:
                def __init__(self, **kwargs: object) -> None:
                    self.kwargs = kwargs

                async def __aenter__(self) -> "FakeAiohttp.ClientSession":
                    return self

                async def __aexit__(self, *args: object) -> None:
                    return None

        class FakeDriveClient:
            def __init__(self, session: object, token: str) -> None:
                self.session = session
                self.token = token

            async def folder(self, create: bool) -> dict[str, str]:
                self.assert_verify_mode(create)
                return {"id": "folder"}

            async def backups(self) -> list[dict[str, object]]:
                return [item]

            async def range_proof(self, remote_item: dict[str, object]) -> None:
                if remote_item is not item:
                    raise AssertionError("unexpected remote item")

            @staticmethod
            def assert_verify_mode(create: bool) -> None:
                if create:
                    raise AssertionError("verification must not create a folder")

        with mock.patch.object(sys, "argv", ["opengym_drive_backup.py", "--verify-drive"]):
            with mock.patch.object(backup, "aiohttp", FakeAiohttp):
                with mock.patch.object(backup, "access_token", mock.AsyncMock(return_value="redacted-token")):
                    with mock.patch.object(backup, "DriveClient", FakeDriveClient):
                        with mock.patch("builtins.print") as print_mock:
                            backup.main()

        print_mock.assert_called_once()
        output = json.loads(print_mock.call_args.args[0])
        self.assertEqual(output["state"], "HEALTHY")
        self.assertEqual(output["folder_name"], "OpenGym Backups")
        self.assertTrue(output["remote_verified"])
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["range_reads_verified"], 1)
        self.assertEqual(output["files"], [{
            "name": "opengym-project.tar.zst",
            "size": 7,
            "created": "2026-08-23T12:00:00Z",
            "checksum": hashlib.sha256(b"archive").hexdigest(),
            "parents_count": 1,
        }])


if __name__ == "__main__":
    unittest.main()
