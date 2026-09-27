import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def timer_module(monkeypatch):
    class FakeTimer:
        def __init__(self):
            self.callback = []

        def start(self, delay, single_shot=False):
            self.delay = delay
            self.single_shot = single_shot

    # Only the event-loop timer is native; exercise the real scheduling code.
    monkeypatch.setitem(sys.modules, "enigma", SimpleNamespace(eTimer=FakeTimer))
    source = Path(__file__).resolve().parents[1] / "lib/python/timer.py"
    spec = importlib.util.spec_from_file_location("timer_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with monkeypatch.context() as timezone:
        timezone.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
        time.tzset()
        try:
            yield module
        finally:
            timezone.undo()
            time.tzset()


def timestamp(year, month, day, hour, minute=0):
    return int(time.mktime((year, month, day, hour, minute, 0, -1, -1, -1)))


@pytest.mark.parametrize("start, expected", [
    ((2007, 3, 24, 4), (2007, 3, 25, 4)),
    ((2007, 10, 27, 4), (2007, 10, 28, 4)),
    # 02:00 does not exist on the spring transition day: skip that day.
    ((2007, 3, 24, 2), (2007, 3, 26, 2)),
])
def test_repeated_timer_preserves_local_time(timer_module, monkeypatch, start, expected):
    begin = timestamp(*start)
    entry = timer_module.TimerEntry(begin, begin + 1000)
    entry.repeated = 0x7f
    monkeypatch.setattr(timer_module, "time", lambda: entry.end + 1)
    entry.processRepeated()
    assert entry.begin == timestamp(*expected)
    assert entry.end - entry.begin == 1000


def test_repeated_timer_respects_weekdays(timer_module, monkeypatch):
    begin = timestamp(2007, 3, 23, 12)  # Friday
    entry = timer_module.TimerEntry(begin, begin + 1000)
    for weekday in range(5):
        entry.setRepeated(weekday)
    monkeypatch.setattr(timer_module, "time", lambda: begin + 1001)
    entry.processRepeated()
    assert entry.begin == timestamp(2007, 3, 26, 12)  # Monday after DST
    assert entry.end - entry.begin == 1000


def test_one_shot_timer_completes_once(timer_module, monkeypatch):
    now = [timestamp(2007, 3, 20, 12)]
    monkeypatch.setattr(timer_module, "time", lambda: now[0])
    activations = []

    class Entry(timer_module.TimerEntry):
        def getNextActivation(self):
            return (self.begin - self.prepare_time, self.begin, self.end)[self.state]

        def activate(self):
            activations.append(self.state)
            return True

    scheduler = timer_module.Timer()
    entry = Entry(now[0] + 3600, now[0] + 4600)
    scheduler.addTimerEntry(entry)
    for activation in (entry.begin - entry.prepare_time, entry.begin, entry.end):
        now[0] = activation
        scheduler.calcNextActivation()
    assert activations == [entry.StateWaiting, entry.StatePrepared, entry.StateRunning]
    assert entry.state == entry.StateEnded
    assert scheduler.timer_list == []
    assert scheduler.processed_timers == [entry]
    now[0] += 86400
    scheduler.calcNextActivation()
    assert len(activations) == 3
    assert scheduler.processed_timers == [entry]


def test_disabled_timer_does_not_activate(timer_module, monkeypatch):
    now = timestamp(2007, 3, 20, 12)
    monkeypatch.setattr(timer_module, "time", lambda: now)
    scheduler = timer_module.Timer()
    entry = timer_module.TimerEntry(now + 3600, now + 4600)
    entry.disable()
    scheduler.addTimerEntry(entry)
    assert scheduler.timer_list == []
    assert scheduler.processed_timers == [entry]
    assert entry.state == entry.StateEnded
