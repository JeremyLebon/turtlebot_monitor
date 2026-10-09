#!/bin/bash
# TurtleBot Monitor starten in WSL of Linux (zie README.md voor WSL-netwerk).
cd "$(dirname "$0")"
exec python3 monitor.py "$@"
