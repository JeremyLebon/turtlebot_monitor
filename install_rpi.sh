#!/bin/bash
# TurtleBot Monitor op een eigen Raspberry Pi 5 (optie E in README.md).
# Eenmalig uitvoeren OP de RPi, als gewone gebruiker (niet met sudo):
#
#   bash ~/turtlebot_monitor/monitor/install_rpi.sh            # monitor + scherm (alleen kijken)
#   bash ~/turtlebot_monitor/monitor/install_rpi.sh --knoppen  # scherm toont de pagina MET knoppen
#   bash ~/turtlebot_monitor/monitor/install_rpi.sh --geen-scherm
#
# Doet: Docker installeren, de monitor-container starten (start daarna vanzelf
# bij elke opstart), knoppen om de RPi te herstarten/af te sluiten, en met een desktop: automatisch aanmelden, scherm niet
# laten uitgaan en de monitorpagina schermvullend openen bij het opstarten.
# Opnieuw uitvoeren mag (bv. om --knoppen aan of uit te zetten).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
KIOSK=view
case "${1:-}" in
  "") ;;
  --knoppen) KIOSK=control ;;
  --geen-scherm) KIOSK=none ;;
  *) echo "Onbekende optie: $1 (gebruik --knoppen of --geen-scherm)"; exit 1 ;;
esac

if [ "$(id -u)" = 0 ]; then
  echo "Voer dit uit als gewone gebruiker, niet met sudo."; exit 1
fi
if [ ! -f "$HERE/../turtlebot_config.csv" ]; then
  echo "turtlebot_config.csv ontbreekt naast de map monitor/ (zie README.md, optie E)."; exit 1
fi

# 1. Docker
if ! command -v docker >/dev/null; then
  echo "== Docker installeren"
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

# 2. Monitor-container (restart: unless-stopped -> start mee bij opstart)
echo "== Monitor starten"
cd "$HERE"
sudo docker compose up -d

# 3. Knoppen "Monitor-RPi: Herstarten / Afsluiten": de container schrijft
#    power_request in deze map, een systemd path unit voert het uit op de RPi.
echo "== Knoppen herstarten/afsluiten"
sudo tee /etc/systemd/system/turtlebot-monitor-power.path >/dev/null <<EOF
[Unit]
Description=TurtleBot Monitor: herstarten/afsluiten vanaf de monitorpagina
[Path]
PathExists=$HERE/power_request
[Install]
WantedBy=multi-user.target
EOF
sudo tee /etc/systemd/system/turtlebot-monitor-power.service >/dev/null <<EOF
[Unit]
Description=TurtleBot Monitor: herstarten/afsluiten uitvoeren
[Service]
Type=oneshot
# \$\$ = a literal \$ for the shell (systemd expands \$VAR itself)
ExecStart=/bin/sh -c 'a=\$\$(cat "$HERE/power_request"); rm -f "$HERE/power_request"; case "\$\$a" in reboot) systemctl reboot ;; poweroff) systemctl poweroff ;; esac'
EOF
rm -f "$HERE/power_request"
touch "$HERE/.rpi_power"
sudo systemctl daemon-reload
sudo systemctl enable --now turtlebot-monitor-power.path

# 3b. Wifi-omgeving: de vrije wlan0 van de RPi (die hangt aan de kabel) scant
#     elke minuut; de monitor toont alle netwerken in de buurt (Wifikanalen).
if nmcli -t -f DEVICE,TYPE dev 2>/dev/null | grep -q ':wifi$'; then
  echo "== Wifi-scan elke minuut"
  sudo tee /etc/systemd/system/turtlebot-monitor-wifiscan.service >/dev/null <<EOF
[Unit]
Description=TurtleBot Monitor: wifinetwerken in de buurt scannen
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'nmcli -t -f SSID,BSSID,CHAN,FREQ,BANDWIDTH,SIGNAL dev wifi list --rescan yes > "$HERE/wifi_scan.tmp" && mv "$HERE/wifi_scan.tmp" "$HERE/wifi_scan.txt"'
EOF
  sudo tee /etc/systemd/system/turtlebot-monitor-wifiscan.timer >/dev/null <<EOF
[Unit]
Description=TurtleBot Monitor: wifi-scan elke minuut
[Timer]
OnBootSec=30
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now turtlebot-monitor-wifiscan.timer
fi

# 4. Scherm: pagina schermvullend bij opstart
AUTOSTART="$HOME/.config/autostart/turtlebot-monitor.desktop"
if [ "$KIOSK" = none ]; then
  rm -f "$AUTOSTART"
elif [ ! -x /usr/sbin/lightdm ]; then
  echo "== Geen desktop gevonden (Raspberry Pi OS Lite): enkel de monitor, geen scherm."
  KIOSK=none
else
  echo "== Scherm instellen"
  sudo raspi-config nonint do_boot_behaviour B4   # desktop, automatisch aanmelden
  sudo raspi-config nonint do_blanking 1          # scherm niet laten uitgaan
  PORT=8090; [ "$KIOSK" = control ] && PORT=8091
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=TurtleBot Monitor
Exec=$HERE/kiosk.sh http://localhost:$PORT/?kiosk
X-GNOME-Autostart-enabled=true
EOF
  chmod +x "$HERE/kiosk.sh"
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "Klaar. Monitor: http://$IP:8090 (kijken), knoppen: http://localhost:8091 op deze RPi"
echo "of vanaf je laptop via: ssh -L 8091:localhost:8091 $USER@$IP"
echo "Robots: MONITOR_URL=http://$IP:8090/push in turtlebot_robot/monitor.conf"
echo "        (vast IP nodig: DHCP-reservatie in de router van de switch)."
[ "$KIOSK" != none ] && echo "Herstart de RPi (sudo reboot): daarna opent de pagina vanzelf."
exit 0
