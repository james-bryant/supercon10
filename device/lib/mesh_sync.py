"""Shared clock over the ESP-NOW mesh, so badges can animate in step.

Each badge that joins (the Light show app does) broadcasts a small SYNC packet
to its radio neighbours every SYNC_MS. It goes out with ttl 0, so nobody relays
it and no bridge carries it: MQTT is far too slow for timing.

The badge with the lowest MAC leads, and its clock is the shared clock. The
leader numbers its SYNC packets; a badge takes the time from whichever
neighbour first brings it a newer number, then passes that time on in its own
SYNC packets, so the clock spreads hop by hop with a millisecond or two of error
per hop. Arrival times come from the radio driver, so a badge that is busy
(drawing its screen, say) reads packets late but still times them right. Small
corrections are eased in; a badge joining a group jumps straight to its clock.
If no newer number arrives for ROOT_TIMEOUT_MS the leader is gone, and the next
lowest MAC takes over without the clock jumping. Badges then ignore the old
leader's last packets still echoing around, unless it really comes back. When
two shows meet, the one whose leader has the lower MAC wins, pattern included.

The group also agrees on a light pattern: whoever changed it most recently, by
the shared clock, wins, and the choice spreads the same way.

    now()            the shared clock in ms (wraps at 2**32)
    pattern()        the group's pattern number
    set_pattern(n)   choose the pattern for the whole group
    status()         "leading" or "following aa:bb:cc, 2 hops"
"""

import asyncio as aio
import binascii
import os
import struct
import time

import network

import lib.espnow_mesh as mesh

SYNC = 3                  # mesh packet type
SYNC_MS = 2000            # how often each badge tells its neighbours the time
ROOT_TIMEOUT_MS = 9000    # no newer leader packet for this long: the leader is gone
JUMP_MS = 100             # clock differences beyond this jump; smaller ones are eased in
LATENCY_MS = 1            # from stamping a SYNC packet to the neighbour's radio receiving it

# leader MAC, leader's packet number, hops from the leader, time, pattern, when it was chosen
_SYNC = ">6sHBIBI"        # (MicroPython's struct has no Struct class)
_SYNC_LEN = struct.calcsize(_SYNC)
_WRAP = 0xFFFFFFFF

_me = b""
_root = b""               # the leader this badge follows; itself while leading
_root_seq = int.from_bytes(os.urandom(2), "big")
_depth = 0                # hops from the leader
_offset = 0               # shared clock = local clock + _offset, mod 2**32
_heard = 0                # local ms when the leader's clock last reached this badge
_pattern = 0
_chosen = 0               # shared ms when the pattern was chosen
_announce_at = 0          # local ms of the next SYNC packet
_gone = {}                # leaders that went quiet: MAC -> (last packet number, ignore until)
_started = False
_local_ms = 0             # local clock: ticks_ms without the wrap
_last_tick = time.ticks_ms()


def _local():
    global _local_ms, _last_tick
    t = time.ticks_ms()
    _local_ms += time.ticks_diff(t, _last_tick)
    _last_tick = t
    return _local_ms


def _diff(a, b):
    """a - b for times that wrap at 2**32, as a signed number."""
    return ((a - b + 0x80000000) & _WRAP) - 0x80000000


def _short(mac):
    return binascii.hexlify(mac[3:], ":").decode()


def now():
    """The shared clock, in ms."""
    return (_local() + _offset) & _WRAP


def pattern():
    return _pattern


def set_pattern(n):
    """Choose the pattern for the whole group; the neighbours hear right away."""
    global _pattern, _chosen, _announce_at
    _pattern, _chosen, _announce_at = n & 0xFF, now(), _local()


def status():
    if _root == _me:
        return "leading"
    return "following %s, %d hop%s" % (_short(_root), _depth, "" if _depth == 1 else "s")


def _on_packet(ptype, origin, seq, hops, payload):
    """Mesh listener: take the time and the pattern from a neighbour's SYNC packet."""
    global _root, _root_seq, _depth, _offset, _heard, _pattern, _chosen, _announce_at
    # Only SYNC packets straight from a radio neighbour carry usable timing.
    if ptype != SYNC or hops or not isinstance(mesh.rx_from, bytes) or len(payload) != _SYNC_LEN:
        return
    root, root_seq, depth, their_time, their_pattern, chosen = struct.unpack(_SYNC, payload)
    lt = _local()
    arrival = lt - time.ticks_diff(_last_tick, mesh.rx_time)  # on the local clock
    offset = (their_time + LATENCY_MS - arrival) & _WRAP
    newer = 0 < ((root_seq - _root_seq) & 0xFFFF) < 0x8000
    gone = _gone.get(root)
    if gone and (lt > gone[1] or 0 < ((root_seq - gone[0]) & 0xFFFF) < 0x8000):
        del _gone[root]  # long enough ago, or newer packets: it's really back
        gone = None
    if (root < _root and not gone) or (root == _root and root != _me and newer):
        joined = root != _root
        _root, _root_seq, _depth, _heard = root, root_seq, min(depth + 1, 255), lt
        step = _diff(offset, _offset)
        if abs(step) > JUMP_MS:
            _offset = offset
            _pattern, _chosen = their_pattern, chosen  # a different clock: take the group's pattern too
        else:
            _offset = (_offset + step // 2) & _WRAP    # ease small corrections in
        if joined:
            print("sync:", status())
            _announce_at = min(_announce_at, lt + 20 + os.urandom(1)[0] % 100)  # spread the news
    if root == _root and _diff(chosen, _chosen) > 0:
        _pattern, _chosen = their_pattern, chosen
        _announce_at = min(_announce_at, lt + 20 + os.urandom(1)[0] % 100)  # pass it on soon


def _announce():
    global _root_seq
    if _root == _me:
        _root_seq = (_root_seq + 1) & 0xFFFF
    mesh.send(struct.pack(_SYNC, _root, _root_seq, _depth, now(), _pattern, _chosen), SYNC, ttl=0)


async def _loop():
    global _root, _depth, _announce_at
    while True:
        lt = _local()
        if _root != _me and lt - _heard > ROOT_TIMEOUT_MS:
            # Don't let the old leader's last packets, still echoing around, win it back.
            _gone[_root] = (_root_seq, lt + 2 * ROOT_TIMEOUT_MS)
            _root, _depth = _me, 0  # keep the offset, so the clock carries on smoothly
            _announce_at = lt
            print("sync: leading (the previous leader went quiet)")
        for mac in [m for m, (_, until) in _gone.items() if lt > until]:
            del _gone[mac]
        if lt >= _announce_at:
            _announce()
            _announce_at = lt + SYNC_MS + os.urandom(1)[0] % 200  # jitter keeps badges apart
        await aio.sleep_ms(50)


def start():
    """Join the shared clock. False if ESP-NOW isn't available."""
    global _started, _me, _root
    if _started:
        return True
    if not mesh.start():
        return False
    _me = _root = network.WLAN(network.STA_IF).config("mac")
    mesh.quiet.add(SYNC)
    mesh.listeners.append(_on_packet)
    aio.create_task(_loop())
    _started = True
    print("sync: joined, leading until a lower MAC is heard")
    return True
