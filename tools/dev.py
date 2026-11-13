#!/usr/bin/env python3
"""Device helper for the supercon10 MicroPython project.

Mirrors the ESP32's filesystem into ./device and pushes changes back, using
mpremote's serial transport so each command is a single connection.

    python tools/dev.py info            board, MicroPython version, free space
    python tools/dev.py ls              list every file on the board
    python tools/dev.py pull            snapshot board -> backups/<time>/, then sync into device/
    python tools/dev.py push [paths]    copy changed files from device/ to the board
    python tools/dev.py restart         hard reset (boot.py + main.py run) and attach the REPL
    python tools/dev.py repl            attach the REPL (Ctrl-] or Ctrl-X to leave)
    python tools/dev.py flash-backup    full flash image via esptool (firmware + filesystem)

The serial port defaults to COM6. Override with --port COMx or the MPY_PORT
environment variable. Close any other program holding the port first
(the MicroPython Tools plugin, a serial monitor, Thonny...).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import filecmp
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEVICE_DIR = ROOT / "device"
BACKUP_DIR = ROOT / "backups"
DEFAULT_PORT = os.environ.get("MPY_PORT", "COM10")

# Never pushed to the board (any path component starting with "." is skipped too).
PUSH_IGNORE_NAMES = {"__pycache__", "Thumbs.db", "desktop.ini"}
PUSH_IGNORE_SUFFIXES = {".pyc", ".pyi"}

READ_CHUNK = 1024  # bytes per round trip when downloading

INFO_CODE = r"""
import sys, os, gc
u = os.uname()
v = sys.implementation.version
print('machine=' + getattr(sys.implementation, '_machine', u.machine))
print('version=%d.%d.%d' % (v[0], v[1], v[2]))
print('release=' + u.release)
print('build=' + u.version)
try:
    import esp
    print('flash_bytes=%d' % esp.flash_size())
except Exception:
    pass
try:
    s = os.statvfs('/')
    print('fs_total=%d' % (s[0] * s[2]))
    print('fs_free=%d' % (s[0] * s[3]))
except Exception:
    pass
gc.collect()
print('mem_free=%d' % gc.mem_free())
print('sys_path=' + ' '.join(sys.path))
"""


# --------------------------------------------------------------------------- helpers


def human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _import_mpremote():
    try:
        from mpremote.transport import TransportError
        from mpremote.transport_serial import SerialTransport
    except ImportError:
        sys.exit(
            "mpremote isn't installed for this interpreter.\n"
            "Run setup.cmd, or: python -m pip install -r requirements.txt"
        )
    return SerialTransport, TransportError


class Device:
    """Context manager: open the port and sit in the raw REPL for the duration."""

    def __init__(self, port: str, soft_reset: bool = True):
        self.port = port
        self.soft_reset = soft_reset
        self.t = None

    def __enter__(self):
        SerialTransport, TransportError = _import_mpremote()
        try:
            self.t = SerialTransport(self.port, baudrate=115200)
        except TransportError:
            sys.exit(
                f"Couldn't open {self.port}. Is the board plugged in, and is the port free?\n"
                "Disconnect the MicroPython Tools plugin / any serial monitor and try again."
            )
        try:
            self.t.enter_raw_repl(soft_reset=self.soft_reset)
        except TransportError:
            self.t.close()
            sys.exit(
                f"Opened {self.port} but couldn't get a MicroPython prompt.\n"
                "If boot.py takes a long time (Wi-Fi etc.) retry with --no-soft-reset. If main.py\n"
                "disables Ctrl-C, hold the board's BOOT/user button or reflash to regain access."
            )
        return self.t

    def __exit__(self, *exc):
        if self.t is not None:
            try:
                self.t.exit_raw_repl()
            except Exception:
                pass
            self.t.close()
        return False


def walk_device(t, path: str = "/", exclude: tuple[str, ...] = ()):
    """Return (files, dirs) where files is [(device_path, size)] and dirs is [device_path]."""
    files: list[tuple[str, int]] = []
    dirs: list[str] = []
    for entry in sorted(t.fs_listdir(path), key=lambda e: e.name):
        full = path.rstrip("/") + "/" + entry.name
        if any(full == ex or full.startswith(ex.rstrip("/") + "/") for ex in exclude):
            continue
        if entry.st_mode & 0x4000:
            dirs.append(full)
            sub_files, sub_dirs = walk_device(t, full, exclude)
            files += sub_files
            dirs += sub_dirs
        else:
            files.append((full, entry.st_size))
    return files, dirs


def local_path(base: Path, device_path: str) -> Path:
    return base.joinpath(*[p for p in device_path.split("/") if p])


def device_path_for(local: Path) -> str:
    return "/" + "/".join(local.relative_to(DEVICE_DIR).parts)


def is_ignored(local: Path) -> bool:
    parts = local.relative_to(DEVICE_DIR).parts
    if any(p.startswith(".") or p in PUSH_IGNORE_NAMES for p in parts):
        return True
    return local.suffix.lower() in PUSH_IGNORE_SUFFIXES


def run_mpremote(port: str, *args: str) -> int:
    _import_mpremote()
    return subprocess.call([sys.executable, "-m", "mpremote", "connect", port, *args])


def wait_for_port(port: str, timeout: float = 10.0) -> None:
    """After a hard reset, native-USB boards drop off the bus briefly. Wait for the port."""
    try:
        from serial.tools import list_ports
    except ImportError:
        time.sleep(2)
        return
    time.sleep(1.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(p.device.upper() == port.upper() for p in list_ports.comports()):
            time.sleep(0.5)
            return
        time.sleep(0.25)


# --------------------------------------------------------------------------- commands


def cmd_info(args) -> int:
    with Device(args.port, not args.no_soft_reset) as t:
        out = t.exec(INFO_CODE).decode("utf-8", "replace")
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    rows = [
        ("Board", info.get("machine")),
        ("MicroPython", f"{info.get('version')}  ({info.get('build')})"),
        ("Flash chip", human(int(info["flash_bytes"])) if "flash_bytes" in info else None),
        (
            "Filesystem",
            f"{human(int(info['fs_free']))} free of {human(int(info['fs_total']))}"
            if "fs_total" in info
            else None,
        ),
        ("Free RAM", human(int(info["mem_free"])) if "mem_free" in info else None),
        ("sys.path", info.get("sys_path")),
    ]
    for label, value in rows:
        if value:
            print(f"{label:<12} {value}")

    machine = (info.get("machine") or "").upper().replace("-", "")
    variant = "esp32_generic"
    for chip in ("ESP32S3", "ESP32S2", "ESP32C3", "ESP32C6"):
        if chip in machine:
            variant = "esp32_generic_" + chip[5:].lower()
            break
    major_minor = ".".join((info.get("version") or "0.0.0").split(".")[:2])
    print(
        f"\nLikely stubs for PyCharm: micropython-esp32-{variant}-stubs {major_minor}.x\n"
        "(pick them under Settings > Languages & Frameworks > MicroPython Tools)"
    )
    return 0


def cmd_ls(args) -> int:
    with Device(args.port, not args.no_soft_reset) as t:
        files, dirs = walk_device(t, "/", tuple(args.exclude))
    for path, size in files:
        print(f"{size:>10}  {path}")
    print(f"\n{len(files)} files, {len(dirs)} folders, {human(sum(s for _, s in files))}")
    return 0


def cmd_pull(args) -> int:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = BACKUP_DIR / stamp
    failed: list[str] = []

    with Device(args.port, not args.no_soft_reset) as t:
        files, dirs = walk_device(t, "/", tuple(args.exclude))
        total = sum(s for _, s in files)
        print(f"{len(files)} files ({human(total)}) in {len(dirs)} folders on {args.port}")
        snap.mkdir(parents=True, exist_ok=True)
        for d in dirs:
            local_path(snap, d).mkdir(parents=True, exist_ok=True)
        for i, (dpath, size) in enumerate(files, 1):
            print(f"  [{i}/{len(files)}] {dpath} ({human(size)})", flush=True)
            try:
                data = bytes(t.fs_readfile(dpath, chunk_size=READ_CHUNK))
                dest = local_path(snap, dpath)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            except Exception as e:  # keep going: one unreadable file shouldn't sink the backup
                print(f"     ! skipped: {e}")
                failed.append(dpath)

    print(f"\nSnapshot of the board saved to {snap.relative_to(ROOT)}")

    # Sync the snapshot into device/ without clobbering local edits unless --force.
    new, updated, kept, same = [], [], [], 0
    DEVICE_DIR.mkdir(exist_ok=True)
    for d in dirs:
        local_path(DEVICE_DIR, d).mkdir(parents=True, exist_ok=True)
    for dpath, _ in files:
        src = local_path(snap, dpath)
        if not src.exists():
            continue
        dst = local_path(DEVICE_DIR, dpath)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            new.append(dpath)
        elif filecmp.cmp(src, dst, shallow=False):
            same += 1
        elif args.force:
            shutil.copy2(src, dst)
            updated.append(dpath)
        else:
            kept.append(dpath)

    print(f"device/: {len(new)} new, {len(updated)} overwritten, {same} unchanged")
    if kept:
        print(
            f"\n{len(kept)} file(s) differ from the board and were left as-is in device/ "
            "(rerun with --force to overwrite):"
        )
        for p in kept:
            print("   ", p)
    if failed:
        print(f"\n{len(failed)} file(s) could not be read:")
        for p in failed:
            print("   ", p)
        return 1
    return 0


def _collect_push_files(paths: list[str]) -> list[Path]:
    targets = [Path(p) for p in paths] if paths else [DEVICE_DIR]
    out: list[Path] = []
    for target in targets:
        target = target.resolve()
        try:
            target.relative_to(DEVICE_DIR)
        except ValueError:
            sys.exit(f"{target} isn't inside {DEVICE_DIR}; only files under device/ go to the board.")
        if target.is_dir():
            out += [f for f in sorted(target.rglob("*")) if f.is_file() and not is_ignored(f)]
        elif target.is_file():
            out.append(target)  # explicitly named files are pushed even if they'd normally be ignored
        else:
            sys.exit(f"Not found: {target}")
    return out


def cmd_push(args) -> int:
    from mpremote.transport import TransportError

    files = _collect_push_files(args.paths)
    if not files:
        print("Nothing to push.")
        return 0

    sent = skipped = 0
    with Device(args.port, not args.no_soft_reset) as t:
        known_dirs = {""}
        for f in files:
            dpath = device_path_for(f)
            # make parent folders on the board
            parts = dpath.strip("/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/" + "/".join(parts[:i])
                if d not in known_dirs:
                    if not t.fs_isdir(d):
                        t.fs_mkdir(d)
                        print(f"  mkdir {d}")
                    known_dirs.add(d)

            data = f.read_bytes()
            if not args.force:
                try:
                    remote = t.fs_hashfile(dpath, "sha256")
                except (OSError, TransportError):
                    remote = None
                if remote == hashlib.sha256(data).digest():
                    skipped += 1
                    continue
            print(f"  -> {dpath} ({human(len(data))})", flush=True)
            t.fs_writefile(dpath, data)
            sent += 1

    print(f"Pushed {sent} file(s), {skipped} already up to date.")
    if args.restart:
        return cmd_restart(args)
    return 0


def cmd_restart(args) -> int:
    print(f"Resetting the board on {args.port}...")
    run_mpremote(args.port, "reset")
    wait_for_port(args.port)
    return cmd_repl(args)


def cmd_repl(args) -> int:
    print("Attaching REPL. Ctrl-C stops main.py, Ctrl-D soft-reboots, Ctrl-] or Ctrl-X leaves.")
    return run_mpremote(args.port, "repl")


def cmd_flash_backup(args) -> int:
    try:
        import esptool  # noqa: F401
    except ImportError:
        sys.exit("esptool isn't installed. Run: python -m pip install -r requirements.txt")
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"flash-{stamp}.bin"
    print(
        f"Reading the whole flash chip to {out.relative_to(ROOT)}. This takes a few minutes.\n"
        "If esptool can't connect, hold BOOT, tap RESET, release BOOT, and rerun."
    )
    rc = subprocess.call(
        [
            sys.executable, "-m", "esptool",
            "--port", args.port, "--baud", str(args.baud),
            "read-flash", "0", "ALL", str(out),
        ]
    )
    if rc == 0:
        print(
            f"\nSaved {out.name} ({human(out.stat().st_size)}). To restore everything later:\n"
            f"  python -m esptool --port {args.port} write-flash 0 {out.relative_to(ROOT)}"
        )
    else:
        print(
            "\nesptool couldn't read the flash. Boards using native USB need to be put in the\n"
            "ROM bootloader by hand: hold BOOT, tap RESET, release BOOT (or run\n"
            f"`python -m mpremote connect {args.port} bootloader`). The COM port number can change\n"
            "while it's in the bootloader; check Device Manager and pass --port if so."
        )
    return rc


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=DEFAULT_PORT, help=f"serial port (default {DEFAULT_PORT})")
    common.add_argument(
        "--no-soft-reset",
        action="store_true",
        help="don't soft-reset before talking to the board (use if boot.py is slow)",
    )

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", parents=[common], help="board and firmware details")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("ls", parents=[common], help="list files on the board")
    s.add_argument("--exclude", action="append", default=[], metavar="PATH", help="skip a device path, e.g. /sd")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("pull", parents=[common], help="copy the board's files into backups/ and device/")
    s.add_argument("--force", action="store_true", help="overwrite local files in device/ that differ")
    s.add_argument("--exclude", action="append", default=[], metavar="PATH", help="skip a device path, e.g. /sd")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("push", parents=[common], help="copy changed files from device/ to the board")
    s.add_argument("paths", nargs="*", help="files or folders under device/ (default: all of device/)")
    s.add_argument("--force", action="store_true", help="send files even if the board already has them")
    s.add_argument("--restart", action="store_true", help="hard reset and attach the REPL afterwards")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("restart", parents=[common], help="hard reset the board and attach the REPL")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("repl", parents=[common], help="attach the MicroPython REPL")
    s.set_defaults(func=cmd_repl)

    s = sub.add_parser("flash-backup", parents=[common], help="dump the full flash chip with esptool")
    s.add_argument("--baud", type=int, default=460800)
    s.set_defaults(func=cmd_flash_backup)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
