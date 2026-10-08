"""Unit tests for the 9Router manager."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.router_manager import DEFAULT_MODEL, DEFAULT_ROUTER_URL, NineRouterManager


class TestNineRouterManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app_root = Path(self.temp_dir.name)
        self.bin_dir = self.app_root / "app" / "bin"
        self.bin_dir.mkdir(parents=True)
        self.manager = NineRouterManager(app_root=self.app_root)

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.temp_dir.cleanup()

    def test_find_binary_in_app_bin(self) -> None:
        binary_name = "9router.exe" if sys.platform == "win32" else "9router"
        fake_binary = self.bin_dir / binary_name
        # On Windows, real PE binary starts with MZ and must be >= 10KB
        payload = b"MZ" + b"\x00" * 20000 if sys.platform == "win32" else b"#!/bin/sh\nexit 0\n"
        fake_binary.write_bytes(payload)
        fake_binary.chmod(0o755)

        found = self.manager.find_binary()
        self.assertEqual(found, fake_binary)

    def test_find_command_bundled_node(self) -> None:
        node_name = "node.exe" if sys.platform == "win32" else "node"
        fake_node = self.bin_dir / node_name
        payload = b"MZ" + b"\x00" * 20000 if sys.platform == "win32" else b"#!/bin/sh\nexit 0\n"
        fake_node.write_bytes(payload)
        fake_node.chmod(0o755)

        script = self.bin_dir / "9router" / "node_modules" / "9router" / "cli.js"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// cli entry", encoding="utf-8")

        cmd_info = self.manager.find_command()
        self.assertIsNotNone(cmd_info)
        args, use_shell = cmd_info
        self.assertEqual(args[0], str(fake_node))
        self.assertEqual(args[1], str(script))
        self.assertFalse(use_shell)

    def test_find_binary_not_found(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(self.manager.find_binary())

    @mock.patch("requests.get")
    def test_is_healthy_success(self, mock_get) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        self.assertTrue(self.manager.is_healthy())
        mock_get.assert_called_once_with(DEFAULT_ROUTER_URL, timeout=1.0)

    @mock.patch("requests.get")
    def test_is_healthy_failure(self, mock_get) -> None:
        mock_get.side_effect = Exception("Connection refused")
        self.assertFalse(self.manager.is_healthy())

    @mock.patch.object(NineRouterManager, "is_healthy", return_value=True)
    def test_start_already_running(self, mock_health) -> None:
        started = self.manager.start()
        self.assertTrue(started)
        self.assertFalse(self.manager.spawned_by_us)
        self.assertIsNone(self.manager.process)

    @mock.patch.object(NineRouterManager, "is_healthy")
    @mock.patch("subprocess.Popen")
    def test_start_launches_binary(self, mock_popen, mock_health) -> None:
        binary_name = "9router.exe" if sys.platform == "win32" else "9router"
        fake_binary = self.bin_dir / binary_name
        fake_binary.write_text("#!/bin/sh\n")
        fake_binary.chmod(0o755)

        # First call False (not running), then True (ready)
        mock_health.side_effect = [False, True]
        mock_proc = mock.Mock()
        mock_popen.return_value = mock_proc

        started = self.manager.start(wait_seconds=1.0)
        self.assertTrue(started)
        self.assertTrue(self.manager.spawned_by_us)
        self.assertEqual(self.manager.process, mock_proc)
        mock_popen.assert_called_once()

    @mock.patch("requests.get")
    def test_get_available_models_success(self, mock_get) -> None:
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"id": "ag/gemini-3.8-flash-low"},
                {"id": "claude-3-5-sonnet-20241022"},
            ]
        }
        mock_get.return_value = mock_response

        models = self.manager.get_available_models()
        self.assertEqual(models, ["ag/gemini-3.8-flash-low", "claude-3-5-sonnet-20241022"])

    @mock.patch("requests.get")
    def test_get_available_models_fallback(self, mock_get) -> None:
        mock_get.side_effect = Exception("Connection error")
        models = self.manager.get_available_models()
        self.assertEqual(models, [DEFAULT_MODEL])

    @mock.patch("webbrowser.open")
    def test_open_dashboard(self, mock_open) -> None:
        self.manager.open_dashboard()
        mock_open.assert_called_once_with(DEFAULT_ROUTER_URL)

    def test_shutdown_terminates_process(self) -> None:
        mock_proc = mock.Mock()
        self.manager.process = mock_proc
        self.manager.spawned_by_us = True

        self.manager.shutdown()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(self.manager.process)
        self.assertFalse(self.manager.spawned_by_us)

    def test_get_api_key_creates_and_finds_key(self) -> None:
        import sqlite3

        db_dir = self.app_root / "db"
        db_dir.mkdir(parents=True)
        db_file = db_dir / "data.sqlite"
        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "CREATE TABLE apiKeys (id TEXT PRIMARY KEY, key TEXT UNIQUE, name TEXT, machineId TEXT, isActive INTEGER, createdAt TEXT, accessRestricted INTEGER, accessAllow TEXT)"
            )
            conn.commit()

        with mock.patch.object(NineRouterManager, "get_data_dir", return_value=self.app_root):
            # First call creates a new key
            created_key = self.manager.get_api_key()
            self.assertTrue(created_key.startswith("sk-aegistrans-"))

            # Second call retrieves the existing active key
            retrieved_key = self.manager.get_api_key()
            self.assertEqual(created_key, retrieved_key)


if __name__ == "__main__":
    unittest.main()
