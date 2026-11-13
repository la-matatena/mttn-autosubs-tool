"""Local HTTP server and orchestration backend for AutoSubs ES.

This module is the backend of MTTN AutoSubs Tool. It serves the single-page
Studio UI from `ui/` and a small JSON API on loopback (default
http://127.0.0.1:55120). It orchestrates the offline Spanish subtitle
pipeline:

  media upload / Resolve inbox WAV -> ffmpeg 16 kHz mono WAV -> whisper-cli
  (whisper.cpp, JSON "OJF" output) -> `spanish_nlp` (RAE rules, segmentation,
  line wrapping) -> `exporters` (SRT/VTT/ASS/FCPXML/...) or ffmpeg burn-in.

Long-running work (model download, transcription, benchmark, MP4 burn-in) runs
in daemon threads tracked as "jobs" in `AppState.jobs`; the UI polls
`GET /api/jobs/<id>` for progress and can cancel via
`POST /api/jobs/<id>/cancel`.

Entry points:
  * `main()`: CLI argument parsing. Default mode serves HTTP and optionally
    opens a browser tab or a chromeless Chrome app window.
  * `run_desktop_app()`: native macOS window via pywebview (`--desktop`).
  * `run_cli_transcription()`: headless one-shot transcription (`--cli`).
  * `create_server()`: used by tests to build a server bound to any port.

DaVinci Resolve integration goes through `resolve_bridge` (Lua scripts drop a
WAV in an inbox folder and call `POST /api/resolve/notify-export`).
"""

from __future__ import annotations

import argparse
import dataclasses
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import webbrowser

# Prefer the monorepo (Blaze) package path; fall back to flat sibling imports
# when running from the standalone repo / macOS bundle.
try:
  from google3.experimental.users.joseluiszapata.autosubs_es import exporters
  from google3.experimental.users.joseluiszapata.autosubs_es import font_manager
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
  from google3.experimental.users.joseluiszapata.autosubs_es import machine_settings
  from google3.experimental.users.joseluiszapata.autosubs_es import resolve_bridge
  from google3.experimental.users.joseluiszapata.autosubs_es import spanish_nlp
except ImportError:
  import exporters  # type: ignore[no-redef]
  import font_manager  # type: ignore[no-redef]
  import hardware_imac2017  # type: ignore[no-redef]
  import machine_settings  # type: ignore[no-redef]
  import resolve_bridge  # type: ignore[no-redef]
  import spanish_nlp  # type: ignore[no-redef]


# --- Module constants and process helpers ---


def _find_ui_dir() -> Path:
  """Locates the ui/ directory across standalone macOS and Blaze runfiles.

  Candidates are tried in order: next to this file, next to the resolved
  (symlink-free) file, the monorepo path relative to CWD, and (first, when
  set) the Blaze `TEST_SRCDIR` runfiles tree.

  Returns:
    The first candidate containing `index.html`, or the first candidate when
    none match (static requests will then 404 instead of crashing at import).
  """
  candidates = [
      Path(__file__).parent / "ui",
      Path(__file__).resolve().parent / "ui",
      Path.cwd()
      / "experimental"
      / "users"
      / "joseluiszapata"
      / "autosubs_es"
      / "ui",
  ]
  test_srcdir = os.environ.get("TEST_SRCDIR")
  if test_srcdir:
    candidates.insert(
        0,
        Path(test_srcdir)
        / "google3"
        / "experimental"
        / "users"
        / "joseluiszapata"
        / "autosubs_es"
        / "ui",
    )
  for cand in candidates:
    if (cand / "index.html").is_file():
      return cand
  return candidates[0]


UI_DIR = _find_ui_dir()

# Containers ffmpeg can burn subtitles into (input side of /api/export-video).
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v", ".mkv"})
# Subset the WebKit/Chromium <video> element can preview (.mkv is excluded).
_BROWSER_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
# Host/Origin values accepted by the DNS-rebinding / CSRF guard.
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Upper bound on jobs kept in RAM; finished jobs beyond it are pruned.
_MAX_RETAINED_JOBS = 100
# Largest body accepted by POST /api/upload (8 GiB). Uploads are streamed to
# disk, so RAM is not the concern; the cap stops a runaway or hostile client
# from filling the user's disk while still fitting long high-bitrate masters
# (e.g. ~1-2 h of 4K H.264 at 10-20 Mbit/s, or a long WAV/MOV export).
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
# Job statuses that mean "still in flight" (used by the dedupe checks).
_ACTIVE_JOB_STATUSES = ("queued", "running")
# Errors `resolve_bridge.extract_waveform_peaks` raises on WAVs the stdlib
# `wave` module cannot decode (32-bit float, truncated or corrupt headers).
_PEAK_ERRORS = (wave.Error, EOFError, ValueError)


def _safe_waveform_peaks(wav_path: Path | str) -> dict[str, Any]:
  """Extracts waveform peaks, degrading to an empty waveform on bad WAVs.

  User media must still activate (and be transcribable through ffmpeg) even
  when the waveform preview cannot be computed, so decoding errors are
  swallowed here instead of failing the HTTP request or job.

  Args:
    wav_path: WAV file to analyze.

  Returns:
    The `extract_waveform_peaks` result, or `{"peaks": [], "duration_ms": 0}`
    if the file is not a WAV the stdlib can decode.
  """
  try:
    return resolve_bridge.extract_waveform_peaks(str(wav_path))
  except _PEAK_ERRORS:
    return {"peaks": [], "duration_ms": 0}


def _file_rev(path: Path | None) -> str | None:
  """Returns the "<name>:<mtime_ns>:<size>" revision tag of a file.

  Used as the session `media_rev` (UI cache-buster and the optimistic
  concurrency token of PUT /api/cues). Does file I/O, so call it without
  holding `AppState.lock`.

  Args:
    path: File to stat, or None.

  Returns:
    The revision string, or None if `path` is None or not a readable file.
  """
  if path is None or not path.is_file():
    return None
  try:
    st = path.stat()
  except OSError:  # Deleted/replaced between the two calls.
    return None
  return f"{path.name}:{st.st_mtime_ns}:{st.st_size}"


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
  """Stops a child process group (nice + whisper-cli/ffmpeg) cleanly.

  Children are started with `start_new_session=True` on POSIX, so the whole
  process group (the `nice` wrapper plus the real tool) gets SIGTERM. A
  daemon thread then waits up to ~1 s and escalates to SIGKILL, so the caller
  (often an HTTP handler holding no lock) never blocks.

  Args:
    proc: The child process to stop. No-op if it already exited.
  """
  if proc.poll() is not None:
    return
  try:
    if os.name == "posix":
      os.killpg(proc.pid, signal.SIGTERM)
    else:
      proc.terminate()
  except OSError:
    return

  def _reap() -> None:
    """Polls 10 x 100 ms for exit, then force-kills the process group."""
    for _ in range(10):
      if proc.poll() is not None:
        return
      time.sleep(0.1)
    try:
      if os.name == "posix":
        os.killpg(proc.pid, signal.SIGKILL)
      else:
        proc.kill()
    except OSError:
      pass

  threading.Thread(target=_reap, daemon=True).start()


# --- Application state and background jobs ---


class AppState:
  """Thread-safe application state shared across HTTP handler threads.

  One instance lives for the whole server process. `ThreadingHTTPServer`
  runs each request on its own thread and every job runs on a daemon worker
  thread, so all mutable session and job fields below must be read/written
  while holding `self.lock` (a non-reentrant `threading.Lock`; never call
  `session_snapshot()` or `update_job()` while already holding it). Slow work
  (ffmpeg, whisper-cli, file I/O on large media, network) is done outside the
  lock; results are swapped in with a short critical section.

  Attributes:
    app_dir: Per-user data root (`~/Library/Application Support/...`).
    cache_dir: Uploads, 16 kHz working WAVs, temp whisper/ASS/MP4 outputs.
    uploads_dir: `cache_dir/uploads`, where POST /api/upload stores media.
      Kept separate so a client-chosen name can never overwrite internal
      cache files (`working_*_16k.wav`, the demo WAV, burn-in MP4s).
    models_dir: Downloaded whisper.cpp GGML models.
    bridge: `resolve_bridge.ResolveBridge` (inbox WAV, timeline metadata,
      pushing cues to DaVinci Resolve). Not guarded by `lock`.
    fonts: `font_manager.FontManager` for subtitle fonts. Not guarded.
    settings: `machine_settings.MachineSettings` persisted per-machine
      performance preferences (has its own internal synchronization).
    lock: Guards every attribute listed below.
    jobs: job_id -> job dict with keys `job_id`, `kind` ("download",
      "transcribe", "benchmark", "burnin"), `status` ("running", "done",
      "error", "cancelled"), `progress` (0..100), `stage` (Spanish UI text),
      `error`, `started_at`/`finished_at` (epoch seconds), `result`.
      Guarded by `lock`; at most `_MAX_RETAINED_JOBS` finished jobs are kept.
    active_procs: job_id -> running child `Popen`, so `cancel_job()` can kill
      it. Guarded by `lock`.
    cancel_flags: job_id -> True once the user cancels. Workers poll it at
      stage boundaries and inside read loops. Guarded by `lock`.
    _active_downloads: model_id -> job_id of the in-flight download, used to
      dedupe repeated download clicks. Guarded by `lock`.
    _active_transcription: job_id of the in-flight transcription (or None),
      so a double-click on "Transcribir" joins the running job instead of
      starting a second whisper-cli. Guarded by `lock`.
    _active_benchmark: Same dedupe slot for the speed benchmark.
    media_path: Active source media (uploaded file, inbox WAV, or demo WAV).
    wav_path: 16 kHz mono WAV used for the waveform/preview; None when the
      upload is a non-WAV file that has not been transcribed yet.
    exported_video_path: Last burned-in MP4, served by
      `/api/exported-video`. Cleared whenever the media changes.
    _last_inbox_sig: (path, mtime_ns, size) of the last Resolve inbox WAV
      that was loaded, so repeated notify calls do not wipe edited cues.
    media_generation: Monotonic counter bumped every time the active media
      is replaced (upload, demo, inbox). A transcription job records it at
      start and discards its result if it changed meanwhile (stale job).
    media_name: Display name of the active media.
    duration_ms: Active media duration in milliseconds.
    peaks: Normalized 0..1 waveform peaks for the UI waveform.
    words: Word-level timings from whisper (source of truth for resegment).
    cues: Current subtitle cues (possibly hand-edited via PUT /api/cues).
    format_config: `spanish_nlp.SpanishFormatConfig` used for segmentation.
    demo_mode: True while the built-in demo session is loaded.
  """

  def __init__(self, app_dir: Path | None = None) -> None:
    """Creates data directories and initializes an empty session.

    Args:
      app_dir: Override for the data root (tests pass a temp dir). Defaults
        to the per-user application-support directory.
    """
    self.app_dir = app_dir or hardware_imac2017.get_app_data_dir(create=True)
    self.cache_dir = self.app_dir / "cache"
    self.uploads_dir = self.cache_dir / "uploads"
    self.models_dir = self.app_dir / "models"
    self.cache_dir.mkdir(parents=True, exist_ok=True)
    self.uploads_dir.mkdir(parents=True, exist_ok=True)
    self.models_dir.mkdir(parents=True, exist_ok=True)

    self.bridge = resolve_bridge.ResolveBridge(app_data_dir=self.app_dir)
    self.fonts = font_manager.FontManager(app_dir=self.app_dir)
    self.settings = machine_settings.MachineSettings(self.app_dir)
    self.lock = threading.Lock()
    # Everything below is guarded by self.lock.
    # shortcut: single local user session in RAM (<=100 jobs retained), persist to SQLite if multi-project history is needed
    self.jobs: dict[str, dict[str, Any]] = {}
    self.active_procs: dict[str, subprocess.Popen[Any]] = {}
    self.cancel_flags: dict[str, bool] = {}
    self._active_downloads: dict[str, str] = {}
    self._active_transcription: str | None = None
    self._active_benchmark: str | None = None

    self.media_path: Path | None = None
    self.wav_path: Path | None = None
    self.exported_video_path: Path | None = None
    self._last_inbox_sig: tuple[str, int, int] | None = None
    self.media_generation: int = 0
    self.media_name: str = ""
    self.duration_ms: int = 0
    self.peaks: list[float] = []
    self.words: list[spanish_nlp.WordTiming] = []
    self.cues: list[spanish_nlp.SubtitleCue] = []
    self.format_config = spanish_nlp.SpanishFormatConfig()
    self.demo_mode: bool = False

  def session_snapshot(self, include_peaks: bool = True) -> dict[str, Any]:
    """Returns a JSON-serializable copy of the current session for the UI.

    Acquires `self.lock`; callers must not already hold it. The lock only
    guards copying the in-memory fields; the `stat`/`is_file` calls for
    `media_rev` and the exported video run after it is released, so a slow
    disk never blocks other request threads.

    Args:
      include_peaks: Whether to include the waveform peaks list. PUT
        /api/cues passes False to keep frequent autosave responses small.

    Returns:
      Dict with media flags/URLs, duration, peaks, cues and format config.
      `media_rev` is "<name>:<mtime_ns>:<size>" of the active file (or None);
      the UI uses it as a cache-buster for `/api/media` so the <audio>/<video>
      element reloads only when the underlying file actually changes.
    """
    with self.lock:
      media_path = self.media_path
      wav_path = self.wav_path
      exported_video_path = self.exported_video_path
      media_name = self.media_name
      duration_ms = self.duration_ms
      peaks = list(self.peaks) if include_peaks else None
      word_count = len(self.words)
      cues = [c.to_dict() for c in self.cues]
      format_config = self.format_config.to_dict()
      demo_mode = self.demo_mode
    # File I/O below runs without the lock (see docstring).
    is_video = bool(
        media_path and media_path.suffix.lower() in _BROWSER_VIDEO_EXTENSIONS
    )
    can_burnin = bool(
        media_path and media_path.suffix.lower() in _VIDEO_EXTENSIONS
    )
    has_exported_video = bool(
        exported_video_path and exported_video_path.is_file()
    )
    media_rev = _file_rev(media_path or wav_path)
    return {
        "has_media": wav_path is not None or media_path is not None,
        "media_name": media_name,
        "media_rev": media_rev,
        "media_url": "/api/media" if (wav_path or media_path) else None,
        "is_video": is_video,
        "can_burnin_video": can_burnin,
        "exported_video_url": (
            "/api/exported-video" if has_exported_video else None
        ),
        "duration_ms": duration_ms,
        "peaks": peaks,
        "word_count": word_count,
        "cue_count": len(cues),
        "cues": cues,
        "format_config": format_config,
        "demo_mode": demo_mode,
    }

  def _discard_replaced_media(
      self, *old_paths: Path | None, keep: tuple[Path | None, ...] = ()
  ) -> None:
    """Deletes cache files of media that was just replaced.

    Prevents the cache from growing with every upload. Only two kinds of
    files are ever deleted, and only if they live inside the cache dir:
    previous uploads (`uploads_dir/*`) and transcription working WAVs
    (`cache_dir/working_*_16k.wav`). The Resolve inbox WAV, the demo WAV,
    burn-in outputs and any file outside the cache are never touched, nor is
    a file that is still the active media/WAV (e.g. a concurrent request
    re-activated it). Must be called without holding `self.lock`.

    Args:
      *old_paths: Paths that were active before the swap (None is ignored).
      keep: Paths that must survive (typically the new media/WAV).
    """
    with self.lock:
      in_use = [self.media_path, self.wav_path]
    protected = set()
    for p in (*keep, *in_use):
      if p is not None:
        protected.add(p.resolve())
    cache_root = self.cache_dir.resolve()
    uploads_root = self.uploads_dir.resolve()
    for old in old_paths:
      if old is None:
        continue
      resolved = old.resolve()
      if resolved in protected:
        continue
      is_upload = resolved.parent == uploads_root
      is_working_wav = (
          resolved.parent == cache_root
          and resolved.name.startswith("working_")
          and resolved.name.endswith("_16k.wav")
      )
      if is_upload or is_working_wav:
        try:
          resolved.unlink(missing_ok=True)
        except OSError:
          pass  # Best effort: a leftover cache file is harmless.

  def load_demo(
      self, format_raw: dict[str, Any] | None = None
  ) -> dict[str, Any]:
    """Populates a Spanish board-game + Resolve demo session.

    Generates a synthetic demo WAV (tones aligned to the cue windows) and a
    hard-coded list of deliberately misspelled anglicisms ("eurogueim",
    "mipels") so the user can see the Spanish NLP corrections without having
    whisper or a model installed. Replaces the active media and bumps
    `media_generation`, which invalidates any running transcription.

    Args:
      format_raw: Optional `SpanishFormatConfig` dict from the UI; when None
        the current `format_config` is reused.

    Returns:
      The new `session_snapshot()`.
    """
    # (text, start_ms, end_ms) word timings, as whisper would emit them.
    demo_specs: list[tuple[str, int, int]] = [
        ("Hola,", 320, 680),
        ("hoy", 700, 960),
        ("reseñamos", 980, 1620),
        ("un", 1640, 1800),
        ("eurogueim", 1820, 2480),
        ("con", 2500, 2720),
        ("mipels", 2740, 3260),
        ("y", 3280, 3420),
        ("dec", 3440, 3680),
        ("bilding.", 3700, 4120),
        ("eh", 4220, 4440),
        ("Sabías", 4520, 4980),
        ("que", 5000, 5180),
        ("el", 5200, 5360),
        ("setap", 5380, 5820),
        ("en", 5840, 5980),
        ("pleimat", 6000, 6540),
        ("reduce", 6560, 7020),
        ("el", 7040, 7180),
        ("dauntaim", 7200, 7820),
        ("del", 7840, 8040),
        ("worker", 8060, 8540),
        ("pleisment?", 8560, 9160),
        ("Con", 9420, 9700),
        ("32", 9720, 10020),
        ("GB", 10040, 10360),
        ("de", 10380, 10520),
        ("RAM", 10540, 10940),
        ("y", 10960, 11100),
        ("tu", 11120, 11280),
        ("Polaris", 11300, 11840),
        ("libre,", 11860, 12320),
        ("reconoce", 12340, 12920),
        ("kikstarter", 12940, 13680),
        ("y", 13700, 13840),
        ("be", 13860, 14120),
        ("ge", 14140, 14400),
        ("ge.", 14420, 14740),
        ("Qué", 15060, 15320),
        ("cómodo", 15340, 15820),
        ("usar", 15840, 16180),
        ("la", 16200, 16360),
        ("app", 16380, 16660),
        ("sola", 16680, 17000),
        ("o", 17020, 17140),
        ("con", 17160, 17360),
        ("DaVinci", 17380, 17760),
        ("Resolve", 17780, 18080),
        ("20!", 18100, 18360),
    ]
    words = [
        spanish_nlp.WordTiming(text=t, start_ms=s, end_ms=e, prob=0.96)
        for t, s, e in demo_specs
    ]
    demo_wav = self.cache_dir / "demo_autosubs_es.wav"
    resolve_bridge.generate_demo_wav(
        str(demo_wav),
        cue_windows_ms=[
            (320, 4120),
            (4520, 9160),
            (9420, 14740),
            (15060, 18360),
        ],
        duration_ms=18800,
    )
    wave_info = resolve_bridge.extract_waveform_peaks(str(demo_wav))
    cfg = (
        spanish_nlp.SpanishFormatConfig.from_dict(format_raw)
        if format_raw
        else self.format_config
    )
    cues = spanish_nlp.segment_and_wrap_spanish(words, cfg)

    with self.lock:
      old_media, old_wav = self.media_path, self.wav_path
      self.media_path = demo_wav
      self.wav_path = demo_wav
      self.exported_video_path = None
      self.media_name = "Reseña_JuegosDeMesa_ES.wav"
      self.duration_ms = int(wave_info["duration_ms"])
      self.peaks = list(wave_info["peaks"])
      self.words = words
      self.cues = cues
      self.format_config = cfg
      self._last_inbox_sig = None
      self.media_generation += 1
      self.demo_mode = True
    self._discard_replaced_media(old_media, old_wav, keep=(demo_wav,))
    return self.session_snapshot()

  def _new_job_locked(self, kind: str, detail: str = "") -> str:
    """Registers a new running job; caller must already hold `self.lock`.

    Split out of `create_job()` so dedupe checks (transcription, benchmark)
    can look for an active job and insert a new one atomically.

    Args:
      kind: Job type shown to the UI ("transcribe", "benchmark", "burnin").
      detail: Initial Spanish stage text; defaults to "Iniciando...".

    Returns:
      The new 10-hex-char job id.
    """
    job_id = uuid.uuid4().hex[:10]
    self.jobs[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "progress": 0,
        "stage": detail or "Iniciando...",
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
    }
    self.cancel_flags[job_id] = False
    if len(self.jobs) > _MAX_RETAINED_JOBS:
      # Dicts keep insertion order, so this drops the oldest finished jobs
      # first. Running jobs are never pruned.
      stale_ids = [
          jid for jid, j in self.jobs.items() if j.get("status") != "running"
      ]
      for old_id in stale_ids[: max(0, len(self.jobs) - _MAX_RETAINED_JOBS)]:
        self.jobs.pop(old_id, None)
        self.cancel_flags.pop(old_id, None)
    return job_id

  def _active_job_locked(self, job_id: str | None) -> str | None:
    """Returns `job_id` if that job is still queued/running, else None.

    Caller must hold `self.lock`. Used by the dedupe slots
    (`_active_transcription`, `_active_benchmark`).
    """
    if job_id:
      job = self.jobs.get(job_id)
      if job and job.get("status") in _ACTIVE_JOB_STATUSES:
        return job_id
    return None

  def create_job(self, kind: str, detail: str = "") -> str:
    """Registers a new running job and prunes old finished jobs.

    Args:
      kind: Job type shown to the UI ("transcribe", "benchmark", "burnin").
      detail: Initial Spanish stage text; defaults to "Iniciando...".

    Returns:
      The new 10-hex-char job id.
    """
    with self.lock:
      return self._new_job_locked(kind, detail)

  def update_job(self, job_id: str, **updates: Any) -> None:
    """Merges `updates` into a job dict under the lock (no-op if pruned).

    Args:
      job_id: Job to update.
      **updates: Job keys to overwrite (status, progress, stage, ...).
    """
    with self.lock:
      if job_id in self.jobs:
        self.jobs[job_id].update(updates)

  def cancel_job(self, job_id: str) -> str | None:
    """Flags a queued/running job as cancelled and kills its child process.

    The job is marked "cancelled" immediately; the worker thread notices
    `cancel_flags[job_id]` at its next check and returns without writing a
    result. The child is terminated outside the lock. Jobs that already
    finished (done/error/cancelled) are left untouched, so a late click on
    "Cancelar" can never turn a finished transcription into "cancelled".

    Args:
      job_id: Job to cancel.

    Returns:
      None if the job id is unknown, otherwise the job's status after the
      call ("cancelled", or its unchanged final status).
    """
    with self.lock:
      job = self.jobs.get(job_id)
      if job is None:
        return None
      if job.get("status") not in _ACTIVE_JOB_STATUSES:
        return str(job.get("status"))
      self.cancel_flags[job_id] = True
      proc = self.active_procs.get(job_id)
      job["status"] = "cancelled"
      job["stage"] = "Cancelado por el usuario"
      job["finished_at"] = time.time()
    if proc is not None and proc.poll() is None:
      _terminate_process(proc)
    return "cancelled"

  def settings_payload(
      self, resolve_running: bool | None = None
  ) -> dict[str, Any]:
    """This Mac's profile, saved settings and the resulting parameters.

    Args:
      resolve_running: Whether DaVinci Resolve is running (it changes the
        effective thread count / GPU use). Detected via process list when
        None.

    Returns:
      Dict with `settings` (saved), `effective` (resolved performance
      parameters), `profile` (detected hardware), `recommended_model_id` and
      `settings_path`.
    """
    profile = hardware_imac2017.detect_hardware_profile()
    data = self.settings.snapshot()
    is_running = (
        resolve_running
        if resolve_running is not None
        else hardware_imac2017.is_resolve_running()
    )
    effective = machine_settings.resolve_performance(
        profile, data["performance"], is_running
    )
    return {
        "settings": data,
        "effective": effective,
        "profile": dataclasses.asdict(profile),
        "recommended_model_id": machine_settings.recommended_model_id(profile),
        "settings_path": str(self.settings.path),
    }

  def effective_performance(self) -> dict[str, Any]:
    """Returns the effective performance parameters (model, threads, GPU)."""
    return self.settings_payload()["effective"]

  def _make_benchmark_wav(self, out_wav: Path, ffmpeg_bin: str | None) -> float:
    """Builds ~30 s of Spanish speech (macOS `say`), or falls back to tones.

    Tries Spanish voices (Paulina, Mónica, then the system default) and
    converts the AIFF to 16 kHz mono with ffmpeg. Without `say` or ffmpeg a
    synthetic 30 s tone WAV is used, which still times whisper but less
    realistically.

    Args:
      out_wav: Destination WAV path.
      ffmpeg_bin: ffmpeg executable, or None if not installed.

    Returns:
      Audio duration in seconds.
    """
    text = (
        "Hola, hoy reseñamos un juego de mesa con mucha estrategia. Cada"
        " jugador coloca sus trabajadores, junta recursos y construye su"
        " ciudad. ¿Vale la pena comprarlo? En nuestra opinión, sí: las"
        " partidas duran una hora, las reglas se aprenden rápido y la"
        " caja trae componentes de muy buena calidad. "
    ) * 2
    say_bin = shutil.which("say")
    aiff = out_wav.with_suffix(".aiff")
    if say_bin and ffmpeg_bin:
      for voice in ("Paulina", "Mónica", ""):
        cmd = [say_bin, "-o", str(aiff), text]
        if voice:
          cmd[1:1] = ["-v", voice]
        try:
          res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
          continue
        if res.returncode == 0 and aiff.is_file():
          try:
            resolve_bridge.prepare_wav_16k(
                str(aiff), str(out_wav), ffmpeg_bin=ffmpeg_bin,
                enhance_voice=False,
            )
          finally:
            aiff.unlink(missing_ok=True)
          info = resolve_bridge.extract_waveform_peaks(str(out_wav), 16)
          if info["duration_ms"] > 0:
            return info["duration_ms"] / 1000.0
    resolve_bridge.generate_demo_wav(
        str(out_wav), cue_windows_ms=[(0, 30000)], duration_ms=30000
    )
    return 30.0

  def start_benchmark(self, payload: dict[str, Any]) -> str:
    """Times whisper-cli on ~30 s of speech with this Mac's settings.

    Validation runs synchronously so errors surface as HTTP 400; the work
    runs on a daemon thread. Stages: build test WAV (progress 0) ->
    cancellation check -> run whisper-cli at progress 20 (nice 0 so the
    timing is not skewed) -> cancellation check -> compute real-time factor
    (audio seconds / wall seconds), store it via
    `settings.record_benchmark()` and finish at 100. Temp WAV/JSON files are
    always removed. Does not touch the active session. If a benchmark is
    already queued/running, its job id is returned instead of starting a
    second one.

    Args:
      payload: Optional overrides `model_id`, `threads`, `force_no_gpu`;
        missing keys fall back to the effective settings.

    Returns:
      The job id to poll (the in-flight one when deduplicated).

    Raises:
      ValueError: Unknown model id.
      FileNotFoundError: Model not downloaded.
      RuntimeError: whisper-cli not installed.
    """
    effective = self.effective_performance()
    model_id = str(payload.get("model_id") or effective["model_id"])
    spec = hardware_imac2017.MODEL_CATALOG.get(model_id)
    if not spec:
      raise ValueError(f"Modelo inválido: {model_id}")
    model_path = self.models_dir / spec.filename
    if not model_path.is_file():
      raise FileNotFoundError(
          f"Descarga primero el modelo {spec.label} para probar su velocidad."
      )
    env = hardware_imac2017.detect_environment()
    whisper_bin = env.get("whisper_cli_path")
    if not whisper_bin:
      raise RuntimeError(
          "No se encontró whisper-cli. Instala con: brew install whisper-cpp"
      )
    threads = int(payload.get("threads") or effective["threads"])
    force_no_gpu = bool(payload.get("force_no_gpu", effective["force_no_gpu"]))
    with self.lock:
      # Dedupe: a second click while a benchmark runs joins it, so two
      # whisper-cli runs never compete for the CPU and skew the timing.
      existing_job_id = self._active_job_locked(self._active_benchmark)
      if existing_job_id:
        return existing_job_id
      job_id = self._new_job_locked(
          "benchmark", "Preparando audio de prueba..."
      )
      self._active_benchmark = job_id

    def _worker() -> None:
      """Runs the benchmark stages and writes the job result/error."""
      bench_wav = self.cache_dir / f"bench_{job_id}.wav"
      out_prefix = self.cache_dir / f"bench_{job_id}"
      try:
        audio_s = self._make_benchmark_wav(bench_wav, env.get("ffmpeg_path"))
        with self.lock:
          if self.cancel_flags.get(job_id):
            return
        self.update_job(
            job_id,
            progress=20,
            stage=f"Transcribiendo {audio_s:.0f} s con {spec.label}...",
        )
        cmd = hardware_imac2017.build_whisper_command(
            whisper_bin=whisper_bin,
            model_path=str(model_path),
            wav_path=str(bench_wav),
            output_prefix=str(out_prefix),
            prompt="",
            threads=threads,
            force_no_gpu=force_no_gpu,
            fast_beam=bool(effective["fast_beam"]),
            nice_level=0,
        )
        started = time.monotonic()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        with self.lock:
          self.active_procs[job_id] = proc
        rc = proc.wait()
        # Floor avoids division by zero if whisper exits instantly.
        elapsed = max(0.01, time.monotonic() - started)
        with self.lock:
          self.active_procs.pop(job_id, None)
          if self.cancel_flags.get(job_id):
            return
        if rc != 0:
          raise RuntimeError(f"whisper-cli terminó con código {rc}.")
        rtf = audio_s / elapsed
        rec = machine_settings.recommend_from_benchmark(model_id, rtf)
        result = {
            "model_id": model_id,
            "realtime_factor": round(rtf, 2),
            "elapsed_s": round(elapsed, 1),
            "audio_s": round(audio_s, 1),
            "threads": threads,
            "force_no_gpu": force_no_gpu,
            "recommended_model_id": rec["model_id"],
            "note": rec["note"],
        }
        self.settings.record_benchmark(model_id, result)
        self.update_job(
            job_id,
            status="done",
            progress=100,
            stage=f"{rtf:.1f}× tiempo real con {spec.label}",
            finished_at=time.time(),
            result=result,
        )
      except Exception as exc:  # pylint: disable=broad-except
        self.update_job(
            job_id,
            status="error",
            stage="Error en la prueba de velocidad",
            error=str(exc),
            finished_at=time.time(),
        )
      finally:
        bench_wav.unlink(missing_ok=True)
        Path(f"{out_prefix}.json").unlink(missing_ok=True)
        # Release the dedupe slot only if it still points at this job.
        with self.lock:
          if self._active_benchmark == job_id:
            self._active_benchmark = None

    threading.Thread(target=_worker, daemon=True).start()
    return job_id

  def start_model_download(self, model_id: str) -> str:
    """Starts (or joins) a resumable download of a whisper GGML model.

    Repeated calls for a model whose download is still running return the
    existing job id (tracked in `_active_downloads`). The job dict is created
    inline rather than via `create_job()` so the dedupe check and insertion
    happen atomically under one lock acquisition.

    Worker stages: skip if the model file already exists (> 1 MiB) ->
    request with `Range: bytes=<part size>-` to resume a `.part` file ->
    handle HTTP 416 (the `.part` may already be complete) -> stream 256 KiB
    chunks, checking `cancel_flags` before each chunk and reporting progress
    capped at 99 -> atomically rename `.part` to the final file -> done.
    On cancellation the `.part` file is kept so the next attempt resumes.

    Args:
      model_id: Key in `hardware_imac2017.MODEL_CATALOG`.

    Returns:
      The job id to poll.

    Raises:
      ValueError: Unknown model id.
    """
    spec = hardware_imac2017.MODEL_CATALOG.get(model_id)
    if not spec:
      raise ValueError(f"Modelo desconocido: {model_id}")

    with self.lock:
      existing_job_id = self._active_downloads.get(model_id)
      if existing_job_id:
        existing_job = self.jobs.get(existing_job_id)
        if existing_job and existing_job.get("status") in ("queued", "running"):
          return existing_job_id
      job_id = uuid.uuid4().hex[:10]
      self.jobs[job_id] = {
          "job_id": job_id,
          "kind": "download",
          "status": "running",
          "progress": 0,
          "stage": f"Conectando con Hugging Face ({spec.filename})...",
          "error": None,
          "started_at": time.time(),
          "finished_at": None,
          "result": None,
      }
      self.cancel_flags[job_id] = False
      self._active_downloads[model_id] = job_id

    def _worker() -> None:
      """Downloads into `<model>.part`, then renames it into place."""
      dest = self.models_dir / spec.filename
      part = self.models_dir / f"{spec.filename}.part"
      try:
        # Anything over 1 MiB is treated as a complete model (no checksum).
        if dest.is_file() and dest.stat().st_size > 1024 * 1024:
          self.update_job(
              job_id,
              status="done",
              progress=100,
              stage=f"Modelo {spec.filename} ya disponible.",
              finished_at=time.time(),
              result={"model_id": model_id, "path": str(dest)},
          )
          return

        existing = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": "AutoSubsES/1.0 (macOS x86_64)"}
        req = urllib.request.Request(spec.url, headers=headers)
        if existing > 0:
          req.add_header("Range", f"bytes={existing}-")

        try:
          resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
          # 416 Range Not Satisfiable: the resume offset is at/after EOF.
          # Learn the real size from Content-Range ("bytes */<total>") or a
          # HEAD request to decide whether the .part is already complete.
          if exc.code == 416 and existing > 0:
            total_size = 0
            cr = exc.headers.get("Content-Range", "") if exc.headers else ""
            exc.close()
            m_cr = re.search(r"/(\d+)\s*$", cr)
            if m_cr:
              total_size = int(m_cr.group(1))
            else:
              head_req = urllib.request.Request(
                  spec.url, headers=headers, method="HEAD"
              )
              try:
                with urllib.request.urlopen(head_req, timeout=15) as hresp:
                  total_size = int(hresp.headers.get("Content-Length") or 0)
              except Exception:  # pylint: disable=broad-except
                total_size = 0
            # Only an exact size match means the .part is complete; a larger
            # file means the upstream model changed, so restart from zero.
            if total_size > 0 and existing == total_size:
              part.replace(dest)
              self.update_job(
                  job_id,
                  status="done",
                  progress=100,
                  stage=f"Modelo {spec.filename} instalado correctamente.",
                  finished_at=time.time(),
                  result={"model_id": model_id, "path": str(dest)},
              )
              return
            part.unlink(missing_ok=True)
            existing = 0
            req = urllib.request.Request(spec.url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30)
          else:
            raise

        with resp:
          # Append only if the server honored the Range (206); a plain 200
          # means it sent the whole file, so restart the .part from zero.
          status_code = getattr(resp, "status", 200)
          mode = "ab" if (existing > 0 and status_code == 206) else "wb"
          if mode == "wb":
            existing = 0
          content_len = int(resp.headers.get("Content-Length") or 0)
          # Fall back to the catalog size when Content-Length is missing.
          total = (
              (existing + content_len)
              if content_len > 0
              else spec.size_mb * 1024 * 1024
          )
          downloaded = existing

          with open(part, mode) as fp:
            while True:
              # Cancellation point: checked before every 256 KiB chunk.
              with self.lock:
                if self.cancel_flags.get(job_id):
                  return
              chunk = resp.read(262144)
              if not chunk:
                break
              fp.write(chunk)
              downloaded += len(chunk)
              # Capped at 99 so "100" always means installed.
              pct = min(99, int((downloaded * 100) / max(1, total)))
              mb_done = downloaded / (1024 * 1024)
              mb_tot = total / (1024 * 1024)
              self.update_job(
                  job_id,
                  progress=pct,
                  stage=(
                      f"Descargando {spec.filename}: {mb_done:.1f} /"
                      f" {mb_tot:.1f} MB"
                  ),
              )

        part.replace(dest)
        self.update_job(
            job_id,
            status="done",
            progress=100,
            stage=f"Modelo {spec.filename} instalado correctamente.",
            finished_at=time.time(),
            result={"model_id": model_id, "path": str(dest)},
        )
      except Exception as exc:  # pylint: disable=broad-except
        self.update_job(
            job_id,
            status="error",
            stage="Error al descargar modelo",
            error=str(exc),
            finished_at=time.time(),
        )
      finally:
        # Release the dedupe slot only if it still points at this job.
        with self.lock:
          if self._active_downloads.get(model_id) == job_id:
            self._active_downloads.pop(model_id, None)

    threading.Thread(target=_worker, daemon=True).start()
    return job_id

  def start_transcription(self, payload: dict[str, Any]) -> str:
    """Validates inputs and starts an offline Spanish transcription job.

    Input resolution (synchronous, errors become HTTP 400): pick the model
    (payload, else the saved model if downloaded, else the best installed),
    locate whisper-cli/ffmpeg, and resolve the source: `source == "inbox"`
    uses the Resolve inbox WAV; `media_path` must lie inside `app_dir` (path
    traversal guard); otherwise the active `media_path` is used.

    Worker stages and progress: ffmpeg -> 16 kHz mono WAV (0..15) ->
    cancellation check -> waveform peaks -> whisper-cli with a Spanish
    initial prompt (15..94, scaled from whisper's own "progress = N%" lines,
    cancellation checked per output line) -> cancellation check -> parse the
    JSON (OJF) output and run `segment_and_wrap_spanish` (96) -> done (100).

    State written on success: media_path, wav_path, exported_video_path
    (cleared), media_name, duration_ms, peaks, words, cues, format_config,
    demo_mode, and `_last_inbox_sig` when the source is the Resolve inbox
    (so a later "Leer buzón"/notify-export of the same WAV keeps the cues).
    Skipped if `media_generation` changed since the job started, i.e. the
    user loaded other media; the job still reports done but its result
    carries `"stale": true`.

    Only one transcription runs at a time: if one is already queued/running
    its job id is returned (a double-click must not start two whisper-cli
    processes).

    Temp files: the whisper JSON is always deleted; the new working WAV is
    kept only if it became the session WAV, in which case the previous
    `working_*_16k.wav` (and a replaced upload) in the cache dir is deleted.

    Args:
      payload: UI request; optional keys `source`, `media_path`, `model_id`,
        `format_config`, `threads`, `fast_beam`, `enhance_voice`,
        `force_no_gpu`.

    Returns:
      The job id to poll (the in-flight one when deduplicated).

    Raises:
      ValueError: Unknown model id.
      FileNotFoundError: Missing model, inbox audio, or source file.
      PermissionError: `media_path` outside the app data directory.
      RuntimeError: whisper-cli not installed.
    """
    source = str(payload.get("source", "uploaded"))
    effective = self.effective_performance()
    model_id = str(payload.get("model_id") or "")
    if not model_id:
      # This Mac's saved model if it is downloaded, else the best installed.
      saved = hardware_imac2017.MODEL_CATALOG[effective["model_id"]]
      model_id = (
          effective["model_id"]
          if (self.models_dir / saved.filename).is_file()
          else hardware_imac2017.choose_default_model_id(self.models_dir)
      )
    spec = hardware_imac2017.MODEL_CATALOG.get(model_id)
    if not spec:
      raise ValueError(f"Modelo inválido: {model_id}")

    model_path = self.models_dir / spec.filename
    if not model_path.is_file():
      raise FileNotFoundError(
          f"El modelo {spec.filename} no está descargado todavía."
      )

    env = hardware_imac2017.detect_environment()
    whisper_bin = env.get("whisper_cli_path")
    if not whisper_bin:
      raise RuntimeError(
          "No se encontró whisper-cli. Instala con: brew install whisper-cpp"
      )
    ffmpeg_bin = env.get("ffmpeg_path")

    if source == "inbox":
      src_path = self.bridge.get_inbox_audio_path()
      if not src_path:
        raise FileNotFoundError(
            "No hay audio exportado desde Resolve todavía. Ejecuta en Resolve:"
            " Workspace > Scripts > 1 - MTTN AutoSubs - Exportar Audio."
        )
    elif payload.get("media_path"):
      # Security: only files under the app data dir may be transcribed by
      # path, so a web page cannot make the server read arbitrary files.
      candidate = Path(str(payload["media_path"])).expanduser().resolve()
      if not candidate.is_relative_to(self.app_dir.resolve()):
        raise PermissionError(
            "Ruta fuera del directorio de trabajo de AutoSubs ES."
        )
      src_path = candidate
    else:
      with self.lock:
        src_path = self.media_path
      if not src_path:
        raise FileNotFoundError("Selecciona primero un vídeo o audio.")

    if not src_path.is_file():
      raise FileNotFoundError(f"No existe el archivo: {src_path}")
    inbox_sig: tuple[str, int, int] | None = None
    if source == "inbox":
      # Same (path, mtime_ns, size) signature notify-export computes. Taken
      # before ffmpeg reads the file, so if Resolve re-exports mid-job the
      # next notify-export still sees a change and reloads.
      st = src_path.stat()
      inbox_sig = (str(src_path), st.st_mtime_ns, st.st_size)

    cfg = spanish_nlp.SpanishFormatConfig.from_dict(
        payload.get("format_config") or self.format_config.to_dict()
    )
    threads = payload.get("threads") or effective["threads"]
    fast_beam = bool(payload.get("fast_beam", effective["fast_beam"]))
    enhance_voice = bool(payload.get("enhance_voice", effective["enhance_voice"]))
    force_no_gpu = bool(payload.get("force_no_gpu", effective["force_no_gpu"]))
    accel_label = "CPU, GPU libre" if force_no_gpu else "GPU Metal"

    with self.lock:
      # Dedupe check and job creation share one critical section so two
      # concurrent requests cannot both start a job.
      existing_job_id = self._active_job_locked(self._active_transcription)
      if existing_job_id:
        return existing_job_id
      # Snapshot used later to detect that the media changed mid-job.
      expected_generation = self.media_generation
      job_id = self._new_job_locked(
          "transcribe", "Preparando audio 16 kHz mono..."
      )
      self._active_transcription = job_id

    def _worker() -> None:
      """Runs ffmpeg -> whisper-cli -> Spanish NLP and swaps in the result."""
      working_wav = self.cache_dir / f"working_{job_id}_16k.wav"
      out_prefix = self.cache_dir / f"whisper_{job_id}"
      ojf_json = Path(f"{out_prefix}.json")
      completed_ok = False
      old_wav: Path | None = None
      old_media: Path | None = None
      try:
        resolve_bridge.prepare_wav_16k(
            str(src_path),
            str(working_wav),
            ffmpeg_bin=ffmpeg_bin,
            enhance_voice=enhance_voice,
        )
        with self.lock:
          if self.cancel_flags.get(job_id):
            return
        wave_info = _safe_waveform_peaks(working_wav)
        self.update_job(
            job_id,
            progress=15,
            stage=(
                f"Transcribiendo en español con {spec.label}"
                f" ({accel_label})..."
            ),
        )

        prompt = spanish_nlp.build_spanish_initial_prompt(
            cfg.dialect, cfg.custom_glossary, domain=cfg.domain
        )
        cmd = hardware_imac2017.build_whisper_command(
            whisper_bin=whisper_bin,
            model_path=str(model_path),
            wav_path=str(working_wav),
            output_prefix=str(out_prefix),
            prompt=prompt,
            threads=int(threads) if threads is not None else None,
            force_no_gpu=force_no_gpu,
            fast_beam=fast_beam,
        )

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
        with self.lock:
          self.active_procs[job_id] = proc

        progress_re = re.compile(r"progress\s*=\s*(\d+)%")
        if proc.stdout:
          for line in proc.stdout:
            with self.lock:
              if self.cancel_flags.get(job_id):
                break
            match = progress_re.search(line)
            if match:
              # Map whisper's 0..100% onto the job's 15..93 band.
              wpct = int(match.group(1))
              scaled = 15 + int(wpct * 0.78)
              self.update_job(
                  job_id,
                  progress=min(94, scaled),
                  stage=f"Transcribiendo audio en español ({wpct}%)...",
              )

        rc = proc.wait()
        if proc.stdout:
          proc.stdout.close()  # Release the pipe fd once the child exited.
        with self.lock:
          self.active_procs.pop(job_id, None)
          if self.cancel_flags.get(job_id):
            return

        if rc != 0 or not ojf_json.is_file():
          raise RuntimeError(
              f"whisper-cli terminó con código {rc}. Comprueba los permisos y"
              " el modelo."
          )

        self.update_job(
            job_id,
            progress=96,
            stage="Aplicando reglas RAE, tildes y signos ¿ ¡...",
        )
        ojf_data = json.loads(ojf_json.read_text(encoding="utf-8"))
        words = spanish_nlp.parse_whisper_ojf(ojf_data)
        cues = spanish_nlp.segment_and_wrap_spanish(words, cfg)

        stale = False
        with self.lock:
          # Discard the result if the user loaded other media meanwhile.
          if self.media_generation != expected_generation:
            stale = True
          else:
            old_wav = self.wav_path
            old_media = self.media_path
            self.media_path = src_path
            self.wav_path = working_wav
            self.exported_video_path = None
            self.media_name = src_path.name
            self.duration_ms = int(wave_info["duration_ms"])
            self.peaks = list(wave_info["peaks"])
            self.words = words
            self.cues = cues
            self.format_config = cfg
            self.demo_mode = False
            if inbox_sig is not None:
              # Mark this inbox WAV as already loaded; otherwise the next
              # notify-export ("Leer buzón") would see an unknown signature
              # and wipe the cues just produced.
              self._last_inbox_sig = inbox_sig
        if not stale:
          completed_ok = True

        result = self.session_snapshot()
        # Lets the UI tell a discarded result (media changed mid-job) from a
        # real one; the snapshot then describes the *new* media.
        result["stale"] = stale
        self.update_job(
            job_id,
            status="done",
            progress=100,
            stage=(
                "Transcripción descartada: el audio activo cambió durante"
                " el proceso."
                if stale
                else f"¡Listo! {len(cues)} subtítulos generados en español."
            ),
            finished_at=time.time(),
            result=result,
        )
      except Exception as exc:  # pylint: disable=broad-except
        self.update_job(
            job_id,
            status="error",
            stage="Error durante la transcripción",
            error=str(exc),
            finished_at=time.time(),
        )
      finally:
        ojf_json.unlink(missing_ok=True)
        if not completed_ok:
          working_wav.unlink(missing_ok=True)
        else:
          # Only our own temp working WAVs and replaced uploads are deleted
          # (never the Resolve inbox WAV or files outside the cache dir).
          self._discard_replaced_media(
              old_wav, old_media, keep=(working_wav, src_path)
          )
        # Release the dedupe slot only if it still points at this job.
        with self.lock:
          if self._active_transcription == job_id:
            self._active_transcription = None

    threading.Thread(target=_worker, daemon=True).start()
    return job_id

  def start_video_burnin(self, payload: dict[str, Any]) -> str:
    """Burns styled .ass subtitles onto local video using CPU libx264.

    Uses the hardware profile's encoder (h264_videotoolbox on supported
    Macs) when the libass-capable ffmpeg supports it, else libx264.
    Validation is synchronous (errors become HTTP 400); the cue list and
    source path are snapshotted under the lock so later edits do not affect
    a running render.

    Worker stages: write the karaoke ASS file -> encode (progress 25,
    cancellation checked per ffmpeg output line) -> cancellation check ->
    on success set `exported_video_path` and finish at 100 with a download
    URL. The temp ASS file is always deleted; the MP4 stays in the cache dir.

    Args:
      payload: Optional `style` (exporters.StyleConfig dict), `width`,
        `height` (ASS play resolution, px, default 1920x1080), `threads`.

    Returns:
      The job id to poll.

    Raises:
      FileNotFoundError: No local media loaded.
      ValueError: Active media is audio-only, or there are no cues.
      RuntimeError: ffmpeg missing or built without libass.
    """
    with self.lock:
      src_video = self.media_path
      cues = list(self.cues)
      media_name = self.media_name or "video_subtitulado"
    if not src_video or not src_video.is_file():
      raise FileNotFoundError(
          "Sube primero un archivo de vídeo local (.mp4/.mov/.mkv)."
      )
    if src_video.suffix.lower() not in _VIDEO_EXTENSIONS:
      raise ValueError(
          "El archivo activo es solo audio. Para renderizar vídeo subtitulado"
          " sube primero un .mp4, .mov o .mkv."
      )
    if not cues:
      raise ValueError("No hay subtítulos activos para quemar en el vídeo.")

    env = hardware_imac2017.detect_environment()
    if not env.get("ffmpeg_path"):
      raise RuntimeError(
          "No se encontró ffmpeg. Instala con: brew install ffmpeg-full"
      )
    ffmpeg_bin = hardware_imac2017.find_burnin_ffmpeg()
    if not ffmpeg_bin:
      raise RuntimeError(
          "Tu FFmpeg no incluye libass (filtro 'ass') para quemar"
          " subtítulos. Instala con: brew install ffmpeg-full"
      )
    profile = hardware_imac2017.detect_hardware_profile()
    video_encoder = profile.video_encoder
    if video_encoder != "libx264" and not (
        hardware_imac2017.ffmpeg_supports_encoder(ffmpeg_bin, video_encoder)
    ):
      video_encoder = "libx264"
    encoder_label = (
        "VideoToolbox por hardware"
        if video_encoder == "h264_videotoolbox"
        else "CPU libx264"
    )

    style = exporters.StyleConfig.from_dict(payload.get("style"))
    width = int(payload.get("width") or 1920)
    height = int(payload.get("height") or 1080)
    threads = int(
        payload.get("threads") or self.effective_performance()["threads"]
    )
    job_id = self.create_job(
        "burnin", f"Preparando render MP4 ({encoder_label})..."
    )

    def _worker() -> None:
      """Writes the ASS file, runs ffmpeg and publishes the MP4."""
      ass_file = self.cache_dir / f"burnin_{job_id}.ass"
      base_stem = _safe_filename(Path(media_name).stem)
      out_mp4 = self.cache_dir / f"{base_stem}_subtitulado_{job_id}.mp4"
      try:
        ass_content = exporters.export_ass_karaoke(cues, style, width, height)
        ass_file.write_text(ass_content, encoding="utf-8")
        self.update_job(
            job_id,
            progress=25,
            stage=f"Codificando MP4 con subtítulos ({encoder_label})...",
        )
        cmd = hardware_imac2017.build_ffmpeg_burnin_command(
            ffmpeg_bin=ffmpeg_bin,
            input_video_path=str(src_video),
            ass_path=str(ass_file),
            output_mp4_path=str(out_mp4),
            threads=threads,
            fonts_dir=str(self.fonts.app_fonts_dir),
            video_encoder=video_encoder,
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
        with self.lock:
          self.active_procs[job_id] = proc
        if proc.stdout:
          # Output is drained (not parsed) so the pipe never fills up and
          # blocks ffmpeg; each line is also a cancellation point.
          for _ in proc.stdout:
            with self.lock:
              if self.cancel_flags.get(job_id):
                break
        rc = proc.wait()
        if proc.stdout:
          proc.stdout.close()  # Release the pipe fd once the child exited.
        with self.lock:
          self.active_procs.pop(job_id, None)
          if self.cancel_flags.get(job_id):
            return
        if rc != 0 or not out_mp4.is_file():
          raise RuntimeError(
              f"ffmpeg terminó con código {rc} al quemar subtítulos."
          )
        with self.lock:
          self.exported_video_path = out_mp4
        self.update_job(
            job_id,
            status="done",
            progress=100,
            stage=f"¡Vídeo listo! {out_mp4.name}",
            finished_at=time.time(),
            result={
                "download_url": "/api/exported-video",
                "filename": out_mp4.name,
                "output_path": str(out_mp4),
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self.update_job(
            job_id,
            status="error",
            stage="Error al exportar vídeo MP4",
            error=str(exc),
            finished_at=time.time(),
        )
      finally:
        ass_file.unlink(missing_ok=True)

    threading.Thread(target=_worker, daemon=True).start()
    return job_id


def _safe_filename(raw_name: str) -> str:
  """Sanitizes a filename for use inside the cache dir.

  Security: keeps only the basename (dropping any directory components such
  as "../"), and replaces every character outside ASCII alphanumerics, ".",
  "_", "-" and Latin-1/Latin Extended-A letters (U+00C0..U+017F, so Spanish
  accents survive) with "_". The bare names "." and ".." are rejected.

  The input is NOT URL-decoded here: callers that receive a percent-encoded
  name (the X-Filename upload header) decode it exactly once themselves, so
  a literal "%41" in a real filename is never decoded a second time.

  Args:
    raw_name: Already-decoded filename (e.g. the decoded X-Filename header
      or the active media name).

  Returns:
    A safe basename; "media_upload.bin" if nothing usable remains.
  """
  base = Path(raw_name or "media.wav").name
  cleaned = re.sub(r"[^A-Za-z0-9._\-\u00C0-\u017F]", "_", base)
  if cleaned in ("", ".", ".."):
    return "media_upload.bin"
  return cleaned


# --- HTTP request handler ---


class AutoSubsHandler(BaseHTTPRequestHandler):
  """HTTP request handler serving static Studio UI and JSON APIs.

  A new instance is created per request (on its own thread) by
  `ThreadingHTTPServer`; shared data lives in `self.state`. Every verb
  first runs `_check_request_origin()`. API errors are returned as JSON
  `{"error": "<Spanish message>"}` so the UI can show them verbatim.

  Attributes:
    state: The process-wide `AppState`, injected via `functools.partial`.
  """

  # HTTP/1.1 enables keep-alive; every response must set Content-Length.
  protocol_version = "HTTP/1.1"

  def __init__(self, *args: Any, state: AppState, **kwargs: Any) -> None:
    """Stores the shared state, then lets the base class handle the request.

    Args:
      *args: Forwarded to `BaseHTTPRequestHandler` (request, address,
        server).
      state: Shared application state.
      **kwargs: Forwarded to `BaseHTTPRequestHandler`.
    """
    # Must be set before super().__init__, which processes the request.
    self.state = state
    super().__init__(*args, **kwargs)

  def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
    """Disables the base class's per-request stderr access log."""
    # Suppress noisy per-request access logs in interactive terminal.
    return

  # --- Security guard ---

  def _check_request_origin(self, mutating: bool) -> bool:
    """Rejects requests that did not come from this machine's own UI.

    Security: the server listens on loopback but any web page the user
    visits could still target it. Two defenses:
      * DNS rebinding: the Host header must be a loopback name/IP
        (`_ALLOWED_LOCAL_HOSTS`), for every verb.
      * CSRF (mutating verbs only): reject `Sec-Fetch-Site: cross-site`, an
        `Origin: null` (sandboxed iframes, file://), or any Origin whose host
        is not loopback. Requests without Origin (curl, Resolve Lua scripts)
        are allowed.
    On rejection a 403 JSON response is sent and the connection is closed.

    Args:
      mutating: True for POST/PUT, enabling the Origin checks.

    Returns:
      True if the request may proceed; False if a 403 was already sent.
    """
    host_hdr = (self.headers.get("Host") or "").strip()
    # Bracketed IPv6 literal ("[::1]:55120") vs "host:port".
    if host_hdr.startswith("[") and "]" in host_hdr:
      host_only = host_hdr[1 : host_hdr.index("]")]
    else:
      host_only = host_hdr.split(":", 1)[0]
    if host_only.lower() not in _ALLOWED_LOCAL_HOSTS:
      self.close_connection = True
      self._send_json(HTTPStatus.FORBIDDEN, {"error": "Host no permitido"})
      return False
    if mutating:
      sec_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
      if sec_site == "cross-site":
        self.close_connection = True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Origen no permitido"})
        return False
      origin = (self.headers.get("Origin") or "").strip()
      if origin:
        origin_host = (urllib.parse.urlparse(origin).hostname or "").lower()
        if origin.lower() == "null" or origin_host not in _ALLOWED_LOCAL_HOSTS:
          self.close_connection = True
          self._send_json(
              HTTPStatus.FORBIDDEN, {"error": "Origen no permitido"}
          )
          return False
    return True

  # --- Response and body helpers ---

  def _send_json(self, status: int, payload: Any) -> None:
    """Sends `payload` as UTF-8 JSON with no-store caching.

    Client disconnects (broken pipe/reset) are silently ignored.

    Args:
      status: HTTP status code.
      payload: Any `json.dumps`-serializable value.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
      self.send_response(status)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
      return

  def _read_json_body(self) -> dict[str, Any]:
    """Reads and parses a JSON object request body.

    Returns:
      The parsed object; {} when the body is empty or not a JSON object.

    Raises:
      ValueError: Invalid Content-Length, non-UTF-8 body or invalid JSON.
        The message is Spanish and safe to return to the UI as a 400.
    """
    try:
      length = int(self.headers.get("Content-Length") or 0)
    except ValueError:
      # The body length is unknown, so it cannot be skipped: drop keep-alive
      # instead of parsing leftover bytes as the next request.
      self.close_connection = True
      raise ValueError("Content-Length inválido") from None
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    if not raw:
      return {}
    try:
      parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
      raise ValueError("JSON inválido: el cuerpo no es UTF-8") from None
    except json.JSONDecodeError as exc:
      raise ValueError(f"JSON inválido: {exc}") from None
    return parsed if isinstance(parsed, dict) else {}

  # --- Static files and media streaming ---

  def _serve_static(self, rel_path: str) -> None:
    """Serves a file from `UI_DIR` with explicit UTF-8 content types.

    Security: absolute paths and any ".." component are rejected with 403.
    `do_GET` additionally only forwards a fixed allowlist of paths.

    Args:
      rel_path: URL path such as "/app.js"; "/" maps to index.html.
    """
    safe = rel_path.lstrip("/") or "index.html"
    if safe in ("", "/"):
      safe = "index.html"
    if ".." in Path(safe).parts or Path(safe).is_absolute():
      self.send_error(HTTPStatus.FORBIDDEN)
      return
    ui_root = UI_DIR
    target = ui_root / safe
    if not target.is_file():
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    ctype, _ = mimetypes.guess_type(str(target))
    if target.suffix == ".js":
      ctype = "application/javascript; charset=utf-8"
    elif target.suffix == ".css":
      ctype = "text/css; charset=utf-8"
    elif target.suffix == ".html":
      ctype = "text/html; charset=utf-8"
    data = target.read_bytes()
    try:
      self.send_response(HTTPStatus.OK)
      self.send_header("Content-Type", ctype or "application/octet-stream")
      self.send_header("Content-Length", str(len(data)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
      return

  def _stream_file(
      self,
      target: Path,
      content_type: str,
      cache_control: str = "no-store",
      attachment_name: str | None = None,
  ) -> None:
    """Streams a file with single-range HTTP Range support.

    Range streaming lets the <audio>/<video> elements seek in large media
    without downloading it all (WebKit requires 206 support to scrub
    video). Supported forms: "bytes=start-", "bytes=start-end" (end clamped
    to EOF) and the suffix form "bytes=-N" (last N bytes). Only the first
    range of a multi-range header is honored. Per RFC 7233, a syntactically
    valid but unsatisfiable range (start at/after EOF, or "bytes=-0") gets
    416 with `Content-Range: bytes */<total>`; unparseable ranges (including
    end < start) are ignored and the full file is sent with 200.
    The body is sent in 128 KiB chunks so memory use stays flat.

    Args:
      target: File to send.
      content_type: Value of the Content-Type header.
      cache_control: Value of the Cache-Control header.
      attachment_name: If set, adds `Content-Disposition: attachment` with
        this filename (double quotes stripped to keep the header valid).
    """
    total = target.stat().st_size
    range_hdr = self.headers.get("Range")
    start = 0
    end = max(0, total - 1)
    status = HTTPStatus.OK
    if total > 0 and range_hdr and range_hdr.startswith("bytes="):
      spec = range_hdr.replace("bytes=", "", 1).split(",")[0].strip()
      m_range = re.fullmatch(r"(\d*)-(\d*)", spec)
      if m_range and (m_range.group(1) or m_range.group(2)):
        first, last = m_range.groups()
        if first:
          req_start = int(first)
          req_end = int(last) if last else None
          if req_end is not None and req_end < req_start:
            pass  # Invalid syntax (RFC 7233 2.1): ignore, send 200.
          elif req_start >= total:
            status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
          else:
            start = req_start
            end = total - 1 if req_end is None else min(total - 1, req_end)
            status = HTTPStatus.PARTIAL_CONTENT
        elif int(last) == 0:
          status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
        else:
          start, end = max(0, total - int(last)), total - 1
          status = HTTPStatus.PARTIAL_CONTENT

    if status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
      try:
        self.send_response(status)
        self.send_header("Content-Range", f"bytes */{total}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
      except (BrokenPipeError, ConnectionResetError):
        pass
      return

    length = 0 if total == 0 else (end - start + 1)
    try:
      self.send_response(status)
      self.send_header("Content-Type", content_type)
      self.send_header("Accept-Ranges", "bytes")
      if attachment_name:
        safe_disp = attachment_name.replace('"', "")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{safe_disp}"'
        )
      self.send_header("Content-Length", str(length))
      if status == HTTPStatus.PARTIAL_CONTENT:
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
      self.send_header("Cache-Control", cache_control)
      self.end_headers()

      if length > 0:
        with open(target, "rb") as fp:
          fp.seek(start)
          remaining = length
          while remaining > 0:
            chunk = fp.read(min(131072, remaining))
            if not chunk:
              break
            self.wfile.write(chunk)
            remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
      return

  def _serve_media(self) -> None:
    """Streams the active media for the UI preview player (/api/media).

    Prefers the original upload; if its container is not playable in the
    browser (e.g. .mkv, .flac) and a 16 kHz preview WAV exists, the WAV is
    served instead. Responds 404 when no media is loaded.
    """
    with self.state.lock:
      media = self.state.media_path or self.state.wav_path
      if (
          media
          and media.suffix.lower()
          not in (".wav", ".mp3", ".m4a", ".mp4", ".m4v", ".mov", ".webm")
          and self.state.wav_path
      ):
        media = self.state.wav_path
    if not media or not media.is_file():
      self.send_error(HTTPStatus.NOT_FOUND, "Sin audio cargado")
      return

    ctype, _ = mimetypes.guess_type(str(media))
    if not ctype:
      ctype = (
          "audio/wav"
          if media.suffix.lower() == ".wav"
          else "application/octet-stream"
      )
    self._stream_file(media, content_type=ctype, cache_control="no-store")

  # --- GET routes ---

  def do_GET(self) -> None:
    """Dispatches GET requests: static UI files, media streams, JSON reads.

    GET routes are read-only (no state mutation), so only the Host check of
    the origin guard applies. Unknown paths return a 404 JSON error.
    """
    if not self._check_request_origin(mutating=False):
      return
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path

    # GET / | /index.html | /styles.css | /app.js -> static Studio UI files.
    # Fixed allowlist: no other file under ui/ is reachable.
    if path in ("/", "/index.html", "/styles.css", "/app.js"):
      self._serve_static(path)
      return

    # GET /api/media -> Range-capable stream of the active media (or 404).
    if path == "/api/media":
      self._serve_media()
      return

    # GET /api/exported-video -> last burned-in MP4 as a download
    # (Content-Disposition: attachment), Range-capable; 404 if none.
    if path == "/api/exported-video":
      with self.state.lock:
        out_vid = self.state.exported_video_path
      if not out_vid or not out_vid.is_file():
        self.send_error(HTTPStatus.NOT_FOUND, "No hay vídeo exportado.")
        return
      self._stream_file(
          out_vid,
          content_type="video/mp4",
          cache_control="no-store",
          attachment_name=out_vid.name,
      )
      return

    # GET /api/status -> {environment, bridge, presets, dialects, domains,
    # fonts, session, machine}: everything the UI needs at startup and on
    # its periodic refresh. Probes tools/processes, so it is not free.
    if path == "/api/status":
      env = hardware_imac2017.detect_environment()
      resolve_running = bool(env.get("resolve_running"))
      bridge_status = self.state.bridge.get_status(
          resolve_running=resolve_running
      )
      self._send_json(
          HTTPStatus.OK,
          {
              "environment": env,
              "bridge": bridge_status,
              "presets": spanish_nlp.FORMAT_PRESETS,
              "dialects": list(spanish_nlp.SPANISH_DIALECT_PROMPTS.keys()),
              "domains": {
                  k: {
                      "label": v["label"],
                      "glossary_sample": list(v["prompt_terms"])[:18],
                  }
                  for k, v in spanish_nlp.DOMAIN_PROFILES.items()
              },
              "fonts": self.state.fonts.list_fonts(),
              "session": self.state.session_snapshot(),
              "machine": self.state.settings_payload(
                  resolve_running=resolve_running
              ),
          },
      )
      return

    # GET /api/settings -> settings_payload() (saved + effective + profile).
    if path == "/api/settings":
      self._send_json(HTTPStatus.OK, self.state.settings_payload())
      return

    # GET /api/settings/export -> portable preferences JSON for backup.
    if path == "/api/settings/export":
      self._send_json(HTTPStatus.OK, self.state.settings.export_preferences())
      return

    # GET /api/fonts -> list of available subtitle fonts.
    if path == "/api/fonts":
      self._send_json(HTTPStatus.OK, self.state.fonts.list_fonts())
      return

    # GET /api/fonts/file/<font_id> -> font binary for CSS @font-face
    # previews (cacheable for 1 h). FontManager resolves the id, so the
    # URL cannot point at arbitrary files.
    if path.startswith("/api/fonts/file/"):
      font_id = urllib.parse.unquote(
          path.split("/api/fonts/file/", 1)[1].strip("/")
      )
      resolved = self.state.fonts.get_font_file(font_id)
      if resolved is None:
        self.send_error(HTTPStatus.NOT_FOUND, "Fuente no encontrada")
        return
      font_path, mime = resolved
      self._stream_file(
          font_path, content_type=mime, cache_control="public, max-age=3600"
      )
      return

    # GET /api/jobs/<job_id> -> copy of the job dict (polled by the UI for
    # progress/stage/result); 404 JSON if unknown or pruned.
    if path.startswith("/api/jobs/"):
      job_id = path.split("/api/jobs/", 1)[1].strip("/")
      with self.state.lock:
        raw_job = self.state.jobs.get(job_id)
        job = dict(raw_job) if raw_job else None
      if not job:
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "Trabajo no encontrado"}
        )
        return
      self._send_json(HTTPStatus.OK, job)
      return

    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

  # --- POST routes ---

  def do_POST(self) -> None:
    """Dispatches POST requests (uploads, job starts, session mutations).

    The two upload routes read a raw binary body and are handled first; all
    other routes expect a JSON object body (400 on invalid JSON). Job routes
    return 202 `{"ok": true, "job_id": ...}` and do the work in background
    threads. Unknown paths return a 404 JSON error.
    """
    if not self._check_request_origin(mutating=True):
      return
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path

    # POST /api/upload
    #   Body: raw media bytes; headers X-Filename (URL-encoded, decoded
    #   exactly once) and Content-Length (required, <= _MAX_UPLOAD_BYTES).
    #   Response: {ok, media_path, filename, size_bytes, duration_ms,
    #   session}; 400 on bad/empty/incomplete upload, 413 if too large.
    #   Side effects: writes <cache_dir>/uploads/<sanitized name> (atomic
    #   replace of a same-named upload; internal cache files can never be
    #   hit), replaces the active media, deletes the previous upload and
    #   its working WAV, clears words/cues and the exported video, bumps
    #   media_generation (invalidating running transcriptions). Peaks are
    #   computed only for .wav uploads (empty if the stdlib cannot decode
    #   the WAV, e.g. 32-bit float); other formats get them after
    #   transcription.
    if path == "/api/upload":
      raw_name = urllib.parse.unquote(
          self.headers.get("X-Filename", "media.wav")
      )
      safe_name = _safe_filename(raw_name)
      try:
        length = int(self.headers.get("Content-Length") or 0)
      except ValueError:
        self.close_connection = True
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Content-Length inválido"},
        )
        return
      if length <= 0:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Archivo vacío o sin Content-Length"},
        )
        return
      if length > _MAX_UPLOAD_BYTES:
        # Body is left unread, so drop keep-alive to avoid parsing it as
        # the next request.
        self.close_connection = True
        self._send_json(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            {"error": "El archivo supera el límite máximo de 8 GB."},
        )
        return
      uploads_dir = self.state.uploads_dir
      uploads_dir.mkdir(parents=True, exist_ok=True)
      dest = uploads_dir / safe_name
      # Write to a unique temp file and rename into place, so an aborted
      # upload never destroys a same-named file that a running job may be
      # reading, and the active media is only swapped when complete.
      tmp_dest = uploads_dir / f".upload_{uuid.uuid4().hex[:10]}.part"
      remaining = length
      try:
        # Stream to disk in 1 MiB chunks so large videos never sit in RAM.
        with open(tmp_dest, "wb") as fp:
          while remaining > 0:
            chunk = self.rfile.read(min(1048576, remaining))
            if not chunk:
              break
            fp.write(chunk)
            remaining -= len(chunk)
        if remaining == 0:
          os.replace(tmp_dest, dest)
      finally:
        tmp_dest.unlink(missing_ok=True)
      if remaining != 0:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Subida incompleta antes de terminar la transferencia"},
        )
        return

      wave_info = {"peaks": [], "duration_ms": 0}
      if dest.suffix.lower() == ".wav":
        wave_info = _safe_waveform_peaks(dest)

      with self.state.lock:
        old_media = self.state.media_path
        old_wav = self.state.wav_path
        self.state.media_path = dest
        self.state.wav_path = dest if dest.suffix.lower() == ".wav" else None
        self.state.exported_video_path = None
        self.state.media_name = safe_name
        self.state.duration_ms = int(wave_info["duration_ms"])
        self.state.peaks = list(wave_info["peaks"])
        self.state.words = []
        self.state.cues = []
        self.state._last_inbox_sig = None
        self.state.media_generation += 1
        self.state.demo_mode = False
      self.state._discard_replaced_media(old_media, old_wav, keep=(dest,))

      self._send_json(
          HTTPStatus.OK,
          {
              "ok": True,
              "media_path": str(dest),
              "filename": safe_name,
              "size_bytes": dest.stat().st_size,
              "duration_ms": wave_info["duration_ms"],
              "session": self.state.session_snapshot(),
          },
      )
      return

    # POST /api/fonts/upload
    #   Body: raw font bytes (<= font_manager.MAX_UPLOAD_BYTES, 64 MB);
    #   headers X-Filename, X-Install-Macos ("0"/"false"/"no" to skip
    #   installing into ~/Library/Fonts).
    #   Response: FontManager.install_upload() result; 400 on empty, too
    #   large, or invalid font.
    #   Side effects: copies the font into the app fonts dir (used by the
    #   burn-in) and optionally into the user's macOS fonts.
    if path == "/api/fonts/upload":
      raw_name = self.headers.get("X-Filename", "custom_font.otf")
      decoded_name = urllib.parse.unquote(raw_name)
      install_hdr = (self.headers.get("X-Install-Macos", "1") or "1").strip()
      install_to_macos = install_hdr.lower() not in ("0", "false", "no")
      try:
        length = int(self.headers.get("Content-Length") or 0)
      except ValueError:
        self.close_connection = True
        self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": "Content-Length inválido"}
        )
        return
      if length <= 0:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Archivo de fuente vacío o sin Content-Length"},
        )
        return
      if length > font_manager.MAX_UPLOAD_BYTES:
        # Body is left unread, so drop keep-alive to avoid parsing it as
        # the next request.
        self.close_connection = True
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "La fuente supera el límite máximo de 64 MB."},
        )
        return
      raw_bytes = self.rfile.read(length)
      try:
        res = self.state.fonts.install_upload(
            decoded_name,
            raw_bytes,
            install_to_macos=install_to_macos,
        )
        self._send_json(HTTPStatus.OK, res)
      except ValueError as exc:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # All routes below take a JSON object body.
    try:
      body = self._read_json_body()
    except ValueError as exc:
      self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # POST /api/models/download
    #   Body: {model_id?} (defaults to the best model for this Mac).
    #   Response: 202 {ok, job_id}; 400 on unknown model.
    #   Side effects: starts/joins a resumable download job into models_dir.
    if path == "/api/models/download":
      try:
        default_id = hardware_imac2017.choose_default_model_id(
            self.state.models_dir
        )
        job_id = self.state.start_model_download(
            str(body.get("model_id", default_id))
        )
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # POST /api/settings/reset
    #   Body: ignored. Response: settings_payload().
    #   Side effects: resets saved performance settings to auto defaults.
    if path == "/api/settings/reset":
      self.state.settings.reset_performance()
      self._send_json(HTTPStatus.OK, self.state.settings_payload())
      return

    # POST /api/settings/import
    #   Body: JSON produced by GET /api/settings/export.
    #   Response: settings_payload(); 400 if the file is invalid.
    #   Side effects: overwrites saved preferences on disk.
    if path == "/api/settings/import":
      try:
        self.state.settings.import_preferences(body)
      except ValueError as exc:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return
      self._send_json(HTTPStatus.OK, self.state.settings_payload())
      return

    # POST /api/benchmark
    #   Body: {model_id?, threads?, force_no_gpu?}.
    #   Response: 202 {ok, job_id}; 400 if model/whisper-cli is missing.
    #   Side effects: benchmark job; stores the result in settings.
    if path == "/api/benchmark":
      try:
        job_id = self.state.start_benchmark(body)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # POST /api/transcribe
    #   Body: see AppState.start_transcription (source, model_id,
    #   format_config, threads, ...).
    #   Response: 202 {ok, job_id}; 400 on validation errors.
    #   Side effects: transcription job; replaces session words/cues.
    if path == "/api/transcribe":
      try:
        job_id = self.state.start_transcription(body)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # POST /api/export-video
    #   Body: {style?, width?, height?, threads?}.
    #   Response: 202 {ok, job_id}; 400 if no video/cues/ffmpeg+libass.
    #   Side effects: burn-in job; sets exported_video_path on success.
    if path == "/api/export-video":
      try:
        job_id = self.state.start_video_burnin(body)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # POST /api/jobs/<job_id>/cancel
    #   Body: ignored. Response: {ok, status} (ok false / status null if the
    #   job id is unknown; status is the job's state after the call).
    #   Side effects: marks a queued/running job cancelled and kills its
    #   child process; finished jobs are left unchanged.
    if path.startswith("/api/jobs/") and path.endswith("/cancel"):
      # "/api/jobs/<id>/cancel".split("/") -> ["", "api", "jobs", id, ...]
      parts = path.split("/")
      job_id = parts[3] if len(parts) >= 5 else ""
      job_status = self.state.cancel_job(job_id)
      self._send_json(
          HTTPStatus.OK, {"ok": job_status is not None, "status": job_status}
      )
      return

    # POST /api/demo
    #   Body: {format_config?}. Response: {ok, session}; 400 on a malformed
    #   format_config.
    #   Side effects: replaces the active media with the demo session.
    if path == "/api/demo":
      try:
        snap = self.state.load_demo(body.get("format_config"))
      except (ValueError, TypeError, AttributeError) as exc:
        self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": f"format_config inválido: {exc}"}
        )
        return
      self._send_json(HTTPStatus.OK, {"ok": True, "session": snap})
      return

    # POST /api/resegment
    #   Body: {format_config}. Response: {ok, session}; 400 if there are no
    #   transcribed words or format_config is malformed.
    #   Side effects: rebuilds cues from `words` with the new config,
    #   discarding manual cue edits.
    if path == "/api/resegment":
      try:
        cfg = spanish_nlp.SpanishFormatConfig.from_dict(
            body.get("format_config")
        )
      except (ValueError, TypeError, AttributeError) as exc:
        self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": f"format_config inválido: {exc}"}
        )
        return
      with self.state.lock:
        if not self.state.words:
          self._send_json(
              HTTPStatus.BAD_REQUEST,
              {"error": "No hay transcripción activa para reformatear."},
          )
          return
        self.state.format_config = cfg
        self.state.cues = spanish_nlp.segment_and_wrap_spanish(
            self.state.words, cfg
        )
      self._send_json(
          HTTPStatus.OK, {"ok": True, "session": self.state.session_snapshot()}
      )
      return

    # POST /api/export
    #   Body: {format ("srt", "vtt", "ass", "fcpxml", ...), style?, fps?,
    #   timeline_start_frame?}. Missing fps/start frame/size come from the
    #   last Resolve timeline metadata (defaults 24 fps, frame 86400 =
    #   01:00:00:00 at 24 fps, 1920x1080).
    #   Response: {ok, filename, mime_type, content} (file text; the UI
    #   saves it); 400 on unknown format or non-numeric/invalid fps, start
    #   frame or style values. No server-side state change.
    if path == "/api/export":
      fmt = str(body.get("format", "srt")).lower()
      meta = self.state.bridge.read_timeline_meta()
      try:
        style = exporters.StyleConfig.from_dict(body.get("style"))
        fps = float(body.get("fps") or meta.get("fps") or 24.0)
        # JSON allows NaN/Infinity and negatives; none is a usable rate.
        if not math.isfinite(fps) or fps <= 0:
          raise ValueError(fps)
        raw_start_frame = body.get("timeline_start_frame")
        if raw_start_frame is None:
          raw_start_frame = meta.get("start_frame")
        start_frame = int(
            raw_start_frame if raw_start_frame is not None else 86400
        )
        width = int(meta.get("width") or 1920)
        height = int(meta.get("height") or 1080)
      except (ValueError, TypeError, OverflowError, AttributeError):
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {
                "error": (
                    "Parámetros de exportación inválidos (fps,"
                    " timeline_start_frame o estilo)."
                )
            },
        )
        return
      tl_name = str(meta.get("timeline_name") or "AutoSubs ES")

      with self.state.lock:
        cues = list(self.state.cues)
        base_stem = _safe_filename(
            Path(self.state.media_name or "subtitulos_es").stem
        )

      try:
        content, fname, mime = exporters.render_export(
            fmt=fmt,
            cues=cues,
            base_stem=base_stem,
            style=style,
            fps=fps,
            start_frame=start_frame,
            width=width,
            height=height,
            timeline_name=tl_name,
        )
      except ValueError as exc:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        return

      self._send_json(
          HTTPStatus.OK,
          {
              "ok": True,
              "filename": fname,
              "mime_type": mime,
              "content": content,
          },
      )
      return

    # POST /api/resolve/export-audio
    #   Body: ignored. Response: bridge result dict ({ok: false,
    #   requires_lua: true, message} when the external scripting API is
    #   unavailable, e.g. Resolve Free).
    #   Side effects: starts a non-blocking Deliver WAV render of the
    #   current timeline via the Resolve Studio scripting API.
    if path == "/api/resolve/export-audio":
      res = self.state.bridge.export_timeline_audio_studio()
      self._send_json(HTTPStatus.OK, res)
      return

    # POST /api/resolve/notify-export
    #   Called (fire-and-forget curl) by the Resolve Lua export script and by
    #   the UI after a new inbox WAV is written. Body: ignored. Response:
    #   {ok, bridge, session}.
    #   Side effects: if the inbox WAV's (path, mtime, size) signature
    #   differs from `_last_inbox_sig`, loads it as the active media, clears
    #   words/cues/exported video, deletes a replaced upload/working WAV and
    #   bumps media_generation. Unchanged signatures are a no-op so repeated
    #   calls keep the user's edits. A WAV whose peaks cannot be decoded
    #   (e.g. 32-bit float) still activates, with an empty waveform.
    if path == "/api/resolve/notify-export":
      inbox_wav = self.state.bridge.get_inbox_audio_path()
      if inbox_wav and inbox_wav.is_file():
        st = inbox_wav.stat()
        sig = (str(inbox_wav), st.st_mtime_ns, st.st_size)
        with self.state.lock:
          changed = sig != self.state._last_inbox_sig
        if changed:
          # Peaks are computed outside the lock (file I/O).
          wave_info = _safe_waveform_peaks(inbox_wav)
          with self.state.lock:
            old_media = self.state.media_path
            old_wav = self.state.wav_path
            self.state._last_inbox_sig = sig
            self.state.media_path = inbox_wav
            self.state.wav_path = inbox_wav
            self.state.media_name = inbox_wav.name
            self.state.duration_ms = int(wave_info["duration_ms"])
            self.state.peaks = list(wave_info["peaks"])
            self.state.demo_mode = False
            self.state.exported_video_path = None
            self.state.words = []
            self.state.cues = []
            self.state.media_generation += 1
          self.state._discard_replaced_media(
              old_media, old_wav, keep=(inbox_wav,)
          )
      self._send_json(
          HTTPStatus.OK,
          {
              "ok": True,
              "bridge": self.state.bridge.get_status(),
              "session": self.state.session_snapshot(),
          },
      )
      return

    # POST /api/resolve/push
    #   Body: push options forwarded to ResolveBridge (mode, track_name,
    #   template_name, fps, ...).
    #   Response: bridge result; 400 if there are no cues.
    #   Side effects: writes bridge/outbox payloads for the Lua import
    #   script and pushes directly if Resolve Studio is reachable.
    if path == "/api/resolve/push":
      with self.state.lock:
        cues = list(self.state.cues)
      if not cues:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "No hay subtítulos generados para enviar a Resolve."},
        )
        return
      res = self.state.bridge.stage_or_push_to_resolve(cues, body)
      self._send_json(HTTPStatus.OK, res)
      return

    # POST /api/resolve/jump
    #   Body: {ms} (media time in milliseconds). Response: bridge result;
    #   400 if ms is not a number.
    #   Side effects: moves the Resolve playhead (Studio API) or writes
    #   outbox/jump_tc.lua with the target timecode for the Lua script.
    if path == "/api/resolve/jump":
      try:
        ms = int(body.get("ms", 0))
      except (ValueError, TypeError, OverflowError):
        self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": "'ms' debe ser un número"}
        )
        return
      res = self.state.bridge.jump_playhead(ms)
      self._send_json(HTTPStatus.OK, res)
      return

    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

  # --- PUT routes ---

  def do_PUT(self) -> None:
    """Dispatches PUT requests that replace a whole resource (cues/settings).

    Unknown paths get 404 before the body is read; invalid JSON gets 400.
    """
    if not self._check_request_origin(mutating=True):
      return
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path not in ("/api/cues", "/api/settings"):
      self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})
      return
    try:
      body = self._read_json_body()
    except ValueError as exc:
      self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    # PUT /api/settings
    #   Body: partial settings object. Response: settings_payload().
    #   Side effects: merges and persists this Mac's saved settings.
    if parsed.path == "/api/settings":
      if not isinstance(body, dict):
        self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": "Se esperaba un objeto JSON"}
        )
        return
      self.state.settings.update(body)
      self._send_json(HTTPStatus.OK, self.state.settings_payload())
      return

    # PUT /api/cues
    #   Body: {cues: [SubtitleCue dict, ...], media_rev?} — the full edited
    #   cue list from the cue editor (autosave). `media_rev` (optional, the
    #   session value the edits were made against) enables an optimistic
    #   concurrency check: if the active media changed meanwhile (e.g. the
    #   Resolve Lua script's notify-export loaded a new inbox WAV), the
    #   edits belong to the old audio and must not be applied.
    #   Response: {ok, session} without peaks; 400 if `cues` is not a list
    #   of valid cue objects; 409 {error, session} on a media_rev mismatch
    #   (cues unchanged). Without `media_rev` no check is made.
    #   Side effects: replaces `state.cues` (words are untouched, so
    #   /api/resegment can still rebuild from the original transcription).
    raw_cues = body.get("cues", [])
    if not isinstance(raw_cues, list):
      self._send_json(
          HTTPStatus.BAD_REQUEST, {"error": "'cues' debe ser lista"}
      )
      return

    check_rev = "media_rev" in body
    with self.state.lock:
      cfg = self.state.format_config
      active_file = self.state.media_path or self.state.wav_path
      generation = self.state.media_generation
    # Stat outside the lock; the generation/path recheck below catches a
    # media swap that happens in between.
    if check_rev and body.get("media_rev") != _file_rev(active_file):
      self._send_media_conflict()
      return

    try:
      updated: list[spanish_nlp.SubtitleCue] = []
      for idx, item in enumerate(raw_cues, start=1):
        if not isinstance(item, dict):
          raise TypeError("cada subtítulo debe ser un objeto")
        cue = spanish_nlp.SubtitleCue.from_dict(item)
        # Renumber 1..N and push overlapping cues forward so cues never
        # overlap; keep at least 80 ms on screen and re-spread word timings.
        cue.index = idx
        if updated and cue.start_ms < updated[-1].end_ms:
          cue.start_ms = updated[-1].end_ms
          cue.end_ms = max(cue.start_ms + 80, cue.end_ms)
          cue.words = spanish_nlp.align_words_to_lines(
              cue.words, cue.lines, cue.start_ms, cue.end_ms
          )
        spanish_nlp.refresh_cue_metrics(cue, cfg)
        updated.append(cue)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
      # AttributeError: e.g. a `words` item that is not an object.
      self._send_json(
          HTTPStatus.BAD_REQUEST, {"error": f"Subtítulos inválidos: {exc}"}
      )
      return

    with self.state.lock:
      conflict = check_rev and (
          self.state.media_generation != generation
          or (self.state.media_path or self.state.wav_path) != active_file
      )
      if not conflict:
        self.state.cues = updated
    if conflict:
      self._send_media_conflict()
      return

    self._send_json(
        HTTPStatus.OK,
        {
            "ok": True,
            "session": self.state.session_snapshot(include_peaks=False),
        },
    )

  def _send_media_conflict(self) -> None:
    """Sends the 409 reply of PUT /api/cues for a stale `media_rev`.

    The body carries the current session (without peaks) so the UI can
    reload the cues of the new media instead of retrying blindly.
    """
    self._send_json(
        HTTPStatus.CONFLICT,
        {
            "error": (
                "El audio activo cambió (por ejemplo, desde Resolve); los"
                " cambios en los subtítulos no se aplicaron."
            ),
            "session": self.state.session_snapshot(include_peaks=False),
        },
    )


# --- Server factory and desktop entry points ---


def create_server(
    host: str = "127.0.0.1",
    port: int = 55120,
    app_dir: Path | None = None,
) -> tuple[ThreadingHTTPServer, AppState]:
  """Creates the AutoSubs ES ThreadingHTTPServer bound to host:port.

  The socket is bound immediately but not served; call `serve_forever()`.
  Handler threads are daemons so a hung request never blocks exit.

  Args:
    host: Interface to bind; keep it on loopback (the origin guard rejects
      non-loopback Host headers anyway).
    port: TCP port; 0 picks a free port (used by tests).
    app_dir: Optional data root override for `AppState`.

  Returns:
    (server, state) so callers/tests can inspect the shared state.

  Raises:
    OSError: The port is already in use.
  """
  state = AppState(app_dir=app_dir)
  handler_cls = partial(AutoSubsHandler, state=state)
  server = ThreadingHTTPServer((host, port), handler_cls)
  server.daemon_threads = True
  return server, state


def open_standalone_window(url: str) -> None:
  """Opens AutoSubs ES in a chromeless macOS app window when possible.

  On macOS, launches the first installed Chromium browser (Chrome, Brave,
  Edge) as a new instance with `--app=<url>` (no tabs/address bar).
  Otherwise, or if launching fails, opens the default browser.

  Args:
    url: Server base URL, e.g. "http://127.0.0.1:55120".
  """
  if sys.platform == "darwin":
    chromium_apps = (
        "/Applications/Google Chrome.app",
        "/Applications/Brave Browser.app",
        "/Applications/Microsoft Edge.app",
    )
    for app_path in chromium_apps:
      if Path(app_path).exists():
        try:
          subprocess.Popen(["open", "-na", app_path, "--args", f"--app={url}"])
          return
        except OSError:
          pass
  webbrowser.open(url)


def _server_already_running(url: str) -> bool:
  """Returns True if an AutoSubs server already answers /api/status at url.

  Used by the desktop app to reuse an existing instance instead of failing
  to bind the port. 1.5 s timeout keeps startup snappy.
  """
  try:
    with urllib.request.urlopen(f"{url}/api/status", timeout=1.5) as resp:
      return resp.status == 200
  except (OSError, ValueError):
    return False


def _brand_macos_app(icon_path: Path) -> None:
  """Shows the app name and icon in the Dock/menu bar instead of "Python".

  Needs PyObjC (AppKit/Foundation, bundled with pywebview on macOS); a
  silent no-op when unavailable. Must run before the Cocoa app starts.

  Args:
    icon_path: PNG used as the Dock icon (skipped if missing).
  """
  try:
    import AppKit  # pylint: disable=import-outside-toplevel
    import Foundation  # pylint: disable=import-outside-toplevel
  except ImportError:
    return
  bundle = Foundation.NSBundle.mainBundle()
  info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
  if info is not None:
    info["CFBundleName"] = "MTTN AutoSubs Tool"
  if icon_path.is_file():
    image = AppKit.NSImage.alloc().initByReferencingFile_(str(icon_path))
    AppKit.NSApplication.sharedApplication().setApplicationIconImage_(image)


def run_desktop_app(host: str = "127.0.0.1", port: int = 55120) -> int:
  """Runs the studio inside a native macOS window (WKWebView via pywebview).

  The local server lives only as long as the window: closing it stops the
  server. Exports open a native "Save as…" panel. Falls back to the Chrome
  app-window mode when pywebview is not installed.

  The HTTP server runs on a daemon thread; `webview.start()` must own the
  main thread (Cocoa requirement) and blocks until the window closes.

  Args:
    host: Interface to bind / connect to.
    port: TCP port.

  Returns:
    Process exit code (0 on normal window close).
  """
  url = f"http://{host}:{port}"
  try:
    import webview  # pylint: disable=import-outside-toplevel
  except ImportError:
    print(
        "[MTTN AutoSubs Tool] pywebview no está instalado; abriendo en el"
        " navegador. Ejecuta ./install_imac.sh para la ventana nativa.",
        file=sys.stderr,
    )
    return main(["--host", host, "--port", str(port), "--app-window"])

  server = None
  owns_server = False
  if _server_already_running(url):
    # Another instance owns the port (e.g. a window already open): reuse it.
    print(f"[MTTN AutoSubs Tool] Usando el servidor ya activo en {url}")
  else:
    server, _ = create_server(host=host, port=port)
    owns_server = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[MTTN AutoSubs Tool] Servidor activo en {url} (ventana nativa)")

  app_dir = hardware_imac2017.get_app_data_dir(create=True)
  icon = Path(__file__).resolve().parent / "assets" / "icon.png"
  _brand_macos_app(icon)
  webview.settings["ALLOW_DOWNLOADS"] = True
  webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
  webview.create_window(
      "MTTN AutoSubs Tool",
      url,
      width=1540,
      height=980,
      min_size=(1100, 720),
      background_color="#18181B",
      text_select=True,
      zoomable=True,
  )
  try:
    # private_mode=False keeps localStorage (theme, karaoke, saved styles).
    webview.start(
        private_mode=False,
        storage_path=str(app_dir / "webview"),
        icon=str(icon) if icon.is_file() else None,
    )
  finally:
    if owns_server and server is not None:
      server.shutdown()
      server.server_close()
  return 0


# --- CLI entry points ---


def run_cli_transcription(
    media_file: str,
    output_path: str | None = None,
    fmt: str = "srt",
    model_id: str | None = None,
    domain: str = "juegos_mesa",
    dialect: str = "neutro",
    preset_id: str = "youtube_16_9",
    glossary: str = "",
    threads: int | None = None,
    fast_beam: bool = False,
    enhance_voice: bool = True,
    force_no_gpu: bool | None = None,
    remove_muletillas: bool = False,
    karaoke: bool = False,
    app_dir: Path | None = None,
) -> int:
  """Runs standalone offline Spanish transcription directly from CLI.

  Same pipeline as `AppState.start_transcription` but synchronous and
  without the HTTP server: ffmpeg -> 16 kHz WAV, whisper-cli (output shown
  in the terminal), Spanish NLP segmentation, then `exporters.render_export`
  to a file. Temp WAV/JSON files in the cache dir are always removed.

  Args:
    media_file: Input audio/video path.
    output_path: Output file; defaults to the input path with the export
      format's extension.
    fmt: Export format id (see `--format` choices in `main`).
    model_id: Model catalog id; defaults to the best installed model.
    domain: Vocabulary/glossary domain key in `spanish_nlp.DOMAIN_PROFILES`.
    dialect: Key in `spanish_nlp.SPANISH_DIALECT_PROMPTS`.
    preset_id: Line-length/layout preset key in `FORMAT_PRESETS`.
    glossary: Comma-separated user terms (exact casing enforced; also fed
      to the whisper prompt).
    threads: whisper threads; None uses this Mac's effective settings.
    fast_beam: Smaller beam search (2 instead of 5): faster, slightly less
      accurate.
    enhance_voice: Apply ffmpeg voice filters before transcription.
    force_no_gpu: Disable Metal; None uses the effective settings.
    remove_muletillas: Drop hesitation fillers ("eh", "em", "mm", ...).
    karaoke: Add per-word \\kf karaoke tags to .ass output.
    app_dir: Data root override (models/ and cache/).

  Returns:
    Process exit code: 0 on success, 1 on any error (message on stderr).
  """
  src = Path(media_file).expanduser()
  if not src.is_file():
    print(f"[AutoSubs ES] Error: no existe el archivo {src}", file=sys.stderr)
    return 1

  app_base = app_dir or hardware_imac2017.get_app_data_dir(create=True)
  models_dir = app_base / "models"
  cache_dir = app_base / "cache"
  cache_dir.mkdir(parents=True, exist_ok=True)

  # Same per-machine performance settings as the desktop app.
  effective = machine_settings.resolve_performance(
      hardware_imac2017.detect_hardware_profile(),
      machine_settings.MachineSettings(app_base).snapshot()["performance"],
      hardware_imac2017.is_resolve_running(),
  )
  if threads is None:
    threads = effective["threads"]
  if force_no_gpu is None:
    force_no_gpu = effective["force_no_gpu"]
  chosen_model = model_id or hardware_imac2017.choose_default_model_id(
      models_dir
  )
  spec = hardware_imac2017.MODEL_CATALOG.get(chosen_model)
  if not spec:
    print(
        f"[AutoSubs ES] Error: modelo desconocido {chosen_model}",
        file=sys.stderr,
    )
    return 1
  model_path = models_dir / spec.filename
  if not model_path.is_file():
    print(
        f"[AutoSubs ES] Error: falta el modelo {model_path}. Descárgalo con"
        f" ./install_imac.sh --model={chosen_model}",
        file=sys.stderr,
    )
    return 1

  env = hardware_imac2017.detect_environment()
  whisper_bin = env.get("whisper_cli_path")
  if not whisper_bin:
    print(
        "[AutoSubs ES] Error: whisper-cli no encontrado (brew install"
        " whisper-cpp)",
        file=sys.stderr,
    )
    return 1

  job_tag = uuid.uuid4().hex[:8]
  working_wav = cache_dir / f"cli_{job_tag}_16k.wav"
  out_prefix = cache_dir / f"cli_{job_tag}"
  ojf_json = Path(f"{out_prefix}.json")

  try:
    resolve_bridge.prepare_wav_16k(
        str(src),
        str(working_wav),
        ffmpeg_bin=env.get("ffmpeg_path"),
        enhance_voice=enhance_voice,
    )

    cfg = spanish_nlp.SpanishFormatConfig.from_dict({
        "preset_id": preset_id,
        "dialect": dialect,
        "domain": domain,
        "remove_muletillas": remove_muletillas,
        "custom_glossary": glossary,
    })
    prompt = spanish_nlp.build_spanish_initial_prompt(
        cfg.dialect, cfg.custom_glossary, domain=cfg.domain
    )
    cmd = hardware_imac2017.build_whisper_command(
        whisper_bin=whisper_bin,
        model_path=str(model_path),
        wav_path=str(working_wav),
        output_prefix=str(out_prefix),
        prompt=prompt,
        threads=threads,
        force_no_gpu=force_no_gpu,
        fast_beam=fast_beam,
    )
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0 or not ojf_json.is_file():
      print(
          "[AutoSubs ES] Error: whisper-cli falló con código"
          f" {proc.returncode}",
          file=sys.stderr,
      )
      return 1

    ojf_data = json.loads(ojf_json.read_text(encoding="utf-8"))
    words = spanish_nlp.parse_whisper_ojf(ojf_data)
    cues = spanish_nlp.segment_and_wrap_spanish(words, cfg)
    style = exporters.StyleConfig(karaoke_enabled=karaoke)

    try:
      content, fname, _ = exporters.render_export(
          fmt=fmt,
          cues=cues,
          base_stem=src.stem,
          style=style,
          timeline_name=src.stem,
      )
    except ValueError as exc:
      print(f"[AutoSubs ES] {exc}", file=sys.stderr)
      return 1

    ext = Path(fname).suffix
    dest = (
        Path(output_path).expanduser() if output_path else src.with_suffix(ext)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(
        f"[AutoSubs ES] ✓ {len(cues)} subtítulos exportados a {dest}"
        f" ({spec.model_id}, dominio={cfg.domain})"
    )
    return 0
  finally:
    working_wav.unlink(missing_ok=True)
    ojf_json.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
  """Parses CLI flags and runs the selected mode.

  Modes, in priority order: `--cli FILE` (headless transcription),
  `--desktop` (native pywebview window), otherwise the HTTP server in the
  foreground, opening a browser tab (or a Chromium app window with
  `--app-window`) ~0.6 s after start unless `--no-browser`. Ctrl+C stops it.

  Args:
    argv: Argument list without the program name; None reads sys.argv.

  Returns:
    Process exit code.
  """
  parser = argparse.ArgumentParser(
      description=(
          "MTTN AutoSubs Tool (La Matatena) — App independiente y puente para"
          " DaVinci Resolve 20"
      )
  )
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=55120)
  parser.add_argument("--no-browser", action="store_true")
  parser.add_argument("--app-window", action="store_true")
  parser.add_argument(
      "--desktop",
      action="store_true",
      help="Abre la app en una ventana nativa de macOS (requiere pywebview).",
  )
  parser.add_argument(
      "--cli",
      metavar="MEDIA_FILE",
      help="Modo CLI independiente: transcribe un audio/vídeo y sale.",
  )
  parser.add_argument("--output", "-o", help="Ruta de salida para --cli.")
  parser.add_argument(
      "--format",
      "-f",
      default="srt",
      choices=(
          "srt",
          "srt_tc",
          "vtt",
          "ass",
          "txt",
          "txt_tc",
          "fcpxml",
          "json",
      ),
      help="Formato de exportación para --cli (por defecto srt).",
  )
  parser.add_argument("--model", "-m", help="ID del modelo Whisper GGML.")
  parser.add_argument(
      "--domain",
      default="juegos_mesa",
      choices=tuple(spanish_nlp.DOMAIN_PROFILES.keys()),
      help="Temática y diccionario de anglicismos.",
  )
  parser.add_argument(
      "--dialect",
      default="neutro",
      choices=tuple(spanish_nlp.SPANISH_DIALECT_PROMPTS.keys()),
  )
  parser.add_argument(
      "--preset",
      default="youtube_16_9",
      choices=tuple(spanish_nlp.FORMAT_PRESETS.keys()),
  )
  parser.add_argument("--glossary", default="")
  parser.add_argument("--threads", type=int, default=None)
  parser.add_argument("--fast-beam", action="store_true")
  parser.add_argument("--remove-muletillas", action="store_true")
  parser.add_argument(
      "--karaoke",
      action="store_true",
      help="Incluye karaoke palabra por palabra (\\kf) en exportaciones .ass.",
  )
  args = parser.parse_args(argv)

  if args.cli:
    return run_cli_transcription(
        media_file=args.cli,
        output_path=args.output,
        fmt=args.format,
        model_id=args.model,
        domain=args.domain,
        dialect=args.dialect,
        preset_id=args.preset,
        glossary=args.glossary,
        threads=args.threads,
        fast_beam=args.fast_beam,
        remove_muletillas=args.remove_muletillas,
        karaoke=args.karaoke,
    )

  if args.desktop:
    return run_desktop_app(args.host, args.port)

  server, _ = create_server(host=args.host, port=args.port)
  actual_port = server.server_address[1]
  url = f"http://{args.host}:{actual_port}"
  print(f"[MTTN AutoSubs Tool] Servidor activo en {url}")
  profile = hardware_imac2017.detect_hardware_profile()
  print(
      f"[MTTN AutoSubs Tool] La Matatena · Perfil de hardware: {profile.name}"
      f" ({'solo CPU' if profile.force_no_gpu else 'GPU Metal'},"
      f" {profile.threads_standalone} hilos)"
  )
  if not args.no_browser:
    # Delay so the browser connects after serve_forever() is running.
    opener = open_standalone_window if args.app_window else webbrowser.open
    threading.Timer(0.6, lambda: opener(url)).start()

  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\n[MTTN AutoSubs Tool] Cerrando servidor...")
  finally:
    server.server_close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
