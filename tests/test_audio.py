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


def test_global_mute_silences_speak_and_play_wav(monkeypatch):
    """Reported by a tester: mechanic alerts fired every few seconds with
    no in-app way to turn them off, only Windows' own volume mixer."""
    audio = _reload_audio()
    spoken = []
    played = []
    monkeypatch.setattr(audio, "_speak_one", lambda text: spoken.append(text))
    monkeypatch.setattr(audio, "_play_wav_one", lambda path: played.append(path))

    audio.apply_settings({"muted": True, "category_muted": {}})
    audio.speak("hello")
    audio.play_wav("alert.wav")

    time.sleep(0.1)
    assert spoken == []
    assert played == []


def test_category_mute_only_silences_that_category(monkeypatch):
    audio = _reload_audio()
    spoken = []
    monkeypatch.setattr(audio, "_speak_one", lambda text: spoken.append(text))

    audio.apply_settings({"muted": False, "category_muted": {"boss": True}})
    audio.speak("muted one", category="boss")
    audio.speak("audible one", category="phase")

    deadline = time.time() + 2.0
    while len(spoken) < 1 and time.time() < deadline:
        time.sleep(0.02)

    assert spoken == ["audible one"]


def test_unmuted_settings_allow_sound_through(monkeypatch):
    audio = _reload_audio()
    spoken = []
    monkeypatch.setattr(audio, "_speak_one", lambda text: spoken.append(text))

    audio.apply_settings({"muted": False, "category_muted": {"boss": False}})
    audio.speak("hello", category="boss")

    deadline = time.time() + 2.0
    while len(spoken) < 1 and time.time() < deadline:
        time.sleep(0.02)

    assert spoken == ["hello"]


class TestLayerAndEncounterMute:
    """The tester's request was three-part: "Sound buttons in every boss
    encounter or in individual layers would be a must. In the settings
    too." Only the settings part shipped first time round. These cover the
    two that were missing, plus that all four levels compose."""

    def _audio(self, monkeypatch, **settings):
        audio = _reload_audio()
        spoken = []
        monkeypatch.setattr(audio, "_speak_one", lambda t: spoken.append(t))
        base = {"muted": False, "category_muted": {}, "layer_muted": {}, "encounter_muted": {}}
        base.update(settings)
        audio.apply_settings(base)
        return audio, spoken

    def _drain(self, spoken, expect):
        deadline = time.time() + 2.0
        while len(spoken) < expect and time.time() < deadline:
            time.sleep(0.02)

    def test_muting_a_layer_silences_only_that_layer(self, monkeypatch):
        audio, spoken = self._audio(monkeypatch, layer_muted={"cooldowns": True})
        audio.speak("cd", layer="cooldowns")
        audio.speak("mech", layer="timers")
        self._drain(spoken, 1)
        assert spoken == ["mech"]

    def test_muting_an_encounter_silences_only_that_boss(self, monkeypatch):
        audio, spoken = self._audio(monkeypatch, encounter_muted={"styrak_k": True})
        audio.speak("styrak thing", boss_id="styrak_k")
        audio.speak("brontes thing", boss_id="brontes")
        self._drain(spoken, 1)
        assert spoken == ["brontes thing"]

    def test_an_untagged_callout_is_never_silenced_by_a_finer_toggle(self, monkeypatch):
        """A custom timer has no boss and a legacy caller may pass no layer.
        Neither should be collateral damage of a per-boss/per-layer mute."""
        audio, spoken = self._audio(
            monkeypatch, layer_muted={"timers": True}, encounter_muted={"styrak_k": True})
        audio.speak("plain call")           # no layer, no boss_id
        self._drain(spoken, 1)
        assert spoken == ["plain call"]

    def test_global_mute_still_beats_everything(self, monkeypatch):
        audio, spoken = self._audio(monkeypatch, muted=True)
        audio.speak("x", category="boss", layer="timers", boss_id="styrak_k")
        time.sleep(0.1)
        assert spoken == []

    def test_levels_are_anded_not_ranked(self, monkeypatch):
        """Any one mute is enough; no level "unmutes" another."""
        audio, spoken = self._audio(monkeypatch, category_muted={"boss": True})
        # layer and encounter both permit it, category does not
        audio.speak("nope", category="boss", layer="timers", boss_id="brontes")
        time.sleep(0.1)
        assert spoken == []

    def test_current_settings_reports_what_is_in_force(self, monkeypatch):
        audio, _ = self._audio(monkeypatch, layer_muted={"dots": True},
                                encounter_muted={"soa": True})
        cur = audio.current_settings()
        assert cur["layer_muted"]["dots"] is True
        assert cur["encounter_muted"]["soa"] is True
