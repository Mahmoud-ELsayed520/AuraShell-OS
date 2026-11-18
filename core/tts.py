import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from core import tts as tts_module


class FakeVoice:
    def __init__(self, voice_id, name, languages=()):
        self.id = voice_id
        self.name = name
        self.languages = list(languages)


EN_VOICE = FakeVoice("en-voice", "English", [b"\x05en-us"])
AR_VOICE = FakeVoice("ar-voice", "Arabic", [b"\x02ar"])


class FakeEngine:
    """Records what was said; can hold runAndWait() open with a gate."""

    def __init__(self, voices, gate=None):
        self.props = {"rate": 200, "volume": 1.0, "voice": "default"}
        self._voices = voices
        self.gate = gate
        self.entered = threading.Event()
        self.said = []  # (text, voice id used)
        self._pending = ""

    def setProperty(self, key, value):
        self.props[key] = value

    def getProperty(self, key):
        return self._voices if key == "voices" else self.props[key]

    def say(self, text):
        self._pending = text

    def runAndWait(self):
        self.said.append((self._pending, self.props["voice"]))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)

    def stop(self):
        if self.gate is not None:
            self.gate.set()  # an interrupted sentence returns at once


def wait_until(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class TtsTestCase(unittest.TestCase):
    def make_tts(self, voices=None, gate=None, **kwargs):
        engine = FakeEngine([EN_VOICE, AR_VOICE] if voices is None else voices,
                            gate)
        self.errors = []
        self.statuses = []
        kwargs.setdefault("on_error", self.errors.append)
        kwargs.setdefault("on_status", self.statuses.append)
        fake_module = types.SimpleNamespace(init=lambda: engine)
        with mock.patch.dict(sys.modules, {"pyttsx3": fake_module}):
            service = tts_module.TextToSpeech(**kwargs)
        self.addCleanup(service.close)
        return service, engine


class TextHelpersTests(unittest.TestCase):
    def test_non_string_becomes_empty(self):
        self.assertEqual(tts_module._clean_text(None), "")
        self.assertEqual(tts_module._clean_text(123), "")

    def test_symbols_removed_arabic_kept(self):
        self.assertEqual(tts_module._clean_text("hello <b>world</b> {x}"),
                         "hello bworldb x")
        self.assertEqual(tts_module._clean_text("  مرحبا   بك  "), "مرحبا بك")

    def test_long_text_is_cut_at_a_word_boundary(self):
        cleaned = tts_module._clean_text("word " * 200)
        self.assertLessEqual(len(cleaned), tts_module.MAX_TEXT_LENGTH)
        self.assertTrue(cleaned.endswith("word"))

    def test_is_arabic(self):
        self.assertTrue(tts_module._is_arabic("افتح المتصفح"))
        self.assertFalse(tts_module._is_arabic("open the browser"))

    def test_find_voice_uses_language_codes(self):
        voices = [EN_VOICE, AR_VOICE]
        self.assertIs(tts_module._find_voice(voices, "ar"), AR_VOICE)
        self.assertIs(tts_module._find_voice(voices, "en"), EN_VOICE)
        self.assertIsNone(tts_module._find_voice([EN_VOICE], "ar"))

    def test_find_voice_does_not_match_inside_words(self):
        voice = FakeVoice("clear-voice", "Clear Standard", [])
        self.assertIsNone(tts_module._find_voice([voice], "ar"))


class SpeakTests(TtsTestCase):
    def test_english_uses_english_voice(self):
        service, engine = self.make_tts()
        self.assertTrue(service.speak("Browser opened"))
        self.assertEqual(engine.said[-1], ("Browser opened", "en-voice"))

    def test_arabic_uses_arabic_voice_then_english_switches_back(self):
        service, engine = self.make_tts()
        self.assertTrue(service.speak("تم فتح المتصفح"))
        self.assertEqual(engine.said[-1][1], "ar-voice")
        self.assertTrue(service.speak("Done"))
        self.assertEqual(engine.said[-1][1], "en-voice")

    def test_arabic_without_arabic_voice_fails_loudly(self):
        service, engine = self.make_tts(voices=[EN_VOICE])
        self.assertFalse(service.speak("مرحبا"))
        self.assertEqual(engine.said, [])
        self.assertTrue(any("No Arabic voice" in e for e in self.errors))

    def test_no_english_voice_returns_to_the_default_voice(self):
        service, engine = self.make_tts(voices=[AR_VOICE])
        self.assertTrue(service.speak("مرحبا"))
        self.assertEqual(engine.said[-1][1], "ar-voice")
        self.assertTrue(service.speak("Hello"))
        self.assertEqual(engine.said[-1][1], "default")

    def test_pinned_voice_disables_auto_selection(self):
        service, engine = self.make_tts(voices=[EN_VOICE], voice_id="custom")
        self.assertTrue(service.speak("مرحبا"))
        self.assertEqual(engine.said[-1][1], "custom")

    def test_empty_and_non_string_text_is_not_spoken(self):
        service, engine = self.make_tts()
        self.assertFalse(service.speak(""))
        self.assertFalse(service.speak("   "))
        self.assertFalse(service.speak(None))
        self.assertEqual(engine.said, [])

    def test_non_blocking_returns_at_once_and_keeps_order(self):
        gate = threading.Event()
        service, engine = self.make_tts(gate=gate)
        started = time.monotonic()
        self.assertTrue(service.speak("one", block=False))
        self.assertTrue(engine.entered.wait(2))
        self.assertTrue(service.speak("two", block=False))
        self.assertTrue(service.speak("three", block=False))
        self.assertLess(time.monotonic() - started, 1.0)
        gate.set()
        self.assertTrue(wait_until(lambda: len(engine.said) == 3))
        self.assertEqual([t for t, _ in engine.said], ["one", "two", "three"])

    def test_idle_is_reported_only_when_the_queue_is_empty(self):
        gate = threading.Event()
        service, engine = self.make_tts(gate=gate)
        service.speak("one", block=False)
        self.assertTrue(engine.entered.wait(2))
        service.speak("two", block=False)
        gate.set()
        self.assertTrue(wait_until(lambda: self.statuses[-1:] == ["idle"]))
        self.assertEqual(self.statuses.count("idle"), 1)
        self.assertEqual(self.statuses.count("speaking"), 2)

    def test_stop_interrupts_current_and_cancels_queued(self):
        gate = threading.Event()
        service, engine = self.make_tts(gate=gate)
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(ok=service.speak("first")))
        thread.start()
        self.assertTrue(engine.entered.wait(2))
        service.speak("second", block=False)
        service.stop()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(outcome["ok"])  # interrupted is not a success
        time.sleep(0.1)
        self.assertEqual([t for t, _ in engine.said], ["first"])

    def test_available_voices_lists_plain_dicts(self):
        service, _ = self.make_tts()
        voices = service.available_voices()
        self.assertEqual([v["id"] for v in voices], ["en-voice", "ar-voice"])
        voices[0]["id"] = "changed"
        self.assertEqual(service.available_voices()[0]["id"], "en-voice")


class LifecycleTests(TtsTestCase):
    def test_engine_start_failure_is_reported(self):
        errors = []

        def broken_init():
            raise RuntimeError("no audio device")

        fake_module = types.SimpleNamespace(init=broken_init)
        with mock.patch.dict(sys.modules, {"pyttsx3": fake_module}):
            service = tts_module.TextToSpeech(on_error=errors.append)
        self.addCleanup(service.close)
        self.assertFalse(service.is_available())
        self.assertFalse(service.speak("hello"))
        self.assertTrue(any("failed to start" in e for e in errors))

    def test_constructor_does_not_hang_if_the_engine_hangs(self):
        release = threading.Event()
        engine = FakeEngine([EN_VOICE])
        errors = []

        def slow_init():
            release.wait(5)
            return engine

        fake_module = types.SimpleNamespace(init=slow_init)
        with mock.patch.object(tts_module, "ENGINE_START_TIMEOUT", 0.2), \
                mock.patch.dict(sys.modules, {"pyttsx3": fake_module}):
            started = time.monotonic()
            service = tts_module.TextToSpeech(on_error=errors.append)
        self.addCleanup(service.close)
        self.addCleanup(release.set)  # runs first (LIFO)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(any("did not start" in e for e in errors))
        self.assertFalse(service.speak("hello"))

    def test_close_stops_the_worker_and_is_idempotent(self):
        service, _ = self.make_tts()
        service.close()
        service.close()
        self.assertFalse(service._worker.is_alive())
        self.assertFalse(service.is_available())
        self.assertFalse(service.speak("after close"))

    def test_stop_after_close_does_not_hang(self):
        service, _ = self.make_tts()
        service.close()
        service.stop()

    def test_context_manager_closes(self):
        engine = FakeEngine([EN_VOICE])
        fake_module = types.SimpleNamespace(init=lambda: engine)
        with mock.patch.dict(sys.modules, {"pyttsx3": fake_module}):
            with tts_module.TextToSpeech() as service:
                self.assertTrue(service.speak("hi"))
        self.assertFalse(service._worker.is_alive())

    def test_broken_callback_does_not_kill_the_worker(self):
        def bad_status(_):
            raise ValueError("boom")

        service, engine = self.make_tts(on_status=bad_status)
        self.assertTrue(service.speak("one"))
        self.assertTrue(service.speak("two"))
        self.assertEqual(len(engine.said), 2)


class EffectTests(TtsTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sound_dir = Path(self._tmp.name) / "sounds"
        patcher = mock.patch.object(tts_module, "SOUND_DIR", self.sound_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _which(*available):
        return lambda name, path=None: (
            f"/usr/bin/{name}" if name in available else None)

    def test_unknown_effect_is_rejected(self):
        service, _ = self.make_tts()
        with self.assertRaises(ValueError):
            service.play_effect("../../etc/passwd")
        with self.assertRaises(ValueError):
            service.play_effect("nope")

    def test_effect_is_written_privately_and_played_safely(self):
        service, _ = self.make_tts()
        with mock.patch.object(tts_module.shutil, "which",
                               self._which("paplay")), \
                mock.patch.object(tts_module.subprocess, "run") as run:
            self.assertTrue(service.play_effect("listen_start"))
        wav = self.sound_dir / "listen_start.wav"
        self.assertTrue(wav.is_file())
        self.assertEqual(wav.read_bytes()[:4], b"RIFF")
        self.assertEqual(sorted(p.name for p in self.sound_dir.iterdir()),
                         ["listen_start.wav"])  # no temp file left behind
        self.assertEqual(self.sound_dir.stat().st_mode & 0o777, 0o700)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/paplay", str(wav)])
        self.assertEqual(kwargs["timeout"], tts_module.EFFECT_TIMEOUT)
        self.assertFalse(kwargs.get("shell", False))

    def test_aplay_fallback_is_quiet(self):
        service, _ = self.make_tts()
        with mock.patch.object(tts_module.shutil, "which",
                               self._which("aplay")), \
                mock.patch.object(tts_module.subprocess, "run") as run:
            self.assertTrue(service.play_effect("listen_end"))
        self.assertEqual(run.call_args[0][0][:2], ["/usr/bin/aplay", "-q"])

    def test_broken_existing_file_is_rebuilt(self):
        self.sound_dir.mkdir(parents=True)
        (self.sound_dir / "listen_start.wav").write_bytes(b"x")
        service, _ = self.make_tts()
        with mock.patch.object(tts_module.shutil, "which",
                               self._which("paplay")), \
                mock.patch.object(tts_module.subprocess, "run"):
            self.assertTrue(service.play_effect("listen_start"))
        size = (self.sound_dir / "listen_start.wav").stat().st_size
        self.assertGreater(size, 44)

    def test_no_player_installed(self):
        service, _ = self.make_tts()
        with mock.patch.object(tts_module.shutil, "which",
                               self._which()):
            self.assertFalse(service.play_effect("listen_start"))
        self.assertTrue(any("paplay nor aplay" in e for e in self.errors))

    def test_player_timeout_does_not_hang_the_app(self):
        service, _ = self.make_tts()
        with mock.patch.object(tts_module.shutil, "which",
                               self._which("paplay")), \
                mock.patch.object(
                    tts_module.subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("paplay", 5)):
            self.assertFalse(service.play_effect("listen_start"))
        self.assertTrue(any("Could not play" in e for e in self.errors))

    def test_unwritable_sound_folder_is_reported(self):
        service, _ = self.make_tts()
        with mock.patch.object(tts_module, "_ensure_sound",
                               side_effect=PermissionError("denied")):
            self.assertFalse(service.play_effect("listen_start"))
        self.assertTrue(any("prepare sound effect" in e for e in self.errors))


if __name__ == "__main__":
    unittest.main()