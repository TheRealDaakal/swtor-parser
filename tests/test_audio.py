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


def test_play_wav_returns_instantly_without_waiting_for_playback(monkeypatch):
    audio = _reload_audio()

    def slow_play_one(path):
        time.sleep(0.2)

    monkeypatch.setattr(audio, "_play_wav_one", slow_play_one)

    started = time.time()
    audio.play_wav("alert.wav")
    elapsed = time.time() - started

    assert elapsed < 0.1, "play_wav() must queue and return immediately, not block on playback"


def test_speak_and_play_wav_share_one_queue_in_strict_call_order(monkeypatch):
    """The whole reason play_wav() shares speak()'s queue/worker instead of
    getting its own: a custom-sound alert and a spoken one landing close
    together (e.g. a Custom Timers rule with a .wav right next to a boss
    mechanic timer that still speaks) must not race each other or come out
    in the wrong order."""
    audio = _reload_audio()

    played = []
    played_lock = threading.Lock()

    def fake_speak_one(text):
        if text == "first":
            time.sleep(0.15)  # slowest -- would come out of order if racing
        with played_lock:
            played.append(("speak", text))

    def fake_play_wav_one(path):
        with played_lock:
            played.append(("wav", path))

    monkeypatch.setattr(audio, "_speak_one", fake_speak_one)
    monkeypatch.setattr(audio, "_play_wav_one", fake_play_wav_one)

    audio.speak("first")
    audio.play_wav("second.wav")
    audio.speak("third")

    deadline = time.time() + 2.0
    while len(played) < 3 and time.time() < deadline:
        time.sleep(0.02)

    assert played == [("speak", "first"), ("wav", "second.wav"), ("speak", "third")]


def test_play_wav_falls_back_to_beep_on_playback_failure(monkeypatch):
    """A missing/corrupt .wav file must not go silent -- winsound.PlaySound
    raises RuntimeError for a bad file, same as it does for a bad Beep()
    call, which beep() itself already falls back from."""
    audio = _reload_audio()

    beeped = []
    monkeypatch.setattr(audio, "beep", lambda *a, **kw: beeped.append(True))

    def raising_winsound_playsound(*a, **kw):
        raise RuntimeError("bad file")

    if audio._HAS_WINSOUND:
        monkeypatch.setattr(audio.winsound, "PlaySound", raising_winsound_playsound)
    audio._play_wav_one("nonexistent.wav")

    if audio._HAS_WINSOUND:
        assert beeped == [True]
