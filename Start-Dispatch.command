#!/usr/bin/env bash
# ============================================================
#   DISPATCH LIVE  -  Mac / Linux launcher
#   Runs the local server (which opens the deck in your browser).
#   Requires Python 3.7+  OR  Node.js 18+  (no other dependencies).
# ============================================================
cd "$(dirname "$0")" || exit 1

echo ""
echo "  =================================================="
echo "    DISPATCH LIVE  -  TV + News + Red Alerts"
echo "  =================================================="
echo ""

# --- Close anything already listening on port 3100 (clean restart) ---
if command -v lsof >/dev/null 2>&1; then
  OLDPID=$(lsof -ti tcp:3100 2>/dev/null)
  if [ -n "$OLDPID" ]; then
    echo "  Closing old server on port 3100 (PID $OLDPID)..."
    kill -9 $OLDPID 2>/dev/null
    sleep 1
  fi
fi

# --- Prefer Python 3, fall back to Node ---
if command -v python3 >/dev/null 2>&1; then
  echo "  [OK] Engine: python3 server.py"
  echo "  Starting... the deck will open in your browser shortly."
  echo ""
  exec python3 server.py
elif command -v python >/dev/null 2>&1; then
  echo "  [OK] Engine: python server.py"
  exec python server.py
elif command -v node >/dev/null 2>&1; then
  echo "  [OK] Engine: node server.js"
  exec node server.js
else
  echo "  [!] Neither Python 3 nor Node.js was found."
  echo ""
  echo "  Install ONE of these (both are free):"
  echo "    - Python:  https://www.python.org/downloads/"
  echo "    - Node.js: https://nodejs.org"
  echo ""
  read -p "  Press Enter to close..."
  exit 1
fi
