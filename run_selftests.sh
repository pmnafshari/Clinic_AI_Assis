#!/bin/sh
set -e
for f in *_selftest.py ask.py agent.py; do .venv/bin/python "$f" --selftest; done
