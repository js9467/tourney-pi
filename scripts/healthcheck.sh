#!/usr/bin/env bash
set -e
# If your Flask app exposes /healthz, this is perfect:
curl -fsS http://127.0.0.1:5000/healthz >/dev/null
