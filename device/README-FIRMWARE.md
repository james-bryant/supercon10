# Billboard Badge — MicroPython firmware (ESP32-S3 + Pervasive Displays dual-COG eink)
Captured from a running badge 2026-11-06 (MicroPython v1.20.0-2527.g44aa2b153, board
"Billboard Badge Generic ESP32S3 module with Octal-SPIRAM").

## Layout
- boot.py / main.py — asyncio app entry (app switcher + 1ms button poll + 60s heartbeat)
- lib/ — board config, epd.py (the display driver: dual-COG PDI panel, 12MHz SPI,
  volatile fast LUTs + anti-ghost cleanup passes, lvgl integration, viper fast paths),
  buttons (GPIO 39/40/15/16 + BOOT=0, active-low, pull-ups), PNG decode, app framework
- apps/ — gallery, nametag, schedule + template for new apps
- data/ — rendered_luts.py (the fast/cleanup LUT tables), panel_config.json, schedule.csv

## Known gap in this capture
- /images/*.png (the gallery art, ~44 files) not included — pull from a running badge
  via raw REPL if wanted; all CODE is complete.

## Note
This tree includes the modified display driver (multi-fast-pass + white anti-ghosting
passes) as it was running at capture time.

## Upstream
Merged with github.com/Hack-a-Day/2026-Billboard_Badge at 5945518 (2026-11-08):
gray and mixed display modes (lib/epd_grayscale.py, display.refresh_lvgl()),
opt-in smooth schedule text (lib/smooth_text.py, lib/binfont.py, data/fonts),
an opt-in partial refresh (lib/epd_partial.py), an opt-in BLE presence
broadcast (lib/presence.py), the soft-reset guard in main.py, and upstream's
fixes for badges whose displays don't update (EPD power-enable pin, B1 booster
setting, BUSY handling). The driver keeps this badge's fast LUT refresh:
display.update() still takes slow / y_start / y_end / fast_passes, and also
upstream's grayscale / mixed.
