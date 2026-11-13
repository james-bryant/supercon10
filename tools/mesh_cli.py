r"""Mesh console: send and receive ESP-NOW mesh packets through the MQTT broker.

Connects to the same broker and topic as the badge bridges (lib/mesh_mqtt.py)
and speaks the mesh packet format of lib/espnow_mesh.py. The laptop is one more
node on the broker side: bridges relay what it sends into their radios, and
every packet the bridges publish is listed here once, using the badges'
duplicate filter (origin + seq). Its own packets coming back from the broker
are dropped. Incoming packets print above the prompt, so typing isn't disturbed.

Run it in a terminal, or with PyCharm's "Mesh: console" run configuration:

    .venv\Scripts\python tools\mesh_cli.py
    .venv\Scripts\python tools\mesh_cli.py --heartbeat 30   # badges also count the laptop

The broker login comes from device/data/mqtt.json; --host, --port, --user,
--password, --topic and --tls override it.

Commands:
    send <text>          send a text message into the mesh
    nodes                nodes heard in the last minute
    heartbeats on|off    show or hide heartbeat packets
    help                 this list
    quit                 leave (Ctrl-D and Ctrl-C work too)
"""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import uuid
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.completion import NestedCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
except ImportError as ex:
    sys.exit("Missing %s. Install the tools: .venv\\Scripts\\pip install -r requirements.txt" % ex.name)

CONFIG = Path(__file__).resolve().parent.parent / "device" / "data" / "mqtt.json"
TOPIC = "sxmesh/packets"   # the bridges' default topic
FORGET_S = 60              # how long packets and nodes are remembered, as on the badges

# The mesh packet of lib/espnow_mesh.py (big-endian, 15-byte header):
# magic "SX", version 1, type, ttl, hops, origin MAC, seq, payload length.
HEADER = struct.Struct(">2sBBBB6sHB")
MAGIC, VERSION = b"SX", 1
HEARTBEAT, TEXT = 1, 2
MAX_PAYLOAD = 250 - HEADER.size

HELP = __doc__[__doc__.index("Commands:"):].rstrip()
STYLE = Style.from_dict({
    "dim": "#888888",
    "text": "bold",
    "me": "ansicyan",
    "info": "ansiyellow",
    "error": "ansired",
})


def make_packet(ptype, ttl, origin, seq, payload):
    return HEADER.pack(MAGIC, VERSION, ptype, ttl, 0, origin, seq, len(payload)) + payload


def parse_packet(data):
    """(type, ttl, hops, origin, seq, payload), or None if it isn't a mesh packet."""
    if len(data) < HEADER.size:
        return None
    magic, version, ptype, ttl, hops, origin, seq, length = HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or len(data) != HEADER.size + length:
        return None
    return ptype, ttl, hops, origin, seq, bytes(data[HEADER.size:])


def describe(ptype, payload):
    """(kind, detail) for printing one packet."""
    if ptype == HEARTBEAT and len(payload) == 4:
        return "heartbeat", "up %ds" % int.from_bytes(payload, "big")
    if ptype == TEXT:
        text = payload.decode("utf-8", "replace")
        # No control characters: a message must not be able to drive the terminal.
        return "text", "".join(c if c.isprintable() else "�" for c in text)
    return "type %d" % ptype, "%d bytes" % len(payload)


def laptop_id():
    """A steady 6-byte node ID for this laptop that doesn't give away its real MAC."""
    node = uuid.getnode()
    # getnode() makes up a random multicast MAC when it can't read one; use the host name then.
    seed = socket.gethostname().encode() if node >> 40 & 1 else node.to_bytes(6, "big")
    digest = hashlib.sha256(b"sxmesh" + seed).digest()
    return bytes([digest[0] & 0xFC | 0x02]) + digest[1:6]  # locally administered, unicast


def mac(b):
    return b.hex(":")


def stamp():
    return time.strftime("%H:%M:%S")


class MeshConsole:
    def __init__(self, cfg, origin, ttl):
        self.cfg, self.origin, self.ttl = cfg, origin, ttl
        self.topic = cfg["topic"]
        self.seq = int.from_bytes(os.urandom(2), "big")  # random start, like the badges
        self.seen = {}      # (origin, seq) -> when first heard: the duplicate filter
        self.nodes = {}     # origin -> (when last heard, hops)
        self.show_heartbeats = True
        self.connected = False
        self.started = time.monotonic()
        self.loop = None
        self._last_status = None
        client_id = "sxmesh-cli-%s-%s" % (origin.hex(), os.urandom(2).hex())
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if cfg.get("user"):
            self.client.username_pw_set(cfg["user"], cfg.get("password"))
        if cfg.get("tls"):
            # Like the bridges: encrypted, but the broker's certificate isn't checked.
            self.client.tls_set(cert_reqs=ssl.CERT_NONE)
            self.client.tls_insecure_set(True)
        self.client.reconnect_delay_set(1, 30)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # paho calls these from its network thread; the work happens in the prompt's thread.
    def _later(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self._later(self.status, "class:error", "broker refused the connection: %s" % reason)
            return
        client.subscribe(self.topic, qos=0)
        self._later(self._up)

    def _on_connect_fail(self, client, userdata):
        self._later(self.status, "class:error",
                    "can't reach %s:%d, retrying" % (self.cfg["host"], self.cfg["port"]))

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        self._later(self._down, str(reason))

    def _on_message(self, client, userdata, msg):
        self._later(self.receive, bytes(msg.payload), msg.retain)

    def say(self, *parts):
        """Print above the prompt, leaving what's being typed alone."""
        print_formatted_text(FormattedText(list(parts)), style=STYLE)

    def status(self, style, text):
        """Connection news, once rather than on every retry."""
        if text != self._last_status:
            self._last_status = text
            self.say((style, text))

    def _up(self):
        self.connected = True
        self.status("class:info", "connected to %s:%d, topic %s; this laptop is %s" % (
            self.cfg["host"], self.cfg["port"], self.topic, mac(self.origin)))

    def _down(self, reason):
        was, self.connected = self.connected, False
        if was:
            self.status("class:error", "lost the broker (%s), reconnecting" % reason)

    def receive(self, data, retained=False):
        """One message from the broker, handled with the badges' rules."""
        if retained:
            return  # an old message the broker replays to newcomers
        pkt = parse_packet(data)
        if pkt is None:
            self.say(("class:dim", "%s  ignored %d bytes that aren't a mesh packet" % (stamp(), len(data))))
            return
        ptype, ttl, hops, origin, seq, payload = pkt
        now = time.monotonic()
        for key in [k for k, t in self.seen.items() if now - t > FORGET_S]:
            del self.seen[key]
        if origin == self.origin or (origin, seq) in self.seen:
            return  # our own packet coming back, or a copy already shown
        self.seen[(origin, seq)] = now
        self.nodes[origin] = (now, hops)
        if ptype == HEARTBEAT and not self.show_heartbeats:
            return
        kind, detail = describe(ptype, payload)
        self.say(("class:dim", stamp() + "  "),
                 ("class:text" if ptype == TEXT else "class:dim",
                  "%-9s from %s  seq %-5d hops %d  %s" % (kind, mac(origin), seq, hops, detail)))

    def send(self, ptype, payload):
        """Publish one new packet. False if the broker isn't connected."""
        if not self.connected:
            return False
        self.seq = (self.seq + 1) & 0xFFFF
        pkt = make_packet(ptype, self.ttl, self.origin, self.seq, payload)
        return self.client.publish(self.topic, pkt, qos=0).rc == mqtt.MQTT_ERR_SUCCESS

    async def heartbeat(self, every):
        while True:
            self.send(HEARTBEAT, int(time.monotonic() - self.started).to_bytes(4, "big"))
            await asyncio.sleep(every)

    def command(self, line):
        """Handle one typed line. False means quit."""
        cmd, _, rest = line.strip().partition(" ")
        rest = rest.strip()
        if not cmd:
            pass
        elif cmd == "send":
            data = rest.encode()
            if not data:
                self.say(("class:error", "usage: send <text>"))
            elif len(data) > MAX_PAYLOAD:
                self.say(("class:error", "too long: %d bytes, the most is %d" % (len(data), MAX_PAYLOAD)))
            elif self.send(TEXT, data):
                self.say(("class:dim", stamp() + "  "), ("class:me", "sent      seq %-5d %s" % (self.seq, rest)))
            else:
                self.say(("class:error", "not connected to the broker, nothing sent"))
        elif cmd == "nodes":
            now = time.monotonic()
            heard = sorted((now - t, o, h) for o, (t, h) in self.nodes.items() if now - t <= FORGET_S)
            if not heard:
                self.say(("class:info", "no nodes heard in the last %d s" % FORGET_S))
            for age, origin, hops in heard:
                self.say(("", "  %s  heard %2ds ago  hops %d" % (mac(origin), age, hops)))
        elif cmd == "heartbeats" and rest in ("on", "off"):
            self.show_heartbeats = rest == "on"
            self.say(("class:info", "heartbeats %s" % ("shown" if self.show_heartbeats else "hidden")))
        elif cmd in ("help", "?"):
            self.say(("", HELP))
        elif cmd in ("quit", "exit"):
            return False
        else:
            self.say(("class:error", "unknown command %r, try help" % cmd))
        return True


async def run(console, heartbeat_s):
    console.loop = asyncio.get_running_loop()
    console.client.connect_async(console.cfg["host"], console.cfg["port"], keepalive=30)
    console.client.loop_start()
    beat = asyncio.ensure_future(console.heartbeat(heartbeat_s)) if heartbeat_s else None
    session = PromptSession(completer=NestedCompleter.from_nested_dict({
        "send": None, "nodes": None, "heartbeats": {"on": None, "off": None},
        "help": None, "quit": None}))
    console.say(("class:info", "connecting to %s:%d... type help for commands" % (
        console.cfg["host"], console.cfg["port"])))
    try:
        with patch_stdout():
            while True:
                try:
                    line = await session.prompt_async("mesh> ")
                except (EOFError, KeyboardInterrupt):
                    break
                if not console.command(line):
                    break
    finally:
        if beat:
            beat.cancel()
        console.client.disconnect()
        console.client.loop_stop()


def load_config(args):
    path = Path(args.config) if args.config else CONFIG
    cfg = {}
    if path.exists():
        cfg = json.loads(path.read_text())
    elif args.config:
        sys.exit("Can't read %s" % path)
    for key in ("host", "port", "user", "password", "topic"):
        if getattr(args, key) is not None:
            cfg[key] = getattr(args, key)
    if args.tls:
        cfg["tls"] = True
    if not cfg.get("host"):
        sys.exit("No broker: put its details in %s or pass --host" % CONFIG)
    cfg["port"] = int(cfg.get("port") or (8883 if cfg.get("tls") else 1883))
    cfg.setdefault("topic", TOPIC)
    return cfg


def main():
    ap = argparse.ArgumentParser(
        description="Send and receive ESP-NOW mesh packets through the MQTT broker.",
        epilog="At the prompt: send <text>, nodes, heartbeats on|off, help, quit.")
    ap.add_argument("--config", help="broker settings (default: device/data/mqtt.json)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--topic", help="default " + TOPIC)
    ap.add_argument("--tls", action="store_true", help="encrypt the connection (port 8883 unless --port)")
    ap.add_argument("--ttl", type=int, default=3, choices=range(256), metavar="N",
                    help="relays a sent packet may take (default 3, as on the badges)")
    ap.add_argument("--heartbeat", type=float, default=0, metavar="SECONDS",
                    help="also send a heartbeat this often, so badges count the laptop")
    ap.add_argument("--id", help="this laptop's node ID, e.g. 02:12:34:56:78:9a "
                                 "(default: derived from this machine)")
    args = ap.parse_args()
    cfg = load_config(args)
    origin = bytes.fromhex(args.id.replace(":", "")) if args.id else laptop_id()
    if len(origin) != 6:
        sys.exit("--id must be 6 bytes, like 02:12:34:56:78:9a")
    try:
        asyncio.run(run(MeshConsole(cfg, origin, args.ttl), args.heartbeat))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
