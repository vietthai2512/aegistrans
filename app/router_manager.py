"""9Router background service manager for AegisTrans.

Manages the lifecycle of the bundled 9Router AI proxy process, provides
OAuth/dashboard launcher, health checks, and active model discovery.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_ROUTER_URL = "http://localhost:20128"
DEFAULT_MODEL = "ag/gemini-3.8-flash-low"


def is_pe_binary(path: Path) -> bool:
    """Check if file has Windows PE magic header (b'MZ') and is a valid executable."""
    if sys.platform != "win32":
        return True
    try:
        if not path.is_file() or path.stat().st_size < 10 * 1024:
            return False
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except Exception:
        return False


class NineRouterManager:
    """Manages the bundled 9Router background process and its API connection."""

    def __init__(
        self,
        app_root: Path | None = None,
        base_url: str = DEFAULT_ROUTER_URL,
    ) -> None:
        if app_root is None:
            self.app_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        else:
            self.app_root = app_root

        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/v1"
        self.process: subprocess.Popen[Any] | None = None
        self.spawned_by_us: bool = False

    def find_command(self) -> tuple[list[str], bool] | None:
        """Resolve command args and shell mode to launch 9Router safely.

        Returns (args_list, use_shell) or None if no valid runner is available.
        """
        candidate_dirs = [
            self.app_root / "app" / "bin",
            self.app_root / "bin",
            Path(sys.executable).parent / "app" / "bin",
            Path(sys.executable).parent / "bin",
        ]

        # 1. Bundled portable node.exe (or node) + 9router/cli.js
        node_name = "node.exe" if sys.platform == "win32" else "node"
        for d in candidate_dirs:
            node_candidate = d / node_name
            is_valid_node = (
                is_pe_binary(node_candidate) if sys.platform == "win32" else node_candidate.is_file()
            )
            if is_valid_node:
                script_candidates = [
                    d / "9router" / "node_modules" / "9router" / "cli.js",
                    d / "9router" / "cli.js",
                    d / "node_modules" / "9router" / "cli.js",
                ]
                for script in script_candidates:
                    if script.is_file():
                        return ([str(node_candidate), str(script), "-n", "--skip-update"], False)

        # 2. Standalone 9router binary (must be verified PE on Windows)
        binary_name = "9router.exe" if sys.platform == "win32" else "9router"
        for d in candidate_dirs:
            candidate = d / binary_name
            if candidate.is_file():
                if sys.platform == "win32":
                    if is_pe_binary(candidate):
                        return ([str(candidate), "-n", "--skip-update"], False)
                elif os.access(candidate, os.X_OK | os.R_OK):
                    return ([str(candidate), "-n", "--skip-update"], False)

        # 3. System PATH
        if sys.platform == "win32":
            for cmd in ("9router.cmd", "n9router.cmd", "9router.bat", "n9router.bat"):
                found = shutil.which(cmd)
                if found:
                    return ([found, "-n", "--skip-update"], True)
            for cmd in ("9router.exe", "n9router.exe"):
                found = shutil.which(cmd)
                if found and is_pe_binary(Path(found)):
                    return ([found, "-n", "--skip-update"], False)
        else:
            for cmd in ("9router", "n9router"):
                found = shutil.which(cmd)
                if found:
                    return ([found, "-n", "--skip-update"], False)

        return None

    def find_binary(self) -> Path | None:
        """Locate the 9Router executable binary (retained for backward compatibility)."""
        cmd_info = self.find_command()
        if cmd_info and cmd_info[0]:
            return Path(cmd_info[0][0])
        return None

    def is_healthy(self, timeout: float = 1.0) -> bool:
        """Check if 9Router is currently reachable at the configured port."""
        try:
            # 9Router serves a dashboard or redirects at root
            response = requests.get(self.base_url, timeout=timeout)
            return response.status_code in (200, 301, 302, 401, 403)
        except Exception:
            return False

    def start(self, wait_seconds: float = 3.0) -> bool:
        """Start the bundled 9Router service in the background if not already running."""
        if self.is_healthy():
            logger.info("9Router is already running at %s", self.base_url)
            self.spawned_by_us = False
            return True

        cmd_info = self.find_command()
        if not cmd_info:
            logger.info("No valid 9Router executable found; service will remain offline until started externally.")
            return False

        cmd_args, use_shell = cmd_info
        logger.info("Starting bundled 9Router via: %s", cmd_args)
        try:
            creationflags = 0x08000000 if sys.platform == "win32" else 0

            self.process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                shell=use_shell,
            )
            self.spawned_by_us = True

            # Poll for readiness
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if self.is_healthy(timeout=0.5):
                    logger.info("9Router started successfully.")
                    return True
                time.sleep(0.3)

            logger.warning("9Router process started but health check timed out.")
            return False
        except Exception as error:
            logger.error("Failed to launch 9Router: %s", error)
            return False

    def open_dashboard(self) -> None:
        """Open the 9Router web dashboard in the user's default browser."""
        webbrowser.open(self.base_url)

    def get_available_models(self, timeout: float = 2.0) -> list[str]:
        """Fetch list of available models from 9Router OpenAI-compatible endpoint."""
        models_url = f"{self.api_url}/models"
        try:
            headers = {"Authorization": "Bearer 9router"}
            response = requests.get(models_url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", [])
                models = [item["id"] for item in data if isinstance(item, dict) and "id" in item]
                if models:
                    return models
        except Exception as error:
            logger.debug("Could not fetch models from 9Router: %s", error)

        return [DEFAULT_MODEL]

    def is_authenticated(self, timeout: float = 2.0) -> bool:
        """Determine if 9Router has at least one active configured provider/model."""
        if not self.is_healthy(timeout=timeout):
            return False

        # Attempt to inspect models endpoint
        models_url = f"{self.api_url}/models"
        try:
            headers = {"Authorization": "Bearer 9router"}
            response = requests.get(models_url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", [])
                return len(data) > 0
        except Exception:
            pass

        return False

    def shutdown(self) -> None:
        """Terminate the background 9Router process if we were the one that started it."""
        if not self.spawned_by_us or not self.process:
            return

        logger.info("Stopping background 9Router process...")
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except Exception as error:
            logger.warning("Error stopping 9Router process: %s", error)
        finally:
            self.process = None
            self.spawned_by_us = False
