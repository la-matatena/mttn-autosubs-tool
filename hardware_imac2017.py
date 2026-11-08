"""Hardware governor and whisper.cpp profile for 2017 Intel Retina 4K iMac."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class HardwareProfile:
  """Target workstation profile for CPU/VRAM scheduling."""

  name: str
  cpu_arch: str
  cpu_description: str
  physical_cores: int
  logical_threads: int
  gpu_name: str
  gpu_vram_mb: int
  ram_gb: int
  os_target: str
  force_no_gpu: bool
  threads_with_resolve: int
  threads_standalone: int
  nice_level: int


IMAC_2017_PROFILE = HardwareProfile(
    name="iMac Retina 4K 21.5-inch (2017)",
    cpu_arch="x86_64",
    cpu_description="3 GHz Quad-Core Intel Core i5 (AVX2, sin Hyper-Threading)",
    physical_cores=4,
    logical_threads=4,
    gpu_name="AMD Radeon Polaris",
    gpu_vram_mb=2048,
    ram_gb=32,
    os_target="macOS 15.8.1 (x86_64)",
    force_no_gpu=True,
    threads_with_resolve=3,
    threads_standalone=4,
    nice_level=10,
)


@dataclass(frozen=True)
class ModelSpec:
  """Downloadable GGML Whisper model entry optimized for Spanish on CPU."""

  model_id: str
  label: str
  filename: str
  url: str
  size_mb: int
  ram_mb: int
  spanish_wer: str
  speed_note: str
  recommended: bool = False


MODEL_CATALOG: dict[str, ModelSpec] = {
    "turbo-q8": ModelSpec(
        model_id="turbo-q8",
        label="Large-v3-Turbo Q8_0 (Recomendado 32 GB RAM)",
        filename="ggml-large-v3-turbo-q8_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-large-v3-turbo-q8_0.bin"
        ),
        size_mb=834,
        ram_mb=1550,
        spanish_wer="~2.1% WER",
        speed_note=(
            "8-bit casi sin pérdida (~1.5 GB de tus 32 GB RAM): máxima"
            " fidelidad en español y anglicismos con velocidad Turbo en i5."
        ),
        recommended=True,
    ),
    "large-v3-q5": ModelSpec(
        model_id="large-v3-q5",
        label="Large-v3 Q5_0 (Máxima precisión ES + Anglicismos)",
        filename="ggml-large-v3-q5_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-large-v3-q5_0.bin"
        ),
        size_mb=1080,
        ram_mb=2100,
        spanish_wer="~1.9% WER",
        speed_note=(
            "Decoder completo de 32 capas (~2.1 GB RAM): el más preciso en"
            " jerga bilingüe compleja y juegos de mesa; más lento en i5."
        ),
        recommended=False,
    ),
    "turbo-q5": ModelSpec(
        model_id="turbo-q5",
        label="Large-v3-Turbo Q5_0 (Rápido equilibrado)",
        filename="ggml-large-v3-turbo-q5_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-large-v3-turbo-q5_0.bin"
        ),
        size_mb=547,
        ram_mb=1100,
        spanish_wer="~2.4% WER",
        speed_note=(
            "4 capas decoder ligeras: excelente velocidad en i5 de 4 núcleos y"
            " muy buena precisión general en español."
        ),
        recommended=False,
    ),
    "small-q8": ModelSpec(
        model_id="small-q8",
        label="Small Q8_0 (Borrador veloz)",
        filename="ggml-small-q8_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-small-q8_0.bin"
        ),
        size_mb=252,
        ram_mb=650,
        spanish_wer="~5.5% WER",
        speed_note=(
            "Muy rápido en 3 núcleos i5; útil para borradores largos o clips"
            " con audio limpio."
        ),
        recommended=False,
    ),
    "base-q8": ModelSpec(
        model_id="base-q8",
        label="Base Q8_0 (Ultra ligero)",
        filename="ggml-base-q8_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-base-q8_0.bin"
        ),
        size_mb=82,
        ram_mb=320,
        spanish_wer="~8.5% WER",
        speed_note=(
            "Descarga instantánea para pruebas rápidas de flujo con o sin"
            " DaVinci Resolve 20."
        ),
        recommended=False,
    ),
}

_WHISPER_FLAG_CACHE: dict[tuple[str, str], bool] = {}


def get_app_data_dir(create: bool = True) -> Path:
  """Returns the AutoSubs ES application data directory."""
  override = os.environ.get("AUTOSUBS_ES_HOME")
  if override:
    base = Path(override).expanduser()
  elif sys.platform == "darwin":
    base = Path.home() / "Library" / "Application Support" / "AutoSubsES"
  else:
    base = Path.home() / ".local" / "share" / "autosubs_es"

  if create:
    for sub in ("models", "fonts", "bridge/inbox", "bridge/outbox", "cache"):
      (base / sub).mkdir(parents=True, exist_ok=True)
  return base


def get_resolve_user_fusion_dir() -> Path:
  """Returns the DaVinci Resolve Fusion user directory on macOS or Linux."""
  override = os.environ.get("AUTOSUBS_RESOLVE_FUSION_DIR")
  if override:
    return Path(override).expanduser()
  if sys.platform == "darwin":
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Blackmagic Design"
        / "DaVinci Resolve"
        / "Fusion"
    )
  return Path.home() / ".local" / "share" / "DaVinciResolve" / "Fusion"


def find_binary(candidates: list[str]) -> str | None:
  """Finds an executable checking Homebrew Intel, Apple Silicon, and PATH."""
  search_dirs = [
      "/usr/local/bin",
      "/usr/local/opt/whisper-cpp/bin",
      "/opt/homebrew/bin",
      "/usr/bin",
  ]
  for name in candidates:
    for directory in search_dirs:
      candidate = Path(directory) / name
      if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    resolved = shutil.which(name)
    if resolved:
      return resolved
  return None


def is_resolve_running() -> bool:
  """Checks whether DaVinci Resolve is currently running without blocking."""
  pgrep_bin = shutil.which("pgrep")
  if not pgrep_bin:
    return False
  for pattern in ("DaVinci Resolve", "Resolve"):
    try:
      result = subprocess.run(
          [pgrep_bin, "-x", pattern],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=1.5,
          check=False,
      )
      if result.returncode == 0:
        return True
    except (OSError, subprocess.SubprocessError):
      return False
  return False


def get_installed_models(models_dir: Path | None = None) -> dict[str, dict]:
  """Inspects downloaded GGML model files and returns catalog metadata."""
  target_dir = models_dir or (get_app_data_dir(create=False) / "models")
  installed: dict[str, dict] = {}
  for model_id, spec in MODEL_CATALOG.items():
    model_path = target_dir / spec.filename
    partial_path = target_dir / f"{spec.filename}.part"
    exists = model_path.is_file() and model_path.stat().st_size > 1024 * 1024
    installed[model_id] = {
        **asdict(spec),
        "installed": exists,
        "path": str(model_path),
        "disk_bytes": model_path.stat().st_size if model_path.is_file() else 0,
        "partial_bytes": (
            partial_path.stat().st_size if partial_path.is_file() else 0
        ),
    }
  return installed


def choose_default_model_id(models_dir: Path | None = None) -> str:
  """Selects the best already-installed model, or turbo-q8 by default."""
  models = get_installed_models(models_dir)
  for preferred in (
      "turbo-q8",
      "large-v3-q5",
      "turbo-q5",
      "small-q8",
      "base-q8",
  ):
    if models[preferred]["installed"]:
      return preferred
  return "turbo-q8"


def whisper_supports_flag(whisper_bin: str, flag: str) -> bool:
  """Checks whether a whisper-cli binary supports a given CLI flag."""
  key = (whisper_bin, flag)
  if key in _WHISPER_FLAG_CACHE:
    return _WHISPER_FLAG_CACHE[key]
  cand = Path(whisper_bin)
  if not (cand.is_file() and os.access(cand, os.X_OK)):
    return False
  try:
    res = subprocess.run(
        [whisper_bin, "--help"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=2.0,
        check=False,
    )
    supported = flag in f"{res.stdout}\n{res.stderr}"
    _WHISPER_FLAG_CACHE[key] = supported
    return supported
  except (OSError, subprocess.SubprocessError):
    return False


def detect_environment() -> dict:
  """Collects host hardware, binary paths, model availability, and Resolve state."""
  app_dir = get_app_data_dir(create=True)
  whisper_bin = find_binary(["whisper-cli", "whisper-cpp", "main"])
  ffmpeg_bin = find_binary(["ffmpeg"])
  resolve_running = is_resolve_running()
  recommended_threads = (
      IMAC_2017_PROFILE.threads_with_resolve
      if resolve_running
      else IMAC_2017_PROFILE.threads_standalone
  )

  host_arch = platform.machine() or "unknown"
  host_os = f"{platform.system()} {platform.release()}".strip()

  return {
      "app_data_dir": str(app_dir),
      "resolve_fusion_dir": str(get_resolve_user_fusion_dir()),
      "whisper_cli_path": whisper_bin,
      "ffmpeg_path": ffmpeg_bin,
      "resolve_running": resolve_running,
      "recommended_threads": recommended_threads,
      "default_model_id": choose_default_model_id(app_dir / "models"),
      "models": get_installed_models(app_dir / "models"),
      "hardware_profile": asdict(IMAC_2017_PROFILE),
      "host_runtime": {
          "arch": host_arch,
          "os": host_os,
          "python_version": platform.python_version(),
          "cpu_count": os.cpu_count() or 4,
      },
  }


def build_whisper_command(
    whisper_bin: str,
    model_path: str,
    wav_path: str,
    output_prefix: str,
    prompt: str = "",
    threads: int | None = None,
    force_no_gpu: bool = True,
    fast_beam: bool = False,
    nice_level: int = IMAC_2017_PROFILE.nice_level,
    carry_initial_prompt: bool | None = None,
) -> list[str]:
  """Builds the whisper-cli invocation tuned for iMac 2017 (i5 + 2GB Polaris)."""
  if threads is None:
    threads = (
        IMAC_2017_PROFILE.threads_with_resolve
        if is_resolve_running()
        else IMAC_2017_PROFILE.threads_standalone
    )
  effective_threads = max(1, min(int(threads), 8))

  cmd: list[str] = []
  nice_bin = shutil.which("nice")
  if nice_bin and nice_level > 0 and os.name == "posix":
    cmd.extend([nice_bin, "-n", str(nice_level)])

  cmd.extend([
      whisper_bin,
      "-m",
      model_path,
      "-f",
      wav_path,
      "-l",
      "es",
      "-t",
      str(effective_threads),
      "-ojf",
      "-of",
      output_prefix,
      "-sow",
      "-bs",
      "2" if fast_beam else "5",
      "-et",
      "2.4",
      "-lpt",
      "-1.0",
      "-nf",
  ])

  if force_no_gpu:
    cmd.append("-ng")
  if prompt.strip():
    cmd.extend(["--prompt", prompt.strip()])
    use_carry = (
        carry_initial_prompt
        if carry_initial_prompt is not None
        else whisper_supports_flag(whisper_bin, "--carry-initial-prompt")
    )
    if use_carry:
      cmd.extend(["--carry-initial-prompt", "-mc", "64"])

  return cmd


def escape_ffmpeg_filter_path(path_str: str) -> str:
  """Escapes special characters in a file path for FFmpeg libass filter."""
  out = path_str.replace("\\", "\\\\")
  for ch in (":", "'", ",", "[", "]"):
    out = out.replace(ch, f"\\{ch}")
  return out


def build_ffmpeg_burnin_command(
    ffmpeg_bin: str,
    input_video_path: str,
    ass_path: str,
    output_mp4_path: str,
    threads: int = 3,
    nice_level: int = IMAC_2017_PROFILE.nice_level,
    fonts_dir: str | None = None,
) -> list[str]:
  """Builds CPU-only libx264 FFmpeg subtitle burn-in command (0 MB Polaris)."""
  effective_threads = max(1, min(int(threads), 8))
  cmd: list[str] = []
  nice_bin = shutil.which("nice")
  if nice_bin and nice_level > 0 and os.name == "posix":
    cmd.extend([nice_bin, "-n", str(nice_level)])

  escaped_ass = escape_ffmpeg_filter_path(ass_path)
  vf_filter = f"ass='{escaped_ass}'"
  if fonts_dir:
    vf_filter += f":fontsdir='{escape_ffmpeg_filter_path(fonts_dir)}'"
  cmd.extend([
      ffmpeg_bin,
      "-y",
      "-i",
      input_video_path,
      "-vf",
      vf_filter,
      "-c:v",
      "libx264",
      "-preset",
      "veryfast",
      "-crf",
      "20",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-b:a",
      "192k",
      "-movflags",
      "+faststart",
      "-threads",
      str(effective_threads),
      output_mp4_path,
  ])
  return cmd
