#!/usr/bin/env bash
# Ubuntu EC2 (Jammy/Noble etc.): WeasyPrint system libraries (Pango/Cairo/GDK pixbuf).
# Debian package pixbuf names differ slightly from Ubuntu; Dockerfile lists bookworm-compatible names.
#
# Usage (from repo):
#   cd backend && sudo bash scripts/install-weasyprint-deps-ubuntu.sh
#
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends \
  libcairo2 \
  libpango-1.0-0 \
  libpangoft2-1.0-0 \
  libpangocairo-1.0-0 \
  libharfbuzz0b \
  libgdk-pixbuf2.0-0 \
  libffi-dev \
  shared-mime-info \
  fontconfig \
  fonts-dejavu-core \
  fonts-noto-core
echo "WeasyPrint OS deps OK — restart porpin/gunicorn (and any RQ workers)."
