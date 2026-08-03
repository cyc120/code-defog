#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP=$($ROOT/scripts/build_macos_app.sh)
/usr/bin/open -n "$APP"
echo "Opened $APP"
