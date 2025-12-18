#!/usr/bin/env python3
"""
Simple GPIO probe GUI for your MicroPython "!probe <gpio> <active_low> <ms>" protocol.

Requires:
  pip3 install pyserial
Run:
  python3 probe_gui.py
"""

import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import serial
from serial.tools import list_ports

DEFAULT_EXCLUDE = {
    0, 1, 2, 3, 4, 5, 6, 7, 16, 17,  # buttons/backlight
    8, 9, 10, 11, 12, 13,            # LCD SPI/control
}


def parse_int_set(csv: str) -> set[int]:
    out: set[int] = set()
    for part in csv.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part, 0))
    return out


class ProbeGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("GPIO Probe (MicroPython !probe)")
        self.geometry("900x560")

        self.ser: serial.Serial | None = None
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()

        self._build_ui()
        self._refresh_ports()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        # Port row
        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=36, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="w")

        ttk.Button(top, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Label(top, text="Baud:").grid(row=0, column=3, sticky="w", padx=(18, 0))
        self.baud_var = tk.StringVar(value="500000")
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, sticky="w")

        self.conn_btn = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.conn_btn.grid(row=0, column=5, sticky="w", padx=(18, 0))

        # Params row
        ttk.Label(top, text="Start GPIO:").grid(row=1, column=0, sticky="w")
        self.start_var = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.start_var, width=6).grid(row=1, column=1, sticky="w")

        ttk.Label(top, text="End GPIO:").grid(row=1, column=2, sticky="w", padx=(18, 0))
        self.end_var = tk.StringVar(value="29")
        ttk.Entry(top, textvariable=self.end_var, width=6).grid(row=1, column=3, sticky="w")

        ttk.Label(top, text="Blink ms:").grid(row=1, column=4, sticky="w", padx=(18, 0))
        self.ms_var = tk.StringVar(value="120")
        ttk.Entry(top, textvariable=self.ms_var, width=8).grid(row=1, column=5, sticky="w")

        # Options row
        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)

        self.use_default_exclude_var = tk.BooleanVar(value=False)  # default: probe ALL pins
        ttk.Checkbutton(
            opt,
            text="Use DEFAULT_EXCLUDE (safer)",
            variable=self.use_default_exclude_var,
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(opt, text="Extra exclude (comma-separated):").grid(row=0, column=1, sticky="w", padx=(18, 0))
        self.exclude_var = tk.StringVar(value="")
        ttk.Entry(opt, textvariable=self.exclude_var, width=38).grid(row=0, column=2, sticky="w")

        ttk.Label(opt, text="Read window ms:").grid(row=0, column=3, sticky="w", padx=(18, 0))
        self.readms_var = tk.StringVar(value="250")
        ttk.Entry(opt, textvariable=self.readms_var, width=8).grid(row=0, column=4, sticky="w")

        # Buttons row
        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)

        self.probe_btn = ttk.Button(btns, text="Start Probe", command=self._start_probe, state="disabled")
        self.probe_btn.pack(side="left")

        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop_probe, state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 0))

        ttk.Button(btns, text="Clear Log", command=self._clear_log).pack(side="left", padx=(10, 0))

        # Log
        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, **pad)

        self.log = tk.Text(log_frame, wrap="none", height=20)
        self.log.pack(side="left", fill="both", expand=True)

        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        yscroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=yscroll.set)

        self._log_line("Not connected. Select a port and click Connect.")

    def _refresh_ports(self) -> None:
        ports = []
        for p in list_ports.comports():
            # show "device - description"
            ports.append(f"{p.device}  ({p.description})")

        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _selected_device(self) -> str:
        val = self.port_var.get().strip()
        if not val:
            return ""
        # device is first token before spaces
        return val.split()[0]

    def _toggle_connect(self) -> None:
        if self.ser:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        dev = self._selected_device()
        if not dev:
            messagebox.showerror("Connect", "No serial port selected.")
            return
        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showerror("Connect", "Invalid baud rate.")
            return

        try:
            self.ser = serial.Serial(dev, baud, timeout=0.1, write_timeout=2.0)
            time.sleep(0.2)
            self._log_line(f"Connected: {dev} (baud={baud})")
            self.conn_btn.config(text="Disconnect")
            self.probe_btn.config(state="normal")
        except Exception as e:
            self.ser = None
            messagebox.showerror("Connect", f"Failed to open port:\n{e}")

    def _disconnect(self) -> None:
        self._stop_probe()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.conn_btn.config(text="Connect")
        self.probe_btn.config(state="disabled")
        self._log_line("Disconnected.")

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")

    def _log_line(self, s: str) -> None:
        self.log.insert("end", s + "\n")
        self.log.see("end")

    def _drain_lines(self, read_window_ms: int) -> list[str]:
        assert self.ser is not None
        end = time.monotonic() + (read_window_ms / 1000.0)
        out: list[str] = []
        while time.monotonic() < end and not self.stop_flag.is_set():
            b = self.ser.readline()
            if b:
                out.append(b.decode("utf-8", errors="replace").rstrip("\r\n"))
        return out

    def _write_and_log(self, cmd: str, read_window_ms: int) -> None:
        assert self.ser is not None
        self.ser.write((cmd + "\n").encode("utf-8"))
        self.ser.flush()

        self._log_line(f">> {cmd}")
        if read_window_ms > 0:
            lines = self._drain_lines(read_window_ms)
            if lines:
                for ln in lines:
                    self._log_line(f"<< {ln}")
            else:
                self._log_line("<< (no response)")

    def _start_probe(self) -> None:
        if not self.ser:
            messagebox.showerror("Probe", "Connect to a serial port first.")
            return
        if self.worker and self.worker.is_alive():
            return

        try:
            start = int(self.start_var.get().strip())
            end = int(self.end_var.get().strip())
            ms = int(self.ms_var.get().strip())
            read_ms = int(self.readms_var.get().strip())
        except ValueError:
            messagebox.showerror("Probe", "Start/End/ms/read-ms must be integers.")
            return

        ms = max(10, min(ms, 2000))
        read_ms = max(0, min(read_ms, 5000))
        if end < start:
            messagebox.showerror("Probe", "End GPIO must be >= Start GPIO.")
            return

        extra_ex = set()
        ex_text = self.exclude_var.get().strip()
        if ex_text:
            try:
                extra_ex = parse_int_set(ex_text)
            except ValueError:
                messagebox.showerror("Probe", "Invalid extra exclude list.")
                return

        exclude = set(extra_ex)
        if self.use_default_exclude_var.get():
            exclude |= DEFAULT_EXCLUDE

        pins = [p for p in range(start, end + 1) if p not in exclude]
        if not pins:
            messagebox.showerror("Probe", "No pins to probe after exclusions.")
            return

        self.stop_flag.clear()
        self.probe_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        def worker_fn() -> None:
            try:
                self._log_line(f"Probing GPIOs: {pins}")
                for p in pins:
                    if self.stop_flag.is_set():
                        break
                    self._log_line(f"\nGPIO{p}: probing (active_low=1 then 0)")

                    # drain a bit before each pin so output attribution is cleaner
                    _ = self._drain_lines(120)

                    self._write_and_log(f"!probe {p} 1 {ms}", read_ms)
                    time.sleep((ms / 1000.0) + 0.08)

                    self._write_and_log(f"!probe {p} 0 {ms}", read_ms)
                    time.sleep((ms / 1000.0) + 0.15)

                self._log_line("\nDone.")
            except Exception as e:
                self._log_line(f"\nERROR: {e}")
            finally:
                self.after(0, lambda: self.probe_btn.config(state="normal" if self.ser else "disabled"))
                self.after(0, lambda: self.stop_btn.config(state="disabled"))
                self.stop_flag.clear()

        self.worker = threading.Thread(target=worker_fn, daemon=True)
        self.worker.start()

    def _stop_probe(self) -> None:
        self.stop_flag.set()
        self.stop_btn.config(state="disabled")

    def _on_close(self) -> None:
        self._disconnect()
        self.destroy()


if __name__ == "__main__":
    app = ProbeGUI()
    app.mainloop()
