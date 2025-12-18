#!/usr/bin/env python3
import argparse
import base64
import queue
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import serial


W = 128
H = 64
RAW_FRAME_BYTES = W * H  # 8-bit grayscale
PACKED_FRAME_BYTES = W * (H // 8)  # 1-bit, ST7567 page format (1024)


def build_ffmpeg_cmd(
    input_path: str,
    fps: float,
    mode: str,
    *,
    seek_s: float | None,
    scale_flags: str,
) -> list[str]:
    if mode == "crop":
        vf = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags={scale_flags},"
            f"crop={W}:{H},"
            "format=gray,"
            f"fps={fps}"
        )
    elif mode == "fit":
        vf = (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags={scale_flags},"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
            "format=gray,"
            f"fps={fps}"
        )
    else:
        raise ValueError("mode must be crop or fit")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if seek_s is not None and seek_s > 0:
        cmd += ["-ss", str(seek_s)]
    cmd += [
        "-i",
        input_path,
        "-an",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    return cmd


def read_exact(stream, n: int) -> bytes:
    out = bytearray(n)
    mv = memoryview(out)
    got = 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            return bytes(out[:got])
        mv[got : got + len(chunk)] = chunk
        got += len(chunk)
    return bytes(out)


def bayer8() -> np.ndarray:
    # 8x8 Bayer matrix, values 0..63
    return np.array(
        [
            [0, 48, 12, 60, 3, 51, 15, 63],
            [32, 16, 44, 28, 35, 19, 47, 31],
            [8, 56, 4, 52, 11, 59, 7, 55],
            [40, 24, 36, 20, 43, 27, 39, 23],
            [2, 50, 14, 62, 1, 49, 13, 61],
            [34, 18, 46, 30, 33, 17, 45, 29],
            [10, 58, 6, 54, 9, 57, 5, 53],
            [42, 26, 38, 22, 41, 25, 37, 21],
        ],
        dtype=np.uint8,
    )


_BAYER_THR = (np.tile(bayer8(), (H // 8, W // 8)) * 4 + 2).astype(np.uint8)


def dither_and_pack(
    gray_frame: bytes,
    *,
    invert: bool,
    rotate180: bool,
    gamma: float,
    brightness: int,
    contrast: float,
    dither: str,
) -> bytes:
    if len(gray_frame) != RAW_FRAME_BYTES:
        raise ValueError("bad raw frame size")

    frame = np.frombuffer(gray_frame, dtype=np.uint8).reshape((H, W))
    if rotate180:
        frame = frame[::-1, ::-1]

    if brightness or contrast != 1.0:
        tmp = frame.astype(np.float32)
        if contrast != 1.0:
            tmp = (tmp - 128.0) * float(contrast) + 128.0
        if brightness:
            tmp = tmp + float(brightness)
        frame = np.clip(tmp, 0.0, 255.0).astype(np.uint8)

    if gamma != 1.0:
        g = float(gamma)
        if g <= 0:
            raise ValueError("gamma must be > 0")
        tmp = frame.astype(np.float32) / 255.0
        tmp = np.power(tmp, g) * 255.0
        frame = np.clip(tmp, 0.0, 255.0).astype(np.uint8)

    # Dither to 1-bit.
    # ST7567 "1" bits are typically black; treat low luma as black.
    if dither == "bayer":
        on = frame < _BAYER_THR  # True => black pixel
    elif dither == "fs":
        arr = frame.astype(np.int16)
        for y in range(H):
            if y & 1:
                x_start, x_end, step = W - 1, -1, -1
                dir_ = -1
            else:
                x_start, x_end, step = 0, W, 1
                dir_ = 1
            for x in range(x_start, x_end, step):
                old = int(arr[y, x])
                new = 0 if old < 128 else 255
                err = old - new
                arr[y, x] = new

                xn = x + dir_
                if 0 <= xn < W:
                    arr[y, xn] += (err * 7) // 16
                if y + 1 < H:
                    arr[y + 1, x] += (err * 5) // 16
                    xp = x - dir_
                    if 0 <= xp < W:
                        arr[y + 1, xp] += (err * 3) // 16
                    if 0 <= xn < W:
                        arr[y + 1, xn] += (err * 1) // 16
        on = arr < 128
    elif dither == "atkinson":
        arr = frame.astype(np.int16)
        for y in range(H):
            for x in range(W):
                old = int(arr[y, x])
                new = 0 if old < 128 else 255
                err = old - new
                arr[y, x] = new
                q = err // 8
                if x + 1 < W:
                    arr[y, x + 1] += q
                if x + 2 < W:
                    arr[y, x + 2] += q
                if y + 1 < H:
                    if x - 1 >= 0:
                        arr[y + 1, x - 1] += q
                    arr[y + 1, x] += q
                    if x + 1 < W:
                        arr[y + 1, x + 1] += q
                if y + 2 < H:
                    arr[y + 2, x] += q
        on = arr < 128
    else:
        raise ValueError("dither must be bayer, fs, or atkinson")

    if invert:
        on = ~on

    packed = np.packbits(on, axis=0, bitorder="little")  # (8, 128)
    return packed.reshape(-1).tobytes()  # page-major: 8*128 = 1024 bytes


def main() -> int:
    ap = argparse.ArgumentParser(description="Stream Bee Movie frames to a MicroPython ST7567 receiver over serial.")
    ap.add_argument("--port", required=True, help="Serial port (e.g. /dev/cu.usbmodem1101)")
    ap.add_argument("--baud", type=int, default=500000, help="Baud rate (USB CDC ignores this, kept for compatibility)")
    ap.add_argument("--fps", type=float, default=15.0, help="Playback FPS (try 10-20)")
    ap.add_argument("--lcd-contrast", type=int, default=-1, help="Set LCD contrast 0..63 before playback")
    ap.add_argument(
        "--backlight",
        default="",
        help="Set backlight before playback: 0..100 (percent), 0..65535 (duty), or 0.0..1.0 (ratio)",
    )
    ap.add_argument("--gamma", type=float, default=1.0, help="Gamma correction (>0). <1 brighter mids, >1 darker.")
    ap.add_argument(
        "--brightness",
        type=int,
        default=0,
        help="Brightness shift (-255..255) applied before dithering",
    )
    ap.add_argument(
        "--contrast",
        type=float,
        default=1.0,
        help="Contrast multiplier (>0) applied around mid-gray before dithering",
    )
    ap.add_argument(
        "--dither",
        choices=("bayer", "fs", "atkinson"),
        default="bayer",
        help="Dither algorithm: bayer (fast, patterned), fs (Floyd-Steinberg), or atkinson",
    )
    ap.add_argument(
        "--scale-flags",
        default="lanczos",
        help="ffmpeg scale filter flags (e.g. lanczos, bicubic, bilinear)",
    )
    ap.add_argument(
        "--mode",
        choices=("crop", "fit"),
        default="crop",
        help="Scale mode: crop (fill) or fit (letterbox)",
    )
    ap.add_argument("--invert", action="store_true", help="Invert pixels (swap black/white)")
    ap.add_argument("--rotate180", action="store_true", help="Rotate frames 180 degrees")
    ap.add_argument("--seek", type=float, default=0.0, help="Seek seconds into the movie")
    ap.add_argument("--frames", type=int, default=0, help="Stop after N frames (0 = until EOF)")
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="Read extra lines from stdin and forward them to the device (e.g. !contrast 40, !bl 0.3)",
    )
    ap.add_argument(
        "--drop-frames",
        action="store_true",
        help="If falling behind real-time, skip input frames to catch up (keeps playback speed)",
    )
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Launch a local player (ffplay) in sync when the first frame is sent",
    )
    ap.add_argument(
        "--preview-mute",
        action="store_true",
        help="Mute audio in the local preview player",
    )
    ap.add_argument(
        "--preview-offset",
        type=float,
        default=0.0,
        help="Start the preview at seek+offset seconds (can be negative)",
    )
    ap.add_argument(
        "--no-realtime",
        action="store_true",
        help="Send frames as fast as possible (default: real-time pacing)",
    )
    ap.add_argument(
        "--input",
        default="bee_movie.mp4",
        help="Input video path (default: bee_movie.mp4)",
    )
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found in PATH.", file=sys.stderr)
        return 2
    if args.preview and not shutil.which("ffplay"):
        print("ffplay not found in PATH (needed for --preview).", file=sys.stderr)
        return 2

    cmd = build_ffmpeg_cmd(
        args.input,
        args.fps,
        args.mode,
        seek_s=args.seek if args.seek > 0 else None,
        scale_flags=args.scale_flags,
    )
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None

    with serial.Serial(args.port, args.baud, timeout=1.0, write_timeout=5.0) as ser:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        if args.lcd_contrast >= 0:
            ser.write(f"!contrast {args.lcd_contrast}\n".encode("utf-8"))
        if args.backlight:
            ser.write(f"!bl {args.backlight}\n".encode("utf-8"))

        preview_cmd: list[str] | None = None
        if args.preview:
            preview_cmd = ["ffplay", "-hide_banner", "-loglevel", "error", "-autoexit"]
            if args.preview_mute:
                preview_cmd += ["-an"]
            preview_seek = float(args.seek) + float(args.preview_offset)
            if preview_seek > 0:
                preview_cmd += ["-ss", str(preview_seek)]
            preview_cmd += ["-i", args.input]
        preview_proc: subprocess.Popen | None = None

        proc_cfg = {
            "invert": bool(args.invert),
            "rotate180": bool(args.rotate180),
            "gamma": float(args.gamma),
            "brightness": int(args.brightness),
            "contrast": float(args.contrast),
            "dither": str(args.dither),
        }
        proc_defaults = dict(proc_cfg)

        device_q: queue.SimpleQueue[bytes] | None = None
        host_q: queue.SimpleQueue[tuple[str, object]] | None = None
        if args.interactive:
            device_q = queue.SimpleQueue()
            host_q = queue.SimpleQueue()

            def stdin_worker():
                while True:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if line.startswith("@"):
                            parts = line[1:].split()
                            if not parts:
                                continue
                            k = parts[0].lower()
                            v = parts[1] if len(parts) > 1 else ""
                            host_q.put((k, v))
                            continue

                        if not line.startswith("!"):
                            line = "!" + line
                        device_q.put((line + "\n").encode("utf-8"))
                    except Exception:
                        break

            threading.Thread(target=stdin_worker, daemon=True).start()

        start: float | None = None
        frames_idx = 0
        frames_sent = 0
        frames_dropped = 0
        _ = 1.0 / max(args.fps, 1e-6)

        while True:
            if host_q is not None:
                try:
                    while True:
                        key, raw_val = host_q.get_nowait()
                        key = str(key).lower()

                        if key in ("help", "?"):
                            sys.stderr.write(
                                "\nInteractive commands:\n"
                                "  Device (sent to board): !contrast 40 | !bl 0.3 | !inv 1 | !reg 3 | !bias 1\n"
                                "  Host (video processing): @gamma 1.2 | @contrast 1.2 | @brightness -10 | @dither fs\n"
                                "  Host: @invert 1 | @rotate180 1 | @reset\n\n"
                            )
                            sys.stderr.flush()
                            continue

                        if key in ("reset",):
                            proc_cfg.update(proc_defaults)
                            sys.stderr.write(f"[host] reset -> {proc_cfg}\n")
                            sys.stderr.flush()
                            continue

                        canon = None
                        if key in ("gamma", "g"):
                            proc_cfg["gamma"] = float(raw_val)
                            canon = "gamma"
                        elif key in ("contrast", "c"):
                            proc_cfg["contrast"] = float(raw_val)
                            canon = "contrast"
                        elif key in ("brightness", "b"):
                            proc_cfg["brightness"] = int(float(raw_val))
                            canon = "brightness"
                        elif key in ("dither", "d"):
                            v = str(raw_val).strip().lower()
                            if v not in ("bayer", "fs", "atkinson"):
                                sys.stderr.write("[host] dither must be bayer|fs|atkinson\n")
                                sys.stderr.flush()
                                continue
                            proc_cfg["dither"] = v
                            canon = "dither"
                        elif key in ("invert", "inv"):
                            v = str(raw_val).strip().lower()
                            proc_cfg["invert"] = v not in ("0", "false", "off", "")
                            canon = "invert"
                        elif key in ("rotate180", "rot", "rotate"):
                            v = str(raw_val).strip().lower()
                            proc_cfg["rotate180"] = v not in ("0", "false", "off", "")
                            canon = "rotate180"
                        else:
                            sys.stderr.write(f"[host] unknown @{key} (try @help)\n")
                            sys.stderr.flush()
                            continue

                        if canon is None:
                            canon = key
                        sys.stderr.write(f"[host] {canon} -> {proc_cfg[canon]}\n")
                        sys.stderr.flush()
                except queue.Empty:
                    pass

            if device_q is not None:
                try:
                    while True:
                        ser.write(device_q.get_nowait())
                except queue.Empty:
                    pass

            if not args.no_realtime and args.drop_frames:
                if start is None:
                    # Don't drop until we have a baseline start time.
                    pass
                now = time.perf_counter()
                should_have = int((now - start) * args.fps) if start is not None else 0
                behind = should_have - frames_idx
                if behind > 1:
                    drop = min(behind - 1, int(args.fps * 2))  # cap at ~2s
                    eof = False
                    for _ in range(drop):
                        raw_skip = read_exact(proc.stdout, RAW_FRAME_BYTES)
                        if len(raw_skip) != RAW_FRAME_BYTES:
                            eof = True
                            break
                        frames_idx += 1
                        frames_dropped += 1
                    if eof:
                        break

            raw = read_exact(proc.stdout, RAW_FRAME_BYTES)
            if len(raw) != RAW_FRAME_BYTES:
                break
            frames_idx += 1

            payload = dither_and_pack(
                raw,
                invert=proc_cfg["invert"],
                rotate180=proc_cfg["rotate180"],
                gamma=proc_cfg["gamma"],
                brightness=proc_cfg["brightness"],
                contrast=proc_cfg["contrast"],
                dither=proc_cfg["dither"],
            )
            if len(payload) != PACKED_FRAME_BYTES:
                raise AssertionError("packed frame size mismatch")

            if frames_sent == 0:
                if preview_cmd is not None and preview_proc is None:
                    try:
                        preview_proc = subprocess.Popen(preview_cmd)
                    except Exception as e:
                        print(f"Failed to start preview player: {e}", file=sys.stderr)
                start = time.perf_counter()

            ser.write(base64.b64encode(payload) + b"\n")
            frames_sent += 1

            if not args.no_realtime:
                if start is None:
                    start = time.perf_counter()
                target = start + (frames_idx / args.fps)
                now = time.perf_counter()
                if target > now:
                    time.sleep(target - now)

            if frames_sent % int(max(1, args.fps * 5)) == 0:
                sys.stderr.write(f"\rframes sent: {frames_sent} (dropped: {frames_dropped})")
                sys.stderr.flush()

            if args.frames and frames_sent >= args.frames:
                break
        sys.stderr.write(f"\nDone. Total frames sent: {frames_sent} (dropped: {frames_dropped})\n")

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    # If ffmpeg errored, surface it.
    rc = proc.returncode if proc.returncode is not None else 0
    if rc != 0:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if err.strip():
            print(err, file=sys.stderr)
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
