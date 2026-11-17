#!/usr/bin/env bash
# ============================================================
#   DISPATCH LIVE  -  Mac / Linux launcher
#   Runs the deck server (which opens the deck in your browser) and the
#   Traffic Brain next to it. Requires Python 3.9+ (no other dependencies).
# ============================================================
cd "$(dirname "$0")" || exit 1

echo ""
echo "  =================================================="
echo "    DISPATCH LIVE  -  TV + News + Red Alerts"
echo "  =================================================="
echo ""

# --- Close anything already listening on port 3100 (clean restart) ---
if command -v lsof >/dev/null 2>&1; then
  OLDPID=$(lsof -ti tcp:3100 -sTCP:LISTEN 2>/dev/null)
  if [ -n "$OLDPID" ]; then
    echo "  Closing old server on port 3100 (PID $OLDPID)..."
    kill -9 $OLDPID 2>/dev/null
    sleep 1
  fi
fi

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  [!] Python 3.9 or newer was not found."
  echo "      Install it from https://www.python.org/downloads/"
  echo ""
  read -r -p "  Press Enter to close..."
  exit 1
fi

# --- Traffic Brain (port 3200), stopped together with the deck ---
BRAIN=""
if [ -f brain.py ] && ! (command -v lsof >/dev/null 2>&1 && lsof -ti tcp:3200 -sTCP:LISTEN >/dev/null 2>&1); then
  "$PY" brain.py >/dev/null 2>&1 &
  BRAIN=$!
fi
trap '[ -n "$BRAIN" ] && kill "$BRAIN" 2>/dev/null' EXIT INT TERM

echo "  [OK] Engine: $PY server.py"
echo "  Starting... the deck will open in your browser shortly."
echo ""
"$PY" server.py
