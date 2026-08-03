import importlib
import threading
import time


def _reload_audio():
    import audio
    importlib.reload(audio)
    return audio


def test_speak_calls_are_spoken_in_call_order_even_when_earlier_ones_are_slower(monkeypatch):
    """Reported live as "they are talking but wrong times": alerts landing
    close together could come out of the speakers in a different order than
    they actually fired, because the old implementation spawned a brand new
    thread per speak() call and let them race for a lock -- nothing
    guaranteed the earlier call's thread would win that race. Simulates the
    exact race: the first call's "utterance" takes far longer than the
    second's, which would flip the order under a naive
    thread-per-call-races-for-a-lock design."""
    audio = _reload_audio()

    spoken = []
    spoken_lock = threading.Lock()

    def fake_speak_one(text):
        # First call sleeps longest -- if anything lets calls run
        # concurrently/out of order, "second" would be recorded before
        # "first".
        if text == "first":
            time.sleep(0.15)
        with spoken_lock:
            spoken.append(text)

    monkeypatch.setattr(audio, "_speak_one", fake_speak_one)

    audio.speak("first")
    audio.speak("second")
    audio.speak("third")

    # Give the single worker thread time to drain the queue.
    deadline = time.time() + 2.0
    while len(spoken) < 3 and time.time() < deadline:
        time.sleep(0.02)

    assert spoken == ["first", "second", "third"]


def test_speak_returns_instantly_without_waiting_for_the_utterance(monkeypatch):
    audio = _reload_audio()

    def slow_speak_one(text):
        time.sleep(0.2)

    monkeypatch.setattr(audio, "_speak_one", slow_speak_one)

    started = time.time()
    audio.speak("hello")
    elapsed = time.time() - started

    assert elapsed < 0.1, "speak() must queue and return immediately, not block on the utterance"
