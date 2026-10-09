@echo off
rem TurtleBot Monitor op Windows starten (dubbelklikken).
rem Vereist Python 3 (python.org of Microsoft Store), geen extra pakketten.
rem Eerste keer: sta Python toe in de Windows-firewall voor "Privénetwerken",
rem anders kunnen de robots de monitor niet bereiken.
cd /d "%~dp0"
start "" http://localhost:8090
python monitor.py %*
if errorlevel 1 py monitor.py %*
pause
