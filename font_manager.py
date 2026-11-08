"""OpenType/TrueType font metadata parser, animated pack grouper, and installer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import threading
from typing import Any
import zipfile

try:
  from google3.experimental.users.joseluiszapata.autosubs_es import hardware_imac2017
except ImportError:
  import hardware_imac2017  # type: ignore[no-redef]


SPANISH_CHECK_CHARS = "áéíóúüñÁÉÍÓÚÜÑ¿¡"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
SUPPORTED_FONT_EXTENSIONS = (".otf", ".ttf", ".ttc")

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


@dataclass
class FontFaceInfo:
  """Metadata for a single parsed .otf/.ttf/.ttc font file."""

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
    data = asdict(self)
    data["url"] = f"/api/fonts/file/{self.font_id}"
    return data


@dataclass
class FontPackInfo:
  """Grouped font pack (single font or multi-variant flipbook such as Anitype)."""

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
    return asdict(self)


def _sfnt_offset(data: bytes) -> int:
  """Validates sfnt header (OTTO/TrueType/TTC) and returns font 0 offset."""
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
  """Parses the OpenType/TrueType table directory into {tag: (offset, len)}."""
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
  if platform_id == 0 or (platform_id == 3 and encoding_id in (0, 1, 10)):
    text = raw.decode("utf_16_be", errors="ignore")
  elif platform_id == 1 and encoding_id == 0:
    text = raw.decode("mac_roman", errors="ignore")
  else:
    text = raw.decode("latin1", errors="ignore")
  return text.replace("\x00", "").strip()


def _parse_name_table(data: bytes, offset: int, length: int) -> dict[int, str]:
  """Extracts nameIDs {1, 2, 4, 6, 16, 17} prioritizing Windows Unicode."""
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
  """Returns codepoints in check_codepoints mapped to non-zero glyph IDs."""
  if sub_off + 14 > len(data):
    return set()
  _, sub_len, _, seg_count_x2, _, _, _ = struct.unpack_from(
      ">HHHHHHH", data, sub_off
  )
  seg_count = seg_count_x2 // 2
  if seg_count <= 0 or sub_off + min(sub_len, len(data) - sub_off) > len(data):
    return set()

  end_table = sub_off + 14
  start_table = end_table + 2 * seg_count + 2
  delta_table = start_table + 2 * seg_count
  ro_table = delta_table + 2 * seg_count
  if ro_table + 2 * seg_count > len(data):
    return set()

  covered: set[int] = set()
  end_codes = struct.unpack_from(f">{seg_count}H", data, end_table)
  start_codes = struct.unpack_from(f">{seg_count}H", data, start_table)
  id_deltas = struct.unpack_from(f">{seg_count}h", data, delta_table)
  id_ros = struct.unpack_from(f">{seg_count}H", data, ro_table)

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
  """Checks segmented 32-bit coverage (Format 12) for check_codepoints."""
  if sub_off + 16 > len(data):
    return set()
  _, _, _, _, num_groups = struct.unpack_from(">HHIII", data, sub_off)
  groups_start = sub_off + 16
  if groups_start + num_groups * 12 > len(data):
    return set()

  covered: set[int] = set()
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
  """Checks cmap subtables for SPANISH_CHECK_CHARS glyph coverage."""
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


def _normalize_pack_key(base_name: str) -> str:
  return re.sub(r"[^a-z0-9]+", "", base_name.lower())


def extract_pack_signature(
    family: str,
    subfamily: str,
    full_name: str = "",
    postscript_name: str = "",
    filename: str = "",
) -> tuple[str, str, int | None]:
  """Extracts (pack_key, base_family, frame_index) for animated font packs."""
  fam = family.strip()
  sub = subfamily.strip()

  # Pattern 1: Family is shared ("Anitype Journal"), style encodes index ("1".."5").
  m_sub = re.fullmatch(
      r"(?:var(?:iant|iante)?|frame|style|alt|v)?\s*[-_ ]*(\d{1,2})",
      sub,
      flags=re.IGNORECASE,
  )
  if m_sub:
    idx = int(m_sub.group(1))
    if 1 <= idx <= 24:
      return _normalize_pack_key(fam), fam, idx

  # Pattern 2: Trailing variant index on Family ("Anitype Journal 1" / "Font-2").
  m_fam = re.match(r"^(.+?)[-_ ]+(\d{1,2})$", fam)
  if m_fam:
    base = m_fam.group(1).strip()
    idx = int(m_fam.group(2))
    if 1 <= idx <= 24 and base:
      return _normalize_pack_key(base), base, idx

  # Pattern 3: Identical family, index encoded on full_name/postscript/filename.
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


def parse_font_bytes(data: bytes, fallback_name: str = "") -> dict[str, Any]:
  """Parses OpenType/TrueType name metadata and Spanish cmap glyph coverage."""
  sfnt_offset = _sfnt_offset(data)
  tables = _extract_table_directory(data, sfnt_offset)
  if "name" not in tables and "cmap" not in tables:
    raise ValueError("La fuente no contiene tablas 'name' o 'cmap' válidas.")

  names: dict[int, str] = {}
  if "name" in tables:
    n_off, n_len = tables["name"]
    names = _parse_name_table(data, n_off, n_len)

  fallback_stem = Path(fallback_name).stem or "CustomFont"
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
  """Groups numbered font variants into animated packs and static fonts."""
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

    # Non-pack group: group static weights by family name if multiple files share family.
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
              variant_count=1,
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


def _safe_font_filename(raw_name: str) -> str:
  base = Path(raw_name).name
  cleaned = re.sub(r"[^A-Za-z0-9._\-]", "_", base).lstrip(".")
  return cleaned or "custom_font.otf"


def _make_font_id(path: Path, family: str, subfamily: str) -> str:
  digest = hashlib.sha1(
      f"{path.name}|{family}|{subfamily}|{path}".encode("utf-8")
  ).hexdigest()
  return digest[:12]


class FontManager:
  """Manages custom fonts, ZIP font packs, macOS FontBook sync, and serving."""

  def __init__(
      self,
      app_dir: Path | None = None,
      macos_fonts_dir: Path | None = None,
  ) -> None:
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
    self._cache: dict[str, tuple[int, int, FontFaceInfo]] = {}
    self._faces_by_id: dict[str, FontFaceInfo] = {}

  def _load_face(self, path: Path, source: str) -> FontFaceInfo | None:
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
    """Scans app_fonts_dir and macOS user Fonts, preferring uploaded copies."""
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
    """Returns built-in fonts, custom uploaded/macOS faces, and grouped packs."""
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
    """Resolves a safe font file path and MIME type for @font-face serving."""
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
    """Installs a .otf/.ttf/.ttc file or .zip font pack (with macOS sync)."""
    if not raw_bytes:
      raise ValueError("Archivo de fuente vacío.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
      raise ValueError("El archivo supera el límite de 64 MB.")

    lower_name = filename.lower()
    extracted: list[tuple[str, bytes]] = []

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
            extracted.append((_safe_font_filename(base), font_data))
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

    installed_faces: list[FontFaceInfo] = []
    copied_to_macos = False
    for safe_name, font_bytes in extracted:
      # Validate OpenType/TrueType header before writing to disk.
      parse_font_bytes(font_bytes, fallback_name=safe_name)
      app_dest = self.app_fonts_dir / safe_name
      app_dest.write_bytes(font_bytes)
      if install_to_macos and self.macos_fonts_dir is not None:
        self.macos_fonts_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(app_dest, self.macos_fonts_dir / safe_name)
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
        "macos_fonts_dir": catalog["macos_fonts_dir"],
        "resolve_running": hardware_imac2017.is_resolve_running(),
        **catalog,
    }
