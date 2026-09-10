#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt -q
python3 steam_free_finder.py
