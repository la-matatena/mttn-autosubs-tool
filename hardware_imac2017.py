"""Hardware governor and whisper.cpp profiles (Intel iMac 2017 / Apple Silicon)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import functools
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
  profile_id: str = "intel_imac2017"
  cpu_short: str = "CPU i5 AVX2"
  video_encoder: str = "libx264"


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

# whisper.cpp scales poorly past 8 threads; with Metal the GPU does most work.
_MAX_WHISPER_THREADS = 8


def _sysctl(key: str) -> str:
  """Reads a macOS sysctl value, returning '' when unavailable."""
  sysctl_bin = shutil.which("sysctl") or "/usr/sbin/sysctl"
  try:
    res = subprocess.run(
        [sysctl_bin, "-n", key],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""
  except (OSError, subprocess.SubprocessError):
    return ""


def _sysctl_int(key: str, default: int = 0) -> int:
  try:
    return int(_sysctl(key))
  except ValueError:
    return default


def build_apple_silicon_profile(
    chip_name: str, perf_cores: int, total_cores: int, ram_gb: int
) -> HardwareProfile:
  """Builds a Metal-accelerated profile for an Apple Silicon (M1–M4+) Mac."""
  perf = max(1, perf_cores or total_cores or 4)
  standalone = max(2, min(perf, _MAX_WHISPER_THREADS))
  # Leave performance cores free for Resolve playback when it is open.
  with_resolve = max(2, min(standalone - 2, perf // 2 + 1))
  return HardwareProfile(
      name=f"Mac con {chip_name}",
      cpu_arch="arm64",
      cpu_description=(
          f"{chip_name} ({perf} núcleos de rendimiento,"
          f" {max(0, total_cores - perf)} de eficiencia)"
      ),
      physical_cores=total_cores or perf,
      logical_threads=total_cores or perf,
      gpu_name=f"GPU {chip_name} (Metal)",
      gpu_vram_mb=0,  # Unified memory: shared with system RAM.
      ram_gb=ram_gb,
      os_target=f"macOS {platform.mac_ver()[0] or ''} (arm64)".strip(),
      force_no_gpu=False,
      threads_with_resolve=with_resolve,
      threads_standalone=standalone,
      nice_level=10,
      profile_id="apple_silicon",
      cpu_short=chip_name,
      video_encoder="h264_videotoolbox",
  )


@functools.lru_cache(maxsize=1)
def detect_hardware_profile() -> HardwareProfile:
  """Picks the scheduling profile for the current Mac.

  Apple Silicon gets Metal GPU acceleration and more threads; every other host
  keeps the conservative Intel iMac 2017 profile (CPU-only, 2 GB VRAM safe).
  Set AUTOSUBS_HW_PROFILE=intel_imac2017|apple_silicon to force one.
  """
  forced = os.environ.get("AUTOSUBS_HW_PROFILE", "").strip().lower()
  is_apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
  if forced == "intel_imac2017" or (not forced and not is_apple_silicon):
    return IMAC_2017_PROFILE

  chip = _sysctl("machdep.cpu.brand_string") or "Apple Silicon"
  total = _sysctl_int("hw.physicalcpu", os.cpu_count() or 8)
  perf = _sysctl_int("hw.perflevel0.physicalcpu", total)
  ram_gb = int(round(_sysctl_int("hw.memsize", 16 * 1024**3) / 1024**3))
  return build_apple_silicon_profile(chip, perf, total, ram_gb)


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
        label="Large-v3-Turbo Q8_0 (Recomendado)",
        filename="ggml-large-v3-turbo-q8_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-large-v3-turbo-q8_0.bin"
        ),
        size_mb=834,
        ram_mb=1550,
        spanish_wer="~2.1% WER",
        speed_note=(
            "8-bit casi sin pérdida (~1.5 GB RAM): máxima fidelidad en español"
            " y anglicismos con velocidad Turbo."
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
            " jerga bilingüe compleja; lento en Intel, fluido con Metal."
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
            "4 capas decoder ligeras: excelente velocidad y muy buena"
            " precisión general en español."
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
            "Muy rápido; útil para borradores largos o clips con audio"
            " limpio."
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
  profile = detect_hardware_profile()
  recommended_threads = (
      profile.threads_with_resolve
      if resolve_running
      else profile.threads_standalone
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
      "hardware_profile": asdict(profile),
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
    force_no_gpu: bool | None = None,
    fast_beam: bool = False,
    nice_level: int = IMAC_2017_PROFILE.nice_level,
    carry_initial_prompt: bool | None = None,
) -> list[str]:
  """Builds the whisper-cli invocation tuned for the detected hardware.

  `force_no_gpu=None` follows the profile: CPU-only (-ng) on the Intel iMac to
  protect its 2 GB Polaris, Metal GPU on Apple Silicon.
  """
  profile = detect_hardware_profile()
  if force_no_gpu is None:
    force_no_gpu = profile.force_no_gpu
  if threads is None:
    threads = (
        profile.threads_with_resolve
        if is_resolve_running()
        else profile.threads_standalone
    )
  effective_threads = max(1, min(int(threads), _MAX_WHISPER_THREADS))

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


def ffmpeg_supports_filter(ffmpeg_bin: str, filter_name: str) -> bool:
  """Checks whether the local FFmpeg build ships a filter (e.g. libass 'ass')."""
  try:
    res = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3.0,
        check=False,
    )
    return any(
        line.split()[1:2] == [filter_name] for line in res.stdout.splitlines()
    )
  except (OSError, subprocess.SubprocessError):
    return False


def find_burnin_ffmpeg() -> str | None:
  """Returns an FFmpeg with libass for subtitle burn-in, if one is installed.

  Homebrew's slim `ffmpeg` formula ships without libass; the keg-only
  `ffmpeg-full` formula includes it.
  """
  candidates = [
      "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
      "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
  ]
  default_bin = find_binary(["ffmpeg"])
  if default_bin:
    candidates.append(default_bin)
  for cand in candidates:
    if Path(cand).is_file() and os.access(cand, os.X_OK):
      if ffmpeg_supports_filter(cand, "ass"):
        return cand
  return None


def ffmpeg_supports_encoder(ffmpeg_bin: str, encoder: str) -> bool:
  """Checks whether the local FFmpeg build ships a given video encoder."""
  try:
    res = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3.0,
        check=False,
    )
    return any(
        line.split()[1:2] == [encoder] for line in res.stdout.splitlines()
    )
  except (OSError, subprocess.SubprocessError):
    return False


def build_ffmpeg_burnin_command(
    ffmpeg_bin: str,
    input_video_path: str,
    ass_path: str,
    output_mp4_path: str,
    threads: int = 3,
    nice_level: int = IMAC_2017_PROFILE.nice_level,
    fonts_dir: str | None = None,
    video_encoder: str = "libx264",
) -> list[str]:
  """Builds the FFmpeg subtitle burn-in command.

  `libx264` runs purely on CPU (0 MB Polaris VRAM); `h264_videotoolbox` uses
  the Apple Silicon hardware media engine.
  """
  effective_threads = max(1, min(int(threads), _MAX_WHISPER_THREADS))
  cmd: list[str] = []
  nice_bin = shutil.which("nice")
  if nice_bin and nice_level > 0 and os.name == "posix":
    cmd.extend([nice_bin, "-n", str(nice_level)])

  escaped_ass = escape_ffmpeg_filter_path(ass_path)
  vf_filter = f"ass='{escaped_ass}'"
  if fonts_dir:
    vf_filter += f":fontsdir='{escape_ffmpeg_filter_path(fonts_dir)}'"
  if video_encoder == "h264_videotoolbox":
    # Constant-quality mode (~CRF 20 equivalent) on the hardware encoder.
    codec_args = ["-c:v", "h264_videotoolbox", "-q:v", "65"]
  else:
    codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
  cmd.extend([
      ffmpeg_bin,
      "-y",
      "-i",
      input_video_path,
      "-vf",
      vf_filter,
      *codec_args,
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
