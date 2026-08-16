#!/bin/zsh
# macOS Finder launcher: start the local daemon, then let it open its
# already-connected console in the default browser.
set -e

cd "$(cd "$(dirname "$0")/.." && pwd)"
exec /usr/bin/env python3 -m daemon.serve "$@"
