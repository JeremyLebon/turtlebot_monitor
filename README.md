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

## Testen

- http://localhost:8090 toont alle robots uit `turtlebot_config.csv`, grijs
  tot ze iets sturen.
- Op een robot (statuspagina > Systeem) staat of het versturen lukt.
- Zonder monitor (laptop weg) proberen de robots het om de 30 s opnieuw -
  ze merken er verder niets van.
