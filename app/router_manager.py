"""9Router background service manager for AegisTrans.

Manages the lifecycle of the bundled 9Router AI proxy process, provides
OAuth/dashboard launcher, health checks, and active model discovery.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_ROUTER_URL = "http://localhost:20128"
DEFAULT_MODEL = "ag/gemini-3.8-flash-low"


def _assign_process_to_job(pid: int) -> Any:
    """Bind child process to a Windows Job Object with KILL_ON_JOB_CLOSE.

    Guarantees the Windows kernel terminates node.exe and all its child
    processes immediately whenever AegisTrans exits, even on sudden crash.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoCounters", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        success = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            kernel32.CloseHandle(job)
            return None

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if h_proc:
            kernel32.AssignProcessToJobObject(job, h_proc)
            kernel32.CloseHandle(h_proc)

        return job
    except Exception as e:
        logger.debug("Could not assign process to Windows job object: %s", e)
        return None


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
        log_dir: Path | None = None,
    ) -> None:
        if app_root is None:
            self.app_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        else:
            self.app_root = app_root

        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/v1"
        self.log_dir = log_dir or (self.app_root / "logs")
        self.log_file = None
        self.process: subprocess.Popen[Any] | None = None
        self.spawned_by_us: bool = False
        self.job_handle: Any = None
        atexit.register(self.shutdown)

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

            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                log_path = self.log_dir / "9router.log"
                self.log_file = open(log_path, "a", encoding="utf-8")
                self.log_file.write(
                    f"\n{'=' * 60}\n"
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting 9Router: {' '.join(cmd_args)}\n"
                    f"{'=' * 60}\n"
                )
                self.log_file.flush()
                stdout_target = self.log_file
                stderr_target = subprocess.STDOUT
            except OSError as log_err:
                logger.warning("Could not open 9router.log: %s", log_err)
                stdout_target = subprocess.DEVNULL
                stderr_target = subprocess.DEVNULL

            self.process = subprocess.Popen(
                cmd_args,
                stdout=stdout_target,
                stderr=stderr_target,
                creationflags=creationflags,
                shell=use_shell,
            )
            self.spawned_by_us = True
            if sys.platform == "win32" and getattr(self.process, "pid", None):
                self.job_handle = _assign_process_to_job(self.process.pid)

            # Poll for readiness
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if self.is_healthy(timeout=0.5):
                    logger.info("9Router started successfully.")
                    return True
                exit_code = self.process.poll()
                if isinstance(exit_code, int):
                    logger.warning(
                        "9Router process exited prematurely with code %s. Check %s for details.",
                        exit_code,
                        self.log_dir / "9router.log",
                    )
                    if self.log_file:
                        try:
                            self.log_file.flush()
                        except OSError:
                            pass
                    break
                time.sleep(0.3)

            logger.warning("9Router process started but health check timed out. Check: %s", self.log_dir / "9router.log")
            return False
        except Exception as error:
            logger.error("Failed to launch 9Router: %s", error)
            return False

    def open_dashboard(self) -> None:
        """Open the 9Router web dashboard in the user's default browser."""
        webbrowser.open(self.base_url)

    def get_data_dir(self) -> Path:
        """Locate 9Router user data directory where SQLite database lives."""
        if os.environ.get("DATA_DIR"):
            return Path(os.environ["DATA_DIR"])
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(appdata) / "9router"
        return Path.home() / ".9router"

    def get_api_key(self) -> str:
        """Retrieve active API key from 9Router SQLite database, or create one if none exists."""
        db_path = self.get_data_dir() / "db" / "data.sqlite"
        if not db_path.is_file():
            return "sk-aegistrans"

        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                cur = conn.cursor()
                # 1. Look for existing active API key
                cur.execute("SELECT key FROM apiKeys WHERE isActive = 1 ORDER BY createdAt ASC")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]

                # 2. No active key: insert an AegisTrans key so requireApiKey=true succeeds
                key_id = str(uuid.uuid4())
                new_key = f"sk-aegistrans-{uuid.uuid4().hex[:12]}"
                now = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    "INSERT INTO apiKeys (id, key, name, machineId, isActive, createdAt, accessRestricted, accessAllow) "
                    "VALUES (?, ?, ?, ?, 1, ?, 0, '')",
                    (key_id, new_key, "AegisTrans", "local", now),
                )
                conn.commit()
                logger.info("Created AegisTrans API key in 9Router DB: %s", new_key)
                return new_key
        except Exception as error:
            logger.debug("Could not resolve/create API key from 9Router DB: %s", error)
            return "sk-aegistrans"

    def get_available_models(self, timeout: float = 2.0) -> list[str]:
        """Fetch list of available models from 9Router OpenAI-compatible endpoint."""
        models_url = f"{self.api_url}/models"
        try:
            api_key = self.get_api_key()
            headers = {"Authorization": f"Bearer {api_key}"}
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
            api_key = self.get_api_key()
            headers = {"Authorization": f"Bearer {api_key}"}
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
        if self.spawned_by_us and self.process:
            logger.info("Stopping background 9Router process...")

            # 1. Graceful HTTP shutdown signal
            try:
                requests.post(f"{self.base_url}/api/version/shutdown", timeout=0.8)
            except Exception:
                pass

            # 2. Forcefully kill the entire process tree on Windows (PID + children)
            if sys.platform == "win32" and getattr(self.process, "pid", None):
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        capture_output=True,
                        timeout=3.0,
                    )
                except Exception as taskkill_err:
                    logger.debug("taskkill error: %s", taskkill_err)

            # 3. Always invoke terminate() on process handle
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, Exception):
                    self.process.kill()
            except Exception as error:
                logger.warning("Error stopping 9Router process: %s", error)
            finally:
                self.process = None
                self.spawned_by_us = False

        if self.job_handle:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self.job_handle)
            except Exception:
                pass
            self.job_handle = None

        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
