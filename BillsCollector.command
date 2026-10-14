#!/bin/bash
# Double-click in Finder to start BillsCollector. It opens in your browser.
cd "$(dirname "$0")" || exit 1

for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it with: brew install python  (or from python.org)"
  read -r -p "Press Enter to close."; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (takes a minute)..."
  "$PY" -m venv .venv || exit 1
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt \
    && touch .venv/.installed || { read -r -p "Install failed. Press Enter to close."; exit 1; }
fi

exec .venv/bin/python -m billscollector ui
