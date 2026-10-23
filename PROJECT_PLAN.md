# AuraShell OS — Architecture and Team Master Plan

## 1. Product scope

AuraShell OS is a Python 3.10+ voice-controlled desktop layer for Linux/Ubuntu.
The first milestone is a locally classifying, observable, and safe assistant
shell. Speech recognition, execution, diagnostics, GUI, and Gemini support are
integrated in later increments behind explicit module interfaces. A parsed
intent is data, not authorization to execute it.

## 2. System architecture and data flow

```text
Microphone
   │
   ▼
core.stt — capture, energy threshold, speech recognition
   │ recognized text + locale/confidence metadata
   ▼
core.intent_parser — local bilingual regex classification
   ├── local command ──► core.executor — policy check and allowlisted dispatch
   │                          ├── core.file_app_manager — known applications/files
   │                          └── core.system_info — read-only diagnostics
   └── general query ──► Gemini adapter (explicitly configured and consented)
                              │ response/action result
                              ▼
                   core.tts and gui.main_window — speech and visual feedback
```

The initial `main.py` deliberately stops after parsing and printing JSON. It
does not activate a microphone, call Gemini, or execute power/application
commands. Each later stage should consume typed, validated results from the
previous stage. Errors should be explicit results or raised domain exceptions;
they must not be converted into apparent success.

### Runtime sequence

1. STT captures one bounded utterance, applies the configured energy threshold,
   and returns recognized text plus language/confidence metadata.
2. `IntentParser.parse()` preserves the exact recognized phrase in `raw_text`
   and returns an intent and an optional canonical target.
3. The executor validates the intent-target pair against policy. Unknown,
   malformed, or unapproved actions are rejected and reported to the user.
4. Allowlisted local actions call the file/app manager or read-only diagnostics
   interface. General questions may be sent to Gemini only after configuration
   and privacy/consent policy checks.
5. TTS and GUI present the same success, rejection, or error result.

## 3. Team work breakdown structure

| Owner | Responsibility | Deliverables and acceptance criteria |
|---|---|---|
| **Mahmoud El Sayed** | Team Lead / NLP Engine & Architecture Integration | Owns architecture, `core/intent_parser.py`, interface contracts, integration decisions, and release readiness. Maintain stable schemas and review cross-module changes. |
| **Aisha Mohamed** | Speech-to-Text / STT & Energy Threshold Filtering | Implement microphone lifecycle, Arabic/English recognition configuration, bounded listening timeouts, threshold calibration, and clear permission/device/network errors. Test with mocked microphone input. |
| **Malak Hazem** | Text-to-Speech / TTS & Sci-Fi Sound FX | Implement speech output and opt-in sound effects, volume/rate controls, audio resource cleanup, and non-blocking GUI-compatible playback. |
| **Ibrahim Waleed** | Command Execution & Security Whitelist Sandbox | Implement typed action results, intent-target authorization, safe `subprocess` invocation with argument arrays and timeouts, and explicit confirmation for high-impact actions. No shell interpolation. |
| **Mohamed Nader** | File & Application Management Engine | Maintain an explicit app ID-to-executable mapping, validate requested file operations, and report missing apps/permissions without arbitrary command execution. |
| **Ahmed Mahdy** | System Information & psutil Diagnostics | Provide read-only CPU, memory, disk, and uptime snapshots with stable field names and units; handle unavailable metrics transparently. |
| **Malak Saad** | CustomTkinter Cyberpunk GUI & Audio Visualizer | Build accessible dark-theme status/feedback UI, permission and confirmation dialogs, and a visualizer that consumes audio level events without blocking the UI loop. |
| **Youssef Ahmed** | Integration Pipeline & Test Cases | Own end-to-end test scenarios, fixtures/mocks, CI checks, regression coverage, and integration verification for the agreed module contracts. |

### Suggested sequence

1. Agree schemas and safe dispatch policy; land parser tests.
2. Implement STT and parser as independent input modules.
3. Implement the allowlisted app manager and read-only system diagnostics.
4. Add executor authorization and typed outcomes; verify with mocked dispatch.
5. Add TTS and GUI feedback, keeping UI callbacks non-blocking.
6. Add optional Gemini integration with consent/configuration and data-minimizing
   behavior; run end-to-end tests and document supported Ubuntu setup.

## 4. Module interfaces and data schemas

All APIs below are proposed integration contracts. Keep I/O behind module
boundaries, avoid circular imports, and keep GUI code out of `core/`.

### Shared intent schema

```python
{
    "intent": str,              # OPEN_APP, CLOSE_APP, SYSTEM_DIAGNOSTICS,
                                # POWER_COMMAND, PANIC_MODE, GENERAL_AI_QUERY
    "target": str | None,       # canonical ID, never a shell command
    "raw_text": str,            # exact input phrase
}
```

Targets are stable identifiers such as `chrome`, `vscode`, `ram`, `cpu`,
`shutdown`, and `restart`. The parser only classifies; it does not authorize.

### `core.stt`

```python
class SpeechToText:
    def __init__(
        self,
        language: str = "ar-EG",
        energy_threshold: int = 300,
        phrase_time_limit: float = 8.0,
    ) -> None: ...

    def listen_once(self, timeout: float = 5.0) -> dict[str, object]: ...
    def calibrate(self, duration: float = 1.0) -> int: ...
    def close(self) -> None: ...
```

`listen_once` returns `{"text": str, "language": str, "confidence": float |
None}`. Device, timeout, and recognition failures must be distinguishable.

### `core.tts`

```python
class TextToSpeech:
    def __init__(
        self, voice_id: str | None = None, rate: int = 180, volume: float = 0.8
    ) -> None: ...

    def speak(self, text: str, *, block: bool = True) -> None: ...
    def play_effect(self, effect_id: str) -> None: ...
    def close(self) -> None: ...
```

`effect_id` is selected from a bundled, explicit asset map, never a user-supplied
path. Audio failures are surfaced to the caller/UI.

### `core.intent_parser`

```python
class IntentParser:
    def parse(self, text: str) -> dict[str, str | None]: ...
```

Output keys are exactly `intent`, `target`, and `raw_text`. Unsupported local
phrases return `GENERAL_AI_QUERY` with a `None` target.

### `core.executor`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    data: dict[str, object] | None = None

class CommandExecutor:
    def execute(
        self, intent: str, target: str | None, *, confirmed: bool = False
    ) -> ActionResult: ...
```

The executor accepts canonical IDs only and delegates to injected manager and
diagnostics interfaces. It rejects unknown combinations and requires a fresh
explicit confirmation for power operations.

### `core.file_app_manager`

```python
class FileAppManager:
    def open_app(self, app_id: str) -> None: ...
    def close_app(self, app_id: str) -> None: ...
    def list_available_apps(self) -> list[str]: ...
    def open_path(self, path: str) -> None: ...
```

App IDs must resolve through a reviewed mapping of executable plus fixed
arguments. File operations must be constrained to user-approved roots and
validated paths.

### `core.system_info`

```python
class SystemInfo:
    def snapshot(self) -> dict[str, int | float | str | None]: ...
    def memory(self) -> dict[str, int | float]: ...
    def cpu(self, interval: float = 0.1) -> dict[str, int | float]: ...
    def disk(self, path: str = "/") -> dict[str, int | float | str]: ...
```

Metrics are read-only and use documented units (bytes for memory/disk,
percentage for utilization, seconds for uptime).

### `gui.main_window`

```python
class AuraShellWindow:
    def __init__(self, on_text_command: object | None = None) -> None: ...
    def run(self) -> None: ...
    def show_status(self, message: str, *, level: str = "info") -> None: ...
```

All Tk updates run on the GUI thread; worker threads communicate through a
queue or scheduled callback.

### `main.py`

`main() -> int` owns startup/shutdown and connects modules through their public
interfaces. It must not duplicate parsing, policy, or application allowlists.

## 5. Security and whitelist policy

1. **Default deny:** only documented intent-target pairs can reach an action
   handler. General queries are not interpreted as operating-system commands.
2. **No shell execution:** do not use `shell=True`, shell strings, `eval`,
   `exec`, or dynamically construct commands from recognized text. Invoke a
   fixed executable and validated argument list with bounded timeouts.
3. **Application allowlist:** map canonical app IDs to reviewed executable
   paths/fixed arguments. Never execute a recognized app name as a path or
   command. Report unavailable applications instead of searching arbitrary
   locations and running the first match.
4. **Power safety:** shutdown/restart are high-impact. Require a visible,
   explicit, time-bounded confirmation separate from the voice phrase; do not
   execute these actions in parser/demo mode. Panic mode must not mean “kill
   every process” or bypass confirmation.
5. **Least privilege:** do not request root/sudo. Limit file operations to
   user-approved directories, resolve paths, and reject traversal or symlink
   escapes where applicable.
6. **Privacy:** do not persist raw audio or transcripts by default. Disclose
   when text is sent to Gemini, obtain configuration/consent, keep API keys in
   environment or a secret manager, and never log credentials/audio content.
7. **Robust boundaries:** validate input types, length, target, and state.
   Apply timeouts and resource cleanup to audio, network, and subprocess work.
   Surface errors without success-shaped fallback responses.
8. **Testing:** test policy denials, malformed targets, confirmation paths,
   subprocess arguments, and cleanup with mocks. Never run real shutdown,
   restart, or destructive file actions in automated tests.

## 6. Development and validation

- Target Python 3.10+ and Ubuntu. Install PyAudio using the distribution's
  PortAudio development package when a system wheel is unavailable.
- Run `python setup_project.py` to create any missing starter files without
  replacing edited files.
- Run parser tests with `python -m unittest discover -s tests -v`.
- Run the terminal parser demo with `python main.py`; it does not perform OS
  actions.
- Add linting, type checking, and CI configuration once the team selects the
  repository's preferred tools and supported Ubuntu versions.
