# Changelog - TurtleBot Monitor

Versienummer in `VERSION` (MAJOR.MINOR.PATCH): PATCH = enkel fixes, MINOR =
nieuwe functies, MAJOR = grote herindeling. Verhogen bij elke wijziging die op
de monitor-RPi komt (`git pull`). De pagina toont het naast de titel
("v1.0.0 · e46ac83", met de commit van de clone), de log bij het starten,
`/api/robots` in het veld `version`.

## 1.1.4 - 2026-11-13
- **Wifikanaal per robot**, om de robots verdeeld uit te delen: gekleurd
  bolletje met het kanaal (36, 48, ...) naast de naam van elke robot, en
  bovenaan per kanaal welke robots erop zitten (vet = online). Bron: wat de
  robot meldt, anders de wifiscan van de monitor, anders het plan
  (`wifi_kanalen.csv`, bleek bolletje).

## 1.1.3 - 2026-11-12
- **Per robot: is zijn wifi TB-AP-<nr> zichtbaar?** Uit de wifiscan van de
  monitor-RPi. In de kop van de tegel: groen *AP* = zichtbaar, geel *AP ✗* =
  robot online maar zijn AP niet gezien (een robot die uit staat toont niets,
  of groen *AP* als zijn wifi toch uitzendt). Open tegel: regel *TB-AP-<nr>*
  met band, kanaal en signaal. Zonder recente scan: niets.

## 1.1.2 - 2026-11-12
- **`update.sh` start de monitor ook als hij al de nieuwste versie heeft.**
  Na `docker compose down` en daarna `update.sh` (niets nieuws op GitHub)
  stopte het script meteen: geen container, de kiosk bleef op "site niet
  bereikbaar" staan, ook na een herstart van de RPi.

## 1.1.1 - 2026-11-11
- **Terugschakelen ook voor robots op v1.9.4.** Die nemen de opdracht aan,
  maar melden nog niet welke versie als vorige bewaard is (pas vanaf v1.9.5).
  De knop heet dan *Terug naar vorige versie*; vroeger toonde de monitor voor
  1.9.4 ten onrechte "nu enkel via Fenix > Beheer". Getest op turtlebot09:
  1.9.4 -> 1.9.3 vanuit de monitor.

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
