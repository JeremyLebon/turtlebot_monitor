# Changelog - TurtleBot Monitor

Versienummer in `VERSION` (MAJOR.MINOR.PATCH): PATCH = enkel fixes, MINOR =
nieuwe functies, MAJOR = grote herindeling. Verhogen bij elke wijziging die op
de monitor-RPi komt (`git pull`). De pagina toont het naast de titel
("v1.0.0 · e46ac83", met de commit van de clone), de log bij het starten,
`/api/robots` in het veld `version`.

## 1.1.0 - 2026-11-11
- **Terug naar een oudere robotversie** (na *Docent aanmelden*). Per robot:
  *Terug naar vX.Y.Z* (de vorige image op de robot, zonder internet; nog eens
  = terug naar de nieuwe) en *versie kiezen...* (de versies op Docker Hub,
  internet via de AP nodig). Voor de aangevinkte robots: keuzelijst *andere
  versie...* naast *Bijwerken*. Altijd met bevestiging. Werkt op robots vanaf
  image v1.9.4; oudere robots tonen "nu enkel via Fenix > Beheer".

## 1.0.0 - 2026-11-11
Eerste genummerde versie, sinds de monitor een eigen repo is (voordien
`turtlebot_setup/monitor`, gekopieerd met `scp`).
- **Versienummer** naast de titel en in de log.
- **Iconenregel onder de titel**: robots online, wifi-verkeer van alle robots
  samen (↓/↑) en open Fenix-pagina's; de samenvatting erboven is korter.
- **Cijferklavier** voor de docenten-PIN (Docent aanmelden, Docenten-PIN):
  werkt op het touchscreen zonder toetsenbord.
- **ROS-fouten als lijst** in de opengeklapte tegel.
- **Robotlijst** uit `turtlebot_robot/turtlebot_config.csv` (elke 10 min,
  met een lokale kopie voor een start zonder internet).
- Wifi: kanaalplan (36 + 48), wifiscan in dBm, wifitest per robot.
- Bijwerken in groepjes, kaartenbibliotheek, knoppen per robot en voor alle
  robots, kiosk op de RPi (`install_rpi.sh`).
