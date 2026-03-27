#!/usr/bin/env bash
# Render build script — installs Python deps + ffmpeg
set -e

pip install -r requirements.txt

# Install ffmpeg on Render (Debian/Ubuntu based)
apt-get update -qq && apt-get install -y -qq ffmpeg || echo "ffmpeg install skipped (may already exist)"
