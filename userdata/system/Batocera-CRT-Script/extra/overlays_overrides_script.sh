#!/bin/sh
# Compatibility entry: the UI and filesystem operations are Python.
export DISPLAY="${DISPLAY:-:0.0}"
exec python3 -B /userdata/system/Batocera-CRT-Script/extra/overlays_overrides.py "$@"
