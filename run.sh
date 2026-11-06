#!/usr/bin/env bash
# Formi Outcome-First Controller
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
exec python -m controller.loop --url "$SIM_URL"
