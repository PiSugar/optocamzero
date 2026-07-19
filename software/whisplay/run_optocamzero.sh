#!/bin/sh
set -eu

OPTOCAM_HOME=${OPTOCAM_HOME:-"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"}
export OPTOCAM_HOME
export OPTOCAM_HARDWARE=whisplay
export OPTOCAM_WHISPLAY_BACKEND=${OPTOCAM_WHISPLAY_BACKEND:-auto}
export PYTHONUNBUFFERED=1

exec /usr/bin/python3 "$OPTOCAM_HOME/optocamzero.py"
