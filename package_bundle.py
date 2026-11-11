"""Packages AutoSubs ES source, resolve scripts, templates, and docs to ZIP."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import sys
from typing import Any
import zipfile

ARCHIVE_PREFIX = "AutoSubs_ES"
_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)

EXECUTABLE_REL_PATHS = frozenset({
    "install_imac.sh",
    "Iniciar AutoSubs ES.command",
})

REQUIRED_BUNDLE_ENTRIES = (
    f"{ARCHIVE_PREFIX}/.gitignore",
    f"{ARCHIVE_PREFIX}/BUILD",
    f"{ARCHIVE_PREFIX}/LICENSE",
    f"{ARCHIVE_PREFIX}/README.md",
    f"{ARCHIVE_PREFIX}/README.en.md",
    f"{ARCHIVE_PREFIX}/autosubs_server.py",
    f"{ARCHIVE_PREFIX}/autosubs_es_test.py",
    f"{ARCHIVE_PREFIX}/exporters.py",
    f"{ARCHIVE_PREFIX}/font_manager.py",
    f"{ARCHIVE_PREFIX}/hardware_imac2017.py",
    f"{ARCHIVE_PREFIX}/machine_settings.py",
    f"{ARCHIVE_PREFIX}/package_bundle.py",
    f"{ARCHIVE_PREFIX}/pyproject.toml",
    f"{ARCHIVE_PREFIX}/resolve_bridge.py",
    f"{ARCHIVE_PREFIX}/spanish_nlp.py",
    f"{ARCHIVE_PREFIX}/install_imac.sh",
    f"{ARCHIVE_PREFIX}/Iniciar AutoSubs ES.command",
    f"{ARCHIVE_PREFIX}/docs/design-doc.md",
    f"{ARCHIVE_PREFIX}/docs/user-guide.md",
    f"{ARCHIVE_PREFIX}/docs/imac-2017-architecture.md",
    f"{ARCHIVE_PREFIX}/docs/spanish-subtitling-reference.md",
    f"{ARCHIVE_PREFIX}/docs/troubleshooting.md",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-overview.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-mode-switch.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-status-button.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-machine-panel.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step1-local.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step1-resolve.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step2-whisper.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step3-spanish.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-monitor-waveform.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-cue-editor.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-style-colors.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-fonts-animation.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-anitype-monitor.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step5-resolve.png",
    f"{ARCHIVE_PREFIX}/docs/images/autosubs-es-step6-export.png",
    f"{ARCHIVE_PREFIX}/lua/1_AutoSubs_ES_Exportar_Audio.lua",
    f"{ARCHIVE_PREFIX}/lua/2_AutoSubs_ES_Insertar_Subtitulos.lua",
    f"{ARCHIVE_PREFIX}/lua/3_AutoSubs_ES_Ir_A_Tiempo.lua",
    f"{ARCHIVE_PREFIX}/templates/AutoSubs_ES_Cine_Netflix.setting",
    f"{ARCHIVE_PREFIX}/templates/AutoSubs_ES_Amarillo_Clasico.setting",
    f"{ARCHIVE_PREFIX}/templates/AutoSubs_ES_Reels_Impacto.setting",
    f"{ARCHIVE_PREFIX}/templates/AutoSubs_ES_Caja_Editorial.setting",
    f"{ARCHIVE_PREFIX}/templates/AutoSubs_ES_Anitype_Animado.setting",
    f"{ARCHIVE_PREFIX}/ui/index.html",
    f"{ARCHIVE_PREFIX}/ui/styles.css",
    f"{ARCHIVE_PREFIX}/ui/app.js",
    f"{ARCHIVE_PREFIX}/assets/AppIcon.icns",
    f"{ARCHIVE_PREFIX}/assets/icon.png",
    f"{ARCHIVE_PREFIX}/assets/icon.svg",
)


def find_project_root() -> Path:
  """Locates the autosubs_es directory across macOS and Blaze runfiles."""
  candidates: list[Path] = []
  for env_var in ("TEST_SRCDIR", "RUNFILES_DIR"):
    val = os.environ.get(env_var)
    if val:
      candidates.append(
          Path(val)
          / "google3"
          / "experimental"
          / "users"
          / "joseluiszapata"
          / "autosubs_es"
      )
  if sys.argv and sys.argv[0]:
    candidates.append(
        Path(f"{sys.argv[0]}.runfiles")
        / "google3"
        / "experimental"
        / "users"
        / "joseluiszapata"
        / "autosubs_es"
    )
  candidates.extend([
      Path(__file__).parent,
      Path(__file__).resolve().parent,
      Path.cwd() / "experimental" / "users" / "joseluiszapata" / "autosubs_es",
      Path.cwd(),
  ])

  for cand in candidates:
    if (
        (cand / "README.md").is_file()
        and (cand / "autosubs_server.py").is_file()
        and (cand / "docs" / "user-guide.md").is_file()
        and (cand / "ui" / "index.html").is_file()
    ):
      return cand
  return Path(__file__).parent


_ALLOWED_EXTENSIONS = (
    ".py",
    ".md",
    ".toml",
    ".lua",
    ".setting",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".command",
    ".png",
    ".icns",
    ".svg",
)


def _should_skip_entry(rel_parts: tuple[str, ...], filename: str) -> bool:
  if any(
      part in ("__pycache__", ".git", ".citc", ".github", "site")
      or part.endswith(".runfiles")
      for part in rel_parts
  ):
    return True
  if filename in (".DS_Store", "__init__.py", "AGENTS.md"):
    return True
  if filename in ("BUILD", ".gitignore", "LICENSE"):
    return False
  return not filename.endswith(_ALLOWED_EXTENSIONS)


def collect_bundle_files(source_root: Path | None = None) -> dict[str, bytes]:
  """Collects sorted relative paths and file contents for MTTN_AutoSubs_Tool.zip."""
  root = source_root or find_project_root()
  collected: dict[str, bytes] = {}
  for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
    dirnames[:] = sorted(
        d
        for d in dirnames
        if d not in ("__pycache__", ".git", ".citc", ".github", "site")
    )
    current = Path(dirpath)
    for fname in sorted(filenames):
      file_path = current / fname
      rel_path = file_path.relative_to(root)
      if _should_skip_entry(rel_path.parts, fname):
        continue
      if file_path.is_file():
        collected[rel_path.as_posix()] = file_path.read_bytes()
  return dict(sorted(collected.items()))


def build_zip_bytes(source_root: Path | None = None) -> bytes:
  """Packages AutoSubs ES files into a deterministic UNIX-mode ZIP buffer."""
  files = collect_bundle_files(source_root=source_root)
  buf = io.BytesIO()
  with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for rel_posix, raw_bytes in files.items():
      arcname = f"{ARCHIVE_PREFIX}/{rel_posix}"
      info = zipfile.ZipInfo(filename=arcname, date_time=_ZIP_TIMESTAMP)
      info.compress_type = zipfile.ZIP_DEFLATED
      info.create_system = 3  # UNIX permissions preserved upon extraction.
      mode = 0o100755 if rel_posix in EXECUTABLE_REL_PATHS else 0o100644
      info.external_attr = mode << 16
      zf.writestr(info, raw_bytes)
  return buf.getvalue()


def verify_zip_bytes(zip_bytes: bytes) -> dict[str, Any]:
  """Validates CRC integrity, required files, and macOS executable modes."""
  with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
    corrupt = zf.testzip()
    if corrupt is not None:
      raise ValueError(f"Archivo ZIP corrupto en entrada: {corrupt}")
    names = sorted(zf.namelist())
    name_set = set(names)
    missing = [req for req in REQUIRED_BUNDLE_ENTRIES if req not in name_set]
    if missing:
      raise ValueError(f"Faltan archivos obligatorios en ZIP: {missing}")
    if any(
        "__pycache__" in n or n.endswith(("__init__.py", ".pyc")) for n in names
    ):
      raise ValueError(
          "El archivo ZIP contiene residuos prohibidos (__pycache__ o"
          " __init__.py)"
      )
    for rel_exec in EXECUTABLE_REL_PATHS:
      arc_exec = f"{ARCHIVE_PREFIX}/{rel_exec}"
      perm = (zf.getinfo(arc_exec).external_attr >> 16) & 0o777
      if (perm & 0o111) != 0o111:
        raise ValueError(
            f"Permiso de ejecución faltante en {arc_exec}: {oct(perm)}"
        )
  return {
      "file_count": len(names),
      "size_bytes": len(zip_bytes),
      "sha256": hashlib.sha256(zip_bytes).hexdigest(),
      "entries": names,
  }


def write_bundle_zip(
    output_path: str | Path, source_root: Path | None = None
) -> dict[str, Any]:
  """Builds, validates, and writes AutoSubs_ES.zip to output_path."""
  dest = Path(output_path).expanduser()
  dest.parent.mkdir(parents=True, exist_ok=True)
  zip_bytes = build_zip_bytes(source_root=source_root)
  meta = verify_zip_bytes(zip_bytes)
  dest.write_bytes(zip_bytes)
  meta["output_path"] = str(dest)
  return meta


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description=(
          "Empaqueta MTTN AutoSubs Tool y su documentación en un archivo .zip"
      )
  )
  parser.add_argument(
      "--output",
      default="MTTN_AutoSubs_Tool.zip",
      help=(
          "Ruta de salida para el archivo .zip (por defecto"
          " MTTN_AutoSubs_Tool.zip)"
      ),
  )
  args = parser.parse_args(argv)
  meta = write_bundle_zip(args.output)
  print(
      f"[MTTN AutoSubs Tool] ZIP creado: {meta['output_path']} "
      f"({meta['file_count']} archivos, {meta['size_bytes']} bytes, "
      f"sha256={meta['sha256']})"
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
