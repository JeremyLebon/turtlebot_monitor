#!/bin/bash
# Opent de monitorpagina schermvullend (kiosk). Gestart bij het aanmelden via
# ~/.config/autostart/turtlebot-monitor.desktop (install_rpi.sh).
# Afsluiten: Alt+F4.
URL="${1:-http://localhost:8090/?kiosk}"

# Wachten tot de container antwoordt (Docker start soms later dan de desktop).
for _ in $(seq 120); do
  curl -s -o /dev/null "$URL" && break
  sleep 1
done

BROWSER=$(command -v chromium || command -v chromium-browser)
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --no-first-run --password-store=basic \
  "$URL"
