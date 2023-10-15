from time import sleep, ticks_diff, ticks_ms, sleep_ms
from random import random

try:
    # Local development
    from software.firmware.europi import OLED_WIDTH, OLED_HEIGHT, CHAR_HEIGHT
    from software.firmware.europi import din, ain, k1, k2, oled, b1, b2, cvs, turn_off_all_cvs
    from software.firmware.europi_script import EuroPiScript
except ImportError:
    # Device import path
    from europi import *
    from europi_script import EuroPiScript
# https://docs.micropython.org/en/latest/rp2/quickref.html#timers
from machine import Timer, Pin

try:
    from ucollections import OrderedDict
except ImportError:
    from collections import OrderedDict
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio


TRIGGER_LENGTH = 20


class InternalClocks:
    """
    A group of clocks that can be reset
    """

    def __init__(self) -> None:
        self.timers = []
        for _ in cvs:
            self.timers.append(Timer(mode=Timer.PERIODIC))

    def reset(self, new_times=[], cb=None):
        for clock in self.timers:
            clock.deinit()
            if len(new_times):
                clock.init(period=int(new_times.pop(0)), callback=cb)

    def reset_one(self, idx, period=None, cb=None):
        self.timers[idx].deinit()
        if period and cb:
            self.timers[idx].init(period=period, callback=cb)


class ModeHandler:
    def __init__(self) -> None:
        self.current_mode = None
        self.modes = OrderedDict()
        self.mode_inits = {}
        self.mode_exits = {}

    def register_mode(self, mode, function=None):
        """
        register a mode, with an optional function to call
        """
        self.modes[mode] = function
        if not self.current_mode:
            self.current_mode = mode

    def register_mode_init(self, mode, function):
        """
        init functions should take no arguments
        """
        if mode not in self.modes:
            self.register_mode(mode)
        self.mode_inits[mode] = function

    def register_mode_exit(self, mode, function):
        """
        exit functions should take no arguments
        """
        if mode not in self.modes:
            self.register_mode(mode)
        self.mode_exits[mode] = function

    def __call__(self, idx):
        print(f"Called for {self.current_mode} for index {idx}")
        if self.modes[self.current_mode]:
            return self.modes[self.current_mode](idx)

    def __str__(self) -> str:
        return f"Current mode is {self.current_mode}. {len(self.modes)} modes available."

    def run_if(self, mode_dict):
        fn = mode_dict.get(self.current_mode, None)
        if fn:
            fn()

    def change_mode(self, mode):
        self.run_if(self.mode_exits)
        self.current_mode = mode
        self.run_if(self.mode_inits)
        print(self)

    def next(self):
        modes_list = list(self.modes.keys())
        current = modes_list.index(self.current_mode)
        if self.current_mode == modes_list[-1]:
            next = 0
        else:
            next = current + 1
        self.change_mode(modes_list[next])

    def reinit(self):
        self.run_if(self.mode_inits)


class ClockStateHelper:
    def __init__(self, times=[], indexes=[], max=16, func=None) -> None:
        self.times = times
        self.indexes = indexes
        self.count = 0
        self.max = 16
        if func:
            self.func = func

    def __call__(self, timer):
        self.count += 1
        if self.count >= self.max:
            self.count = 0
        self.func(timer, self)


internal_led = Pin(25, Pin.OUT)


def flash_led(func):
    def wrapper(*args, **kwargs):
        internal_led.on()
        func(*args, **kwargs)
        internal_led.off()

    return wrapper


class BigBen(EuroPiScript):
    def toggle_cv(self, cv_idx):
        print(f"Toggle cv {cv_idx} on @ {ticks_ms()}")
        cvs[cv_idx].on()
        asyncio.sleep_ms(TRIGGER_LENGTH)
        print(f"Toggle cv {cv_idx} off @ {ticks_ms()}")
        cvs[cv_idx].off()

    def __init__(self):
        super().__init__()

        self.quarter = 0
        self.tempo_samples = []
        self.tasks = {}
        self.internal_clocks = InternalClocks()
        self.modes = ModeHandler()

        self.setup_handlers()

    def mode_button(self):
        print(f"Mode button! {self.modes}")
        self.modes.next()

    def get_period(self):
        return int(self.quarter * (4 / self.clock_division()))

    @flash_led
    def setup_handlers(self):
        din.handler(self.measure_tempo)
        b1.handler(self.measure_tempo)
        b2.handler(self.mode_button)

        # divmult is in how the clocks are set up, the action is just to toggle
        self.modes.register_mode("divmult", self.toggle_cv)
        self.modes.register_mode_init("divmult", self.init_divmult)
        self.modes.register_mode_exit("divmult", self.exit_divmult)

        self.modes.register_mode("dilla", self.toggle_cv)
        self.modes.register_mode_init("dilla", self.init_generic)
        self.modes.register_mode_exit("dilla", self.exit_dilla)

        self.modes.register_mode("random", self.random)
        self.modes.register_mode_init("random", self.init_generic)

        self.modes.register_mode("burst", self.burst)
        self.modes.register_mode_init("burst", self.burst_init)
        self.modes.register_mode_exit("burst", self.burst_exit)

    def burst_init(self):
        self.internal_clocks.reset()
        if not self.quarter > 0:
            print("No tempo")
            return

        period = self.get_period()

        evens = ClockStateHelper(times=[2, 4, 8, 16], indexes=[5, 3, 1, 0], func=self.burst)
        three = ClockStateHelper(times=[3], indexes=[2], func=self.burst)
        five = ClockStateHelper(times=[5], indexes=[4], func=self.burst)

        self.internal_clocks.reset(new_times=[int(period / 16)], cb=evens)
        self.internal_clocks.reset_one(2, period=int(period / 3), cb=three)
        self.internal_clocks.reset_one(4, period=int(period / 5), cb=five)

    @flash_led
    def burst(self, _, helper):
        in_threshold = k2.percent() * 100
        for c, i in enumerate(helper.indexes):
            cv_threshold = 15 + 10 * (i + 1)
            if in_threshold > cv_threshold and not helper.count % helper.times[c]:
                print(f"Burst toggling {i} for count {helper.count}, {c}!")
                self.toggle_cv(i)
        sleep_ms(TRIGGER_LENGTH)
        for i in helper.indexes:
            cvs[i].off()

    def burst_exit(self):
        self.internal_clocks.reset()

    def init_divmult(self):
        if self.quarter > 0:
            period = int(self.get_period() / 8)
            evens = ClockStateHelper(
                times=[32, 16, 8, 4, 2, 1], max=64, indexes=[0, 1, 2, 3, 4, 5], func=self.divmult
            )
            self.internal_clocks.reset(new_times=[period], cb=evens)
        else:
            print("No tempo")
            self.internal_clocks.reset()

    @flash_led
    def divmult(self, _, helper):
        for c, i in enumerate(helper.indexes):
            if not helper.count % helper.times[c]:
                self.toggle_cv(i)
        sleep_ms(TRIGGER_LENGTH)
        for i in helper.indexes:
            cvs[i].off()

    def exit_divmult(self):
        print("Exit divmult")
        self.internal_clocks.reset()

    def init_generic(self):
        self.internal_clocks.reset()
        if self.quarter > 0:
            # init a single clock at the tempo
            times = [self.get_period()]
            self.internal_clocks.reset(new_times=times, cb=self.triggered)
        else:
            print("No tempo")

    def dilla(self, idx):
        print("wobble")

    def exit_dilla(self):
        print("Exit dilla")
        self.internal_clocks.reset()

    def random(self, idx):
        """
        Random gates over all six outputs, roughly aligned to the clock
        """
        seed = str(random()).replace("0.", "")
        for i, cv in enumerate(cvs):
            comp = int(seed[i]) % (i + 1)
            if not comp or comp == i:
                print(f"cv {i} on for seed {seed}:{comp}")
                cv.on()
        sleep_ms(TRIGGER_LENGTH)
        turn_off_all_cvs()

    def measure_tempo(self):
        self.tempo_samples.append(ticks_ms())
        if len(self.tempo_samples) >= 4:
            total_time = self.tempo_samples[-1] - self.tempo_samples[-4]
            self.quarter = total_time
            print(f"tempo measured! {total_time} {self.quarter} {self.tempo_bpm():.2f}")
            self.modes.reinit()
            self.tempo_samples = []

    @flash_led
    def triggered(self, timer):
        i = self.internal_clocks.timers.index(timer)
        print(f"Timer {i} triggered")
        self.modes(i)

    def clock_division(self):
        return k1.choice([1, 2, 3, 4, 5, 6, 7, 8, 16, 32])

    def display_name(self):
        title = f"BigBen : {self.modes.current_mode}"
        if self.quarter > 0:
            return f"{title}\n{4 * self.tempo_bpm():.2f} - {self.clock_division()}"
        else:
            return f"{title}\nNo BPM - {self.clock_division()}"

    def tempo_bpm(self):
        return 60000 / self.quarter

    def main(self):
        old = 1
        while True:
            if self.clock_division() != old:
                self.modes.reinit()
                old = self.clock_division()

            oled.centre_text(self.display_name())


if __name__ == "__main__":
    BigBen().main()
