#!/bin/bash
# Monitor bijwerken naar de nieuwste versie op GitHub en herstarten.
#
#   bash ~/turtlebot_monitor/update.sh                       (op de RPi)
#   ssh <gebruiker>@<ip-rpi> bash turtlebot_monitor/update.sh  (vanaf de laptop)
#
# maps/, docent_pin en de logs staan niet in git: blijven staan.
set -e
cd "$(dirname "$0")"

if [ ! -d .git ]; then
    echo "Deze map is geen git-clone (oude kopie met scp): eerst overstappen, zie README.md 'Overstappen van de oude kopie'."
    exit 1
fi

old=$(cat VERSION 2>/dev/null)
old_commit=$(git rev-parse --short HEAD)
git pull --ff-only
new_commit=$(git rev-parse --short HEAD)

if [ "$old_commit" = "$new_commit" ]; then
    echo "Al de nieuwste versie: v$old · $new_commit"
    # toch (her)starten als de container weg is: na 'docker compose down' bleef
    # de kiosk anders op "site niet bereikbaar" staan (2026-11-12)
    docker compose up -d
else
    echo "v$old · $old_commit -> v$(cat VERSION) · $new_commit"
    docker compose up -d
    docker compose restart
fi
for i in $(seq 1 30); do
    v=$(curl -s --max-time 1 http://127.0.0.1:8090/api/robots | grep -o '"version": *"[^"]*"' | cut -d'"' -f4) && [ -n "$v" ] && break
    sleep 1
done
if [ -n "$v" ]; then
    echo "Monitor draait opnieuw: $v"
else
    echo "Monitor antwoordt nog niet, zie: docker logs turtlebot-monitor"
    exit 1
fi
