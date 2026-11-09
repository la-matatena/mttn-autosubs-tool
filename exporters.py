"""Export serializers: SRT, VTT, ASS Karaoke, FCPXML 1.9, and Resolve Lua."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as xml_escape
import json
import math
from typing import Any
import unicodedata

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import spanish_nlp
except ImportError:
  import spanish_nlp  # type: ignore[no-redef]


SubtitleCue = spanish_nlp.SubtitleCue

VALID_ANIMATION_MODES = frozenset(
    {"none", "font_cycle", "boil", "font_cycle_boil"}
)
# shortcut: <=2500 FCPXML title slices (~5 min at hold=3), use AutoSubs_ES_Anitype_Animado Text+ template on longer timelines
_MAX_ANIMATED_FCPXML_SLICES = 2500

_ASCII_FOLD_TABLE = str.maketrans({
    "á": "a",
    "é": "e",
    "í": "i",
    "ó": "o",
    "ú": "u",
    "ü": "u",
    "ñ": "n",
    "Á": "A",
    "É": "E",
    "Í": "I",
    "Ó": "O",
    "Ú": "U",
    "Ü": "U",
    "Ñ": "N",
    "¿": "",
    "¡": "",
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "—": "-",
    "–": "-",
    "…": "...",
})


def fold_spanish_to_ascii(text: str) -> str:
  """Adapts Spanish text to Basic ASCII when a display font lacks accents/ñ."""
  normalized = unicodedata.normalize("NFC", text).translate(_ASCII_FOLD_TABLE)
  decomposed = unicodedata.normalize("NFKD", normalized)
  return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _clamp01(value: Any, default: float) -> float:
  """Parses an opacity value into [0, 1], falling back to `default`."""
  try:
    num = float(value)
  except (TypeError, ValueError):
    return default
  if num != num:  # NaN
    return default
  return max(0.0, min(num, 1.0))


@dataclass
class StyleConfig:
  """Visual styling options shared across Preview, ASS Karaoke, and Text+."""

  preset_name: str = "Cine Netflix"
  font: str = "Open Sans"
  font_style: str = "Bold"
  size: float = 0.058
  pos_y: float = 0.14
  fill_hex: str = "#FFFFFF"
  highlight_hex: str = "#FFD43B"
  outline_hex: str = "#0B0D12"
  outline_thickness: float = 0.09
  shadow_enabled: bool = True
  box_enabled: bool = False
  box_hex: str = "#0B0D12"
  box_alpha: float = 0.72
  # Opacity (0 = transparent, 1 = opaque) per shading layer. Defaults keep
  # the historical output byte-identical.
  fill_alpha: float = 1.0
  highlight_alpha: float = 1.0
  outline_alpha: float = 1.0
  shadow_alpha: float = 0.65
  uppercase: bool = False
  ascii_fold: bool = False
  animation_mode: str = "none"
  anim_hold_frames: int = 3
  anim_jitter: float = 0.15
  font_variants: list[dict[str, str]] = field(default_factory=list)
  karaoke_enabled: bool = False

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> StyleConfig:
    if not raw:
      return cls()
    anim_mode = str(raw.get("animation_mode", "none"))
    if anim_mode not in VALID_ANIMATION_MODES:
      anim_mode = "none"

    variants: list[dict[str, str]] = []
    raw_variants = raw.get("font_variants")
    if isinstance(raw_variants, list):
      for item in raw_variants[:5]:
        if isinstance(item, dict) and str(item.get("font", "")).strip():
          variants.append({
              "font": str(item["font"]).strip(),
              "font_style": str(item.get("font_style") or "Regular").strip(),
          })

    return cls(
        preset_name=str(raw.get("preset_name", "Cine Netflix")),
        font=str(raw.get("font", "Open Sans")),
        font_style=str(raw.get("font_style", "Bold")),
        size=max(0.02, min(float(raw.get("size", 0.058)), 0.25)),
        pos_y=max(0.03, min(float(raw.get("pos_y", 0.14)), 0.92)),
        fill_hex=str(raw.get("fill_hex", "#FFFFFF")),
        highlight_hex=str(raw.get("highlight_hex", "#FFD43B")),
        outline_hex=str(raw.get("outline_hex", "#0B0D12")),
        outline_thickness=max(
            0.0, min(float(raw.get("outline_thickness", 0.09)), 0.35)
        ),
        shadow_enabled=bool(raw.get("shadow_enabled", True)),
        box_enabled=bool(raw.get("box_enabled", False)),
        box_hex=str(raw.get("box_hex", "#0B0D12")),
        box_alpha=_clamp01(raw.get("box_alpha"), 0.72),
        fill_alpha=_clamp01(raw.get("fill_alpha"), 1.0),
        highlight_alpha=_clamp01(raw.get("highlight_alpha"), 1.0),
        outline_alpha=_clamp01(raw.get("outline_alpha"), 1.0),
        shadow_alpha=_clamp01(raw.get("shadow_alpha"), 0.65),
        uppercase=bool(raw.get("uppercase", False)),
        ascii_fold=bool(raw.get("ascii_fold", False)),
        animation_mode=anim_mode,
        anim_hold_frames=max(1, min(int(raw.get("anim_hold_frames", 3)), 12)),
        anim_jitter=max(0.0, min(float(raw.get("anim_jitter", 0.15)), 1.0)),
        font_variants=variants,
        karaoke_enabled=bool(raw.get("karaoke_enabled", False)),
    )


def hex_to_rgb_norm(hex_color: str) -> tuple[float, float, float]:
  """Converts '#RRGGBB' into normalized (r, g, b) floats in [0.0, 1.0]."""
  h = hex_color.strip().lstrip("#")
  if len(h) != 6:
    return (1.0, 1.0, 1.0)
  try:
    return (
        int(h[0:2], 16) / 255.0,
        int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0,
    )
  except ValueError:
    return (1.0, 1.0, 1.0)


def hex_to_ass_bgr(hex_color: str, alpha_0_1: float = 1.0) -> str:
  """Converts '#RRGGBB' + opacity into ASS '&HAABBGGRR' color string."""
  h = hex_color.strip().lstrip("#")
  if len(h) != 6:
    h = "FFFFFF"
  r, g, b = h[0:2].upper(), h[2:4].upper(), h[4:6].upper()
  ass_alpha = int(round((1.0 - max(0.0, min(1.0, alpha_0_1))) * 255.0))
  return f"&H{ass_alpha:02X}{b}{g}{r}"


def format_ms_srt(ms: int) -> str:
  total_ms = max(0, int(ms))
  hours = total_ms // 3_600_000
  minutes = (total_ms % 3_600_000) // 60_000
  seconds = (total_ms % 60_000) // 1_000
  millis = total_ms % 1_000
  return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_ms_vtt(ms: int) -> str:
  return format_ms_srt(ms).replace(",", ".")


def format_ms_ass(ms: int) -> str:
  total_cs = max(0, int(round(ms / 10.0)))
  hours = total_cs // 360_000
  minutes = (total_cs % 360_000) // 6_000
  seconds = (total_cs % 6_000) // 100
  cs = total_cs % 100
  return f"{hours:d}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def _fps_rational(fps: float) -> tuple[int, int, str]:
  """Returns (num, den, frameDuration_str) matching DaVinci Resolve rates."""
  rounded = round(fps, 3)
  table = {
      23.976: (24000, 1001, "1001/24000s"),
      23.98: (24000, 1001, "1001/24000s"),
      24.0: (2400, 100, "100/2400s"),
      25.0: (2500, 100, "100/2500s"),
      29.97: (30000, 1001, "1001/30000s"),
      30.0: (3000, 100, "100/3000s"),
      50.0: (5000, 100, "100/5000s"),
      59.94: (60000, 1001, "1001/60000s"),
      60.0: (6000, 100, "100/6000s"),
  }
  for key, val in table.items():
    if abs(rounded - key) < 0.005:
      return val
  num = max(1, int(round(fps * 100)))
  return (num, 100, f"100/{num}s")


def effective_fps(fps: float = 24.0) -> float:
  """Returns exact Resolve frame rate (e.g. 24000/1001 for 23.976)."""
  num, den, _ = _fps_rational(fps)
  return num / float(den)


def frames_to_smpte(total_frames: int, fps: float = 24.0) -> str:
  """Formats a frame count into NDF SMPTE HH:MM:SS:FF timecode."""
  nominal = max(1, int(round(fps)))
  frames = max(0, int(total_frames))
  ff = frames % nominal
  total_sec = frames // nominal
  ss = total_sec % 60
  mm = (total_sec // 60) % 60
  hh = total_sec // 3600
  return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def format_ms_smpte(ms: int, fps: float = 24.0) -> str:
  """Formats milliseconds into SMPTE HH:MM:SS:FF timecode."""
  eff = effective_fps(fps)
  return frames_to_smpte(max(0, int(round((max(0, ms) / 1000.0) * eff))), fps)


def ms_to_timeline_tc_ms(
    ms: int, start_frame: int = 86400, fps: float = 24.0
) -> int:
  """Converts audio elapsed ms into NDF timeline SRT ms without 23.976/29.97 drift."""
  nominal = max(1, int(round(fps)))
  eff = effective_fps(fps)
  start_tc_ms = (max(0, int(start_frame)) / float(nominal)) * 1000.0
  elapsed_ms = max(0, int(ms))
  if abs(eff - nominal) < 1e-4:
    return int(round(start_tc_ms)) + elapsed_ms
  elapsed_tc_ms = elapsed_ms * (eff / float(nominal))
  return int(round(start_tc_ms + elapsed_tc_ms))


def _apply_style_text_transforms(
    text: str, style: StyleConfig | None = None
) -> str:
  if style and style.ascii_fold:
    text = fold_spanish_to_ascii(text)
  if style and style.uppercase:
    text = text.upper()
  return text


def _cue_text(cue: SubtitleCue, style: StyleConfig | None = None) -> str:
  return _apply_style_text_transforms(cue.text, style)


def export_srt(
    cues: list[SubtitleCue],
    offset_ms: int = 0,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to SubRip (.srt), optionally shifted to timeline TC."""
  blocks: list[str] = []
  for idx, cue in enumerate(cues, start=1):
    s = format_ms_srt(cue.start_ms + offset_ms)
    e = format_ms_srt(max(cue.start_ms + 40, cue.end_ms) + offset_ms)
    blocks.append(f"{idx}\n{s} --> {e}\n{_cue_text(cue, style)}")
  return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_srt_timeline_tc(
    cues: list[SubtitleCue],
    start_frame: int = 86400,
    fps: float = 24.0,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to SRT aligned with Resolve NDF timeline timecode."""
  blocks: list[str] = []
  for idx, cue in enumerate(cues, start=1):
    s_ms = ms_to_timeline_tc_ms(cue.start_ms, start_frame=start_frame, fps=fps)
    e_ms = max(
        s_ms + 40,
        ms_to_timeline_tc_ms(
            max(cue.start_ms + 40, cue.end_ms), start_frame=start_frame, fps=fps
        ),
    )
    blocks.append(
        f"{idx}\n{format_ms_srt(s_ms)} --> {format_ms_srt(e_ms)}\n"
        f"{_cue_text(cue, style)}"
    )
  return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_vtt(
    cues: list[SubtitleCue],
    offset_ms: int = 0,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to WebVTT (.vtt)."""
  blocks: list[str] = ["WEBVTT"]
  for idx, cue in enumerate(cues, start=1):
    s = format_ms_vtt(cue.start_ms + offset_ms)
    e = format_ms_vtt(max(cue.start_ms + 40, cue.end_ms) + offset_ms)
    blocks.append(f"{idx}\n{s} --> {e}\n{_cue_text(cue, style)}")
  return "\n\n".join(blocks) + "\n"


def export_txt(
    cues: list[SubtitleCue],
    include_timestamps: bool = False,
    offset_ms: int = 0,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to plain text script or timestamped transcript (.txt)."""
  if not cues:
    return ""
  lines: list[str] = []
  for cue in cues:
    flat_text = " ".join(
        ln.strip() for ln in _cue_text(cue, style).splitlines() if ln.strip()
    )
    if not flat_text:
      continue
    if include_timestamps:
      s = format_ms_srt(cue.start_ms + offset_ms)
      e = format_ms_srt(max(cue.start_ms + 40, cue.end_ms) + offset_ms)
      lines.append(f"[{s} --> {e}] {flat_text}")
    else:
      lines.append(flat_text)
  return "\n".join(lines) + ("\n" if lines else "")


def _sanitize_ass_field(val: str) -> str:
  return val.replace(",", " ").replace("\r", " ").replace("\n", " ").strip()


def _escape_ass_text(val: str) -> str:
  return val.replace("{", r"\{").replace("}", r"\}")


def export_ass_karaoke(
    cues: list[SubtitleCue],
    style: StyleConfig | None = None,
    width: int = 1920,
    height: int = 1080,
) -> str:
  """Serializes cues to SubStation Alpha (.ass), with optional \\kf karaoke.

  With `style.karaoke_enabled` each word sweeps from the fill colour to the
  highlight colour; otherwise every cue is rendered in the fill colour.
  """
  st = style or StyleConfig()
  font_name = _sanitize_ass_field(st.font) or "Open Sans"
  font_px = max(24, int(round(st.size * height * 1.15)))
  margin_v = max(24, int(round(st.pos_y * height * 0.6)))
  fill = hex_to_ass_bgr(st.fill_hex, st.fill_alpha)
  highlight = hex_to_ass_bgr(st.highlight_hex, st.highlight_alpha)
  # \kf sweeps SecondaryColour -> PrimaryColour, so karaoke needs
  # Primary=highlight; static text must use Primary=fill.
  primary, secondary = (highlight, fill) if st.karaoke_enabled else (fill, fill)
  fill_override = "{\\1c&H" + fill[4:] + "&\\1a&H" + fill[2:4] + "&}"
  # BorderStyle 3 (opaque box) paints the box with OutlineColour, while
  # BackColour is always the drop shadow.
  if st.box_enabled:
    outline = hex_to_ass_bgr(st.box_hex, st.box_alpha)
  else:
    outline = hex_to_ass_bgr(st.outline_hex, st.outline_alpha)
  back = hex_to_ass_bgr(st.box_hex, st.shadow_alpha)
  border_style = 3 if st.box_enabled else 1
  outline_px = 6 if st.box_enabled else max(1, round(st.outline_thickness * 32))
  shadow_px = 2 if (st.shadow_enabled and not st.box_enabled) else 0
  bold_flag = -1 if "bold" in st.font_style.lower() else 0

  header = "\n".join([
      "[Script Info]",
      "Title: AutoSubs ES",
      "ScriptType: v4.00+",
      f"PlayResX: {width}",
      f"PlayResY: {height}",
      "WrapStyle: 0",
      "ScaledBorderAndShadow: yes",
      "",
      "[V4+ Styles]",
      (
          "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
          " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
          " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
          " Alignment, MarginL, MarginR, MarginV, Encoding"
      ),
      (
          f"Style: AutoSubsES,{font_name},{font_px},{primary},{secondary},"
          f"{outline},{back},{bold_flag},0,0,0,100,100,0,0,{border_style},"
          f"{outline_px},{shadow_px},2,60,60,{margin_v},1"
      ),
      "",
      "[Events]",
      (
          "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV,"
          " Effect, Text"
      ),
  ])

  events: list[str] = []
  for cue in cues:
    start_str = format_ms_ass(cue.start_ms)
    end_str = format_ms_ass(max(cue.start_ms + 40, cue.end_ms))
    speaker = _sanitize_ass_field(cue.speaker) or "Hablante 1"
    line_tokens = sum(len(ln.split()) for ln in cue.lines)
    if st.karaoke_enabled and cue.words and line_tokens == len(cue.words):
      # Map words onto line breaks using the cue's wrapped lines.
      word_idx = 0
      line_parts: list[str] = []
      for line in cue.lines:
        tokens = line.split()
        k_tokens: list[str] = []
        for tok in tokens:
          w = cue.words[word_idx]
          dur_cs = max(2, int(round((w.end_ms - w.start_ms) / 10.0)))
          word_idx += 1
          txt = _escape_ass_text(_apply_style_text_transforms(tok, st))
          k_tokens.append(f"{{\\kf{dur_cs}}}{txt}")
        line_parts.append(" ".join(k_tokens))
      dialogue = r"\N".join(line_parts)
    else:
      dialogue = _escape_ass_text(_cue_text(cue, st)).replace("\n", r"\N")
      if st.karaoke_enabled:
        dialogue = fill_override + dialogue
    events.append(
        f"Dialogue: 0,{start_str},{end_str},AutoSubsES,{speaker},0,0,0,,"
        f"{dialogue}"
    )

  return header + "\n" + "\n".join(events) + "\n"


def _frames_to_fcpxml_time(frames: int, num: int, den: int) -> str:
  if frames <= 0:
    return "0s"
  return f"{frames * den}/{num}s"


def export_fcpxml(
    cues: list[SubtitleCue],
    timeline_name: str = "AutoSubs ES",
    fps: float = 24.0,
    start_frame: int = 86400,
    width: int = 1920,
    height: int = 1080,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to FCPXML 1.9 Basic Titles compatible with Resolve 20."""
  st = style or StyleConfig()
  num, den, frame_dur = _fps_rational(fps)
  effective_fps = num / float(den)
  font_px = max(24, int(round(st.size * height * 1.15)))
  r, g, b = hex_to_rgb_norm(st.fill_hex)
  # Resolve may ignore the alpha component on import (documented as partial).
  fill_a = f"{st.fill_alpha:g}"
  # FCPXML Basic Title Center Y: bottom safe area roughly -360..420 on 1080p.
  y_offset_px = int(round((st.pos_y - 0.5) * height))

  end_ms = max([cue.end_ms for cue in cues], default=1000)
  total_frames = max(1, int(round((end_ms / 1000.0) * effective_fps)) + 24)
  tc_start = _frames_to_fcpxml_time(start_frame, num, den)
  spine_dur = _frames_to_fcpxml_time(total_frames, num, den)

  cycle_variants: list[dict[str, str]] = []
  if (
      st.animation_mode in ("font_cycle", "font_cycle_boil")
      and len(st.font_variants) > 1
  ):
    hold = max(2, st.anim_hold_frames)
    est_slices = 0
    for cue in cues:
      s_f = max(0, int(round((cue.start_ms / 1000.0) * effective_fps)))
      e_f = max(s_f + 1, int(round((cue.end_ms / 1000.0) * effective_fps)))
      est_slices += max(1, math.ceil((e_f - s_f) / hold))
    if est_slices <= _MAX_ANIMATED_FCPXML_SLICES:
      cycle_variants = st.font_variants[:5]

  title_nodes: list[str] = []
  style_counter = 0
  hold = max(2, st.anim_hold_frames)
  for idx, cue in enumerate(cues, start=1):
    s_frame = max(0, int(round((cue.start_ms / 1000.0) * effective_fps)))
    e_frame = max(
        s_frame + 1, int(round((cue.end_ms / 1000.0) * effective_fps))
    )
    text_body = xml_escape(_cue_text(cue, st))
    short_name = xml_escape(" ".join(cue.lines)[:32] or f"Sub {idx}")

    if cycle_variants:
      cur_frame = s_frame
      var_idx = 0
      while cur_frame < e_frame:
        next_frame = min(e_frame, cur_frame + hold)
        slice_dur = max(1, next_frame - cur_frame)
        variant = cycle_variants[var_idx % len(cycle_variants)]
        v_font = variant.get("font") or st.font
        v_face = variant.get("font_style") or st.font_style
        style_counter += 1
        style_id = f"ts{style_counter}"
        offset_str = _frames_to_fcpxml_time(start_frame + cur_frame, num, den)
        dur_str = _frames_to_fcpxml_time(slice_dur, num, den)
        style_block = (
            f'          <text-style-def id="{style_id}">\n'
            f'            <text-style font="{xml_escape(v_font)}"'
            f' fontSize="{font_px}" fontFace="{xml_escape(v_face)}"'
            f' fontColor="{r:.3f} {g:.3f} {b:.3f} {fill_a}" alignment="center"/>\n'
            "          </text-style-def>"
        )
        title_nodes.append(
            f'            <title name="{short_name}" lane="1" ref="r2"'
            f' offset="{offset_str}" duration="{dur_str}" start="0s">\n        '
            '      <param name="Position"'
            ' key="9999/999166631/999166633/1/100/101" value="0'
            f' {y_offset_px}"/>\n              <text>\n               '
            f' <text-style ref="{style_id}">{text_body}</text-style>\n         '
            f"     </text>\n{style_block}\n            </title>"
        )
        cur_frame = next_frame
        var_idx += 1
    else:
      dur_frames = e_frame - s_frame
      offset_str = _frames_to_fcpxml_time(start_frame + s_frame, num, den)
      dur_str = _frames_to_fcpxml_time(dur_frames, num, den)
      style_counter += 1
      style_id = f"ts{style_counter}"
      style_block = (
          f'          <text-style-def id="{style_id}">\n'
          f'            <text-style font="{xml_escape(st.font)}"'
          f' fontSize="{font_px}" fontFace="{xml_escape(st.font_style)}"'
          f' fontColor="{r:.3f} {g:.3f} {b:.3f} {fill_a}" alignment="center"/>\n'
          "          </text-style-def>"
      )
      title_nodes.append(
          f'            <title name="{short_name}" lane="1" ref="r2"'
          f' offset="{offset_str}" duration="{dur_str}" start="0s">\n          '
          '    <param name="Position" key="9999/999166631/999166633/1/100/101"'
          f' value="0 {y_offset_px}"/>\n              <text>\n               '
          f' <text-style ref="{style_id}">{text_body}</text-style>\n           '
          f"   </text>\n{style_block}\n            </title>"
      )

  titles_xml = "\n".join(title_nodes)
  return (
      '<?xml version="1.0" encoding="UTF-8"?>\n'
      "<!DOCTYPE fcpxml>\n"
      '<fcpxml version="1.9">\n'
      "  <resources>\n"
      f'    <format id="r1" name="FFVideoFormat{height}p{int(round(fps))}"'
      f' frameDuration="{frame_dur}" width="{width}" height="{height}"/>\n'
      '    <effect id="r2" name="Basic Title"'
      ' uid=".../Titles.localized/Bumper:Opener.localized/Basic'
      ' Title.localized/Basic Title.moti"/>\n'
      "  </resources>\n"
      "  <library>\n"
      '    <event name="AutoSubs ES">\n'
      f'      <project name="{xml_escape(timeline_name)}">\n'
      f'        <sequence format="r1" duration="{spine_dur}"'
      f' tcStart="{tc_start}" tcFormat="NDF">\n'
      "          <spine>\n"
      f'            <gap name="AutoSubs Base" offset="{tc_start}"'
      f' duration="{spine_dur}" start="{tc_start}">\n'
      f"{titles_xml}\n"
      "            </gap>\n"
      "          </spine>\n"
      "        </sequence>\n"
      "      </project>\n"
      "    </event>\n"
      "  </library>\n"
      "</fcpxml>\n"
  )


def lua_quote(value: str) -> str:
  """Safely escapes a Python string into a single-line Lua string literal."""
  escaped = (
      value.replace("\\", "\\\\")
      .replace('"', '\\"')
      .replace("\r", "")
      .replace("\n", "\\n")
  )
  return f'"{escaped}"'


def export_resolve_lua_payload(
    cues: list[SubtitleCue],
    mode: str,
    track_name: str,
    template_name: str,
    srt_path: str,
    srt_timeline_tc_path: str,
    fcpxml_path: str,
    fps: float = 24.0,
    timeline_start_frame: int = 86400,
    style: StyleConfig | None = None,
    include_return: bool = True,
) -> str:
  """Serializes cues and styling into a Lua module or pure bmd.readfile table."""
  st = style or StyleConfig()
  fr, fg, fb = hex_to_rgb_norm(st.fill_hex)
  olr, olg, olb = hex_to_rgb_norm(st.outline_hex)
  br, bg, bb = hex_to_rgb_norm(st.box_hex)

  eff_fps = effective_fps(fps)
  cue_entries: list[str] = []
  for idx, cue in enumerate(cues, start=1):
    s_frame = max(0, int(round((cue.start_ms / 1000.0) * eff_fps)))
    e_frame = max(s_frame + 1, int(round((cue.end_ms / 1000.0) * eff_fps)))
    dur_frames = max(1, e_frame - s_frame)
    cue_entries.append(
        "    {\n"
        f"      index = {idx},\n"
        f"      start_ms = {cue.start_ms},\n"
        f"      end_ms = {cue.end_ms},\n"
        f"      start_frame_offset = {s_frame},\n"
        f"      duration_frames = {dur_frames},\n"
        f"      text = {lua_quote(_cue_text(cue, st))},\n"
        f"      speaker = {lua_quote(cue.speaker)},\n"
        "    }"
    )

  variants = st.font_variants[:5] or [
      {"font": st.font, "font_style": st.font_style}
  ]
  variants_lua = ", ".join(
      f"{{ font = {lua_quote(v['font'])}, "
      f"font_style = {lua_quote(v.get('font_style') or 'Regular')} }}"
      for v in variants
  )

  cues_block = ",\n".join(cue_entries)
  header = (
      "-- Auto-generated by AutoSubs ES\nreturn {\n"
      if include_return
      else "{\n"
  )
  return (
      f"{header}  mode = {lua_quote(mode)},\n  track_name ="
      f" {lua_quote(track_name)},\n  template_name ="
      f" {lua_quote(template_name)},\n  srt_path = {lua_quote(srt_path)},\n "
      f" srt_timeline_tc_path = {lua_quote(srt_timeline_tc_path)},\n "
      f" fcpxml_path = {lua_quote(fcpxml_path)},\n  fps = {fps:.4f},\n "
      f" timeline_start_frame = {int(timeline_start_frame)},\n  style = {{\n   "
      f" font = {lua_quote(st.font)},\n    font_style ="
      f" {lua_quote(st.font_style)},\n    size = {st.size:.4f},\n    pos_y ="
      f" {st.pos_y:.4f},\n    fill_r = {fr:.4f}, fill_g = {fg:.4f}, fill_b ="
      f" {fb:.4f}, fill_a = {st.fill_alpha:.4f},\n    outline_r = {olr:.4f},"
      f" outline_g = {olg:.4f}, outline_b = {olb:.4f}, outline_a ="
      f" {st.outline_alpha:.4f},\n    shadow_a = {st.shadow_alpha:.4f},\n   "
      f" outline_thickness = {st.outline_thickness:.4f},\n   "
      f" shadow_enabled = {'true' if st.shadow_enabled else 'false'},\n   "
      f" box_enabled = {'true' if st.box_enabled else 'false'},\n    box_r ="
      f" {br:.4f}, box_g = {bg:.4f}, box_b = {bb:.4f},\n    box_alpha ="
      f" {st.box_alpha:.4f},\n    uppercase ="
      f" {'true' if st.uppercase else 'false'},\n    ascii_fold ="
      f" {'true' if st.ascii_fold else 'false'},\n    animation_mode ="
      f" {lua_quote(st.animation_mode)},\n    anim_hold_frames ="
      f" {int(st.anim_hold_frames)},\n    anim_jitter ="
      f" {st.anim_jitter:.4f},\n    font_variants = {{ {variants_lua} }},\n "
      f" }},\n  cues = {{\n{cues_block}\n  }},\n}}\n"
  )


def render_export(
    fmt: str,
    cues: list[SubtitleCue],
    base_stem: str = "subtitulos_es",
    style: StyleConfig | None = None,
    fps: float = 24.0,
    start_frame: int = 86400,
    width: int = 1920,
    height: int = 1080,
    timeline_name: str = "AutoSubs ES",
) -> tuple[str, str, str]:
  """Renders (content, filename, mime_type) for any supported export format."""
  fmt_l = str(fmt or "srt").strip().lower()
  if fmt_l == "srt":
    return (
        export_srt(cues, 0, style),
        f"{base_stem}.srt",
        "text/plain; charset=utf-8",
    )
  if fmt_l == "srt_tc":
    return (
        export_srt_timeline_tc(
            cues, start_frame=start_frame, fps=fps, style=style
        ),
        f"{base_stem}_resolve_tc.srt",
        "text/plain; charset=utf-8",
    )
  if fmt_l == "vtt":
    return (
        export_vtt(cues, 0, style),
        f"{base_stem}.vtt",
        "text/vtt; charset=utf-8",
    )
  if fmt_l == "ass":
    return (
        export_ass_karaoke(cues, style, width, height),
        f"{base_stem}_karaoke.ass"
        if style and style.karaoke_enabled
        else f"{base_stem}.ass",
        "text/plain; charset=utf-8",
    )
  if fmt_l == "txt":
    return (
        export_txt(cues, include_timestamps=False, style=style),
        f"{base_stem}.txt",
        "text/plain; charset=utf-8",
    )
  if fmt_l == "txt_tc":
    return (
        export_txt(cues, include_timestamps=True, offset_ms=0, style=style),
        f"{base_stem}_tiempos.txt",
        "text/plain; charset=utf-8",
    )
  if fmt_l == "fcpxml":
    return (
        export_fcpxml(
            cues, timeline_name, fps, start_frame, width, height, style
        ),
        f"{base_stem}.fcpxml",
        "application/xml; charset=utf-8",
    )
  if fmt_l == "json":
    return (
        json.dumps([c.to_dict() for c in cues], ensure_ascii=False, indent=2),
        f"{base_stem}.json",
        "application/json; charset=utf-8",
    )
  raise ValueError(f"Formato no soportado: {fmt}")
