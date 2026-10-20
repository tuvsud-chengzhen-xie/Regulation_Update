#!/usr/bin/env sh

# ISO, BIFMA, ALI and WSTDA are fetched through headless Chromium. App Service
# images do not ship it, so install it into persistent /home storage in the
# background (the app starts at once; until Chromium is ready those sources
# fall back to plain HTTPS and any failure is reported on the Issues page).
# Set INSTALL_BROWSER=false to skip.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/home/ms-playwright}"
if [ "${INSTALL_BROWSER:-true}" = "true" ]; then
  LOG_DIR="${LOG_DIR:-/home/LogFiles}"
  mkdir -p "$LOG_DIR" "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null
  (python -m playwright install --with-deps chromium \
     || python -m playwright install chromium) >"$LOG_DIR/playwright-install.log" 2>&1 &
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"