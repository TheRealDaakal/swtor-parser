"""
audio.py

Alert sounds for timers. Two tiers:

- beep(): always available, no extra install -- uses winsound on Windows
  (SWTOR is Windows-only, so this is the common case), falls back to the
  terminal bell character elsewhere (e.g. if you're developing on Mac/Linux).
- speak(text): announces the timer's label out loud (e.g. "Slam incoming")
  using pyttsx3 if it's installed, since hearing a name is far more useful
  mid-raid than a generic beep telling you *something* fired. Falls back to
  beep() if pyttsx3 isn't available.

pyttsx3 is optional -- install with:  pip install pyttsx3
"""

import sys
import threading

try:
    import winsound
    _HAS_WINSOUND = True
except ImportError:
    _HAS_WINSOUND = False

try:
    import pyttsx3
    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False

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
        if _HAS_PYTTSX3:
            try:
                with _tts_lock:
                    engine = pyttsx3.init()
                    engine.say(text)
                    engine.runAndWait()
                return
            except Exception:
                pass
        beep()

    threading.Thread(target=_run, daemon=True).start()
