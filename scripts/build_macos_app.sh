#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$ROOT/build/macos"
APP_DIR="$ROOT/dist/CodeCCTV.app"

mkdir -p "$BUILD_DIR" "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
swiftc \
  -target arm64-apple-macosx13.0 \
  -framework AppKit \
  -framework SwiftUI \
  -O \
  "$ROOT/macos/CodeCCTVApp.swift" \
  "$ROOT/macos/CodeCCTVViews.swift" \
  "$ROOT/macos/ManagementView.swift" \
  "$ROOT/macos/StatusStore.swift" \
  -o "$BUILD_DIR/CodeCCTV"

cp "$BUILD_DIR/CodeCCTV" "$APP_DIR/Contents/MacOS/CodeCCTV"
cp "$ROOT/macos/Info.plist" "$APP_DIR/Contents/Info.plist"
chmod +x "$APP_DIR/Contents/MacOS/CodeCCTV"
codesign --force --deep --sign - "$APP_DIR" >/dev/null
echo "$APP_DIR"
