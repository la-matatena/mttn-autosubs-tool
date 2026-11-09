"""Per-machine performance profile and portable user preferences.

Settings live in the app data folder of each Mac (settings.json), so an Intel
iMac can stay in a safe profile while an Apple Silicon laptop runs at full
speed. Only `preferences` (language, glossary...) is meant to be exported and
shared between machines; `performance` and `benchmarks` are hardware-specific.
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


def recommended_model_id(profile: hardware_imac2017.HardwareProfile) -> str:
  """Default model for a machine before any speed test has been run."""
  if profile.ram_gb and profile.ram_gb < 12:
    return "turbo-q5"
  return "turbo-q8"


def resolve_performance(
    profile: hardware_imac2017.HardwareProfile,
    performance: dict[str, Any],
    resolve_running: bool = False,
) -> dict[str, Any]:
  """Turns a performance mode into concrete whisper/ffmpeg parameters."""
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
    raw_t = performance.get("threads")
    threads = int(raw_t) if raw_t else (resolve_t if resolve_running else max_t)
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
      "threads": max(1, min(int(threads), 8)),
      "force_no_gpu": force_no_gpu,
      "fast_beam": fast_beam,
      "model_id": model_id,
      "enhance_voice": bool(performance.get("enhance_voice", True)),
  }


def recommend_from_benchmark(model_id: str, realtime_factor: float) -> dict[str, str]:
  """Suggests a model given how many times faster than real time one ran."""
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


class MachineSettings:
  """Thread-safe JSON store for this Mac's settings."""

  def __init__(self, app_dir: Path) -> None:
    self.path = Path(app_dir) / "settings.json"
    self._lock = threading.Lock()
    self._data = self._load()

  def _load(self) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULT_SETTINGS)
    try:
      raw = json.loads(self.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      return data
    if isinstance(raw, dict):
      for section in ("performance", "preferences", "benchmarks"):
        if isinstance(raw.get(section), dict):
          data[section].update(raw[section])
    return data

  def _save(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(self.path)

  def snapshot(self) -> dict[str, Any]:
    with self._lock:
      return copy.deepcopy(self._data)

  def update(self, patch: dict[str, Any]) -> dict[str, Any]:
    """Merges `performance` / `preferences` patches and persists them."""
    with self._lock:
      perf_patch = patch.get("performance")
      if isinstance(perf_patch, dict):
        perf = self._data["performance"]
        for key in DEFAULT_SETTINGS["performance"]:
          if key in perf_patch:
            perf[key] = perf_patch[key]
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
    with self._lock:
      self._data["performance"] = copy.deepcopy(DEFAULT_SETTINGS["performance"])
      self._save()
      return copy.deepcopy(self._data)

  def record_benchmark(self, model_id: str, result: dict[str, Any]) -> None:
    with self._lock:
      accel = "cpu" if result.get("force_no_gpu") else "gpu"
      self._data["benchmarks"][f"{model_id}:{accel}"] = {
          **result,
          "at": int(time.time()),
      }
      self._save()

  def export_preferences(self) -> dict[str, Any]:
    with self._lock:
      return {
          "app": "MTTN AutoSubs Tool",
          "kind": "preferences",
          "version": SETTINGS_VERSION,
          "preferences": copy.deepcopy(self._data["preferences"]),
      }

  def import_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
    prefs = payload.get("preferences") if isinstance(payload, dict) else None
    if not isinstance(prefs, dict):
      raise ValueError("El archivo no contiene preferencias de AutoSubs.")
    return self.update({"preferences": prefs})
