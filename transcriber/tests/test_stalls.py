import logging
import threading

from sdrtrunk_transcriber.stalls import StallWatch, thread_report


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_a_stuck_call_is_logged_once_with_its_step_and_the_threads(caplog):
    clock = Clock()
    watch = StallWatch(stuck_after_s=60, stuck_again_s=300, clock=clock)
    watch.begin(7)
    watch.step("Whisper")
    clock.now = 30
    watch.step("saving to the call log")
    with caplog.at_level(logging.WARNING):
        assert not watch.check()
        clock.now = 90
        assert watch.check()
        assert not watch.check()  # not again until stuck_again_s has passed
        clock.now = 400
        assert watch.check()
    text = caplog.records[0].getMessage()
    assert "call 7 has taken 90 s" in text and '60 s of it in the step "saving to the call log"' in text
    assert "What every part of the program is doing" in text


def test_a_slow_call_that_finished_logs_its_steps_and_quick_calls_log_nothing(caplog):
    clock = Clock()
    watch = StallWatch(slow_s=15, clock=clock)
    with caplog.at_level(logging.WARNING):
        watch.begin(1)
        watch.step("Whisper")
        clock.now = 2
        watch.end()
        assert not caplog.records
        watch.begin(2)
        watch.step("Whisper")
        clock.now = 4
        watch.step("after the call")
        clock.now = 40
        watch.end()
    assert "Call 2 took 38 s in all: Whisper 2.0 s, after the call 36.0 s" in caplog.records[0].getMessage()
    assert not watch.check()  # nothing in progress


def test_thread_report_names_the_other_threads():
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="waiting-thread")
    thread.start()
    try:
        report = thread_report()
    finally:
        release.set()
        thread.join()
    assert "--- waiting-thread ---" in report and "wait" in report
