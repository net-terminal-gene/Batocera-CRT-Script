#!/bin/sh
# CRT Tools entry: no terminal or dialog is kept behind the menu.
export DISPLAY="${DISPLAY:-:0.0}"
exec python3 -B /userdata/system/Batocera-CRT-Script/extra/overlays_overrides.py "$@"
