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

MODES = ["DivMult", "Burst", "Dilla", "RandomSync"]
TRIGGER_LENGTH = 20


class TaskManager:
    def __init__(self, size=6) -> None:
        self.tasks = []
        self.size = size

    async def _wrap(self, fn):
        await fn

    def create(self, task_fn):
        task = asyncio.create_task(self._wrap(task_fn))
        if len(self.tasks) >= self.size:
            print(f"Too many tasks: {len(self.tasks)} ignoring {task}")
            self.status()
        else:
            self.tasks.append(task)
            print(self.status())

    def status(self):
        print(f"Have {len(self.tasks)} tasks")
        for t in self.tasks:
            print(t, t.state, t.done(), dir(t))
            if t.done():
                self.discard(t)

    def discard(self, task):
        print(f"discarding {task} {self.tasks.index(task)}")
        self.tasks.remove(task)

    async def run_internal(self):
        run = len(self.tasks) > 0
        while run:
            print("run internal")
            for t in self.tasks:
                await t
            self.status()
            run = len(self.tasks) > 0

    def run(self):
        asyncio.run(self.run_internal())

    def reset(self):
        for t in self.tasks:
            t.cancel()
        self.tasks = []


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

    def reset_one(self, idx):
        self.timers[idx].deinit()


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

    def __call__(self) -> Any:
        print(f"Called for {self.current_mode}")
        if self.modes[self.current_mode]:
            return self.modes[self.current_mode]()

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


class BigBen(EuroPiScript):
    def toggle_cv(self, cv):
        cv.on()
        asyncio.sleep_ms(TRIGGER_LENGTH)
        cv.off()

    def __init__(self):
        super().__init__()

        self.quarter = 0
        self.tempo_samples = []
        self.tasks = {}
        self.internal_clocks = InternalClocks()
        self.modes = ModeHandler()
        self.internal_led = Pin(25, Pin.OUT)

        self.setup_handlers()

    def setup_handlers(self):
        din.handler(self.measure_tempo)
        b1.handler(self.measure_tempo)
        b2.handler(self.mode_button)

        self.tasks["burst"] = TaskManager()

        self.modes.register_mode_init("divmult", self.init_divmult)
        self.modes.register_mode_init("dilla", self.init_dilla)

        self.modes.register_mode_exit("divmult", self.exit_divmult)
        self.modes.register_mode_exit("dilla", self.exit_dilla)

        self.modes.register_mode("random", self.random)
        self.modes.register_mode("burst", self.burst)
        self.modes.register_mode_init("burst", self.burst_init)
        self.modes.register_mode_exit("burst", self.burst_exit)

    def burst_init(self):
        self.tasks["burst"].reset()
        in_threshold = k2.percent() * 100
        print(ain.read_voltage())
        print(ain.range())
        print(f"should I burst? {in_threshold}")
        times = {1: 2, 2: 3, 3: 4, 4: 8, 5: 16}
        self.tasks["burst"].create(self.toggle_cv(cvs[0]))

        for i, cv in enumerate(cvs[1:]):
            cv_threshold = 15 + 10 * (i + 1)
            if in_threshold > cv_threshold:
                print(f"burst {i} (in_threshold = {in_threshold}, cv_threshold = {cv_threshold})!")
                self.tasks["burst"].create(self.burst_cv(cv, times[i + 1]))
            else:
                print(
                    f"no burst {i} (in_threshold = {in_threshold}, cv_threshold = {cv_threshold})!"
                )
                break
        self.tasks["burst"].run()

    def burst_exit(self):
        self.tasks["burst"].reset()

    def mode_button(self):
        print(f"Mode button! {self.modes}")
        self.modes.next()

    def init_divmult(self):
        if self.quarter > 0:
            period = int(self.quarter * (4 / self.clock_division()))
            times = [period, period * 2, period * 4, period / 2, period / 4, period / 8]
            self.internal_clocks.reset(new_times=times, cb=self.triggered)
        else:
            print("No tempo")

    def exit_divmult(self):
        print("Exit divmult")
        self.reset_internal_clocks()

    def dilla(self):
        print("wobble")

    def init_dilla(self):
        pass

    def exit_dilla(self):
        print("Exit dilla")
        self.reset_internal_clocks()

    def get_times(self):
        """
        Calculate the appropriate clock timings, with clock division read from knob 1 applied to the BPM.

        In DivMult mode, clocks are set to BPM, BPM * 2, BPM * 4, BPM * 8, BPM/2 & BPM/4
        """
        period = int(self.quarter * (4 / self.clock_division()))
        if self.modes.current_mode == "divmult":
            return [period, period * 2, period * 4, period / 2, period / 4, period / 8]
        else:
            return [period] * 6

    def reset_internal_clocks(self):
        """
        Stop the internal clocks and, if a tempo is set, start new ones.
        """
        if self.quarter > 0:
            self.internal_clocks.reset(new_times=self.get_times(), cb=self.triggered)
        else:
            self.internal_clocks.reset()

    def burst_cv(self, cv, times):
        # TODO: make async/non-blocking
        duration = self.quarter / times
        for _ in range(times):
            self.toggle_cv(cv)
            sleep_ms(int(duration))

    def measure_tempo(self):
        self.tempo_samples.append(ticks_ms())
        if len(self.tempo_samples) >= 4:
            total_time = self.tempo_samples[-1] - self.tempo_samples[-4]
            self.quarter = total_time
            print(f"tempo measured! {total_time} {self.quarter} {self.tempo_bpm():.2f}")
            self.modes.reinit()
            self.tempo_samples = []

    def random(self):
        """
        Random gates over all six outputs, roughly aligned to the clock
        """
        seed = random()
        for i, cv in enumerate(cvs):
            print(seed * (10 * i + 1))
            if i == int(seed * (10 * i + 1)):
                cv.on()
        sleep_ms(20)
        turn_off_all_cvs()

    def triggered(self, timer):
        i = self.internal_clocks.timers.index(timer)
        print("triggered", timer, i)
        self.modes()
        self.internal_led.on()
        sleep_ms(TRIGGER_LENGTH)
        self.internal_led.off()

    def clock_division(self):
        return k1.choice([1, 2, 3, 4, 5, 6, 7, 8, 16, 32])

    def display_name(self):
        title = f"BigBen : {self.modes.current_mode}"
        if self.quarter > 0:
            return f"{title}\n{self.tempo_bpm():.2f} - {self.clock_division()}"
        else:
            return f"{title}\nNo BPM - {self.clock_division()}"

    def tempo_bpm(self):
        return 60000 / self.quarter

    def main(self):
        old = 1
        while True:
            if self.clock_division() != old:
                self.reset_internal_clocks()
                old = self.clock_division()

            oled.centre_text(self.display_name())


if __name__ == "__main__":
    BigBen().main()
