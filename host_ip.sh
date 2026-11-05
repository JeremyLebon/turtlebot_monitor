#!/bin/sh
# Adressen van de monitor-RPi voor de pagina (install_rpi.sh, elke minuut mee
# met de wifi-scan): de container ziet zelf enkel zijn Docker-netwerk.
ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|veth)/ {print $2, $4}' > "$(dirname "$0")/host_ip.txt"
