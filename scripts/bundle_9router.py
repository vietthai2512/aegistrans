#!/usr/bin/env python3
"""Stage and bundle 9Router for portable Windows distribution.

Ensures that 9Router runs portably using the official 64-bit node.exe runtime
and the bundled 9router package in app/bin, preventing invalid 16-bit application
errors.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = SKILL_ROOT / "app" / "bin"
NODE_VERSION = "v22.23.3"
NODE_WIN64_URL = f"https://nodejs.org/dist/{NODE_VERSION}/win-x64/node.exe"


def is_valid_pe(path: Path) -> bool:
    """Verify that a Windows executable is a genuine PE binary (starts with 'MZ')."""
    try:
        if not path.is_file() or path.stat().st_size < 100 * 1024:
            return False
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except Exception:
        return False


def clean_invalid_binaries() -> None:
    """Remove any invalid stub or non-PE 9router.exe from previous builds."""
    target = BIN_DIR / "9router.exe"
    if target.is_file() and not is_valid_pe(target):
        print(f"Removing invalid non-PE executable stub: {target}")
        try:
            target.unlink()
        except OSError:
            pass


def ensure_node_exe() -> bool:
    """Download official standalone 64-bit node.exe (Node 22 LTS with built-in node:sqlite)."""
    target = BIN_DIR / "node.exe"
    stamp_file = BIN_DIR / "node_version.txt"
    if target.is_file() and is_valid_pe(target):
        if stamp_file.is_file() and stamp_file.read_text().strip() == NODE_VERSION:
            print(f"Valid 64-bit node.exe ({NODE_VERSION}) already present: {target} ({target.stat().st_size / 1e6:.1f} MB)")
            return True
        print(f"Existing node.exe is not {NODE_VERSION}. Re-downloading...")
        try:
            target.unlink()
        except OSError:
            pass

    print(f"Downloading official 64-bit node.exe ({NODE_VERSION}) from {NODE_WIN64_URL}...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(NODE_WIN64_URL, target)
        if is_valid_pe(target):
            stamp_file.write_text(NODE_VERSION)
            print(f"Downloaded node.exe successfully ({target.stat().st_size / 1e6:.1f} MB)")
            return True
        print("Error: Downloaded node.exe is not a valid PE binary.", file=sys.stderr)
        target.unlink(missing_ok=True)
        return False
    except Exception as error:
        print(f"Failed to download node.exe: {error}", file=sys.stderr)
        return False


def ensure_9router_package() -> bool:
    """Install the 9router and sql.js packages into app/bin/9router."""
    cli_js = BIN_DIR / "9router" / "node_modules" / "9router" / "cli.js"
    sql_js = BIN_DIR / "9router" / "node_modules" / "sql.js"
    if cli_js.is_file() and sql_js.is_dir():
        print(f"9Router and sql.js packages already present: {cli_js}")
        return True

    npm = shutil.which("npm")
    if not npm:
        print("Notice: 'npm' command not found. Cannot auto-install 9router package.", file=sys.stderr)
        return False

    print("Installing 9router and sql.js packages into app/bin/9router...")
    target_dir = BIN_DIR / "9router"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [npm, "install", "--prefix", str(target_dir), "9router", "sql.js@1.14.1", "--no-audit", "--no-fund", "--omit=dev"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and cli_js.is_file():
            print("Successfully installed 9router and sql.js packages into app/bin/9router.")
            return True
        print(f"npm install failed: {res.stderr}", file=sys.stderr)
        return False
    except Exception as error:
        print(f"Failed to install 9router package: {error}", file=sys.stderr)
        return False


def stage_custom_binary(source: Path) -> bool:
    if not source.is_file():
        print(f"Error: Specified binary does not exist: {source}", file=sys.stderr)
        return False
    if sys.platform == "win32" and not is_valid_pe(source):
        print(f"Error: Specified binary '{source}' is not a valid 64-bit Windows PE executable.", file=sys.stderr)
        return False

    target = BIN_DIR / ("9router.exe" if sys.platform == "win32" else "9router")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755)
    print(f"Staged custom binary: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 9Router for portable distribution")
    parser.add_argument("--binary", type=Path, help="Path to pre-compiled standalone executable")
    args = parser.parse_args()

    clean_invalid_binaries()

    if args.binary:
        return 0 if stage_custom_binary(args.binary) else 1

    # On Windows or cross-building for Windows
    ensure_node_exe()
    ensure_9router_package()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
