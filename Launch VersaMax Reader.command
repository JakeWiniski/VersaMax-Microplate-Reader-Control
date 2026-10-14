#!/bin/zsh
# Double-click to start the VersaMax control program.
cd "$(dirname "$0")"
PY=/opt/anaconda3/bin/python3
[ -x "$PY" ] || PY=python3
exec "$PY" versamax_reader.py
