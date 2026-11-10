import os
import platform
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aw_watcher_afk.afk import AFKWatcher

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
POLL_TIME = 5
TIMEOUT = 180
# ping() passes timeout + poll_time as the heartbeat pulsetime.
PULSETIME = TIMEOUT + POLL_TIME
TD1MS = timedelta(milliseconds=1)


class BucketModel:
    """In-memory port of the aw-server heartbeat merge rule.

    Source: aw-server-rust/aw-transform/src/heartbeat.rs. A heartbeat merges
    into the last event only if the data is equal and its timestamp lies in
    [last.start, last.end + pulsetime]. Otherwise it is stored as a new event.
    A same-data heartbeat that starts before the last event is counted in
    `out_of_order`: it is stored separately, which is the duplicate bug (#61).
    """

    def __init__(self):
        self.events = []
        self.out_of_order = 0

    def heartbeat(self, data, start, duration, pulsetime):
        end = start + timedelta(seconds=duration)
        if self.events:
            last = self.events[-1]
            if last["data"] == data:
                if last["start"] > start:
                    self.out_of_order += 1
                elif start <= last["end"] + timedelta(seconds=pulsetime):
                    last["end"] = max(last["end"], end)
                    return
        self.events.append({"start": start, "end": end, "data": data})

    def intervals(self, status):
        return [
            (e["start"], e["end"]) for e in self.events if e["data"]["status"] == status
        ]


def replay(ping_calls):
    bucket = BucketModel()
    for c in ping_calls:
        data = {"status": "afk" if c.args[0] else "not-afk"}
        bucket.heartbeat(
            data,
            c.kwargs["timestamp"],
            c.kwargs.get("duration", 0),
            PULSETIME,
        )
    return bucket


class FakeWorld:
    """Fake machine: wall clock, sleep-inclusive clock and idle counter.

    steps[i] = (input_happened, pause_seconds). Sample i happens one poll
    interval after sample i-1, plus the pause. A pause advances the wall clock,
    the sleep-inclusive clock and the idle counter, as a real suspend does.
    """

    def __init__(self, steps):
        assert steps[0] == (True, 0), "the first sample must start with input"
        self.steps = list(steps)
        self.index = 0
        self.now = NOW
        self.last_input = NOW

    def idle(self):
        return (self.now - self.last_input).total_seconds()

    def clock(self):
        return (self.now - NOW).total_seconds()

    def sleep(self, seconds):
        self.index += 1
        if self.index >= len(self.steps):
            raise KeyboardInterrupt
        self.now += timedelta(seconds=seconds)
        input_happened, pause = self.steps[self.index]
        self.now += timedelta(seconds=pause)
        if input_happened:
            self.last_input = self.now


class SleepGapTests(unittest.TestCase):
    def run_steps(self, steps):
        world = FakeWorld(steps)
        watcher = object.__new__(AFKWatcher)
        watcher.settings = SimpleNamespace(timeout=TIMEOUT, poll_time=POLL_TIME)
        watcher._initial_ppid = os.getppid()
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "aw_watcher_afk.afk.datetime",
                    SimpleNamespace(now=lambda tz=None: world.now),
                )
            )
            stack.enter_context(
                patch("aw_watcher_afk.afk.seconds_since_last_input", new=world.idle)
            )
            stack.enter_context(
                patch("aw_watcher_afk.afk.elapsed_clock", new=world.clock)
            )
            stack.enter_context(
                patch("aw_watcher_afk.afk.is_screen_locked", new=lambda: False)
            )
            stack.enter_context(patch("aw_watcher_afk.afk.sleep", new=world.sleep))
            ping = stack.enter_context(patch.object(watcher, "ping"))
            watcher.heartbeat_loop()
        return ping.call_args_list

    def assert_interval(self, actual, start, end):
        self.assertEqual(len(actual), 1, f"expected one AFK interval, got {actual}")
        s, e = actual[0]
        self.assertLessEqual(
            abs((s - (NOW + timedelta(seconds=start))).total_seconds()), 0.01
        )
        self.assertLessEqual(
            abs((e - (NOW + timedelta(seconds=end))).total_seconds()), 0.01
        )

    def test_sleep_while_active_user_returns_at_resume(self):
        # Active, then the machine sleeps 600s between samples at t=10 and t=615.
        # The gap must be AFK from the last sample (t=10), not from the resume.
        steps = [(True, 0), (False, 0), (False, 0), (True, 600), (False, 0)]
        bucket = replay(self.run_steps(steps))

        self.assertEqual(bucket.out_of_order, 0)
        self.assert_interval(bucket.intervals("afk"), start=10, end=615)

    def test_sleep_then_still_away_stays_afk_until_input(self):
        steps = [
            (True, 0),
            (False, 0),
            (False, 0),
            (False, 600),
            (False, 0),
            (True, 0),
            (False, 0),
        ]
        bucket = replay(self.run_steps(steps))

        self.assertEqual(bucket.out_of_order, 0)
        # Input at t=625 ends the AFK period at that input.
        self.assert_interval(bucket.intervals("afk"), start=10, end=625)

    def test_sleep_while_already_afk_extends_the_afk_event(self):
        # Idle past the 180s timeout (AFK from t=0), then a 600s sleep, then input.
        steps = [(True, 0)] + [(False, 0)] * 40 + [(False, 600), (True, 0)]
        bucket = replay(self.run_steps(steps))

        self.assertEqual(bucket.out_of_order, 0)
        afk = bucket.intervals("afk")
        self.assertEqual(len(afk), 1, f"sleep should not split AFK: {afk}")
        self.assertLessEqual(afk[0][0], NOW + timedelta(seconds=1))
        self.assertGreaterEqual(afk[0][1], NOW + timedelta(seconds=800))

    def test_jitter_below_threshold_is_not_a_gap(self):
        # A 5s delay on one poll is below 3 * POLL_TIME of extra time: no gap.
        with_jitter = self.run_steps([(True, 0), (False, 0), (False, 5), (False, 0)])
        without = self.run_steps([(True, 0), (False, 0), (False, 0), (False, 0)])

        self.assertEqual(
            [c.args[0] for c in with_jitter],
            [c.args[0] for c in without],
        )
        self.assertEqual(len(with_jitter), len(without))


class UnixClockTests(unittest.TestCase):
    @unittest.skipUnless(platform.system() == "Linux", "CLOCK_BOOTTIME is Linux-only")
    def test_elapsed_clock_is_monotonic_seconds(self):
        from aw_watcher_afk.unix import elapsed_clock

        first = elapsed_clock()
        second = elapsed_clock()
        self.assertIsInstance(first, float)
        self.assertGreaterEqual(second, first)


if __name__ == "__main__":
    unittest.main()
