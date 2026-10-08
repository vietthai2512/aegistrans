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

    def find_binary(self) -> Path | None:
        """Locate the 9Router executable binary."""
        binary_name = "9router.exe" if sys.platform == "win32" else "9router"

        candidate_dirs = [
            self.app_root / "app" / "bin",
            self.app_root / "bin",
            Path(sys.executable).parent / "app" / "bin",
            Path(sys.executable).parent / "bin",
        ]

        for directory in candidate_dirs:
            candidate = directory / binary_name
            if candidate.is_file() and os.access(candidate, os.X_OK | os.R_OK):
                return candidate
            # On Windows, sometimes file permissions aren't executable flag
            if sys.platform == "win32" and candidate.is_file():
                return candidate

        # Check system PATH
        system_binary = shutil.which("9router")
        if system_binary:
            return Path(system_binary)

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

        binary = self.find_binary()
        if not binary:
            logger.warning("No 9Router binary found in bundled directories or PATH.")
            return False

        logger.info("Starting bundled 9Router from %s", binary)
        try:
            creationflags = 0
            if sys.platform == "win32":
                # CREATE_NO_WINDOW = 0x08000000
                creationflags = 0x08000000

            self.process = subprocess.Popen(
                [str(binary)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
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
            logger.error("Failed to launch 9Router binary: %s", error)
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
