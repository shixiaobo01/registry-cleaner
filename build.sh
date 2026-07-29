#!/usr/bin/env bash
# Build on the same OS and CPU architecture used by the customer.
# Requires Python 3.9+ and PyInstaller on the build machine only.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_DIR="$PROJECT_DIR/release"
WORK_DIR="$PROJECT_DIR/.pyinstaller-build"

cd "$PROJECT_DIR"

if ! "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller is required only on the build machine." >&2
  echo "Install it with: $PYTHON_BIN -m pip install pyinstaller" >&2
  exit 1
fi

rm -rf "$OUTPUT_DIR" "$WORK_DIR"
"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name registry-cleaner \
  --distpath "$OUTPUT_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$WORK_DIR" \
  cleaner.py

PACKAGE_DIR="$OUTPUT_DIR/registry-cleaner"
cp config.example.json "$PACKAGE_DIR/config.json.example"
cp README.md "$PACKAGE_DIR/README.md"

if command -v shasum >/dev/null 2>&1; then
  (cd "$OUTPUT_DIR" && shasum -a 256 registry-cleaner/registry-cleaner > SHA256SUMS.txt)
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUTPUT_DIR" && sha256sum registry-cleaner/registry-cleaner > SHA256SUMS.txt)
fi

echo "Package created: $PACKAGE_DIR"
echo "Copy config.json.example to config.json and edit it before delivery."
