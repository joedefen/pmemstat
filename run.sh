#!/bin/bash

SRC="$(dirname "$0")/src"

set -x
##### choose one:

PYTHONPATH="$SRC" python3 -m pmemstat.main "$@"
# PYTHONPATH="$SRC" python3 src/pmemstat/main.py "$@"
