"""ESP-NOW mesh: every node broadcasts a heartbeat and relays what it hears.

Self-contained (only MicroPython's network, espnow and asyncio), so the same
file runs on any MicroPython ESP32. On the badge, main() starts it with:

    import lib.espnow_mesh as espnow_mesh
    espnow_mesh.start()

Each node broadcasts a heartbeat every HEARTBEAT_S seconds. When a node hears a
packet for the first time it prints it and, while the packet's ttl is above 0,
broadcasts it again with ttl one lower and hops one higher. Nodes remember the
origin and seq of each packet for FORGET_S seconds and drop copies they have
already handled, so a packet crosses each node once and then dies out.

Packet layout (big-endian), always sent to ff:ff:ff:ff:ff:ff:

    offset size field
       0    2  magic    b"SX"; anything else is other ESP-NOW traffic, ignored
       2    1  version  1; other versions are ignored
       3    1  type     1 heartbeat, 2 text; unknown types are still relayed
       4    1  ttl      relays left: relayed only while ttl > 0, as ttl - 1
       5    1  hops     relays so far: 0 means heard straight from the origin
       6    6  origin   MAC of the node that created the packet
      12    2  seq      origin's packet counter, from a random start each boot
      14    1  length   payload bytes; the packet must be exactly 15 + length
      15    n  payload  heartbeat: seconds since the origin's mesh started
                        (4 bytes); text: UTF-8

There is no checksum: the radio already drops corrupted frames. The random seq
start keeps a rebooted node's packets from looking like duplicates.

Channel: ESP-NOW only reaches nodes on the same Wi-Fi channel. An idle node is
put on CHANNEL, and a node on Wi-Fi uses its access point's channel, so code
that joins Wi-Fi should use join_wifi(): it only joins an access point on
CHANNEL, and the link is dropped if that access point moves to another channel.
Nodes print "mesh: on channel N". A badge still searching for its Wi-Fi network
hops channels and misses packets.

Bridges: another link, such as MQTT, adds a function to `bridges`; it is handed
every packet this node sends or relays. Packets arriving over that link go to
receive(), which applies the same duplicate filter and TTL as the radio.

Apps: functions in `listeners` are called as fn(type, origin, seq, hops,
payload) for every new packet this node hears, over the radio or a bridge;
node_count() is how many nodes were heard in the last FORGET_S seconds. While a
listener runs, rx_time is when the packet arrived (time.ticks_ms, stamped by the
radio driver) and rx_from is the radio neighbour it came from, or a bridge name
such as "mqtt". send(..., ttl=0) reaches radio neighbours only: nobody relays
it and bridges don't carry it. Packet types in `quiet` aren't printed.

From the REPL (Ctrl-C out of main first):

    >>> import lib.espnow_mesh as mesh
    >>> mesh.send("hello")   # one text packet into the mesh
    >>> mesh.run()           # run just the mesh and print what it hears
"""

import asyncio as aio
import binascii
import os
import struct
import time

import network

try:
    import espnow
except ImportError:  # firmware without ESP-NOW: the mesh stays off
    espnow = None

HEARTBEAT_S = 5        # how often this node broadcasts a heartbeat
TTL = 3                # how many times a new packet may be relayed
CHANNEL = 1            # the mesh's Wi-Fi channel; join_wifi() keeps Wi-Fi on it too
FORGET_S = 60          # how long packets (duplicate filter) and nodes are kept
STATS_S = 60           # how often to print a summary line
PRINT_PACKETS = True   # print every new packet heard

MAGIC = b"SX"
VERSION = 1
HEARTBEAT = 1          # packet types
TEXT = 2

_HEADER = ">2sBBBB6sHB"                # magic version type ttl hops origin seq length
HEADER_LEN = struct.calcsize(_HEADER)  # 15
MAX_PAYLOAD = 250 - HEADER_LEN         # ESP-NOW carries at most 250 bytes
BROADCAST = b"\xff" * 6
_SEEN_MAX = 4096                       # flood guard for the duplicate filter
_IDLE = getattr(network, "STAT_IDLE", 1000)

_wlan = network.WLAN(network.STA_IF)
_e = None              # the ESPNow object once the radio is up
_mac = b""
_seq = 0
_seen = {}             # origin + seq -> ticks_ms first heard (duplicate filter)
_nodes = {}            # origin -> ticks_ms last heard
_channel = None
_joined_ap = None      # BSSID that join_wifi() joined, kept to CHANNEL
wifi_tried_ms = None   # time.ticks_ms of the last join_wifi(), so joiners don't overlap
_started = False
_last_error = None
_start_s = time.time()
stats = {"sent": 0, "heard": 0, "dup": 0, "relayed": 0, "ignored": 0, "errors": 0}
bridges = []           # functions fn(pkt) that carry packets over other links
listeners = []         # functions fn(type, origin, seq, hops, payload) for new packets
quiet = set()          # packet types not printed, e.g. clock sync
rx_time = 0            # while listeners run: when the packet arrived (time.ticks_ms)
rx_from = None         # ...and where from: a neighbour's MAC, or a bridge name
_TICKS_MASK = time.ticks_add(0, -1)  # wraps the driver's arrival times like ticks_ms


def _hex(mac):
    return binascii.hexlify(mac, ":").decode()


def _report(what, ex):
    """Print a problem once instead of every time it happens."""
    global _last_error
    text = "%s - %r" % (what, ex)
    if text != _last_error:
        print("mesh:", text)
        _last_error = text


def _radio_up():
    """Bring up the station interface and ESP-NOW. True when ready."""
    global _e, _mac, _seq
    if _e is not None:
        return True
    if espnow is None:
        print("mesh: no espnow module in this firmware, mesh off")
        return False
    try:
        _wlan.active(True)
        try:
            # Wi-Fi power saving naps the radio between AP beacons, missing packets.
            _wlan.config(pm=_wlan.PM_NONE)
        except Exception:
            pass
        e = espnow.ESPNow()
        e.config(rxbuf=8192)  # holds packets that arrive while an app is busy
        e.active(True)
        try:
            e.add_peer(BROADCAST)
        except OSError as ex:
            if "EXIST" not in str(ex):  # already added before a soft reboot
                raise
        _mac = _wlan.config("mac")
        _seq = int.from_bytes(os.urandom(2), "big")
    except Exception as ex:
        print("mesh: can't start ESP-NOW -", repr(ex))
        return False
    _e = e
    _check_channel()
    return True


def _check_channel():
    """Put an idle station on CHANNEL, and leave an access point that join_wifi()
    joined if it has moved to another channel (other Wi-Fi links are left alone);
    log changes."""
    global _channel, _joined_ap
    try:
        if _wlan.status() == _IDLE and _wlan.config("channel") != CHANNEL:
            _wlan.config(channel=CHANNEL)
        elif _joined_ap and _wlan.isconnected() and _wlan.config("channel") != CHANNEL:
            print("wifi: the access point moved to channel %d, leaving it (the mesh is on %d)"
                  % (_wlan.config("channel"), CHANNEL))
            _joined_ap = None
            _wlan.disconnect()
    except Exception as ex:
        _report("can't set channel %d" % CHANNEL, ex)
    try:
        channel = _wlan.config("channel")
    except Exception:
        channel = None
    if channel != _channel:
        print("mesh: on channel", channel)
        _channel = channel


def join_wifi(wlan, ssid, password=""):
    """Join network ssid through an access point on CHANNEL. False if none is in range.

    A badge on Wi-Fi uses its access point's channel, so on a network with access
    points on several channels a badge could end up where the rest of the mesh
    can't hear it. This scans (about 2 s, during which nothing else runs) and
    joins the strongest access point called ssid on CHANNEL, by its BSSID, so the
    badge doesn't roam to another one. Call it with the station active and not
    joining, and not more often than every half minute or so.
    """
    global _joined_ap, wifi_tried_ms
    wifi_tried_ms = time.ticks_ms()
    want = ssid.encode() if isinstance(ssid, str) else ssid
    try:
        aps = wlan.scan()
    except OSError:  # the driver may still be leaving a join attempt
        time.sleep_ms(200)
        try:
            aps = wlan.scan()
        except OSError as ex:
            print("wifi: can't scan for %s - %r" % (ssid, ex))
            return False
    best, elsewhere = None, []
    for ap in aps:  # (ssid, bssid, channel, rssi, security, hidden)
        if ap[0] != want:
            continue
        if ap[2] == CHANNEL:
            if best is None or ap[3] > best[3]:
                best = ap
        elif ap[2] not in elsewhere:
            elsewhere.append(ap[2])
    if best is None:
        print("wifi: no %s access point on channel %d%s" % (ssid, CHANNEL, (
            ", only on " + ", ".join(str(c) for c in sorted(elsewhere))) if elsewhere else ""))
        return False
    print("wifi: joining %s at %s on channel %d" % (ssid, _hex(best[1]), CHANNEL))
    wlan.connect(ssid, password, bssid=best[1])
    _joined_ap = best[1]
    return True


def _transmit(pkt):
    """Broadcast one packet. True if the radio took it."""
    global _last_error
    if _e is None:
        return False
    try:
        _e.send(BROADCAST, pkt, False)  # broadcasts are never ACKed, so don't wait
    except Exception as ex:
        stats["errors"] += 1
        _report("send failed", ex)
        return False
    _last_error = None
    return True


def _bridge(pkt, source=None):
    """Hand a packet to every bridge except the one it came from."""
    for fn in bridges:
        if fn is not source:
            try:
                fn(pkt)
            except Exception as ex:
                stats["errors"] += 1
                _report("bridge failed", ex)


def send(payload, ptype=TEXT, ttl=TTL):
    """Put one new packet into the mesh: str or bytes, up to MAX_PAYLOAD bytes.

    ttl=0 reaches radio neighbours only: nobody relays it, and a bridge would be a relay.
    """
    global _seq
    if not _radio_up():
        return False
    if isinstance(payload, str):
        payload = payload.encode()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload over %d bytes" % MAX_PAYLOAD)
    _seq = (_seq + 1) & 0xFFFF
    pkt = struct.pack(_HEADER, MAGIC, VERSION, ptype, ttl, 0, _mac, _seq, len(payload)) + payload
    if ttl > 0:
        _bridge(pkt)
    if not _transmit(pkt):
        return False
    stats["sent"] += 1
    return True


def _print_packet(ptype, origin, seq, hops, peer, payload):
    if ptype == HEARTBEAT and len(payload) == 4:
        kind, detail = "heartbeat", "up %ds" % struct.unpack(">I", payload)[0]
    elif ptype == TEXT:
        kind, detail = "text", repr(payload)
    else:
        kind, detail = "type %d" % ptype, "%d bytes" % len(payload)
    if isinstance(peer, str):  # arrived over a bridge, e.g. "mqtt"
        via = " via " + peer
    else:
        via = " via " + _hex(peer) if hops else ""
    print("mesh: %s from %s seq=%d hops=%d%s, %s" % (kind, _hex(origin), seq, hops, via, detail))


def _arrival(peer):
    """When the radio received the packet just read from peer, as time.ticks_ms."""
    try:
        return _e.peers_table[peer][1] & _TICKS_MASK  # stamped by the driver on arrival
    except Exception:  # firmware without the peers table: now is the best guess
        return time.ticks_ms()


def _handle(peer, msg, relays, source=None, arrival=None):
    """Check one received packet: print it and queue its relay if it is new."""
    global rx_time, rx_from
    if len(msg) < HEADER_LEN:
        stats["ignored"] += 1
        return
    magic, version, ptype, ttl, hops, origin, seq, length = struct.unpack_from(_HEADER, msg)
    if magic != MAGIC or version != VERSION or len(msg) != HEADER_LEN + length:
        stats["ignored"] += 1
        return
    key = bytes(msg[6:14])  # origin + seq
    if origin == _mac or key in _seen:  # our own coming back, or a copy already handled
        stats["dup"] += 1
        return
    if len(_seen) >= _SEEN_MAX:
        _seen.clear()  # flood guard: at worst a few packets get relayed twice
    now = time.ticks_ms()
    _seen[key] = now
    _nodes[origin] = now
    stats["heard"] += 1
    if PRINT_PACKETS and ptype not in quiet:
        _print_packet(ptype, origin, seq, hops, peer, msg[HEADER_LEN:])
    rx_time, rx_from = (time.ticks_ms() if arrival is None else arrival), peer
    for fn in listeners:
        try:
            fn(ptype, origin, seq, hops, msg[HEADER_LEN:])
        except Exception as ex:
            stats["errors"] += 1
            _report("listener failed", ex)
    if ttl > 0:
        pkt = bytearray(msg)
        pkt[4] = ttl - 1
        pkt[5] = min(hops + 1, 255)
        relays.append(pkt)
        _bridge(pkt, source)


def receive(msg, source=None, via="bridge"):
    """Handle a packet that arrived over a bridge, with the same rules as radio.

    source is that bridge's function in `bridges`, so the packet isn't sent back
    the way it came; via is the name to print, e.g. "mqtt".
    """
    relays = []
    _handle(via, msg, relays, source)
    for pkt in relays:
        if _transmit(pkt):
            stats["relayed"] += 1


def _forget_old():
    """Drop packets and nodes not heard in the last FORGET_S seconds."""
    now = time.ticks_ms()
    for table in (_seen, _nodes):
        for key in [k for k, t in table.items() if time.ticks_diff(now, t) > FORGET_S * 1000]:
            del table[key]


async def _receive_loop():
    while True:
        relays = []
        try:
            for _ in range(32):  # then yield, so a flood can't starve the badge
                peer, msg = _e.recv(0)  # 0: return at once when nothing is waiting
                if peer is None:
                    break
                _handle(peer, msg, relays, None, _arrival(peer))
            if relays:
                # Wait a random 0-31 ms so neighbours relaying the same packet
                # don't all transmit at the same instant and collide.
                await aio.sleep_ms(os.urandom(1)[0] & 31)
                for pkt in relays:
                    if _transmit(pkt):
                        stats["relayed"] += 1
        except Exception as ex:  # never let one bad packet stop the mesh
            stats["errors"] += 1
            _report("receive failed", ex)
        await aio.sleep_ms(20)


async def _heartbeat_loop():
    last_stats = time.ticks_ms()
    while True:
        try:
            if _wlan.active():  # if something turned the radio off on purpose, stay quiet
                _check_channel()
                send(struct.pack(">I", max(0, int(time.time() - _start_s))), HEARTBEAT)
            _forget_old()
            if time.ticks_diff(time.ticks_ms(), last_stats) >= STATS_S * 1000:
                last_stats = time.ticks_ms()
                print("mesh:", status())
        except Exception as ex:
            stats["errors"] += 1
            _report("heartbeat failed", ex)
        await aio.sleep(HEARTBEAT_S)


def node_count():
    """How many other nodes were heard in the last FORGET_S seconds."""
    return len(_nodes)


def status():
    """One line: channel, nodes heard lately, and packet counters."""
    if _e is None:
        return "off"
    return "ch %s, %d nodes, sent %d, heard %d, dup %d, relayed %d, ignored %d, errors %d" % (
        _channel, len(_nodes), stats["sent"], stats["heard"], stats["dup"],
        stats["relayed"], stats["ignored"], stats["errors"])


def start():
    """Start the mesh inside a running asyncio program. False if ESP-NOW can't start."""
    global _started
    if _started:
        return True
    if not _radio_up():
        return False
    aio.create_task(_receive_loop())
    aio.create_task(_heartbeat_loop())
    _started = True
    print("mesh: node %s, heartbeat every %d s, ttl %d" % (_hex(_mac), HEARTBEAT_S, TTL))
    return True


def run():
    """REPL: run only the mesh, in the foreground, until Ctrl-C."""
    global _started
    aio.new_event_loop()  # forget tasks left over from an interrupted main.py
    _started = False
    if start():
        aio.get_event_loop().run_forever()
