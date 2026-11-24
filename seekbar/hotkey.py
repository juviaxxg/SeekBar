class DoubleCommand:
    def __init__(self, delay=0.55, hold=0.40):
        self.delay = delay
        self.hold = hold
        self.reset()

    def reset(self):
        self.down = None
        self.last_up = None

    def modifier(self, keycode, command, other, now):
        if keycode not in (55, 54) or other:
            self.reset()
            return False
        if command:
            if self.down is not None:
                self.reset()
                return False
            self.down = (keycode, now)
            return False
        if self.down is None or self.down[0] != keycode:
            self.reset()
            return False
        pressed = self.down[1]
        self.down = None
        if now - pressed > self.hold:
            self.reset()
            return False
        if self.last_up is not None and pressed - self.last_up <= self.delay:
            self.reset()
            return True
        self.last_up = now
        return False
