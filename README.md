# supercon10

Working copy of the MicroPython code on the ESP32 badge (COM6), set up for PyCharm.

## Layout

| Path | What it is |
|---|---|
| `device/` | Mirror of the board's filesystem. `device/main.py` is `/main.py` on the board. |
| `tools/dev.py` | Pull / push / REPL helper built on mpremote. |
| `backups/` | Timestamped snapshot of the board from every pull, plus any full flash images. Not committed to git. |
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

## Restoring the original firmware

If a flash image was saved during setup:

```
.venv\Scripts\python -m esptool --port COM6 write-flash 0 backups\flash-<time>.bin
```

That puts back the exact MicroPython build and filesystem the badge shipped with. For just the code, the first git commit holds the untouched files.
