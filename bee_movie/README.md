# Bee Movie on the 128x64 LCD

This board’s flash is only a couple MB (`flash_dump.bin` is 2,097,152 bytes), so fitting the whole Bee Movie *on-device* isn’t realistic. The approach here is **streaming**: the laptop decodes/downscales/dithers frames and sends **1024 bytes/frame** over USB serial; the board blits them to the ST7567 LCD.

To keep the MicroPython REPL usable (Ctrl-C works) the host sends frames as **base64 lines**. The device decodes each line into a 1024-byte frame and displays it.

You can also send simple commands on the same serial port (lines starting with `!`), e.g. `!contrast 40` or `!bl 0.3`. If you run the streamer with `--interactive`, lines starting with `@` adjust host-side video processing live (e.g. `@gamma 1.2`, `@dither fs`).

## What you need

- `ffmpeg` on your host
- Python deps on your host: `pyserial`, `numpy`
- The device in **BOOTSEL** mode once (to flash MicroPython)

## 1) Flash MicroPython

1. Put the device in BOOTSEL mode (hold **BOOTSEL**, tap **EN/reset**, or plug in while holding BOOTSEL).
2. Copy a **MicroPython UF2 for Pico 2 / RP2350** onto the BOOT drive.
3. (Optional) To restore the original CTF firmware later: copy `flash_dump.uf2` in BOOTSEL mode.

## 2) Upload the receiver code to the board

After MicroPython boots, it will expose a USB serial port.

1. Find the port (macOS example):
   - `python3 bee_movie/upload_micropython.py --list`
2. Upload:
   - `python3 bee_movie/upload_micropython.py --port /dev/cu.usbmodemXXXX`

That writes `main.py` and `st7567.py` to the device and resets it.

## 3) Stream the movie

Run:

- `python3 bee_movie/stream_bee_movie.py --port /dev/cu.usbmodemXXXX --fps 15`

For a “go hard” 60fps attempt:

- `python3 bee_movie/stream_bee_movie.py --port /dev/cu.usbmodemXXXX --fps 60 --drop-frames --scale-flags lanczos --dither bayer`

Useful flags:

- `--preview` to launch `ffplay` when the first frame is sent (best-effort sync)
- `--preview-mute` to mute preview audio
- `--preview-offset -0.25` to nudge preview start (seconds)
- `--frames 300` to test ~20s at 15fps
- `--invert` if black/white are swapped
- `--rotate180` if the image is upside down
- `--mode fit` to letterbox instead of cropping
- `--seek 120` to start 2 minutes in
- `--lcd-contrast 40` to tune LCD drive (0..63)
- `--backlight 30` to dim the backlight (percent)
- `--gamma 1.2 --contrast 1.2 --brightness -10` to tune image levels before dithering
- `--dither fs` for Floyd–Steinberg dithering (often nicer than Bayer, but can shimmer)
- `--dither atkinson` for a softer diffusion that often looks less noisy
- `--drop-frames` to keep real-time speed if your host can’t keep up
- `--interactive` to type commands while streaming: device (`!contrast 40`, `!bl 0.3`) and host (`@gamma 1.2`, `@dither fs`, `@reset`)

## If the screen is blank or shifted

Quick sanity check (should visibly blink):

- `python3 bee_movie/blink_test.py --port /dev/cu.usbmodemXXXX`
  - Try `--lcd-contrast 40` and/or `--backlight 30` if it’s washed out.

Edit `bee_movie/pico/main.py`:

- `CONTRAST`: try `0x10`..`0x3F`
- `COL_OFFSET`: this board’s Zephyr config uses `0` (but `2` is common on some modules)
- `PIN_BL`: backlight is on PWM `GPIO6` (the receiver enables it automatically)

The defaults are based on the original Zephyr firmware configuration found in `flash_dump.bin` (ST7567 over SPI, DC=GPIO9, RST=GPIO8, CS=GPIO13, backlight PWM on GPIO6).

## On-device tuning (buttons)

While streaming:

- **UP/DOWN**: LCD contrast up/down
- **BACK/BOOTSEL**: backlight down/up
- **OK**: toggle LCD invert
- **OK + UP/DOWN**: regulation ratio up/down (deep contrast tuning)
- **OK + BACK**: toggle bias (1/7 vs 1/9)

## D2/D3 LEDs (optional)

The receiver will try to use D2 (green) and D3 (yellow) as a simple “fps OK / not OK” indicator. If they don’t light, set pins interactively:

- `!led2 <gpio> [active_low]` (default active_low = 1)
- `!led3 <gpio> [active_low]`
- `!targetfps 60`

To discover the GPIOs, probe candidate pins (watch for a blink on the LEDs at the top of the board):

- `python3 bee_movie/led_probe.py --port /dev/cu.usbmodemXXXX`

Manual probing is also available:

- `!probe <gpio> [active_low] [ms]`
