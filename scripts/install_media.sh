#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
vendor_path="$project_root/data/vendor/douyin-downloader"
vendor_commit=1e08f03b8909b364fb6237d8f9f441e3fbf3d427
if [ ! -d "$vendor_path/.git" ]; then
  if [ -e "$vendor_path" ]; then
    echo 'Existing non-Git vendor directory preserved; inspect it before installing.' >&2
    exit 1
  fi
  mkdir -p "$project_root/data/vendor"
  git clone --no-checkout https://github.com/jiji262/douyin-downloader.git "$vendor_path"
  git -C "$vendor_path" checkout --detach "$vendor_commit"
fi
if [ "$(git -C "$vendor_path" rev-parse HEAD)" != "$vendor_commit" ]; then
  echo 'Existing vendor revision differs; preserved without reset. Review before upgrading.' >&2
  exit 1
fi
if [ ! -x data/media-venv/bin/python ]; then python3 -m venv data/media-venv; fi
data/media-venv/bin/python -m pip install -r requirements-media.lock
data/media-venv/bin/python -m pip check
data/media-venv/bin/python -m yt_dlp --version
if [ ! -x "${CHROMIUM_PATH:-/usr/bin/chromium}" ]; then
  echo 'TikTok search requires Chromium. Set CHROMIUM_PATH to an installed executable.' >&2
fi
