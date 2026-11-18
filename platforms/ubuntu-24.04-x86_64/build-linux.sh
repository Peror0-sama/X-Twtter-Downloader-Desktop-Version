#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv-linux
.venv-linux/bin/python -m pip install -r requirements-lock.txt
test -x third_party/electron/electron
test -x third_party/ffmpeg/ffmpeg
.venv-linux/bin/python -m PyInstaller --noconfirm XDownloader-Linux.spec
