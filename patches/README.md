# Badge mesh patches

Patches for [github.com/Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge),
checked against `main` at 5945518 (2026-11-08). They also work on badges still
running the code they shipped with.

| Patch | What it adds | Which badges |
|---|---|---|
| `espnow-mesh.patch` | the ESP-NOW mesh: heartbeats, relaying, duplicate filter | every badge |
| `light-show.patch` | a clock the badges share, and the "Light show" app | every badge |
| `mqtt-bridge.patch` | the bridge between the mesh and the MQTT broker | bridges only |

Leave out `mesh-app.patch`.

## Every badge

Apply the two patches to a fresh clone (`--ignore-whitespace` is for Git on
Windows, which checks files out with CRLF line endings; it's harmless elsewhere):

    git clone https://github.com/Hack-a-Day/2026-Billboard_Badge
    cd 2026-Billboard_Badge
    git apply --ignore-whitespace --directory=firmware/badge path/to/espnow-mesh.patch path/to/light-show.patch

Copy the four changed files to each badge, here with mpremote
(`pip install mpremote`), run from `firmware/badge`:

    mpremote cp main.py :main.py
    mpremote cp lib/espnow_mesh.py :lib/espnow_mesh.py
    mpremote cp lib/mesh_sync.py :lib/mesh_sync.py
    mpremote cp apps/lights.py :apps/lights.py
    mpremote reset

## Bridges

In the same clone, after the steps above:

    git apply --ignore-whitespace --directory=firmware/badge path/to/mqtt-bridge.patch

On each bridge, copy `main.py` again (it now starts the bridge too) and the
bridge itself:

    mpremote cp main.py :main.py
    mpremote cp lib/mesh_mqtt.py :lib/mesh_mqtt.py

Each bridge also needs two files of its own. The broker login isn't stored
here; get it from whoever sent you these patches.

`data/mqtt.json`:

    {"host": "50.17.58.122", "port": 1883, "user": "<broker user>", "password": "<broker password>"}

`data/wifi.json`:

    {"ssid": "<network>", "password": "<password>"}

    mpremote cp mqtt.json :data/mqtt.json
    mpremote cp wifi.json :data/wifi.json
    mpremote reset

Bridges only join an access point on channel 1, the mesh's channel: badges off
Wi-Fi run the mesh on channel 1, and a badge on Wi-Fi uses its access point's
channel. If the network has access points on several channels, a bridge picks
the strongest one on channel 1. With none in range, it stays off Wi-Fi as an
ordinary mesh badge and looks again every 30 seconds, and if its access point
switches channels later, the bridge leaves it. So set at least one access point
near the bridges to channel 1, not to automatic.

Don't copy the bridge's `main.py` to the other badges: it starts the bridge,
and a badge without `lib/mesh_mqtt.py` won't boot.

## Checking

Watch a badge's console with `mpremote repl` (Ctrl-] or Ctrl-X leaves):

- every badge: `mesh: on channel 1`, then `mesh: heartbeat from ...` every few
  seconds for each badge in range
- bridges: `wifi: joining <network> at <access point> on channel 1`, then
  `mqtt: connected to 50.17.58.122:1883 ...`. If a bridge prints
  `wifi: no <network> access point on channel 1, only on 6`, no access point in
  its range is on channel 1.
- open "Light show" from the menu on two badges: within a few seconds they
  animate together, and UserA / UserB change the pattern on all of them
