"""MQTT bridge for the ESP-NOW mesh in lib/espnow_mesh.py.

A badge with data/mqtt.json becomes a bridge: it joins Wi-Fi, connects to the
broker, publishes the packets it originates and the radio packets it relays,
and feeds packets from the broker into the mesh. Those go through the mesh's
duplicate filter and TTL like radio packets, so the broker is one more hop: a
bridge publishes a packet with ttl one lower, and other bridges relay it over
their radios only while ttl allows. Badges without data/mqtt.json don't bridge.

data/mqtt.json (only host is required):

    {"host": "broker.example.com", "port": 8883, "tls": true,
     "user": "badge", "password": "secret", "topic": "sxmesh/packets"}

port defaults to 8883 with tls, otherwise 1883. With tls the link is encrypted
but the broker's certificate isn't checked. If nothing else on the badge is
joining Wi-Fi, the bridge joins the network in data/wifi.json
({"ssid": "...", "password": "..."}), through an access point on the mesh's
channel only (espnow_mesh.join_wifi), so it can hear the badges off Wi-Fi.

On the broker, each mesh packet (format in espnow_mesh.py) is one raw binary
message on the topic, QoS 0. Every bridge publishes to the topic and subscribes
to it; retained messages are ignored.

main() starts it right after the mesh:

    import lib.mesh_mqtt as mesh_mqtt
    mesh_mqtt.start()
"""

import asyncio as aio
import binascii
import json
import struct
import time

import network

import lib.espnow_mesh as mesh

CONFIG_FILE = "data/mqtt.json"
WIFI_FILE = "data/wifi.json"
JOIN_EVERY_S = 30      # how often to look for the network while off Wi-Fi
TOPIC = "sxmesh/packets"   # used when data/mqtt.json doesn't name a topic
KEEPALIVE_S = 30           # the bridge pings the broker every KEEPALIVE_S / 2
OUTBOX_MAX = 32            # packets waiting for the broker; beyond this the oldest go
STATS_S = 60               # how often to print a summary line while connected

_REFUSED = ("", "unsupported protocol", "client id rejected", "server unavailable",
            "bad user name or password", "not authorized")
_IDLE = getattr(network, "STAT_IDLE", 1000)

_wlan = network.WLAN(network.STA_IF)
_cfg = {}
_topic = b""
_client_id = ""
_outbox = []
_connected = False
_started = False
_last_error = None
_wait_from = None       # when _wifi_ready() first ran (Wi-Fi head start)
stats = {"to_broker": 0, "from_broker": 0, "dropped": 0, "connects": 0}


def _report(text):
    """Print a problem once instead of on every retry."""
    global _last_error
    if text != _last_error:
        print("mqtt:", text)
        _last_error = text


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _str(s):
    """MQTT string: 2-byte length, then the bytes."""
    if isinstance(s, str):
        s = s.encode()
    return struct.pack(">H", len(s)) + s


def _packet(kind, body):
    """MQTT control packet: type byte, remaining length (7 bits per byte), body."""
    n, length = len(body), bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        length.append(byte | 0x80 if n else byte)
        if not n:
            return bytes([kind]) + length + body


async def _read_packet(reader):
    kind = (await reader.readexactly(1))[0]
    n = shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        n |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 21:
            raise ValueError("bad packet length from broker")
    return kind, (await reader.readexactly(n) if n else b"")


def _wifi_ready():
    """True on Wi-Fi. If nothing else is joining a network, join data/wifi.json's.

    Other Wi-Fi code (lib/wifi.py) gets a head start, and nobody may have tried
    to join in the last JOIN_EVERY_S: each try scans, which stalls the badge."""
    global _wait_from
    if _wlan.isconnected():
        return True
    now = time.ticks_ms()
    if _wait_from is None:
        _wait_from = now
    tried = mesh.wifi_tried_ms
    if (_wlan.status() == _IDLE and time.ticks_diff(now, _wait_from) >= 10000 and
            (tried is None or time.ticks_diff(now, tried) >= JOIN_EVERY_S * 1000)):
        cfg = _load(WIFI_FILE)
        if cfg and cfg.get("ssid"):
            _wlan.active(True)
            mesh.join_wifi(_wlan, cfg["ssid"], cfg.get("password", ""))
        else:
            _report("waiting for Wi-Fi (no %s to join)" % WIFI_FILE)
    return False


def _tls():
    try:
        import ssl
    except ImportError:  # builds without the ssl wrapper still have tls
        import tls as ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if hasattr(ctx, "check_hostname"):  # CPython (used for testing) needs this off first
        ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # encrypted, but the broker's certificate isn't checked
    return ctx


async def _session():
    """Connect, log in, subscribe, then pass packets both ways until the link drops."""
    global _connected, _last_error
    host, tls = _cfg["host"], bool(_cfg.get("tls"))
    port = int(_cfg.get("port") or (8883 if tls else 1883))
    reader, writer = await aio.wait_for(
        aio.open_connection(host, port, ssl=_tls() if tls else None), 30)
    sender = None
    try:
        flags, login = 0x02, b""  # clean session
        user, password = _cfg.get("user"), _cfg.get("password")
        if user:
            flags, login = flags | 0x80, _str(user)
            if password:
                flags, login = flags | 0x40, login + _str(password)
        writer.write(_packet(0x10, _str("MQTT") + bytes([4, flags]) +
                             struct.pack(">H", KEEPALIVE_S) + _str(_client_id) + login))
        await writer.drain()
        kind, body = await aio.wait_for(_read_packet(reader), 30)
        if kind != 0x20 or len(body) != 2:
            raise OSError("no CONNACK from the broker")
        if body[1]:
            raise OSError("broker refused login: %s" % (_REFUSED[body[1]] if body[1] < 6 else body[1]))
        writer.write(_packet(0x82, b"\x00\x01" + _str(_topic) + b"\x00"))  # SUBSCRIBE, QoS 0
        await writer.drain()
        _connected, _last_error = True, None
        stats["connects"] += 1
        print("mqtt: connected to %s:%d as %s, topic %s" % (host, port, _client_id, _topic.decode()))
        sender = aio.create_task(_send_loop(writer))
        while True:
            # The broker answers our pings, so this long without a packet means the link is dead.
            kind, body = await aio.wait_for(_read_packet(reader), 2 * KEEPALIVE_S)
            if kind & 0xF0 == 0x30:
                _on_publish(kind, body)
            elif kind == 0x90 and body[2:3] == b"\x80":
                raise OSError("broker refused the subscription to " + _topic.decode())
    finally:
        _connected = False
        _outbox.clear()
        if sender:
            sender.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _send_loop(writer):
    """Publish queued packets, ping the broker, and print a summary now and then."""
    last_ping = last_stats = time.ticks_ms()
    try:
        while True:
            while _outbox:
                writer.write(_packet(0x30, _str(_topic) + _outbox.pop(0)))  # PUBLISH, QoS 0
                stats["to_broker"] += 1
            now = time.ticks_ms()
            if time.ticks_diff(now, last_ping) >= KEEPALIVE_S * 500:
                writer.write(b"\xc0\x00")  # PINGREQ
                last_ping = now
            if time.ticks_diff(now, last_stats) >= STATS_S * 1000:
                print("mqtt:", status())
                last_stats = now
            await writer.drain()
            await aio.sleep_ms(50)
    except Exception as ex:  # the receive side then notices the dead link and reconnects
        _report("send failed - %r" % ex)


def _on_publish(kind, body):
    """A message from the broker: hand its mesh packet to the mesh."""
    if kind & 0x01:  # retained: an old message the broker replays to new subscribers
        return
    start = 2 + struct.unpack_from(">H", body)[0]  # skip the topic
    if kind & 0x06:  # QoS 1/2 add a packet id (not expected: we subscribe at QoS 0)
        start += 2
    stats["from_broker"] += 1
    mesh.receive(bytes(body[start:]), _to_mqtt, "mqtt")


def _to_mqtt(pkt):
    """Mesh bridge hook: queue a packet for the broker."""
    if not _connected:
        return  # the mesh is best-effort; don't pile up packets while offline
    if len(_outbox) >= OUTBOX_MAX:
        _outbox.pop(0)
        stats["dropped"] += 1
    _outbox.append(bytes(pkt))


async def _run():
    delay = 2
    while True:
        if not _wifi_ready():
            await aio.sleep(2)
            continue
        began, connects = time.ticks_ms(), stats["connects"]
        try:
            await _session()
        except Exception as ex:
            if stats["connects"] > connects:
                _report("lost the broker - %r" % ex)
            else:
                _report("can't connect to %s - %r" % (_cfg["host"], ex))
        if time.ticks_diff(time.ticks_ms(), began) > 60000:
            delay = 2  # the link was up for a while: retry soon
        await aio.sleep(delay)
        delay = min(delay * 2, 60)


def status():
    """One line: link state and message counters."""
    return "%s, sent %d, received %d, dropped %d" % (
        "connected" if _connected else "offline",
        stats["to_broker"], stats["from_broker"], stats["dropped"])


def start():
    """Bridge the mesh to MQTT if data/mqtt.json exists. False if not a bridge."""
    global _cfg, _topic, _client_id, _started
    if _started:
        return True
    cfg = _load(CONFIG_FILE)
    if not cfg or not cfg.get("host"):
        print("mqtt: no usable %s, not a bridge" % CONFIG_FILE)
        return False
    _cfg = cfg
    _topic = cfg.get("topic", TOPIC).encode()
    _client_id = cfg.get("client_id") or "sxmesh-" + binascii.hexlify(_wlan.config("mac")).decode()
    mesh.bridges.append(_to_mqtt)
    aio.create_task(_run())
    _started = True
    print("mqtt: bridging the mesh to %s" % cfg["host"])
    return True
