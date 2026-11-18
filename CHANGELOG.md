# Changelog - TurtleBot Monitor

Versienummer in `VERSION` (MAJOR.MINOR.PATCH): PATCH = enkel fixes, MINOR =
nieuwe functies, MAJOR = grote herindeling. Verhogen bij elke wijziging die op
de monitor-RPi komt (`git pull`). De pagina toont het naast de titel
("v1.0.0 · e46ac83", met de commit van de clone), de log bij het starten,
`/api/robots` in het veld `version`.

## 1.3.1 - 2026-11-18
- **Fout: de vlootkaart bleef leeg als de kaart nog niet in de bibliotheek
  stond.** De pagina probeerde de kaart maar één keer op te halen. Nu om de
  10 s opnieuw, dus ze verschijnt zodra je ze met "kaart naar bibliotheek"
  doorstuurt. Een kaart die opnieuw onder dezelfde naam in de bibliotheek komt,
  wordt ook opnieuw geladen.

## 1.3.0 - 2026-11-18
- **Vlootkaart** (concept): robots met Navigatie
  op een kaart uit de bibliotheek sturen hun positie naar `POST /fleet` en
  krijgen de andere robots terug. De pagina toont ze live op de kaart: spoor,
  gepland pad, doel, waarschuwing onder 0,5 m. `GET /api/fleet`. Testen
  zonder robots: `tools/fake_fleet.py`. Robotkant: `robot/fleet_node.py`
  (hoort in turtlebot_docker) met `fleet_core.py`: onthoudt de laatst gekende
  posities, service `/fleet/nearby`, andere robots als obstakel via de Nav2
  KeepoutFilter. Uitleg en volgende fasen: `docs/vloot.md`.
- **Navigatietoestand op de tegel** (robot-image 1.10.0+, veld `nav`): doel
  of route, punt n/m, onderweg/gepauzeerd/mislukt, resterende afstand;
  waarschuwing "navigatie mislukt".

## 1.2.1 - 2026-11-18
- **5V-voeding van de Pi** (`ext5v_v`, de PMIC-meting die de robots al
  meestuurden): in de opengeklapte tegel (rij *5V Pi*), waarschuwing onder
  4,85 V en als de Pi sinds de opstart onderspanning had (get_throttled bit 16).
  In de historiek een grafiek met lijnen op 4,85 V en 4,63 V, en een gebeurtenis
  bij een dip onder 4,85 V (fout onder 4,75 V, hoogstens 1 per minuut). Een
  bestaande `history.db` krijgt de kolom er vanzelf bij.
- Beperking: de robot meet elke 5 s één keer; kortere dips zie je enkel via
  "onderspanning sinds opstart".

## 1.2.0 - 2026-11-18
- **Historiek en gebeurtenissen** (`history.py`, `history.db`): de waarden van
  elke robot (elke 10 s, 30 dagen) en gebeurtenissen die de monitor afleidt uit
  de statusberichten: offline/online, Pi herstart, onderspanning, batterij,
  wifi, toestand, diensten, OpenCR/USB, ROS-fouten, software. Onder de tegels
  de gebeurtenissen van de laatste 24 u; per robot de knop Historiek met
  grafieken, gebeurtenissen en CSV-export. Uitleg in README.md.
- De vlootkaart (branch `feature/vlootkaart`, daar nog 1.2.0) wordt bij het
  mergen 1.3.0.

## 1.1.6 - 2026-11-18
- **Fout: op de kiosk werd je ~5 s na Docent aanmelden weer afgemeld.** Een tweede,
  onzichtbaar kioskvenster of -tabblad deelt de aanmeldcookie, maar niemand raakt
  het aan: zijn afmeldtimer meldde iedereen af. Nu onthouden alle vensters samen de
  laatste aanraking, meldt enkel een zichtbare pagina af, tellen touch-events mee
  en telt aanmelden zelf als aanraking.
- Afmelden na **30 min** zonder aanraking (was 10), met een gele melding de laatste
  minuut ("tik hier om aangemeld te blijven").
- Op Docent afmelden tikken vraagt eerst of je zeker bent.
- Aanmeldingen blijven bewaard na een herstart van de monitor (`sessions.json`,
  niet in git), dus ook na Monitor bijwerken.

## 1.1.5 - 2026-11-18
- **Monitor bijwerken met een knop** (naast Monitor-RPi Herstarten/Afsluiten, na
  Docent aanmelden): de container schrijft `update_request`, een systemd path unit
  op de RPi voert `update.sh` uit (git pull + container herstarten), uitvoer in
  `update_log.txt` (laatste regels in de tooltip van de knop). De monitor kijkt elke
  10 min op GitHub of er een nieuwere commit is; dan kleurt de knop geel.
- Elke open pagina (ook de kiosk) herlaadt vanzelf zodra de monitor een andere
  versie meldt, ook na `update.sh` via ssh.
- Eenmalig nodig op de RPi: `bash install_rpi.sh` opnieuw uitvoeren (zelfde opties
  als bij de installatie); dat zet de path unit en `.self_update` klaar. Zonder die
  stap blijft de knop verborgen.

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
