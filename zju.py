#!/bin/sh
""":"
command -v uv >/dev/null 2>&1 || { echo 'ZJU LearnPilot requires uv. Install: https://docs.astral.sh/uv/getting-started/installation/' >&2; exit 127; }
exec uv run --quiet --script "$0" "$@"
":"""
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# SPDX-License-Identifier: MIT
# Repository launcher. The only CLI implementation and its dependencies live in the Skill.
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    uv = shutil.which("uv")
    if uv is None:
        print("ZJU LearnPilot requires uv. Install: https://docs.astral.sh/uv/getting-started/installation/", file=sys.stderr)
        return 127
    script = Path(__file__).resolve().parent / "skills" / "zju-learnpilot" / "scripts" / "zju.py"
    if not script.is_file():
        print("ZJU LearnPilot: missing skills/zju-learnpilot/scripts/zju.py; keep the repository layout or run the installed Skill's scripts/zju.py.", file=sys.stderr)
        return 1
    try:
        return subprocess.call([uv, "run", "--quiet", "--script", str(script), *sys.argv[1:]])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
