"""DaVinci Resolve 20 dual-mode bridge, audio converter, and waveform peaks."""

from __future__ import annotations

from array import array
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
import wave

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import exporters
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
  from google3.experimental.users.joseluiszapata.autosubs_es import spanish_nlp
except ImportError:
  import exporters  # type: ignore[no-redef]
  import hardware_imac2017  # type: ignore[no-redef]
  import spanish_nlp  # type: ignore[no-redef]


SubtitleCue = spanish_nlp.SubtitleCue
StyleConfig = exporters.StyleConfig


def _is_16k_mono_wav(path: str | Path) -> bool:
  try:
    with wave.open(str(path), "rb") as wf:
      return (
          wf.getnchannels() == 1
          and wf.getframerate() == 16000
          and wf.getsampwidth() == 2
      )
  except (wave.Error, OSError):
    return False


def prepare_wav_16k(
    input_media_path: str,
    output_wav_path: str,
    ffmpeg_bin: str | None = None,
    enhance_voice: bool = True,
) -> None:
  """Extracts 16 kHz mono PCM16 WAV using ffmpeg or direct copy if ready."""
  src = Path(input_media_path)
  dst = Path(output_wav_path)
  dst.parent.mkdir(parents=True, exist_ok=True)

  if not src.is_file():
    raise FileNotFoundError(f"Archivo de entrada no encontrado: {src}")

  if not ffmpeg_bin:
    if _is_16k_mono_wav(src):
      if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
      return
    raise RuntimeError(
        "FFmpeg no está instalado. Ejecuta ./install_imac.sh o 'brew install"
        " ffmpeg'."
    )

  cmd = [
      ffmpeg_bin,
      "-y",
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      str(src),
      "-vn",
      "-ac",
      "1",
      "-ar",
      "16000",
      "-c:a",
      "pcm_s16le",
  ]
  if enhance_voice:
    cmd.extend([
        "-af",
        "highpass=f=80,lowpass=f=7800,dynaudnorm=f=150:g=15:p=0.9",
    ])
  cmd.append(str(dst))

  res = subprocess.run(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      check=False,
  )
  if res.returncode != 0:
    raise RuntimeError(f"FFmpeg falló al convertir audio: {res.stderr.strip()}")


def extract_waveform_peaks(
    wav_path: str, num_buckets: int = 900
) -> dict[str, Any]:
  """Computes normalized 0.0..1.0 waveform peaks from a PCM WAV in chunks."""
  path = Path(wav_path)
  if not path.is_file():
    return {"peaks": [], "duration_ms": 0, "sample_rate": 16000}

  with wave.open(str(path), "rb") as wf:
    channels = wf.getnchannels()
    sampwidth = wf.getsampwidth()
    rate = wf.getframerate()
    total_frames = wf.getnframes()

    if total_frames <= 0 or rate <= 0:
      return {"peaks": [], "duration_ms": 0, "sample_rate": rate or 16000}

    duration_ms = int(round((total_frames / float(rate)) * 1000.0))
    buckets = max(16, min(int(num_buckets), total_frames))
    frames_per_bucket = max(1, total_frames // buckets)
    peaks: list[float] = []

    while len(peaks) < buckets:
      raw = wf.readframes(frames_per_bucket)
      if not raw:
        break
      if sampwidth == 2:
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
          samples.byteswap()
        if channels > 1:
          samples = samples[::channels]
        max_amp = max((abs(s) for s in samples), default=0)
        peaks.append(round(min(1.0, max_amp / 32768.0), 4))
      else:
        max_b = max((abs(b - 128) for b in raw), default=0)
        peaks.append(round(min(1.0, max_b / 128.0), 4))

    return {
        "peaks": peaks,
        "duration_ms": duration_ms,
        "sample_rate": rate,
    }


def generate_demo_wav(
    output_wav_path: str,
    cue_windows_ms: list[tuple[int, int]] | None = None,
    duration_ms: int = 18500,
) -> None:
  """Generates a gentle speech-envelope 16 kHz WAV for offline Demo mode."""
  dst = Path(output_wav_path)
  dst.parent.mkdir(parents=True, exist_ok=True)
  rate = 16000
  total_frames = int((duration_ms / 1000.0) * rate)
  windows = cue_windows_ms or [
      (300, 3100),
      (3400, 6700),
      (7100, 10800),
      (11200, 14600),
      (14900, 18100),
  ]

  samples = array("h")
  for idx in range(total_frames):
    ms = (idx * 1000.0) / rate
    active = any(s <= ms <= e for s, e in windows)
    if active:
      syllable = 0.45 + 0.55 * abs(math.sin(ms * 0.018))
      tone = 0.55 * math.sin(
          2.0 * math.pi * 175.0 * (idx / rate)
      ) + 0.25 * math.sin(2.0 * math.pi * 350.0 * (idx / rate))
      val = int(max(-32767, min(32767, tone * syllable * 9000.0)))
    else:
      val = int(90.0 * math.sin(idx * 0.07))
    samples.append(val)

  if sys.byteorder != "little":
    samples.byteswap()
  with wave.open(str(dst), "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(rate)
    wf.writeframes(samples.tobytes())


class ResolveBridge:
  """Manages Studio Direct API and Free/Studio file-mailbox integration."""

  def __init__(self, app_data_dir: Path | None = None) -> None:
    self.app_dir = app_data_dir or hardware_imac2017.get_app_data_dir(
        create=True
    )
    self.inbox_dir = self.app_dir / "bridge" / "inbox"
    self.outbox_dir = self.app_dir / "bridge" / "outbox"
    self.inbox_dir.mkdir(parents=True, exist_ok=True)
    self.outbox_dir.mkdir(parents=True, exist_ok=True)
    self._resolve_obj: Any = None

  def try_studio_connection(self) -> Any:
    """Attempts connection to DaVinci Resolve Studio external scripting API."""
    if sys.platform == "darwin":
      module_dir = Path(
          "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
          "Developer/Scripting/Modules"
      )
      if module_dir.is_dir() and str(module_dir) not in sys.path:
        sys.path.append(str(module_dir))
    try:
      import DaVinciResolveScript as dvr  # type: ignore[import-not-found]

      resolve = dvr.scriptapp("Resolve")
      if resolve:
        self._resolve_obj = resolve
        return resolve
    except Exception:  # pylint: disable=broad-except
      pass
    self._resolve_obj = None
    return None

  def read_timeline_meta(self) -> dict[str, Any]:
    """Reads timeline metadata exported by Resolve Lua script or Fusion.prefs."""
    meta_file = self.inbox_dir / "timeline_meta.json"
    if meta_file.is_file():
      try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
          return data
      except (OSError, json.JSONDecodeError):
        pass

    # Fallback: read Fusion.prefs if Resolve sandbox disabled io.open.
    prefs_path = (
        hardware_imac2017.get_resolve_user_fusion_dir()
        / "Profiles"
        / "Default"
        / "Fusion.prefs"
    )
    if prefs_path.is_file():
      try:
        raw = prefs_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(
            r'AutoSubsES\s*=\s*\{\s*Meta\s*=\s*"((?:[^"\\]|\\.)*)"',
            raw,
            re.DOTALL,
        )
        if match:
          unescaped = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
          data = json.loads(unescaped)
          if isinstance(data, dict):
            return data
      except (OSError, json.JSONDecodeError):
        pass

    return {}

  def get_inbox_audio_path(self) -> Path | None:
    """Finds the latest timeline WAV exported into bridge/inbox."""
    meta = self.read_timeline_meta()
    audio_from_meta = meta.get("audio_path")
    if audio_from_meta and Path(audio_from_meta).is_file():
      return Path(audio_from_meta)
    wavs = sorted(
        self.inbox_dir.glob("*.wav"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return wavs[0] if wavs else None

  def list_installed_templates(self) -> list[str]:
    """Lists installed MTTN AutoSubs Fusion .setting templates plus defaults."""
    titles_root = (
        hardware_imac2017.get_resolve_user_fusion_dir()
        / "Templates"
        / "Edit"
        / "Titles"
    )
    names: list[str] = []
    for folder_name in ("MTTN AutoSubs", "AutoSubs ES"):
      templates_dir = titles_root / folder_name
      if templates_dir.is_dir():
        names = sorted(p.stem for p in templates_dir.glob("*.setting"))
        if names:
          break
    if not names:
      names = [
          "AutoSubs_ES_Cine_Netflix",
          "AutoSubs_ES_Amarillo_Clasico",
          "AutoSubs_ES_Reels_Impacto",
          "AutoSubs_ES_Caja_Editorial",
          "AutoSubs_ES_Anitype_Animado",
      ]
    return names

  def get_status(self) -> dict[str, Any]:
    """Returns bridge mode, timeline metadata, and available Resolve scripts."""
    studio = self.try_studio_connection()
    meta = self.read_timeline_meta()
    inbox_audio = self.get_inbox_audio_path()

    scripts_dir = (
        hardware_imac2017.get_resolve_user_fusion_dir() / "Scripts" / "Utility"
    )
    lua_installed = (
        scripts_dir / "1 - MTTN AutoSubs - Exportar Audio.lua"
    ).is_file() or (
        scripts_dir / "1 - AutoSubs ES - Exportar Audio.lua"
    ).is_file()

    if studio is not None:
      mode = "studio_direct"
      mode_label = "DaVinci Resolve 20 Studio (Conexión directa)"
    elif lua_installed or meta:
      mode = "lua_bridge_ready"
      mode_label = "DaVinci Resolve 20 Free / Studio (Puente Lua listo)"
    else:
      mode = "standalone"
      mode_label = (
          "Modo local (ejecuta ./install_imac.sh para instalar scripts)"
      )

    return {
        "mode": mode,
        "mode_label": mode_label,
        "resolve_running": hardware_imac2017.is_resolve_running(),
        "lua_scripts_installed": lua_installed,
        "inbox_audio_path": str(inbox_audio) if inbox_audio else None,
        "timeline_meta": meta,
        "installed_templates": self.list_installed_templates(),
        "outbox_dir": str(self.outbox_dir),
    }

  def export_timeline_audio_studio(self) -> dict[str, Any]:
    """Triggers non-blocking Deliver WAV export via Studio scripting API."""
    resolve = self.try_studio_connection()
    if resolve is None:
      return {
          "ok": False,
          "requires_lua": True,
          "message": (
              "DaVinci Resolve 20 Free bloquea la API externa. En Resolve abre:"
              " Workspace > Scripts > 1 - MTTN AutoSubs - Exportar Audio."
          ),
      }
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
      return {
          "ok": False,
          "message": "No hay ningún proyecto abierto en Resolve.",
      }
    timeline = project.GetCurrentTimeline()
    if not timeline:
      return {
          "ok": False,
          "message": "No hay ningún timeline activo en Resolve.",
      }

    fps = float(timeline.GetSetting("timelineFrameRate") or 24.0)
    start_frame = int(timeline.GetStartFrame() or 0)
    meta = {
        "project_name": project.GetName(),
        "timeline_name": timeline.GetName(),
        "fps": fps,
        "start_frame": start_frame,
        "width": int(timeline.GetSetting("timelineResolutionWidth") or 1920),
        "height": int(timeline.GetSetting("timelineResolutionHeight") or 1080),
        "audio_path": str(self.inbox_dir / "timeline_audio.wav"),
    }

    project.SetCurrentRenderFormatAndCodec("wav", "LinearPCM")
    project.SetRenderSettings({
        "TargetDir": str(self.inbox_dir),
        "CustomName": "timeline_audio",
        "ExportVideo": False,
        "ExportAudio": True,
        "AudioSampleRate": 48000,
        "AudioBitDepth": 16,
        "SelectAllFrames": True,
    })
    job_id = project.AddRenderJob()
    if not job_id:
      return {
          "ok": False,
          "message": (
              "No se pudo crear el trabajo de render de audio en Resolve."
          ),
      }
    project.StartRendering(job_id)
    (self.inbox_dir / "timeline_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "job_id": job_id, "timeline_meta": meta}

  def stage_or_push_to_resolve(
      self,
      cues: list[SubtitleCue],
      push_cfg: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    """Writes bridge/outbox payloads and pushes directly if Studio is active."""
    cfg = push_cfg or {}
    meta = self.read_timeline_meta()
    mode = str(cfg.get("mode", "subtitle_srt"))
    if mode not in ("subtitle_srt", "textplus", "fcpxml"):
      mode = "subtitle_srt"

    track_name = str(cfg.get("track_name", "Subtítulos ES"))
    template_name = str(cfg.get("template_name", "AutoSubs_ES_Cine_Netflix"))
    fps = float(cfg.get("fps") or meta.get("fps") or 24.0)
    start_frame = int(
        cfg.get("timeline_start_frame") or meta.get("start_frame") or 86400
    )
    width = int(cfg.get("width") or meta.get("width") or 1920)
    height = int(cfg.get("height") or meta.get("height") or 1080)
    timeline_name = str(meta.get("timeline_name") or "AutoSubs ES")
    style = StyleConfig.from_dict(cfg.get("style"))

    srt_path = self.outbox_dir / "autosubs_es.srt"
    srt_tc_path = self.outbox_dir / "autosubs_es_resolve_tc.srt"
    ass_path = self.outbox_dir / "autosubs_es.ass"
    fcpxml_path = self.outbox_dir / "autosubs_es.fcpxml"
    payload_lua_path = self.outbox_dir / "push_payload.lua"
    payload_table_path = self.outbox_dir / "push_payload.table"

    srt_path.write_text(exporters.export_srt(cues, 0, style), encoding="utf-8")
    srt_tc_path.write_text(
        exporters.export_srt_timeline_tc(
            cues, start_frame=start_frame, fps=fps, style=style
        ),
        encoding="utf-8",
    )
    ass_path.write_text(
        exporters.export_ass_karaoke(cues, style, width, height),
        encoding="utf-8",
    )
    fcpxml_path.write_text(
        exporters.export_fcpxml(
            cues, timeline_name, fps, start_frame, width, height, style
        ),
        encoding="utf-8",
    )
    payload_lua_path.write_text(
        exporters.export_resolve_lua_payload(
            cues=cues,
            mode=mode,
            track_name=track_name,
            template_name=template_name,
            srt_path=str(srt_path),
            srt_timeline_tc_path=str(srt_tc_path),
            fcpxml_path=str(fcpxml_path),
            fps=fps,
            timeline_start_frame=start_frame,
            style=style,
            include_return=True,
        ),
        encoding="utf-8",
    )
    payload_table_path.write_text(
        exporters.export_resolve_lua_payload(
            cues=cues,
            mode=mode,
            track_name=track_name,
            template_name=template_name,
            srt_path=str(srt_path),
            srt_timeline_tc_path=str(srt_tc_path),
            fcpxml_path=str(fcpxml_path),
            fps=fps,
            timeline_start_frame=start_frame,
            style=style,
            include_return=False,
        ),
        encoding="utf-8",
    )

    pushed_directly = False
    studio_note = ""
    resolve = self.try_studio_connection()
    if resolve is not None:
      try:
        pushed_directly, studio_note = self._push_via_studio(
            resolve=resolve,
            cues=cues,
            mode=mode,
            track_name=track_name,
            template_name=template_name,
            srt_path=srt_path,
            fcpxml_path=fcpxml_path,
            fps=fps,
            start_frame=start_frame,
            style=style,
        )
      except Exception as exc:  # pylint: disable=broad-except
        studio_note = f"No se pudo insertar directamente en Studio ({exc})."

    return {
        "ok": True,
        "mode": mode,
        "pushed_directly": pushed_directly,
        "cue_count": len(cues),
        "files": {
            "srt": str(srt_path),
            "srt_resolve_tc": str(srt_tc_path),
            "ass": str(ass_path),
            "fcpxml": str(fcpxml_path),
            "lua_payload": str(payload_lua_path),
            "lua_table": str(payload_table_path),
        },
        "message": (
            studio_note
            if pushed_directly
            else (
                "Paquete preparado para DaVinci Resolve 20. En Resolve haz clic"
                " en Workspace > Scripts > 2 - MTTN AutoSubs - Insertar"
                " Subtitulos."
            )
        ),
    }

  def _find_template_item(self, folder: Any, template_name: str) -> Any:
    """Recursively looks for a Text+ or AutoSubs template in Media Pool."""
    candidates = []
    for clip in folder.GetClipList() or []:
      name = str(clip.GetClipProperty("Clip Name") or clip.GetName() or "")
      if name == template_name or template_name.lower() in name.lower():
        return clip
      if "autosubs" in name.lower() or "text+" in name.lower():
        candidates.append(clip)
    for sub in folder.GetSubFolderList() or []:
      found = self._find_template_item(sub, template_name)
      if found:
        return found
    return candidates[0] if candidates else None

  def _push_via_studio(
      self,
      resolve: Any,
      cues: list[SubtitleCue],
      mode: str,
      track_name: str,
      template_name: str,
      srt_path: Path,
      fcpxml_path: Path,
      fps: float,
      start_frame: int,
      style: StyleConfig,
  ) -> tuple[bool, str]:
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
      return False, "No hay proyecto abierto en Resolve."
    media_pool = project.GetMediaPool()
    timeline = project.GetCurrentTimeline()
    if not timeline:
      return False, "No hay timeline abierto en Resolve."

    if mode == "subtitle_srt":
      timeline.AddTrack("subtitle")
      sub_count = int(timeline.GetTrackCount("subtitle") or 1)
      timeline.SetTrackName("subtitle", sub_count, track_name)
      imported = media_pool.ImportMedia([str(srt_path)])
      if imported:
        media_pool.AppendToTimeline([{
            "mediaPoolItem": imported[0],
            "startFrame": 0,
            "recordFrame": int(timeline.GetStartFrame() or start_frame),
            "trackIndex": sub_count,
        }])
        return True, f"Subtítulos SRT insertados en la pista {sub_count}."
      return False, "ImportMedia del archivo SRT devolvió vacío."

    if mode == "fcpxml":
      ok = media_pool.ImportTimelineFromFile(str(fcpxml_path))
      return bool(ok), (
          "Timeline FCPXML importado en DaVinci Resolve 20 Studio."
          if ok
          else "Fallo al importar FCPXML."
      )

    # Mode: textplus (GPU-light single-node TextPlus)
    template_item = self._find_template_item(
        media_pool.GetRootFolder(), template_name
    )
    if not template_item:
      return (
          False,
          (
              "Coloca primero una plantilla Text+ (p. ej."
              " AutoSubs_ES_Cine_Netflix) en el Media Pool o usa Modo SRT /"
              " FCPXML."
          ),
      )

    timeline.AddTrack("video")
    target_track = int(timeline.GetTrackCount("video") or 1)
    timeline.SetTrackName("video", target_track, track_name)
    tl_start = int(timeline.GetStartFrame() or start_frame)
    eff_fps = exporters.effective_fps(fps)
    clip_infos = []
    for cue in cues:
      s_frame = max(0, int(round((cue.start_ms / 1000.0) * eff_fps)))
      e_frame = max(s_frame + 1, int(round((cue.end_ms / 1000.0) * eff_fps)))
      clip_infos.append({
          "mediaPoolItem": template_item,
          "startFrame": 0,
          "endFrame": max(1, e_frame - s_frame - 1),
          "recordFrame": tl_start + s_frame,
          "trackIndex": target_track,
      })

    appended = media_pool.AppendToTimeline(clip_infos)
    if not appended:
      return False, "AppendToTimeline no pudo insertar los clips Text+."

    for item, cue in zip(appended, cues):
      if item.GetFusionCompCount() > 0:
        comp = item.GetFusionCompByIndex(1)
        self._apply_style_to_fusion_comp(
            comp=comp,
            cue_text=exporters._cue_text(cue, style),
            style=style,
        )
    return True, f"Se insertaron {len(cues)} clips Text+ en V{target_track}."

  def _apply_style_to_fusion_comp(
      self,
      comp: Any,
      cue_text: str,
      style: StyleConfig,
  ) -> bool:
    """Populates primary TextPlus node plus multi-variant flipbook nodes."""
    if comp is None:
      return False
    tp1 = comp.FindTool("TextPlus1")
    if tp1 is None:
      tools = comp.GetToolList(False, "TextPlus")
      if isinstance(tools, dict):
        tp1 = tools.get(1) or next(iter(tools.values()), None)
      elif tools:
        tp1 = tools[0]
    if tp1 is None:
      return False

    fr, fg, fb = exporters.hex_to_rgb_norm(style.fill_hex)
    olr, olg, olb = exporters.hex_to_rgb_norm(style.outline_hex)
    br, bg, bb = exporters.hex_to_rgb_norm(style.box_hex)
    variants = style.font_variants[:5] or [
        {"font": style.font, "font_style": style.font_style}
    ]
    cycle_active = (
        style.animation_mode in ("font_cycle", "font_cycle_boil")
        and len(variants) > 1
    )

    tp1.SetInput("StyledText", cue_text)
    tp1.SetInput("Font", variants[0]["font"])
    tp1.SetInput("Style", variants[0].get("font_style") or style.font_style)
    tp1.SetInput("Size", style.size)
    tp1.SetInput("Center", {1: 0.5, 2: style.pos_y})
    tp1.SetInput("Red1", fr)
    tp1.SetInput("Green1", fg)
    tp1.SetInput("Blue1", fb)
    tp1.SetInput("Enabled2", 1 if style.outline_thickness > 0 else 0)
    tp1.SetInput("Thickness2", style.outline_thickness)
    tp1.SetInput("Red2", olr)
    tp1.SetInput("Green2", olg)
    tp1.SetInput("Blue2", olb)
    tp1.SetInput("Enabled3", 1 if style.shadow_enabled else 0)
    tp1.SetInput("Enabled4", 1 if style.box_enabled else 0)
    tp1.SetInput("Red4", br)
    tp1.SetInput("Green4", bg)
    tp1.SetInput("Blue4", bb)
    tp1.SetInput("Alpha4", style.box_alpha)

    for k in range(2, 6):
      tp_k = comp.FindTool(f"TextPlus{k}")
      if tp_k is not None:
        variant = (
            variants[(k - 1) % len(variants)] if cycle_active else variants[0]
        )
        tp_k.SetInput("Font", variant["font"])
        tp_k.SetInput("Style", variant.get("font_style") or style.font_style)

    macro_op = comp.FindTool("AutoSubs_ES_Anitype_Animado")
    if macro_op is not None:
      effective_count = min(5, len(variants)) if cycle_active else 1
      boil_val = (
          style.anim_jitter
          if style.animation_mode in ("boil", "font_cycle_boil")
          else 0.0
      )
      macro_op.SetInput("HoldFrames", int(style.anim_hold_frames))
      macro_op.SetInput("VariantCount", int(effective_count))
      macro_op.SetInput("BoilJitter", float(boil_val))
    return True

  def jump_playhead(self, ms: int) -> dict[str, Any]:
    """Moves Resolve playhead directly in Studio or stages jump_tc.lua."""
    meta = self.read_timeline_meta()
    fps = float(meta.get("fps") or 24.0)
    start_frame = int(meta.get("start_frame") or 86400)
    eff_fps = exporters.effective_fps(fps)
    elapsed_frames = max(0, int(round((max(0, ms) / 1000.0) * eff_fps)))
    tc = exporters.frames_to_smpte(start_frame + elapsed_frames, fps=fps)

    jump_lua = self.outbox_dir / "jump_tc.lua"
    jump_lua.write_text(
        f"return {{ timecode = {exporters.lua_quote(tc)}, ms = {int(ms)} }}\n",
        encoding="utf-8",
    )
    jump_table = self.outbox_dir / "jump_tc.table"
    jump_table.write_text(
        f"{{ timecode = {exporters.lua_quote(tc)}, ms = {int(ms)} }}\n",
        encoding="utf-8",
    )

    resolve = self.try_studio_connection()
    if resolve is not None:
      try:
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        if timeline and timeline.SetCurrentTimecode(tc):
          return {"ok": True, "timecode": tc, "jumped_directly": True}
      except Exception:  # pylint: disable=broad-except
        pass

    return {
        "ok": True,
        "timecode": tc,
        "jumped_directly": False,
        "message": (
            f"Timecode {tc} preparado. Ejecuta Workspace > Scripts > 3 - MTTN"
            " AutoSubs - Ir a Tiempo."
        ),
    }
