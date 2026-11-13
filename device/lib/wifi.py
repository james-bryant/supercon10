"""Wi-Fi (station mode) for the badge.

The network name and password live in data/wifi.json:

    {"ssid": "...", "password": "..."}

main.py runs keep_connected() as an asyncio task. It joins in the background
and rejoins if the link drops, so boot, buttons and the display never wait on
the network. With the ESP-NOW mesh on the badge, it only joins an access point
on the mesh's channel (lib/espnow_mesh.py, join_wifi), so the badges on and off
Wi-Fi can hear each other.

From the REPL (Ctrl-C out of main first):

    >>> import lib.wifi as wifi
    >>> wifi.connect()      # blocks up to 20 s, True once there's an IP
    >>> wifi.ip()
    >>> wifi.status()
    >>> wifi.disconnect()   # radio off, saves battery
"""

import asyncio as aio
import json
import time

import network

try:
    import lib.espnow_mesh as _mesh
except ImportError:  # no mesh on this badge: join whichever access point
    _mesh = None

CONFIG_FILE = "data/wifi.json"
CHECK_EVERY_S = 2         # how often the background task looks at the link
RETRY_AFTER_S = 30        # start a fresh join if still offline after this long
CONNECT_TIMEOUT_S = 20    # how long the blocking connect() waits

wlan = network.WLAN(network.STA_IF)
_wanted = True            # False after disconnect(), so the task leaves it off

# Map the port's STAT_* constants to short names (not every port has them all).
_STATUS_NAMES = {}
for _n in ("STAT_IDLE", "STAT_CONNECTING", "STAT_GOT_IP", "STAT_NO_AP_FOUND",
           "STAT_WRONG_PASSWORD", "STAT_BEACON_TIMEOUT", "STAT_ASSOC_FAIL",
           "STAT_HANDSHAKE_TIMEOUT", "STAT_CONNECT_FAIL"):
    if hasattr(network, _n):
        _STATUS_NAMES[getattr(network, _n)] = _n[5:].lower()


def _credentials():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg["ssid"], cfg.get("password", "")
    except (OSError, ValueError, KeyError) as ex:
        print("wifi: can't read", CONFIG_FILE, "-", repr(ex))
        return None, None


def _begin():
    """Start one join attempt without waiting. False if there are no credentials."""
    ssid, password = _credentials()
    if not ssid:
        return False
    wlan.active(True)
    try:
        wlan.disconnect()  # drop any half-finished attempt; harmless when idle
    except OSError:
        pass
    if _mesh is not None:
        _mesh.join_wifi(wlan, ssid, password)  # if no AP on the mesh's channel, the next try looks again
    else:
        print("wifi: joining", ssid)
        wlan.connect(ssid, password)
    return True


def is_connected():
    return wlan.isconnected()


def ip():
    """The badge's IP address, or None when not connected."""
    return wlan.ifconfig()[0] if wlan.isconnected() else None


def status():
    """Short link state, e.g. 'got_ip 10.0.1.23', 'connecting', 'wrong_password'."""
    if not wlan.active():
        return "off"
    name = _STATUS_NAMES.get(wlan.status(), str(wlan.status()))
    return (name + " " + ip()) if wlan.isconnected() else name


def connect(timeout_s=CONNECT_TIMEOUT_S):
    """Join and wait for an IP (blocking). For the REPL; main.py uses keep_connected()."""
    global _wanted
    _wanted = True
    if wlan.isconnected():
        return True
    if not _begin():
        return False
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if wlan.isconnected():
            print("wifi: connected,", ip())
            return True
        time.sleep_ms(200)
    print("wifi: not connected yet -", status())
    return False


def disconnect():
    """Leave the network and turn the radio off until connect() is called."""
    global _wanted
    _wanted = False
    try:
        wlan.disconnect()
    except OSError:
        pass
    wlan.active(False)


async def keep_connected():
    """Background task: join, then watch the link and rejoin when it drops."""
    was_connected = False
    last_try = None
    while True:
        try:
            connected = wlan.isconnected()
            if connected != was_connected:
                if connected:
                    print("wifi: connected,", ip())
                else:
                    print("wifi: link dropped -", status())
                was_connected = connected
                # After a drop the driver retries on its own; give it a head start.
                last_try = time.ticks_ms()
            if (not connected and _wanted and
                    (last_try is None or
                     time.ticks_diff(time.ticks_ms(), last_try) > RETRY_AFTER_S * 1000)):
                if last_try is not None:
                    print("wifi: still offline -", status(), "- retrying")
                if not _begin():
                    return  # no usable data/wifi.json; nothing more to do
                last_try = time.ticks_ms()
        except Exception as ex:  # never let a driver hiccup kill the task
            print("wifi: error -", repr(ex))
        await aio.sleep(CHECK_EVERY_S)
