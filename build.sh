#!/usr/bin/env bash
set -euo pipefail

ADDON_ID="plugin.service.ytlounge-cast"
VERSION=$(python3 -c "import xml.etree.ElementTree as ET; print(ET.parse('addon.xml').getroot().attrib['version'])")
DIST_DIR="dist"
BUILD_DIR="build/${ADDON_ID}"
ZIP_NAME="${ADDON_ID}-${VERSION}.zip"

echo "==> Building ${ADDON_ID} version ${VERSION}..."

rm -rf build "${DIST_DIR}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

# Copy addon files
cp addon.xml service.py update_ytdlp.py icon.png fanart.jpg "${BUILD_DIR}/"
cp -r resources "${BUILD_DIR}/"

# Remove any temporary or pycache files
find "${BUILD_DIR}" -type d -name "__pycache__" -exec rm -rf {} +
find "${BUILD_DIR}" -type f -name "*.pyc" -delete

# Architecture-specific yt-dlp download support
# Usage: ./build.sh [x86_64|aarch64|armv7l]
ARCH="${1:-}"
if [ -n "${ARCH}" ]; then
    mkdir -p "${BUILD_DIR}/resources/bin"
    BIN_NAME="yt-dlp"
    YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
    if [ "${ARCH}" = "aarch64" ] || [ "${ARCH}" = "arm64" ]; then
        YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64"
    elif [ "${ARCH}" = "armv7l" ]; then
        YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_armv7l"
    fi
    echo "  Downloading yt-dlp for ${ARCH} from ${YTDLP_URL}..."
    curl -sL "${YTDLP_URL}" -o "${BUILD_DIR}/resources/bin/${BIN_NAME}"
    chmod +x "${BUILD_DIR}/resources/bin/${BIN_NAME}"
fi

# Package zip
cd build
zip -rq "../${DIST_DIR}/${ZIP_NAME}" "${ADDON_ID}"
cd ..

echo "==> Addon package created at ${DIST_DIR}/${ZIP_NAME}"
