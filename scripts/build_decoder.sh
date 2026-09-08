#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Building BRAW Native Decoder (braw_decode)..."

if [ -n "${BRAW_SDK_DIR}" ] && [ -d "${BRAW_SDK_DIR}" ]; then
    SDK_DIR="${BRAW_SDK_DIR}"
elif [ -d "${ROOT_DIR}/Documents/Blackmagic RAW SDK/Mac" ]; then
    SDK_DIR="${ROOT_DIR}/Documents/Blackmagic RAW SDK/Mac"
elif [ -d "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac" ]; then
    SDK_DIR="/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac"
elif [ -d "${ROOT_DIR}/../davinci-braw/Documents/Blackmagic RAW SDK/Mac" ]; then
    SDK_DIR="${ROOT_DIR}/../davinci-braw/Documents/Blackmagic RAW SDK/Mac"
else
    echo "Error: Blackmagic RAW SDK (Mac) not found in system or project locations." >&2
    exit 1
fi

INCLUDE_DIR="${SDK_DIR}/Include"
LIB_DIR="${SDK_DIR}/Libraries"
SRC_FILE="${ROOT_DIR}/src/native/braw_decode.mm"
DISPATCH_FILE="${INCLUDE_DIR}/BlackmagicRawAPIDispatch.cpp"
OUT_BIN="${ROOT_DIR}/bin/braw_decode"

mkdir -p "${ROOT_DIR}/bin"

clang++ -std=c++17 -O3 -fobjc-arc \
    -I"${INCLUDE_DIR}" \
    -F"${LIB_DIR}" \
    -framework CoreFoundation \
    -framework CoreServices \
    -framework Foundation \
    -framework Metal \
    -framework AVFoundation \
    -framework VideoToolbox \
    -framework CoreMedia \
    -framework CoreVideo \
    -framework AudioToolbox \
    -framework Accelerate \
    "${DISPATCH_FILE}" \
    "${SRC_FILE}" \
    -o "${OUT_BIN}"

chmod +x "${OUT_BIN}"
echo "==> Successfully built ${OUT_BIN}"
