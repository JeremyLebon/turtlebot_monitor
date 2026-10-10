# TurtleBot Monitor

Overzicht van alle TurtleBots op één pagina: online/offline, batterij,
temperatuur, wifi, wat er draait (bringup, SLAM, Navigatie, joystick, camera),
softwareversie en waarschuwingen.

Elke robot **stuurt** elke 5 s zijn status naar deze monitor
(`system_info_node`, `MONITOR_URL`). Omgekeerd kan niet: elke robot zit achter
de NAT van zijn eigen AP. Enkel de Python-standaardbibliotheek, geen extra
pakketten.

## 1. Adres instellen (eenmalig)

1. Geef de laptop een **vast IP** op het subnet van de switch
   (DHCP-reservatie in de router van de switch).
2. Zet dat adres in [`../monitor.conf`](../monitor.conf), commit en push:
   `MONITOR_URL=http://<ip-van-de-laptop>:8090/push`
3. Op elke robot: **Software bijwerken** (statuspagina > Systeem). De setup
   zet `MONITOR_URL` in de `.env`, de container start opnieuw.

## 2. Monitor starten - kies één manier

### A. Windows, Python in Windows (eenvoudigst)

- Python 3 installeren (python.org of Microsoft Store).
- Dubbelklik `start_monitor.bat` - de pagina opent op http://localhost:8090.
- Eerste keer: Windows vraagt om Python toe te laten in de firewall - kies
  **Privénetwerken**. Of eenmalig als administrator:
  `powershell -ExecutionPolicy Bypass -File firewall_regel.ps1`

### B. WSL met mirrored networking (Windows 11 22H2+)

WSL deelt dan het IP-adres van Windows, zodat de robots de monitor bereiken.
In `%UserProfile%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Daarna `wsl --shutdown` (in PowerShell) en WSL opnieuw openen. De Hyper-V-
firewall van WSL laat binnenkomend verkeer standaard niet toe; eenmalig als
administrator in PowerShell:

```powershell
New-NetFirewallHyperVRule -Name TurtleBotMonitor -DisplayName "TurtleBot Monitor" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8090
```

Starten in WSL: `./start_monitor.sh`, pagina in Windows: http://localhost:8090.
(Mirrored mode helpt ook `turtlebot_vis`: `.local`-namen werken dan.)

### C. WSL in de standaard NAT-modus (port proxy)

Windows moet poort 8090 doorsturen naar WSL. Het IP van WSL verandert na elke
herstart, dus dit telkens opnieuw doen (PowerShell als administrator):

```powershell
$wsl = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy delete v4tov4 listenport=8090 listenaddress=0.0.0.0
netsh interface portproxy add v4tov4 listenport=8090 listenaddress=0.0.0.0 connectport=8090 connectaddress=$wsl
```

plus de firewallregel uit A (`firewall_regel.ps1`). Starten in WSL:
`./start_monitor.sh`.

## Knoppen

- **Alle robots:** STOP ALLES (zonder bevestiging - noodstop voor de klas),
  bringup starten, bijwerken, uitschakelen (met bevestiging).
- **Per robot:** Stop, Piep (robot terugvinden), en via "meer...":
  bringup/SLAM/Navigatie/joystick/camera starten of stoppen, bijwerken,
  herstarten, uitschakelen.
- De robot vraagt zelf om opdrachten (long-poll op `/poll`), dus ook dit werkt
  door de NAT; een opdracht komt binnen ~1 s aan. Het resultaat staat onder
  de knoppen van de robot.
- Uitschakelen: `MONITOR_URL=` leeg in monitor.conf (robots sturen niets, luisteren niet), `python monitor.py --view-only` (monitor zonder knoppen), of per robot het vinkje "Monitor-opdrachten toelaten" op de Systeem-pagina.
- De knoppen werken **enkel vanaf de laptop zelf** (`localhost`), zodat een
  student die de monitor vanaf een robot-AP opent niets kan bedienen. Bewust
  openzetten: `python monitor.py --remote-control`.
- Rijden kan niet via de monitor (vertraging, geen zicht op de robot) - daarvoor
  de statuspagina op de wifi van de robot of de joystick.

### D. Docker (Docker Desktop of Docker in WSL)

Geen eigen image: `docker-compose.yaml` draait `monitor.py` in het standaard
Python-image. In deze map: `docker compose up -d`, daarna
**http://localhost:8091** (met knoppen). Poort 8090 is voor de robots en voor
kijken zonder knoppen; 8091 wordt enkel op 127.0.0.1 gepubliceerd, zodat
niemand anders kan bedienen. Docker Desktop zet poort 8090 open op Windows
zelf - geen mirrored networking of port proxy nodig, wel de firewallregel
(`firewall_regel.ps1`). De kaartenbibliotheek blijft in `monitor/maps/`.

## Testen

- http://localhost:8090 toont alle robots uit `turtlebot_config.csv`, grijs
  tot ze iets sturen.
- Op een robot (statuspagina > Systeem) staat of het versturen lukt.
- Zonder monitor (laptop weg) proberen de robots het om de 30 s opnieuw -
  ze merken er verder niets van.

## Docenten-PIN voor de hele vloot

Knop **Docenten-PIN** in de balk "Alle robots": vraagt de PIN (4-8 cijfers)
twee keer en stuurt naar elke robot die luistert enkel de PBKDF2-hash (zelfde
formaat als `fenix_admin.py` op de robot). Daarmee ontgrendel je **Beheer** op
de statuspagina. Een robot die niet luistert (uit, of "Monitor-opdrachten"
uit) krijgt hem niet - dan later opnieuw, of op die robot zelf:
`docker exec -it turtlebot_<nr> fenix_admin.py set`.

