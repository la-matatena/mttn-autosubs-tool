"""Unit and HTTP integration tests for AutoSubs ES."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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
  """Builds a minimal valid OpenType/TrueType font with name + cmap Format 4."""
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

  def test_profile_and_command_flags(self) -> None:
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


class MachineSettingsTest(unittest.TestCase):

  def test_intel_profile_from_real_specs(self) -> None:
    imac = hardware_imac2017.build_intel_profile(
        "Intel(R) Core(TM) i5-7500 CPU @ 3.40GHz", 4, 4, 32, "Radeon Pro 555", 2048
    )
    self.assertTrue(imac.force_no_gpu)
    self.assertEqual((imac.threads_standalone, imac.threads_with_resolve), (4, 3))
    self.assertEqual(imac.cpu_short, "Intel Core i5")
    self.assertEqual(imac.video_encoder, "libx264")
    big = hardware_imac2017.build_intel_profile(
        "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz", 8, 16, 32
    )
    self.assertEqual((big.threads_standalone, big.threads_with_resolve), (8, 7))
    self.assertEqual(big.gpu_name, "GPU integrada")

  def test_modes_persistence_and_portable_preferences(self) -> None:
    prof = hardware_imac2017.IMAC_2017_PROFILE
    with tempfile.TemporaryDirectory() as tmp:
      store = machine_settings.MachineSettings(Path(tmp))
      perf = store.snapshot()["performance"]
      auto = machine_settings.resolve_performance(prof, perf, resolve_running=False)
      self.assertEqual((auto["mode"], auto["threads"], auto["force_no_gpu"]), ("auto", 4, True))
      self.assertEqual(
          machine_settings.resolve_performance(prof, perf, resolve_running=True)["threads"], 3
      )
      safe = machine_settings.resolve_performance(prof, {"mode": "safe"})
      self.assertEqual((safe["threads"], safe["fast_beam"]), (2, True))

      store.update({
          "performance": {"mode": "custom", "threads": 2, "force_no_gpu": False,
                          "model_id": "small-q8", "bogus": 1},
          "preferences": {"dialect": "es_mx", "custom_glossary": "Catan", "evil": "x"},
      })
      # Persisted to disk and reloaded by a fresh instance.
      data = machine_settings.MachineSettings(Path(tmp)).snapshot()
      self.assertNotIn("bogus", data["performance"])
      self.assertEqual(data["preferences"], {"dialect": "es_mx", "custom_glossary": "Catan"})
      custom = machine_settings.resolve_performance(prof, data["performance"])
      self.assertEqual((custom["threads"], custom["force_no_gpu"], custom["model_id"]),
                       (2, False, "small-q8"))

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
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 0.8)["model_id"], "turbo-q5"
    )
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 20)["model_id"], "large-v3-q5"
    )
    self.assertEqual(
        machine_settings.recommend_from_benchmark("turbo-q8", 4)["model_id"], "turbo-q8"
    )


class SpanishNlpTest(unittest.TestCase):

  def test_align_words_keeps_timings_after_text_edits(self) -> None:
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
    self.assertEqual((by_text["Hoy"].start_ms, by_text["Hoy"].end_ms), (1000, 1300))
    self.assertEqual((by_text["vez."].start_ms, by_text["vez."].end_ms), (2900, 3200))
    for prev, cur in zip(aligned, aligned[1:]):
      self.assertLessEqual(prev.start_ms, cur.start_ms)
      self.assertGreater(cur.end_ms, cur.start_ms)

    # Moving/resizing the cue rescales out-of-range words into the new span.
    moved = spanish_nlp.align_words_to_lines(words, ["Hoy jugamos Katan otra vez."], 5000, 6100)
    self.assertEqual(moved[0].start_ms, 5000)
    self.assertEqual(moved[-1].end_ms, 6100)

    # Cues without word timings get proportional ones (karaoke still works).
    synth = spanish_nlp.align_words_to_lines([], ["uno dos tres"], 0, 900)
    self.assertEqual(len(synth), 3)
    self.assertEqual(synth[-1].end_ms, 900)

  def test_parse_whisper_ojf_bpe_utf8_recovery_and_hallucinations(self) -> None:
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

  def test_clean_spanish_words_glossary_muletillas_and_inverted_punct(
      self,
  ) -> None:
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

  def test_boardgame_domain_anglicisms_and_hesitation_punct(self) -> None:
    prompt = spanish_nlp.build_spanish_initial_prompt(
        "es_es", ["Catan", "Devir"], domain="juegos_mesa"
    )
    self.assertLessEqual(len(prompt), 580)
    self.assertIn("vosotros", prompt)
    self.assertIn("Catan", prompt)
    self.assertIn("meeple", prompt)

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


class ExportersTest(unittest.TestCase):

  def _sample_cues(self) -> list[spanish_nlp.SubtitleCue]:
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
    self.assertIn("Style: AutoSubsES,Open Sans,72,&H003BD4FF,&H00FFFFFF,", ass_k)

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

  def test_style_opacity_across_exporters(self) -> None:
    cues = self._sample_cues()

    # Clamping and safe fallbacks for malformed input.
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

    # Defaults keep the historical style line byte-identical.
    ass_default = exporters.export_ass_karaoke(cues, exporters.StyleConfig())
    self.assertIn(
        "&H00FFFFFF,&H00FFFFFF,&H00120D0B,&H59120D0B,", ass_default
    )

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
    self.assertIn("&H80332211,&H59332211,", ass_box)
    self.assertIn(",3,6,0,2,", ass_box)

    fcp_default = exporters.export_fcpxml(
        cues, timeline_name="A", style=exporters.StyleConfig()
    )
    self.assertIn('fontColor="1.000 1.000 1.000 1"', fcp_default)
    fcp_alpha = exporters.export_fcpxml(
        cues, timeline_name="A", style=exporters.StyleConfig(fill_alpha=0.5)
    )
    self.assertIn('fontColor="1.000 1.000 1.000 0.5"', fcp_alpha)

    lua_code = exporters.export_resolve_lua_payload(
        cues=cues,
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

  def test_ndf_23976_timecode_ass_escaping_and_burnin_fontsdir(self) -> None:
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

    cues = self._sample_cues() + [
        spanish_nlp.SubtitleCue(
            index=2,
            start_ms=1_001_000,
            end_ms=1_003_002,
            lines=["Sin deriva NDF"],
        )
    ]
    srt_23976 = exporters.export_srt_timeline_tc(
        cues, start_frame=86400, fps=23.976
    )
    self.assertIn("01:00:00,500 --> 01:00:02,498", srt_23976)
    self.assertIn("01:16:40,000 --> 01:16:42,000", srt_23976)

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

    edited_raw = cues[0].to_dict()
    edited_raw["lines"] = ["Texto editado {2 VP}"]
    edited_raw["text"] = "Texto editado {2 VP}"
    edited_cue = spanish_nlp.SubtitleCue.from_dict(edited_raw)
    self.assertEqual(
        [w.text for w in edited_cue.words], ["Texto", "editado", "{2", "VP}"]
    )
    self.assertGreaterEqual(edited_cue.words[0].start_ms, edited_cue.start_ms)
    self.assertLessEqual(edited_cue.words[-1].end_ms, edited_cue.end_ms)
    edited_cue.speaker = "Alex, Host"
    ass_safe = exporters.export_ass_karaoke(
        [edited_cue],
        style=exporters.StyleConfig(font="Mi Fuente, Bold"),
    )
    self.assertIn("Style: AutoSubsES,Mi Fuente  Bold,", ass_safe)
    self.assertIn(",Alex  Host,", ass_safe)
    self.assertIn(r"\{2 VP\}", ass_safe)
    self.assertNotIn(r"\kf", ass_safe)

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


class FontManagerAndAnitypeTest(unittest.TestCase):

  def _make_anitype_zip_bytes(self) -> bytes:
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


class BridgeAndServerIntegrationTest(unittest.TestCase):

  def setUp(self) -> None:
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
    self.thread = threading.Thread(
        target=self.server.serve_forever, daemon=True
    )
    self.thread.start()

  def tearDown(self) -> None:
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

  def test_full_studio_http_workflow(self) -> None:
    # 1. Static UI & Status
    st_html, _, html = self._request("/")
    self.assertEqual(st_html, 200)
    html_txt = html.decode("utf-8")
    self.assertIn("MTTN", html_txt)
    self.assertIn("AutoSubs Tool", html_txt)
    self.assertIn("La Matatena", html_txt)

    st_css, _, css = self._request("/styles.css")
    self.assertEqual(st_css, 200)
    css_str = css.decode("utf-8")
    self.assertIn("#8367c7", css_str)
    self.assertIn("color-scheme", css_str)
    self.assertIn("theme-toggle-btn", css_str)

    st_status, _, status_bytes = self._request("/api/status")
    self.assertEqual(st_status, 200)
    status_json = json.loads(status_bytes.decode("utf-8"))
    self.assertEqual(
        status_json["environment"]["hardware_profile"]["profile_id"],
        hardware_imac2017.detect_hardware_profile().profile_id,
    )

    # 2. Load Spanish Demo + verify waveform + HTTP 206 media Range
    _, _, demo_bytes = self._request("/api/demo", method="POST", body={})
    demo_json = json.loads(demo_bytes.decode("utf-8"))
    self.assertTrue(demo_json["ok"])
    self.assertGreaterEqual(demo_json["session"]["cue_count"], 4)
    self.assertGreater(len(demo_json["session"]["peaks"]), 50)

    st_media, hdrs_media, chunk = self._request(
        "/api/media", headers={"Range": "bytes=0-63"}
    )
    self.assertEqual(st_media, 206)
    self.assertEqual(len(chunk), 64)
    self.assertTrue(
        hdrs_media.get("Content-Range", "").startswith("bytes 0-63/")
    )

    # 3. Instant RAE re-segmentation (Reels 9:16 + remove muletillas)
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
    reseg_cues = json.loads(reseg_bytes.decode("utf-8"))["session"]["cues"]
    self.assertGreater(len(reseg_cues), 6)
    joined_all = " ".join(c["text"] for c in reseg_cues)
    self.assertIn("¿Sabías", joined_all)
    self.assertIn("¡Qué", joined_all)

    # 4. Edit cues via PUT /api/cues
    reseg_cues[0]["text"] = "Hola,\nAutoSubs ES"
    reseg_cues[0]["lines"] = ["Hola,", "AutoSubs ES"]
    _, _, put_bytes = self._request(
        "/api/cues", method="PUT", body={"cues": reseg_cues}
    )
    put_cues = json.loads(put_bytes.decode("utf-8"))["session"]["cues"]
    self.assertEqual(put_cues[0]["text"], "Hola,\nAutoSubs ES")

    # 5. Export SRT, ASS Karaoke, TXT, FCPXML
    for fmt, needle in (
        ("srt", "-->"),
        ("ass", "[Events]"),
        ("txt", "AutoSubs ES"),
        ("txt_tc", "[00:00:0"),
        ("fcpxml", '<fcpxml version="1.9">'),
    ):
      _, _, exp_bytes = self._request(
          "/api/export", method="POST", body={"format": fmt}
      )
      exp_json = json.loads(exp_bytes.decode("utf-8"))
      self.assertIn(needle, exp_json["content"])

    # 6. Push to Resolve outbox & jump timecode
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
      self.assertTrue(Path(push_json["files"][key]).is_file())
    push_table_txt = Path(push_json["files"]["lua_table"]).read_text(
        encoding="utf-8"
    )
    self.assertTrue(push_table_txt.lstrip().startswith("{"))
    self.assertFalse(push_table_txt.lstrip().startswith("return"))

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

    # 7. Binary upload via POST /api/upload
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

  def test_security_origin_guard_video_range_and_upload_reset(self) -> None:
    # 1. Cross-origin mutating POST blocked with 403.
    with self.assertRaises(urllib.error.HTTPError) as ctx_origin:
      self._request(
          "/api/demo",
          method="POST",
          body={},
          headers={"Origin": "https://evil.example"},
      )
    self.assertEqual(ctx_origin.exception.code, 403)
    ctx_origin.exception.close()

    # 2. Disallowed Host header blocked with 403.
    with self.assertRaises(urllib.error.HTTPError) as ctx_host:
      self._request("/api/status", headers={"Host": "evil.example:55120"})
    self.assertEqual(ctx_host.exception.code, 403)
    ctx_host.exception.close()

    # 3. Arbitrary filesystem media_path in /api/transcribe blocked outside app_dir.
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

    # 4. Uploading new media clears stale cues from demo session & bumps media_rev.
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

    # 5. Exported MP4 streams via HTTP 206 Range requests instead of full read.
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


class ResolveTemplatesAndBundleTest(unittest.TestCase):

  def test_lua_bridges_templates_and_zip_bundle(self) -> None:
    root = package_bundle.find_project_root()

    lua_insert = (
        root / "lua" / "2_AutoSubs_ES_Insertar_Subtitulos.lua"
    ).read_text(encoding="utf-8")
    lua_jump = (root / "lua" / "3_AutoSubs_ES_Ir_A_Tiempo.lua").read_text(
        encoding="utf-8"
    )
    for lua_src in (lua_insert, lua_jump):
      self.assertIn("io.open", lua_src)
      self.assertIn("bmd.readfile", lua_src)
      self.assertIn("loadfile", lua_src)
    self.assertIn("#appended > 0", lua_insert)
    self.assertIn("GetItemListInTrack", lua_insert)

    templates = (
        "AutoSubs_ES_Amarillo_Clasico.setting",
        "AutoSubs_ES_Caja_Editorial.setting",
        "AutoSubs_ES_Cine_Netflix.setting",
        "AutoSubs_ES_Reels_Impacto.setting",
        "AutoSubs_ES_Anitype_Animado.setting",
    )
    for tmpl in templates:
      txt = (root / "templates" / tmpl).read_text(encoding="utf-8")
      self.assertIn("UseFrameFormatSettings = Input { Value = 1", txt, tmpl)

    anim_txt = (
        root / "templates" / "AutoSubs_ES_Anitype_Animado.setting"
    ).read_text(encoding="utf-8")
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
      self.assertIn(required_sym, anim_txt)
    exprs = re.findall(r'Expression\s*=\s*"([^"]*)"', anim_txt)
    self.assertGreaterEqual(len(exprs), 10)
    for expr in exprs:
      self.assertNotIn("math.", expr)
      self.assertNotIn("%", expr)

    ui_html = (root / "ui" / "index.html").read_text(encoding="utf-8")
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
      self.assertIn(elem_id, ui_html)

    zip_bytes = package_bundle.build_zip_bytes(root)
    meta = package_bundle.verify_zip_bytes(zip_bytes)
    self.assertEqual(meta["file_count"], 51, meta["entries"])
    self.assertNotIn("AutoSubs_ES/__init__.py", meta["entries"])
    self.assertIn("AutoSubs_ES/.gitignore", meta["entries"])
    self.assertIn("AutoSubs_ES/BUILD", meta["entries"])
    self.assertIn("AutoSubs_ES/LICENSE", meta["entries"])
    self.assertIn("AutoSubs_ES/README.md", meta["entries"])
    self.assertIn("AutoSubs_ES/README.en.md", meta["entries"])
    self.assertIn("AutoSubs_ES/pyproject.toml", meta["entries"])
    self.assertIn("AutoSubs_ES/font_manager.py", meta["entries"])
    self.assertIn(
        "AutoSubs_ES/templates/AutoSubs_ES_Anitype_Animado.setting",
        meta["entries"],
    )
    self.assertIn("AutoSubs_ES/docs/design-doc.md", meta["entries"])
    self.assertIn("AutoSubs_ES/docs/user-guide.md", meta["entries"])
    self.assertIn("AutoSubs_ES/docs/imac-2017-architecture.md", meta["entries"])
    self.assertIn(
        "AutoSubs_ES/docs/spanish-subtitling-reference.md", meta["entries"]
    )
    self.assertIn("AutoSubs_ES/docs/troubleshooting.md", meta["entries"])
    self.assertIn(
        "AutoSubs_ES/docs/images/autosubs-es-overview.png", meta["entries"]
    )
    self.assertIn(
        "AutoSubs_ES/docs/images/autosubs-es-step6-export.png",
        meta["entries"],
    )
    self.assertIn(
        "AutoSubs_ES/docs/images/autosubs-es-fonts-animation.png", meta["entries"]
    )


if __name__ == "__main__":
  unittest.main()
