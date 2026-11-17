import math
import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import wave
from pathlib import Path

SAMPLE_RATE = 44100
SPEECH_RATE = 165
SPEECH_VOLUME = 1.0
MAX_TEXT_LENGTH = 300

SOUND_DIR = Path(__file__).parent / "assets" / "sounds"

EFFECTS = {
    "listen_start": (400, 1200),
    "listen_end": (1200, 400),
}


def _clean_text(text):
    """Clean text before sending it to the speech engine."""
    if not isinstance(text, str):
        return ""

    text = text[:MAX_TEXT_LENGTH]
    text = re.sub(
        r"[^a-zA-Z0-9\u0600-\u06FF\s.,!?;:'\"()\-]",
        "",
        text
    )
    return text.strip()


def _is_arabic(text):
    return bool(re.search(r"[\u0600-\u06FF]", text))


def _voice_languages(voice):
    """Read language information from a pyttsx3 voice."""
    values = [
        getattr(voice, "id", ""),
        getattr(voice, "name", ""),
    ]

    for language in getattr(voice, "languages", []):
        if isinstance(language, bytes):
            language = language.decode("utf-8", errors="ignore")
        values.append(str(language))

    return " ".join(values).lower()


def _find_voice(voices, language):
    for voice in voices:
        info = _voice_languages(voice)

        if language == "ar" and (
            "arabic" in info
            or re.search(r"(^|[^a-z])ar([^a-z]|$)", info)
            or "ar-" in info
            or "ar_" in info
        ):
            return voice

        if language == "en" and (
            "english" in info
            or re.search(r"(^|[^a-z])en([^a-z]|$)", info)
            or "en-" in info
            or "en_" in info
        ):
            return voice

    return None


def _build_tone(path, start_freq, end_freq, duration=0.2):
    total = int(SAMPLE_RATE * duration)
    frames = bytearray()
    phase = 0.0

    for i in range(total):
        progress = i / total
        frequency = start_freq + (end_freq - start_freq) * progress
        phase += 2 * math.pi * frequency / SAMPLE_RATE

        value = (
            math.sin(phase) + 0.4 * math.sin(2 * phase)
        ) / 1.4

        fade = math.sin(math.pi * progress)
        sample = int(value * fade * 0.35 * 32767)
        frames += struct.pack("<h", sample)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(frames))


class TextToSpeech:
    """Thread-safe speech service using one dedicated pyttsx3 worker."""

    def __init__(
        self,
        voice_id=None,
        rate=SPEECH_RATE,
        volume=SPEECH_VOLUME,
        on_error=None,
        on_status=None,
    ):
        self.voice_id = voice_id
        self.rate = rate
        self.volume = volume
        self.on_error = on_error
        self.on_status = on_status

        self._queue = queue.Queue()
        self._engine = None
        self._engine_ready = threading.Event()
        self._closed = False
        self._state_lock = threading.Lock()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="TTS-Worker",
            daemon=True,
        )
        self._worker.start()
        self._engine_ready.wait()

    def _report_error(self, error):
        if self.on_error:
            try:
                self.on_error(str(error))
            except Exception:
                pass

    def _report_status(self, status):
        if self.on_status:
            try:
                self.on_status(status)
            except Exception as error:
                self._report_error(error)

    def _worker_loop(self):
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self.rate)
            self._engine.setProperty("volume", self.volume)

            if self.voice_id:
                self._engine.setProperty("voice", self.voice_id)

        except Exception as error:
            self._report_error(error)
            self._engine_ready.set()
            return

        self._engine_ready.set()

        while True:
            task = self._queue.get()

            if task is None:
                break

            text, finished, result = task

            try:
                if self._engine is None:
                    raise RuntimeError("TTS engine is unavailable")

                voices = self._engine.getProperty("voices") or []

                if _is_arabic(text):
                    voice = _find_voice(voices, "ar")

                    if voice is None:
                        raise RuntimeError(
                            "No Arabic voice is installed. "
                            "Install an Arabic speech voice."
                        )
                else:
                    voice = _find_voice(voices, "en")

                if voice:
                    self._engine.setProperty("voice", voice.id)

                self._report_status("speaking")
                self._engine.say(text)
                self._engine.runAndWait()
                result["success"] = True

            except Exception as error:
                self._report_error(error)
                result["success"] = False

            finally:
                self._report_status("idle")
                finished.set()
                self._queue.task_done()

    def speak(self, text, block=True):
        text = _clean_text(text)

        if not text or self._closed:
            return False

        if self._engine is None or not self._worker.is_alive():
            self._report_error("TTS engine is unavailable")
            return False

        finished = threading.Event()
        result = {"success": False}

        self._queue.put((text, finished, result))

        if not block:
            return True

        finished.wait()
        return result["success"]

    def stop(self):
        """Stop current speech and cancel pending speech requests."""
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception as error:
                self._report_error(error)

        while True:
            try:
                task = self._queue.get_nowait()

                if task is None:
                    self._queue.task_done()
                    continue

                _, finished, result = task
                result["success"] = False
                finished.set()
                self._queue.task_done()

            except queue.Empty:
                break

    def play_effect(self, effect_id):
        if effect_id not in EFFECTS:
            raise ValueError(f"Unknown sound effect: {effect_id}")

        SOUND_DIR.mkdir(parents=True, exist_ok=True)

        path = SOUND_DIR / f"{effect_id}.wav"

        if not path.exists():
            _build_tone(path, *EFFECTS[effect_id])

        player = shutil.which("paplay") or shutil.which("aplay")

        if not player:
            self._report_error("Neither paplay nor aplay is installed")
            return False

        command = (
            [player, str(path)]
            if player.endswith("paplay")
            else [player, "-q", str(path)]
        )

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError) as error:
            self._report_error(error)
            return False

    def close(self):
        if self._closed:
            return

        self._closed = True
        self.stop()
        self._queue.put(None)

        if threading.current_thread() != self._worker:
            self._worker.join(timeout=5)

        if self._worker.is_alive():
            self._report_error("TTS worker did not stop within 5 seconds")


# Backward compatibility with existing project calls.
_default_tts = None


def _get_default_tts():
    global _default_tts

    if _default_tts is None:
        _default_tts = TextToSpeech()

    return _default_tts


def speak(text, block=True):
    return _get_default_tts().speak(text, block=block)


def play_effect(effect_id):
    return _get_default_tts().play_effect(effect_id)


def play_listen_start():
    return play_effect("listen_start")


def play_listen_end():
    return play_effect("listen_end")


if __name__ == "__main__":
    tts = TextToSpeech(
        on_error=lambda error: print("[TTS ERROR]", error),
        on_status=lambda status: print("[TTS STATUS]", status),
    )

    try:
        tts.play_effect("listen_start")
        print("English:", tts.speak("Aura Shell online. How can I help you?"))
        print("Arabic:", tts.speak("مرحبا بك في نظام أورا شيل"))
        tts.play_effect("listen_end")
    finally:
        tts.close()
