#!/usr/bin/env python3
import argparse
import base64
import pathlib
import sys
import time

import serial
from serial.tools import list_ports


CTRL_A = b"\x01"  # raw REPL
CTRL_B = b"\x02"  # friendly REPL
CTRL_C = b"\x03"
CTRL_D = b"\x04"


def list_serial_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("(no serial ports found)")
        return
    for p in ports:
        print(f"{p.device}\t{p.description}")


def _read_with_timeout(ser: serial.Serial, timeout_s: float) -> bytes:
    end = time.time() + timeout_s
    out = bytearray()
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            out += chunk
            continue
        time.sleep(0.01)
    return bytes(out)


def read_until(ser: serial.Serial, needle: bytes, timeout_s: float) -> bytes:
    end = time.time() + timeout_s
    buf = bytearray()
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if needle in buf:
                return bytes(buf)
        else:
            time.sleep(0.01)
    raise TimeoutError(f"Timed out waiting for {needle!r}. Got: {bytes(buf)[-200:]!r}")


def enter_raw_repl(ser: serial.Serial) -> None:
    # Interrupt any running program and enter raw REPL.
    ser.write(CTRL_C)
    ser.write(CTRL_C)
    time.sleep(0.1)
    _ = _read_with_timeout(ser, 0.2)

    ser.write(CTRL_A)
    read_until(ser, b"raw REPL", 2.0)


def exec_raw(ser: serial.Serial, src: str) -> tuple[bytes, bytes]:
    if not src.endswith("\n"):
        src += "\n"
    ser.write(src.encode("utf-8"))
    ser.write(CTRL_D)

    # Raw REPL protocol: 'OK' then stdout until 0x04 then stderr until 0x04.
    end_ok = time.time() + 2.0
    buf = bytearray()
    while time.time() < end_ok:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            if b"OK" in buf:
                break
        else:
            time.sleep(0.01)

    ok_idx = buf.find(b"OK")
    if ok_idx < 0:
        raise TimeoutError("Timed out waiting for raw REPL OK")

    tail = bytes(buf[ok_idx + 2 :])

    def read_until_ctrl_d(tail_bytes: bytes, timeout_s: float, which: str) -> tuple[bytes, bytes]:
        end = time.time() + timeout_s
        tmp = bytearray(tail_bytes)
        while time.time() < end:
            pos = tmp.find(CTRL_D)
            if pos >= 0:
                return bytes(tmp[:pos]), bytes(tmp[pos + 1 :])
            chunk = ser.read(4096)
            if chunk:
                tmp += chunk
            else:
                time.sleep(0.01)
        raise TimeoutError(f"Timed out waiting for raw {which} terminator")

    out, tail = read_until_ctrl_d(tail, 10.0, "stdout")
    err, _tail = read_until_ctrl_d(tail, 10.0, "stderr")
    return out, err


def write_remote_file(ser: serial.Serial, remote_name: str, data: bytes) -> None:
    b64 = base64.b64encode(data).decode("ascii")
    chunk = 512  # must be divisible by 4
    lines = [b64[i : i + chunk] for i in range(0, len(b64), chunk)]

    code = [
        "import ubinascii",
        f"f = open({remote_name!r}, 'wb')",
    ]
    code += [f"f.write(ubinascii.a2b_base64({ln.encode('ascii')!r}))" for ln in lines]
    code += ["f.close()", f"print('WROTE', {remote_name!r}, {len(data)})"]

    out, err = exec_raw(ser, "\n".join(code))
    if err.strip():
        raise RuntimeError(f"MicroPython error writing {remote_name}:\n{err.decode('utf-8','replace')}")
    # Optional: show stdout if debugging
    _ = out


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload the Bee Movie MicroPython receiver files over MicroPython raw REPL.")
    ap.add_argument("--list", action="store_true", help="List serial ports and exit")
    ap.add_argument("--port", help="Serial port (e.g. /dev/cu.usbmodem1101)")
    ap.add_argument("--baud", type=int, default=115200, help="Baud rate (USB CDC ignores this)")
    ap.add_argument(
        "--pico-dir",
        default=str(pathlib.Path(__file__).resolve().parent / "pico"),
        help="Directory containing main.py + st7567.py (default: bee_movie/pico)",
    )
    ap.add_argument("--no-reset", action="store_true", help="Don't reset after upload")
    args = ap.parse_args()

    if args.list:
        list_serial_ports()
        return 0

    if not args.port:
        print("Missing --port. Use --list to see candidates.", file=sys.stderr)
        return 2

    pico_dir = pathlib.Path(args.pico_dir)
    main_py = pico_dir / "main.py"
    st_py = pico_dir / "st7567.py"
    if not main_py.exists() or not st_py.exists():
        print(f"Expected {main_py} and {st_py}", file=sys.stderr)
        return 2

    with serial.Serial(args.port, args.baud, timeout=0.2, write_timeout=2.0) as ser:
        enter_raw_repl(ser)

        write_remote_file(ser, "st7567.py", st_py.read_bytes())
        write_remote_file(ser, "main.py", main_py.read_bytes())

        if not args.no_reset:
            try:
                _out, err = exec_raw(ser, "import machine; machine.reset()")
                if err.strip():
                    raise RuntimeError(err.decode("utf-8", "replace"))
            except Exception:
                # A hard reset will typically drop USB and cause reads to fail.
                pass

        try:
            ser.write(CTRL_B)
        except Exception:
            pass

    print("Uploaded. If you used --no-reset, reset the board to start playback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
