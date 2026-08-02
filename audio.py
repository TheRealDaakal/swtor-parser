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
needed two things pyttsx3's wrapper was quietly doing for us: each speak()
call runs on its OWN fresh background thread (so a slow utterance never
stalls the GUI), and COM is apartment-threaded -- a thread that never calls
pythoncom.CoInitialize() can hit "CoInitialize has not been called" the
moment it touches a COM object (win32com.client.Dispatch sometimes gets
away without it, but not reliably -- confirmed failing under concurrent
calls specifically, i.e. two alerts landing close together, exactly the
case that matters). Explicit CoInitialize()/CoUninitialize() around the
Dispatch()+Speak() call fixes that. Concurrent calls also hit a SECOND,
separate failure without serialization -- two SpVoice.Speak() calls
racing for the audio device at once -- which is what _tts_lock guards
against (not just "avoid overlapping/garbled speech," it's load-bearing).
"""

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

# Serializes actual speech: two alerts firing close together must be
# spoken one at a time, not just for clarity -- concurrent SpVoice.Speak()
# calls racing for the audio device can throw outright (see module
# docstring), so this is load-bearing, not cosmetic.
_tts_lock = threading.Lock()


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


def speak(text: str) -> None:
    """Runs off the main thread so a slow TTS engine never stalls the GUI."""
    def _run():
        if _HAS_SAPI:
            try:
                pythoncom.CoInitialize()
                try:
                    with _tts_lock:
                        win32com.client.Dispatch("SAPI.SpVoice").Speak(text)
                    return
                finally:
                    pythoncom.CoUninitialize()
            except Exception:
                pass
        beep()

    threading.Thread(target=_run, daemon=True).start()
