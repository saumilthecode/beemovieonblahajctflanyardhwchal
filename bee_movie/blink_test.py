#!/usr/bin/env python3
import argparse
import base64
import time

import serial


FRAME_BYTES = 128 * (64 // 8)  # 1024


def main() -> int:
    ap = argparse.ArgumentParser(description="Send simple test patterns to the MicroPython ST7567 receiver.")
    ap.add_argument("--port", required=True, help="Serial port (e.g. /dev/cu.usbmodem1101)")
    ap.add_argument("--baud", type=int, default=115200, help="Baud rate (USB CDC ignores this)")
    ap.add_argument("--fps", type=float, default=2.0, help="Toggle rate for blink pattern")
    ap.add_argument("--seconds", type=float, default=10.0, help="How long to run")
    ap.add_argument("--lcd-contrast", type=int, default=-1, help="Set LCD contrast 0..63 before sending patterns")
    ap.add_argument(
        "--backlight",
        default="",
        help="Set backlight before patterns: 0..100 (percent), 0..65535 (duty), or 0.0..1.0 (ratio)",
    )
    ap.add_argument(
        "--pattern",
        choices=("blink", "black", "white"),
        default="blink",
        help="Pattern to send",
    )
    args = ap.parse_args()

    black = base64.b64encode(b"\x00" * FRAME_BYTES) + b"\n"
    white = base64.b64encode(b"\xFF" * FRAME_BYTES) + b"\n"

    with serial.Serial(args.port, args.baud, timeout=0.5, write_timeout=2.0) as ser:
        if args.lcd_contrast >= 0:
            ser.write(f"!contrast {args.lcd_contrast}\n".encode("utf-8"))
        if args.backlight:
            ser.write(f"!bl {args.backlight}\n".encode("utf-8"))
        deadline = time.time() + args.seconds
        if args.pattern == "black":
            ser.write(black)
            return 0
        if args.pattern == "white":
            ser.write(white)
            return 0

        period = 1.0 / max(args.fps, 0.1)
        frame = 0
        while time.time() < deadline:
            ser.write(white if (frame % 2) else black)
            frame += 1
            time.sleep(period)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
