"""Per-machine performance profile and portable user preferences.

Settings live in the app data folder of each Mac (settings.json), so an Intel
iMac can stay in a safe profile while an Apple Silicon laptop runs at full
speed. Only `preferences` (language, glossary...) is meant to be exported and
shared between machines; `performance` and `benchmarks` are hardware-specific.

Persistence format (`<app_data_dir>/settings.json`, UTF-8, 2-space indent):

  {
    "version": 1,
    "performance": {"mode": "auto|safe|max|custom", "threads": int|null,
                    "force_no_gpu": bool|null, "fast_beam": bool,
                    "model_id": str|null, "enhance_voice": bool},
    "preferences": {<PREFERENCE_KEYS subset>},
    "benchmarks": {"<model_id>:<cpu|gpu>": {...result, "at": unix_seconds}}
  }

Writes are atomic (temp file + rename), so a crash mid-save never leaves a
truncated file. Unknown or corrupt files fall back to DEFAULT_SETTINGS; values
are validated on load and on update (see _coerce_performance_value).

Main entry points: `MachineSettings` (store used by autosubs_server.py) and
`resolve_performance` (turns the stored mode into whisper/ffmpeg knobs).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time
from typing import Any

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
except ImportError:
  import hardware_imac2017  # type: ignore[no-redef]

SETTINGS_VERSION = 1
# auto: follow the profile, fewer threads while Resolve is open.
# safe: low CPU load, smaller beam. max: all profile threads.
# custom: user-chosen threads / GPU / beam.
PERFORMANCE_MODES = ("auto", "safe", "max", "custom")

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    "performance": {
        "mode": "auto",
        # Used only in "custom" mode (None = follow the profile).
        "threads": None,
        "force_no_gpu": None,
        "fast_beam": False,
        # Applies in every mode (None = recommended for this Mac).
        "model_id": None,
        "enhance_voice": True,
    },
    "preferences": {},
    "benchmarks": {},
}

# Portable preference keys accepted on import (anything else is dropped).
PREFERENCE_KEYS = frozenset({
    "dialect",
    "domain",
    "preset_id",
    "max_cpl",
    "max_lines",
    "pause_split_ms",
    "fix_inverted_punctuation",
    "remove_muletillas",
    "uppercase",
    "custom_glossary",
    "karaoke_enabled",
    "custom_style_presets",
})


# --- Value validation ---

# Returned by _coerce_performance_value for a value that must be ignored.
_INVALID = object()


def _coerce_bool(value: Any) -> Any:
  """Accepts JSON booleans plus 0/1 and "true"/"false" style strings.

  Args:
    value: Raw value from a request body or settings.json.

  Returns:
    The bool, or _INVALID for anything else.
  """
  if isinstance(value, bool):
    return value
  if isinstance(value, int) and value in (0, 1):
    return bool(value)
  if isinstance(value, str):
    lowered = value.strip().lower()
    if lowered in ("true", "1"):
      return True
    if lowered in ("false", "0"):
      return False
  return _INVALID


def _coerce_performance_value(key: str, value: Any) -> Any:
  """Validates and normalizes one `performance` value before it is stored.

  resolve_performance() calls int()/bool() on these values, so a bad type
  (e.g. threads="abc" from a hand-edited file or a buggy client) would make
  every later request fail. Rules per key:
    * mode: one of PERFORMANCE_MODES; anything else becomes "auto".
    * threads: None, or a non-negative int (ints in strings and integral
      floats accepted; bool rejected), capped at MAX_WHISPER_THREADS.
      0 keeps its "not set" meaning.
    * force_no_gpu: None or a bool-like value (see _coerce_bool).
    * fast_beam, enhance_voice: bool-like value.
    * model_id: None or a string; blank strings become None. Unknown ids are
      kept (resolve_performance falls back to the recommended model).

  Args:
    key: Key of DEFAULT_SETTINGS["performance"].
    value: Raw value.

  Returns:
    The normalized value, or _INVALID if it must be ignored.
  """
  if key == "mode":
    return value if value in PERFORMANCE_MODES else "auto"
  if key == "threads":
    if value is None:
      return None
    if isinstance(value, bool):
      return _INVALID
    if isinstance(value, float) and value.is_integer():
      value = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
      value = int(value.strip())
    if not isinstance(value, int) or value < 0:
      return _INVALID
    return min(value, hardware_imac2017.MAX_WHISPER_THREADS)
  if key == "force_no_gpu":
    return None if value is None else _coerce_bool(value)
  if key in ("fast_beam", "enhance_voice"):
    return _coerce_bool(value)
  if key == "model_id":
    if value is None:
      return None
    if isinstance(value, str):
      return value.strip() or None
    return _INVALID
  return _INVALID


def _sanitize_performance(raw: dict[str, Any]) -> dict[str, Any]:
  """Keeps only known performance keys whose values pass validation.

  Args:
    raw: Performance patch or loaded section.

  Returns:
    {key: normalized value} for every valid known key present in `raw`.
  """
  clean: dict[str, Any] = {}
  for key in DEFAULT_SETTINGS["performance"]:
    if key in raw:
      value = _coerce_performance_value(key, raw[key])
      if value is not _INVALID:
        clean[key] = value
  return clean


# --- Performance resolution ---


def recommended_model_id(profile: hardware_imac2017.HardwareProfile) -> str:
  """Default model for a machine before any speed test has been run.

  Macs with less than 12 GB RAM get the lighter Turbo Q5 (~1.1 GB working
  set) so Resolve keeps enough memory; others get Turbo Q8 (~1.5 GB).

  Args:
    profile: Detected hardware profile.

  Returns:
    A MODEL_CATALOG key ("turbo-q5" or "turbo-q8").
  """
  if profile.ram_gb and profile.ram_gb < 12:
    return "turbo-q5"
  return "turbo-q8"


def resolve_performance(
    profile: hardware_imac2017.HardwareProfile,
    performance: dict[str, Any],
    resolve_running: bool = False,
) -> dict[str, Any]:
  """Turns a performance mode into concrete whisper/ffmpeg parameters.

  Thread budgets come from the hardware profile: `threads_standalone` when
  the Mac is free, `threads_with_resolve` to leave cores for Resolve
  playback. GPU use defaults to the profile (CPU-only on Intel Macs, Metal
  on Apple Silicon) except in custom mode.

  Args:
    profile: Detected hardware profile.
    performance: The stored `performance` section; unknown modes and
      unknown model ids fall back to auto / the recommended model.
    resolve_running: Whether DaVinci Resolve is open right now.

  Returns:
    Dict with mode, threads (clamped to 1..MAX_WHISPER_THREADS), force_no_gpu,
    fast_beam (beam size 2 instead of 5), model_id and enhance_voice.
  """
  mode = performance.get("mode", "auto")
  if mode not in PERFORMANCE_MODES:
    mode = "auto"
  max_t = profile.threads_standalone
  resolve_t = profile.threads_with_resolve

  if mode == "safe":
    # Low CPU load: keeps the machine (and Resolve) responsive while it works.
    threads = max(2, min(resolve_t, max_t // 2))
    force_no_gpu = profile.force_no_gpu
    fast_beam = True
  elif mode == "max":
    threads = max_t
    force_no_gpu = profile.force_no_gpu
    fast_beam = False
  elif mode == "custom":
    # Coerced again here so hand-edited files or direct callers cannot make
    # int() raise; invalid values behave like "not set".
    raw_t = _coerce_performance_value("threads", performance.get("threads"))
    if raw_t is _INVALID:
      raw_t = None
    # 0/None means "not set": follow auto's Resolve-aware choice.
    threads = raw_t if raw_t else (resolve_t if resolve_running else max_t)
    raw_gpu = performance.get("force_no_gpu")
    force_no_gpu = profile.force_no_gpu if raw_gpu is None else bool(raw_gpu)
    fast_beam = bool(performance.get("fast_beam", False))
  else:  # auto
    threads = resolve_t if resolve_running else max_t
    force_no_gpu = profile.force_no_gpu
    fast_beam = False

  model_id = performance.get("model_id") or recommended_model_id(profile)
  if model_id not in hardware_imac2017.MODEL_CATALOG:
    model_id = recommended_model_id(profile)
  return {
      "mode": mode,
      "threads": max(
          1, min(int(threads), hardware_imac2017.MAX_WHISPER_THREADS)
      ),
      "force_no_gpu": force_no_gpu,
      "fast_beam": fast_beam,
      "model_id": model_id,
      "enhance_voice": bool(performance.get("enhance_voice", True)),
  }


def recommend_from_benchmark(
    model_id: str, realtime_factor: float
) -> dict[str, str]:
  """Suggests a model given how many times faster than real time one ran.

  Heuristic thresholds: below 1.2x real time the transcription cannot keep
  up with the audio, so step one model lighter; at 12x or more with a Turbo
  model there is enough headroom for full Large-v3. Unknown ids are treated
  as "turbo-q8" (index 3) for the step-down.

  Args:
    model_id: Model that was benchmarked.
    realtime_factor: Audio duration / processing time (2.0 = twice as fast
      as real time).

  Returns:
    {"model_id": suggested id, "note": Spanish explanation for the UI}.
  """
  # Ordered lightest -> heaviest.
  order = ["base-q8", "small-q8", "turbo-q5", "turbo-q8", "large-v3-q5"]
  idx = order.index(model_id) if model_id in order else 3
  if realtime_factor < 1.2 and idx > 0:
    suggestion = order[idx - 1]
    note = "Va más lento que el audio real: usa un modelo más ligero."
  elif realtime_factor >= 12 and model_id in ("turbo-q8", "turbo-q5"):
    suggestion = "large-v3-q5"
    note = "Sobra velocidad: Large-v3 da la máxima precisión en esta Mac."
  else:
    suggestion = model_id
    note = "Buen equilibrio entre velocidad y precisión para esta Mac."
  return {"model_id": suggestion, "note": note}


# --- Settings store ---


class MachineSettings:
  """Thread-safe JSON store for this Mac's settings.

  Every public method takes `_lock`, works on the in-memory copy and, for
  mutations, saves to disk before releasing it. Callers always receive deep
  copies, so they cannot mutate the internal state by accident.

  Attributes:
    path: Location of settings.json inside the app data folder.
  """

  def __init__(self, app_dir: Path) -> None:
    """Loads settings from `<app_dir>/settings.json` (defaults if absent).

    Args:
      app_dir: AutoSubs ES app data folder.
    """
    self.path = Path(app_dir) / "settings.json"
    self._lock = threading.Lock()
    self._data = self._load()

  def _load(self) -> dict[str, Any]:
    """Reads settings.json merged over DEFAULT_SETTINGS.

    Only the three known sections are merged (shallow, key by key), so a
    file written by an older version gains new default keys. Performance
    values go through the same validation as update() and preferences are
    filtered by PREFERENCE_KEYS, so a hand-edited file cannot break
    resolve_performance(). When "version" is missing or is not
    SETTINGS_VERSION the file's schema is unknown: valid performance and
    preference values are still kept, but cached benchmarks are dropped
    (they are cheap to re-run). A missing or invalid file yields defaults.

    Returns:
      The merged settings dict (always tagged with SETTINGS_VERSION).
    """
    data = copy.deepcopy(DEFAULT_SETTINGS)
    try:
      raw = json.loads(self.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      return data
    if not isinstance(raw, dict):
      return data
    version = raw.get("version")
    known_version = (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == SETTINGS_VERSION
    )
    if isinstance(raw.get("performance"), dict):
      data["performance"].update(_sanitize_performance(raw["performance"]))
    if isinstance(raw.get("preferences"), dict):
      data["preferences"].update(
          {k: v for k, v in raw["preferences"].items() if k in PREFERENCE_KEYS}
      )
    if known_version and isinstance(raw.get("benchmarks"), dict):
      data["benchmarks"].update(
          {k: v for k, v in raw["benchmarks"].items() if isinstance(v, dict)}
      )
    return data

  def _save(self) -> None:
    """Atomically writes the in-memory settings; caller must hold `_lock`.

    Writes `settings.json.tmp` first and renames it over settings.json
    (atomic on the same filesystem). ensure_ascii=False keeps Spanish
    glossary entries readable in the file.
    """
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(self.path)

  def snapshot(self) -> dict[str, Any]:
    """Returns a deep copy of all current settings."""
    with self._lock:
      return copy.deepcopy(self._data)

  def update(self, patch: dict[str, Any]) -> dict[str, Any]:
    """Merges `performance` / `preferences` patches and persists them.

    Only keys present in DEFAULT_SETTINGS["performance"] and in
    PREFERENCE_KEYS are accepted; anything else (including `benchmarks`) is
    ignored. Performance values are validated and coerced with
    _coerce_performance_value(): invalid ones (e.g. threads="abc") are
    ignored and keep their previous value; an invalid mode becomes "auto".

    Args:
      patch: Partial settings, e.g. {"performance": {"mode": "safe"}}.

    Returns:
      Deep copy of the full settings after saving.
    """
    with self._lock:
      perf_patch = patch.get("performance")
      if isinstance(perf_patch, dict):
        perf = self._data["performance"]
        perf.update(_sanitize_performance(perf_patch))
        if perf.get("mode") not in PERFORMANCE_MODES:
          perf["mode"] = "auto"
      prefs_patch = patch.get("preferences")
      if isinstance(prefs_patch, dict):
        self._data["preferences"].update(
            {k: v for k, v in prefs_patch.items() if k in PREFERENCE_KEYS}
        )
      self._save()
      return copy.deepcopy(self._data)

  def reset_performance(self) -> dict[str, Any]:
    """Restores the default performance section and saves it.

    Returns:
      Deep copy of the full settings after saving.
    """
    with self._lock:
      self._data["performance"] = copy.deepcopy(DEFAULT_SETTINGS["performance"])
      self._save()
      return copy.deepcopy(self._data)

  def record_benchmark(self, model_id: str, result: dict[str, Any]) -> None:
    """Stores a speed-test result keyed by model and accelerator.

    The key is "<model_id>:cpu" or "<model_id>:gpu" (from
    result["force_no_gpu"]), so one entry per combination is kept and a new
    run overwrites the previous one. An "at" Unix timestamp (seconds) is
    added.

    Args:
      model_id: Benchmarked model id.
      result: Benchmark metrics as produced by the server.
    """
    with self._lock:
      accel = "cpu" if result.get("force_no_gpu") else "gpu"
      self._data["benchmarks"][f"{model_id}:{accel}"] = {
          **result,
          "at": int(time.time()),
      }
      self._save()

  def export_preferences(self) -> dict[str, Any]:
    """Builds the portable preferences file (no hardware-specific data).

    Returns:
      {"app", "kind": "preferences", "version", "preferences"}; the
      app/kind fields let the UI recognize the file on import.
    """
    with self._lock:
      return {
          "app": "MTTN AutoSubs Tool",
          "kind": "preferences",
          "version": SETTINGS_VERSION,
          "preferences": copy.deepcopy(self._data["preferences"]),
      }

  def import_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Imports preferences exported from another Mac.

    Only the "preferences" object is used (filtered by PREFERENCE_KEYS);
    "app", "kind" and "version" are not checked.

    Args:
      payload: Parsed JSON of an export_preferences() file.

    Returns:
      Deep copy of the full settings after saving.

    Raises:
      ValueError: If the payload has no "preferences" object.
    """
    prefs = payload.get("preferences") if isinstance(payload, dict) else None
    if not isinstance(prefs, dict):
      raise ValueError("El archivo no contiene preferencias de AutoSubs.")
    return self.update({"preferences": prefs})
