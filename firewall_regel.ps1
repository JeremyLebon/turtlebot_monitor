# Eenmalig, in PowerShell als administrator:
#   powershell -ExecutionPolicy Bypass -File firewall_regel.ps1
# Laat binnenkomende verbindingen op poort 8090 toe (de robots sturen hun
# status naar de monitor). Enkel op privé- en domeinnetwerken.
New-NetFirewallRule -DisplayName "TurtleBot Monitor (8090)" -Direction Inbound `
  -Protocol TCP -LocalPort 8090 -Action Allow -Profile Private,Domain
