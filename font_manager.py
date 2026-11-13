"""OpenType/TrueType font metadata parser, animated pack grouper, and installer.

Responsibilities:
  * Parse raw SFNT bytes (.otf/.ttf/.ttc) with only `struct`, no fontTools:
    read the table directory, the `name` table (family/style names) and the
    `cmap` table (character -> glyph map) to check that every Spanish glyph
    in SPANISH_CHECK_CHARS (accents, ü, ñ, ¿, ¡) is actually present.
  * Group numbered font variants ("Anitype Journal 1".."5") into "animated
    packs" that the UI and the Fusion template cycle through frame by frame
    (flipbook / font-cycle animation), and group static weights by family.
  * Install uploads (single font files or ZIP packs) into the app data folder
    and, optionally, into ~/Library/Fonts so DaVinci Resolve can use them.
  * Serve installed font files to the browser UI for @font-face previews.

Main entry points: `FontManager` (used by autosubs_server.py for the
/api/fonts/* routes), `parse_font_bytes`, `extract_pack_signature`, and
`group_font_packs`.

All SFNT integers are big-endian, hence the `>` prefix in every struct format.
User-facing error strings are intentionally in Spanish (UI language).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import struct
import sys
import threading
from typing import Any
import zipfile

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
except ImportError:
  import hardware_imac2017  # type: ignore[no-redef]


# --- Constants ---

# Every character a Spanish subtitle can need beyond ASCII. A font missing any
# of these would render tofu boxes (or fall back to another font) in Resolve.
SPANISH_CHECK_CHARS = "áéíóúüñÁÉÍÓÚÜÑ¿¡"
# Upper bound for an upload and for the *uncompressed* total of a ZIP pack
# (guards against zip bombs exhausting disk/RAM).
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
SUPPORTED_FONT_EXTENSIONS = (".otf", ".ttf", ".ttc")

# Fonts assumed present on every Mac (system or bundled with Resolve). They
# are not parsed; their Spanish coverage is known to be complete.
BUILTIN_FONTS: tuple[dict[str, Any], ...] = (
    {
        "font": "Open Sans",
        "styles": ["Regular", "Semibold", "Bold", "ExtraBold"],
        "supports_spanish": True,
        "missing_spanish_chars": [],
    },
    {
        "font": "Helvetica Neue",
        "styles": ["Regular", "Medium", "Bold"],
        "supports_spanish": True,
        "missing_spanish_chars": [],
    },
    {
        "font": "SF Pro Display",
        "styles": ["Regular", "Semibold", "Bold"],
        "supports_spanish": True,
        "missing_spanish_chars": [],
    },
    {
        "font": "Montserrat",
        "styles": ["Regular", "Bold", "ExtraBold"],
        "supports_spanish": True,
        "missing_spanish_chars": [],
    },
    {
        "font": "Arial",
        "styles": ["Regular", "Bold"],
        "supports_spanish": True,
        "missing_spanish_chars": [],
    },
)


# --- Data model ---


@dataclass
class FontFaceInfo:
  """Metadata for a single parsed .otf/.ttf/.ttc font file.

  Attributes:
    font_id: 12-hex-char SHA-1 prefix, stable for a given path + names; used
      in the /api/fonts/file/<font_id> URL.
    filename: Base filename on disk.
    family: Typographic family (nameID 16, else 1, else filename stem).
    subfamily: Typographic style (nameID 17, else 2, else "Regular").
    full_name: Full font name (nameID 4).
    postscript_name: PostScript name (nameID 6), may be "".
    ext: Lower-case file extension including the dot.
    source: "uploaded" (app fonts dir) or "macos_user" (~/Library/Fonts).
    path: Absolute path of the font file.
    supports_spanish: True when no SPANISH_CHECK_CHARS glyph is missing.
    missing_spanish_chars: Spanish characters without a glyph in the cmap.
    base_family: Family name without the animated-pack frame number.
    pack_key: Normalized key used to group frames of one animated pack.
    frame_index: 1-based frame number inside an animated pack, or None.
    css_family: Unique CSS font-family name used by the UI @font-face rule.
  """

  font_id: str
  filename: str
  family: str
  subfamily: str
  full_name: str
  postscript_name: str
  ext: str
  source: str  # "uploaded" | "macos_user"
  path: str
  supports_spanish: bool
  missing_spanish_chars: list[str]
  base_family: str
  pack_key: str
  frame_index: int | None
  css_family: str

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-ready dict plus the `url` the UI fetches the file from."""
    data = asdict(self)
    data["url"] = f"/api/fonts/file/{self.font_id}"
    return data


@dataclass
class FontPackInfo:
  """Grouped font pack (single font or multi-variant flipbook such as Anitype).

  Attributes:
    pack_id: "pack_<pack_key>" for animated packs, "font_<font_id>" for
      static families.
    label: Human-readable (Spanish) label shown in the UI font picker.
    family: Family name of the first variant.
    is_animated_pack: True when >= 2 distinct numbered frames were found.
    variant_count: Number of faces in the pack.
    supports_spanish: True when no variant misses a Spanish glyph.
    missing_spanish_chars: Union of missing Spanish chars over all variants.
    source: Source of the first variant ("uploaded" or "macos_user").
    variants: Per-face dicts (font, font_style, font_id, css_family, url,
      filename) in frame order for packs, style order otherwise.
  """

  pack_id: str
  label: str
  family: str
  is_animated_pack: bool
  variant_count: int
  supports_spanish: bool
  missing_spanish_chars: list[str]
  source: str
  variants: list[dict[str, str]]

  def to_dict(self) -> dict[str, Any]:
    """Returns the pack as a JSON-ready dict."""
    return asdict(self)


# --- SFNT binary parsing (table directory, name, cmap) ---
#
# Layout reference (OpenType spec, all fields big-endian):
#   Offset table (12 bytes): sfntVersion u32 | numTables u16 |
#     searchRange u16 | entrySelector u16 | rangeShift u16
#   Table record (16 bytes each, follows the offset table):
#     tag 4s | checksum u32 | offset u32 | length u32
# Table offsets are absolute from the start of the file, even inside a TTC.


def _sfnt_offset(data: bytes) -> int:
  """Validates sfnt header (OTTO/TrueType/TTC) and returns font 0 offset.

  Recognized signatures: 0x00010000 and "true" (TrueType outlines), "OTTO"
  (CFF outlines), "typ1" (old Mac Type 1 wrapper) and "ttcf" (TrueType
  Collection). For a TTC only the first font is inspected; the TTC header is
  tag 4s | version u32 | numFonts u32 | offsetTable[numFonts] u32.

  Args:
    data: Whole font file contents.

  Returns:
    Byte offset of the offset table of font 0 (0 for single-font files).

  Raises:
    ValueError: If the file is truncated or the signature is unknown.
  """
  if len(data) < 12:
    raise ValueError("Archivo de fuente truncado (< 12 bytes).")
  tag = data[:4]
  if tag in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
    return 0
  if tag == b"ttcf":
    if len(data) < 16:
      raise ValueError("Cabecera TTC inválida.")
    num_fonts = struct.unpack_from(">I", data, 8)[0]
    if num_fonts < 1:
      raise ValueError("Colección TTC sin fuentes.")
    offset = struct.unpack_from(">I", data, 12)[0]
    if offset + 12 > len(data):
      raise ValueError("Offset TTC fuera de rango.")
    return offset
  raise ValueError(f"Firma sfnt no reconocida ({tag!r}).")


def _extract_table_directory(
    data: bytes, sfnt_offset: int
) -> dict[str, tuple[int, int]]:
  """Parses the OpenType/TrueType table directory into {tag: (offset, len)}.

  Records whose data would extend past the end of the file are silently
  dropped, so later parsers can trust every returned range. Checksums are
  not verified (not needed for metadata and often wrong in the wild).

  Args:
    data: Whole font file contents.
    sfnt_offset: Offset of the offset table, from `_sfnt_offset`.

  Returns:
    Mapping of 4-char table tag (e.g. "name", "cmap") to absolute
    (offset, length) in bytes.

  Raises:
    ValueError: If the offset table or the record array is truncated.
  """
  if sfnt_offset + 12 > len(data):
    raise ValueError("Cabecera de tablas OpenType fuera de rango.")
  _, num_tables, _, _, _ = struct.unpack_from(">4sHHHH", data, sfnt_offset)
  dir_start = sfnt_offset + 12
  dir_end = dir_start + num_tables * 16
  if dir_end > len(data):
    raise ValueError("Directorio de tablas OpenType truncado.")

  tables: dict[str, tuple[int, int]] = {}
  for idx in range(num_tables):
    rec_off = dir_start + idx * 16
    raw_tag, _, t_offset, t_length = struct.unpack_from(">4sIII", data, rec_off)
    if t_offset + t_length <= len(data):
      tag = raw_tag.decode("latin1", errors="ignore")
      tables[tag] = (t_offset, t_length)
  return tables


def _decode_name_bytes(raw: bytes, platform_id: int, encoding_id: int) -> str:
  """Decodes one `name` record string according to its platform/encoding.

  Platform 0 (Unicode) and Windows (3) encodings 0 (Symbol), 1 (BMP) and
  10 (full Unicode) are UTF-16BE; Mac (1) encoding 0 is MacRoman. Anything
  else falls back to Latin-1, which never raises.

  Args:
    raw: Raw string bytes from the name table storage area.
    platform_id: OpenType platformID of the record.
    encoding_id: OpenType encodingID of the record.

  Returns:
    Decoded text with NUL characters removed and whitespace stripped.
  """
  if platform_id == 0 or (platform_id == 3 and encoding_id in (0, 1, 10)):
    text = raw.decode("utf_16_be", errors="ignore")
  elif platform_id == 1 and encoding_id == 0:
    text = raw.decode("mac_roman", errors="ignore")
  else:
    text = raw.decode("latin1", errors="ignore")
  return text.replace("\x00", "").strip()


def _parse_name_table(data: bytes, offset: int, length: int) -> dict[int, str]:
  """Extracts nameIDs {1, 2, 4, 6, 16, 17} prioritizing Windows Unicode.

  Name table layout: header (6 bytes) = version u16 | count u16 |
  storageOffset u16, followed by `count` 12-byte records = platformID u16 |
  encodingID u16 | languageID u16 | nameID u16 | length u16 | offset u16.
  Record string offsets are relative to table start + storageOffset.

  NameIDs read: 1 family, 2 subfamily, 4 full name, 6 PostScript name,
  16 typographic family, 17 typographic subfamily. When a nameID appears in
  several records the best one wins: Windows Unicode (US English 0x0409
  preferred) > Windows Symbol > Unicode platform > MacRoman (English
  preferred) > anything else.

  Args:
    data: Whole font file contents.
    offset: Absolute offset of the `name` table.
    length: Length of the `name` table in bytes.

  Returns:
    Mapping nameID -> decoded string; empty if the table is malformed.
  """
  if length < 6 or offset + length > len(data):
    return {}
  _, count, string_offset = struct.unpack_from(">HHH", data, offset)
  records_end = offset + 6 + count * 12
  storage_start = offset + string_offset
  if records_end > offset + length or storage_start > offset + length:
    return {}

  wanted_ids = {1, 2, 4, 6, 16, 17}
  best: dict[int, tuple[int, str]] = {}
  for idx in range(count):
    rec_off = offset + 6 + idx * 12
    plat_id, enc_id, lang_id, name_id, str_len, str_off = struct.unpack_from(
        ">HHHHHH", data, rec_off
    )
    if name_id not in wanted_ids or str_len == 0:
      continue
    s_pos = storage_start + str_off
    e_pos = s_pos + str_len
    if e_pos > offset + length:
      continue
    decoded = _decode_name_bytes(data[s_pos:e_pos], plat_id, enc_id)
    if not decoded:
      continue

    # Higher score = more trustworthy record for this nameID (see docstring).
    if plat_id == 3 and enc_id in (1, 10):
      priority = 100 + (10 if lang_id == 0x0409 else 0)
    elif plat_id == 3 and enc_id == 0:
      priority = 80
    elif plat_id == 0:
      priority = 70
    elif plat_id == 1 and enc_id == 0:
      priority = 50 + (5 if lang_id == 0 else 0)
    else:
      priority = 10

    prev = best.get(name_id)
    if prev is None or priority > prev[0]:
      best[name_id] = (priority, decoded)

  return {name_id: val for name_id, (_, val) in best.items()}


def _check_cmap_format4(
    data: bytes, sub_off: int, check_codepoints: set[int]
) -> set[int]:
  """Returns codepoints in check_codepoints mapped to non-zero glyph IDs.

  Format 4 (segment mapping to delta values, BMP only) layout:
    format u16 | length u16 | language u16 | segCountX2 u16 |
    searchRange u16 | entrySelector u16 | rangeShift u16   (14 bytes)
    endCode[segCount] u16 | reservedPad u16 |
    startCode[segCount] u16 | idDelta[segCount] i16 |
    idRangeOffset[segCount] u16 | glyphIdArray[...] u16
  For a codepoint inside [startCode, endCode]:
    * idRangeOffset == 0: glyph = (cp + idDelta) mod 65536.
    * otherwise idRangeOffset is a byte offset *from its own slot* into
      glyphIdArray: addr = &idRangeOffset[i] + idRangeOffset[i] +
      2 * (cp - startCode[i]); a non-zero value there gets idDelta added.
  Glyph 0 is .notdef ("missing"), so only non-zero results count as covered.
  The final 0xFFFF/0xFFFF segment is the mandatory sentinel.

  Args:
    data: Whole font file contents.
    sub_off: Absolute offset of the format 4 subtable.
    check_codepoints: Codepoints to test (values > 0xFFFF are skipped).

  Returns:
    Subset of check_codepoints that map to a real glyph.
  """
  if sub_off + 14 > len(data):
    return set()
  _, sub_len, _, seg_count_x2, _, _, _ = struct.unpack_from(
      ">HHHHHHH", data, sub_off
  )
  seg_count = seg_count_x2 // 2
  if seg_count <= 0:
    return set()
  # The header plus the four parallel arrays and reservedPad take
  # 16 + 8 * segCount bytes; they must fit inside the declared subtable
  # length. Like FreeType, a length running past the end of the file is
  # first clamped to the bytes actually available.
  avail_len = min(sub_len, len(data) - sub_off)
  if 16 + 8 * seg_count > avail_len:
    return set()

  # Absolute offsets of the four parallel arrays; "+ 2" skips reservedPad.
  end_table = sub_off + 14
  start_table = end_table + 2 * seg_count + 2
  delta_table = start_table + 2 * seg_count
  ro_table = delta_table + 2 * seg_count
  if ro_table + 2 * seg_count > len(data):
    return set()

  covered: set[int] = set()
  # idDelta is signed ("h"); the others are unsigned ("H").
  end_codes = struct.unpack_from(f">{seg_count}H", data, end_table)
  start_codes = struct.unpack_from(f">{seg_count}H", data, start_table)
  id_deltas = struct.unpack_from(f">{seg_count}h", data, delta_table)
  id_ros = struct.unpack_from(f">{seg_count}H", data, ro_table)

  # Linear scan is fine: only ~16 codepoints are checked per font.
  for cp in check_codepoints:
    if cp > 0xFFFF:
      continue
    for seg_i in range(seg_count):
      start_c = start_codes[seg_i]
      end_c = end_codes[seg_i]
      if start_c == 0xFFFF and end_c == 0xFFFF:
        break
      if start_c <= cp <= end_c:
        ro = id_ros[seg_i]
        delta = id_deltas[seg_i]
        if ro == 0:
          gid = (cp + delta) & 0xFFFF
        else:
          ro_byte_pos = ro_table + 2 * seg_i
          gid_pos = ro_byte_pos + ro + 2 * (cp - start_c)
          if gid_pos + 2 > len(data):
            gid = 0
          else:
            raw_gid = struct.unpack_from(">H", data, gid_pos)[0]
            gid = ((raw_gid + delta) & 0xFFFF) if raw_gid != 0 else 0
        if gid != 0:
          covered.add(cp)
        break
  return covered


def _check_cmap_format12(
    data: bytes, sub_off: int, check_codepoints: set[int]
) -> set[int]:
  """Checks segmented 32-bit coverage (Format 12) for check_codepoints.

  Format 12 (segmented coverage, full Unicode) layout:
    format u16 | reserved u16 | length u32 | language u32 |
    numGroups u32   (16 bytes)
    groups[numGroups] = startCharCode u32 | endCharCode u32 |
                        startGlyphID u32   (12 bytes each)
  Glyph for cp in a group = startGlyphID + (cp - startCharCode).

  Args:
    data: Whole font file contents.
    sub_off: Absolute offset of the format 12 subtable.
    check_codepoints: Codepoints to test.

  Returns:
    Subset of check_codepoints that map to a non-zero glyph.
  """
  if sub_off + 16 > len(data):
    return set()
  _, _, _, _, num_groups = struct.unpack_from(">HHIII", data, sub_off)
  groups_start = sub_off + 16
  if groups_start + num_groups * 12 > len(data):
    return set()

  covered: set[int] = set()
  # Stop early once every codepoint is found; big CJK fonts have many groups.
  remaining = set(check_codepoints)
  for g_idx in range(num_groups):
    if not remaining:
      break
    start_c, end_c, start_gid = struct.unpack_from(
        ">III", data, groups_start + g_idx * 12
    )
    for cp in tuple(remaining):
      if start_c <= cp <= end_c:
        if start_gid + (cp - start_c) != 0:
          covered.add(cp)
          remaining.discard(cp)
  return covered


def _check_cmap_format6(
    data: bytes, sub_off: int, check_codepoints: set[int]
) -> set[int]:
  """Checks trimmed table mapping (Format 6) for check_codepoints.

  Format 6 layout: format u16 | length u16 | language u16 | firstCode u16 |
  entryCount u16 (10 bytes), then glyphIdArray[entryCount] u16 covering the
  dense range [firstCode, firstCode + entryCount).

  Args:
    data: Whole font file contents.
    sub_off: Absolute offset of the format 6 subtable.
    check_codepoints: Codepoints to test.

  Returns:
    Subset of check_codepoints that map to a non-zero glyph.
  """
  if sub_off + 10 > len(data):
    return set()
  _, _, _, first_code, entry_count = struct.unpack_from(">HHHHH", data, sub_off)
  arr_start = sub_off + 10
  if arr_start + entry_count * 2 > len(data):
    return set()
  covered: set[int] = set()
  for cp in check_codepoints:
    if first_code <= cp < first_code + entry_count:
      gid = struct.unpack_from(">H", data, arr_start + (cp - first_code) * 2)[0]
      if gid != 0:
        covered.add(cp)
  return covered


def _check_cmap_format0_mac(
    data: bytes, sub_off: int, check_codepoints: set[int]
) -> set[int]:
  """Checks a Mac Roman byte encoding table (Format 0) for check_codepoints.

  Format 0 layout: format u16 | length u16 | language u16 (6 bytes), then
  glyphIdArray[256] u8 indexed by the MacRoman byte value (262 bytes total).
  Each byte code is decoded through MacRoman to get its Unicode codepoint,
  which is how old Mac-only fonts expose á, ñ, ¿, etc.

  Args:
    data: Whole font file contents.
    sub_off: Absolute offset of the format 0 subtable.
    check_codepoints: Unicode codepoints to test.

  Returns:
    Subset of check_codepoints whose MacRoman byte maps to a non-zero glyph.
  """
  if sub_off + 262 > len(data):
    return set()
  covered: set[int] = set()
  glyph_ids = data[sub_off + 6 : sub_off + 262]
  for code, gid in enumerate(glyph_ids):
    if gid == 0:
      continue
    ch = bytes([code]).decode("mac_roman", errors="ignore")
    if ch and ord(ch) in check_codepoints:
      covered.add(ord(ch))
  return covered


def _check_spanish_cmap_coverage(
    data: bytes, offset: int, length: int
) -> tuple[bool, list[str]]:
  """Checks cmap subtables for SPANISH_CHECK_CHARS glyph coverage.

  cmap layout: version u16 | numTables u16, then numTables 8-byte encoding
  records = platformID u16 | encodingID u16 | subtableOffset u32 (relative
  to the cmap table start). Coverage is the union over every supported
  subtable: Unicode formats 4/6/12 (platform 0, or Windows 3 with encoding
  0/1/10) plus the MacRoman format 0 subtable (platform 1, encoding 0).
  Other formats (2, 8, 10, 13, 14) are ignored.

  Args:
    data: Whole font file contents.
    offset: Absolute offset of the `cmap` table.
    length: Length of the `cmap` table in bytes.

  Returns:
    (parsed, missing): `parsed` is False when no supported subtable was
    found, in which case `missing` is [] and coverage is unknown (callers
    then treat the font as Spanish-capable rather than reject it). Otherwise
    `missing` lists SPANISH_CHECK_CHARS without a glyph, in that order.
  """
  if length < 4 or offset + length > len(data):
    return False, []
  _, num_subtables = struct.unpack_from(">HH", data, offset)
  if offset + 4 + num_subtables * 8 > offset + length:
    return False, []

  target_cps = {ord(ch) for ch in SPANISH_CHECK_CHARS}
  covered: set[int] = set()
  parsed_subtable = False

  for idx in range(num_subtables):
    plat_id, enc_id, rel_off = struct.unpack_from(
        ">HHI", data, offset + 4 + idx * 8
    )
    sub_off = offset + rel_off
    if sub_off + 2 > len(data):
      continue
    fmt = struct.unpack_from(">H", data, sub_off)[0]

    is_unicode_table = plat_id == 0 or (plat_id == 3 and enc_id in (0, 1, 10))
    if is_unicode_table:
      if fmt == 12:
        parsed_subtable = True
        covered |= _check_cmap_format12(data, sub_off, target_cps)
      elif fmt == 4:
        parsed_subtable = True
        covered |= _check_cmap_format4(data, sub_off, target_cps)
      elif fmt == 6:
        parsed_subtable = True
        covered |= _check_cmap_format6(data, sub_off, target_cps)
    elif plat_id == 1 and enc_id == 0 and fmt == 0:
      parsed_subtable = True
      covered |= _check_cmap_format0_mac(data, sub_off, target_cps)

  if not parsed_subtable:
    return False, []
  missing = [ch for ch in SPANISH_CHECK_CHARS if ord(ch) not in covered]
  return True, missing


# --- Animated pack grouping heuristics ---


def _normalize_pack_key(base_name: str) -> str:
  """Lower-cases and strips non-alphanumerics ("Ani-Type 2" -> "anitype2")."""
  return re.sub(r"[^a-z0-9]+", "", base_name.lower())


def extract_pack_signature(
    family: str,
    subfamily: str,
    full_name: str = "",
    postscript_name: str = "",
    filename: str = "",
) -> tuple[str, str, int | None]:
  """Extracts (pack_key, base_family, frame_index) for animated font packs.

  Animated "flipbook" packs (e.g. Anitype) ship the same design as several
  near-identical fonts numbered 1..N; cycling them every few frames makes the
  text "boil". Vendors encode the number inconsistently, so four patterns
  are tried in order, first match wins:
    1. Style is just a number, optionally prefixed by var/variant/variante/
       frame/style/alt/v ("Anitype Journal" + "2", "Var 3").
    2. Family ends with a separator + number ("Anitype Journal 1", "Font-2").
    3. Full name or PostScript name ends with one, optionally followed by
       " Regular" ("Anitype-Journal-3", "Anitype Journal 4 Regular").
    4. Filename stem ends with one ("AnitypeJournal_5.otf").
  Only indices 1..24 count as frames, so weights like "Font 300" or years
  are not mistaken for pack members. When the family already names the
  base design, it is kept as the display name for nicer labels.

  Args:
    family: Family name from the name table.
    subfamily: Style name from the name table.
    full_name: Full font name (nameID 4).
    postscript_name: PostScript name (nameID 6).
    filename: Original filename, used as the last resort.

  Returns:
    (pack_key, base_family, frame_index): normalized grouping key, display
    family without the frame number, and the 1-based frame number or None
    when the font is not part of a numbered pack.
  """
  fam = family.strip()
  sub = subfamily.strip()

  # Pattern 1: Family is shared ("Anitype Journal"), style encodes index
  # ("1".."5").
  m_sub = re.fullmatch(
      r"(?:var(?:iant|iante)?|frame|style|alt|v)?\s*[-_ ]*(\d{1,2})",
      sub,
      flags=re.IGNORECASE,
  )
  if m_sub:
    idx = int(m_sub.group(1))
    if 1 <= idx <= 24:
      return _normalize_pack_key(fam), fam, idx

  # Pattern 2: Trailing variant index on Family ("Anitype Journal 1" /
  # "Font-2").
  m_fam = re.match(r"^(.+?)[-_ ]+(\d{1,2})$", fam)
  if m_fam:
    base = m_fam.group(1).strip()
    idx = int(m_fam.group(2))
    if 1 <= idx <= 24 and base:
      return _normalize_pack_key(base), base, idx

  # Pattern 3: Identical family, index encoded on full_name/postscript.
  for candidate in (full_name.strip(), postscript_name.strip()):
    m_cand = re.match(r"^(.+?)[-_ ]+(\d{1,2})(?:\s+Regular)?$", candidate)
    if m_cand:
      base = m_cand.group(1).strip()
      idx = int(m_cand.group(2))
      if 1 <= idx <= 24 and base:
        base_display = (
            fam
            if _normalize_pack_key(fam) == _normalize_pack_key(base)
            else base
        )
        return _normalize_pack_key(base), base_display, idx

  # Pattern 4: index only in the filename stem. Note the key is derived from
  # base_display here (unlike pattern 3, which uses base).
  stem = Path(filename).stem if filename else ""
  m_stem = re.match(r"^(.+?)[-_ ]+(\d{1,2})$", stem)
  if m_stem:
    base = m_stem.group(1).strip()
    idx = int(m_stem.group(2))
    if 1 <= idx <= 24 and base:
      base_display = (
          fam
          if fam
          and _normalize_pack_key(fam).startswith(_normalize_pack_key(base))
          else base
      )
      return _normalize_pack_key(base_display), base_display, idx

  return _normalize_pack_key(fam), fam, None


# --- Public parsing / grouping API ---


def parse_font_bytes(data: bytes, fallback_name: str = "") -> dict[str, Any]:
  """Parses OpenType/TrueType name metadata and Spanish cmap glyph coverage.

  Pure function (no disk I/O), used both for scanning installed fonts and to
  validate uploads in memory before anything is written.

  Args:
    data: Whole font file contents.
    fallback_name: Original filename; its stem names the family when the
      name table is missing, and it feeds pack pattern 4.

  Returns:
    Dict with family, subfamily, full_name, postscript_name,
    supports_spanish, missing_spanish_chars, pack_key, base_family and
    frame_index (see FontFaceInfo for meanings).

  Raises:
    ValueError: If the header is invalid or neither `name` nor `cmap`
      exists (messages are Spanish because they reach the UI).
  """
  sfnt_offset = _sfnt_offset(data)
  tables = _extract_table_directory(data, sfnt_offset)
  if "name" not in tables and "cmap" not in tables:
    raise ValueError("La fuente no contiene tablas 'name' o 'cmap' válidas.")

  names: dict[int, str] = {}
  if "name" in tables:
    n_off, n_len = tables["name"]
    names = _parse_name_table(data, n_off, n_len)

  fallback_stem = Path(fallback_name).stem or "CustomFont"
  # Typographic names (16/17) beat legacy ones (1/2): legacy family names are
  # limited to 4 styles, so "Foo Black" often hides the real family "Foo".
  family = names.get(16) or names.get(1) or fallback_stem
  subfamily = names.get(17) or names.get(2) or "Regular"
  full_name = names.get(4) or f"{family} {subfamily}".strip()
  postscript_name = names.get(6) or ""

  missing_chars: list[str] = []
  if "cmap" in tables:
    c_off, c_len = tables["cmap"]
    parsed_cmap, missing = _check_spanish_cmap_coverage(data, c_off, c_len)
    if parsed_cmap:
      missing_chars = missing

  pack_key, base_family, frame_idx = extract_pack_signature(
      family=family,
      subfamily=subfamily,
      full_name=full_name,
      postscript_name=postscript_name,
      filename=fallback_name,
  )

  return {
      "family": family,
      "subfamily": subfamily,
      "full_name": full_name,
      "postscript_name": postscript_name,
      "supports_spanish": len(missing_chars) == 0,
      "missing_spanish_chars": missing_chars,
      "pack_key": pack_key,
      "base_family": base_family,
      "frame_index": frame_idx,
  }


def group_font_packs(faces: list[FontFaceInfo]) -> list[FontPackInfo]:
  """Groups numbered font variants into animated packs and static fonts.

  A group (same pack_key) becomes an animated pack only when it holds at
  least 2 faces with at least 2 *distinct* frame numbers; otherwise its faces
  are regrouped by exact family name as static font families. The Spanish
  coverage of a group is the union of its members' missing characters, since
  the animation will show every variant.

  Args:
    faces: Parsed faces, already deduplicated by `_scan_all_faces`.

  Returns:
    Packs sorted animated first, then uploaded, then macOS fonts, each
    bucket alphabetically by label.
  """
  grouped: dict[str, list[FontFaceInfo]] = {}
  for face in faces:
    key = face.pack_key or _normalize_pack_key(face.family)
    grouped.setdefault(key, []).append(face)

  packs: list[FontPackInfo] = []
  for pack_key, items in grouped.items():
    numbered = {f.frame_index for f in items if f.frame_index is not None}
    if len(items) >= 2 and len(numbered) >= 2:
      sorted_items = sorted(
          items,
          key=lambda f: (
              f.frame_index if f.frame_index is not None else 999,
              f.family.lower(),
              f.subfamily.lower(),
          ),
      )
      base_family = sorted_items[0].base_family or sorted_items[0].family
      missing_union = [
          ch
          for ch in SPANISH_CHECK_CHARS
          if any(ch in f.missing_spanish_chars for f in sorted_items)
      ]
      packs.append(
          FontPackInfo(
              pack_id=f"pack_{pack_key}",
              label=(
                  f"{base_family} (Pack animado · {len(sorted_items)} fuentes)"
              ),
              family=sorted_items[0].family,
              is_animated_pack=True,
              variant_count=len(sorted_items),
              supports_spanish=len(missing_union) == 0,
              missing_spanish_chars=missing_union,
              source=sorted_items[0].source,
              variants=[
                  {
                      "font": f.family,
                      "font_style": f.subfamily,
                      "font_id": f.font_id,
                      "css_family": f.css_family,
                      "url": f"/api/fonts/file/{f.font_id}",
                      "filename": f.filename,
                  }
                  for f in sorted_items
              ],
          )
      )
      continue

    # Non-pack group: group static weights by family name if multiple files
    # share family.
    by_family: dict[str, list[FontFaceInfo]] = {}
    for f in items:
      by_family.setdefault(f.family, []).append(f)

    for fam_name, fam_faces in by_family.items():
      ordered = sorted(fam_faces, key=lambda f: f.subfamily.lower())
      primary = ordered[0]
      missing_union = [
          ch
          for ch in SPANISH_CHECK_CHARS
          if any(ch in f.missing_spanish_chars for f in ordered)
      ]
      # The missing-glyph warning takes precedence over the source tag so the
      # editor sees it right in the picker.
      suffix = (
          " (sin tildes/ñ)"
          if missing_union
          else " (Personalizada)"
          if primary.source == "uploaded"
          else " (macOS)"
      )
      packs.append(
          FontPackInfo(
              pack_id=f"font_{primary.font_id}",
              label=f"{fam_name}{suffix}",
              family=fam_name,
              is_animated_pack=False,
              variant_count=len(ordered),
              supports_spanish=len(missing_union) == 0,
              missing_spanish_chars=missing_union,
              source=primary.source,
              variants=[
                  {
                      "font": f.family,
                      "font_style": f.subfamily,
                      "font_id": f.font_id,
                      "css_family": f.css_family,
                      "url": f"/api/fonts/file/{f.font_id}",
                      "filename": f.filename,
                  }
                  for f in ordered
              ],
          )
      )

  packs.sort(
      key=lambda p: (
          0 if p.is_animated_pack else (1 if p.source == "uploaded" else 2),
          p.label.lower(),
      )
  )
  return packs


# --- Installation helpers ---


def _safe_font_filename(raw_name: str) -> str:
  """Sanitizes an uploaded filename for writing into the fonts folders.

  Drops any directory part (no path traversal), replaces characters outside
  [A-Za-z0-9._-] with "_" and strips leading dots (no hidden files).

  Args:
    raw_name: Filename as sent by the browser or stored in the ZIP.

  Returns:
    A safe basename, or "custom_font.otf" if nothing usable remains.
  """
  base = Path(raw_name).name
  cleaned = re.sub(r"[^A-Za-z0-9._\-]", "_", base).lstrip(".")
  return cleaned or "custom_font.otf"


def _numbered_font_name(name: str, n: int) -> str:
  """Returns `name` for n == 1, else "<stem>_<n><ext>" (e.g. Font_2.otf).

  Args:
    name: Safe font filename.
    n: 1-based attempt number.

  Returns:
    Candidate filename for that attempt.
  """
  if n == 1:
    return name
  path = Path(name)
  return f"{path.stem}_{n}{path.suffix}"


def _same_file_content(path: Path, data: bytes) -> bool:
  """Returns True if `path` is a regular file whose bytes equal `data`.

  The size is compared first so a different font is rejected without
  reading it. Unreadable files count as different.
  """
  try:
    return (
        path.is_file()
        and path.stat().st_size == len(data)
        and path.read_bytes() == data
    )
  except OSError:
    return False


def _write_font_no_overwrite(
    dest_dir: Path, name: str, data: bytes
) -> tuple[str, str]:
  """Writes a font into `dest_dir` without ever replacing a different file.

  Used for the shared user font folder (~/Library/Fonts), where an existing
  file with the same name may belong to another app or font. Each candidate
  name is created with exclusive mode ("xb"), so an existing file is never
  truncated even if it appears between the check and the write. If the
  existing file has identical bytes the font is already installed and
  nothing is written.

  Args:
    dest_dir: Existing destination folder.
    name: Preferred safe filename.
    data: Font bytes.

  Returns:
    (filename used, action) where action is "installed" (preferred name),
    "renamed" (a "_N" suffix was needed) or "skipped_identical".

  Raises:
    ValueError: If no free name is found after 999 attempts.
  """
  for n in range(1, 1000):
    candidate = _numbered_font_name(name, n)
    path = dest_dir / candidate
    try:
      with open(path, "xb") as fh:
        fh.write(data)
    except FileExistsError:
      if _same_file_content(path, data):
        return candidate, "skipped_identical"
      continue
    return candidate, "installed" if n == 1 else "renamed"
  raise ValueError(f"No hay un nombre libre para instalar {name}.")


def _make_font_id(path: Path, family: str, subfamily: str) -> str:
  """Derives a short, stable, URL-safe id for a font face.

  SHA-1 is used only as a fingerprint (not for security). The full path is
  part of the input, so the same font in ~/Library/Fonts and in the app
  folder gets different ids.

  Args:
    path: Font file path.
    family: Parsed family name.
    subfamily: Parsed style name.

  Returns:
    First 12 hex characters of the SHA-1 digest.
  """
  digest = hashlib.sha1(
      f"{path.name}|{family}|{subfamily}|{path}".encode("utf-8")
  ).hexdigest()
  return digest[:12]


# --- FontManager ---


class FontManager:
  """Manages custom fonts, ZIP font packs, macOS FontBook sync, and serving.

  Thread-safety: the HTTP server calls this from several request threads.
  `_lock` guards `_cache` and `_faces_by_id`; file parsing happens outside
  the lock so a slow scan never blocks font serving for long.

  Attributes:
    app_dir: AutoSubs ES app data folder.
    app_fonts_dir: `<app_dir>/fonts`, where uploads are stored ("uploaded").
    macos_fonts_dir: User font folder scanned as "macos_user" and used for
      installs (~/Library/Fonts on macOS, AUTOSUBS_USER_FONTS_DIR if set),
      or None on non-macOS hosts without the override.
  """

  def __init__(
      self,
      app_dir: Path | None = None,
      macos_fonts_dir: Path | None = None,
  ) -> None:
    """Initializes folders and empty caches.

    Args:
      app_dir: App data folder; defaults to
        hardware_imac2017.get_app_data_dir(create=True).
      macos_fonts_dir: Explicit user fonts folder (tests); when None, uses
        AUTOSUBS_USER_FONTS_DIR, then ~/Library/Fonts on macOS.
    """
    self.app_dir = app_dir or hardware_imac2017.get_app_data_dir(create=True)
    self.app_fonts_dir = self.app_dir / "fonts"
    self.app_fonts_dir.mkdir(parents=True, exist_ok=True)

    if macos_fonts_dir is not None:
      self.macos_fonts_dir: Path | None = macos_fonts_dir
    else:
      env_fonts = os.environ.get("AUTOSUBS_USER_FONTS_DIR")
      if env_fonts:
        self.macos_fonts_dir = Path(env_fonts).expanduser()
      elif sys.platform == "darwin":
        self.macos_fonts_dir = Path.home() / "Library" / "Fonts"
      else:
        self.macos_fonts_dir = None

    self._lock = threading.Lock()
    # "<source>:<path>" -> (st_mtime_ns, st_size, parsed face). A file is
    # re-parsed only when its mtime or size changes.
    self._cache: dict[str, tuple[int, int, FontFaceInfo]] = {}
    # font_id -> face from the latest scan; backs get_font_file().
    self._faces_by_id: dict[str, FontFaceInfo] = {}

  def _load_face(self, path: Path, source: str) -> FontFaceInfo | None:
    """Parses one font file, reusing the cached result when unchanged.

    Args:
      path: Font file to read.
      source: "uploaded" or "macos_user"; part of the cache key.

    Returns:
      The parsed face, or None if the file is unreadable or not a valid font
      (bad files in ~/Library/Fonts must not break the catalog).
    """
    try:
      st = path.stat()
    except OSError:
      return None
    cache_key = f"{source}:{path}"
    with self._lock:
      cached = self._cache.get(cache_key)
      if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    try:
      raw = path.read_bytes()
      meta = parse_font_bytes(raw, fallback_name=path.name)
    except (OSError, ValueError):
      return None

    fid = _make_font_id(path, meta["family"], meta["subfamily"])
    # Unique per face so the browser never confuses two fonts that share a
    # family name (e.g. all Anitype frames).
    css_fam = f"AutoSubsFont_{fid}"
    face = FontFaceInfo(
        font_id=fid,
        filename=path.name,
        family=meta["family"],
        subfamily=meta["subfamily"],
        full_name=meta["full_name"],
        postscript_name=meta["postscript_name"],
        ext=path.suffix.lower(),
        source=source,
        path=str(path),
        supports_spanish=meta["supports_spanish"],
        missing_spanish_chars=meta["missing_spanish_chars"],
        base_family=meta["base_family"],
        pack_key=meta["pack_key"],
        frame_index=meta["frame_index"],
        css_family=css_fam,
    )
    with self._lock:
      self._cache[cache_key] = (st.st_mtime_ns, st.st_size, face)
    return face

  def _scan_all_faces(self) -> list[FontFaceInfo]:
    """Scans app_fonts_dir and macOS user Fonts, preferring uploaded copies.

    The app folder is scanned first, so when the same face (family, style,
    full name; case-insensitive) also lives in ~/Library/Fonts the uploaded
    copy wins. Hidden files and unsupported extensions are skipped. Also
    refreshes `_faces_by_id` as a side effect.

    Returns:
      Deduplicated faces in scan order (alphabetical per folder).
    """
    seen_pairs: set[tuple[str, str, str]] = set()
    faces: list[FontFaceInfo] = []

    dirs_to_scan: list[tuple[Path, str]] = [(self.app_fonts_dir, "uploaded")]
    if self.macos_fonts_dir and self.macos_fonts_dir.is_dir():
      dirs_to_scan.append((self.macos_fonts_dir, "macos_user"))

    for directory, source in dirs_to_scan:
      try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
      except OSError:
        continue
      for path in entries:
        if (
            not path.is_file()
            or path.name.startswith(".")
            or path.suffix.lower() not in SUPPORTED_FONT_EXTENSIONS
        ):
          continue
        face = self._load_face(path, source)
        if face is None:
          continue
        dedupe_key = (
            face.family.lower(),
            face.subfamily.lower(),
            face.full_name.lower(),
        )
        if dedupe_key in seen_pairs:
          continue
        seen_pairs.add(dedupe_key)
        faces.append(face)

    with self._lock:
      self._faces_by_id = {f.font_id: f for f in faces}
    return faces

  def list_fonts(self) -> dict[str, Any]:
    """Returns built-in fonts, custom uploaded/macOS faces, and grouped packs.

    Rescans both font folders on every call (cheap thanks to the mtime
    cache).

    Returns:
      Dict with builtin_fonts, custom_faces, packs, macos_fonts_dir and
      app_fonts_dir; this is the /api/fonts JSON payload.
    """
    faces = self._scan_all_faces()
    packs = group_font_packs(faces)
    return {
        "builtin_fonts": list(BUILTIN_FONTS),
        "custom_faces": [f.to_dict() for f in faces],
        "packs": [p.to_dict() for p in packs],
        "macos_fonts_dir": (
            str(self.macos_fonts_dir) if self.macos_fonts_dir else None
        ),
        "app_fonts_dir": str(self.app_fonts_dir),
    }

  def get_font_file(self, font_id: str) -> tuple[Path, str] | None:
    """Resolves a safe font file path and MIME type for @font-face serving.

    Only ids from a scan are served, so arbitrary paths can never be
    requested. An unknown id triggers one rescan (e.g. a font installed
    since the last catalog request).

    Args:
      font_id: Id from FontFaceInfo.font_id.

    Returns:
      (path, mime) with "font/otf" for .otf and "font/ttf" otherwise
      (including .ttc), or None if unknown or deleted.
    """
    with self._lock:
      face = self._faces_by_id.get(font_id)
    if face is None:
      self._scan_all_faces()
      with self._lock:
        face = self._faces_by_id.get(font_id)
    if face is None:
      return None
    path = Path(face.path)
    if not path.is_file():
      return None
    mime = "font/otf" if path.suffix.lower() == ".otf" else "font/ttf"
    return path, mime

  def install_upload(
      self,
      filename: str,
      raw_bytes: bytes,
      install_to_macos: bool = True,
  ) -> dict[str, Any]:
    """Installs a .otf/.ttf/.ttc file or .zip font pack (with macOS sync).

    ZIP handling skips folders, __MACOSX resource forks, AppleDouble "._*"
    files, hidden entries, ".." components and non-font files, and enforces
    MAX_UPLOAD_BYTES on the declared uncompressed total. Every font is parsed
    in memory first, so one corrupt file aborts the install before anything
    is written. ZIP entries whose sanitized basenames collide (same name in
    different folders) are kept as "<stem>_2<ext>", "_3"... in ZIP order;
    byte-identical duplicates are written once. Files are then written to
    app_fonts_dir (same name overwrites the app's own previous upload) and,
    if requested, to macos_fonts_dir without ever overwriting a different
    existing file there: identical content is skipped, otherwise a "_N"
    name is used (see _write_font_no_overwrite). Resolve only picks up new
    fonts after a restart, hence `resolve_running` in the result.

    Args:
      filename: Uploaded filename; its extension selects font vs ZIP.
      raw_bytes: Uploaded file contents.
      install_to_macos: Also copy into the user font folder so Resolve and
        other apps can use the font.

    Returns:
      Dict with ok, installed_count, faces, selected_pack_id (pack that
      contains the new fonts, animated packs preferred), installed_to_macos,
      collisions (list of {"file", "installed_as", "location": "zip" |
      "macos", "action": "renamed" | "skipped_identical"}),
      resolve_running, plus every list_fonts() key.

    Raises:
      ValueError: Empty/oversized upload, bad ZIP, unsupported format, no
        fonts found, or an invalid font (Spanish messages for the UI).
    """
    if not raw_bytes:
      raise ValueError("Archivo de fuente vacío.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
      raise ValueError("El archivo supera el límite de 64 MB.")

    lower_name = filename.lower()
    extracted: list[tuple[str, bytes]] = []
    # Renamed or skipped files, reported to the UI instead of silently
    # overwriting one font with another.
    collisions: list[dict[str, str]] = []
    # Lowercased safe name -> bytes already taken from this ZIP.
    seen_in_zip: dict[str, bytes] = {}

    if lower_name.endswith(".zip"):
      total_extracted = 0
      try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
          for info in zf.infolist():
            if info.is_dir():
              continue
            parts = Path(info.filename).parts
            if (
                "__MACOSX" in parts
                or ".." in parts
                or any(p.startswith(".") for p in parts)
            ):
              continue
            base = Path(info.filename).name
            if base.startswith("._") or not base.lower().endswith(
                SUPPORTED_FONT_EXTENSIONS
            ):
              continue
            total_extracted += int(info.file_size)
            if total_extracted > MAX_UPLOAD_BYTES:
              raise ValueError(
                  "El ZIP de fuentes descomprimido supera el límite de 64 MB."
              )
            font_data = zf.read(info.filename)
            safe_name = _safe_font_filename(base)
            # Same basename in two ZIP folders (e.g. OTF/Font.otf and
            # TTF/Font.otf): keep both under deterministic "_N" names, in ZIP
            # order. Keys are lowercased because macOS volumes are usually
            # case-insensitive.
            prior = seen_in_zip.get(safe_name.lower())
            if prior is not None:
              if prior == font_data:
                collisions.append({
                    "file": info.filename,
                    "installed_as": safe_name,
                    "location": "zip",
                    "action": "skipped_identical",
                })
                continue
              n = 2
              while _numbered_font_name(safe_name, n).lower() in seen_in_zip:
                n += 1
              renamed = _numbered_font_name(safe_name, n)
              collisions.append({
                  "file": info.filename,
                  "installed_as": renamed,
                  "location": "zip",
                  "action": "renamed",
              })
              safe_name = renamed
            seen_in_zip[safe_name.lower()] = font_data
            extracted.append((safe_name, font_data))
      except zipfile.BadZipFile as exc:
        raise ValueError(f"Archivo ZIP de fuentes inválido: {exc}") from exc
    elif lower_name.endswith(SUPPORTED_FONT_EXTENSIONS):
      extracted.append((_safe_font_filename(filename), raw_bytes))
    else:
      raise ValueError(
          "Formato no compatible. Sube archivos .otf, .ttf, .ttc o .zip."
      )

    if not extracted:
      raise ValueError(
          "No se encontraron archivos .otf o .ttf válidos dentro de la subida."
      )

    for safe_name, font_bytes in extracted:
      # Validate all OpenType/TrueType headers in memory before writing to disk.
      parse_font_bytes(font_bytes, fallback_name=safe_name)

    installed_faces: list[FontFaceInfo] = []
    copied_to_macos = False
    for safe_name, font_bytes in extracted:
      app_dest = self.app_fonts_dir / safe_name
      app_dest.write_bytes(font_bytes)
      if install_to_macos and self.macos_fonts_dir is not None:
        self.macos_fonts_dir.mkdir(parents=True, exist_ok=True)
        mac_name, action = _write_font_no_overwrite(
            self.macos_fonts_dir, safe_name, font_bytes
        )
        if action != "installed":
          collisions.append({
              "file": safe_name,
              "installed_as": mac_name,
              "location": "macos",
              "action": action,
          })
        copied_to_macos = True
      face = self._load_face(app_dest, "uploaded")
      if face is not None:
        installed_faces.append(face)

    if not installed_faces:
      raise ValueError(
          "No se pudo leer ninguna fuente válida (.otf/.ttf/.ttc)."
      )

    catalog = self.list_fonts()
    installed_ids = {f.font_id for f in installed_faces}
    selected_pack_id = ""
    for pack in catalog["packs"]:
      if any(v.get("font_id") in installed_ids for v in pack["variants"]):
        selected_pack_id = pack["pack_id"]
        if pack["is_animated_pack"]:
          break

    return {
        "ok": True,
        "installed_count": len(installed_faces),
        "faces": [f.to_dict() for f in installed_faces],
        "selected_pack_id": selected_pack_id,
        "installed_to_macos": copied_to_macos,
        "collisions": collisions,
        "macos_fonts_dir": catalog["macos_fonts_dir"],
        "resolve_running": hardware_imac2017.is_resolve_running(),
        **catalog,
    }
