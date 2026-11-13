"""Unit and HTTP integration tests for AutoSubs ES.

Run the whole suite with `python3 -B autosubs_es_test.py -v`. Every test is
offline: whisper-cli is replaced by small shell stubs, fonts are synthesized in
memory by `_make_test_sfnt`, and the HTTP suite talks to a real server bound
to an ephemeral loopback port inside a temporary app directory. No Resolve,
ffmpeg, or GGML model downloads are required.
"""

# White-box tests: they deliberately exercise private helpers and state.
# pylint: disable=protected-access

from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time
from typing import Any
import unittest
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
import zipfile

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import autosubs_server
  from google3.experimental.users.joseluiszapata.autosubs_es import exporters
  from google3.experimental.users.joseluiszapata.autosubs_es import font_manager
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
  from google3.experimental.users.joseluiszapata.autosubs_es import machine_settings
  from google3.experimental.users.joseluiszapata.autosubs_es import package_bundle
  from google3.experimental.users.joseluiszapata.autosubs_es import resolve_bridge
  from google3.experimental.users.joseluiszapata.autosubs_es import spanish_nlp
except ImportError:
  import autosubs_server  # type: ignore[no-redef]
  import exporters  # type: ignore[no-redef]
  import font_manager  # type: ignore[no-redef]
  import hardware_imac2017  # type: ignore[no-redef]
  import machine_settings  # type: ignore[no-redef]
  import package_bundle  # type: ignore[no-redef]
  import resolve_bridge  # type: ignore[no-redef]
  import spanish_nlp  # type: ignore[no-redef]


def _make_test_sfnt(
    sfnt_tag: bytes,
    family: str,
    subfamily: str,
    codepoint_ranges: list[tuple[int, int]],
    full_name: str = "",
    ps_name: str = "",
) -> bytes:
  """Builds a minimal valid OpenType/TrueType font with name + cmap Format 4.

  Args:
    sfnt_tag: SFNT version tag: b"OTTO" for CFF OpenType, or
      b"\\x00\\x01\\x00\\x00" for TrueType outlines.
    family: Name ID 1 (font family).
    subfamily: Name ID 2 (style, e.g. "Regular" or a flipbook index "1").
    codepoint_ranges: Inclusive (start, end) Unicode ranges mapped in cmap.
    full_name: Name ID 4; defaults to "<family> <subfamily>".
    ps_name: Name ID 6; defaults to the alphanumeric family + subfamily.

  Returns:
    Raw font bytes with only the "cmap" and "name" tables (checksums zeroed).
  """
  # 1. Build name table (Windows Unicode BMP)
  entries = [
      (1, family),
      (2, subfamily),
      (4, full_name or f"{family} {subfamily}".strip()),
      (6, ps_name or re.sub(r"[^A-Za-z0-9]", "", f"{family}{subfamily}")),
  ]
  str_blobs = bytearray()
  rec_bytes = bytearray()
  for name_id, val in entries:
    encoded = val.encode("utf_16_be")
    rec_bytes.extend(
        struct.pack(
            ">HHHHHH", 3, 1, 0x0409, name_id, len(encoded), len(str_blobs)
        )
    )
    str_blobs.extend(encoded)
  name_table = (
      struct.pack(">HHH", 0, len(entries), 6 + len(rec_bytes))
      + bytes(rec_bytes)
      + bytes(str_blobs)
  )

  # 2. Build cmap Format 4 subtable
  segs = list(codepoint_ranges) + [(0xFFFF, 0xFFFF)]
  seg_count = len(segs)
  end_counts = b"".join(struct.pack(">H", e) for _, e in segs)
  start_counts = b"".join(struct.pack(">H", s) for s, _ in segs)
  deltas = b"".join(struct.pack(">h", 1) for _ in segs)
  ros = b"".join(struct.pack(">H", 0) for _ in segs)
  sub_body = end_counts + struct.pack(">H", 0) + start_counts + deltas + ros
  sub_len = 14 + len(sub_body)
  fmt4_subtable = (
      struct.pack(">HHHHHHH", 4, sub_len, 0, seg_count * 2, 0, 0, 0) + sub_body
  )
  cmap_table = (
      struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + fmt4_subtable
  )

  # 3. Assemble table directory ("cmap", "name")
  tables = [("cmap", cmap_table), ("name", name_table)]
  offset = 12 + len(tables) * 16
  dir_bytes = bytearray()
  payload_bytes = bytearray()
  for tag_str, t_data in tables:
    padded = t_data + (b"\x00" * ((4 - (len(t_data) % 4)) % 4))
    dir_bytes.extend(
        struct.pack(">4sIII", tag_str.encode("ascii"), 0, offset, len(t_data))
    )
    payload_bytes.extend(padded)
    offset += len(padded)
  header = struct.pack(">4sHHHH", sfnt_tag, len(tables), 0, 0, 0)
  return header + bytes(dir_bytes) + bytes(payload_bytes)


class HardwareIMac2017Test(unittest.TestCase):
  """Hardware profiles and whisper-cli / ffmpeg command builders.

  Covers the iMac 2017 Intel profile (CPU-only Whisper, libx264), Apple
  Silicon profiles (Metal, h264_videotoolbox), GGML model discovery, and
  whisper-cli flag probing.
  """

  def test_profile_and_command_flags(self) -> None:
    """iMac 2017 profile forces CPU (-ng) and builds the Whisper flags."""
    prof = hardware_imac2017.IMAC_2017_PROFILE
    self.assertEqual(prof.cpu_arch, "x86_64")
    self.assertEqual(prof.physical_cores, 4)
    self.assertEqual(prof.gpu_vram_mb, 2048)
    self.assertEqual(prof.ram_gb, 32)
    self.assertTrue(prof.force_no_gpu)
    self.assertEqual(prof.threads_with_resolve, 3)

    cmd = hardware_imac2017.build_whisper_command(
        whisper_bin="/usr/local/bin/whisper-cli",
        model_path="/tmp/ggml-large-v3-turbo-q5_0.bin",
        wav_path="/tmp/in.wav",
        output_prefix="/tmp/out",
        prompt="Transcripción en español.",
        threads=3,
        force_no_gpu=True,
        fast_beam=True,
        nice_level=0,
    )
    self.assertEqual(cmd[0], "/usr/local/bin/whisper-cli")
    self.assertIn("-ng", cmd)
    self.assertIn("-ojf", cmd)
    self.assertIn("-sow", cmd)
    self.assertEqual(cmd[cmd.index("-l") + 1], "es")
    self.assertEqual(cmd[cmd.index("-t") + 1], "3")
    self.assertEqual(cmd[cmd.index("-bs") + 1], "2")
    self.assertEqual(
        cmd[cmd.index("--prompt") + 1], "Transcripción en español."
    )

  def test_installed_model_detection(self) -> None:
    """Default model is turbo-q8 unless only another model is installed."""
    with tempfile.TemporaryDirectory() as tmp:
      models_dir = Path(tmp) / "models"
      models_dir.mkdir()
      self.assertEqual(
          hardware_imac2017.choose_default_model_id(models_dir), "turbo-q8"
      )
      self.assertIn("large-v3-q5", hardware_imac2017.MODEL_CATALOG)
      self.assertTrue(hardware_imac2017.MODEL_CATALOG["turbo-q8"].recommended)

      dummy_q5 = models_dir / "ggml-large-v3-turbo-q5_0.bin"
      dummy_q5.write_bytes(b"0" * (2 * 1024 * 1024))
      models = hardware_imac2017.get_installed_models(models_dir)
      self.assertTrue(models["turbo-q5"]["installed"])
      self.assertFalse(models["turbo-q8"]["installed"])
      self.assertEqual(
          hardware_imac2017.choose_default_model_id(models_dir), "turbo-q5"
      )

      dummy_q8 = models_dir / "ggml-large-v3-turbo-q8_0.bin"
      dummy_q8.write_bytes(b"1" * (2 * 1024 * 1024))
      self.assertEqual(
          hardware_imac2017.choose_default_model_id(models_dir), "turbo-q8"
      )

  def test_carry_initial_prompt_flag_and_ffmpeg_burnin_command(self) -> None:
    """Probed --carry-initial-prompt adds -mc; burn-in escapes ':' and '['."""
    with tempfile.TemporaryDirectory() as tmp:
      fake_whisper = Path(tmp) / "whisper-cli"
      fake_whisper.write_text(
          "#!/bin/sh\necho '  --carry-initial-prompt  carry prompt'\n",
          encoding="utf-8",
      )
      fake_whisper.chmod(0o755)
      self.assertTrue(
          hardware_imac2017.whisper_supports_flag(
              str(fake_whisper), "--carry-initial-prompt"
          )
      )
      cmd = hardware_imac2017.build_whisper_command(
          whisper_bin=str(fake_whisper),
          model_path="/tmp/m.bin",
          wav_path="/tmp/in.wav",
          output_prefix="/tmp/out",
          prompt="meeple, worker placement",
          nice_level=0,
      )
      self.assertIn("--carry-initial-prompt", cmd)
      self.assertIn("-mc", cmd)

      burn_cmd = hardware_imac2017.build_ffmpeg_burnin_command(
          ffmpeg_bin="/usr/local/bin/ffmpeg",
          input_video_path="/tmp/video.mp4",
          ass_path="/tmp/sub:1 [es]/subs.ass",
          output_mp4_path="/tmp/out.mp4",
          threads=3,
          nice_level=0,
      )
      self.assertEqual(burn_cmd[0], "/usr/local/bin/ffmpeg")
      self.assertIn("libx264", burn_cmd)
      vf_arg = burn_cmd[burn_cmd.index("-vf") + 1]
      self.assertIn(r"\:", vf_arg)
      self.assertIn(r"\[", vf_arg)

  def test_apple_silicon_profile_metal_and_videotoolbox(self) -> None:
    """Apple Silicon enables GPU, scales threads, uses h264_videotoolbox."""
    prof = hardware_imac2017.build_apple_silicon_profile(
        "Apple M4 Pro", perf_cores=10, total_cores=14, ram_gb=48
    )
    self.assertFalse(prof.force_no_gpu)
    self.assertEqual(prof.threads_standalone, 8)
    self.assertEqual(prof.threads_with_resolve, 6)
    self.assertEqual(prof.video_encoder, "h264_videotoolbox")
    small = hardware_imac2017.build_apple_silicon_profile(
        "Apple M1", perf_cores=4, total_cores=8, ram_gb=8
    )
    self.assertEqual(small.threads_standalone, 4)
    self.assertEqual(small.threads_with_resolve, 2)

    cmd = hardware_imac2017.build_whisper_command(
        whisper_bin="/opt/homebrew/bin/whisper-cli",
        model_path="/tmp/m.bin",
        wav_path="/tmp/in.wav",
        output_prefix="/tmp/out",
        threads=8,
        force_no_gpu=False,
        nice_level=0,
    )
    self.assertNotIn("-ng", cmd)
    self.assertEqual(cmd[cmd.index("-t") + 1], "8")

    vt_cmd = hardware_imac2017.build_ffmpeg_burnin_command(
        ffmpeg_bin="/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        input_video_path="/tmp/in.mov",
        ass_path="/tmp/sub.ass",
        output_mp4_path="/tmp/out.mp4",
        nice_level=0,
        video_encoder="h264_videotoolbox",
    )
    self.assertIn("h264_videotoolbox", vt_cmd)
    self.assertNotIn("libx264", vt_cmd)
    self.assertNotIn("-crf", vt_cmd)

  @staticmethod
  def _av_get_token(buf: str, term: str) -> tuple[str, str]:
    """Python port of libavutil av_get_token(), used by both escaping levels.

    Skips leading whitespace, stops at an unescaped/unquoted char in `term`,
    a backslash copies the next char literally, '...' copies its content
    literally, and unprotected trailing whitespace is trimmed.

    Args:
      buf: Text to parse.
      term: Terminator characters.

    Returns:
      (token, unparsed rest starting at the terminator).
    """
    ws = " \n\t\r"
    i = 0
    while i < len(buf) and buf[i] in ws:
      i += 1
    out: list[str] = []
    end = 0
    while i < len(buf) and buf[i] not in term:
      c = buf[i]
      i += 1
      if c == "\\" and i < len(buf):
        out.append(buf[i])
        i += 1
        end = len(out)
      elif c == "'":
        while i < len(buf) and buf[i] != "'":
          out.append(buf[i])
          i += 1
        if i < len(buf):
          i += 1
          end = len(out)
      else:
        out.append(c)
    while len(out) > end and out[-1] in ws:
      out.pop()
    return "".join(out), buf[i:]

  def test_burnin_filter_paths_with_apostrophe_colon_comma(self) -> None:
    """ass=/fontsdir= paths with ', :, comma, [], \\ round-trip ffmpeg rules."""
    ass_path = "/tmp/José's clips: v2, [final]/sub\\títulos; ñ .ass"
    fonts_dir = "/tmp/it's fonts:1, [x] "
    burn_cmd = hardware_imac2017.build_ffmpeg_burnin_command(
        ffmpeg_bin="/usr/local/bin/ffmpeg",
        input_video_path="/tmp/in.mp4",
        ass_path=ass_path,
        output_mp4_path="/tmp/out.mp4",
        fonts_dir=fonts_dir,
        nice_level=0,
    )
    vf_arg = burn_cmd[burn_cmd.index("-vf") + 1]
    # Level 2 (filtergraph): filter name, then its whole argument string.
    name, rest = self._av_get_token(vf_arg, "=,;[")
    self.assertEqual(name, "ass")
    self.assertTrue(rest.startswith("="))
    args, rest = self._av_get_token(rest[1:], "[],;")
    self.assertEqual(rest, "", vf_arg)
    # Level 1 (options): positional filename, then fontsdir=value.
    filename, rest = self._av_get_token(args, ":")
    self.assertEqual(filename, ass_path)
    self.assertTrue(rest.startswith(":fontsdir="), rest)
    fonts_val, rest = self._av_get_token(rest[len(":fontsdir=") :], ":")
    self.assertEqual(fonts_val, fonts_dir)
    self.assertEqual(rest, "")

  def test_burnin_filter_paths_with_local_ffmpeg(self) -> None:
    """Odd paths survive a real ffmpeg movie= probe and libass burn-in."""
    # End-to-end with the local ffmpeg when present. The "movie" source
    # filter parses its filename option exactly like "ass" (av_get_token on
    # both levels), so it validates the quoting even without libass.
    ffmpeg_bin = hardware_imac2017.find_binary(["ffmpeg"])
    if not ffmpeg_bin or not hardware_imac2017.ffmpeg_supports_filter(
        ffmpeg_bin, "movie"
    ):
      self.skipTest("no local ffmpeg with the movie filter")
    real_subprocess = hardware_imac2017.subprocess
    with tempfile.TemporaryDirectory() as tmp:
      odd_dir = Path(tmp) / "José's clip: v1, [final] ñandú; x"
      odd_dir.mkdir()
      video = odd_dir / "it's a, test: [1].nut"
      gen = real_subprocess.run(
          [
              ffmpeg_bin,
              "-v",
              "error",
              "-y",
              "-f",
              "lavfi",
              "-i",
              "color=c=black:s=32x32:d=0.2",
              "-c:v",
              "rawvideo",
              str(video),
          ],
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(gen.returncode, 0, gen.stderr)
      graph = f"movie={hardware_imac2017.quote_ffmpeg_filter_value(str(video))}"
      probe = real_subprocess.run(
          [
              ffmpeg_bin,
              "-v",
              "error",
              "-filter_complex",
              graph,
              "-frames:v",
              "1",
              "-f",
              "null",
              "-",
          ],
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(probe.returncode, 0, probe.stderr)

      # Real libass burn-in when an ffmpeg with the "ass" filter exists
      # (Homebrew ffmpeg-full); the slim formula lacks it.
      burnin_bin = hardware_imac2017.find_burnin_ffmpeg()
      if burnin_bin and hardware_imac2017.ffmpeg_supports_encoder(
          burnin_bin, "libx264"
      ):
        ass_file = odd_dir / "sub's, 1: [es].ass"
        ass_file.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 32\nPlayResY: 32\n"
            "\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
            "Style: Default,Arial,8\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:00.20,Default,Hola\n",
            encoding="utf-8",
        )
        burn = hardware_imac2017.build_ffmpeg_burnin_command(
            ffmpeg_bin=burnin_bin,
            input_video_path=str(video),
            ass_path=str(ass_file),
            output_mp4_path=str(odd_dir / "out.mp4"),
            fonts_dir=str(odd_dir),
            nice_level=0,
        )
        res = real_subprocess.run(
            burn, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stderr[-500:])
        self.assertGreater((odd_dir / "out.mp4").stat().st_size, 0)

  def test_is_resolve_running_tries_next_pattern_after_error(self) -> None:
    """A pgrep failure on the first pattern still checks the second one."""
    real_subprocess = hardware_imac2017.subprocess
    real_shutil = hardware_imac2017.shutil
    calls: list[str] = []

    class _Result:
      """Minimal CompletedProcess stand-in."""

      returncode = 0

    class _FakeSubprocess:
      """Times out on the first pgrep pattern and matches the second."""

      DEVNULL = real_subprocess.DEVNULL
      # Mirrors the real `subprocess.SubprocessError` attribute name.
      # pylint: disable-next=invalid-name
      SubprocessError = real_subprocess.SubprocessError

      @staticmethod
      def run(cmd: list[str], **_: object) -> _Result:
        """Records the pattern; raises TimeoutExpired on the first call."""
        calls.append(cmd[-1])
        if len(calls) == 1:
          raise real_subprocess.TimeoutExpired(cmd, 1.5)
        return _Result()

    class _FakeShutil:
      """Pretends pgrep is installed."""

      @staticmethod
      def which(_name: str) -> str:
        """Returns a fixed pgrep path."""
        return "/usr/bin/pgrep"

    hardware_imac2017.subprocess = _FakeSubprocess
    hardware_imac2017.shutil = _FakeShutil
    try:
      self.assertTrue(hardware_imac2017.is_resolve_running())
    finally:
      hardware_imac2017.subprocess = real_subprocess
      hardware_imac2017.shutil = real_shutil
    self.assertEqual(calls, ["DaVinci Resolve", "Resolve"])


class MachineSettingsTest(unittest.TestCase):
  """Per-machine performance modes, persisted preferences, and benchmarks."""

  def test_intel_profile_from_real_specs(self) -> None:
    """Intel profiles from CPU brand strings force CPU Whisper and libx264."""
    imac = hardware_imac2017.build_intel_profile(
        "Intel(R) Core(TM) i5-7500 CPU @ 3.40GHz",
        4,
        4,
        32,
        "Radeon Pro 555",
        2048,
    )
    self.assertTrue(imac.force_no_gpu)
    self.assertEqual(
        (imac.threads_standalone, imac.threads_with_resolve), (4, 3)
    )
    self.assertEqual(imac.cpu_short, "Intel Core i5")
    self.assertEqual(imac.video_encoder, "libx264")
    big = hardware_imac2017.build_intel_profile(
        "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz", 8, 16, 32
    )
    self.assertEqual((big.threads_standalone, big.threads_with_resolve), (8, 7))
    self.assertEqual(big.gpu_name, "GPU integrada")

  def test_modes_persistence_and_portable_preferences(self) -> None:
    """Modes resolve and persist; preference import/export is whitelisted."""
    prof = hardware_imac2017.IMAC_2017_PROFILE
    with tempfile.TemporaryDirectory() as tmp:
      store = machine_settings.MachineSettings(Path(tmp))
      perf = store.snapshot()["performance"]
      auto = machine_settings.resolve_performance(
          prof, perf, resolve_running=False
      )
      self.assertEqual(
          (auto["mode"], auto["threads"], auto["force_no_gpu"]),
          ("auto", 4, True),
      )
      self.assertEqual(
          machine_settings.resolve_performance(
              prof, perf, resolve_running=True
          )["threads"],
          3,
      )
      safe = machine_settings.resolve_performance(prof, {"mode": "safe"})
      self.assertEqual((safe["threads"], safe["fast_beam"]), (2, True))

      store.update({
          "performance": {
              "mode": "custom",
              "threads": 2,
              "force_no_gpu": False,
              "model_id": "small-q8",
              "bogus": 1,
          },
          "preferences": {
              "dialect": "es_mx",
              "custom_glossary": "Catan",
              "evil": "x",
          },
      })
      # Persisted to disk and reloaded by a fresh instance.
      data = machine_settings.MachineSettings(Path(tmp)).snapshot()
      self.assertNotIn("bogus", data["performance"])
      self.assertEqual(
          data["preferences"], {"dialect": "es_mx", "custom_glossary": "Catan"}
      )
      custom = machine_settings.resolve_performance(prof, data["performance"])
      self.assertEqual(
          (custom["threads"], custom["force_no_gpu"], custom["model_id"]),
          (2, False, "small-q8"),
      )

      exported = store.export_preferences()
      self.assertNotIn("performance", exported)
      other = machine_settings.MachineSettings(Path(tmp) / "otra_mac")
      other.import_preferences(exported)
      self.assertEqual(other.snapshot()["preferences"]["dialect"], "es_mx")
      self.assertEqual(other.snapshot()["performance"]["mode"], "auto")
      with self.assertRaises(ValueError):
        other.import_preferences({"nope": 1})

      store.reset_performance()
      self.assertEqual(store.snapshot()["performance"]["mode"], "auto")
      self.assertEqual(store.snapshot()["preferences"]["dialect"], "es_mx")

  def test_benchmark_recommendations(self) -> None:
    """Benchmark speed ratios recommend a faster, larger, or same model."""
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 0.8)["model_id"],
        "turbo-q5",
    )
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 20)["model_id"],
        "large-v3-q5",
    )
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 4)["model_id"],
        "turbo-q8",
    )

  def test_update_validates_types_and_load_checks_version(self) -> None:
    """Bad performance types are ignored; unknown versions merge safely."""
    prof = hardware_imac2017.IMAC_2017_PROFILE
    max_t = hardware_imac2017.MAX_WHISPER_THREADS
    self.assertEqual(hardware_imac2017._MAX_WHISPER_THREADS, max_t)
    # Direct callers with garbage must not crash either.
    direct = machine_settings.resolve_performance(
        prof, {"mode": "custom", "threads": "abc"}
    )
    self.assertEqual(direct["threads"], prof.threads_standalone)

    with tempfile.TemporaryDirectory() as tmp:
      store = machine_settings.MachineSettings(Path(tmp))
      store.update({
          "performance": {
              "mode": "custom",
              "threads": "abc",
              "force_no_gpu": "maybe",
              "fast_beam": "true",
              "enhance_voice": 0,
              "model_id": 42,
          }
      })
      perf = store.snapshot()["performance"]
      self.assertEqual(perf["mode"], "custom")
      self.assertIsNone(perf["threads"])
      self.assertIsNone(perf["force_no_gpu"])
      self.assertIs(perf["fast_beam"], True)
      self.assertIs(perf["enhance_voice"], False)
      self.assertIsNone(perf["model_id"])
      eff = machine_settings.resolve_performance(prof, perf)
      self.assertEqual(eff["threads"], prof.threads_standalone)

      store.update({"performance": {"threads": "3"}})
      self.assertEqual(store.snapshot()["performance"]["threads"], 3)
      store.update({"performance": {"threads": 99}})
      self.assertEqual(store.snapshot()["performance"]["threads"], max_t)
      store.update({"performance": {"threads": True, "mode": "turbo"}})
      self.assertEqual(store.snapshot()["performance"]["threads"], max_t)
      self.assertEqual(store.snapshot()["performance"]["mode"], "auto")

      bench = {"turbo-q8:cpu": {"realtime_factor": 3.0}}
      raw = {
          "performance": {"threads": "abc", "mode": "bogus", "fast_beam": 1},
          "preferences": {"dialect": "es_ar", "evil": 1},
          "benchmarks": bench,
      }
      path = Path(tmp) / "settings.json"
      for version, keeps_bench in ((None, False), (99, False), (1, True)):
        doc = dict(raw)
        if version is not None:
          doc["version"] = version
        path.write_text(json.dumps(doc), encoding="utf-8")
        data = machine_settings.MachineSettings(Path(tmp)).snapshot()
        self.assertEqual(data["version"], machine_settings.SETTINGS_VERSION)
        self.assertIsNone(data["performance"]["threads"])
        self.assertEqual(data["performance"]["mode"], "auto")
        self.assertIs(data["performance"]["fast_beam"], True)
        self.assertEqual(data["preferences"], {"dialect": "es_ar"})
        self.assertEqual(data["benchmarks"], bench if keeps_bench else {})
        machine_settings.resolve_performance(prof, data["performance"])


class SpanishNlpTest(unittest.TestCase):
  """Whisper -ojf parsing, Spanish cleanup, RAE wrapping, and alignment."""

  def test_align_words_keeps_timings_after_text_edits(self) -> None:
    """Edited cue text keeps untouched Whisper word timings, in order."""
    words = [
        spanish_nlp.WordTiming("Hoy", 1000, 1300),
        spanish_nlp.WordTiming("jugamos", 1300, 1900),
        spanish_nlp.WordTiming("Katan", 1900, 2500),
        spanish_nlp.WordTiming("otra", 2600, 2900),
        spanish_nlp.WordTiming("vez.", 2900, 3200),
    ]
    # Fix a word, insert two, delete one.
    aligned = spanish_nlp.align_words_to_lines(
        words, ["Hoy jugamos a Catan", "de nuevo otra vez."], 1000, 3200
    )
    self.assertEqual(
        [w.text for w in aligned],
        ["Hoy", "jugamos", "a", "Catan", "de", "nuevo", "otra", "vez."],
    )
    by_text = {w.text: w for w in aligned}
    # Untouched words keep their exact Whisper timings.
    self.assertEqual(
        (by_text["Hoy"].start_ms, by_text["Hoy"].end_ms), (1000, 1300)
    )
    self.assertEqual(
        (by_text["vez."].start_ms, by_text["vez."].end_ms), (2900, 3200)
    )
    for prev, cur in zip(aligned, aligned[1:]):
      self.assertLessEqual(prev.start_ms, cur.start_ms)
      self.assertGreater(cur.end_ms, cur.start_ms)

    # Moving/resizing the cue rescales out-of-range words into the new span.
    moved = spanish_nlp.align_words_to_lines(
        words, ["Hoy jugamos Katan otra vez."], 5000, 6100
    )
    self.assertEqual(moved[0].start_ms, 5000)
    self.assertEqual(moved[-1].end_ms, 6100)

    # Cues without word timings get proportional ones (karaoke still works).
    synth = spanish_nlp.align_words_to_lines([], ["uno dos tres"], 0, 900)
    self.assertEqual(len(synth), 3)
    self.assertEqual(synth[-1].end_ms, 900)

  def test_parse_whisper_ojf_bpe_utf8_recovery_and_hallucinations(self) -> None:
    """Split UTF-8 BPE tokens are rejoined; silence hallucinations dropped."""
    ojf = {
        "transcription": [
            {
                "offsets": {"from": 0, "to": 2200},
                "text": " Vamos a transcribir español mañana.",
                "tokens": [
                    {
                        "text": "[_BEG_]",
                        "offsets": {"from": 0, "to": 0},
                        "p": 0.9,
                    },
                    {
                        "text": " Vamos",
                        "offsets": {"from": 100, "to": 450},
                        "p": 0.95,
                    },
                    {
                        "text": " a",
                        "offsets": {"from": 450, "to": 550},
                        "p": 0.98,
                    },
                    {
                        "text": " trans",
                        "offsets": {"from": 560, "to": 800},
                        "p": 0.92,
                    },
                    {
                        "text": "crib",
                        "offsets": {"from": 800, "to": 1000},
                        "p": 0.91,
                    },
                    {
                        "text": "ir",
                        "offsets": {"from": 1000, "to": 1150},
                        "p": 0.94,
                    },
                    {
                        "text": " espa\ufffd",
                        "offsets": {"from": 1160, "to": 1450},
                        "p": 0.88,
                    },
                    {
                        "text": "\ufffdol",
                        "offsets": {"from": 1450, "to": 1650},
                        "p": 0.88,
                    },
                    {
                        "text": " ma\ufffdana.",
                        "offsets": {"from": 1660, "to": 2100},
                        "p": 0.90,
                    },
                ],
            },
            {
                "offsets": {"from": 2400, "to": 5000},
                "text": " Subtítulos realizados por la comunidad de Amara.org",
                "tokens": [
                    {
                        "text": " Subtítulos",
                        "offsets": {"from": 2400, "to": 3100},
                        "p": 0.4,
                    },
                ],
            },
            {
                "offsets": {"from": 5200, "to": 6800},
                "text": " Suscríbete al canal para más",
                "tokens": [],
            },
        ]
    }
    words = spanish_nlp.parse_whisper_ojf(ojf)
    texts = [w.text for w in words]
    self.assertEqual(texts, ["Vamos", "a", "transcribir", "español", "mañana."])
    self.assertEqual(words[0].start_ms, 100)
    self.assertGreater(words[3].end_ms, words[3].start_ms)

  def test_parse_whisper_ojf_strips_timestamp_tokens(self) -> None:
    """Special [_BEG_] and [_TT_n] timestamp tokens never become words."""
    ojf = {
        "transcription": [{
            "offsets": {"from": 0, "to": 1000},
            "text": " Hola[_TT_25] mundo.[_TT_50]",
            "tokens": [
                {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}, "p": 0.9},
                {"text": " Hola", "offsets": {"from": 0, "to": 400}, "p": 0.9},
                {
                    "text": "[_TT_25]",
                    "offsets": {"from": 500, "to": 500},
                    "p": 0.9,
                },
                {
                    "text": " mundo.",
                    "offsets": {"from": 500, "to": 900},
                    "p": 0.9,
                },
                {
                    "text": "[_TT_335]",
                    "offsets": {"from": 1000, "to": 1000},
                    "p": 0.9,
                },
            ],
        }]
    }
    texts = [w.text for w in spanish_nlp.parse_whisper_ojf(ojf)]
    self.assertEqual(texts, ["Hola", "mundo."])

  def test_clean_spanish_words_glossary_muletillas_and_inverted_punct(
      self,
  ) -> None:
    """Filler removal, glossary casing, and ¿/¡ repair work together."""
    raw_words = [
        spanish_nlp.WordTiming("eh", 100, 300),
        spanish_nlp.WordTiming("Oye,", 500, 750),
        spanish_nlp.WordTiming("cuánto", 760, 1100),
        spanish_nlp.WordTiming("cuesta", 1110, 1450),
        spanish_nlp.WordTiming("davinci", 1460, 1800),
        spanish_nlp.WordTiming("resolve?", 1810, 2200),
        spanish_nlp.WordTiming("este...", 2500, 2800),
        spanish_nlp.WordTiming("Este", 3200, 3450),
        spanish_nlp.WordTiming("flujo", 3460, 3800),
        spanish_nlp.WordTiming("es", 3810, 3950),
        spanish_nlp.WordTiming("genial!", 3960, 4400),
    ]
    cfg = spanish_nlp.SpanishFormatConfig(
        remove_muletillas=True,
        fix_inverted_punctuation=True,
        custom_glossary=["DaVinci Resolve"],
    )
    cleaned = spanish_nlp.clean_spanish_words(raw_words, cfg)
    texts = [w.text for w in cleaned]
    self.assertNotIn("eh", texts)
    self.assertNotIn("este...", texts)
    self.assertIn("¡Este", texts)  # Keeps real demonstrative "Este flujo"
    self.assertEqual(
        texts,
        [
            "Oye,",
            "¿cuánto",
            "cuesta",
            "DaVinci",
            "Resolve?",
            "¡Este",
            "flujo",
            "es",
            "genial!",
        ],
    )

  def test_rae_line_wrapping_avoids_weak_endings(self) -> None:
    """RAE wrapping never ends the first line on a weak word (de, en...)."""
    words = [
        "Configura",
        "el",
        "audio",
        "de",
        "tu",
        "línea",
        "de",
        "tiempo",
        "en",
        "Resolve.",
    ]
    lines = spanish_nlp.wrap_spanish_lines(words, max_cpl=30, max_lines=2)
    self.assertEqual(len(lines), 2)
    last_word_l1 = lines[0].split()[-1].lower()
    self.assertNotIn(last_word_l1, spanish_nlp.SPANISH_WEAK_LINE_ENDINGS)

  def test_segmentation_presets_and_non_overlapping_cues(self) -> None:
    """Reels preset yields more cues than YouTube, and none overlap."""
    words = [
        spanish_nlp.WordTiming("Bienvenidos", 200, 650),
        spanish_nlp.WordTiming("al", 660, 800),
        spanish_nlp.WordTiming("tutorial", 810, 1250),
        spanish_nlp.WordTiming("rápido", 1260, 1650),
        spanish_nlp.WordTiming("de", 1660, 1780),
        spanish_nlp.WordTiming("edición.", 1790, 2300),
        spanish_nlp.WordTiming("Funciona", 3100, 3550),
        spanish_nlp.WordTiming("bien?", 3560, 3900),
    ]
    cues_yt = spanish_nlp.segment_and_wrap_spanish(
        words,
        spanish_nlp.SpanishFormatConfig.from_dict(
            {"preset_id": "youtube_16_9"}
        ),
    )
    cues_reels = spanish_nlp.segment_and_wrap_spanish(
        words,
        spanish_nlp.SpanishFormatConfig.from_dict({"preset_id": "reels_9_16"}),
    )
    self.assertEqual(len(cues_yt), 2)
    self.assertTrue(cues_yt[1].text.startswith("¿Funciona"))
    self.assertGreater(len(cues_reels), len(cues_yt))
    for i in range(len(cues_reels) - 1):
      self.assertLessEqual(cues_reels[i].end_ms, cues_reels[i + 1].start_ms)

  def test_boardgame_initial_prompt_fits_and_includes_glossary(self) -> None:
    """juegos_mesa initial prompt stays <= 580 chars with glossary terms."""
    prompt = spanish_nlp.build_spanish_initial_prompt(
        "es_es", ["Catan", "Devir"], domain="juegos_mesa"
    )
    self.assertLessEqual(len(prompt), 580)
    self.assertIn("vosotros", prompt)
    self.assertIn("Catan", prompt)
    self.assertIn("meeple", prompt)

  def test_boardgame_domain_anglicisms_and_hesitation_punct(self) -> None:
    """juegos_mesa fixes board-game anglicisms; ¿ skips hesitations."""
    tokens = [
        ("Hoy", 100, 300),
        ("probamos", 310, 650),
        ("un", 660, 780),
        ("eurogueim", 790, 1200),
        ("con", 1210, 1340),
        ("mipels,", 1350, 1720),
        ("worker", 1730, 2000),
        ("pleisment", 2010, 2400),
        ("y", 2410, 2500),
        ("dec", 2510, 2720),
        ("bilding.", 2730, 3100),
        ("eh", 3500, 3700),
        ("Cuánto", 3710, 4100),
        ("dura", 4110, 4350),
        ("el", 4360, 4450),
        ("setap", 4460, 4800),
        ("en", 4810, 4920),
        ("pleimat", 4930, 5300),
        ("y", 5310, 5400),
        ("el", 5410, 5500),
        ("dauntaim?", 5510, 5950),
        ("Salió", 6300, 6600),
        ("en", 6610, 6720),
        ("kikstarter,", 6730, 7200),
        ("gueimfaund", 7210, 7700),
        ("y", 7710, 7800),
        ("be", 7810, 7950),
        ("ge", 7960, 8100),
        ("ge", 8110, 8250),
        ("con", 8260, 8420),
        ("glumjeiven!", 8430, 8950),
    ]
    raw_words = [spanish_nlp.WordTiming(t, s, e) for t, s, e in tokens]
    cfg = spanish_nlp.SpanishFormatConfig(
        domain="juegos_mesa",
        remove_muletillas=False,
        fix_inverted_punctuation=True,
    )
    cleaned = [w.text for w in spanish_nlp.clean_spanish_words(raw_words, cfg)]
    self.assertIn("eurogame", cleaned)
    self.assertIn("meeples,", cleaned)
    self.assertIn("placement", cleaned)
    self.assertIn("deckbuilding.", cleaned)
    self.assertIn("eh", cleaned)
    self.assertIn("¿Cuánto", cleaned)
    self.assertNotIn("¿eh", cleaned)
    self.assertIn("setup", cleaned)
    self.assertIn("playmat", cleaned)
    self.assertIn("downtime?", cleaned)
    self.assertIn("Kickstarter,", cleaned)
    self.assertIn("Gamefound", cleaned)
    self.assertIn("BGG", cleaned)
    self.assertIn("Gloomhaven!", cleaned)

  def test_align_words_never_overlaps(self) -> None:
    # Short neighbours (<20ms) + multiple inserted words in a tight gap.
    """Aligned words stay inside the cue span and never overlap."""
    words = [
        spanish_nlp.WordTiming("Hola", 100, 110),
        spanish_nlp.WordTiming("mundo", 120, 130),
    ]
    aligned = spanish_nlp.align_words_to_lines(
        words, ["Hola gran nuevo mundo"], 100, 130
    )
    self.assertEqual(
        [w.text for w in aligned], ["Hola", "gran", "nuevo", "mundo"]
    )
    for w in aligned:
      self.assertGreaterEqual(w.start_ms, 100)
      self.assertLessEqual(w.end_ms, 130)
      self.assertLessEqual(w.start_ms, w.end_ms)
    for prev, cur in zip(aligned, aligned[1:]):
      self.assertLessEqual(prev.end_ms, cur.start_ms)

    # Unsorted/overlapping source words where src[0].start_ms != min(start_ms).
    unsorted = [
        spanish_nlp.WordTiming("uno", 200, 260),
        spanish_nlp.WordTiming("dos", 50, 220),
    ]
    rescaled = spanish_nlp.align_words_to_lines(
        unsorted, ["uno extra dos"], 100, 500
    )
    self.assertEqual([w.text for w in rescaled], ["uno", "extra", "dos"])
    for prev, cur in zip(rescaled, rescaled[1:]):
      self.assertLessEqual(prev.end_ms, cur.start_ms)

  def test_unknown_preset_id_normalized_to_youtube(self) -> None:
    """Unknown preset ids report the youtube_16_9 preset actually applied."""
    cfg = spanish_nlp.SpanishFormatConfig.from_dict({"preset_id": "nope"})
    self.assertEqual(cfg.preset_id, "youtube_16_9")
    self.assertEqual(cfg.max_cpl, 42)
    known = spanish_nlp.SpanishFormatConfig.from_dict(
        {"preset_id": "reels_9_16"}
    )
    self.assertEqual(known.preset_id, "reels_9_16")

  def test_refresh_cue_metrics_counts_joined_line_chars(self) -> None:
    """CPS counts the lines joined by single spaces over the duration."""
    cue = spanish_nlp.SubtitleCue(
        index=1, start_ms=0, end_ms=1000, lines=["Hola", "mundo"]
    )
    spanish_nlp.refresh_cue_metrics(cue)
    self.assertAlmostEqual(cue.cps, len("Hola mundo"))


class ExportersTest(unittest.TestCase):
  """Subtitle serializers (SRT/VTT/ASS/TXT/FCPXML/Lua) and NDF timecodes."""

  def _sample_cues(self) -> list[spanish_nlp.SubtitleCue]:
    """Returns one two-line Spanish cue with word timings (500-2500 ms)."""
    return [
        spanish_nlp.SubtitleCue(
            index=1,
            start_ms=500,
            end_ms=2500,
            lines=["¿Cómo estás", "en Resolve 20?"],
            words=[
                spanish_nlp.WordTiming("¿Cómo", 500, 900),
                spanish_nlp.WordTiming("estás", 900, 1400),
                spanish_nlp.WordTiming("en", 1400, 1600),
                spanish_nlp.WordTiming("Resolve", 1600, 2100),
                spanish_nlp.WordTiming("20?", 2100, 2500),
            ],
        )
    ]

  def test_srt_vtt_ass_fcpxml_and_lua_payload(self) -> None:
    """Each exporter emits correct timings, styles, and Lua escaping."""
    cues = self._sample_cues()
    style = exporters.StyleConfig()

    srt0 = exporters.export_srt(cues, 0, style)
    self.assertIn("00:00:00,500 --> 00:00:02,500", srt0)

    srt_tc = exporters.export_srt(cues, 3_600_000, style)
    self.assertIn("01:00:00,500 --> 01:00:02,500", srt_tc)

    vtt = exporters.export_vtt(cues, 0, style)
    self.assertTrue(vtt.startswith("WEBVTT"))
    self.assertIn("00:00:00.500 --> 00:00:02.500", vtt)

    txt = exporters.export_txt(cues, include_timestamps=False, style=style)
    self.assertEqual(txt.strip(), "¿Cómo estás en Resolve 20?")
    txt_tc = exporters.export_txt(cues, include_timestamps=True, style=style)
    self.assertIn(
        "[00:00:00,500 --> 00:00:02,500] ¿Cómo estás en Resolve 20?", txt_tc
    )

    ass = exporters.export_ass_karaoke(cues, style)
    self.assertIn("[V4+ Styles]", ass)
    self.assertNotIn(r"\kf", ass)  # Karaoke is opt-in.
    self.assertIn("Style: AutoSubsES,Open Sans,72,&H00FFFFFF,&H00FFFFFF,", ass)
    ass_k = exporters.export_ass_karaoke(
        cues, exporters.StyleConfig(karaoke_enabled=True)
    )
    self.assertIn(r"{\kf40}¿Cómo", ass_k)
    # Primary = highlight (#FFD43B -> BGR 3BD4FF), Secondary = fill.
    self.assertIn(
        "Style: AutoSubsES,Open Sans,72,&H003BD4FF,&H00FFFFFF,", ass_k
    )

    for fps, expected_dur in ((24.0, "100/2400s"), (29.97, "1001/30000s")):
      xml_str = exporters.export_fcpxml(
          cues, timeline_name="Mi Demo", fps=fps, start_frame=86400, style=style
      )
      self.assertIn(expected_dur, xml_str)
      root = ET.fromstring(xml_str.split("<!DOCTYPE fcpxml>\n", 1)[1])
      self.assertEqual(root.tag, "fcpxml")
      self.assertIsNotNone(root.find(".//title"))

    lua_code = exporters.export_resolve_lua_payload(
        cues=cues,
        mode="textplus",
        track_name='Sub "ES"',
        template_name="AutoSubs_ES_Cine_Netflix",
        srt_path="/tmp/a.srt",
        srt_timeline_tc_path="/tmp/b.srt",
        fcpxml_path="/tmp/c.fcpxml",
        fps=24.0,
        timeline_start_frame=86400,
        style=style,
    )
    self.assertIn('track_name = "Sub \\"ES\\""', lua_code)
    self.assertIn("¿Cómo estás\\nen Resolve 20?", lua_code)

  def test_style_from_dict_clamps_alpha_values(self) -> None:
    """StyleConfig.from_dict clamps alphas to 0..1 with safe fallbacks."""
    parsed = exporters.StyleConfig.from_dict({
        "fill_alpha": 2,
        "outline_alpha": -1,
        "shadow_alpha": "abc",
        "box_alpha": 0.4,
    })
    self.assertEqual(parsed.fill_alpha, 1.0)
    self.assertEqual(parsed.outline_alpha, 0.0)
    self.assertEqual(parsed.shadow_alpha, 0.65)
    self.assertEqual(parsed.box_alpha, 0.4)
    self.assertEqual(
        exporters.StyleConfig.from_dict({"font": "Arial"}).highlight_alpha, 1.0
    )

  def test_ass_style_alpha_bytes_for_fill_outline_and_box(self) -> None:
    """ASS style colours carry inverted alpha bytes for fill/outline/box."""
    cues = self._sample_cues()

    # Defaults keep the expected style line with black drop shadow.
    ass_default = exporters.export_ass_karaoke(cues, exporters.StyleConfig())
    self.assertIn("&H00FFFFFF,&H00FFFFFF,&H00120D0B,&H59000000,", ass_default)

    # 50 % fill and 0 % outline -> ASS alpha bytes (00 opaque, FF clear).
    ass_alpha = exporters.export_ass_karaoke(
        cues, exporters.StyleConfig(fill_alpha=0.5, outline_alpha=0.0)
    )
    self.assertIn("&H80FFFFFF,&H80FFFFFF,&HFF120D0B,", ass_alpha)

    # Opaque box (BorderStyle 3) is painted with OutlineColour.
    ass_box = exporters.export_ass_karaoke(
        cues,
        exporters.StyleConfig(
            box_enabled=True, box_hex="#112233", box_alpha=0.5
        ),
    )
    self.assertIn("&H80332211,&H59000000,", ass_box)
    self.assertIn(",3,6,0,2,", ass_box)

  def test_fcpxml_font_color_carries_fill_alpha(self) -> None:
    """FCPXML fontColor appends the fill alpha (1 by default)."""
    cues = self._sample_cues()
    fcp_default = exporters.export_fcpxml(
        cues, timeline_name="A", style=exporters.StyleConfig()
    )
    self.assertIn('fontColor="1.000 1.000 1.000 1"', fcp_default)
    fcp_alpha = exporters.export_fcpxml(
        cues, timeline_name="A", style=exporters.StyleConfig(fill_alpha=0.5)
    )
    self.assertIn('fontColor="1.000 1.000 1.000 0.5"', fcp_alpha)

  def test_lua_payload_carries_alpha_fields(self) -> None:
    """The Resolve Lua payload exports fill/outline/shadow alphas."""
    lua_code = exporters.export_resolve_lua_payload(
        cues=self._sample_cues(),
        mode="textplus",
        track_name="Sub",
        template_name="AutoSubs_ES_Cine_Netflix",
        srt_path="/tmp/a.srt",
        srt_timeline_tc_path="/tmp/b.srt",
        fcpxml_path="/tmp/c.fcpxml",
        style=exporters.StyleConfig(
            fill_alpha=0.25, outline_alpha=0.5, shadow_alpha=0.3
        ),
    )
    self.assertIn("fill_a = 0.2500", lua_code)
    self.assertIn("outline_a = 0.5000", lua_code)
    self.assertIn("shadow_a = 0.3000", lua_code)

  def _ndf_23976_cues(self) -> list[spanish_nlp.SubtitleCue]:
    """Returns the sample cue plus one cue at 1001 s for 23.976 NDF checks.

    1001 s of media is exactly 24000 frames at 24000/1001 fps, i.e. 1000 s
    (00:16:40) of non-drop-frame timecode, which exposes any fps rounding.

    Returns:
      Two cues: the 500-2500 ms sample cue and a 1001-1003.002 s cue.
    """
    return self._sample_cues() + [
        spanish_nlp.SubtitleCue(
            index=2,
            start_ms=1_001_000,
            end_ms=1_003_002,
            lines=["Sin deriva NDF"],
        )
    ]

  def _edited_cue_with_braces(self) -> spanish_nlp.SubtitleCue:
    """Returns the sample cue re-parsed after editing its text to "{2 VP}".

    Returns:
      A cue rebuilt with SubtitleCue.from_dict from the edited dict, so its
      words are re-aligned to the new text.
    """
    edited_raw = self._sample_cues()[0].to_dict()
    edited_raw["lines"] = ["Texto editado {2 VP}"]
    edited_raw["text"] = "Texto editado {2 VP}"
    return spanish_nlp.SubtitleCue.from_dict(edited_raw)

  def test_ms_to_timeline_tc_23976_ndf_is_drift_free(self) -> None:
    """23.976 NDF media ms map to timeline ms without drift from 01:00:00."""
    self.assertEqual(
        exporters.ms_to_timeline_tc_ms(0, start_frame=86400, fps=23.976),
        3_600_000,
    )
    self.assertEqual(
        exporters.ms_to_timeline_tc_ms(
            1_001_000, start_frame=86400, fps=23.976
        ),
        4_600_000,
    )

  def test_srt_timeline_tc_23976_ndf_is_drift_free(self) -> None:
    """Timeline-TC SRT at 23.976 keeps cues on NDF timecode after 1001 s."""
    srt_23976 = exporters.export_srt_timeline_tc(
        self._ndf_23976_cues(), start_frame=86400, fps=23.976
    )
    self.assertIn("01:00:00,500 --> 01:00:02,498", srt_23976)
    self.assertIn("01:16:40,000 --> 01:16:42,000", srt_23976)

  def test_jump_playhead_23976_ndf_timecode(self) -> None:
    """Bridge jump at 23.976 NDF yields frame-exact HH:MM:SS:FF timecodes."""
    with tempfile.TemporaryDirectory() as tmp:
      tmp_p = Path(tmp)
      bridge = resolve_bridge.ResolveBridge(app_data_dir=tmp_p / "app")
      (bridge.inbox_dir / "timeline_meta.json").write_text(
          json.dumps({"fps": 23.976, "start_frame": 86400}), encoding="utf-8"
      )
      j0 = bridge.jump_playhead(0)
      j1 = bridge.jump_playhead(1_001_000)
      self.assertEqual(j0["timecode"], "01:00:00:00")
      self.assertEqual(j1["timecode"], "01:16:40:00")

  def test_cue_from_dict_realigns_words_after_text_edit(self) -> None:
    """Editing cue text re-splits words and keeps them inside the cue span."""
    edited_cue = self._edited_cue_with_braces()
    self.assertEqual(
        [w.text for w in edited_cue.words], ["Texto", "editado", "{2", "VP}"]
    )
    self.assertGreaterEqual(edited_cue.words[0].start_ms, edited_cue.start_ms)
    self.assertLessEqual(edited_cue.words[-1].end_ms, edited_cue.end_ms)

  def test_ass_escapes_braces_and_commas_in_text_speaker_font(self) -> None:
    """ASS escapes {} in text and strips commas from font and speaker."""
    edited_cue = self._edited_cue_with_braces()
    edited_cue.speaker = "Alex, Host"
    ass_safe = exporters.export_ass_karaoke(
        [edited_cue],
        style=exporters.StyleConfig(font="Mi Fuente, Bold"),
    )
    self.assertIn("Style: AutoSubsES,Mi Fuente  Bold,", ass_safe)
    self.assertIn(",Alex  Host,", ass_safe)
    self.assertIn(r"\{2 VP\}", ass_safe)
    self.assertNotIn(r"\kf", ass_safe)

  def test_burnin_fontsdir_escapes_space_and_colon(self) -> None:
    """Burn-in quotes a fontsdir containing a space and a colon."""
    burn_cmd = hardware_imac2017.build_ffmpeg_burnin_command(
        ffmpeg_bin="/usr/local/bin/ffmpeg",
        input_video_path="/tmp/in.mp4",
        ass_path="/tmp/sub.ass",
        output_mp4_path="/tmp/out.mp4",
        fonts_dir="/tmp/my fonts:1",
        nice_level=0,
    )
    vf_arg = burn_cmd[burn_cmd.index("-vf") + 1]
    self.assertIn(r":fontsdir='/tmp/my fonts\:1'", vf_arg)

  def test_clean_words_inverted_punct_after_dash_and_quotes(self) -> None:
    """¿ goes after a leading dash; quoted jargon is still repaired."""
    quoted_words = [
        spanish_nlp.WordTiming("—Sabías", 100, 500),
        spanish_nlp.WordTiming("“mipels”?", 520, 980),
    ]
    norm = [
        w.text
        for w in spanish_nlp.clean_spanish_words(
            quoted_words,
            spanish_nlp.SpanishFormatConfig(
                domain="juegos_mesa", fix_inverted_punctuation=True
            ),
        )
    ]
    self.assertEqual(norm, ["—¿Sabías", "“meeples”?"])

  def test_ass_back_colour_black_and_timeline_start_frame_zero(self) -> None:
    """ASS BackColour stays black; start_frame=0 survives every exporter."""
    cues = self._sample_cues()
    style = exporters.StyleConfig(
        box_hex="#FF3366",
        box_enabled=False,
        shadow_enabled=True,
        shadow_alpha=0.5,
    )
    ass = exporters.export_ass_karaoke(cues, style=style)
    style_lines = [
        ln for ln in ass.splitlines() if ln.startswith("Style: AutoSubsES,")
    ]
    self.assertEqual(len(style_lines), 1)
    # Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
    # OutlineColour, BackColour, ...
    fields = style_lines[0].split(",")
    self.assertEqual(fields[6], "&H80000000")

    lua_zero = exporters.export_resolve_lua_payload(
        cues=cues,
        mode="subtitle_srt",
        track_name="Subtítulos ES",
        template_name="AutoSubs_ES_Cine_Netflix",
        srt_path="/tmp/a.srt",
        srt_timeline_tc_path="/tmp/b.srt",
        fcpxml_path="/tmp/c.fcpxml",
        fps=24.0,
        timeline_start_frame=0,
        style=style,
    )
    self.assertIn("timeline_start_frame = 0,", lua_zero)

    fcpxml_zero = exporters.export_fcpxml(cues, start_frame=0, style=style)
    self.assertIn('tcStart="0s"', fcpxml_zero)

    with tempfile.TemporaryDirectory() as tmp:
      bridge = resolve_bridge.ResolveBridge(Path(tmp))
      bridge.inbox_dir.mkdir(parents=True, exist_ok=True)
      (bridge.inbox_dir / "timeline_meta.json").write_text(
          json.dumps({"start_frame": 86400, "fps": 24.0}), encoding="utf-8"
      )
      bridge.stage_or_push_to_resolve(cues, {"timeline_start_frame": 0})
      pushed_lua = (bridge.outbox_dir / "push_payload.lua").read_text(
          encoding="utf-8"
      )
      self.assertIn("timeline_start_frame = 0,", pushed_lua)

  def _karaoke_syllables(self, ass: str) -> list[tuple[int, int, str]]:
    """Parses the first Dialogue into (start_cs, kf_cs, text) per \\kf word.

    `start_cs` is the sum of every preceding \\k/\\kf value, i.e. when the
    renderer starts that word's sweep relative to the event start.
    """
    dialogue = next(
        ln for ln in ass.splitlines() if ln.startswith("Dialogue:")
    ).split(",", 9)[9]
    out: list[tuple[int, int, str]] = []
    elapsed = 0
    for m in re.finditer(r"\{\\k(f?)(\d+)\}([^{]*)", dialogue):
      val = int(m.group(2))
      if m.group(1):
        out.append((elapsed, val, m.group(3).replace(r"\N", "").strip()))
      elapsed += val
    return out

  def test_ass_karaoke_sweeps_start_at_word_start(self) -> None:
    """Each \\kf sweep starts at its word's start_ms (gaps become spacers)."""
    words = [
        spanish_nlp.WordTiming("Hola", 1230, 1500),  # 230 ms lead-in.
        spanish_nlp.WordTiming("a", 1755, 1755),  # Zero duration.
        spanish_nlp.WordTiming("todos", 2105, 2600),
        spanish_nlp.WordTiming("y", 2550, 2700),  # Overlaps previous.
        spanish_nlp.WordTiming("todas", 3333, 9000),  # Ends past the cue.
    ]
    cue = spanish_nlp.SubtitleCue(
        index=1,
        start_ms=1000,
        end_ms=4000,
        lines=["Hola a todos", "y todas"],
        words=words,
    )
    ass = exporters.export_ass_karaoke(
        [cue], exporters.StyleConfig(karaoke_enabled=True)
    )
    self.assertIn(r"{\k23}{\kf27}Hola", ass)
    self.assertIn(r"\N", ass)
    sylls = self._karaoke_syllables(ass)
    self.assertEqual([s[2] for s in sylls], [w.text for w in words])
    for (start_cs, dur_cs, _), w in zip(sylls, words):
      self.assertGreaterEqual(dur_cs, 0)
      if w.text == "y":
        # Overlap: starts when the previous sweep ends (never negative).
        self.assertEqual(start_cs, (2600 - 1000) // 10)
        continue
      self.assertLessEqual(abs(start_cs - (w.start_ms - cue.start_ms) / 10), 1)
    # Sweeps never run past the event end (300 cs).
    self.assertLessEqual(sylls[-1][0] + sylls[-1][1], 300)

    # Long cue: no cumulative rounding drift across many odd-ms words.
    many = [
        spanish_nlp.WordTiming(f"w{i}", 15 + i * 337, 15 + i * 337 + 205)
        for i in range(60)
    ]
    long_cue = spanish_nlp.SubtitleCue(
        index=1,
        start_ms=7,
        end_ms=many[-1].end_ms + 100,
        lines=[" ".join(w.text for w in many)],
        words=many,
    )
    long_ass = exporters.export_ass_karaoke(
        [long_cue], exporters.StyleConfig(karaoke_enabled=True)
    )
    for (start_cs, _, _), w in zip(self._karaoke_syllables(long_ass), many):
      self.assertLessEqual(
          abs(start_cs - (w.start_ms - long_cue.start_ms) / 10), 1
      )

    # Word/token count mismatch falls back to static fill text.
    broken = spanish_nlp.SubtitleCue(
        index=1,
        start_ms=0,
        end_ms=1000,
        lines=["dos palabras"],
        words=[spanish_nlp.WordTiming("dos", 0, 500)],
    )
    ass_broken = exporters.export_ass_karaoke(
        [broken], exporters.StyleConfig(karaoke_enabled=True)
    )
    self.assertNotIn(r"\kf", ass_broken)
    self.assertIn(r"\1c&H", ass_broken)

  def test_fps_rational_common_rates(self) -> None:
    """Common UI frame rates map to Resolve's exact rationals."""
    ntsc24 = (24000, 1001, "1001/24000s")
    expected = {
        23.976: ntsc24,
        23.98: ntsc24,
        23.984: ntsc24,  # Only matched by the 23.98 table key.
        24: (2400, 100, "100/2400s"),
        25: (2500, 100, "100/2500s"),
        29.97: (30000, 1001, "1001/30000s"),
        30: (3000, 100, "100/3000s"),
        50: (5000, 100, "100/5000s"),
        59.94: (60000, 1001, "1001/60000s"),
        60: (6000, 100, "100/6000s"),
        12.5: (1250, 100, "100/1250s"),  # Generic /100 fallback.
    }
    for fps, rational in expected.items():
      self.assertEqual(exporters._fps_rational(fps), rational, fps)


class FontManagerAndAnitypeTest(unittest.TestCase):
  """Font parsing, Spanish glyph checks, pack grouping, and ASCII folding."""

  def _make_anitype_zip_bytes(self) -> bytes:
    """Returns a ZIP with five Anitype Journal OTF faces plus macOS junk."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
      for i in range(1, 6):
        otf = _make_test_sfnt(
            b"OTTO",
            family="Anitype Journal",
            subfamily=str(i),
            codepoint_ranges=[(0x0020, 0x007E)],
            full_name=f"Anitype Journal {i}",
            ps_name=f"AnitypeJournal-{i}",
        )
        zf.writestr(f"AnitypeJournal/AnitypeJournal-{i}.otf", otf)
      # macOS finder metadata junk should be ignored safely.
      zf.writestr("__MACOSX/AnitypeJournal/._AnitypeJournal-1.otf", b"junk")
      zf.writestr("AnitypeJournal/._AnitypeJournal-2.otf", b"junk")
    return buf.getvalue()

  def test_zip_anitype_pack_detection_spanish_glyphs_and_macos_sync(
      self,
  ) -> None:
    """ZIP install groups a 5-face pack, flags glyph gaps, syncs to macOS."""
    with tempfile.TemporaryDirectory() as tmp:
      tmp_p = Path(tmp)
      fm = font_manager.FontManager(
          app_dir=tmp_p / "app",
          macos_fonts_dir=tmp_p / "macos_fonts",
      )
      res = fm.install_upload(
          "anitype_journal.zip",
          self._make_anitype_zip_bytes(),
          install_to_macos=True,
      )
      self.assertTrue(res["ok"])
      self.assertEqual(res["installed_count"], 5)
      self.assertTrue(res["installed_to_macos"])
      for i in range(1, 6):
        self.assertTrue(
            (tmp_p / f"macos_fonts/AnitypeJournal-{i}.otf").is_file()
        )
      self.assertFalse((tmp_p / "macos_fonts/._AnitypeJournal-2.otf").exists())

      packs = res["packs"]
      self.assertEqual(len(packs), 1)
      pack = packs[0]
      self.assertEqual(res["selected_pack_id"], pack["pack_id"])
      self.assertTrue(pack["is_animated_pack"])
      self.assertEqual(pack["variant_count"], 5)
      self.assertFalse(pack["supports_spanish"])
      for ch in "áéíóúüñÁÉÍÓÚÜÑ¿¡":
        self.assertIn(ch, pack["missing_spanish_chars"])
      self.assertEqual(
          [v["font_style"] for v in pack["variants"]], ["1", "2", "3", "4", "5"]
      )

  def test_trailing_index_family_and_complete_spanish_ttf(self) -> None:
    """Families ending in an index form one animated, Spanish-ready pack."""
    with tempfile.TemporaryDirectory() as tmp:
      tmp_p = Path(tmp)
      fm = font_manager.FontManager(
          app_dir=tmp_p / "app",
          macos_fonts_dir=tmp_p / "macos_fonts",
      )
      for i in (1, 2):
        ttf = _make_test_sfnt(
            b"\x00\x01\x00\x00",
            family=f"Boil Serif {i}",
            subfamily="Regular",
            codepoint_ranges=[(0x0020, 0x00FF)],
        )
        res = fm.install_upload(
            f"BoilSerif-{i}.ttf", ttf, install_to_macos=False
        )

      packs = res["packs"]
      self.assertEqual(len(packs), 1)
      self.assertTrue(packs[0]["is_animated_pack"])
      self.assertEqual(packs[0]["variant_count"], 2)
      self.assertTrue(packs[0]["supports_spanish"])
      self.assertEqual(packs[0]["missing_spanish_chars"], [])

  def test_fold_spanish_and_animated_exporters(self) -> None:
    """ASCII folding and font-cycle animation reach FCPXML slices and Lua."""
    self.assertEqual(
        exporters.fold_spanish_to_ascii("¿Qué año mágico, Pingüino? ¡Órale!"),
        "Que ano magico, Pinguino? Orale!",
    )
    cues = [
        spanish_nlp.SubtitleCue(
            index=1,
            start_ms=0,
            end_ms=500,
            lines=["¿Año nuevo", "en España!"],
        )
    ]
    style = exporters.StyleConfig.from_dict({
        "font": "Anitype Journal",
        "font_style": "1",
        "ascii_fold": True,
        "animation_mode": "font_cycle_boil",
        "anim_hold_frames": 3,
        "anim_jitter": 0.25,
        "font_variants": [
            {"font": "Anitype Journal", "font_style": str(i)}
            for i in range(1, 6)
        ],
    })
    fcpxml = exporters.export_fcpxml(
        cues, timeline_name="Anim", fps=24.0, start_frame=86400, style=style
    )
    root = ET.fromstring(fcpxml.split("<!DOCTYPE fcpxml>\n", 1)[1])
    titles = root.findall(".//title")
    # 500ms @ 24fps = 12 frames; hold=3 => 4 flipbook slices cycling faces 1..4.
    self.assertEqual(len(titles), 4)
    faces = [
        ts.attrib.get("fontFace")
        for ts in root.findall(".//text-style[@fontFace]")
    ]
    self.assertEqual(faces, ["1", "2", "3", "4"])
    for t_el in root.findall(".//text/text-style"):
      self.assertEqual(t_el.text, "Ano nuevo\nen Espana!")

    lua_code = exporters.export_resolve_lua_payload(
        cues=cues,
        mode="textplus",
        track_name="Anitype ES",
        template_name="AutoSubs_ES_Anitype_Animado",
        srt_path="/tmp/a.srt",
        srt_timeline_tc_path="/tmp/b.srt",
        fcpxml_path="/tmp/c.fcpxml",
        fps=24.0,
        timeline_start_frame=86400,
        style=style,
    )
    self.assertIn('animation_mode = "font_cycle_boil"', lua_code)
    self.assertIn("anim_hold_frames = 3", lua_code)
    self.assertIn("ascii_fold = true", lua_code)
    self.assertIn("font_variants = {", lua_code)
    self.assertIn("Ano nuevo\\nen Espana!", lua_code)

  def test_group_font_packs_variant_count_and_atomic_zip_install(self) -> None:
    """Style variants are not animated; a bad ZIP entry rolls back all."""
    faces = [
        font_manager.FontFaceInfo(
            font_id="f1",
            family="MiFuente",
            subfamily="Regular",
            full_name="MiFuente Regular",
            postscript_name="MiFuente-Regular",
            css_family="MiFuente",
            path="/tmp/MiFuente-Regular.ttf",
            filename="MiFuente-Regular.ttf",
            ext=".ttf",
            source="uploaded",
            supports_spanish=True,
            missing_spanish_chars=[],
            base_family="MiFuente",
            pack_key="mifuente",
            frame_index=None,
        ),
        font_manager.FontFaceInfo(
            font_id="f2",
            family="MiFuente",
            subfamily="Bold",
            full_name="MiFuente Bold",
            postscript_name="MiFuente-Bold",
            css_family="MiFuente",
            path="/tmp/MiFuente-Bold.ttf",
            filename="MiFuente-Bold.ttf",
            ext=".ttf",
            source="uploaded",
            supports_spanish=True,
            missing_spanish_chars=[],
            base_family="MiFuente",
            pack_key="mifuente",
            frame_index=None,
        ),
    ]
    packs = font_manager.group_font_packs(faces)
    self.assertEqual(len(packs), 1)
    self.assertFalse(packs[0].is_animated_pack)
    self.assertEqual(packs[0].variant_count, 2)

    # Invalid entry mid-ZIP must not leave partial installs on disk.
    buf = io.BytesIO()
    good_ttf = _make_test_sfnt(
        b"\x00\x01\x00\x00", "ValidFont", "Regular", [(0x0020, 0x00FF)]
    )
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("ValidFont.ttf", good_ttf)
      zf.writestr("CorruptFont.ttf", b"not-a-font-file")
    with tempfile.TemporaryDirectory() as tmp:
      app_dir = Path(tmp) / "app_fonts"
      mac_dir = Path(tmp) / "mac_fonts"
      mgr = font_manager.FontManager(app_dir, mac_dir)
      with self.assertRaises(ValueError):
        mgr.install_upload(
            "mixed_pack.zip", buf.getvalue(), install_to_macos=True
        )
      self.assertEqual(list(mgr.app_fonts_dir.glob("*")), [])
      self.assertFalse(mac_dir.exists() and any(mac_dir.iterdir()))

  def test_zip_basename_collisions_and_no_macos_overwrite(self) -> None:
    """Same-name ZIP entries get _N names; user fonts are never clobbered."""
    font_a = _make_test_sfnt(b"OTTO", "Alpha Sans", "Regular", [(0x20, 0x7E)])
    font_b = _make_test_sfnt(b"OTTO", "Beta Serif", "Regular", [(0x20, 0x7E)])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("OTF/Font.otf", font_a)
      zf.writestr("TTF/Font.otf", font_b)
      zf.writestr("copy/Font.otf", font_a)  # Byte-identical duplicate.
    with tempfile.TemporaryDirectory() as tmp:
      tmp_p = Path(tmp)
      mac_dir = tmp_p / "mac_fonts"
      mac_dir.mkdir()
      other = _make_test_sfnt(b"OTTO", "Someone Else", "Bold", [(0x20, 0x7E)])
      (mac_dir / "Font.otf").write_bytes(other)
      mgr = font_manager.FontManager(tmp_p / "app", mac_dir)
      res = mgr.install_upload("pack.zip", buf.getvalue(), True)

      self.assertEqual(res["installed_count"], 2)
      self.assertEqual((mgr.app_fonts_dir / "Font.otf").read_bytes(), font_a)
      self.assertEqual((mgr.app_fonts_dir / "Font_2.otf").read_bytes(), font_b)
      # The pre-existing, different macOS font is untouched.
      self.assertEqual((mac_dir / "Font.otf").read_bytes(), other)
      self.assertEqual((mac_dir / "Font_2.otf").read_bytes(), font_a)
      self.assertEqual((mac_dir / "Font_2_2.otf").read_bytes(), font_b)
      summary = [
          (c["location"], c["action"], c["installed_as"])
          for c in res["collisions"]
      ]
      self.assertEqual(
          summary,
          [
              ("zip", "renamed", "Font_2.otf"),
              ("zip", "skipped_identical", "Font.otf"),
              ("macos", "renamed", "Font_2.otf"),
              ("macos", "renamed", "Font_2_2.otf"),
          ],
      )

      # Re-uploading the same font finds its identical copy: nothing new.
      again = mgr.install_upload("Font.otf", font_a, install_to_macos=True)
      self.assertEqual(
          [(c["action"], c["installed_as"]) for c in again["collisions"]],
          [("skipped_identical", "Font_2.otf")],
      )
      self.assertEqual(
          sorted(p.name for p in mac_dir.iterdir()),
          ["Font.otf", "Font_2.otf", "Font_2_2.otf"],
      )

  def test_cmap_format4_rejects_declared_length_too_short(self) -> None:
    """A format 4 subtable whose length cannot hold its arrays is ignored."""
    segs = [(0x41, 0x5A), (0xFFFF, 0xFFFF)]
    body = (
        b"".join(struct.pack(">H", e) for _, e in segs)
        + struct.pack(">H", 0)
        + b"".join(struct.pack(">H", s) for s, _ in segs)
        + b"".join(struct.pack(">h", 1) for _ in segs)
        + b"".join(struct.pack(">H", 0) for _ in segs)
    )
    full_len = 16 + 8 * len(segs)

    def _subtable(length: int) -> bytes:
      """Returns the subtable with `length` written in its header."""
      header = struct.pack(">HHHHHHH", 4, length, 0, 2 * len(segs), 0, 0, 0)
      return header + body

    check = font_manager._check_cmap_format4
    self.assertEqual(check(_subtable(full_len), 0, {0x41, 0xF1}), {0x41})
    # Lengths past the end of the file are clamped, not rejected.
    self.assertEqual(check(_subtable(0xFFFF), 0, {0x41}), {0x41})
    self.assertEqual(check(_subtable(20), 0, {0x41}), set())
    self.assertEqual(check(_subtable(full_len)[:24], 0, {0x41}), set())


class BridgeAndServerIntegrationTest(unittest.TestCase):
  """End-to-end HTTP tests against a real loopback AutoSubs ES server.

  Each test gets a fresh temporary app dir (wired through the
  AUTOSUBS_* environment variables) and a server bound to an ephemeral
  port on 127.0.0.1, served from a daemon thread.

  Attributes:
    tmp_path: Root of the per-test temporary directory.
    server: The running HTTP server instance.
    state: Shared AppState backing the server (guard with state.lock).
    port: Ephemeral TCP port the server listens on.
    thread: Daemon thread running server.serve_forever.
  """

  def setUp(self) -> None:
    """Points AUTOSUBS_* dirs at a temp tree and starts a loopback server."""
    self._tmp = tempfile.TemporaryDirectory()
    self.tmp_path = Path(self._tmp.name)
    self._orig_home = os.environ.get("AUTOSUBS_ES_HOME")
    self._orig_fusion = os.environ.get("AUTOSUBS_RESOLVE_FUSION_DIR")
    self._orig_fonts = os.environ.get("AUTOSUBS_USER_FONTS_DIR")
    os.environ["AUTOSUBS_ES_HOME"] = str(self.tmp_path / "app")
    os.environ["AUTOSUBS_RESOLVE_FUSION_DIR"] = str(self.tmp_path / "fusion")
    os.environ["AUTOSUBS_USER_FONTS_DIR"] = str(self.tmp_path / "macos_fonts")

    self.server, self.state = autosubs_server.create_server(
        host="127.0.0.1", port=0, app_dir=self.tmp_path / "app"
    )
    self.port = self.server.server_address[1]
    # A short poll interval keeps tearDown fast: shutdown() waits up to one
    # poll (0.5 s by default) for serve_forever to notice, once per test.
    self.thread = threading.Thread(
        target=self.server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    self.thread.start()

  def tearDown(self) -> None:
    """Stops the server, restores AUTOSUBS_* env vars, removes temp files."""
    self.server.shutdown()
    self.server.server_close()
    if self._orig_home is None:
      os.environ.pop("AUTOSUBS_ES_HOME", None)
    else:
      os.environ["AUTOSUBS_ES_HOME"] = self._orig_home
    if self._orig_fusion is None:
      os.environ.pop("AUTOSUBS_RESOLVE_FUSION_DIR", None)
    else:
      os.environ["AUTOSUBS_RESOLVE_FUSION_DIR"] = self._orig_fusion
    if self._orig_fonts is None:
      os.environ.pop("AUTOSUBS_USER_FONTS_DIR", None)
    else:
      os.environ["AUTOSUBS_USER_FONTS_DIR"] = self._orig_fonts
    self._tmp.cleanup()

  def _request(
      self,
      path: str,
      method: str = "GET",
      body: bytes | dict | None = None,
      headers: dict[str, str] | None = None,
  ) -> tuple[int, dict[str, str], bytes]:
    """Sends an HTTP request to the test server.

    Args:
      path: URL path (plus query) relative to the server root.
      method: HTTP method.
      body: Raw bytes, a dict sent as JSON, or None for no body.
      headers: Extra request headers (e.g. Range, Origin, X-Filename).

    Returns:
      Tuple of (status code, response headers, response body bytes).

    Raises:
      urllib.error.HTTPError: For any 4xx/5xx response.
    """
    url = f"http://127.0.0.1:{self.port}{path}"
    hdrs = dict(headers or {})
    raw_body: bytes | None = None
    if isinstance(body, dict):
      raw_body = json.dumps(body).encode("utf-8")
      hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(body, bytes):
      raw_body = body
    req = urllib.request.Request(
        url, data=raw_body, headers=hdrs, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
      return resp.status, dict(resp.headers.items()), resp.read()

  # --- Studio workflow: demo -> resegment -> edit -> export/push ---
  #
  # The studio steps are sequential (exports read the edited cues), so the
  # helpers below replay the earlier steps without asserting, letting each
  # focused test start from the state it needs.

  def _studio_load_demo(self) -> dict[str, Any]:
    """Loads the Spanish demo session via POST /api/demo.

    Returns:
      The decoded JSON response (`ok` flag plus `session`).
    """
    _, _, demo_bytes = self._request("/api/demo", method="POST", body={})
    return json.loads(demo_bytes.decode("utf-8"))

  def _studio_resegment_reels(self) -> list[dict[str, Any]]:
    """Loads the demo, then re-segments it with the Reels 9:16 preset.

    Muletillas are removed and inverted punctuation is fixed, as the studio's
    instant RAE re-segmentation does.

    Returns:
      The re-segmented session cues as dicts.
    """
    self._studio_load_demo()
    _, _, reseg_bytes = self._request(
        "/api/resegment",
        method="POST",
        body={
            "format_config": {
                "preset_id": "reels_9_16",
                "remove_muletillas": True,
                "fix_inverted_punctuation": True,
            }
        },
    )
    return json.loads(reseg_bytes.decode("utf-8"))["session"]["cues"]

  def _studio_edit_first_cue(self) -> list[dict[str, Any]]:
    """Runs demo + re-segment, then rewrites cue 0 via PUT /api/cues.

    Cue 0 becomes "Hola,\\nAutoSubs ES", which the exporters must reflect.

    Returns:
      The session cues returned by the server after the edit.
    """
    reseg_cues = self._studio_resegment_reels()
    reseg_cues[0]["text"] = "Hola,\nAutoSubs ES"
    reseg_cues[0]["lines"] = ["Hola,", "AutoSubs ES"]
    _, _, put_bytes = self._request(
        "/api/cues", method="PUT", body={"cues": reseg_cues}
    )
    return json.loads(put_bytes.decode("utf-8"))["session"]["cues"]

  def test_studio_serves_index_html_with_branding(self) -> None:
    """GET / serves the studio HTML with MTTN / La Matatena branding."""
    st_html, _, html = self._request("/")
    self.assertEqual(st_html, 200)
    html_txt = html.decode("utf-8")
    self.assertIn("MTTN", html_txt)
    self.assertIn("AutoSubs Tool", html_txt)
    self.assertIn("La Matatena", html_txt)

  def test_studio_serves_css_with_theme_support(self) -> None:
    """GET /styles.css serves the brand palette and the theme toggle."""
    st_css, _, css = self._request("/styles.css")
    self.assertEqual(st_css, 200)
    css_str = css.decode("utf-8")
    self.assertIn("#8367c7", css_str)
    self.assertIn("color-scheme", css_str)
    self.assertIn("theme-toggle-btn", css_str)

  def test_studio_status_reports_hardware_profile(self) -> None:
    """GET /api/status reports the detected hardware profile ID."""
    st_status, _, status_bytes = self._request("/api/status")
    self.assertEqual(st_status, 200)
    status_json = json.loads(status_bytes.decode("utf-8"))
    self.assertEqual(
        status_json["environment"]["hardware_profile"]["profile_id"],
        hardware_imac2017.detect_hardware_profile().profile_id,
    )

  def test_studio_demo_loads_cues_and_waveform(self) -> None:
    """POST /api/demo loads at least 4 cues and a dense waveform."""
    demo_json = self._studio_load_demo()
    self.assertTrue(demo_json["ok"])
    self.assertGreaterEqual(demo_json["session"]["cue_count"], 4)
    self.assertGreater(len(demo_json["session"]["peaks"]), 50)

  def test_studio_demo_media_serves_range_206(self) -> None:
    """GET /api/media honors a Range header with 206 + Content-Range."""
    self._studio_load_demo()
    st_media, hdrs_media, chunk = self._request(
        "/api/media", headers={"Range": "bytes=0-63"}
    )
    self.assertEqual(st_media, 206)
    self.assertEqual(len(chunk), 64)
    self.assertTrue(
        hdrs_media.get("Content-Range", "").startswith("bytes 0-63/")
    )

  def test_studio_resegment_reels_adds_inverted_punctuation(self) -> None:
    """Reels 9:16 re-segmentation yields short cues with ¿ and ¡ added."""
    reseg_cues = self._studio_resegment_reels()
    self.assertGreater(len(reseg_cues), 6)
    joined_all = " ".join(c["text"] for c in reseg_cues)
    self.assertIn("¿Sabías", joined_all)
    self.assertIn("¡Qué", joined_all)

  def test_studio_put_cues_persists_edit(self) -> None:
    """PUT /api/cues stores edited cue text in the session."""
    put_cues = self._studio_edit_first_cue()
    self.assertEqual(put_cues[0]["text"], "Hola,\nAutoSubs ES")

  def test_studio_exports_every_format_after_edit(self) -> None:
    """POST /api/export renders SRT/ASS/TXT/TXT-TC/FCPXML from edited cues."""
    self._studio_edit_first_cue()
    for fmt, needle in (
        ("srt", "-->"),
        ("ass", "[Events]"),
        ("txt", "AutoSubs ES"),
        ("txt_tc", "[00:00:0"),
        ("fcpxml", '<fcpxml version="1.9">'),
    ):
      with self.subTest(fmt=fmt):
        _, _, exp_bytes = self._request(
            "/api/export", method="POST", body={"format": fmt}
        )
        exp_json = json.loads(exp_bytes.decode("utf-8"))
        self.assertIn(needle, exp_json["content"])

  def test_studio_resolve_push_writes_outbox_files(self) -> None:
    """POST /api/resolve/push writes every outbox file; table is bare {...}."""
    self._studio_edit_first_cue()
    _, _, push_bytes = self._request(
        "/api/resolve/push",
        method="POST",
        body={"mode": "textplus", "track_name": "Subtítulos ES"},
    )
    push_json = json.loads(push_bytes.decode("utf-8"))
    self.assertTrue(push_json["ok"])
    for key in (
        "srt",
        "srt_resolve_tc",
        "ass",
        "fcpxml",
        "lua_payload",
        "lua_table",
    ):
      with self.subTest(file=key):
        self.assertTrue(Path(push_json["files"][key]).is_file())
    push_table_txt = Path(push_json["files"]["lua_table"]).read_text(
        encoding="utf-8"
    )
    self.assertTrue(push_table_txt.lstrip().startswith("{"))
    self.assertFalse(push_table_txt.lstrip().startswith("return"))

  def test_studio_resolve_jump_writes_jump_files(self) -> None:
    """POST /api/resolve/jump writes jump_tc.lua and a bare {...} table."""
    # The jump route only reads timeline metadata, never the session cues,
    # so it needs no demo/edit setup.
    _, _, jump_bytes = self._request(
        "/api/resolve/jump", method="POST", body={"ms": 4500}
    )
    jump_json = json.loads(jump_bytes.decode("utf-8"))
    self.assertTrue(jump_json["ok"])
    self.assertTrue((self.tmp_path / "app/bridge/outbox/jump_tc.lua").is_file())
    jump_table_txt = (
        self.tmp_path / "app/bridge/outbox/jump_tc.table"
    ).read_text(encoding="utf-8")
    self.assertTrue(jump_table_txt.lstrip().startswith("{"))

  def test_studio_upload_decodes_filename_and_flags_audio(self) -> None:
    """POST /api/upload over a demo session decodes %-names; WAV is audio."""
    self._studio_load_demo()
    sample_wav = self.tmp_path / "sample.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=1200)
    wav_bytes = sample_wav.read_bytes()
    st_up, _, up_bytes = self._request(
        "/api/upload",
        method="POST",
        body=wav_bytes,
        headers={"X-Filename": "mi%20clip%20espa%C3%B1ol.wav"},
    )
    self.assertEqual(st_up, 200)
    up_json = json.loads(up_bytes.decode("utf-8"))
    self.assertEqual(up_json["filename"], "mi_clip_español.wav")
    self.assertTrue(Path(up_json["media_path"]).is_file())
    self.assertFalse(up_json["session"]["is_video"])

  def test_guard_rejects_cross_origin_mutating_post(self) -> None:
    """A cross-origin mutating POST is blocked with 403."""
    with self.assertRaises(urllib.error.HTTPError) as ctx_origin:
      self._request(
          "/api/demo",
          method="POST",
          body={},
          headers={"Origin": "https://evil.example"},
      )
    self.assertEqual(ctx_origin.exception.code, 403)
    ctx_origin.exception.close()

  def test_guard_rejects_disallowed_host_header(self) -> None:
    """A request with a non-loopback Host header is blocked with 403."""
    with self.assertRaises(urllib.error.HTTPError) as ctx_host:
      self._request("/api/status", headers={"Host": "evil.example:55120"})
    self.assertEqual(ctx_host.exception.code, 403)
    ctx_host.exception.close()

  def test_transcribe_rejects_media_path_outside_app_dir(self) -> None:
    """/api/transcribe refuses a filesystem media_path outside app_dir."""
    models_dir = self.tmp_path / "app/models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "ggml-large-v3-turbo-q8_0.bin").write_bytes(
        b"0" * (2 * 1024 * 1024)
    )
    outside_wav = self.tmp_path / "outside.wav"
    resolve_bridge.generate_demo_wav(str(outside_wav), duration_ms=500)
    orig_find = hardware_imac2017.find_binary
    try:
      hardware_imac2017.find_binary = (
          lambda cands: "/bin/echo"
          if "whisper-cli" in cands
          else orig_find(cands)
      )
      with self.assertRaises(urllib.error.HTTPError) as ctx_path:
        self._request(
            "/api/transcribe",
            method="POST",
            body={"source": "uploaded", "media_path": str(outside_wav)},
        )
      self.assertEqual(ctx_path.exception.code, 400)
      err_payload = json.loads(ctx_path.exception.read().decode("utf-8"))
      self.assertIn("fuera", err_payload["error"].lower())
      ctx_path.exception.close()
    finally:
      hardware_imac2017.find_binary = orig_find

  def test_upload_clears_demo_cues_and_bumps_media_rev(self) -> None:
    """Uploading new media clears stale demo cues and bumps media_rev."""
    _, _, demo_bytes = self._request("/api/demo", method="POST", body={})
    demo_sess = json.loads(demo_bytes.decode("utf-8"))["session"]
    self.assertGreater(demo_sess["cue_count"], 0)
    sample_wav = self.tmp_path / "fresh.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=900)
    _, _, up_bytes = self._request(
        "/api/upload",
        method="POST",
        body=sample_wav.read_bytes(),
        headers={"X-Filename": "fresh.wav"},
    )
    up_sess = json.loads(up_bytes.decode("utf-8"))["session"]
    self.assertEqual(up_sess["cue_count"], 0)
    self.assertNotEqual(up_sess["media_rev"], demo_sess["media_rev"])

  def test_exported_video_streams_range_206(self) -> None:
    """The exported MP4 streams via HTTP 206 Range instead of a full read."""
    fake_mp4 = self.tmp_path / "app/cache/fake_out.mp4"
    fake_mp4.write_bytes(b"0123456789ABCDEF" * 8)
    with self.state.lock:
      self.state.exported_video_path = fake_mp4
    st_vid, hdrs_vid, vid_chunk = self._request(
        "/api/exported-video", headers={"Range": "bytes=0-31"}
    )
    self.assertEqual(st_vid, 206)
    self.assertEqual(len(vid_chunk), 32)
    self.assertTrue(
        hdrs_vid.get("Content-Range", "").startswith("bytes 0-31/128")
    )

  def test_standalone_cli_transcription_with_boardgame_domain(self) -> None:
    """--cli runs a stub whisper-cli, repairs jargon, and cleans cache."""
    fake_bin_dir = self.tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_whisper = fake_bin_dir / "whisper-cli"
    fake_whisper.write_text(
        """#!/bin/sh
out_prefix=""
prev=""
for arg in "$@"; do
  if [ "$arg" = "--help" ]; then
    echo "  --carry-initial-prompt  carry prompt"
    exit 0
  fi
  if [ "$prev" = "-of" ]; then
    out_prefix="$arg"
  fi
  prev="$arg"
done
cat << 'EOF' > "${out_prefix}.json"
{
  "transcription": [{
    "offsets": {"from": 0, "to": 2800},
    "text": " Repartimos los mipels del dec bilding.",
    "tokens": [
      {"text": " Repartimos", "offsets": {"from": 100, "to": 600}, "p": 0.95},
      {"text": " los", "offsets": {"from": 620, "to": 800}, "p": 0.95},
      {"text": " mipels", "offsets": {"from": 820, "to": 1300}, "p": 0.91},
      {"text": " del", "offsets": {"from": 1320, "to": 1500}, "p": 0.96},
      {"text": " dec", "offsets": {"from": 1520, "to": 1800}, "p": 0.90},
      {"text": " bilding.", "offsets": {"from": 1820, "to": 2400}, "p": 0.90}
    ]
  }]
}
EOF
""",
        encoding="utf-8",
    )
    fake_whisper.chmod(0o755)

    models_dir = self.tmp_path / "app/models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "ggml-large-v3-turbo-q8_0.bin").write_bytes(
        b"0" * (2 * 1024 * 1024)
    )
    sample_wav = self.tmp_path / "cli_boardgame.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=3000)
    out_srt = self.tmp_path / "cli_out.srt"

    orig_path = os.environ.get("PATH", "")
    orig_find = hardware_imac2017.find_binary
    try:
      os.environ["PATH"] = f"{fake_bin_dir}:{orig_path}"
      hardware_imac2017.find_binary = (
          lambda cands: str(fake_whisper)
          if "whisper-cli" in cands
          else orig_find(cands)
      )
      rc = autosubs_server.run_cli_transcription(
          media_file=str(sample_wav),
          output_path=str(out_srt),
          fmt="srt",
          model_id="turbo-q8",
          domain="juegos_mesa",
          app_dir=self.tmp_path / "app",
      )
    finally:
      hardware_imac2017.find_binary = orig_find
      os.environ["PATH"] = orig_path

    self.assertEqual(rc, 0)
    self.assertEqual(list((self.tmp_path / "app/cache").glob("cli_*")), [])
    srt_text = out_srt.read_text(encoding="utf-8")
    self.assertIn("meeples", srt_text)
    self.assertIn("deckbuilding.", srt_text)

  def test_font_upload_serving_and_anitype_resolve_push(self) -> None:
    """Uploaded font packs are listed, served, and drive an animated push."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
      for i in range(1, 6):
        zf.writestr(
            f"AnitypeJournal-{i}.otf",
            _make_test_sfnt(
                b"OTTO",
                family="Anitype Journal",
                subfamily=str(i),
                codepoint_ranges=[(0x0020, 0x007E)],
            ),
        )
    st_up, _, up_bytes = self._request(
        "/api/fonts/upload",
        method="POST",
        body=buf.getvalue(),
        headers={"X-Filename": "anitype_journal.zip", "X-Install-Macos": "1"},
    )
    self.assertEqual(st_up, 200)
    up_json = json.loads(up_bytes.decode("utf-8"))
    self.assertEqual(up_json["installed_count"], 5)
    self.assertTrue(
        (self.tmp_path / "macos_fonts/AnitypeJournal-1.otf").is_file()
    )

    st_list, _, list_bytes = self._request("/api/fonts")
    self.assertEqual(st_list, 200)
    list_json = json.loads(list_bytes.decode("utf-8"))
    self.assertEqual(len(list_json["packs"]), 1)
    first_face = list_json["custom_faces"][0]

    st_font, hdrs_font, font_bytes = self._request(first_face["url"])
    self.assertEqual(st_font, 200)
    self.assertEqual(hdrs_font.get("Content-Type"), "font/otf")
    self.assertTrue(font_bytes.startswith(b"OTTO"))

    self._request("/api/demo", method="POST", body={})
    _, _, push_bytes = self._request(
        "/api/resolve/push",
        method="POST",
        body={
            "mode": "textplus",
            "track_name": "Anitype ES",
            "template_name": "AutoSubs_ES_Anitype_Animado",
            "style": {
                "font": "Anitype Journal",
                "font_style": "1",
                "ascii_fold": True,
                "animation_mode": "font_cycle_boil",
                "anim_hold_frames": 3,
                "anim_jitter": 0.2,
                "font_variants": list_json["packs"][0]["variants"],
            },
        },
    )
    push_json = json.loads(push_bytes.decode("utf-8"))
    self.assertTrue(push_json["ok"])
    lua_txt = Path(push_json["files"]["lua_payload"]).read_text(
        encoding="utf-8"
    )
    self.assertIn('template_name = "AutoSubs_ES_Anitype_Animado"', lua_txt)
    self.assertIn("ascii_fold = true", lua_txt)
    self.assertIn("Sabias", lua_txt)

  def test_notify_export_skips_unchanged_and_upload_decodes_filename(
      self,
  ) -> None:
    """notify-export skips unchanged WAVs; upload decodes %-encoded names."""
    inbox_wav = self.state.bridge.inbox_dir / "timeline_export.wav"
    inbox_wav.parent.mkdir(parents=True, exist_ok=True)
    resolve_bridge.generate_demo_wav(str(inbox_wav), duration_ms=800)

    call_count = 0
    orig_extract = resolve_bridge.extract_waveform_peaks

    def _counting_extract(path: str, num_buckets: int | None = None):
      """Counts calls, then delegates to the real extract_waveform_peaks."""
      nonlocal call_count
      call_count += 1
      return orig_extract(path, num_buckets=num_buckets)

    resolve_bridge.extract_waveform_peaks = _counting_extract
    try:
      st1, _, b1 = self._request(
          "/api/resolve/notify-export", method="POST", body={}
      )
      self.assertEqual(st1, 200)
      j1 = json.loads(b1.decode("utf-8"))
      self.assertEqual(call_count, 1)
      self.assertGreater(j1["session"]["duration_ms"], 0)
      rev1 = j1["session"]["media_rev"]

      st2, _, b2 = self._request(
          "/api/resolve/notify-export", method="POST", body={}
      )
      self.assertEqual(st2, 200)
      j2 = json.loads(b2.decode("utf-8"))
      self.assertEqual(call_count, 1)
      self.assertEqual(j2["session"]["media_rev"], rev1)
    finally:
      resolve_bridge.extract_waveform_peaks = orig_extract

    sample_wav = self.tmp_path / "url_encoded.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=500)
    st_up, _, up_bytes = self._request(
        "/api/upload",
        method="POST",
        body=sample_wav.read_bytes(),
        headers={"X-Filename": "v%C3%ADdeo%20final%20%28es%29.wav"},
    )
    self.assertEqual(st_up, 200)
    up_json = json.loads(up_bytes.decode("utf-8"))
    self.assertEqual(up_json["filename"], "vídeo_final__es_.wav")

  # --- Robustness regression tests (server bug-fix round) ---

  def _raw_request(
      self,
      method: str,
      path: str,
      headers: dict[str, str],
      body: bytes = b"",
  ) -> tuple[int, dict[str, Any]]:
    """Sends a request with verbatim headers (e.g. a bogus Content-Length).

    urllib always computes Content-Length itself, so http.client is used to
    exercise malformed headers.

    Args:
      method: HTTP method.
      path: URL path.
      headers: Headers sent exactly as given.
      body: Raw body bytes sent after the headers.

    Returns:
      Tuple of (status code, parsed JSON body or {}).
    """
    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
    try:
      conn.putrequest(method, path)
      for key, value in headers.items():
        conn.putheader(key, value)
      conn.endheaders()
      if body:
        conn.send(body)
      resp = conn.getresponse()
      raw = resp.read()
      return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    finally:
      conn.close()

  def _expect_http_error(
      self, path: str, method: str, body: bytes | dict | None, code: int
  ) -> dict[str, Any]:
    """Asserts that a request fails with `code` and returns its JSON body."""
    with self.assertRaises(urllib.error.HTTPError) as ctx:
      self._request(path, method=method, body=body)
    try:
      self.assertEqual(ctx.exception.code, code)
      raw = ctx.exception.read()
      return json.loads(raw.decode("utf-8")) if raw else {}
    finally:
      ctx.exception.close()

  def _install_fake_whisper(self, sleep_s: float) -> Path:
    """Creates a stub whisper-cli that sleeps, then writes a tiny OJF JSON.

    Also writes a dummy turbo-q8 model so validation passes.

    Args:
      sleep_s: Seconds the stub sleeps, keeping its job in "running".

    Returns:
      Path to the executable stub.
    """
    fake_bin_dir = self.tmp_path / "bin"
    fake_bin_dir.mkdir(exist_ok=True)
    fake_whisper = fake_bin_dir / "whisper-cli"
    fake_whisper.write_text(
        f"""#!/bin/sh
out_prefix=""
prev=""
for arg in "$@"; do
  if [ "$arg" = "--help" ]; then
    exit 0
  fi
  if [ "$prev" = "-of" ]; then
    out_prefix="$arg"
  fi
  prev="$arg"
done
sleep {sleep_s}
cat << 'EOF' > "${{out_prefix}}.json"
{{"transcription": [{{
  "offsets": {{"from": 0, "to": 1500}},
  "text": " Hola a todos.",
  "tokens": [
    {{"text": " Hola", "offsets": {{"from": 100, "to": 500}}, "p": 0.95}},
    {{"text": " a", "offsets": {{"from": 520, "to": 700}}, "p": 0.95}},
    {{"text": " todos.", "offsets": {{"from": 720, "to": 1300}}, "p": 0.95}}
  ]
}}]}}
EOF
""",
        encoding="utf-8",
    )
    fake_whisper.chmod(0o755)
    models_dir = self.tmp_path / "app/models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "ggml-large-v3-turbo-q8_0.bin").write_bytes(
        b"0" * (2 * 1024 * 1024)
    )
    return fake_whisper

  def _wait_job(self, job_id: str, timeout_s: float = 20.0) -> dict[str, Any]:
    """Polls GET /api/jobs/<id> until the job leaves "running"."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
      _, _, raw = self._request(f"/api/jobs/{job_id}")
      job = json.loads(raw.decode("utf-8"))
      if job["status"] != "running":
        return job
      time.sleep(0.05)
    self.fail(f"Job {job_id} did not finish in {timeout_s} s")

  def _wait_transcription_slot_free(self) -> None:
    """Waits until the transcription worker's finally block has run."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
      with self.state.lock:
        if self.state._active_transcription is None:
          return
      time.sleep(0.02)
    self.fail("Transcription dedupe slot was never released")

  def test_upload_and_notify_survive_undecodable_wav_peaks(self) -> None:
    """A WAV the wave module rejects (32-bit float) still activates."""

    def _raise_wave_error(path: str, num_buckets: int | None = None):
      """Simulates extract_waveform_peaks on a 32-bit float WAV."""
      raise wave.Error("unknown format: 3")

    inbox_wav = self.state.bridge.inbox_dir / "timeline_export.wav"
    inbox_wav.parent.mkdir(parents=True, exist_ok=True)
    resolve_bridge.generate_demo_wav(str(inbox_wav), duration_ms=500)
    sample_wav = self.tmp_path / "float32.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=500)
    orig_extract = resolve_bridge.extract_waveform_peaks
    resolve_bridge.extract_waveform_peaks = _raise_wave_error
    try:
      st_up, _, up_bytes = self._request(
          "/api/upload",
          method="POST",
          body=sample_wav.read_bytes(),
          headers={"X-Filename": "float32.wav"},
      )
      self.assertEqual(st_up, 200)
      up_json = json.loads(up_bytes.decode("utf-8"))
      self.assertEqual(up_json["duration_ms"], 0)
      self.assertTrue(up_json["session"]["has_media"])
      self.assertEqual(up_json["session"]["media_name"], "float32.wav")
      self.assertEqual(up_json["session"]["peaks"], [])

      st_n, _, n_bytes = self._request(
          "/api/resolve/notify-export", method="POST", body={}
      )
      self.assertEqual(st_n, 200)
      n_sess = json.loads(n_bytes.decode("utf-8"))["session"]
      self.assertTrue(n_sess["has_media"])
      self.assertEqual(n_sess["media_name"], "timeline_export.wav")
      self.assertEqual(n_sess["peaks"], [])
    finally:
      resolve_bridge.extract_waveform_peaks = orig_extract

  def test_upload_storage_cleanup_size_cap_and_single_decode(self) -> None:
    """Uploads go to uploads/, replace old ones, are capped, decoded once."""
    self._request("/api/demo", method="POST", body={})
    cache_dir = self.tmp_path / "app/cache"
    demo_wav = cache_dir / "demo_autosubs_es.wav"
    demo_bytes = demo_wav.read_bytes()
    inbox_wav = self.state.bridge.inbox_dir / "timeline_export.wav"
    inbox_wav.parent.mkdir(parents=True, exist_ok=True)
    resolve_bridge.generate_demo_wav(str(inbox_wav), duration_ms=400)
    sample_wav = self.tmp_path / "clip.wav"
    resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=500)
    wav_bytes = sample_wav.read_bytes()

    # A name equal to an internal cache file must not overwrite it.
    _, _, b1 = self._request(
        "/api/upload",
        method="POST",
        body=wav_bytes,
        headers={"X-Filename": "demo_autosubs_es.wav"},
    )
    j1 = json.loads(b1.decode("utf-8"))
    self.assertEqual(j1["filename"], "demo_autosubs_es.wav")
    first_upload = Path(j1["media_path"])
    self.assertEqual(first_upload.parent, cache_dir / "uploads")
    self.assertEqual(demo_wav.read_bytes(), demo_bytes)

    # "%2541" decodes once to "%41" (sanitized to "_41"), never to "A".
    _, _, b2 = self._request(
        "/api/upload",
        method="POST",
        body=wav_bytes,
        headers={"X-Filename": "100%2541.wav"},
    )
    j2 = json.loads(b2.decode("utf-8"))
    self.assertEqual(j2["filename"], "100_41.wav")
    self.assertFalse(first_upload.exists())
    self.assertTrue(Path(j2["media_path"]).is_file())
    self.assertTrue(inbox_wav.is_file())
    self.assertTrue(demo_wav.is_file())
    self.assertEqual(
        [p.name for p in (cache_dir / "uploads").iterdir()], ["100_41.wav"]
    )

    # ".." must not resolve to a directory outside uploads/.
    _, _, b3 = self._request(
        "/api/upload",
        method="POST",
        body=wav_bytes,
        headers={"X-Filename": "%2E%2E"},
    )
    self.assertEqual(
        json.loads(b3.decode("utf-8"))["filename"], "media_upload.bin"
    )

    orig_max = autosubs_server._MAX_UPLOAD_BYTES
    autosubs_server._MAX_UPLOAD_BYTES = 16
    try:
      err = self._expect_http_error("/api/upload", "POST", wav_bytes, 413)
      self.assertIn("límite", err["error"])
    finally:
      autosubs_server._MAX_UPLOAD_BYTES = orig_max

  def test_malformed_requests_return_400(self) -> None:
    """Bad Content-Length, non-UTF-8 JSON and bad numbers give JSON 400s."""
    self._request("/api/demo", method="POST", body={})
    for path in ("/api/demo", "/api/fonts/upload", "/api/upload"):
      status, payload = self._raw_request(
          "POST", path, {"Content-Length": "abc"}
      )
      self.assertEqual(status, 400, path)
      self.assertIn("Content-Length", payload["error"])
    status, payload = self._raw_request(
        "POST",
        "/api/demo",
        {"Content-Length": "2", "Content-Type": "application/json"},
        body=b"\xff\xfe",
    )
    self.assertEqual(status, 400)
    self.assertIn("UTF-8", payload["error"])

    for body in (
        {"format": "srt", "fps": "abc"},
        {"format": "srt", "fps": float("inf")},
        {"format": "srt", "timeline_start_frame": "x"},
    ):
      self._expect_http_error("/api/export", "POST", body, 400)
    for body in ({"ms": "abc"}, {"ms": None}):
      self._expect_http_error("/api/resolve/jump", "POST", body, 400)
    with self.state.lock:
      cues_before = list(self.state.cues)
    for body in (
        {"cues": [1]},
        {"cues": [{"start_ms": "x"}]},
        {"cues": [{"start_ms": 0, "words": ["nope"]}]},
    ):
      self._expect_http_error("/api/cues", "PUT", body, 400)
    with self.state.lock:
      self.assertEqual(self.state.cues, cues_before)

  def test_cancel_only_affects_active_jobs(self) -> None:
    """Cancelling a finished job keeps its status; running jobs cancel."""
    done_id = self.state.create_job("burnin")
    self.state.update_job(done_id, status="done", result={"x": 1})
    _, _, raw = self._request(
        f"/api/jobs/{done_id}/cancel", method="POST", body={}
    )
    self.assertEqual(json.loads(raw), {"ok": True, "status": "done"})
    with self.state.lock:
      self.assertEqual(self.state.jobs[done_id]["status"], "done")
      self.assertEqual(self.state.jobs[done_id]["result"], {"x": 1})

    run_id = self.state.create_job("burnin")
    _, _, raw = self._request(
        f"/api/jobs/{run_id}/cancel", method="POST", body={}
    )
    self.assertEqual(json.loads(raw), {"ok": True, "status": "cancelled"})
    _, _, raw = self._request("/api/jobs/nope/cancel", method="POST", body={})
    self.assertEqual(json.loads(raw), {"ok": False, "status": None})

  def test_transcription_and_benchmark_are_deduplicated(self) -> None:
    """A second Transcribir/benchmark click joins the in-flight job."""
    fake_whisper = self._install_fake_whisper(sleep_s=1)
    inbox_wav = self.state.bridge.inbox_dir / "timeline_export.wav"
    inbox_wav.parent.mkdir(parents=True, exist_ok=True)
    resolve_bridge.generate_demo_wav(str(inbox_wav), duration_ms=1500)
    self.state._make_benchmark_wav = (  # Skip macOS `say` (slow).
        lambda out_wav, ffmpeg_bin: (
            resolve_bridge.generate_demo_wav(str(out_wav), duration_ms=500),
            0.5,
        )[1]
    )
    orig_find = hardware_imac2017.find_binary
    hardware_imac2017.find_binary = lambda cands: (
        str(fake_whisper)
        if "whisper-cli" in cands
        else None
        if "ffmpeg" in cands
        else orig_find(cands)
    )
    try:
      body = {"source": "inbox", "model_id": "turbo-q8"}
      _, _, r1 = self._request("/api/transcribe", method="POST", body=body)
      _, _, r2 = self._request("/api/transcribe", method="POST", body=body)
      id1 = json.loads(r1)["job_id"]
      self.assertEqual(json.loads(r2)["job_id"], id1)
      self.assertEqual(self._wait_job(id1)["status"], "done")
      self._wait_transcription_slot_free()

      bench = {"model_id": "turbo-q8"}
      _, _, b1 = self._request("/api/benchmark", method="POST", body=bench)
      _, _, b2 = self._request("/api/benchmark", method="POST", body=bench)
      bench_id = json.loads(b1)["job_id"]
      self.assertEqual(json.loads(b2)["job_id"], bench_id)
      self.assertEqual(self._wait_job(bench_id)["status"], "done")
    finally:
      hardware_imac2017.find_binary = orig_find

  def test_inbox_transcription_survives_notify_and_stale_is_flagged(
      self,
  ) -> None:
    """notify-export keeps inbox cues; a superseded job reports stale."""
    fake_whisper = self._install_fake_whisper(sleep_s=0.5)
    inbox_wav = self.state.bridge.inbox_dir / "timeline_export.wav"
    inbox_wav.parent.mkdir(parents=True, exist_ok=True)
    resolve_bridge.generate_demo_wav(str(inbox_wav), duration_ms=1500)
    orig_find = hardware_imac2017.find_binary
    hardware_imac2017.find_binary = lambda cands: (
        str(fake_whisper)
        if "whisper-cli" in cands
        else None
        if "ffmpeg" in cands
        else orig_find(cands)
    )
    body = {"source": "inbox", "model_id": "turbo-q8"}
    try:
      _, _, r1 = self._request("/api/transcribe", method="POST", body=body)
      job = self._wait_job(json.loads(r1)["job_id"])
      self.assertEqual(job["status"], "done")
      self.assertFalse(job["result"]["stale"])
      self.assertGreater(job["result"]["cue_count"], 0)
      self._wait_transcription_slot_free()

      _, _, n_bytes = self._request(
          "/api/resolve/notify-export", method="POST", body={}
      )
      n_sess = json.loads(n_bytes.decode("utf-8"))["session"]
      self.assertEqual(n_sess["cue_count"], job["result"]["cue_count"])

      # Media replaced while the job runs -> result discarded and flagged.
      _, _, r2 = self._request("/api/transcribe", method="POST", body=body)
      sample_wav = self.tmp_path / "otra.wav"
      resolve_bridge.generate_demo_wav(str(sample_wav), duration_ms=400)
      self._request(
          "/api/upload",
          method="POST",
          body=sample_wav.read_bytes(),
          headers={"X-Filename": "otra.wav"},
      )
      stale_job = self._wait_job(json.loads(r2)["job_id"])
      self.assertEqual(stale_job["status"], "done")
      self.assertTrue(stale_job["result"]["stale"])
      self.assertEqual(stale_job["result"]["media_name"], "otra.wav")
      self._wait_transcription_slot_free()
      with self.state.lock:
        self.assertEqual(self.state.cues, [])
      # The superseded working WAVs were cleaned out of the cache.
      self.assertEqual(
          list((self.tmp_path / "app/cache").glob("working_*_16k.wav")), []
      )
      self.assertTrue(inbox_wav.is_file())
    finally:
      hardware_imac2017.find_binary = orig_find

  def test_put_cues_rejects_stale_media_rev(self) -> None:
    """PUT /api/cues with an outdated media_rev returns 409, cues intact."""
    _, _, demo_bytes = self._request("/api/demo", method="POST", body={})
    sess = json.loads(demo_bytes.decode("utf-8"))["session"]
    cues = sess["cues"]
    cues[0]["text"] = "Editado"
    cues[0]["lines"] = ["Editado"]

    _, _, ok_bytes = self._request(
        "/api/cues",
        method="PUT",
        body={"cues": cues, "media_rev": sess["media_rev"]},
    )
    ok_sess = json.loads(ok_bytes.decode("utf-8"))["session"]
    self.assertEqual(ok_sess["cues"][0]["text"], "Editado")

    cues[0]["text"] = "No debe aplicarse"
    cues[0]["lines"] = ["No debe aplicarse"]
    err = self._expect_http_error(
        "/api/cues", "PUT", {"cues": cues, "media_rev": "otro:1:2"}, 409
    )
    self.assertIn("error", err)
    self.assertEqual(err["session"]["cues"][0]["text"], "Editado")
    self.assertIsNone(err["session"]["peaks"])

    # Backward compatible: no media_rev -> no check.
    _, _, legacy = self._request("/api/cues", method="PUT", body={"cues": cues})
    self.assertEqual(
        json.loads(legacy)["session"]["cues"][0]["text"], "No debe aplicarse"
    )

  def test_unsatisfiable_range_returns_416(self) -> None:
    """Valid-but-unsatisfiable ranges get 416; invalid syntax gets 200."""
    self._request("/api/demo", method="POST", body={})
    total = (self.tmp_path / "app/cache/demo_autosubs_es.wav").stat().st_size
    for rng in (f"bytes={total}-", "bytes=-0"):
      with self.assertRaises(urllib.error.HTTPError) as ctx:
        self._request("/api/media", headers={"Range": rng})
      self.assertEqual(ctx.exception.code, 416, rng)
      self.assertEqual(
          ctx.exception.headers.get("Content-Range"), f"bytes */{total}"
      )
      ctx.exception.close()
    for rng in ("bytes=5-2", "bytes=abc"):
      st, _, data = self._request("/api/media", headers={"Range": rng})
      self.assertEqual(st, 200, rng)
      self.assertEqual(len(data), total)
    st, _, data = self._request(
        "/api/media", headers={"Range": f"bytes={total - 10}-{total + 50}"}
    )
    self.assertEqual(st, 206)
    self.assertEqual(len(data), 10)

  def test_static_ui_serves_app_helpers_before_app(self) -> None:
    """/app_helpers.js is served as JS and loaded by index.html first."""
    st, hdrs, body = self._request("/app_helpers.js")
    self.assertEqual(st, 200)
    self.assertEqual(
        hdrs.get("Content-Type"), "application/javascript; charset=utf-8"
    )
    self.assertIn("AutoSubsHelpers", body.decode("utf-8"))
    _, _, html = self._request("/")
    html_txt = html.decode("utf-8")
    helpers_pos = html_txt.find('<script src="/app_helpers.js"></script>')
    self.assertGreaterEqual(helpers_pos, 0)
    self.assertLess(
        helpers_pos, html_txt.find('<script src="/app.js"></script>')
    )


class ResolveTemplatesAndBundleTest(unittest.TestCase):
  """Resolve Lua scripts, Fusion templates, UI IDs, ZIP bundle, WAV peaks."""

  def test_lua_scripts_load_payloads_with_fallbacks(self) -> None:
    """Insert/jump scripts read payloads via io.open, bmd.readfile, loadfile."""
    for rel_path in (
        "lua/2_AutoSubs_ES_Insertar_Subtitulos.lua",
        "lua/3_AutoSubs_ES_Ir_A_Tiempo.lua",
    ):
      lua_src = self._read_repo_text(rel_path)
      with self.subTest(script=rel_path):
        self.assertIn("io.open", lua_src)
        self.assertIn("bmd.readfile", lua_src)
        self.assertIn("loadfile", lua_src)

  def test_lua_insert_checks_append_and_uses_opacity3(self) -> None:
    """Insert script checks appended clips and sets Opacity3, not Alpha3."""
    lua_insert = self._read_repo_text(
        "lua/2_AutoSubs_ES_Insertar_Subtitulos.lua"
    )
    self.assertIn("#appended > 0", lua_insert)
    self.assertIn("GetItemListInTrack", lua_insert)
    self.assertIn('SetInput("Opacity3"', lua_insert)
    self.assertNotIn('SetInput("Alpha3"', lua_insert)

  def test_templates_use_frame_format_settings(self) -> None:
    """Every bundled Text+ macro enables UseFrameFormatSettings."""
    templates = (
        "AutoSubs_ES_Amarillo_Clasico.setting",
        "AutoSubs_ES_Caja_Editorial.setting",
        "AutoSubs_ES_Cine_Netflix.setting",
        "AutoSubs_ES_Reels_Impacto.setting",
        "AutoSubs_ES_Anitype_Animado.setting",
    )
    for tmpl in templates:
      with self.subTest(template=tmpl):
        txt = self._read_repo_text(f"templates/{tmpl}")
        self.assertIn("UseFrameFormatSettings = Input { Value = 1", txt, tmpl)

  def test_anitype_template_balanced_with_required_controls(self) -> None:
    """Anitype macro has balanced brackets and all its controls/tools."""
    anim_txt = self._read_repo_text(
        "templates/AutoSubs_ES_Anitype_Animado.setting"
    )
    self.assertEqual(anim_txt.count("{"), anim_txt.count("}"))
    self.assertEqual(anim_txt.count("("), anim_txt.count(")"))
    for required_sym in (
        "HoldFrames",
        "VariantCount",
        "BoilJitter",
        "TextPlus1",
        "TextPlus5",
        "VariantSel2",
        "VariantSel5",
        "BoilTransform",
    ):
      with self.subTest(symbol=required_sym):
        self.assertIn(required_sym, anim_txt)

  def test_anitype_template_expressions_avoid_math_and_modulo(self) -> None:
    """Anitype expressions (>= 10) never use math.* or the % operator."""
    anim_txt = self._read_repo_text(
        "templates/AutoSubs_ES_Anitype_Animado.setting"
    )
    exprs = re.findall(r'Expression\s*=\s*"([^"]*)"', anim_txt)
    self.assertGreaterEqual(len(exprs), 10)
    for expr in exprs:
      with self.subTest(expression=expr):
        self.assertNotIn("math.", expr)
        self.assertNotIn("%", expr)

  def test_ui_index_html_exposes_required_ids(self) -> None:
    """ui/index.html keeps the element IDs the studio JS depends on."""
    ui_html = self._read_repo_text("ui/index.html")
    for elem_id in (
        "btn-theme-toggle",
        "st-fill-a",
        "st-highlight-a",
        "st-outline-a",
        "st-shadow-a",
        "st-box-a",
        "btn-preset-reset",
        "btn-preset-save",
        "preset-save-form",
        "style-contrast",
        "stage-bg-toggle",
        "font-input",
        "st-ascii-fold",
        "st-anim-mode",
        "AutoSubs_ES_Anitype_Animado",
    ):
      with self.subTest(elem_id=elem_id):
        self.assertIn(elem_id, ui_html)

  def _bundle_meta(self) -> dict[str, Any]:
    """Builds and verifies the distribution ZIP once per test class.

    Building the ZIP reads the whole repo, so the result is cached on the
    class and shared by the bundle tests.

    Returns:
      The metadata from package_bundle.verify_zip_bytes (file_count,
      entries).
    """
    cls = type(self)
    if getattr(cls, "_cached_bundle_meta", None) is None:
      root = package_bundle.find_project_root()
      zip_bytes = package_bundle.build_zip_bytes(root)
      cls._cached_bundle_meta = package_bundle.verify_zip_bytes(zip_bytes)
    return cls._cached_bundle_meta

  def test_zip_bundle_file_count_and_exclusions(self) -> None:
    """The ZIP has exactly 52 files and excludes __init__.py and site/."""
    meta = self._bundle_meta()
    self.assertEqual(meta["file_count"], 52, meta["entries"])
    self.assertNotIn("AutoSubs_ES/__init__.py", meta["entries"])
    self.assertFalse(
        any(e.startswith("AutoSubs_ES/site/") for e in meta["entries"])
    )

  def test_zip_bundle_includes_required_entries(self) -> None:
    """The ZIP ships metadata, sources, the Anitype macro, docs, images."""
    meta = self._bundle_meta()
    for entry in (
        "AutoSubs_ES/.gitignore",
        "AutoSubs_ES/BUILD",
        "AutoSubs_ES/LICENSE",
        "AutoSubs_ES/README.md",
        "AutoSubs_ES/README.en.md",
        "AutoSubs_ES/pyproject.toml",
        "AutoSubs_ES/font_manager.py",
        "AutoSubs_ES/templates/AutoSubs_ES_Anitype_Animado.setting",
        "AutoSubs_ES/docs/design-doc.md",
        "AutoSubs_ES/docs/user-guide.md",
        "AutoSubs_ES/docs/imac-2017-architecture.md",
        "AutoSubs_ES/docs/spanish-subtitling-reference.md",
        "AutoSubs_ES/docs/troubleshooting.md",
        "AutoSubs_ES/docs/images/autosubs-es-overview.png",
        "AutoSubs_ES/docs/images/autosubs-es-step6-export.png",
        "AutoSubs_ES/docs/images/autosubs-es-fonts-animation.png",
    ):
      with self.subTest(entry=entry):
        self.assertIn(entry, meta["entries"])

  def test_extract_waveform_peaks_covers_tail_frames(self) -> None:
    """Frames left over after even bucketing still count in the last peak."""
    with tempfile.TemporaryDirectory() as tmp:
      wav_path = Path(tmp) / "tail_test.wav"
      total_frames = 1005
      buckets = 16
      # 1005 // 16 = 62 frames per bucket -> 992 frames in first 16*62,
      # leaving 13 tail frames that must still be included in the last peak.
      samples = [0] * 992 + [16384] * (total_frames - 992)
      with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

      res = resolve_bridge.extract_waveform_peaks(
          str(wav_path), num_buckets=buckets
      )
      self.assertEqual(len(res["peaks"]), buckets)
      self.assertGreater(res["peaks"][-1], 0.4)

  def test_extract_waveform_peaks_24bit_pcm(self) -> None:
    """24-bit stereo PCM peaks are decoded and normalized to 0..1."""
    with tempfile.TemporaryDirectory() as tmp:
      wav_path = Path(tmp) / "test_24bit.wav"
      # 32 stereo frames (64 samples total):
      # First 16 frames: ch0 = +4194304 (0.5), ch1 = 0
      # Next 16 frames: ch0 = -8388608 (-1.0), ch1 = 0
      raw = bytearray()
      pos_half = int(4194304).to_bytes(3, "little", signed=True)
      neg_full = int(-8388608).to_bytes(3, "little", signed=True)
      zero_24 = b"\x00\x00\x00"
      for _ in range(16):
        raw.extend(pos_half)
        raw.extend(zero_24)
      for _ in range(16):
        raw.extend(neg_full)
        raw.extend(zero_24)
      with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(3)
        wf.setframerate(16000)
        wf.writeframes(bytes(raw))

      res = resolve_bridge.extract_waveform_peaks(str(wav_path), num_buckets=16)
      self.assertEqual(len(res["peaks"]), 16)
      self.assertEqual(res["duration_ms"], 2)
      self.assertEqual(res["peaks"][0], 0.5)
      self.assertEqual(res["peaks"][-1], 1.0)

  def test_resolve_bridge_uses_opacity3_for_shadow(self) -> None:
    """Studio styling sets the Text+ shadow via Opacity3, never Alpha3."""
    recorded: dict[str, object] = {}

    # The fakes mirror the PascalCase Resolve/Fusion scripting API.
    # pylint: disable=invalid-name
    class _FakeTextPlus:
      """Records SetInput calls like a Fusion Text+ tool."""

      def SetInput(self, key: str, val: object) -> None:
        """Stores the value under its input name."""
        recorded[key] = val

    class _FakeComp:
      """Minimal Fusion comp that only exposes TextPlus1."""

      def FindTool(self, name: str) -> object | None:
        """Returns a fake Text+ for TextPlus1, else None."""
        return _FakeTextPlus() if name == "TextPlus1" else None

    with tempfile.TemporaryDirectory() as tmp:
      bridge = resolve_bridge.ResolveBridge(app_data_dir=Path(tmp))
      style = exporters.StyleConfig(shadow_enabled=True, shadow_alpha=0.65)
      ok = bridge._apply_style_to_fusion_comp(_FakeComp(), "Hola", style)
      self.assertTrue(ok)
      self.assertEqual(recorded.get("Opacity3"), 0.65)
      self.assertNotIn("Alpha3", recorded)

  def test_extract_waveform_peaks_unsupported_wavs_return_empty(self) -> None:
    """Float32, truncated and garbage WAVs yield empty peaks, not errors."""
    empty = {"peaks": [], "duration_ms": 0, "sample_rate": 16000}
    with tempfile.TemporaryDirectory() as tmp:
      data = struct.pack("<4f", 0.0, 0.5, -0.5, 1.0)
      # WAVE_FORMAT_IEEE_FLOAT (tag 3), mono, 16 kHz, 32-bit.
      fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
      float_wav = (
          b"RIFF"
          + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data))
          + b"WAVE"
          + b"fmt "
          + struct.pack("<I", len(fmt))
          + fmt
          + b"data"
          + struct.pack("<I", len(data))
          + data
      )
      cases = {
          "float32.wav": float_wav,
          "truncated.wav": float_wav[:22],
          "garbage.wav": b"RIFF\x00\x00",
      }
      for name, payload in cases.items():
        path = Path(tmp) / name
        path.write_bytes(payload)
        with self.subTest(name=name):
          self.assertEqual(
              resolve_bridge.extract_waveform_peaks(str(path)), empty
          )

  def test_find_template_item_prefers_exact_match_anywhere(self) -> None:
    """An exact template in a later folder beats an earlier fuzzy fallback."""

    # The fakes mirror the PascalCase Resolve/Fusion scripting API.
    # pylint: disable=invalid-name
    class _Clip:
      """Media Pool clip double with a display name."""

      def __init__(self, name: str) -> None:
        """Stores the clip name."""
        self.name = name

      def GetClipProperty(self, _key: str) -> str:
        """Returns the clip name for "Clip Name"."""
        return self.name

      def GetName(self) -> str:
        """Returns the clip name."""
        return self.name

    class _Folder:
      """Media Pool folder double."""

      def __init__(self, clips: list[_Clip], subs: list["_Folder"]) -> None:
        """Stores own clips and subfolders."""
        self.clips = clips
        self.subs = subs

      def GetClipList(self) -> list[_Clip]:
        """Returns own clips."""
        return self.clips

      def GetSubFolderList(self) -> list["_Folder"]:
        """Returns subfolders."""
        return self.subs

    exact = _Clip("AutoSubs_ES_Cine_Netflix")
    root = _Folder(
        [],
        [
            _Folder([_Clip("AutoSubs_Otro")], []),
            _Folder([exact], []),
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
      bridge = resolve_bridge.ResolveBridge(app_data_dir=Path(tmp))
      self.assertIs(
          bridge._find_template_item(root, "AutoSubs_ES_Cine_Netflix"), exact
      )
      partial = _Clip("AutoSubs_ES_Cine_Netflix v2")
      root2 = _Folder([_Clip("Text+ basico")], [_Folder([partial], [])])
      self.assertIs(
          bridge._find_template_item(root2, "AutoSubs_ES_Cine_Netflix"), partial
      )
      fallback = _Clip("Mi Text+")
      root3 = _Folder([_Clip("Logo")], [_Folder([fallback], [])])
      self.assertIs(bridge._find_template_item(root3, "Nada"), fallback)
      self.assertIsNone(bridge._find_template_item(_Folder([], []), "Nada"))

  def test_push_textplus_pairs_clips_by_start_frame(self) -> None:
    """Text+ clips get the text of the cue at their start frame."""
    cues = [
        spanish_nlp.SubtitleCue(1, 0, 1000, ["Uno"]),
        spanish_nlp.SubtitleCue(2, 1000, 2000, ["Dos"]),
        spanish_nlp.SubtitleCue(3, 2000, 3000, ["Tres"]),
    ]

    # The fakes mirror the PascalCase Resolve/Fusion scripting API.
    # pylint: disable=invalid-name
    class _Comp:
      """Fusion comp double remembering which clip it belongs to."""

      def __init__(self, start: int) -> None:
        """Stores the owning clip's start frame."""
        self.start = start

    class _Item:
      """Timeline item double returned by AppendToTimeline."""

      def __init__(self, start: int) -> None:
        """Stores the record start frame."""
        self.start = start

      def GetStart(self) -> int:
        """Returns the record start frame."""
        return self.start

      def GetFusionCompCount(self) -> int:
        """Every Text+ clip has one comp."""
        return 1

      def GetFusionCompByIndex(self, _idx: int) -> _Comp:
        """Returns a comp tagged with this item's start."""
        return _Comp(self.start)

    class _MediaPool:
      """Media Pool double; `drop` removes requested clips by index."""

      def __init__(self, drop: set[int]) -> None:
        """Stores which requested clips Resolve "fails" to insert."""
        self.drop = drop

      def GetRootFolder(self) -> object:
        """Unused: the template lookup is stubbed."""
        return object()

      def AppendToTimeline(self, infos: list[dict]) -> list[_Item]:
        """Returns the inserted items in reverse order."""
        items = [
            _Item(info["recordFrame"])
            for idx, info in enumerate(infos)
            if idx not in self.drop
        ]
        return list(reversed(items))

    class _Timeline:
      """Timeline double starting at frame 90000."""

      def AddTrack(self, _kind: str) -> bool:
        """Accepts the new track."""
        return True

      def GetTrackCount(self, _kind: str) -> int:
        """Reports two video tracks."""
        return 2

      def SetTrackName(self, *_args: object) -> bool:
        """Accepts the track name."""
        return True

      def GetStartFrame(self) -> int:
        """Timeline starts at 01:00:00:00 at 25 fps."""
        return 90000

    class _Project:
      """Project double exposing the media pool and timeline."""

      def __init__(self, pool: _MediaPool) -> None:
        """Stores the media pool."""
        self.pool = pool

      def GetMediaPool(self) -> _MediaPool:
        """Returns the media pool."""
        return self.pool

      def GetCurrentTimeline(self) -> _Timeline:
        """Returns the timeline."""
        return _Timeline()

    class _Resolve:
      """Resolve double: GetProjectManager().GetCurrentProject()."""

      def __init__(self, pool: _MediaPool) -> None:
        """Stores the project."""
        self.project = _Project(pool)

      def GetProjectManager(self) -> "_Resolve":
        """Acts as its own project manager."""
        return self

      def GetCurrentProject(self) -> _Project:
        """Returns the project."""
        return self.project

    with tempfile.TemporaryDirectory() as tmp:
      bridge = resolve_bridge.ResolveBridge(app_data_dir=Path(tmp))
      styled: list[tuple[int, str]] = []
      bridge._find_template_item = lambda folder, name: object()
      bridge._apply_style_to_fusion_comp = (
          lambda comp, cue_text, style: styled.append((comp.start, cue_text))
      )

      def _push(drop: set[int]) -> tuple[bool, str]:
        """Runs a textplus push with the given dropped clip indices."""
        return bridge._push_via_studio(
            resolve=_Resolve(_MediaPool(drop)),
            cues=cues,
            mode="textplus",
            track_name="Subs",
            template_name="AutoSubs_ES_Cine_Netflix",
            srt_path=Path(tmp) / "a.srt",
            fcpxml_path=Path(tmp) / "a.fcpxml",
            fps=25.0,
            start_frame=0,
            style=exporters.StyleConfig(),
        )

      ok, msg = _push(set())
      self.assertTrue(ok, msg)
      self.assertEqual(
          sorted(styled), [(90000, "Uno"), (90025, "Dos"), (90050, "Tres")]
      )
      styled.clear()
      ok, msg = _push({1})
      self.assertTrue(ok, msg)
      self.assertEqual(sorted(styled), [(90000, "Uno"), (90050, "Tres")])
      self.assertIn("1 subtítulos", msg)

    pair = resolve_bridge._pair_textplus_items_with_cues
    # Unreadable starts: index pairing only when the counts match.
    self.assertEqual(pair(["a", "b"], [0, 10]), [("a", 0), ("b", 1)])
    self.assertEqual(pair(["a"], [0, 10]), [])
    # Constant offset in the same order: safe index fallback.
    shifted = [_Item(500), _Item(510)]
    self.assertEqual([idx for _, idx in pair(shifted, [0, 10])], [0, 1])
    # Offset plus a missing clip: nothing can be paired safely.
    self.assertEqual(pair([_Item(500)], [0, 10]), [])

  def _read_repo_text(self, rel_path: str) -> str:
    """Returns a repo file's UTF-8 text.

    Args:
      rel_path: Path relative to the project root.

    Returns:
      The file contents.
    """
    root = package_bundle.find_project_root()
    return (root / rel_path).read_text(encoding="utf-8")

  def test_export_audio_lua_uses_wall_clock_and_restores_render(self) -> None:
    """Script 1 times out on wall time and restores render settings."""
    src = self._read_repo_text("lua/1_AutoSubs_ES_Exportar_Audio.lua")
    # Wall-clock timeout instead of counting loop iterations.
    self.assertIn("os.difftime(os.time(), t0)", src)
    self.assertNotIn("elapsed = elapsed + 0.2", src)
    self.assertIn("bmd.wait(seconds)", src)
    # Previous render settings are captured and restored on every path.
    self.assertIn("GetCurrentRenderFormatAndCodec", src)
    self.assertIn("SaveAsNewRenderPreset", src)
    self.assertIn("LoadRenderPreset", src)
    self.assertIn("DeleteRenderPreset", src)
    run_idx = src.index("pcall(runAudioRender)")
    restore_idx = src.index("restoreRenderSettings()", run_idx)
    # Restore happens right after the protected render, before any return.
    self.assertNotIn("return", src[run_idx:restore_idx])

  def test_insert_lua_matches_cues_by_frame_and_cleans_tracks(self) -> None:
    """Script 2 pairs clips by start frame and removes failed tracks."""
    src = self._read_repo_text("lua/2_AutoSubs_ES_Insertar_Subtitulos.lua")
    # Cue/clip pairing is positional (start frame), never by list index.
    self.assertIn("item:GetStart()", src)
    self.assertIn("MATCH_TOLERANCE_FRAMES", src)
    self.assertNotIn("local cue = cues[i]", src)
    self.assertIn("no coinciden con ningún subtítulo", src)
    # Tracks are added only after import and deleted if the append fails.
    self.assertNotIn('timeline:AddTrack("subtitle")\n', src)
    self.assertIn("DeleteTrack", src)
    srt_block = src[src.index('if mode == "subtitle_srt"') :]
    self.assertLess(
        srt_block.index("ImportMedia"), srt_block.index("addNamedTrack(")
    )
    self.assertIn('removeAddedTrack("subtitle", subCount)', src)
    self.assertIn('removeAddedTrack("video", trackIdx)', src)
    # Exact template name anywhere in the tree beats an earlier fallback.
    self.assertIn("return visit(folder) or substringMatch or fallback", src)

  def test_lua_loaders_survive_corrupt_payloads(self) -> None:
    """loadLuaModule pcall-protects chunks and is Lua 5.1-safe."""
    for rel in (
        "lua/2_AutoSubs_ES_Insertar_Subtitulos.lua",
        "lua/3_AutoSubs_ES_Ir_A_Tiempo.lua",
    ):
      src = self._read_repo_text(rel)
      self.assertIn("pcall(fn)", src, rel)
      self.assertNotIn("return fn()", src, rel)
      # Plain Lua 5.1 `load` rejects strings, so loadstring must win.
      self.assertIn("loadstring or load", src, rel)
      self.assertNotIn("load or loadstring", src, rel)

  def test_jump_lua_reports_failures(self) -> None:
    """Script 3 checks SetCurrentTimecode and reports missing Resolve."""
    src = self._read_repo_text("lua/3_AutoSubs_ES_Ir_A_Tiempo.lua")
    self.assertIn('bmd.scriptapp("Fusion")', src)
    self.assertNotIn("if not resolve then return end", src)
    self.assertIn("No se pudo conectar con DaVinci Resolve 20", src)
    self.assertIn("if ok and moved then", src)
    self.assertIn("no se pudo mover el cabezal", src)

  def test_installer_optional_steps_are_non_fatal(self) -> None:
    """venv and Homebrew failures warn instead of aborting under set -e."""
    src = self._read_repo_text("install_imac.sh")
    self.assertIn('if ! "${HOST_PYTHON}" -m venv "${VENV_DIR}"; then', src)
    self.assertIn('if ! "${BREW_BIN}" install "${pkg}"; then', src)
    # No bare (fatal) invocations of the optional steps remain.
    self.assertIsNone(
        re.search(r'^\s*"\$\{HOST_PYTHON\}" -m venv', src, re.MULTILINE)
    )
    self.assertIsNone(
        re.search(r'^\s*"\$\{BREW_BIN\}" install', src, re.MULTILINE)
    )

  def test_bundle_ships_app_helpers_but_not_node_tests(self) -> None:
    """The ZIP requires ui/app_helpers.js and leaves out tests/js/*.mjs."""
    self.assertIn(
        "AutoSubs_ES/ui/app_helpers.js", package_bundle.REQUIRED_BUNDLE_ENTRIES
    )
    root = package_bundle.find_project_root()
    entries = package_bundle.verify_zip_bytes(
        package_bundle.build_zip_bytes(root)
    )["entries"]
    self.assertIn("AutoSubs_ES/ui/app_helpers.js", entries)
    self.assertFalse(any(e.endswith(".mjs") for e in entries), entries)

  def test_bundle_leaves_out_dev_lint_tooling(self) -> None:
    """Lint configs, tools/ and local tool installs never reach the ZIP."""
    entries = self._bundle_meta()["entries"]
    for name in (
        ".pylintrc",
        ".prettierrc.json",
        ".prettierignore",
        ".shellcheckrc",
        ".stylua.toml",
        "eslint.config.mjs",
        "tools/lint.sh",
    ):
      with self.subTest(name=name):
        self.assertNotIn(f"AutoSubs_ES/{name}", entries)
    skip = package_bundle._should_skip_entry
    self.assertTrue(skip(("tools", "lint.sh"), "lint.sh"))
    self.assertTrue(skip((".venv-dev", "lib", "x.py"), "x.py"))
    self.assertTrue(skip(("node_modules", "a", "b.js"), "b.js"))
    self.assertTrue(skip((".stylua.toml",), ".stylua.toml"))
    # pyproject.toml carries the [tool.pyink] section but is still shipped.
    self.assertFalse(skip(("pyproject.toml",), "pyproject.toml"))


if __name__ == "__main__":
  unittest.main()
