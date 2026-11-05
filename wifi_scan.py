#!/usr/bin/env python3
"""Wifi networks around the monitor RPi, with the signal in dBm.

Runs on the RPi itself (install_rpi.sh: systemd timer, every minute, as
root): NetworkManager scans on the free wlan0, then the results are read
via nl80211 (generic netlink, stdlib only, no `iw` needed) and written to
wifi_scan.json next to this script for monitor.py. nmcli only gives a
percentage that sticks at 100 % close by; dBm keeps showing a difference,
e.g. when an AP goes from Transmit Power High to Low.
    sudo python3 wifi_scan.py [wlan0]
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "wlan0"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_scan.json")
KEEP_S = 180

NL80211_CMD_GET_SCAN = 32
NL80211_ATTR_IFINDEX = 3
NL80211_ATTR_BSS = 47
BSS_BSSID, BSS_FREQUENCY, BSS_IES, BSS_SIGNAL_MBM, BSS_BEACON_IES = 1, 2, 6, 7, 11


def nl_attrs(data):
    out, i = {}, 0
    while i + 4 <= len(data):
        ln, typ = struct.unpack_from("HH", data, i)
        if ln < 4:
            break
        out[typ & 0x3FFF] = data[i + 4:i + ln]
        i += (ln + 3) & ~3
    return out


def info_elements(b):
    out, i = {}, 0
    while i + 2 <= len(b):
        t, n = b[i], b[i + 1]
        out.setdefault(t, b[i + 2:i + 2 + n])
        i += 2 + n
    return out


def channel(freq):
    return (freq - 2407) // 5 if freq < 3000 else (freq - 5000) // 5


def width_center(freq, ie):
    """Channel width and centre from the HT (61) and VHT (192) operation
    elements in the beacon."""
    vht, ht = ie.get(192), ie.get(61)
    if vht and len(vht) >= 3 and vht[0] >= 1:
        seg0, seg1 = vht[1], vht[2]
        if seg1 and abs(seg1 - seg0) == 8:
            return 160, 5000 + 5 * seg1
        return 80, 5000 + 5 * seg0
    if ht and len(ht) >= 2 and ht[1] & 0x04:   # STA channel width: 40 MHz allowed
        offset = ht[1] & 0x03                   # 1 = secondary above, 3 = below
        if offset in (1, 3):
            return 40, freq + (10 if offset == 1 else -10)
    return 20, freq


def scan_dump():
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16)   # NETLINK_GENERIC
    s.bind((0, 0))
    s.settimeout(3)

    def send(family, cmd, attrs, flags, seq):
        p = b"".join(struct.pack("HH", 4 + len(v), t) + v + b"\0" * (-len(v) % 4) for t, v in attrs)
        m = struct.pack("BBH", cmd, 1, 0) + p
        s.send(struct.pack("IHHII", 16 + len(m), family, flags, seq, 0) + m)

    send(0x10, 3, [(2, b"nl80211\0")], 1, 1)                     # CTRL_CMD_GETFAMILY
    fid = struct.unpack("H", nl_attrs(s.recv(65536)[20:])[1][:2])[0]
    send(fid, NL80211_CMD_GET_SCAN, [(NL80211_ATTR_IFINDEX, struct.pack("I", socket.if_nametoindex(IFACE)))],
         1 | 0x300, 2)                                           # NLM_F_REQUEST | NLM_F_DUMP
    out = []
    while True:
        d, i = s.recv(1 << 20), 0
        while i < len(d):
            ln, typ = struct.unpack_from("IH", d, i)
            if typ in (2, 3):                                    # NLMSG_ERROR / NLMSG_DONE
                s.close()
                return out
            a = nl_attrs(d[i + 20:i + ln])
            if NL80211_ATTR_BSS in a:
                b = nl_attrs(a[NL80211_ATTR_BSS])
                ie = info_elements(b.get(BSS_IES) or b.get(BSS_BEACON_IES) or b"")
                freq = struct.unpack("I", b[BSS_FREQUENCY][:4])[0]
                width, center = width_center(freq, ie)
                out.append({"ssid": ie.get(0, b"").decode("utf-8", "replace"),
                            "bssid": ":".join("%02X" % x for x in b.get(BSS_BSSID, b"")),
                            "freq": freq, "ch": channel(freq), "width": width, "center": center,
                            "dbm": struct.unpack("i", b[BSS_SIGNAL_MBM][:4])[0] / 100
                            if BSS_SIGNAL_MBM in b else None})
            i += (ln + 3) & ~3


def main():
    subprocess.run(["nmcli", "dev", "wifi", "rescan", "ifname", IFACE], capture_output=True, timeout=20)
    time.sleep(6)   # the scan takes a few seconds; rescan returns at once
    now = time.time()
    bss = scan_dump()
    for b in bss:
        b["seen"] = now
    # one scan sometimes misses a beacon: keep a network from the previous
    # scans for KEEP_S, with its last values
    try:
        with open(OUT) as f:
            seen = {(b["bssid"], b["freq"]) for b in bss}
            bss += [b for b in json.load(f)["bss"]
                    if (b["bssid"], b["freq"]) not in seen and now - b.get("seen", 0) < KEEP_S]
    except (OSError, ValueError, KeyError):
        pass
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": now, "iface": IFACE, "bss": bss}, f)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
