import time


class ST7567:
    def __init__(
        self,
        spi,
        *,
        cs,
        dc,
        rst,
        width=128,
        height=64,
        col_offset=0,
        invert=False,
        contrast=0x20,
        regulation_ratio=3,
        line_offset=0,
        com_reverse=True,
        segment_reverse=False,
        bias_1_7=True,
    ):
        self._spi = spi
        self._cs = cs
        self._dc = dc
        self._rst = rst
        self.width = width
        self.height = height
        self.pages = height // 8
        self.col_offset = col_offset
        self._cmd_buf = bytearray(3)
        col = self.col_offset
        self._cmd_buf[1] = 0x10 | ((col >> 4) & 0x0F)
        self._cmd_buf[2] = 0x00 | (col & 0x0F)

        self._cs.value(1)
        self._dc.value(0)
        self._rst.value(1)

        self._hw_reset()
        self._init_display(
            contrast=contrast,
            invert=invert,
            regulation_ratio=regulation_ratio,
            line_offset=line_offset,
            com_reverse=com_reverse,
            segment_reverse=segment_reverse,
            bias_1_7=bias_1_7,
        )

    def _hw_reset(self):
        self._rst.value(0)
        time.sleep_ms(50)
        self._rst.value(1)
        time.sleep_ms(50)

    def _cmd(self, b):
        self._cs.value(0)
        self._dc.value(0)
        self._spi.write(bytes((b & 0xFF,)))
        self._cs.value(1)

    def _data(self, buf):
        self._cs.value(0)
        self._dc.value(1)
        self._spi.write(buf)
        self._cs.value(1)

    def _init_display(
        self,
        *,
        contrast,
        invert,
        regulation_ratio,
        line_offset,
        com_reverse,
        segment_reverse,
        bias_1_7,
    ):
        # ST7567/ST7565-compatible init. Defaults match the original Zephyr config.
        reg = max(0, min(int(regulation_ratio), 7))
        line = max(0, min(int(line_offset), 63))
        self._cmd(0xE2)  # software reset
        self._cmd(0xAE)  # display off
        self._cmd(0xA2 | (1 if bias_1_7 else 0))  # LCD bias 1/9 (0) or 1/7 (1)
        self._cmd(0xA0 | (1 if segment_reverse else 0))  # ADC select
        self._cmd(0xC0 | (0x08 if com_reverse else 0x00))  # COM output direction
        self._cmd(0x40 | line)  # start line
        self._cmd(0x2F)  # power control: booster/regulator/follower on
        self._cmd(0x20 | reg)  # regulation ratio
        self.set_contrast(contrast)
        self._cmd(0xA6 if not invert else 0xA7)  # normal / inverse
        self._cmd(0xA4)  # all points normal
        self._cmd(0xAF)  # display on

    def set_contrast(self, contrast):
        v = max(0, min(contrast, 0x3F))
        self._cmd(0x81)
        self._cmd(v)

    def set_regulation_ratio(self, ratio):
        r = max(0, min(int(ratio), 7))
        self._cmd(0x20 | r)

    def set_bias(self, bias_1_7: bool):
        self._cmd(0xA2 | (1 if bias_1_7 else 0))

    def set_invert(self, invert):
        self._cmd(0xA7 if invert else 0xA6)

    def fill(self, on):
        v = 0xFF if on else 0x00
        self.show(bytes([v]) * (self.width * self.pages))

    def show(self, frame):
        if len(frame) != self.width * self.pages:
            raise ValueError("frame must be width*(height/8) bytes")

        # Page-addressed: each byte is a vertical column of 8 pixels.
        mv = memoryview(frame)
        cmd = self._cmd_buf

        # Keep CS asserted for the whole frame; only toggle DC.
        self._cs.value(0)
        for page in range(self.pages):
            cmd[0] = 0xB0 | page
            self._dc.value(0)
            self._spi.write(cmd)

            start = page * self.width
            self._dc.value(1)
            self._spi.write(mv[start : start + self.width])
        self._cs.value(1)
