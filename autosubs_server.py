"""Local HTTP server and orchestration backend for AutoSubs ES."""

from __future__ import annotations

import argparse
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.parse
import urllib.request
import uuid
import webbrowser

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import exporters
  from google3.experimental.users.joseluiszapata.autosubs_es import font_manager
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
  from google3.experimental.users.joseluiszapata.autosubs_es import resolve_bridge
  from google3.experimental.users.joseluiszapata.autosubs_es import spanish_nlp
except ImportError:
  import exporters  # type: ignore[no-redef]
  import font_manager  # type: ignore[no-redef]
  import hardware_imac2017  # type: ignore[no-redef]
  import resolve_bridge  # type: ignore[no-redef]
  import spanish_nlp  # type: ignore[no-redef]


def _find_ui_dir() -> Path:
  """Locates the ui/ directory across standalone macOS and Blaze runfiles."""
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

_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v", ".mkv"})
_BROWSER_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_RETAINED_JOBS = 100


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
  """Stops a child process group (nice + whisper-cli/ffmpeg) cleanly."""
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


class AppState:
  """Thread-safe application state shared across HTTP handler threads."""

  def __init__(self, app_dir: Path | None = None) -> None:
    self.app_dir = app_dir or hardware_imac2017.get_app_data_dir(create=True)
    self.cache_dir = self.app_dir / "cache"
    self.models_dir = self.app_dir / "models"
    self.cache_dir.mkdir(parents=True, exist_ok=True)
    self.models_dir.mkdir(parents=True, exist_ok=True)

    self.bridge = resolve_bridge.ResolveBridge(app_data_dir=self.app_dir)
    self.fonts = font_manager.FontManager(app_dir=self.app_dir)
    self.lock = threading.Lock()
    # shortcut: single local user session in RAM (<=100 jobs retained), persist to SQLite if multi-project history is needed
    self.jobs: dict[str, dict[str, Any]] = {}
    self.active_procs: dict[str, subprocess.Popen[Any]] = {}
    self.cancel_flags: dict[str, bool] = {}

    self.media_path: Path | None = None
    self.wav_path: Path | None = None
    self.exported_video_path: Path | None = None
    self._last_inbox_sig: tuple[str, int, int] | None = None
    self.media_name: str = ""
    self.duration_ms: int = 0
    self.peaks: list[float] = []
    self.words: list[spanish_nlp.WordTiming] = []
    self.cues: list[spanish_nlp.SubtitleCue] = []
    self.format_config = spanish_nlp.SpanishFormatConfig()
    self.demo_mode: bool = False

  def session_snapshot(self) -> dict[str, Any]:
    with self.lock:
      is_video = bool(
          self.media_path
          and self.media_path.suffix.lower() in _BROWSER_VIDEO_EXTENSIONS
      )
      can_burnin = bool(
          self.media_path
          and self.media_path.suffix.lower() in _VIDEO_EXTENSIONS
      )
      has_exported_video = bool(
          self.exported_video_path and self.exported_video_path.is_file()
      )
      active_file = self.media_path or self.wav_path
      media_rev: str | None = None
      if active_file and active_file.is_file():
        st = active_file.stat()
        media_rev = f"{active_file.name}:{st.st_mtime_ns}:{st.st_size}"
      return {
          "has_media": self.wav_path is not None or self.media_path is not None,
          "media_name": self.media_name,
          "media_rev": media_rev,
          "media_url": (
              "/api/media" if (self.wav_path or self.media_path) else None
          ),
          "is_video": is_video,
          "can_burnin_video": can_burnin,
          "exported_video_url": (
              "/api/exported-video" if has_exported_video else None
          ),
          "duration_ms": self.duration_ms,
          "peaks": list(self.peaks),
          "word_count": len(self.words),
          "cue_count": len(self.cues),
          "cues": [c.to_dict() for c in self.cues],
          "format_config": self.format_config.to_dict(),
          "demo_mode": self.demo_mode,
      }

  def load_demo(
      self, format_raw: dict[str, Any] | None = None
  ) -> dict[str, Any]:
    """Populates a Spanish board-game + Resolve demo session."""
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
      self.demo_mode = True
    return self.session_snapshot()

  def create_job(self, kind: str, detail: str = "") -> str:
    job_id = uuid.uuid4().hex[:10]
    with self.lock:
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
        stale_ids = [
            jid for jid, j in self.jobs.items() if j.get("status") != "running"
        ]
        for old_id in stale_ids[: max(0, len(self.jobs) - _MAX_RETAINED_JOBS)]:
          self.jobs.pop(old_id, None)
          self.cancel_flags.pop(old_id, None)
    return job_id

  def update_job(self, job_id: str, **updates: Any) -> None:
    with self.lock:
      if job_id in self.jobs:
        self.jobs[job_id].update(updates)

  def cancel_job(self, job_id: str) -> bool:
    with self.lock:
      if job_id not in self.jobs:
        return False
      self.cancel_flags[job_id] = True
      proc = self.active_procs.get(job_id)
      self.jobs[job_id]["status"] = "cancelled"
      self.jobs[job_id]["stage"] = "Cancelado por el usuario"
      self.jobs[job_id]["finished_at"] = time.time()
    if proc is not None and proc.poll() is None:
      _terminate_process(proc)
    return True

  def start_model_download(self, model_id: str) -> str:
    spec = hardware_imac2017.MODEL_CATALOG.get(model_id)
    if not spec:
      raise ValueError(f"Modelo desconocido: {model_id}")

    job_id = self.create_job(
        "download", f"Conectando con Hugging Face ({spec.filename})..."
    )

    def _worker() -> None:
      dest = self.models_dir / spec.filename
      part = self.models_dir / f"{spec.filename}.part"
      try:
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
        req = urllib.request.Request(
            spec.url,
            headers={"User-Agent": "AutoSubsES/1.0 (macOS x86_64)"},
        )
        if existing > 0:
          req.add_header("Range", f"bytes={existing}-")

        with urllib.request.urlopen(req, timeout=30) as resp:
          status_code = getattr(resp, "status", 200)
          mode = "ab" if (existing > 0 and status_code == 206) else "wb"
          if mode == "wb":
            existing = 0
          content_len = int(resp.headers.get("Content-Length") or 0)
          total = (
              (existing + content_len)
              if content_len > 0
              else spec.size_mb * 1024 * 1024
          )
          downloaded = existing

          with open(part, mode) as fp:
            while True:
              with self.lock:
                if self.cancel_flags.get(job_id):
                  return
              chunk = resp.read(262144)
              if not chunk:
                break
              fp.write(chunk)
              downloaded += len(chunk)
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

    threading.Thread(target=_worker, daemon=True).start()
    return job_id

  def start_transcription(self, payload: dict[str, Any]) -> str:
    source = str(payload.get("source", "uploaded"))
    model_id = str(
        payload.get("model_id")
        or hardware_imac2017.choose_default_model_id(self.models_dir)
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
            " Workspace > Scripts > 1 - AutoSubs ES - Exportar Audio."
        )
    elif payload.get("media_path"):
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

    cfg = spanish_nlp.SpanishFormatConfig.from_dict(
        payload.get("format_config") or self.format_config.to_dict()
    )
    threads = payload.get("threads")
    fast_beam = bool(payload.get("fast_beam", False))
    enhance_voice = bool(payload.get("enhance_voice", True))
    force_no_gpu = bool(payload.get("force_no_gpu", True))

    job_id = self.create_job("transcribe", "Preparando audio 16 kHz mono...")

    def _worker() -> None:
      working_wav = self.cache_dir / f"working_{job_id}_16k.wav"
      out_prefix = self.cache_dir / f"whisper_{job_id}"
      ojf_json = Path(f"{out_prefix}.json")
      completed_ok = False
      old_wav: Path | None = None
      try:
        resolve_bridge.prepare_wav_16k(
            str(src_path),
            str(working_wav),
            ffmpeg_bin=ffmpeg_bin,
            enhance_voice=enhance_voice,
        )
        wave_info = resolve_bridge.extract_waveform_peaks(str(working_wav))
        self.update_job(
            job_id,
            progress=15,
            stage=(
                f"Transcribiendo en español con {spec.label} (CPU AVX2, GPU"
                " Polaris libre)..."
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
              wpct = int(match.group(1))
              scaled = 15 + int(wpct * 0.78)
              self.update_job(
                  job_id,
                  progress=min(94, scaled),
                  stage=f"Transcribiendo audio en español ({wpct}%)...",
              )

        rc = proc.wait()
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

        with self.lock:
          old_wav = self.wav_path
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
        completed_ok = True

        self.update_job(
            job_id,
            status="done",
            progress=100,
            stage=f"¡Listo! {len(cues)} subtítulos generados en español.",
            finished_at=time.time(),
            result=self.session_snapshot(),
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
        elif (
            old_wav is not None
            and old_wav != working_wav
            and old_wav.parent == self.cache_dir
            and old_wav.name.startswith("working_")
            and old_wav.name.endswith("_16k.wav")
        ):
          old_wav.unlink(missing_ok=True)

    threading.Thread(target=_worker, daemon=True).start()
    return job_id

  def start_video_burnin(self, payload: dict[str, Any]) -> str:
    """Burns styled .ass subtitles onto local video using CPU libx264."""
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
    ffmpeg_bin = env.get("ffmpeg_path")
    if not ffmpeg_bin:
      raise RuntimeError(
          "No se encontró ffmpeg. Instala con: brew install ffmpeg"
      )

    style = exporters.StyleConfig.from_dict(payload.get("style"))
    width = int(payload.get("width") or 1920)
    height = int(payload.get("height") or 1080)
    threads = int(payload.get("threads") or env.get("recommended_threads") or 3)
    job_id = self.create_job(
        "burnin", "Preparando render CPU (libx264, GPU Polaris libre)..."
    )

    def _worker() -> None:
      ass_file = self.cache_dir / f"burnin_{job_id}.ass"
      base_stem = _safe_filename(Path(media_name).stem)
      out_mp4 = self.cache_dir / f"{base_stem}_subtitulado.mp4"
      try:
        ass_content = exporters.export_ass_karaoke(cues, style, width, height)
        ass_file.write_text(ass_content, encoding="utf-8")
        self.update_job(
            job_id,
            progress=25,
            stage="Codificando MP4 con subtítulos (CPU libx264)...",
        )
        cmd = hardware_imac2017.build_ffmpeg_burnin_command(
            ffmpeg_bin=ffmpeg_bin,
            input_video_path=str(src_video),
            ass_path=str(ass_file),
            output_mp4_path=str(out_mp4),
            threads=threads,
            fonts_dir=str(self.fonts.app_fonts_dir),
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
          for _ in proc.stdout:
            with self.lock:
              if self.cancel_flags.get(job_id):
                break
        rc = proc.wait()
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
  decoded = urllib.parse.unquote(raw_name or "media.wav")
  base = Path(decoded).name
  cleaned = re.sub(r"[^A-Za-z0-9._\-\u00C0-\u017F]", "_", base)
  return cleaned or "media_upload.bin"


class AutoSubsHandler(BaseHTTPRequestHandler):
  """HTTP request handler serving static Studio UI and JSON APIs."""

  protocol_version = "HTTP/1.1"

  def __init__(self, *args: Any, state: AppState, **kwargs: Any) -> None:
    self.state = state
    super().__init__(*args, **kwargs)

  def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
    # Suppress noisy per-request access logs in interactive terminal.
    return

  def _check_request_origin(self, mutating: bool) -> bool:
    host_hdr = (self.headers.get("Host") or "").strip()
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

  def _send_json(self, status: int, payload: Any) -> None:
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
    length = int(self.headers.get("Content-Length") or 0)
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    if not raw:
      return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}

  def _serve_static(self, rel_path: str) -> None:
    safe = rel_path.lstrip("/") or "index.html"
    if safe in ("", "/"):
      safe = "index.html"
    if ".." in Path(safe).parts or Path(safe).is_absolute():
      self.send_error(HTTPStatus.FORBIDDEN)
      return
    ui_root = _find_ui_dir()
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
    total = target.stat().st_size
    range_hdr = self.headers.get("Range")
    start = 0
    end = max(0, total - 1)
    status = HTTPStatus.OK
    if total > 0 and range_hdr and range_hdr.startswith("bytes="):
      spec = range_hdr.replace("bytes=", "", 1).split(",")[0].strip()
      parts = spec.split("-")
      try:
        if parts[0]:
          start = int(parts[0])
          if len(parts) > 1 and parts[1]:
            end = min(total - 1, int(parts[1]))
        elif len(parts) > 1 and parts[1]:
          suffix = int(parts[1])
          start = max(0, total - suffix)
          end = total - 1
        if 0 <= start <= end < total:
          status = HTTPStatus.PARTIAL_CONTENT
        else:
          start, end = 0, total - 1
      except ValueError:
        start, end = 0, total - 1

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
    with self.state.lock:
      media = self.state.media_path or self.state.wav_path
      if (
          media
          and media.suffix.lower()
          not in (".wav", ".mp3", ".m4a", ".mp4", ".mov", ".webm")
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

  def do_GET(self) -> None:
    if not self._check_request_origin(mutating=False):
      return
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path

    if path in ("/", "/index.html", "/styles.css", "/app.js"):
      self._serve_static(path)
      return

    if path == "/api/media":
      self._serve_media()
      return

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

    if path == "/api/status":
      env = hardware_imac2017.detect_environment()
      bridge_status = self.state.bridge.get_status()
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
          },
      )
      return

    if path == "/api/fonts":
      self._send_json(HTTPStatus.OK, self.state.fonts.list_fonts())
      return

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

    if path.startswith("/api/jobs/"):
      job_id = path.split("/api/jobs/", 1)[1].strip("/")
      with self.state.lock:
        job = self.state.jobs.get(job_id)
      if not job:
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "Trabajo no encontrado"}
        )
        return
      self._send_json(HTTPStatus.OK, job)
      return

    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

  def do_POST(self) -> None:
    if not self._check_request_origin(mutating=True):
      return
    parsed = urllib.parse.urlparse(self.path)
    path = parsed.path

    if path == "/api/upload":
      raw_name = self.headers.get("X-Filename", "media.wav")
      safe_name = _safe_filename(raw_name)
      length = int(self.headers.get("Content-Length") or 0)
      if length <= 0:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Archivo vacío o sin Content-Length"},
        )
        return
      dest = self.state.cache_dir / safe_name
      remaining = length
      with open(dest, "wb") as fp:
        while remaining > 0:
          chunk = self.rfile.read(min(1048576, remaining))
          if not chunk:
            break
          fp.write(chunk)
          remaining -= len(chunk)

      wave_info = {"peaks": [], "duration_ms": 0}
      if dest.suffix.lower() == ".wav":
        wave_info = resolve_bridge.extract_waveform_peaks(str(dest))

      with self.state.lock:
        self.state.media_path = dest
        self.state.wav_path = dest if dest.suffix.lower() == ".wav" else None
        self.state.exported_video_path = None
        self.state.media_name = safe_name
        self.state.duration_ms = int(wave_info["duration_ms"])
        self.state.peaks = list(wave_info["peaks"])
        self.state.words = []
        self.state.cues = []
        self.state._last_inbox_sig = None
        self.state.demo_mode = False

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

    if path == "/api/fonts/upload":
      raw_name = self.headers.get("X-Filename", "custom_font.otf")
      decoded_name = urllib.parse.unquote(raw_name)
      install_hdr = (self.headers.get("X-Install-Macos", "1") or "1").strip()
      install_to_macos = install_hdr.lower() not in ("0", "false", "no")
      length = int(self.headers.get("Content-Length") or 0)
      if length <= 0:
        self._send_json(
            HTTPStatus.BAD_REQUEST,
            {"error": "Archivo de fuente vacío o sin Content-Length"},
        )
        return
      if length > font_manager.MAX_UPLOAD_BYTES:
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

    try:
      body = self._read_json_body()
    except json.JSONDecodeError as exc:
      self._send_json(
          HTTPStatus.BAD_REQUEST, {"error": f"JSON inválido: {exc}"}
      )
      return

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

    if path == "/api/transcribe":
      try:
        job_id = self.state.start_transcription(body)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    if path == "/api/export-video":
      try:
        job_id = self.state.start_video_burnin(body)
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "job_id": job_id})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      return

    if path.startswith("/api/jobs/") and path.endswith("/cancel"):
      parts = path.split("/")
      job_id = parts[3] if len(parts) >= 5 else ""
      ok = self.state.cancel_job(job_id)
      self._send_json(HTTPStatus.OK, {"ok": ok})
      return

    if path == "/api/demo":
      snap = self.state.load_demo(body.get("format_config"))
      self._send_json(HTTPStatus.OK, {"ok": True, "session": snap})
      return

    if path == "/api/resegment":
      cfg = spanish_nlp.SpanishFormatConfig.from_dict(body.get("format_config"))
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

    if path == "/api/export":
      fmt = str(body.get("format", "srt")).lower()
      style = exporters.StyleConfig.from_dict(body.get("style"))
      meta = self.state.bridge.read_timeline_meta()
      fps = float(body.get("fps") or meta.get("fps") or 24.0)
      start_frame = int(
          body.get("timeline_start_frame") or meta.get("start_frame") or 86400
      )
      width = int(meta.get("width") or 1920)
      height = int(meta.get("height") or 1080)
      tl_name = str(meta.get("timeline_name") or "AutoSubs ES")

      with self.state.lock:
        cues = list(self.state.cues)
        base_stem = Path(self.state.media_name or "subtitulos_es").stem

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

    if path == "/api/resolve/export-audio":
      res = self.state.bridge.export_timeline_audio_studio()
      self._send_json(HTTPStatus.OK, res)
      return

    if path == "/api/resolve/notify-export":
      inbox_wav = self.state.bridge.get_inbox_audio_path()
      if inbox_wav and inbox_wav.is_file():
        st = inbox_wav.stat()
        sig = (str(inbox_wav), st.st_mtime_ns, st.st_size)
        wave_info = resolve_bridge.extract_waveform_peaks(str(inbox_wav))
        with self.state.lock:
          changed = sig != self.state._last_inbox_sig
          self.state._last_inbox_sig = sig
          self.state.media_path = inbox_wav
          self.state.wav_path = inbox_wav
          self.state.media_name = inbox_wav.name
          self.state.duration_ms = int(wave_info["duration_ms"])
          self.state.peaks = list(wave_info["peaks"])
          self.state.demo_mode = False
          if changed:
            self.state.exported_video_path = None
            self.state.words = []
            self.state.cues = []
      self._send_json(
          HTTPStatus.OK,
          {
              "ok": True,
              "bridge": self.state.bridge.get_status(),
              "session": self.state.session_snapshot(),
          },
      )
      return

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

    if path == "/api/resolve/jump":
      res = self.state.bridge.jump_playhead(int(body.get("ms", 0)))
      self._send_json(HTTPStatus.OK, res)
      return

    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

  def do_PUT(self) -> None:
    if not self._check_request_origin(mutating=True):
      return
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path != "/api/cues":
      self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})
      return
    try:
      body = self._read_json_body()
    except json.JSONDecodeError as exc:
      self._send_json(
          HTTPStatus.BAD_REQUEST, {"error": f"JSON inválido: {exc}"}
      )
      return

    raw_cues = body.get("cues", [])
    if not isinstance(raw_cues, list):
      self._send_json(
          HTTPStatus.BAD_REQUEST, {"error": "'cues' debe ser lista"}
      )
      return

    with self.state.lock:
      cfg = self.state.format_config
      updated: list[spanish_nlp.SubtitleCue] = []
      for idx, item in enumerate(raw_cues, start=1):
        cue = spanish_nlp.SubtitleCue.from_dict(item)
        cue.index = idx
        if updated and cue.start_ms < updated[-1].end_ms:
          cue.start_ms = updated[-1].end_ms
          cue.end_ms = max(cue.start_ms + 80, cue.end_ms)
        spanish_nlp.refresh_cue_metrics(cue, cfg)
        updated.append(cue)
      self.state.cues = updated

    self._send_json(
        HTTPStatus.OK, {"ok": True, "session": self.state.session_snapshot()}
    )


def create_server(
    host: str = "127.0.0.1",
    port: int = 55120,
    app_dir: Path | None = None,
) -> tuple[ThreadingHTTPServer, AppState]:
  """Creates the AutoSubs ES ThreadingHTTPServer bound to host:port."""
  state = AppState(app_dir=app_dir)
  handler_cls = partial(AutoSubsHandler, state=state)
  server = ThreadingHTTPServer((host, port), handler_cls)
  return server, state


def open_standalone_window(url: str) -> None:
  """Opens AutoSubs ES in a chromeless macOS app window when possible."""
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
    force_no_gpu: bool = True,
    remove_muletillas: bool = False,
    app_dir: Path | None = None,
) -> int:
  """Runs standalone offline Spanish transcription directly from CLI."""
  src = Path(media_file).expanduser()
  if not src.is_file():
    print(f"[AutoSubs ES] Error: no existe el archivo {src}", file=sys.stderr)
    return 1

  app_base = app_dir or hardware_imac2017.get_app_data_dir(create=True)
  models_dir = app_base / "models"
  cache_dir = app_base / "cache"
  cache_dir.mkdir(parents=True, exist_ok=True)

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
    style = exporters.StyleConfig()

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
  parser = argparse.ArgumentParser(
      description=(
          "AutoSubs ES — App independiente y puente para DaVinci Resolve 20"
      )
  )
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=55120)
  parser.add_argument("--no-browser", action="store_true")
  parser.add_argument("--app-window", action="store_true")
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
    )

  server, _ = create_server(host=args.host, port=args.port)
  actual_port = server.server_address[1]
  url = f"http://{args.host}:{actual_port}"
  print(f"[AutoSubs ES] Servidor activo en {url}")
  print("[AutoSubs ES] Optimizado para iMac 2017 (CPU AVX2, GPU Polaris libre)")
  if not args.no_browser:
    opener = open_standalone_window if args.app_window else webbrowser.open
    threading.Timer(0.6, lambda: opener(url)).start()

  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\n[AutoSubs ES] Cerrando servidor...")
  finally:
    server.server_close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
