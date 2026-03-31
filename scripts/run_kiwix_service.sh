#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPMASTER_ENV_FILE:-${SOPR_ENV_FILE:-$REPO_ROOT/config/sopr.env}}"
if [[ ! -f "$ENV_FILE" && -f "$REPO_ROOT/config/prepmaster.env" ]]; then
  ENV_FILE="$REPO_ROOT/config/prepmaster.env"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

resolve_kiwix_bind_address() {
  if [[ "${KIWIX_BIND_ADDRESS:-auto}" != "auto" ]]; then
    echo "$KIWIX_BIND_ADDRESS"
    return
  fi

  echo "127.0.0.1"
}

KIWIX_EFFECTIVE_BIND_ADDRESS="$(resolve_kiwix_bind_address)"

if [[ -s "${KIWIX_LIBRARY_XML:-}" ]]; then
  exec /usr/bin/kiwix-serve \
    --address="$KIWIX_EFFECTIVE_BIND_ADDRESS" \
    --port="$KIWIX_PORT" \
    --library "$KIWIX_LIBRARY_XML"
fi

PLACEHOLDER_DIR="${PREPMASTER_WEB_ROOT:-/srv/sopr/www}/kiwix-placeholder"
mkdir -p "$PLACEHOLDER_DIR"

if [[ ! -f "$PLACEHOLDER_DIR/index.html" ]]; then
  cat > "$PLACEHOLDER_DIR/index.html" <<'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiwix Not Ready</title>
  <style>
    body { font-family: sans-serif; background: #08111e; color: #eef4fb; margin: 0; padding: 40px; }
    main { max-width: 760px; margin: 0 auto; padding: 24px; border: 1px solid #29435c; border-radius: 18px; background: #102238; }
    p { color: #9bb0c7; }
  </style>
</head>
<body>
  <main>
    <h1>Kiwix Content Not Ready Yet</h1>
    <p>The Kiwix service is online, but no library has been built yet. Run the content download/apply flow to load ZIM files.</p>
  </main>
</body>
</html>
EOF
fi

exec /usr/bin/python3 -m http.server "$KIWIX_PORT" --bind "$KIWIX_EFFECTIVE_BIND_ADDRESS" --directory "$PLACEHOLDER_DIR"
