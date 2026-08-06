"""
audio.py

Alert sounds for timers. Two tiers:

- beep(): always available, no extra install -- uses winsound on Windows
  (SWTOR is Windows-only, so this is the common case), falls back to the
  terminal bell character elsewhere (e.g. if you're developing on Mac/Linux).
- speak(text): announces the timer's label out loud (e.g. "Slam incoming")
  via SAPI5 (win32com.client.Dispatch("SAPI.SpVoice")) if available, since
  hearing a name is far more useful mid-raid than a generic beep telling
  you *something* fired. Falls back to beep() if win32com isn't available.

Used to go through pyttsx3 instead. Dropped it after finding a reproducible
bug: on Windows, pyttsx3's SAPI5 driver only ever produces real audio for
the FIRST utterance in a process -- every runAndWait() call after that
returns in ~0.07s with no exception and no sound, whether the engine is
freshly re-created each call (pyttsx3.init() every time, what this used to
do) or reused (one engine, called repeatedly). Confirmed by timing: first
call ~4-5s (real speech), every later call in the same process ~0.07s
regardless of which pattern. This surfaced as "no boss callouts ever spoken
during a whole raid" -- the very first alert of a session may have worked
(or been missed/unnoticed), and every one after it was silently a no-op.

Calling SAPI5 directly via win32com.client sidesteps pyttsx3's bug, but
needed two things pyttsx3's wrapper was quietly doing for us: speech runs
off the main thread (so a slow utterance never stalls the GUI), and COM is
apartment-threaded -- a thread that never calls pythoncom.CoInitialize()
can hit "CoInitialize has not been called" the moment it touches a COM
object (win32com.client.Dispatch sometimes gets away without it, but not
reliably -- confirmed failing under concurrent calls specifically, i.e.
two alerts landing close together, exactly the case that matters).
Explicit CoInitialize()/CoUninitialize() around the Dispatch()+Speak()
call fixes that.

speak() used to spawn a brand new thread per call, each one racing to
grab a lock around the actual Speak(). That serialized the AUDIO (no more
overlapping/garbled speech) but not the ORDER: nothing guaranteed the
thread for an earlier alert would win the race and speak first, so two
alerts landing close together (a warn-before firing right as the next
mechanic's timer starts, say) could come out of the speakers in the
wrong order relative to when they actually fired -- reported live as
"they are talking but wrong times." Fixed by routing every speak() call
through one FIFO queue.Queue(), drained by a single persistent worker
thread -- callers still return instantly (queueing is non-blocking), but
now only one utterance is ever being dispatched at a time and strictly in
call order, so the lock that used to guard the race is no longer needed
(a single consumer can't race with itself).
"""

import queue
import sys
import threading
from typing import Optional

try:
    import winsound
    _HAS_WINSOUND = True
except ImportError:
    _HAS_WINSOUND = False

try:
    import pythoncom
    import win32com.client
    _HAS_SAPI = True
except ImportError:
    _HAS_SAPI = False

_alert_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()  # (kind, payload) -- kind in ("speak", "wav")
_worker_started = False
_worker_start_lock = threading.Lock()

# Global + per-category mute, applied live (see apply_settings) so a
# setting change takes effect on the very next alert without a restart.
# Reported by a tester: mechanic alerts fired every few seconds with no
# in-app way to turn them down, only Windows' own volume mixer.
_muted = False
_category_muted: dict = {}
_layer_muted: dict = {}
_encounter_muted: dict = {}


def apply_settings(settings: dict) -> None:
    """settings: the dict shape storage.load_audio_settings() returns.
    Called once at startup and again whenever the web UI saves a change,
    so a toggle takes effect on the very next alert without a restart.

    Four independent levels, requested live as "sound buttons in every
    boss encounter or in individual layers ... in the settings too":
      muted           -- everything, one switch
      category_muted  -- boss mechanics / phase changes / custom timers
      layer_muted     -- per overlay frame (timers, alerts, cooldowns, dots)
      encounter_muted -- per boss encounter, keyed by definition id
    Any one of them silencing a callout is enough; they're ANDed, not
    ranked, so there's no surprising precedence to reason about."""
    global _muted, _category_muted, _layer_muted, _encounter_muted
    _muted = bool(settings.get("muted", False))
    _category_muted = dict(settings.get("category_muted", {}))
    _layer_muted = dict(settings.get("layer_muted", {}))
    _encounter_muted = dict(settings.get("encounter_muted", {}))


def current_settings() -> dict:
    """What's actually in force right now -- so the UI reflects the live
    state rather than re-reading the file and hoping they agree."""
    return {
        "muted": _muted,
        "category_muted": dict(_category_muted),
        "layer_muted": dict(_layer_muted),
        "encounter_muted": dict(_encounter_muted),
    }


def should_announce(category: Optional[str] = None, layer: Optional[str] = None,
                    boss_id: Optional[str] = None) -> bool:
    """False if ANY applicable mute is on. A None argument simply isn't
    checked -- callers that don't know a dimension (legacy call sites, or
    a timer with no boss) are never silenced by that dimension alone."""
    if _muted:
        return False
    if category is not None and _category_muted.get(category, False):
        return False
    if layer is not None and _layer_muted.get(layer, False):
        return False
    if boss_id is not None and _encounter_muted.get(boss_id, False):
        return False
    return True


def beep(frequency: int = 880, duration_ms: int = 200) -> None:
    if _HAS_WINSOUND:
        try:
            winsound.Beep(frequency, duration_ms)
            return
        except RuntimeError:
            pass
    # Fallback: terminal bell (audible if a console window has focus/sound
    # enabled; silent no-op otherwise, but never raises)
    sys.stdout.write("\a")
    sys.stdout.flush()


def _speak_one(text: str) -> None:
    if _HAS_SAPI:
        try:
            pythoncom.CoInitialize()
            try:
                win32com.client.Dispatch("SAPI.SpVoice").Speak(text)
                return
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            pass
    beep()


def _play_wav_one(path: str) -> None:
    if _HAS_WINSOUND:
        try:
            # No SND_ASYNC: this already runs on the dedicated worker
            # thread below, so blocking here doesn't stall any caller --
            # and blocking is what keeps a wav and a queued speech line
            # from overlapping/garbling each other, same reasoning as
            # _speak_one's synchronous Speak() call.
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        except RuntimeError:
            pass  # bad/missing file -- fall through to a beep instead of silently doing nothing
    beep()


def _alert_worker() -> None:
    while True:
        kind, payload = _alert_queue.get()
        if kind == "wav":
            _play_wav_one(payload)
        else:
            _speak_one(payload)


def _ensure_worker_started() -> None:
    global _worker_started
    with _worker_start_lock:
        if not _worker_started:
            threading.Thread(target=_alert_worker, daemon=True).start()
            _worker_started = True


def speak(text: str, category: Optional[str] = None, layer: Optional[str] = None,
          boss_id: Optional[str] = None) -> None:
    """Queues text to be spoken; returns instantly so a slow (or backlogged)
    TTS engine never stalls the caller. A single worker thread drains the
    queue in strict FIFO order, so alerts are always heard in the order
    they actually fired. No-ops if muted globally or for this category
    (see apply_settings/should_announce)."""
    if not should_announce(category, layer, boss_id):
        return
    _ensure_worker_started()
    _alert_queue.put(("speak", text))


def play_wav(path: str, category: Optional[str] = None, layer: Optional[str] = None,
             boss_id: Optional[str] = None) -> None:
    """Queues a .wav file to be played -- same ordering/non-blocking
    guarantees as speak(), and shares its queue/worker so a wav-triggered
    alert and a spoken one landing close together still play strictly in
    the order they actually fired instead of racing each other. No-ops if
    muted globally or for this category."""
    if not should_announce(category, layer, boss_id):
        return
    _ensure_worker_started()
    _alert_queue.put(("wav", path))
