# supercon10

Working copy of the MicroPython code on the ESP32 badge (COM6), set up for PyCharm.

## Layout

| Path | What it is |
|---|---|
| `device/` | Mirror of the board's filesystem. `device/main.py` is `/main.py` on the board. |
| `tools/dev.py` | Pull / push / REPL helper built on mpremote. |
| `backups/` | Timestamped snapshot of the board from every pull, plus any full flash images. Not committed to git. |
| `patches/` | The mesh and light show as patches for badges running the stock firmware (see `patches/README.md`). |
| `assets/` | Source images for the Nametag rotation. |
| `.run/` | Shared PyCharm run configurations (Run menu, "Device" folder). |
| `setup.cmd` / `setup.ps1` | One-time setup: venv, tools, first pull, optional flash image, first git commit. |

## First-time setup

1. Close anything holding COM6 (MicroPython Tools plugin connection, Thonny, a serial monitor).
2. Double-click `setup.cmd` (or run `.\setup.cmd -Port COM7` for another port). It creates `.venv`, installs mpremote and esptool, copies the board's files into `device/`, offers to save a full flash image, and makes a first git commit of the untouched code.
3. In PyCharm: File > Open > this folder. Accept the `.venv` interpreter it detects (or set it to `.venv\Scripts\python.exe`).
4. Right-click `device/` > Mark Directory as > Sources Root, and do the same for `device/lib` if the board has one, so imports resolve the way they do on the board.
5. Settings > Languages & Frameworks > MicroPython Tools: enable it for this project, set the port to COM6, and install the stubs that `dev.py info` suggested (it prints the package name). Optionally mark `device/` as MicroPython Sources Root too, so the plugin's own uploads land at the board's root.

The "Device" run configurations in `.run/` show up in the Run menu automatically.

## Day to day

All commands take `--port COMx` (or set `MPY_PORT`). From PyCharm, use the matching "Device:" run configuration.

```
.venv\Scripts\python tools\dev.py info            # board, firmware version, free space
.venv\Scripts\python tools\dev.py ls              # list files on the board
.venv\Scripts\python tools\dev.py push            # send changed files in device/ (sha256 compare)
.venv\Scripts\python tools\dev.py push device\main.py
.venv\Scripts\python tools\dev.py push --restart  # push, hard reset, watch output in the REPL
.venv\Scripts\python tools\dev.py repl            # Ctrl-C stop, Ctrl-D soft reboot, Ctrl-] or Ctrl-X leave
.venv\Scripts\python tools\dev.py pull            # re-sync from the board; keeps local edits unless --force
.venv\Scripts\python tools\dev.py flash-backup    # full flash image to backups\flash-<time>.bin
```

Notes:

- `push` never deletes anything on the board. Remove files there with `python -m mpremote connect COM6 rm :/path`.
- Dotfiles, `__pycache__`, `.pyc` and `.pyi` files are never pushed.
- `pull` always writes a fresh snapshot to `backups/` first, so the board's version is never lost even when `device/` keeps your edits.
- The REPL configs run with "Emulate terminal" on. If keys misbehave in PyCharm's run window, run `dev.py repl` from PyCharm's Terminal tab instead.
- Only one program can hold the serial port. Disconnect the MicroPython Tools plugin before using `dev.py`, and vice versa.

## Nametag images

The Nametag app shows `nametag_band_v1` between each image in `/data/nametag` (name, image 1, name, image 2, ...). To change the rotation, add or remove PNGs in `assets/nametag`, then:

```
powershell -ExecutionPolicy Bypass -File tools\build_nametag_monochrome.ps1
.venv\Scripts\python tools\dev.py push
```

`name-v<N>-bw-source.png` becomes `device/data/graphics/nametag_band_v<N>.png` (thresholded, for crisp lettering); every other PNG becomes `device/data/nametag/<same name>.png` (dithered). Both are 960x336 1-bit. `push` never deletes, so remove a dropped image from the board by hand.

## Mesh, light show and MQTT bridge

`device/` adds an ESP-NOW mesh to the badge (`lib/espnow_mesh.py`): every badge broadcasts a heartbeat and relays what it hears, on Wi-Fi channel 1. On top of it:

- **Light show** (`apps/lights.py`): badges running it animate their LEDs in step, on a clock they share over the mesh (`lib/mesh_sync.py`). UserA / UserB change the pattern for the whole group.
- **Mesh** (`apps/mesh.py`): how many badges this one hears, and the latest text messages.
- **MQTT bridge** (`lib/mesh_mqtt.py`): a badge with `data/mqtt.json` joins Wi-Fi (only through an access point on channel 1) and links the mesh to an MQTT broker.

The rest of `device/` is merged with the official firmware, [Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge), at 5945518; `device/README-FIRMWARE.md` has the details.

From the laptop, `tools/mesh_cli.py` sends and receives mesh packets through the broker (or use the "Mesh: console" run configuration):

```
.venv\Scripts\python tools\mesh_cli.py
```

### Files kept out of git

Two files in `device/data/` hold logins, so `.gitignore` keeps them out of the repository. Create them by hand; `dev.py push` copies them to the badge like any other file.

| File | Contents | Used by |
|---|---|---|
| `device/data/wifi.json` | `{"ssid": "...", "password": "..."}` | badges that join Wi-Fi |
| `device/data/mqtt.json` | `{"host": "...", "port": 1883, "user": "...", "password": "..."}` | MQTT bridges and `tools/mesh_cli.py` |

## Restoring the original firmware

If a flash image was saved during setup:

```
.venv\Scripts\python -m esptool --port COM6 write-flash 0 backups\flash-<time>.bin
```

That puts back the exact MicroPython build and filesystem the badge shipped with. For just the code, the first git commit holds the untouched files.
