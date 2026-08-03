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

_tts_queue: "queue.Queue[str]" = queue.Queue()
_worker_started = False
_worker_start_lock = threading.Lock()


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


def _tts_worker() -> None:
    while True:
        text = _tts_queue.get()
        _speak_one(text)


def _ensure_worker_started() -> None:
    global _worker_started
    with _worker_start_lock:
        if not _worker_started:
            threading.Thread(target=_tts_worker, daemon=True).start()
            _worker_started = True


def speak(text: str) -> None:
    """Queues text to be spoken; returns instantly so a slow (or backlogged)
    TTS engine never stalls the caller. A single worker thread drains the
    queue in strict FIFO order, so alerts are always heard in the order
    they actually fired."""
    _ensure_worker_started()
    _tts_queue.put(text)
