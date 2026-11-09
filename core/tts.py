"""core/tts.py - Text-to-Speech & Audio FX for AuraShell-OS (Ubuntu).

Public API:
    speak(text)             -> speaks the text out loud (blocking)
    play_listen_start()     -> rising sci-fi chirp (system starts listening)
    play_listen_end()       -> falling sci-fi chirp (system stopped listening)
"""

import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import wave

SAMPLE_RATE = 44100
SPEECH_RATE = 165
SPEECH_VOLUME = 1.0

_SOUND_DIR = os.path.join(tempfile.gettempdir(), "aurashell_sounds")
_speech_lock = threading.Lock()
_engine = None


def _build_tone(path, freq_start, freq_end, duration=0.2, volume=0.35):
    """Generate a short sweeping 'sci-fi' beep and save it as a WAV file."""
    total = int(SAMPLE_RATE * duration)
    phase = 0.0
    frames = bytearray()
    for i in range(total):
        progress = i / total
        freq = freq_start + (freq_end - freq_start) * progress
        phase += 2 * math.pi * freq / SAMPLE_RATE
        wave_value = (math.sin(phase) + 0.4 * math.sin(2 * phase)) / 1.4
        fade = math.sin(math.pi * progress)
        frames += struct.pack("<h", int(wave_value * fade * volume * 32767))

    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(frames))


def _get_sound(name, freq_start, freq_end):
    """Return the path of a tone file, creating it on first use."""
    os.makedirs(_SOUND_DIR, exist_ok=True)
    path = os.path.join(_SOUND_DIR, f"{name}.wav")
    if not os.path.exists(path):
        _build_tone(path, freq_start, freq_end)
    return path


def _play_file(path):
    """Play a WAV file with whichever player exists on the system."""
    for player in ("paplay", "aplay"):
        exe = shutil.which(player)
        if exe:
            cmd = [exe, path] if player == "paplay" else [exe, "-q", path]
            subprocess.run(cmd, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def play_listen_start():
    """Rising chirp: the system is now listening."""
    _play_file(_get_sound("listen_start", 400, 1200))


def play_listen_end():
    """Falling chirp: the system finished listening."""
    _play_file(_get_sound("listen_end", 1200, 400))


def _get_engine():
    """Create the pyttsx3 engine once and reuse it."""
    global _engine
    if _engine is None:
        import pyttsx3
        _engine = pyttsx3.init()
        _engine.setProperty("rate", SPEECH_RATE)
        _engine.setProperty("volume", SPEECH_VOLUME)
    return _engine


def speak(text):
    """Speak `text` out loud. Blocks until finished. Returns True on success."""
    text = (text or "").strip()
    if not text:
        return False

    with _speech_lock:
        try:
            engine = _get_engine()
            engine.say(text)
            engine.runAndWait()
            return True
        except Exception as error:
            print(f"[TTS] Error: {error}")
            return False


if __name__ == "__main__":
    play_listen_start()
    speak("Aura Shell online. How can I help you?")
    play_listen_end()
