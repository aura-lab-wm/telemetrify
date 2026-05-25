#!/usr/bin/env bash
# Compile the styled toast HUD from source into both the .app bundle (primary,
# launched via `open` so it survives the parent's exit) and a loose fallback
# binary. Run after editing distill-toast.swift.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p DistillToast.app/Contents/MacOS
swiftc -O distill-toast.swift -o DistillToast.app/Contents/MacOS/DistillToast
swiftc -O distill-toast.swift -o distill-toast

echo "built: DistillToast.app/Contents/MacOS/DistillToast + distill-toast"
