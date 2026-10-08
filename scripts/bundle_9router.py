#!/usr/bin/env python3
"""Stage and package the 9Router binary into app/bin for portable Windows distribution.

Supports:
1. Pre-existing binary in app/bin/9router.exe or app/bin/9router
2. Custom path from ROUTER_BINARY_PATH environment variable or --binary argument
3. Packaging via npx @yao-pkg/pkg or npm if Node.js is available
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = SKILL_ROOT / "app" / "bin"


def get_binary_target() -> Path:
    name = "9router.exe" if sys.platform == "win32" else "9router"
    return BIN_DIR / name


def stage_from_path(source_path: Path) -> bool:
    if not source_path.is_file():
        print(f"Error: Specified source binary does not exist: {source_path}", file=sys.stderr)
        return False

    target = get_binary_target()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)
    target.chmod(0o755)
    print(f"Successfully staged {source_path} -> {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return True


def stage_from_system() -> bool:
    target = get_binary_target()
    if target.is_file():
        print(f"9Router binary already present: {target} ({target.stat().st_size / 1e6:.1f} MB)")
        return True

    # Check environment variable
    env_path = os.environ.get("ROUTER_BINARY_PATH")
    if env_path:
        return stage_from_path(Path(env_path))

    # Check system PATH for 9router or n9router
    for cmd in ("9router", "n9router", "9router.exe", "n9router.exe"):
        found = shutil.which(cmd)
        if found:
            print(f"Found system {cmd} at {found}, copying to {target}...")
            return stage_from_path(Path(found))

    # Attempt to compile with npx @yao-pkg/pkg if npm/npx is available
    npx = shutil.which("npx")
    if npx:
        print("Attempting to package 9Router via npx pkg...")
        target_platform = "node18-win-x64" if sys.platform == "win32" else "node18-linux-x64"
        try:
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            cmd = [
                npx,
                "-y",
                "@yao-pkg/pkg",
                "9router",
                "--targets",
                target_platform,
                "--output",
                str(target),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and target.is_file():
                print(f"Compiled standalone binary to {target} ({target.stat().st_size / 1e6:.1f} MB)")
                return True
        except Exception as error:
            print(f"Notice: Automated npx packaging was skipped: {error}")

    print(
        f"Notice: No 9Router binary staged at {target}.\n"
        "To bundle 9Router into the portable package, place your pre-compiled\n"
        f"binary at '{target}' or specify ROUTER_BINARY_PATH.",
        file=sys.stderr,
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 9Router binary into app/bin")
    parser.add_argument("--binary", type=Path, help="Path to pre-compiled 9Router executable")
    args = parser.parse_args()

    if args.binary:
        return 0 if stage_from_path(args.binary) else 1

    stage_from_system()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
