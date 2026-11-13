"""Hardware governor and whisper.cpp profiles (Intel iMac 2017 / Apple Silicon).

The tool was designed for a 21.5" Retina 4K iMac (2017): 4-core i5 without
Hyper-Threading and a 2 GB AMD Radeon Polaris GPU, usually running DaVinci
Resolve at the same time. This module decides how hard transcription and
burn-in may push such a machine, and scales up on Apple Silicon:

  * Intel Macs: whisper.cpp runs CPU-only (`-ng`), because its Metal backend
    is unreliable on AMD/Intel GPUs and the 2 GB of VRAM is needed by
    Resolve; burn-in uses libx264 (pure CPU, 0 MB VRAM).
  * Apple Silicon: whisper.cpp uses the Metal GPU (unified memory) and
    burn-in uses h264_videotoolbox (hardware media engine).
  * Thread budgets: `threads_with_resolve` leaves cores free for Resolve
    playback, `threads_standalone` uses the whole CPU; all external tools
    run under `nice -n 10` so the UI and Resolve keep priority.

It also owns the GGML model catalog, app/Resolve folder locations, binary
discovery (Homebrew Intel and Apple Silicon prefixes), and builders for the
whisper-cli and ffmpeg command lines. Capability probes (`--help`,
`-filters`, `-encoders`) are cached per process because spawning the
binaries is slow on the iMac.

Main entry points: detect_hardware_profile, detect_environment,
build_whisper_command, build_ffmpeg_burnin_command, MODEL_CATALOG.
Set AUTOSUBS_HW_PROFILE to force a profile (see detect_hardware_profile).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import functools
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

# --- Hardware profiles ---


@dataclass(frozen=True)
class HardwareProfile:
  """Target workstation profile for CPU/VRAM scheduling.

  Attributes:
    name: Human-readable machine name shown in the UI.
    cpu_arch: "x86_64" or "arm64".
    cpu_description: Longer CPU description (Spanish, UI text).
    physical_cores: Physical CPU cores.
    logical_threads: Logical CPUs (equals cores without Hyper-Threading).
    gpu_name: GPU model name.
    gpu_vram_mb: Dedicated VRAM in MB (0 = unknown or unified memory).
    ram_gb: System RAM in GB.
    os_target: macOS version and architecture string.
    force_no_gpu: True to pass `-ng` (CPU-only) to whisper-cli by default.
    threads_with_resolve: whisper/ffmpeg threads while Resolve is open.
    threads_standalone: Threads when Resolve is closed.
    nice_level: `nice -n` value for spawned tools (higher = lower priority).
    profile_id: "intel_imac2017", "intel" or "apple_silicon".
    cpu_short: Short CPU label for compact UI badges.
    video_encoder: ffmpeg encoder for burn-in ("libx264" or
      "h264_videotoolbox").
  """

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


# Reference machine and fallback for non-macOS hosts. 3 threads with Resolve
# open leaves one of the 4 cores (no Hyper-Threading) for playback; CPU-only
# because the 2 GB Polaris is fully used by Resolve's own GPU work.
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
# Public so machine_settings clamps with the same limit.
MAX_WHISPER_THREADS = 8
# Backward-compatible private alias for older imports.
_MAX_WHISPER_THREADS = MAX_WHISPER_THREADS


def _sysctl(key: str) -> str:
  """Reads a macOS sysctl value, returning '' when unavailable.

  The 1.5 s timeout keeps startup responsive on non-macOS hosts or when the
  key does not exist (e.g. hw.perflevel0.* on Intel).
  """
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
  """Reads an integer sysctl value.

  Args:
    key: sysctl name, e.g. "hw.physicalcpu".
    default: Value used when the key is missing or not an integer.

  Returns:
    The parsed integer, or `default`.
  """
  try:
    return int(_sysctl(key))
  except ValueError:
    return default


def build_apple_silicon_profile(
    chip_name: str, perf_cores: int, total_cores: int, ram_gb: int
) -> HardwareProfile:
  """Builds a Metal-accelerated profile for an Apple Silicon (M1–M4+) Mac.

  Threads are sized on performance cores only: whisper.cpp splits work
  evenly, so a thread landing on a slower efficiency core becomes a
  straggler. With Metal doing most of the work, the CPU budget mainly feeds
  the GPU. Examples: M1 (4P+4E) gives standalone 4, with Resolve 2; M3 Max
  (12P) gives 8 and 6.

  Args:
    chip_name: CPU brand string, e.g. "Apple M2 Pro".
    perf_cores: Performance cores (hw.perflevel0.physicalcpu), 0 if unknown.
    total_cores: All physical cores.
    ram_gb: Unified memory in GB.

  Returns:
    Profile with Metal enabled and h264_videotoolbox burn-in.
  """
  perf = max(1, perf_cores or total_cores or 4)
  standalone = max(2, min(perf, MAX_WHISPER_THREADS))
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
def _detect_gpu() -> tuple[str, int]:
  """Returns (GPU model, dedicated VRAM in MB) via system_profiler, best effort.

  Cached for the process lifetime: system_profiler can take several seconds,
  hence also the generous 8 s timeout.
  """
  try:
    res = subprocess.run(
        ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"],
        capture_output=True,
        text=True,
        timeout=8.0,
        check=False,
    )
    gpus = json.loads(res.stdout or "{}").get("SPDisplaysDataType") or []
  except (OSError, subprocess.SubprocessError, ValueError):
    return "", 0

  def _vram_mb(gpu: dict) -> int:
    """Parses "2 GB" / "1536 MB" style VRAM strings into MB (0 if absent)."""
    raw = str(gpu.get("spdisplays_vram") or gpu.get("_spdisplays_vram") or "")
    match = re.search(r"(\d+)\s*(GB|MB)", raw, re.IGNORECASE)
    if not match:
      return 0
    val = int(match.group(1))
    return val * 1024 if match.group(2).upper() == "GB" else val

  # Prefer the discrete GPU (largest dedicated VRAM) when there are several.
  best = max(gpus, key=_vram_mb, default=None)
  if not best:
    return "", 0
  return str(best.get("sppci_model") or best.get("_name") or ""), _vram_mb(best)


def build_intel_profile(
    cpu_brand: str,
    physical_cores: int,
    logical_threads: int,
    ram_gb: int,
    gpu_name: str = "",
    gpu_vram_mb: int = 0,
) -> HardwareProfile:
  """Builds a CPU-only profile for any Intel Mac from its real specs.

  whisper.cpp's Metal backend is unreliable on Intel/AMD GPUs and competes with
  Resolve for scarce VRAM, so Intel Macs always transcribe on CPU (-ng).
  Threads follow *physical* cores: the decode is bound by the AVX2 units,
  which Hyper-Threading siblings share, so extra logical threads add
  contention rather than speed. Burn-in uses libx264 for the same VRAM
  reason. Example: the 4-core i5 iMac 2017 gives standalone 4, with
  Resolve 3.

  Args:
    cpu_brand: machdep.cpu.brand_string, e.g.
      "Intel(R) Core(TM) i5-7500 CPU @ 3.40GHz".
    physical_cores: Physical cores (hw.physicalcpu).
    logical_threads: Logical CPUs (hw.logicalcpu).
    ram_gb: System RAM in GB.
    gpu_name: GPU model from system_profiler, "" if unknown.
    gpu_vram_mb: Dedicated VRAM in MB, 0 if unknown.

  Returns:
    Profile with force_no_gpu=True and libx264 burn-in.
  """
  cores = max(1, physical_cores or 4)
  standalone = max(2, min(cores, MAX_WHISPER_THREADS))
  # Keep one core free for Resolve playback when it is open.
  with_resolve = max(2, min(standalone, cores - 1))
  # Strip trademark marks, "CPU" and the "@ 3.40GHz" clock suffix.
  short = re.sub(r"\((R|TM)\)|CPU|@.*$", "", cpu_brand).strip()
  short = re.sub(r"\s+", " ", short) or "CPU Intel"
  match = re.search(r"(Core\(TM\)\s*|Core\s+)(i\d|Xeon|m\d)", cpu_brand)
  cpu_short = f"Intel Core {match.group(2)}" if match else short
  return HardwareProfile(
      name=f"Mac Intel ({cpu_short})",
      cpu_arch="x86_64",
      cpu_description=(
          f"{short} ({cores} núcleos, {logical_threads or cores} hilos)"
      ),
      physical_cores=cores,
      logical_threads=logical_threads or cores,
      gpu_name=gpu_name or "GPU integrada",
      gpu_vram_mb=gpu_vram_mb,
      ram_gb=ram_gb,
      os_target=f"macOS {platform.mac_ver()[0] or ''} (x86_64)".strip(),
      force_no_gpu=True,
      threads_with_resolve=with_resolve,
      threads_standalone=standalone,
      nice_level=10,
      profile_id="intel",
      cpu_short=cpu_short,
      video_encoder="libx264",
  )


@functools.lru_cache(maxsize=1)
def detect_hardware_profile() -> HardwareProfile:
  """Picks the scheduling profile for the current Mac from its real hardware.

  Apple Silicon gets Metal GPU acceleration; Intel Macs get a CPU-only profile
  sized to their cores. Set AUTOSUBS_HW_PROFILE=intel_imac2017|apple_silicon
  to force one (useful to simulate the 2017 iMac on another machine).

  A real 2017 iMac is detected through the generic Intel path (same thread
  numbers); IMAC_2017_PROFILE itself is returned only when forced, on
  non-macOS hosts (dev/CI) or when the CPU brand cannot be read. The result
  is cached per process, so changing the env var needs a server restart.

  Returns:
    The HardwareProfile used for all scheduling decisions.
  """
  forced = os.environ.get("AUTOSUBS_HW_PROFILE", "").strip().lower()
  if forced == "intel_imac2017":
    return IMAC_2017_PROFILE
  is_apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
  total = _sysctl_int("hw.physicalcpu", os.cpu_count() or 4)
  # hw.memsize is in bytes; the default assumes 16 GB when sysctl fails.
  ram_gb = int(round(_sysctl_int("hw.memsize", 16 * 1024**3) / 1024**3))

  if forced == "apple_silicon" or is_apple_silicon:
    chip = _sysctl("machdep.cpu.brand_string") or "Apple Silicon"
    # perflevel0 = performance cluster (P-cores) on Apple Silicon.
    perf = _sysctl_int("hw.perflevel0.physicalcpu", total)
    return build_apple_silicon_profile(chip, perf, total, ram_gb)

  if sys.platform != "darwin":
    return IMAC_2017_PROFILE
  brand = _sysctl("machdep.cpu.brand_string")
  if not brand:
    return IMAC_2017_PROFILE
  gpu_name, vram = _detect_gpu()
  return build_intel_profile(
      brand,
      total,
      _sysctl_int("hw.logicalcpu", total),
      ram_gb,
      gpu_name,
      vram,
  )


# --- Whisper model catalog ---


@dataclass(frozen=True)
class ModelSpec:
  """Downloadable GGML Whisper model entry optimized for Spanish on CPU.

  Attributes:
    model_id: Catalog key (e.g. "turbo-q8"), stored in settings.json.
    label: UI label (Spanish).
    filename: File name under `<app_data_dir>/models`.
    url: Hugging Face download URL (ggerganov/whisper.cpp repo).
    size_mb: Approximate download size in MB.
    ram_mb: Approximate RAM used while transcribing, in MB.
    spanish_wer: Approximate Spanish word error rate, display only.
    speed_note: UI hint about speed/accuracy (Spanish).
    recommended: Whether the UI highlights this model.
  """

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
            "Muy rápido; útil para borradores largos o clips con audio limpio."
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

# --- Capability caches ---
#
# Process-lifetime memo of binary capability probes, keyed by
# (binary path, flag/filter/encoder name). Spawning whisper-cli/ffmpeg costs
# hundreds of ms on the iMac, and the answers only change if the user
# reinstalls a binary (restart the server then). Only successful probes are
# cached; failures (timeouts, missing binary) are retried next time. Plain
# dicts without a lock: a race at worst runs the same probe twice.
_WHISPER_FLAG_CACHE: dict[tuple[str, str], bool] = {}
_FFMPEG_FILTER_CACHE: dict[tuple[str, str], bool] = {}
_FFMPEG_ENCODER_CACHE: dict[tuple[str, str], bool] = {}


# --- Paths, binaries and environment detection ---


def get_app_data_dir(create: bool = True) -> Path:
  """Returns the AutoSubs ES application data directory.

  ~/Library/Application Support/AutoSubsES on macOS (the Lua scripts in
  Resolve hard-code this path for the bridge mailbox),
  ~/.local/share/autosubs_es elsewhere, or AUTOSUBS_ES_HOME if set.

  Args:
    create: Also create the models, fonts, bridge/inbox, bridge/outbox and
      cache subfolders.

  Returns:
    The base folder path.
  """
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
  """Returns the DaVinci Resolve Fusion user directory on macOS or Linux.

  This is where install_imac.sh puts the Lua scripts (Scripts/Utility) and
  Text+ templates (Templates/Edit/Titles), and where Fusion.prefs lives.
  AUTOSUBS_RESOLVE_FUSION_DIR overrides it (tests).
  """
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
  """Finds an executable checking Homebrew Intel, Apple Silicon, and PATH.

  The explicit folders matter because apps launched from Finder (the
  .command launcher) get a minimal PATH without Homebrew. /usr/local is the
  Intel Homebrew prefix, /opt/homebrew the Apple Silicon one.

  Args:
    candidates: Executable names in preference order; each name is looked
      up in every folder and then on PATH before trying the next name.

  Returns:
    Absolute path of the first match, or None.
  """
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
  """Checks whether DaVinci Resolve is currently running without blocking.

  Uses `pgrep -x` (exact process name) with a 1.5 s timeout per pattern.
  A failing pattern (timeout, OSError) does not stop the search: the next
  pattern is still tried. Returns False when pgrep is unavailable or no
  pattern matches, so the caller falls back to the more generous standalone
  thread budget.
  """
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
      continue
  return False


def get_installed_models(models_dir: Path | None = None) -> dict[str, dict]:
  """Inspects downloaded GGML model files and returns catalog metadata.

  A model counts as installed only above 1 MiB, so an empty or failed
  download is not mistaken for a real model. `<filename>.part` files are
  in-progress (resumable) downloads.

  Args:
    models_dir: Models folder; defaults to `<app_data_dir>/models`.

  Returns:
    {model_id: ModelSpec fields + installed, path, disk_bytes,
    partial_bytes} for every MODEL_CATALOG entry.
  """
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
  """Checks whether a whisper-cli binary supports a given CLI flag.

  Parses `--help` output (stdout and stderr, since builds differ). Used to
  enable newer flags such as --carry-initial-prompt only when available.
  Results are memoized in _WHISPER_FLAG_CACHE.

  Args:
    whisper_bin: Path to the whisper-cli executable.
    flag: Flag text to look for, e.g. "--carry-initial-prompt".

  Returns:
    True if the flag appears in the help text; False otherwise or on error.
  """
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
  """Collects host hardware, binary paths, models, and Resolve state.

  Backs several server endpoints (status, transcription, burn-in). Side
  effect: creates the app data folders.

  Returns:
    JSON-ready dict: app_data_dir, resolve_fusion_dir, whisper_cli_path,
    ffmpeg_path, resolve_running, recommended_threads, default_model_id,
    models, hardware_profile and host_runtime.
  """
  app_dir = get_app_data_dir(create=True)
  # "main" is the binary name of older whisper.cpp builds.
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

  Args:
    whisper_bin: Path to whisper-cli.
    model_path: GGML model file.
    wav_path: 16 kHz mono PCM16 input WAV.
    output_prefix: Output path without extension; whisper writes
      `<prefix>.json`.
    prompt: Initial prompt (glossary / style hint); empty to omit.
    threads: CPU threads; None picks the profile budget depending on
      whether Resolve is running. Clamped to 1..MAX_WHISPER_THREADS.
    force_no_gpu: True adds -ng; None follows the profile.
    fast_beam: Beam size 2 instead of 5 (faster, slightly less accurate).
    nice_level: Scheduling niceness; 0 disables the `nice` wrapper.
    carry_initial_prompt: Keep the prompt for every 30 s window; None
      enables it when the binary supports the flag.

  Returns:
    argv list ready for subprocess (optionally prefixed by nice -n N).
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
  effective_threads = max(1, min(int(threads), MAX_WHISPER_THREADS))

  cmd: list[str] = []
  nice_bin = shutil.which("nice")
  if nice_bin and nice_level > 0 and os.name == "posix":
    cmd.extend([nice_bin, "-n", str(nice_level)])

  # -l es: force Spanish (no auto-detect). -ojf: full JSON with per-token
  # timestamps/probabilities. -sow: split on word boundaries.
  # -et 2.4 / -lpt -1.0: whisper's default entropy / log-prob thresholds,
  # pinned explicitly. -nf: no temperature fallback (no re-decoding of
  # doubtful windows at higher temperature), keeping run time bounded and
  # output deterministic on slow CPUs.
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
      # -mc 64 caps the decoder text context at 64 tokens so the carried
      # prompt is not crowded out and hallucinated loops cannot snowball.
      cmd.extend(["--carry-initial-prompt", "-mc", "64"])

  return cmd


# --- FFmpeg capability probes and burn-in ---


# Characters escaped with a backslash in a filter option value. FFmpeg parses
# a -vf string twice with av_get_token() (ffmpeg-filters "Notes on filtergraph
# escaping"): level 1 is the option value, where "\" and "'" are always
# special and ":" separates options; level 2 is the filtergraph, where
# "[],;" are also special. A backslash before any character yields that
# character, so escaping the level-2 set here too is harmless.
_FFMPEG_OPTION_SPECIALS = frozenset("\\':[],;")


def escape_ffmpeg_filter_path(path_str: str) -> str:
  """Escapes a path as one FFmpeg filter option value (escaping level 1).

  Backslash-escapes the characters in _FFMPEG_OPTION_SPECIALS plus a leading
  or trailing whitespace character (av_get_token trims unescaped edge
  whitespace). The result is still raw for the filtergraph level: wrap it
  with quote_ffmpeg_filter_value() (which calls this) rather than pasting it
  between plain single quotes, because a "'" in the path would end the
  quoted section there (FFmpeg has no "\\'" inside single quotes).

  Args:
    path_str: Filesystem path (may contain spaces or accents).

  Returns:
    The level-1 escaped path.
  """
  last = len(path_str) - 1
  out: list[str] = []
  for idx, ch in enumerate(path_str):
    if ch in _FFMPEG_OPTION_SPECIALS or (ch.isspace() and idx in (0, last)):
      out.append("\\")
    out.append(ch)
  return "".join(out)


def quote_ffmpeg_filter_value(value: str) -> str:
  """Returns a filtergraph-safe token for one filter option value.

  Level 1 is escape_ffmpeg_filter_path(); level 2 wraps that in single
  quotes, inside which FFmpeg copies everything literally (including the
  level-1 backslashes). A "'" cannot appear inside quotes, so each one is
  written as close-quote, escaped quote, reopen-quote: "'\\''". Example:
  "/a/it's:b" -> "'/a/it\\'\\''s\\:b'".

  Args:
    value: Raw option value, typically a file or folder path.

  Returns:
    Token ready to follow "name=" in a -vf / -filter_complex string.
  """
  level1 = escape_ffmpeg_filter_path(value)
  return "'" + level1.replace("'", "'\\''") + "'"


def ffmpeg_supports_filter(ffmpeg_bin: str, filter_name: str) -> bool:
  """Checks whether the local FFmpeg build ships a filter (e.g. libass 'ass').

  `ffmpeg -filters` lists one filter per line as "<flags> <name> <pads>
  <description>", so the name is the second whitespace-separated column.
  Results are memoized in _FFMPEG_FILTER_CACHE.

  Args:
    ffmpeg_bin: Path to the ffmpeg executable.
    filter_name: Filter name, e.g. "ass" or "subtitles".

  Returns:
    True if listed; False otherwise or on error.
  """
  key = (ffmpeg_bin, filter_name)
  if key in _FFMPEG_FILTER_CACHE:
    return _FFMPEG_FILTER_CACHE[key]
  try:
    res = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3.0,
        check=False,
    )
    supported = any(
        line.split()[1:2] == [filter_name] for line in res.stdout.splitlines()
    )
    _FFMPEG_FILTER_CACHE[key] = supported
    return supported
  except (OSError, subprocess.SubprocessError):
    return False


def find_burnin_ffmpeg() -> str | None:
  """Returns an FFmpeg with libass for subtitle burn-in, if one is installed.

  Homebrew's slim `ffmpeg` formula ships without libass; the keg-only
  `ffmpeg-full` formula includes it.

  Returns:
    Path of the first ffmpeg-full (Apple Silicon, then Intel prefix) or
    default ffmpeg that has the "ass" filter, else None.
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
  """Checks whether the local FFmpeg build ships a given video encoder.

  Parses `ffmpeg -encoders` the same way as ffmpeg_supports_filter (name in
  the second column). Used to confirm h264_videotoolbox before choosing it.
  Results are memoized in _FFMPEG_ENCODER_CACHE.

  Args:
    ffmpeg_bin: Path to the ffmpeg executable.
    encoder: Encoder name, e.g. "h264_videotoolbox".

  Returns:
    True if listed; False otherwise or on error.
  """
  key = (ffmpeg_bin, encoder)
  if key in _FFMPEG_ENCODER_CACHE:
    return _FFMPEG_ENCODER_CACHE[key]
  try:
    res = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3.0,
        check=False,
    )
    supported = any(
        line.split()[1:2] == [encoder] for line in res.stdout.splitlines()
    )
    _FFMPEG_ENCODER_CACHE[key] = supported
    return supported
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

  Output is H.264 yuv420p (widest player compatibility) with AAC 192 kbps
  audio and +faststart (moov atom first, so the MP4 starts playing before it
  is fully downloaded).

  Args:
    ffmpeg_bin: ffmpeg with libass (see find_burnin_ffmpeg).
    input_video_path: Source video.
    ass_path: ASS subtitle file to burn in.
    output_mp4_path: Destination MP4 (overwritten, -y).
    threads: Encoder threads, clamped to 1..MAX_WHISPER_THREADS.
    nice_level: Scheduling niceness; 0 disables the `nice` wrapper.
    fonts_dir: Extra folder libass searches for fonts (custom uploads).
    video_encoder: "libx264" or "h264_videotoolbox".

  Returns:
    argv list ready for subprocess.
  """
  effective_threads = max(1, min(int(threads), MAX_WHISPER_THREADS))
  cmd: list[str] = []
  nice_bin = shutil.which("nice")
  if nice_bin and nice_level > 0 and os.name == "posix":
    cmd.extend([nice_bin, "-n", str(nice_level)])

  # quote_ffmpeg_filter_value handles apostrophes, ":", ",", brackets and
  # backslashes in both paths (see its docstring for the two levels).
  vf_filter = f"ass={quote_ffmpeg_filter_value(ass_path)}"
  if fonts_dir:
    vf_filter += f":fontsdir={quote_ffmpeg_filter_value(fonts_dir)}"
  if video_encoder == "h264_videotoolbox":
    # Constant-quality mode (~CRF 20 equivalent) on the hardware encoder.
    codec_args = ["-c:v", "h264_videotoolbox", "-q:v", "65"]
  else:
    # "veryfast" keeps 4K burn-in tolerable on the 4-core i5; CRF 20 is
    # visually transparent for subtitle overlays.
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
