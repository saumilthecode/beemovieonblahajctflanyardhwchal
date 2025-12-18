import gc
import sys
import time

import ubinascii
from machine import PWM, Pin, SPI

from st7567 import ST7567


PIN_SCK = 10
PIN_MOSI = 11
PIN_MISO = 12  # unused by the LCD, but required by some ports
PIN_CS = 13
PIN_DC = 9
PIN_RST = 8
PIN_BL = 6  # PWM backlight enable (from Zephyr pinctrl)

# Buttons (from Zephyr gpio_keys config in flash_dump.bin)
PIN_BTN_UP = 7
PIN_BTN_DOWN = 0
PIN_BTN_OK = 5
PIN_BTN_BACK = 16
PIN_BTN_BOOTSEL = 17

SPI_BAUD = 20_000_000
COL_OFFSET = 0
INVERT = False
CONTRAST = 0x2A  # 0..0x3F
REG_RATIO = 3  # 0..7
BIAS_1_7 = True  # matches Zephyr config (0xA3), set False for 0xA2
BACKLIGHT_DUTY = 25000  # 0..65535; lower = better perceived contrast

# Top LEDs (guesses; can be overridden with !led2/!led3 commands)
PIN_LED_D2 = 2  # green
PIN_LED_D3 = 3  # yellow
LED_ACTIVE_LOW = True

FRAME_BYTES = 128 * (64 // 8)  # 1024


_STDIN = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin


def enable_backlight(duty_u16=BACKLIGHT_DUTY):
    try:
        pwm = PWM(Pin(PIN_BL))
        pwm.freq(2000)
        pwm.duty_u16(int(max(0, min(int(duty_u16), 65535))))
        return ("pwm", pwm)
    except Exception:
        bl = Pin(PIN_BL, Pin.OUT)
        bl.value(1 if int(duty_u16) > 0 else 0)
        return ("pin", bl)


def set_backlight(backlight, value):
    kind, obj = backlight
    if kind == "pwm":
        v = int(max(0, min(int(value), 65535)))
        obj.duty_u16(v)
    else:
        obj.value(1 if int(value) > 0 else 0)


def _led_set(led, on, *, active_low):
    if led is None:
        return
    led.value(0 if (on and active_low) else 1 if (on and not active_low) else 1 if active_low else 0)


def _mk_led(pin_num):
    if pin_num is None:
        return None
    try:
        p = int(pin_num)
    except Exception:
        return None
    if p < 0:
        return None
    return Pin(p, Pin.OUT)


def handle_line(line, lcd, backlight, state, leds):
    if not line:
        return None
    if isinstance(line, str):
        line = line.encode("utf-8", "ignore")
    line = line.strip()
    if not line:
        return None

    if line.startswith(b"!"):
        parts = line[1:].split()
        if not parts:
            return None
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        arg2 = parts[2] if len(parts) > 2 else None

        if cmd in (b"quit", b"exit"):
            raise SystemExit

        if cmd in (b"contrast", b"c") and arg is not None:
            try:
                v = int(arg)
                state["contrast"] = max(0, min(v, 0x3F))
                lcd.set_contrast(state["contrast"])
            except Exception:
                pass
            return None

        if cmd in (b"ratio", b"reg") and arg is not None:
            try:
                state["reg_ratio"] = max(0, min(int(arg), 7))
                lcd.set_regulation_ratio(state["reg_ratio"])
            except Exception:
                pass
            return None

        if cmd in (b"bias",) and arg is not None:
            try:
                state["bias_1_7"] = int(arg) != 0
                lcd.set_bias(state["bias_1_7"])
            except Exception:
                pass
            return None

        if cmd in (b"invert", b"inv") and arg is not None:
            try:
                state["invert"] = int(arg) != 0
                lcd.set_invert(state["invert"])
            except Exception:
                pass
            return None

        if cmd in (b"backlight", b"bl") and arg is not None:
            try:
                s = arg.decode("utf-8")
                if "." in s:
                    v = int(float(s) * 65535.0)
                else:
                    v = int(s)
                    if 0 <= v <= 100:
                        v = v * 65535 // 100
                v = int(max(0, min(v, 65535)))
                state["backlight"] = v
                set_backlight(backlight, v)
            except Exception:
                pass
            return None

        if cmd in (b"led2", b"d2") and arg is not None:
            try:
                leds["d2"] = _mk_led(int(arg))
                if arg2 is not None:
                    leds["active_low"] = int(arg2) != 0
                _led_set(leds["d2"], True, active_low=leds["active_low"])
                time.sleep_ms(80)
                _led_set(leds["d2"], False, active_low=leds["active_low"])
            except Exception:
                pass
            return None

        if cmd in (b"led3", b"d3") and arg is not None:
            try:
                leds["d3"] = _mk_led(int(arg))
                if arg2 is not None:
                    leds["active_low"] = int(arg2) != 0
                _led_set(leds["d3"], True, active_low=leds["active_low"])
                time.sleep_ms(80)
                _led_set(leds["d3"], False, active_low=leds["active_low"])
            except Exception:
                pass
            return None

        if cmd in (b"ledpol", b"led_polarity") and arg is not None:
            try:
                leds["active_low"] = int(arg) != 0
            except Exception:
                pass
            return None

        if cmd in (b"probe", b"blinkpin") and arg is not None:
            try:
                pin_num = int(arg)
                active_low = True
                if arg2 is not None:
                    active_low = int(arg2) != 0
                ms = 120
                if len(parts) > 3 and parts[3] is not None:
                    ms = int(parts[3])
                ms = max(10, min(ms, 2000))
                p = Pin(pin_num, Pin.OUT)
                _led_set(p, True, active_low=active_low)
                time.sleep_ms(ms)
                _led_set(p, False, active_low=active_low)
                Pin(pin_num, Pin.IN)
            except Exception:
                pass
            return None

        if cmd in (b"targetfps", b"fps") and arg is not None:
            try:
                state["target_fps"] = int(arg)
            except Exception:
                pass
            return None

        return None

    try:
        frame = ubinascii.a2b_base64(line)
    except Exception:
        return None
    if len(frame) != FRAME_BYTES:
        return None
    return frame


def main():
    backlight = enable_backlight()
    state = {
        "contrast": int(CONTRAST),
        "backlight": int(BACKLIGHT_DUTY),
        "invert": bool(INVERT),
        "target_fps": 60,
        "reg_ratio": int(REG_RATIO),
        "bias_1_7": bool(BIAS_1_7),
    }

    leds = {
        "d2": _mk_led(PIN_LED_D2),
        "d3": _mk_led(PIN_LED_D3),
        "active_low": bool(LED_ACTIVE_LOW),
    }
    _led_set(leds["d2"], False, active_low=leds["active_low"])
    _led_set(leds["d3"], False, active_low=leds["active_low"])

    buttons = {
        "up": Pin(PIN_BTN_UP, Pin.IN, Pin.PULL_UP),
        "down": Pin(PIN_BTN_DOWN, Pin.IN, Pin.PULL_UP),
        "ok": Pin(PIN_BTN_OK, Pin.IN, Pin.PULL_UP),
        "back": Pin(PIN_BTN_BACK, Pin.IN, Pin.PULL_UP),
        "bootsel": Pin(PIN_BTN_BOOTSEL, Pin.IN, Pin.PULL_UP),
    }
    btn_prev = {k: 1 for k in buttons}
    btn_last = time.ticks_ms()
    btn_period_ms = 60
    bl_step = 4000

    spi = SPI(
        1,
        baudrate=SPI_BAUD,
        polarity=0,
        phase=0,
        bits=8,
        firstbit=SPI.MSB,
        sck=Pin(PIN_SCK),
        mosi=Pin(PIN_MOSI),
        miso=Pin(PIN_MISO),
    )

    lcd = ST7567(
        spi,
        cs=Pin(PIN_CS, Pin.OUT),
        dc=Pin(PIN_DC, Pin.OUT),
        rst=Pin(PIN_RST, Pin.OUT),
        col_offset=COL_OFFSET,
        invert=INVERT,
        contrast=CONTRAST,
        regulation_ratio=REG_RATIO,
        bias_1_7=BIAS_1_7,
    )
    lcd.fill(True)
    time.sleep_ms(150)
    lcd.fill(False)

    try:
        import uselect

        poller = uselect.poll()
        poller.register(sys.stdin, uselect.POLLIN)
        use_poll = True
    except Exception:
        use_poll = False
        poller = None

    frames_window = 0
    fps_last = time.ticks_ms()

    while True:
        now = time.ticks_ms()
        if time.ticks_diff(now, btn_last) >= btn_period_ms:
            btn_last = now
            for name, pin in buttons.items():
                v = pin.value()
                if btn_prev[name] and not v:
                    # Edge: released -> pressed (active-low)
                    if name == "up":
                        state["contrast"] = min(0x3F, int(state["contrast"]) + 1)
                        lcd.set_contrast(state["contrast"])
                    elif name == "down":
                        state["contrast"] = max(0, int(state["contrast"]) - 1)
                        lcd.set_contrast(state["contrast"])
                    elif name == "back":
                        state["backlight"] = max(0, int(state["backlight"]) - bl_step)
                        set_backlight(backlight, state["backlight"])
                    elif name == "bootsel":
                        state["backlight"] = min(65535, int(state["backlight"]) + bl_step)
                        set_backlight(backlight, state["backlight"])
                    elif name == "ok":
                        # Combos for deep tuning:
                        #   OK + UP/DOWN: regulation ratio +/- (0..7)
                        #   OK + BACK: toggle bias (1/7 vs 1/9)
                        if buttons["up"].value() == 0:
                            state["reg_ratio"] = min(7, int(state.get("reg_ratio", REG_RATIO)) + 1)
                            lcd.set_regulation_ratio(state["reg_ratio"])
                        elif buttons["down"].value() == 0:
                            state["reg_ratio"] = max(0, int(state.get("reg_ratio", REG_RATIO)) - 1)
                            lcd.set_regulation_ratio(state["reg_ratio"])
                        elif buttons["back"].value() == 0:
                            state["bias_1_7"] = not bool(state.get("bias_1_7", BIAS_1_7))
                            lcd.set_bias(state["bias_1_7"])
                        else:
                            state["invert"] = not bool(state["invert"])
                            lcd.set_invert(state["invert"])
                btn_prev[name] = v

        line = None
        if use_poll:
            if poller.poll(20):
                line = sys.stdin.readline()
        else:
            line = _STDIN.readline()

        if line:
            frame = handle_line(line, lcd, backlight, state, leds)
            if frame is not None:
                lcd.show(frame)
                frames_window += 1

        if time.ticks_diff(now, fps_last) >= 1000:
            fps_last = now
            target = int(state.get("target_fps", 60))
            ok = frames_window >= max(1, target - 3)
            _led_set(leds["d2"], ok, active_low=leds["active_low"])
            _led_set(leds["d3"], not ok, active_low=leds["active_low"])
            frames_window = 0
        # Keep GC from running mid-frame too often.
        if (time.ticks_ms() & 0x3FF) == 0:
            gc.collect()


main()
