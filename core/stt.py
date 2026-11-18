"""core/stt.py - Speech-to-Text module for AuraShell-OS.

Public API:
    listen_and_recognize() -> str | None
        Listens to the microphone and returns the recognized text.

Ubuntu setup:
    sudo apt install portaudio19-dev python3-pyaudio flac
    pip install SpeechRecognition

Note: the Google recognizer used here needs an internet connection.
"""

import logging
from typing import Optional

import speech_recognition as sr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings (change these here, not inside the functions)
# ---------------------------------------------------------------------------
DEFAULT_LANGUAGE = "ar-EG"        # use "ar-EG" for Egyptian Arabic
INITIAL_ENERGY_THRESHOLD = 300    # starting loudness level; below it = noise
CALIBRATION_SECONDS = 1.0         # how long we listen to the room's noise
PAUSE_THRESHOLD = 0.8             # seconds of silence that end a sentence
LISTEN_TIMEOUT = 5                # max seconds to wait for speech to start
PHRASE_TIME_LIMIT = 10            # max seconds for one sentence

# ---------------------------------------------------------------------------
# One shared recognizer, configured once when the module is imported
# ---------------------------------------------------------------------------
_recognizer = sr.Recognizer()
_recognizer.energy_threshold = INITIAL_ENERGY_THRESHOLD
_recognizer.dynamic_energy_threshold = True   # keeps adapting to noise changes
_recognizer.pause_threshold = PAUSE_THRESHOLD

_is_calibrated = False


def calibrate_noise(duration: float = CALIBRATION_SECONDS) -> None:
    """Measure the background noise and set the energy threshold above it.

    Sounds quieter than the threshold are ignored, so fans, keyboard clicks
    and room noise do not get mistaken for speech.
    """
    global _is_calibrated
    with sr.Microphone() as source:
        _recognizer.adjust_for_ambient_noise(source, duration=duration)
    _is_calibrated = True
    logger.info("Noise calibrated. Energy threshold = %.0f",
                _recognizer.energy_threshold)


def listen_and_recognize(
    language: str = DEFAULT_LANGUAGE,
    timeout: float = LISTEN_TIMEOUT,
    phrase_time_limit: float = PHRASE_TIME_LIMIT,
) -> Optional[str]:
    """Listen to the microphone once and return what was said.

    Returns:
        The recognized text, or None if nothing was said or it was not understood.

    Raises:
        RuntimeError: if no microphone can be opened.
    """
    try:
        if not _is_calibrated:
            calibrate_noise()

        with sr.Microphone() as source:
            logger.info("Listening...")
            audio = _recognizer.listen(
                source, timeout=timeout, phrase_time_limit=phrase_time_limit
            )
    except OSError as error:
        raise RuntimeError(f"Could not access the microphone: {error}") from error
    except sr.WaitTimeoutError:
        logger.info("No speech detected within %s seconds.", timeout)
        return None

    try:
        text = _recognizer.recognize_google(audio, language=language)
    except sr.UnknownValueError:
        logger.info("Speech was not understood.")
        return None
    except sr.RequestError as error:
        logger.error("Recognition service error: %s", error)
        return None

    return text.strip()


if __name__ == "__main__":
    # Quick manual test: python core/stt.py
    logging.basicConfig(level=logging.INFO)
    print("Say something (Ctrl+C to stop)...")
    try:
        while True:
            result = listen_and_recognize()
            print(f"You said: {result}" if result else "(nothing recognized)")
    except KeyboardInterrupt:
        print("\nStopped.")
