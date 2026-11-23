#!/bin/bash
# Test runner for the CM:EX Linux Player. The suite needs no mpv, display
# or browser: playback is exercised against fakes and an in-memory CMS.

set -e
cd "$(dirname "$0")"

if ! python3 -c "import pytest" &> /dev/null; then
    echo "pytest not found. Installing test dependencies..."
    pip3 install -r requirements-dev.txt
fi

python3 -m pytest -q "$@"
