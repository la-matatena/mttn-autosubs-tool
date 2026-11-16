"""Export serializers: SRT, VTT, ASS Karaoke, FCPXML 1.9, and Resolve Lua.

Converts `spanish_nlp.SubtitleCue` lists (times in ms from the start of the
audio) into text files for editors and players, plus the Lua payload read
by the DaVinci Resolve scripts in `lua/`. Pure string building, no I/O:
callers (`autosubs_server.py`, `resolve_bridge.py`) write the results.

Main entry points:
  * `render_export(fmt, cues, ...)`: dispatcher used by the HTTP export
    endpoint; returns (content, filename, mime_type).
  * `export_srt`, `export_srt_timeline_tc`, `export_vtt`, `export_txt`,
    `export_ass_karaoke`, `export_fcpxml`: one serializer per format.
  * `export_resolve_lua_payload`: Lua table consumed by the Resolve scripts.
  * `StyleConfig`: visual style shared by the preview, ASS, and Text+.

Timecode conventions:
  * Resolve timelines start at 01:00:00:00 by default, i.e. frame 86400 at
    24 fps (`start_frame` defaults).
  * NTSC rates (23.976, 29.97, 59.94) are really N*1000/1001 fps.
    `effective_fps` returns that exact rate for ms <-> frame conversion,
    while NDF (non-drop-frame) timecode labels count `round(fps)` frames
    per "second". The two drift apart ~3.6 s per hour, which
    `ms_to_timeline_tc_ms` compensates for.
"""

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
  # Standalone checkout (outside google3): import the sibling module.
  import spanish_nlp  # type: ignore[no-redef]


# --- Constants and text transforms ---

SubtitleCue = spanish_nlp.SubtitleCue

# Allowed `StyleConfig.animation_mode` values; anything else becomes "none".
#   font_cycle: rotate through `font_variants` every `anim_hold_frames`.
#   boil: stepped "organic vibration" jitter (BoilTransform in the Text+
#     template and the UI preview); ignored by the FCPXML export.
#   font_cycle_boil: both.
VALID_ANIMATION_MODES = frozenset(
    {"none", "font_cycle", "boil", "font_cycle_boil"}
)
# shortcut: <=2500 FCPXML title slices (~5 min at hold=3), use AutoSubs_ES_Anitype_Animado Text+ template on longer timelines
_MAX_ANIMATED_FCPXML_SLICES = 2500

# Spanish -> ASCII replacements for fonts without Latin-1 glyphs. Inverted
# marks are dropped (¿ ¡) and typographic quotes/dashes/ellipsis become
# their plain ASCII equivalents.
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
  """Adapts Spanish text to Basic ASCII when a display font lacks accents/ñ.

  Applies `_ASCII_FOLD_TABLE` on NFC text, then NFKD-decomposes and drops
  combining marks so any remaining accented letter (à, ç, ...) also folds.
  Non-Latin characters without a decomposition are kept as is.
  """
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


# --- Style configuration ---


@dataclass
class StyleConfig:
  """Visual styling options shared across Preview, ASS Karaoke, and Text+.

  Sizes and positions are resolution independent (fractions of the frame
  height) so the same style works for 1080p, 4K, and vertical video.

  Attributes:
    preset_name: Name of the UI style preset (informational).
    font: Font family name as installed on the system.
    font_style: Font face ("Bold", "Regular", ...); ASS only checks whether
      it contains "bold".
    size: Text height as a fraction of frame height (0.02..0.25).
    pos_y: Vertical position measured up from the bottom edge, as a
      fraction of frame height (0.03..0.92). Each exporter approximates it
      differently (see `export_ass_karaoke` and `export_fcpxml`).
    fill_hex: Text colour "#RRGGBB".
    highlight_hex: Karaoke sweep colour "#RRGGBB".
    outline_hex: Outline colour "#RRGGBB".
    outline_thickness: Outline width in Text+ units (0..0.35); ASS maps it
      to pixels as `round(x * 32)`.
    shadow_enabled: Draw a drop shadow (ignored when `box_enabled`).
    box_enabled: Draw an opaque background box instead of an outline.
    box_hex: Box colour "#RRGGBB".
    box_alpha: Box opacity, 0 transparent .. 1 opaque.
    fill_alpha: Text opacity.
    highlight_alpha: Karaoke highlight opacity.
    outline_alpha: Outline opacity.
    shadow_alpha: Shadow opacity (shadow colour is always black).
    uppercase: Uppercase text at export time.
    ascii_fold: Replace accents/ñ/¿¡ for fonts lacking Spanish glyphs.
    animation_mode: One of `VALID_ANIMATION_MODES`.
    anim_hold_frames: Frames each font variant stays on screen (1..12).
    anim_jitter: Boil jitter strength (0..1).
    font_variants: Up to 5 dicts {"font", "font_style"} for font cycling.
    karaoke_enabled: ASS export uses per-word `\\kf` sweeps.
  """

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
    """Builds a validated style from UI/API JSON.

    Missing keys use the dataclass defaults; numbers are clamped to the
    ranges documented in the class Attributes, unknown animation modes
    become "none", and only the first 5 font variants with a non-empty
    "font" are kept.

    Args:
      raw: Partial style dict, or None/empty for all defaults.

    Returns:
      A new StyleConfig.

    Raises:
      ValueError: If a numeric field (size, pos_y, ...) is not a number.
    """
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


# --- Colour and time formatting helpers ---


def hex_to_rgb_norm(hex_color: str) -> tuple[float, float, float]:
  """Converts '#RRGGBB' into normalized (r, g, b) floats in [0.0, 1.0].

  Invalid input falls back to white (1.0, 1.0, 1.0). Used by FCPXML
  `fontColor` and the Lua payload (Fusion Text+ expects 0..1 channels).
  """
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
  """Converts '#RRGGBB' + opacity into ASS '&HAABBGGRR' color string.

  ASS quirks: channels are stored in reverse order (blue, green, red) and
  alpha is inverted (00 = opaque, FF = fully transparent). Example:
  "#FFD43B" at opacity 1.0 -> "&H003BD4FF".

  Args:
    hex_color: Colour as "#RRGGBB" (the "#" is optional). Invalid input
      falls back to white.
    alpha_0_1: Opacity, 0 transparent .. 1 opaque (clamped).

  Returns:
    The 10-character ASS colour literal "&HAABBGGRR".
  """
  h = hex_color.strip().lstrip("#")
  if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
    h = "FFFFFF"
  r, g, b = h[0:2].upper(), h[2:4].upper(), h[4:6].upper()
  ass_alpha = int(round((1.0 - max(0.0, min(1.0, alpha_0_1))) * 255.0))
  return f"&H{ass_alpha:02X}{b}{g}{r}"


def format_ms_srt(ms: int) -> str:
  """Formats ms as SRT time "HH:MM:SS,mmm" (comma before millis).

  Negative values clamp to 0. Hours are not wrapped at 24.
  """
  total_ms = max(0, int(ms))
  hours = total_ms // 3_600_000
  minutes = (total_ms % 3_600_000) // 60_000
  seconds = (total_ms % 60_000) // 1_000
  millis = total_ms % 1_000
  return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_ms_vtt(ms: int) -> str:
  """Formats ms as WebVTT time "HH:MM:SS.mmm" (dot before millis)."""
  return format_ms_srt(ms).replace(",", ".")


def _ms_to_ass_cs(ms: int) -> int:
  """Rounds ms to whole ASS centiseconds (negative values clamp to 0).

  Shared by `format_ms_ass` and the karaoke `\\k` math so event times and
  word boundaries use exactly the same rounding.
  """
  return max(0, int(round(ms / 10.0)))


def format_ms_ass(ms: int) -> str:
  """Formats ms as ASS time "H:MM:SS.cc" (1-digit hours, centiseconds).

  ASS only has 10 ms resolution, so times are rounded to the nearest
  centisecond.
  """
  total_cs = _ms_to_ass_cs(ms)
  hours = total_cs // 360_000
  minutes = (total_cs % 360_000) // 6_000
  seconds = (total_cs % 6_000) // 100
  cs = total_cs % 100
  return f"{hours:d}:{minutes:02d}:{seconds:02d}.{cs:02d}"


# --- Frame rate and timecode math ---


def _fps_rational(fps: float) -> tuple[int, int, str]:
  """Returns (num, den, frameDuration_str) matching DaVinci Resolve rates.

  The exact rate is `num / den` frames per second and one frame lasts
  `den / num` seconds (the FCPXML `frameDuration`). NTSC rates use the
  1000/1001 factor (e.g. 23.976 -> 24000/1001); integer rates use a /100
  denominator (e.g. 24 -> 2400/100, frameDuration "100/2400s").

  Args:
    fps: Frame rate as shown in the UI (23.976, 24, 29.97, ...).

  Returns:
    (num, den, "den/nums"). Known rates match within 0.005 fps; any other
    rate is approximated as round(fps * 100) / 100.
  """
  rounded = round(fps, 3)
  # 23.976 and 23.98 map to the same rate but are both needed: with the
  # 0.005 tolerance, 23.976 covers (23.971, 23.981) and 23.98 extends the
  # match to 23.985 (e.g. 23.984 -> 24000/1001). Dict order makes 23.976
  # win where they overlap, which is harmless since the values are equal.
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
  """Returns exact Resolve frame rate (e.g. 24000/1001 for 23.976).

  Use this, not `fps`, whenever converting real time (ms) to frames.
  """
  num, den, _ = _fps_rational(fps)
  return num / float(den)


def frames_to_smpte(total_frames: int, fps: float = 24.0) -> str:
  """Formats a frame count into NDF SMPTE HH:MM:SS:FF timecode.

  Non-drop-frame: every timecode second holds exactly `round(fps)` frames
  (24 for 23.976, 30 for 29.97), matching Resolve's NDF timeline labels.
  """
  nominal = max(1, int(round(fps)))
  frames = max(0, int(total_frames))
  ff = frames % nominal
  total_sec = frames // nominal
  ss = total_sec % 60
  mm = (total_sec // 60) % 60
  hh = total_sec // 3600
  return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def format_ms_smpte(ms: int, fps: float = 24.0) -> str:
  """Formats milliseconds into SMPTE HH:MM:SS:FF timecode.

  Real time is converted to a frame index with the exact rate
  (`effective_fps`), then labelled as NDF timecode by `frames_to_smpte`.
  """
  eff = effective_fps(fps)
  return frames_to_smpte(max(0, int(round((max(0, ms) / 1000.0) * eff))), fps)


def ms_to_timeline_tc_ms(
    ms: int, start_frame: int = 86400, fps: float = 24.0
) -> int:
  """Converts audio elapsed ms into NDF timeline SRT ms without NTSC drift.

  Resolve reads SRT times as timecode labels, and at NTSC rates an NDF
  timecode "second" (`round(fps)` frames) lasts 1001/1000 real seconds.
  So real elapsed time is scaled by `effective_fps / nominal` (= 1000/1001)
  before adding the timeline start label. Without it, subtitles would drift
  ~3.6 s late per hour at 23.976 / 29.97.

  Args:
    ms: Elapsed time in ms from the start of the audio.
    start_frame: Timeline start as a frame count (86400 = 01:00:00:00 at
      24 fps nominal).
    fps: Timeline frame rate.

  Returns:
    Milliseconds to write into the SRT so it lines up with the timeline.
  """
  nominal = max(1, int(round(fps)))
  eff = effective_fps(fps)
  start_tc_ms = (max(0, int(start_frame)) / float(nominal)) * 1000.0
  elapsed_ms = max(0, int(ms))
  if abs(eff - nominal) < 1e-4:
    # Integer rates: timecode seconds are real seconds, no scaling needed.
    return int(round(start_tc_ms)) + elapsed_ms
  elapsed_tc_ms = elapsed_ms * (eff / float(nominal))
  return int(round(start_tc_ms + elapsed_tc_ms))


def _apply_style_text_transforms(
    text: str, style: StyleConfig | None = None
) -> str:
  """Applies export-time `ascii_fold` then `uppercase` from `style`.

  These only affect exported text; cue data is never modified. With no
  style, the text is returned unchanged.
  """
  if style and style.ascii_fold:
    text = fold_spanish_to_ascii(text)
  if style and style.uppercase:
    text = text.upper()
  return text


def _cue_text(cue: SubtitleCue, style: StyleConfig | None = None) -> str:
  """Returns the cue's multi-line text with style transforms applied."""
  return _apply_style_text_transforms(cue.text, style)


# --- Plain-text formats: SRT, VTT, TXT ---
# Timed outputs enforce a minimum displayed duration of 40 ms per cue and
# renumber blocks from 1 in list order (ignoring `SubtitleCue.index`).


def export_srt(
    cues: list[SubtitleCue],
    offset_ms: int = 0,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to SubRip (.srt), optionally shifted to timeline TC.

  Args:
    cues: Cues with times in ms from the start of the audio.
    offset_ms: Constant shift added to every time (no NTSC drift
      correction; use `export_srt_timeline_tc` for Resolve timelines).
    style: Optional export-time text transforms.

  Returns:
    SRT text: blocks "N\\nstart --> end\\ntext" separated by blank lines,
    with a trailing newline (empty string for no cues).
  """
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
  """Serializes cues to SRT aligned with Resolve NDF timeline timecode.

  Times are mapped with `ms_to_timeline_tc_ms`, so the file can be imported
  onto a timeline that starts at `start_frame` (default 01:00:00:00) and
  stays in sync at NTSC rates.

  Args:
    cues: Cues with times in ms from the start of the audio.
    start_frame: Timeline start frame (NDF, nominal fps).
    fps: Timeline frame rate.
    style: Optional export-time text transforms.

  Returns:
    SRT text (same layout as `export_srt`).
  """
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
  """Serializes cues to WebVTT (.vtt).

  Same as `export_srt` but with the "WEBVTT" header, "." millisecond
  separator, and a trailing newline even when there are no cues. Numeric
  cue identifiers are kept for parity with SRT.
  """
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
  """Serializes cues to plain text script or timestamped transcript (.txt).

  Each cue becomes one line with its wrapped lines joined by spaces; empty
  cues are skipped.

  Args:
    cues: Cues to export.
    include_timestamps: Prefix each line with "[start --> end] " in SRT
      time format.
    offset_ms: Constant shift for the timestamps.
    style: Optional export-time text transforms.

  Returns:
    The text with a trailing newline, or "" when nothing is exported.
  """
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


# --- ASS / SubStation Alpha (karaoke) ---


def _sanitize_ass_field(val: str) -> str:
  """Makes a value safe for a comma-separated ASS field (font, Name).

  ASS `Style:`/`Dialogue:` lines are CSV without quoting, so commas and
  line breaks are replaced by spaces.
  """
  return val.replace(",", " ").replace("\r", " ").replace("\n", " ").strip()


def _escape_ass_text(val: str) -> str:
  """Escapes braces so dialogue text cannot open an ASS override block."""
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

  Format quirks handled here:
    * Colours are "&HAABBGGRR" (BGR order, inverted alpha); see
      `hex_to_ass_bgr`.
    * Times are "H:MM:SS.cc" (centiseconds); `\\kf` durations are also in
      centiseconds and cumulative from the event start, so silences before
      and between words are written as empty `{\\kNN}` spacers.
    * Line breaks inside dialogue are the literal `\\N`.
    * Karaoke needs exactly one `cue.words` entry per displayed token; if
      that mapping is broken the cue falls back to static fill text.

  Args:
    cues: Cues with times in ms from the start of the audio.
    style: Visual style; defaults to `StyleConfig()`.
    width: PlayResX, the script's virtual canvas width in px.
    height: PlayResY in px; font size and margins scale with it.

  Returns:
    The complete .ass script text with a single "AutoSubsES" style.
  """
  st = style or StyleConfig()
  font_name = _sanitize_ass_field(st.font) or "Open Sans"
  # Empirical factors converting the relative size / position (fractions of
  # frame height) into ASS pixels; both clamp to a 24 px minimum.
  font_px = max(24, int(round(st.size * height * 1.15)))
  margin_v = max(24, int(round(st.pos_y * height * 0.6)))
  fill = hex_to_ass_bgr(st.fill_hex, st.fill_alpha)
  highlight = hex_to_ass_bgr(st.highlight_hex, st.highlight_alpha)
  # \kf sweeps SecondaryColour -> PrimaryColour, so karaoke needs
  # Primary=highlight; static text must use Primary=fill.
  primary, secondary = (highlight, fill) if st.karaoke_enabled else (fill, fill)
  # Inline override that forces the fill colour (\1c = BBGGRR part) and
  # alpha (\1a = AA part) for karaoke cues rendered without word timings.
  fill_override = "{\\1c&H" + fill[4:] + "&\\1a&H" + fill[2:4] + "&}"
  # BorderStyle 3 (opaque box) paints the box with OutlineColour, while
  # BackColour is always the drop shadow.
  if st.box_enabled:
    outline = hex_to_ass_bgr(st.box_hex, st.box_alpha)
  else:
    outline = hex_to_ass_bgr(st.outline_hex, st.outline_alpha)
  back = hex_to_ass_bgr("#000000", st.shadow_alpha)
  border_style = 3 if st.box_enabled else 1
  outline_px = 6 if st.box_enabled else max(1, round(st.outline_thickness * 32))
  shadow_px = 2 if (st.shadow_enabled and not st.box_enabled) else 0
  bold_flag = -1 if "bold" in st.font_style.lower() else 0  # ASS: -1 = on.

  # Style fields after colours: Bold, Italic, Underline, StrikeOut,
  # ScaleX/Y 100%, Spacing 0, Angle 0, BorderStyle, Outline, Shadow,
  # Alignment 2 (bottom centre), MarginL/R 60 px, MarginV, Encoding 1.
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
    event_end_ms = max(cue.start_ms + 40, cue.end_ms)
    start_str = format_ms_ass(cue.start_ms)
    end_str = format_ms_ass(event_end_ms)
    speaker = _sanitize_ass_field(cue.speaker) or "Hablante 1"
    line_tokens = sum(len(ln.split()) for ln in cue.lines)
    if st.karaoke_enabled and cue.words and line_tokens == len(cue.words):
      # Map words onto line breaks using the cue's wrapped lines.
      # \k durations are cumulative: a syllable starts sweeping once the
      # sum of all previous \k values has elapsed since the event start.
      # So each word boundary is taken from absolute times, rounded to cs
      # exactly like the event Start field (no cumulative rounding drift),
      # and silences become empty "{\kNN}" spacers before the word's
      # "{\kfNN}". That keeps every sweep at the word's real `start_ms`,
      # like the UI preview. Boundaries are clamped to the event and kept
      # monotonic, so out-of-range or overlapping words never yield
      # negative values (a zero-length word fills instantly: \kf0).
      event_start_cs = _ms_to_ass_cs(cue.start_ms)
      event_len_cs = max(0, _ms_to_ass_cs(event_end_ms) - event_start_cs)
      cursor_cs = 0  # Sum of all \k values emitted so far in this event.
      word_idx = 0
      line_parts: list[str] = []
      for line in cue.lines:
        tokens = line.split()
        k_tokens: list[str] = []
        for tok in tokens:
          w = cue.words[word_idx]
          word_idx += 1
          s_cs = _ms_to_ass_cs(w.start_ms) - event_start_cs
          s_cs = min(max(s_cs, cursor_cs), event_len_cs)
          e_cs = _ms_to_ass_cs(w.end_ms) - event_start_cs
          e_cs = min(max(e_cs, s_cs), event_len_cs)
          gap_cs = s_cs - cursor_cs
          spacer = f"{{\\k{gap_cs}}}" if gap_cs > 0 else ""
          cursor_cs = e_cs
          txt = _escape_ass_text(_apply_style_text_transforms(tok, st))
          k_tokens.append(f"{spacer}{{\\kf{e_cs - s_cs}}}{txt}")
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


# --- FCPXML 1.9 (Basic Titles for Resolve) ---


def _frames_to_fcpxml_time(frames: int, num: int, den: int) -> str:
  """Formats a frame count as an FCPXML rational time string.

  FCPXML times are exact rationals in seconds ("N/Ds"). With a rate of
  num/den fps, `frames` frames last `frames * den / num` seconds, e.g.
  48 frames at 24000/1001 -> "48048/24000s". The fraction is not reduced,
  which keeps every value a multiple of the frameDuration as FCPXML
  requires.

  Args:
    frames: Frame count (offset or duration).
    num: Rate numerator from `_fps_rational`.
    den: Rate denominator from `_fps_rational`.

  Returns:
    "0s" for frames <= 0, else "{frames*den}/{num}s".
  """
  if frames <= 0:
    return "0s"
  return f"{frames * den}/{num}s"


# Motion parameter path of the Basic Title "Position" param in FCPXML.
_FCPXML_POSITION_KEY = "9999/999166631/999166633/1/100/101"


def _xml_tag(tag: str, attrs: dict[str, str], self_closing: bool) -> str:
  """Builds an XML start tag (or empty-element tag) with escaped attributes.

  Attributes keep the dict's insertion order, so the output is stable.

  Args:
    tag: Element name, e.g. "title".
    attrs: Raw (unescaped) attribute values by name.
    self_closing: Whether to emit `<tag .../>` instead of `<tag ...>`.

  Returns:
    The tag text, e.g. '<param name="Position" value="0 -389"/>'.
  """
  attr_text = "".join(
      f' {key}="{xml_escape(val)}"' for key, val in attrs.items()
  )
  return f"<{tag}{attr_text}{'/' if self_closing else ''}>"


def _fcpxml_title(
    *,
    name: str,
    offset: str,
    duration: str,
    y_offset_px: int,
    style_id: str,
    text: str,
    font: str,
    face: str,
    color: str,
    font_px: int,
) -> str:
  """Serializes one connected Basic Title `<title>` for `export_fcpxml`.

  Each title embeds its own `<text-style-def>` (referenced by `style_id`)
  after its `<text>`. Indentation is fixed so the element nests under the
  spine `<gap>` (12 spaces); the style def's shallower 10-space indent is
  cosmetic only and kept so existing exports stay byte-identical.

  Args:
    name: Raw title name shown in the timeline (escaped here).
    offset: Absolute timeline offset (`_frames_to_fcpxml_time` string).
    duration: Title duration (`_frames_to_fcpxml_time` string).
    y_offset_px: Vertical position in px from frame centre (positive = up).
    style_id: Text style id, e.g. "ts1".
    text: Raw cue text (escaped here); its "\\n" line breaks are kept.
    font: Raw font family name (escaped here).
    face: Raw font face, e.g. "Bold" (escaped here).
    color: Normalized "r g b a" fill colour.
    font_px: Font size in px.

  Returns:
    The title XML, without a trailing newline.
  """
  title_tag = _xml_tag(
      "title",
      {
          "name": name,
          "lane": "1",
          "ref": "r2",
          "offset": offset,
          "duration": duration,
          "start": "0s",
      },
      self_closing=False,
  )
  position_tag = _xml_tag(
      "param",
      {
          "name": "Position",
          "key": _FCPXML_POSITION_KEY,
          "value": f"0 {y_offset_px}",
      },
      self_closing=True,
  )
  style_tag = _xml_tag(
      "text-style",
      {
          "font": font,
          "fontSize": str(font_px),
          "fontFace": face,
          "fontColor": color,
          "alignment": "center",
      },
      self_closing=True,
  )
  text_ref = f'<text-style ref="{style_id}">{xml_escape(text)}</text-style>'
  lines = [
      (12, title_tag),
      (14, position_tag),
      (14, "<text>"),
      (16, text_ref),
      (14, "</text>"),
      (10, f'<text-style-def id="{style_id}">'),
      (12, style_tag),
      (10, "</text-style-def>"),
      (12, "</title>"),
  ]
  return "\n".join(" " * indent + line for indent, line in lines)


def export_fcpxml(
    cues: list[SubtitleCue],
    timeline_name: str = "AutoSubs ES",
    fps: float = 24.0,
    start_frame: int = 86400,
    width: int = 1920,
    height: int = 1080,
    style: StyleConfig | None = None,
) -> str:
  """Serializes cues to FCPXML 1.9 Basic Titles compatible with Resolve 20.

  Structure: one `<format>` and one Basic Title `<effect>` resource, then
  a sequence whose spine holds a single `<gap>` with every cue as a
  connected `<title>` on lane 1. Each title carries its own
  `<text-style-def>` (ids ts1, ts2, ...).

  Time quirks:
    * All times are rational seconds (`_frames_to_fcpxml_time`), computed
      from frame indices at the exact rate (`_fps_rational`), so NTSC
      timelines land on exact frame boundaries.
    * The sequence `tcStart` and the gap `start` both equal `start_frame`,
      so title offsets are absolute timeline times (start_frame + frame).
    * The spine lasts until the last cue end plus 24 frames of padding.

  Font cycling: for "font_cycle" modes with >= 2 variants, each cue is cut
  into slices of `anim_hold_frames` that rotate through the variants, as
  long as the total stays under `_MAX_ANIMATED_FCPXML_SLICES`; otherwise
  static titles are written. Boil jitter is not representable here.

  Args:
    cues: Cues with times in ms from the start of the audio.
    timeline_name: Project name shown in Resolve on import.
    fps: Timeline frame rate.
    start_frame: Timeline start frame (86400 = 01:00:00:00 at 24 fps).
    width: Frame width in px for the `<format>`.
    height: Frame height in px; also scales font size and Y position.
    style: Visual style; defaults to `StyleConfig()`.

  Returns:
    The complete FCPXML document as a string.
  """
  st = style or StyleConfig()
  num, den, frame_dur = _fps_rational(fps)
  # Named `eff_fps` so it does not shadow the module-level `effective_fps()`.
  eff_fps = num / float(den)
  font_px = max(24, int(round(st.size * height * 1.15)))
  r, g, b = hex_to_rgb_norm(st.fill_hex)
  # Resolve may ignore the alpha component on import (documented as partial).
  fill_a = f"{st.fill_alpha:g}"
  # FCPXML Basic Title Center Y: bottom safe area roughly -360..420 on 1080p.
  # Position is in px relative to frame centre (positive = up), so
  # pos_y=0.14 on 1080p gives -389 (near the bottom).
  y_offset_px = int(round((st.pos_y - 0.5) * height))

  end_ms = max([cue.end_ms for cue in cues], default=1000)
  total_frames = max(1, int(round((end_ms / 1000.0) * eff_fps)) + 24)
  tc_start = _frames_to_fcpxml_time(start_frame, num, den)
  spine_dur = _frames_to_fcpxml_time(total_frames, num, den)

  # Decide up front whether font cycling fits the slice budget (see the
  # `_MAX_ANIMATED_FCPXML_SLICES` shortcut note); if not, export static.
  cycle_variants: list[dict[str, str]] = []
  if (
      st.animation_mode in ("font_cycle", "font_cycle_boil")
      and len(st.font_variants) > 1
  ):
    hold = max(1, st.anim_hold_frames)
    est_slices = 0
    for cue in cues:
      s_f = max(0, int(round((cue.start_ms / 1000.0) * eff_fps)))
      e_f = max(s_f + 1, int(round((cue.end_ms / 1000.0) * eff_fps)))
      est_slices += max(1, math.ceil((e_f - s_f) / hold))
    if est_slices <= _MAX_ANIMATED_FCPXML_SLICES:
      cycle_variants = st.font_variants[:5]

  # Every <title> sets the Basic Title "Position" param (see
  # `_fcpxml_title`). Text keeps its "\n" line breaks, which the title
  # renders as separate lines.
  title_nodes: list[str] = []
  style_counter = 0
  hold = max(1, st.anim_hold_frames)
  color = f"{r:.3f} {g:.3f} {b:.3f} {fill_a}"
  for idx, cue in enumerate(cues, start=1):
    s_frame = max(0, int(round((cue.start_ms / 1000.0) * eff_fps)))
    e_frame = max(s_frame + 1, int(round((cue.end_ms / 1000.0) * eff_fps)))
    text = _cue_text(cue, st)
    short_name = " ".join(cue.lines)[:32] or f"Sub {idx}"

    if cycle_variants:
      # One title per hold-slice, rotating fonts (a flipbook effect).
      cur_frame = s_frame
      var_idx = 0
      while cur_frame < e_frame:
        next_frame = min(e_frame, cur_frame + hold)
        slice_dur = max(1, next_frame - cur_frame)
        variant = cycle_variants[var_idx % len(cycle_variants)]
        style_counter += 1
        title_nodes.append(
            _fcpxml_title(
                name=short_name,
                offset=_frames_to_fcpxml_time(
                    start_frame + cur_frame, num, den
                ),
                duration=_frames_to_fcpxml_time(slice_dur, num, den),
                y_offset_px=y_offset_px,
                style_id=f"ts{style_counter}",
                text=text,
                font=variant.get("font") or st.font,
                face=variant.get("font_style") or st.font_style,
                color=color,
                font_px=font_px,
            )
        )
        cur_frame = next_frame
        var_idx += 1
    else:
      style_counter += 1
      title_nodes.append(
          _fcpxml_title(
              name=short_name,
              offset=_frames_to_fcpxml_time(start_frame + s_frame, num, den),
              duration=_frames_to_fcpxml_time(e_frame - s_frame, num, den),
              y_offset_px=y_offset_px,
              style_id=f"ts{style_counter}",
              text=text,
              font=st.font,
              face=st.font_style,
              color=color,
              font_px=font_px,
          )
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


# --- Resolve Lua payload ---


def lua_quote(value: str) -> str:
  """Safely escapes a Python string into a single-line Lua string literal.

  Escapes backslashes and double quotes, drops CR, and encodes LF as "\\n"
  so multi-line subtitle text stays on one source line.
  """
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
  """Serializes cues and styling into a Lua module or pure bmd.readfile table.

  Consumed by `lua/2_AutoSubs_ES_Insertar_Subtitulos.lua` (and script 3).
  `resolve_bridge.py` writes two variants of the same data:
    * `include_return=True`: "return { ... }" module for `loadfile`/`load`.
    * `include_return=False`: bare "{ ... }" table, because Resolve's
      `bmd.readfile` parses raw table literals and returns nil on a
      `return` statement.

  Payload shape: top-level `mode`, `track_name`, `template_name`, the three
  file paths, `fps`, `timeline_start_frame`; a `style` table (colours as
  0..1 floats split into r/g/b/a fields, flags as Lua booleans); and a
  `cues` array whose entries hold `start_frame_offset` / `duration_frames`
  relative to the timeline start (frames at `effective_fps`), the original
  ms times, the styled `text`, and `speaker`.

  Args:
    cues: Cues with times in ms from the start of the audio.
    mode: Insertion mode read by the Lua script (e.g. "subtitle_srt",
      "fcpxml", or Text+ clip modes).
    track_name: Name for the created track.
    template_name: Text+ template (Media Pool title) to instance.
    srt_path: Absolute path of the plain SRT export.
    srt_timeline_tc_path: Absolute path of the timeline-TC SRT export.
    fcpxml_path: Absolute path of the FCPXML export.
    fps: Timeline frame rate.
    timeline_start_frame: Fallback timeline start frame; the Lua script
      prefers the live `timeline:GetStartFrame()` and adds each cue's
      `start_frame_offset` to it.
    style: Visual style; defaults to `StyleConfig()`.
    include_return: Prefix the table with a comment and "return".

  Returns:
    Lua source text.
  """
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

  # Without explicit variants the base font is the only "variant", so the
  # Text+ template always receives at least one entry.
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
  # Indentation inside the literal mimics a hand-written Lua table; Lua
  # ignores it. Booleans must be emitted as lowercase true/false.
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


# --- Export dispatcher ---


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
  """Renders (content, filename, mime_type) for any supported export format.

  Args:
    fmt: Case-insensitive format id: "srt", "srt_tc" (Resolve timeline
      TC), "vtt", "ass" (karaoke if `style.karaoke_enabled`), "txt",
      "txt_tc" (timestamped), "fcpxml", or "json" (raw cue dicts). Empty
      means "srt".
    cues: Cues to export.
    base_stem: File name without extension; a suffix is added for some
      formats (_resolve_tc, _karaoke, _tiempos).
    style: Visual style / text transforms (not applied to "json").
    fps: Timeline frame rate (srt_tc, fcpxml).
    start_frame: Timeline start frame (srt_tc, fcpxml).
    width: Frame width in px (ass, fcpxml).
    height: Frame height in px (ass, fcpxml).
    timeline_name: FCPXML project name.

  Returns:
    (file content, download file name, HTTP Content-Type).

  Raises:
    ValueError: If `fmt` is not supported (message is user-facing Spanish).
  """
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
