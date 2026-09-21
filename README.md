# Turtlebot Monitor

Fleet-dashboard: een Flask-webpagina die laat zien welke turtlebots online
zijn (via netwerkscan) en hun batterijstatus toont (via ROS2, over
`domain_bridge` naar één centraal domein).

## Architectuur

- `app.py` - eenvoudigste versie: enkel netwerk-aanwezigheid (nmap-scan
  van `SCAN_SUBNET`, MAC-adres gematcht tegen `turtlebots.json`).
- `app_V1.py` - uitgebreide versie (wordt effectief gestart, zie
  `docker/Dockerfile`'s `CMD`): netwerkscan **+** een ROS2-node
  (`MultiBatteryMonitor`) die luistert op `/battery_state_1` t/m
  `/battery_state_9`.
- `domain_bridge_sub.yaml` / `domain_bridge_pub.yaml`: elke turtlebot
  draait op zijn eigen `ROS_DOMAIN_ID` (1-9, zie
  `turtlebot_setup/turtlebot_config.csv`). `domain_bridge` haalt
  `battery_state` (hernoemd naar `battery_state_N`) en `nav2_status` van
  elke robot naar domein 0 (waar dit dashboard op luistert), en kan in de
  andere richting `cmd_vel` van domein 0 naar elke robot sturen
  (centrale teleop-mogelijkheid).
- Web: `templates/index_V1.html` / `others_V1.html`, simpele
  auto-refreshende HTML-tabel (geen JS-framework).

## Status (2026-09-21)

Dit project lijkt **nooit volledig afgewerkt/getest** te zijn geweest:
`docker-compose.yaml`'s opstartcommando bevat nog een placeholder
(`ros2 launch my_package my_launch.py`) i.p.v. het effectieve
`domain_bridge`-commando - dat moet nog aangesloten worden voor dit
werkt.

### Bekende, nu-verouderde punten

1. **turtlebot09's MAC-adres is verouderd** in `turtlebots.json`
   (`88:A2:9E:2C:E5:AE` - het board dat sneuvelde en vervangen is). Nieuw
   MAC: `88:A2:9E:2C:EA:4D` (zie `turtlebot_setup/turtlebot_config.csv`
   op de `raspios-migration`-branch, al bijgewerkt daar).
2. **Onverenigbaar met de geplande per-robot access points.** De
   netwerkscan (`SCAN_SUBNET=192.168.60.0/24`) gaat uit van één gedeeld
   subnet voor alle robots. Zodra elke robot zijn eigen geïsoleerde
   AP/subnet krijgt (geplande TP-Link TL-WR902AC-opzet, zie
   `turtlebot_setup/docs/raspios-migration-log.md`), vindt deze ene scan
   geen enkele robot meer. Vereist een architecturale herziening (bv.
   scan per robot-subnet, of per-robot presence i.p.v. één centrale
   scanner) als dit dashboard behouden blijft.
3. **Geen Zenoh-ondersteuning.** `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`
   staat hard in `docker-compose.yaml`, en `ros-humble-rmw-zenoh-cpp`
   ontbreekt in de Dockerfile. Belangrijker: `domain_bridge`'s gedrag
   onder Zenoh (waar domain-ID eerder namespacing dan DDS-wire-niveau-
   partitionering is) is **niet gevalideerd** - dit vereist eigen
   onderzoek/testen, niet enkel een package toevoegen.

## Alternatief in overweging

Voor een eenvoudige, per-robot statuspagina ("belangrijkste zaken van de
turtlebot tonen", vergelijkbaar met iRobot Create3's ingebouwde webpagina)
wordt gekeken naar een lichtere aanpak zonder `domain_bridge`/nmap:
gewone HTML + `roslibjs` die rechtstreeks via `rosbridge_server`
(websocket) met de robot praat, zoals in `JeremyLebon/robot`'s
`components/robot_web`. Dat werkt inherent per-robot en heeft dus geen
last van de aankomende AP-isolatie. Zie `turtlebot_docker` voor de
uitwerking hiervan.
