"""Switch the running badge to a named app over USB, e.g.:
    python launch_app.py Nametag [COM7]
Interrupts main.py, then restarts its asyncio tasks with the app already active.
"""
import serial, sys, time

APP = sys.argv[1] if len(sys.argv) > 1 else "Nametag"
PORT = sys.argv[2] if len(sys.argv) > 2 else "COM7"

CODE = """
import asyncio as aio, micropython
from lib.app_switcher import switcher
from lib.button import scan
from lib.epd import display
from lib.led_bar import leds
names = [a.name for a in switcher.apps]
print('apps:', names)
idx = names.index(%r)
switcher.active_idx = switcher.selection_idx = idx
switcher.selecting_mode = False
switcher.apps[idx].launched = True
leds.clear_sequence(); leds.fill((0, 0, 0)); leds.write()
aio.new_event_loop()
async def _main():
    aio.create_task(scan())
    aio.create_task(display.run())
    aio.create_task(leds.update())
    aio.create_task(switcher.arun())
    print('launched', names[idx])
    while True:
        await aio.sleep(60)
        print('Main 60s heartbeat --^v--^v--')
        micropython.mem_info()
aio.run(_main())
""" % APP

s = serial.Serial(PORT, 115200, timeout=0.3)
s.write(b"\x03\x03"); time.sleep(0.6); s.read(65536)
s.write(b"\x01"); time.sleep(0.5); s.read(65536)
s.write(CODE.encode() + b"\x04")
out = b""
end = time.time() + 45
while time.time() < end:
    out += s.read(4096)
    if b"Traceback" in out or (b"Done loading png" in out and time.time() > end - 40):
        time.sleep(3); out += s.read(65536)
        break
print(out.decode(errors="replace"))
s.close()

