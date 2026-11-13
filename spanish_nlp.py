"""Spanish NLP engine: whisper.cpp -ojf parsing, RAE wrapping, and repair.

This module turns raw whisper.cpp output into broadcast-ready Spanish
subtitle cues. It has no I/O and no third-party dependencies, so it is safe
to import from the HTTP server (`autosubs_server.py`), the Resolve bridge,
and the unit tests (`autosubs_es_test.py`).

Pipeline (the order matters; each stage assumes the previous one ran):

  1. `parse_whisper_ojf(ojf_data)`: reads `whisper-cli --output-json-full`
     (`-ojf`) JSON, drops control/timestamp tokens and hallucinated segments,
     merges BPE sub-word tokens into whole words, and recovers accented words
     that BPE split into U+FFFD bytes. Returns a flat `list[WordTiming]`.
  2. `clean_spanish_words(words, config)`: optional muletilla removal,
     domain phonetic fixes (e.g. "mipel" -> "meeple"), glossary casing,
     opening ¿/¡ repair, and uppercase. Returns new `WordTiming` copies.
  3. `segment_and_wrap_spanish(words, config)`: groups words into cues
     (pauses, speaker changes, CPL/CPS/duration limits, clause ends), wraps
     each cue into balanced lines with RAE/Netflix penalties, then polishes
     durations so cues never overlap. NOTE: it calls `clean_spanish_words`
     itself, so callers normally go straight from step 1 to step 3.

Data model and units:
  * All times are integer milliseconds (ms) relative to the start of the
    transcribed audio (not timeline timecode; see `exporters.py` for TC).
  * `WordTiming`: one displayed token (word plus attached punctuation).
  * `SubtitleCue`: one on-screen subtitle with wrapped `lines` and the
    `words` that compose it (used for karaoke and the cue editor).
  * Invariant enforced by `align_words_to_lines` (used whenever a cue is
    rebuilt from a dict, e.g. after UI edits): a cue's words are sorted,
    non-overlapping (`words[i].start_ms >= words[i-1].end_ms`), and lie
    within `[cue.start_ms, cue.end_ms]`. `parse_whisper_ojf` only
    guarantees non-decreasing word starts.

Spanish word lists, prompts, and RAE rule data are intentionally kept in
Spanish; do not translate them, they are matched against transcribed text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import difflib
import re
from typing import Any


# --- Data model ---


@dataclass
class WordTiming:
  """Single word with millisecond timings and Whisper token confidence.

  Attributes:
    text: Displayed token, including attached punctuation (e.g. "¿Vale?").
    start_ms: Start time in ms from the beginning of the audio.
    end_ms: End time in ms; expected to be >= `start_ms`.
    prob: Mean Whisper token probability in [0, 1]. Synthetic values are
      used for interpolated words (0.9 for UTF-8 recovery, 0.5 for words
      inserted by the cue editor).
    speaker: Speaker label; cues are split when it changes.
  """

  text: str
  start_ms: int
  end_ms: int
  prob: float = 1.0
  speaker: str = "Hablante 1"

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-serializable dict with the same keys as the fields."""
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any]) -> WordTiming:
    """Builds a WordTiming from a (possibly partial) JSON dict.

    Args:
      raw: Dict with any of the field names as keys; missing keys fall back
        to the dataclass defaults (times default to 0).

    Returns:
      A new WordTiming with values coerced to the declared types.
    """
    return cls(
        text=str(raw.get("text", "")),
        start_ms=int(raw.get("start_ms", 0)),
        end_ms=int(raw.get("end_ms", 0)),
        prob=float(raw.get("prob", 1.0)),
        speaker=str(raw.get("speaker", "Hablante 1")),
    )


@dataclass
class SubtitleCue:
  """Formatted subtitle cue ready for preview, SRT/ASS/FCPXML, and Resolve.

  Attributes:
    index: 1-based cue number (SRT block number).
    start_ms: Cue start in ms from the beginning of the audio.
    end_ms: Cue end in ms; producers keep it >= `start_ms + 40`.
    lines: Wrapped display lines (at most `max_lines`), without newlines.
    words: Per-token timings, one per whitespace token of `lines` in order.
      Exporters rely on this 1:1 mapping for ASS karaoke (`\\kf`).
    speaker: Speaker label of the first word.
    cps: Reading speed in characters per second (see
      `refresh_cue_metrics`).
    warnings: Human-readable (Spanish) readability warnings for the UI.
  """

  index: int
  start_ms: int
  end_ms: int
  lines: list[str]
  words: list[WordTiming] = field(default_factory=list)
  speaker: str = "Hablante 1"
  cps: float = 0.0
  warnings: list[str] = field(default_factory=list)

  @property
  def text(self) -> str:
    """Cue text with lines joined by "\\n" (the SRT/VTT body)."""
    return "\n".join(self.lines)

  def to_dict(self) -> dict[str, Any]:
    """Returns the JSON shape used by the HTTP API and the JSON export.

    Includes the derived `text` field and rounds `cps` to one decimal.
    """
    return {
        "index": self.index,
        "start_ms": self.start_ms,
        "end_ms": self.end_ms,
        "text": self.text,
        "lines": list(self.lines),
        "words": [w.to_dict() for w in self.words],
        "speaker": self.speaker,
        "cps": round(self.cps, 1),
        "warnings": list(self.warnings),
    }

  @classmethod
  def from_dict(cls, raw: dict[str, Any]) -> SubtitleCue:
    """Rebuilds a cue from API/UI JSON, re-aligning words and metrics.

    Called after the user edits text or drags/retimes a cue, so `words` may
    no longer match `lines`. Words are re-aligned with
    `align_words_to_lines` and CPS/warnings are recomputed with the default
    `SpanishFormatConfig` limits.

    Args:
      raw: Dict as produced by `to_dict`. `lines` wins over `text`; if
        `lines` is empty, `text` is split on newlines. Minimum duration is
        clamped to 40 ms; a missing `end_ms` defaults to start + 500 ms.

    Returns:
      A new SubtitleCue satisfying the word alignment invariant.
    """
    lines = raw.get("lines")
    if not lines:
      text_val = str(raw.get("text", "")).strip()
      lines = [ln.strip() for ln in text_val.splitlines() if ln.strip()]
    start_ms = int(raw.get("start_ms", 0))
    end_ms = max(start_ms + 40, int(raw.get("end_ms", start_ms + 500)))
    words = align_words_to_lines(
        [WordTiming.from_dict(w) for w in raw.get("words", [])],
        lines,
        start_ms,
        end_ms,
    )
    cue = cls(
        index=int(raw.get("index", 1)),
        start_ms=start_ms,
        end_ms=end_ms,
        lines=list(lines),
        words=words,
        speaker=str(raw.get("speaker", "Hablante 1")),
        cps=float(raw.get("cps", 0.0)),
        warnings=list(raw.get("warnings", [])),
    )
    return refresh_cue_metrics(cue)


# --- Word/line alignment (cue editor support) ---


def _norm_token(token: str) -> str:
  """Lowercased token without edge punctuation, used for diff matching."""
  return _strip_edge_punct(token)[1].lower()


def align_words_to_lines(
    words: list[WordTiming], lines: list[str], start_ms: int, end_ms: int
) -> list[WordTiming]:
  """Keeps one WordTiming per displayed token after edits, retiming, or drags.

  Words still present after a text edit keep their Whisper timings (matched via
  difflib); new or rewritten words share the time between their neighbours in
  proportion to their length. Words outside [start_ms, end_ms] are rescaled.

  Args:
    words: Previous word timings of the cue (may be stale or empty).
    lines: Current display lines; tokens are their whitespace-split words.
    start_ms: Cue start in ms.
    end_ms: Cue end in ms (clamped to at least `start_ms + 40`).

  Returns:
    Exactly one WordTiming per token, in order, sorted and non-overlapping,
    all within [start_ms, end_ms]. Inserted words get `prob=0.5`. Empty if
    `lines` has no tokens.
  """
  tokens = [t for ln in lines for t in ln.split()]
  if not tokens:
    return []
  start_ms = int(start_ms)
  end_ms = max(start_ms + 40, int(end_ms))
  src = [w for w in words if w.text.strip()]
  speaker = src[0].speaker if src else "Hablante 1"

  # If the cue was moved or trimmed so some words fall outside it, linearly
  # remap the old word span onto the cue span (keeps relative word rhythm).
  if src:
    lo = min(w.start_ms for w in src)
    hi = max(w.end_ms for w in src)
    if lo < start_ms or hi > end_ms:
      scale = (end_ms - start_ms) / max(1, hi - lo)
      src = [
          WordTiming(
              w.text,
              start_ms + round((w.start_ms - lo) * scale),
              start_ms + round((w.end_ms - lo) * scale),
              w.prob,
              w.speaker,
          )
          for w in src
      ]

  # Fast path: same token count (typo fix, casing, punctuation, retime).
  # Reuse timings positionally and only clamp to the invariant.
  if len(src) == len(tokens):
    result = [
        WordTiming(tok, w.start_ms, w.end_ms, w.prob, w.speaker)
        for tok, w in zip(tokens, src)
    ]
    for i, w in enumerate(result):
      floor_ms = result[i - 1].end_ms if i > 0 else start_ms
      w.start_ms = max(floor_ms, min(w.start_ms, end_ms))
      w.end_ms = max(w.start_ms, min(w.end_ms, end_ms))
    return result

  # Token count changed: diff normalized tokens and keep the timings of
  # unchanged words; `out[j]` stays None for inserted/replaced tokens.
  out: list[WordTiming | None] = [None] * len(tokens)
  matcher = difflib.SequenceMatcher(
      None,
      [_norm_token(w.text) for w in src],
      [_norm_token(t) for t in tokens],
      autojunk=False,
  )
  for tag, i1, i2, j1, _ in matcher.get_opcodes():
    if tag == "equal":
      for k in range(i2 - i1):
        w = src[i1 + k]
        out[j1 + k] = WordTiming(
            tokens[j1 + k], w.start_ms, w.end_ms, w.prob, w.speaker
        )

  # Fill each run of unmatched tokens [j, k) inside the gap between its
  # matched neighbours [lo, hi], weighting by token length (+1 so 1-char
  # words still get time).
  j = 0
  while j < len(tokens):
    if out[j] is not None:
      j += 1
      continue
    k = j
    while k < len(tokens) and out[k] is None:
      k += 1
    left = out[j - 1] if j > 0 else None
    right = out[k] if k < len(tokens) else None
    lo = left.end_ms if left else start_ms
    hi = right.start_ms if right else end_ms
    need = 60 * (k - j)  # ~60 ms minimum per inserted word.
    if hi - lo < need:
      # Borrow half of each neighbour so inserted words stay visible,
      # without ever expanding short neighbours or overlapping right.
      if left:
        left.end_ms = max(
            left.start_ms,
            min(left.end_ms, max(left.start_ms + 20, (left.start_ms + left.end_ms) // 2)),
        )
        lo = left.end_ms
      if right:
        right.start_ms = min(
            right.end_ms,
            max(right.start_ms, min(right.end_ms - 20, (right.start_ms + right.end_ms) // 2)),
        )
        hi = right.start_ms
      hi = max(lo, hi)
    weights = [len(t) + 1 for t in tokens[j:k]]
    total = float(sum(weights))
    cursor = float(lo)
    for idx in range(j, k):
      seg_start = round(cursor)
      cursor += (hi - lo) * weights[idx - j] / total
      out[idx] = WordTiming(tokens[idx], seg_start, round(cursor), 0.5, speaker)
    j = k

  # Final pass: enforce the sorted, non-overlapping, in-cue invariant.
  result = [w for w in out if w is not None]
  for i, w in enumerate(result):
    floor_ms = result[i - 1].end_ms if i > 0 else start_ms
    w.start_ms = max(floor_ms, min(w.start_ms, end_ms))
    w.end_ms = max(w.start_ms, min(w.end_ms, end_ms))
  return result


# --- Formatting configuration and presets ---


@dataclass
class SpanishFormatConfig:
  """Configurable formatting and linguistic rules for Spanish subtitles.

  Defaults match the "youtube_16_9" preset (Netflix Spanish style guide).

  Attributes:
    preset_id: Key of `FORMAT_PRESETS` used as the base for `from_dict`.
    dialect: Key of `SPANISH_DIALECT_PROMPTS` for the Whisper prompt.
    domain: Key of `DOMAIN_PROFILES` (phonetic fixes and glossary).
    max_cpl: Maximum characters per line.
    max_lines: Maximum lines per cue (1..3).
    max_words: Maximum words per cue before forcing a split.
    target_cps: Desired reading speed in characters/second. Stored and
      round-tripped only; nothing in the engine reads it today.
    max_cps: Reading speed above which a cue gets a warning.
    min_duration_ms: Cues shorter than this are extended when possible.
    max_duration_ms: A cue is split once it would exceed this length.
    pause_split_ms: Silence between words that always starts a new cue.
    gap_bridge_ms: Gaps between cues up to this size are closed to avoid
      subtitle flicker.
    fix_inverted_punctuation: Insert missing opening ¿/¡.
    remove_muletillas: Drop hesitations ("eh", "mmm", isolated "este").
    uppercase: Uppercase every word.
    custom_glossary: User terms whose exact casing is enforced.
  """

  preset_id: str = "youtube_16_9"
  dialect: str = "neutro"
  domain: str = "juegos_mesa"
  max_cpl: int = 42
  max_lines: int = 2
  max_words: int = 14
  target_cps: float = 17.0
  max_cps: float = 20.0
  min_duration_ms: int = 830
  max_duration_ms: int = 6500
  pause_split_ms: int = 650
  gap_bridge_ms: int = 120
  fix_inverted_punctuation: bool = True
  remove_muletillas: bool = False
  uppercase: bool = False
  custom_glossary: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-serializable dict with the same keys as the fields."""
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> SpanishFormatConfig:
    """Builds a validated config from UI/API JSON.

    Resolution order: preset values from `FORMAT_PRESETS[preset_id]`
    (unknown ids fall back to "youtube_16_9"), then any non-None override in
    `raw`. Numeric limits are clamped to safe ranges. If no explicit domain
    is given, the "tech_spanglish" dialect implies the "tech_video" domain.

    Args:
      raw: Partial config dict, or None/empty for all defaults.
        `custom_glossary` may be a list or a string separated by commas,
        semicolons, or newlines.

    Returns:
      A new SpanishFormatConfig.
    """
    if not raw:
      return cls()
    preset_id = str(raw.get("preset_id", "youtube_16_9"))
    base = dict(FORMAT_PRESETS.get(preset_id, FORMAT_PRESETS["youtube_16_9"]))
    for key in (
        "dialect",
        "domain",
        "max_cpl",
        "max_lines",
        "max_words",
        "target_cps",
        "max_cps",
        "min_duration_ms",
        "max_duration_ms",
        "pause_split_ms",
        "gap_bridge_ms",
        "fix_inverted_punctuation",
        "remove_muletillas",
        "uppercase",
        "custom_glossary",
    ):
      if key in raw and raw[key] is not None:
        base[key] = raw[key]

    dialect_val = str(base.get("dialect", "neutro"))
    if "domain" in raw and raw["domain"] is not None:
      domain_val = str(raw["domain"])
    elif dialect_val == "tech_spanglish":
      domain_val = "tech_video"
    else:
      domain_val = str(base.get("domain", "juegos_mesa"))
    if domain_val not in DOMAIN_PROFILES:
      domain_val = "juegos_mesa"

    glossary_raw = base.get("custom_glossary", [])
    if isinstance(glossary_raw, str):
      glossary = [
          t.strip() for t in re.split(r"[,;\n]+", glossary_raw) if t.strip()
      ]
    else:
      glossary = [str(t).strip() for t in glossary_raw if str(t).strip()]

    return cls(
        preset_id=preset_id,
        dialect=dialect_val,
        domain=domain_val,
        max_cpl=max(8, min(int(base.get("max_cpl", 42)), 80)),
        max_lines=max(1, min(int(base.get("max_lines", 2)), 3)),
        max_words=max(1, min(int(base.get("max_words", 14)), 30)),
        target_cps=float(base.get("target_cps", 17.0)),
        max_cps=float(base.get("max_cps", 20.0)),
        min_duration_ms=max(150, int(base.get("min_duration_ms", 830))),
        max_duration_ms=max(1000, int(base.get("max_duration_ms", 6500))),
        pause_split_ms=max(150, int(base.get("pause_split_ms", 650))),
        gap_bridge_ms=max(0, int(base.get("gap_bridge_ms", 120))),
        fix_inverted_punctuation=bool(
            base.get("fix_inverted_punctuation", True)
        ),
        remove_muletillas=bool(base.get("remove_muletillas", False)),
        uppercase=bool(base.get("uppercase", False)),
        custom_glossary=glossary,
    )


# Segmentation presets selectable in the UI. `label` is user-facing Spanish;
# the remaining keys mirror `SpanishFormatConfig` fields (ms / chars / CPS).
FORMAT_PRESETS: dict[str, dict[str, Any]] = {
    "youtube_16_9": {
        "label": "YouTube 16:9 (Netflix / RAE)",
        "max_cpl": 42,
        "max_lines": 2,
        "max_words": 14,
        "target_cps": 17.0,
        "max_cps": 20.0,
        "min_duration_ms": 830,
        "max_duration_ms": 6500,
        "pause_split_ms": 650,
        "gap_bridge_ms": 120,
    },
    "reels_9_16": {
        "label": "Reels / TikTok / Shorts (9:16)",
        "max_cpl": 20,
        "max_lines": 2,
        "max_words": 4,
        "target_cps": 18.0,
        "max_cps": 22.0,
        "min_duration_ms": 480,
        "max_duration_ms": 3500,
        "pause_split_ms": 450,
        "gap_bridge_ms": 100,
    },
    "karaoke_impacto": {
        "label": "Impacto Palabra a Palabra (1-2 palabras)",
        "max_cpl": 15,
        "max_lines": 1,
        "max_words": 2,
        "target_cps": 20.0,
        "max_cps": 26.0,
        "min_duration_ms": 220,
        "max_duration_ms": 2200,
        "pause_split_ms": 320,
        "gap_bridge_ms": 60,
    },
    "cine_es": {
        "label": "Cine / Documental (Lectura reposada)",
        "max_cpl": 40,
        "max_lines": 2,
        "max_words": 12,
        "target_cps": 15.0,
        "max_cps": 18.0,
        "min_duration_ms": 1000,
        "max_duration_ms": 6000,
        "pause_split_ms": 700,
        "gap_bridge_ms": 140,
    },
}


# --- Whisper prompts and domain vocabularies (Spanish data, do not edit) ---

# Dialect-specific Whisper `--prompt` prefixes. Whisper imitates the style of
# the prompt, so these sentences prime Spanish punctuation (¿?, ¡!), accents,
# and regional vocabulary.
SPANISH_DIALECT_PROMPTS: dict[str, str] = {
    "neutro": (
        "Transcripción fiel en español con ortografía, tildes y puntuación"
        " española (¿preguntas?, ¡exclamaciones!)."
    ),
    "es_es": (
        "Español de España peninsular: vosotros, tenéis, podéis, vale, tío."
        " Usa puntuación española (¿...?, ¡...!) y tildes exactas."
    ),
    "es_mx": (
        "Español de México y Latinoamérica: ustedes, qué padre, ahorita,"
        " platicar. Usa puntuación española (¿...?, ¡...!) y tildes exactas."
    ),
    "es_ar": (
        "Español rioplatense (Argentina y Uruguay): vos tenés, mirá, che,"
        " laburo. Usa puntuación española (¿...?, ¡...!) y tildes exactas."
    ),
    "tech_spanglish": (
        "Edición de vídeo y tecnología en español: DaVinci Resolve, Fusion,"
        " timeline, render, LUT, proxy, keyframe, Mac, iMac, plugin, SRT."
    ),
}


# Board-game anglicisms appended to the Whisper prompt so the model spells
# them in English instead of phonetically.
BOARDGAME_PROMPT_TERMS: tuple[str, ...] = (
    "meeple",
    "meeples",
    "worker placement",
    "deckbuilding",
    "draft",
    "setup",
    "playmat",
    "sleeves",
    "tokens",
    "standees",
    "insert",
    "downtime",
    "eurogame",
    "ameritrash",
    "dungeon crawler",
    "roll and write",
    "trick-taking",
    "engine building",
    "area control",
    "set collection",
    "push your luck",
    "bag building",
    "party game",
    "wargame",
    "filler",
    "lore",
    "combo",
    "automa",
    "rulebook",
    "análisis parálisis",
    "entreturno",
    "rejugabilidad",
    "Kickstarter",
    "Gamefound",
    "stretch goals",
    "pledge",
    "all-in",
    "retail",
    "deluxe",
    "BGG",
    "BoardGameGeek",
)

# Proper nouns whose exact casing is restored after transcription by
# `_apply_glossary` (merged with the user's `custom_glossary`).
BOARDGAME_GLOSSARY_TERMS: tuple[str, ...] = (
    "BoardGameGeek",
    "BGG",
    "Kickstarter",
    "Gamefound",
    "Spiel des Jahres",
    "Kennerspiel des Jahres",
    "Essen Spiel",
    "Gen Con",
    "Gloomhaven",
    "Frosthaven",
    "Wingspan",
    "Terraforming Mars",
    "Ark Nova",
    "Brass: Birmingham",
    "Dune: Imperium",
    "Spirit Island",
    "Carcassonne",
    "Catan",
    "Ticket to Ride",
    "Aventureros al Tren",
    "7 Wonders",
    "Scythe",
    "Root",
    "Everdell",
    "Azul",
    "Agricola",
    "Castles of Burgundy",
    "Devir",
    "Asmodee",
    "Maldito Games",
    "Zacatrus",
    "Tranjis Games",
    "Salt & Pepper Games",
    "Stonemaier Games",
    "Awaken Realms",
    "Fantasy Flight",
    "CMON",
)

# Phonetic misspelling -> canonical term. Keys are lowercase and may span
# several words ("worker pleisment"); they are matched by `_apply_phrase_map`
# ignoring case and edge punctuation. Values keep the desired casing.
BOARDGAME_PHONETIC_FIXES: dict[str, str] = {
    "mipel": "meeple",
    "mípel": "meeple",
    "mipels": "meeples",
    "mípels": "meeples",
    "miples": "meeples",
    "pleimat": "playmat",
    "pley mat": "playmat",
    "play mat": "playmat",
    "setap": "setup",
    "sétap": "setup",
    "setop": "setup",
    "eslivs": "sleeves",
    "slivs": "sleeves",
    "estandis": "standees",
    "standis": "standees",
    "rulbuk": "rulebook",
    "rule buk": "rulebook",
    "worker pleisment": "worker placement",
    "uorker pleisment": "worker placement",
    "güorker pleisment": "worker placement",
    "worquer pleisment": "worker placement",
    "dec bilding": "deckbuilding",
    "dek bilding": "deckbuilding",
    "deck bilding": "deckbuilding",
    "deck building": "deckbuilding",
    "decbilding": "deckbuilding",
    "bag bilding": "bag building",
    "enyin bilding": "engine building",
    "engine bilding": "engine building",
    "tabló bilding": "tableau building",
    "eurogueim": "eurogame",
    "eurogueims": "eurogames",
    "euro game": "eurogame",
    "euro games": "eurogames",
    "ameritras": "ameritrash",
    "ameri trash": "ameritrash",
    "danyon croler": "dungeon crawler",
    "danjon croler": "dungeon crawler",
    "dányon cróler": "dungeon crawler",
    "dungeon croler": "dungeon crawler",
    "rol and rait": "roll and write",
    "rol an rait": "roll and write",
    "roll an write": "roll and write",
    "flip and rait": "flip and write",
    "flip an rait": "flip and write",
    "push yor lak": "push your luck",
    "push your lak": "push your luck",
    "trik teiking": "trick-taking",
    "trick teiking": "trick-taking",
    "set colection": "set collection",
    "tail pleisment": "tile placement",
    "parti gueim": "party game",
    "guargueim": "wargame",
    "wargueim": "wargame",
    "print and pley": "print and play",
    "print an play": "print and play",
    "dauntaim": "downtime",
    "daun taim": "downtime",
    "gueimplei": "gameplay",
    "geimplei": "gameplay",
    "analisis paralisis": "análisis parálisis",
    "anboxin": "unboxing",
    "unboxin": "unboxing",
    "jaip": "hype",
    "jaipe": "hype",
    "espóiler": "spoiler",
    "espoiler": "spoiler",
    "kikstarter": "Kickstarter",
    "quicstarter": "Kickstarter",
    "kick starter": "Kickstarter",
    "gueimfaund": "Gamefound",
    "geimfaund": "Gamefound",
    "game found": "Gamefound",
    "estrech gols": "stretch goals",
    "stretch gols": "stretch goals",
    "estrech gol": "stretch goal",
    "erli berd": "early bird",
    "leit pledch": "late pledge",
    "late plech": "late pledge",
    "pledch": "pledge",
    "ol in": "all-in",
    "riteil": "retail",
    "delux": "deluxe",
    "be ge ge": "BGG",
    "bord gueim guik": "BoardGameGeek",
    "board game geek": "BoardGameGeek",
    "glumjeiven": "Gloomhaven",
    "glumhaven": "Gloomhaven",
    "frostjeiven": "Frosthaven",
    "uingspan": "Wingspan",
    "güingspan": "Wingspan",
    "arc nova": "Ark Nova",
    "terraformin mars": "Terraforming Mars",
    "carcasón": "Carcassonne",
    "espirit ailand": "Spirit Island",
    "espil des yares": "Spiel des Jahres",
    "seven wonders": "7 Wonders",
}

# Content domains selectable in the UI. Each profile bundles:
#   label: user-facing Spanish name.
#   prompt_terms: vocabulary appended to the Whisper prompt.
#   glossary_terms: casing enforced after transcription.
#   phonetic_fixes: phrase map applied before the glossary.
DOMAIN_PROFILES: dict[str, dict[str, Any]] = {
    "juegos_mesa": {
        "label": "Juegos de Mesa y Anglicismos (BGG)",
        "prompt_terms": list(BOARDGAME_PROMPT_TERMS),
        "glossary_terms": list(BOARDGAME_GLOSSARY_TERMS),
        "phonetic_fixes": BOARDGAME_PHONETIC_FIXES,
    },
    "general": {
        "label": "General / Estándar",
        "prompt_terms": [],
        "glossary_terms": [],
        "phonetic_fixes": {},
    },
    "tech_video": {
        "label": "Edición de Vídeo y Tech",
        "prompt_terms": [
            "DaVinci Resolve",
            "Fusion",
            "Text+",
            "timeline",
            "render",
            "keyframe",
            "LUT",
            "proxy",
            "SRT",
            "FCPXML",
            "iMac",
            "macOS",
        ],
        "glossary_terms": [
            "DaVinci Resolve",
            "Text+",
            "FCPXML",
            "iMac",
            "macOS",
            "AutoSubs ES",
        ],
        "phonetic_fixes": {
            "davinchi resolve": "DaVinci Resolve",
            "da vinci resolve": "DaVinci Resolve",
        },
    },
}


# --- RAE / Netflix line-break rules (Spanish data, do not edit) ---

# Words that should NEVER stay orphaned at the end of line 1 (RAE / Netflix ES).
# Articles, prepositions, conjunctions, relatives, clitic pronouns,
# possessives, negation/intensifiers, and auxiliaries bind to the next word.
# Matched lowercase, without edge punctuation (see `_last_word_penalty`).
SPANISH_WEAK_LINE_ENDINGS: frozenset[str] = frozenset({
    "el",
    "la",
    "los",
    "las",
    "lo",
    "un",
    "una",
    "unos",
    "unas",
    "al",
    "del",
    "a",
    "ante",
    "bajo",
    "con",
    "contra",
    "de",
    "desde",
    "durante",
    "en",
    "entre",
    "hacia",
    "hasta",
    "mediante",
    "para",
    "por",
    "según",
    "sin",
    "sobre",
    "tras",
    "y",
    "e",
    "o",
    "u",
    "ni",
    "que",
    "quien",
    "cual",
    "cuyo",
    "donde",
    "cuando",
    "como",
    "pero",
    "sino",
    "aunque",
    "porque",
    "pues",
    "si",
    "me",
    "te",
    "se",
    "nos",
    "os",
    "le",
    "les",
    "mi",
    "mis",
    "tu",
    "tus",
    "su",
    "sus",
    "nuestro",
    "nuestra",
    "no",
    "muy",
    "más",
    "menos",
    "tan",
    "he",
    "has",
    "ha",
    "hemos",
    "han",
    "estoy",
    "estás",
    "está",
    "estamos",
    "están",
    "soy",
    "eres",
    "es",
    "somos",
    "son",
    "fue",
    "era",
})


# Discourse markers that may open a question/exclamation before the real
# clause ("Oye, ¿cómo...?"). `_repair_inverted_spanish_punctuation` places
# the opening ¿/¡ after them instead of before them.
SPANISH_DISCOURSE_PREFIXES: frozenset[str] = frozenset({
    "oye",
    "mira",
    "bueno",
    "vale",
    "pues",
    "claro",
    "entonces",
    "ahora",
    "verás",
    "perdón",
    "disculpa",
    "hola",
    "che",
    "pero",
    "y",
    "o",
})


# Known Whisper hallucinations on silence/music in Spanish (credits lines
# from subtitle sites, YouTube outros, bracketed sound tags). A segment that
# matches any pattern is dropped entirely by `parse_whisper_ojf`.
SPANISH_HALLUCINATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"amara\.org", re.IGNORECASE),
    re.compile(r"subt[íi]tulos\s+realizados\s+por", re.IGNORECASE),
    re.compile(r"subtitulado\s+por", re.IGNORECASE),
    re.compile(r"transcripci[óo]n\s+realizada\s+por", re.IGNORECASE),
    re.compile(r"suscr[íi]bete\s+al\s+canal", re.IGNORECASE),
    re.compile(r"gracias\s+por\s+ver\s+(?:el|este)\s+v[íi]deo", re.IGNORECASE),
    re.compile(r"dale\s+(?:a\s+)?like\s+y\s+suscr", re.IGNORECASE),
    re.compile(r"www\.mooji\.org", re.IGNORECASE),
    re.compile(r"^\[?(?:m[úu]sica|aplausos|risas|silencio)\]?$", re.IGNORECASE),
)


# whisper.cpp control tokens: [_BEG_], [_SOT_], [_EOT_]... and timestamp
# tokens [_TT_335] (these end in a digit, not "_]").
_SPECIAL_TOKEN_RE = re.compile(r"^\[_[A-Z]+_\d*\]$")
# Same tokens embedded in segment/token text (with any leading spaces).
_INLINE_SPECIAL_TOKEN_RE = re.compile(r"\s*\[_[A-Z]+_\d*\]")
# Pure hesitation sounds ("eh", "emm", "mmm", "hm", "uh", "ahh").
_MULETILLA_RE = re.compile(r"^(?:eh+|em+|mm+|hm+|uh+|ah+)$", re.IGNORECASE)
# Token ends a sentence: . ? ! … optionally followed by closing quotes or
# brackets. Used as a strong cue/line break point.
_CLAUSE_END_RE = re.compile(r"[.?!…]+[\"'»”’\)\]]*$")
# Token ends with a soft pause: , ; : or a dash (weaker break point).
_SOFT_BREAK_RE = re.compile(r"[,;:—–]+[\"'»”’\)\]]*$")


# --- Whisper prompt and hallucination filtering ---


def build_spanish_initial_prompt(
    dialect: str = "neutro",
    glossary_terms: list[str] | None = None,
    domain: str = "juegos_mesa",
) -> str:
  """Builds the whisper-cli --prompt string in Spanish with domain & glossary.

  Args:
    dialect: Key of `SPANISH_DIALECT_PROMPTS`; unknown keys use "neutro".
    glossary_terms: User terms; listed before domain terms so they survive
      truncation. Duplicates are removed case-insensitively.
    domain: Key of `DOMAIN_PROFILES` providing `prompt_terms`.

  Returns:
    "<dialect sentence> <label>: term1, term2, ...." capped at 580 chars,
    or just the dialect sentence when there are no terms.
  """
  base = SPANISH_DIALECT_PROMPTS.get(dialect, SPANISH_DIALECT_PROMPTS["neutro"])
  profile = DOMAIN_PROFILES.get(domain, {})
  domain_terms = [str(t).strip() for t in profile.get("prompt_terms", [])]

  combined: list[str] = []
  seen: set[str] = set()
  # User-supplied glossary terms always get priority before domain terms.
  for raw_term in list(glossary_terms or []) + domain_terms:
    cleaned = raw_term.strip()
    if not cleaned:
      continue
    key = cleaned.lower()
    if key in seen:
      continue
    seen.add(key)
    combined.append(cleaned)

  if not combined:
    return base

  prefix_label = (
      "Vocabulario clave"
      if domain == "general" and not domain_terms
      else "Términos y anglicismos"
  )
  prefix = f"{base} {prefix_label}: "
  # Character cap that keeps the prompt within whisper.cpp's prompt token
  # budget (whisper keeps at most ~224 prompt tokens and drops the rest).
  max_len = 580
  if len(prefix) >= max_len - 8:
    return base[:max_len].rstrip()

  # Whole terms only; the "+ 1" reserves room for the final period.
  accepted: list[str] = []
  cur_len = len(prefix)
  for term in combined:
    sep = ", " if accepted else ""
    if cur_len + len(sep) + len(term) + 1 > max_len:
      break
    accepted.append(term)
    cur_len += len(sep) + len(term)

  if not accepted:
    return base
  return f"{prefix}{', '.join(accepted)}."


def is_spanish_hallucination(
    segment_text: str, avg_prob: float | None = None
) -> bool:
  """Detects common Whisper Spanish silence hallucinations and loops.

  Args:
    segment_text: Segment text with control tokens already removed.
    avg_prob: Mean token probability of the segment, or None if unknown.

  Returns:
    True if the segment is empty, matches `SPANISH_HALLUCINATION_PATTERNS`,
    or is a repetition loop (>= 6 words, <= 25% unique) with low or unknown
    confidence (< 0.65).
  """
  cleaned = segment_text.strip()
  if not cleaned:
    return True
  for pattern in SPANISH_HALLUCINATION_PATTERNS:
    if pattern.search(cleaned):
      return True

  # Detect degenerate repetition loops with low model confidence.
  tokens = [t.lower() for t in re.findall(r"\w+", cleaned)]
  if len(tokens) >= 6:
    unique_ratio = len(set(tokens)) / float(len(tokens))
    if unique_ratio <= 0.25 and (avg_prob is None or avg_prob < 0.65):
      return True
  return False


# --- whisper.cpp -ojf parsing ---


def _token_time_ms(token: dict[str, Any], seg_start_ms: int) -> tuple[int, int]:
  """Extracts millisecond offsets from a whisper.cpp -ojf token.

  Args:
    token: One entry of `segment["tokens"]`; times live in
      `token["offsets"]["from"/"to"]` in ms.
    seg_start_ms: Segment start used when the token has no offsets.

  Returns:
    (start_ms, end_ms) with end >= start; (seg_start, seg_start + 80) when
    offsets are missing.
  """
  offsets = token.get("offsets") or {}
  t0 = offsets.get("from")
  t1 = offsets.get("to")
  if t0 is None or t1 is None:
    return seg_start_ms, seg_start_ms + 80
  start = max(0, int(t0))
  end = max(start, int(t1))
  return start, end


def _map_clean_words_to_spans(
    clean_words: list[str],
    merged_words: list[dict[str, Any]],
    seg_start_ms: int,
    seg_end_ms: int,
) -> list[WordTiming]:
  """Maps clean UTF-8 words onto token spans when BPE split multi-byte UTF-8.

  Used when token-level text is corrupt (contains U+FFFD) but the segment
  text is clean. If the word counts match, each clean word takes the timing
  of the corresponding merged token word; otherwise the segment span is
  divided among the words proportionally to their character length.

  Args:
    clean_words: Words from the clean segment text (`seg_text.split()`).
    merged_words: Merged token-word dicts built by `parse_whisper_ojf`
      (keys: text, start_ms, end_ms, prob_sum, count).
    seg_start_ms: Segment start in ms.
    seg_end_ms: Segment end in ms (extended to >= 60 ms per word).

  Returns:
    One WordTiming per clean word, each at least 40 ms long. Interpolated
    words get `prob=0.9`.
  """
  if not clean_words:
    return []
  if len(clean_words) == len(merged_words):
    out: list[WordTiming] = []
    for text, slot in zip(clean_words, merged_words):
      s_ms = int(slot["start_ms"])
      e_ms = max(s_ms + 40, int(slot["end_ms"]))
      prob = slot["prob_sum"] / max(1, slot["count"])
      out.append(WordTiming(text=text, start_ms=s_ms, end_ms=e_ms, prob=prob))
    return out

  span_start = seg_start_ms
  span_end = max(seg_end_ms, seg_start_ms + len(clean_words) * 60)
  total_chars = sum(max(1, len(w)) for w in clean_words)
  cursor = float(span_start)
  duration = float(span_end - span_start)
  out = []
  for idx, word in enumerate(clean_words):
    frac = max(1, len(word)) / float(total_chars)
    nxt = span_end if idx == len(clean_words) - 1 else cursor + duration * frac
    s_ms = int(round(cursor))
    e_ms = max(s_ms + 40, int(round(nxt)))
    out.append(WordTiming(text=word, start_ms=s_ms, end_ms=e_ms, prob=0.9))
    cursor = nxt
  return out


def parse_whisper_ojf(ojf_data: dict[str, Any]) -> list[WordTiming]:
  """Parses whisper.cpp --output-json-full (-ojf) into Spanish WordTimings.

  Pipeline stage 1 (see module docstring). For each segment in
  `ojf_data["transcription"]`:
    1. Strip control/timestamp tokens and skip hallucinated segments.
    2. Merge BPE tokens into words: a token that starts with whitespace
       starts a new word; otherwise it is glued to the previous word,
       except an opening ¿/¡ after a sentence end, which starts a new word.
    3. If any merged word contains U+FFFD (an accented character split
       across tokens), fall back to the clean segment text and interpolate
       timings with `_map_clean_words_to_spans`.

  Args:
    ojf_data: Parsed JSON written by `whisper-cli -ojf`. Segment/token
      times are read from `offsets.from` / `offsets.to` in ms; token
      confidence from `p`.

  Returns:
    Words in time order with non-decreasing starts, each >= 40 ms long and
    with the default speaker. Empty if nothing usable was transcribed.
  """
  words: list[WordTiming] = []
  last_end_ms = 0

  for seg in ojf_data.get("transcription", []):
    seg_text = _INLINE_SPECIAL_TOKEN_RE.sub("", str(seg.get("text", ""))).strip()
    if not seg_text:
      continue

    seg_offsets = seg.get("offsets") or {}
    seg_start_ms = max(0, int(seg_offsets.get("from", last_end_ms)))
    seg_end_ms = max(
        seg_start_ms + 60, int(seg_offsets.get("to", seg_start_ms + 200))
    )

    raw_tokens = seg.get("tokens") or []
    probs = [
        float(tok["p"])
        for tok in raw_tokens
        if "p" in tok
        and not _SPECIAL_TOKEN_RE.match(str(tok.get("text", "")).strip())
    ]
    avg_prob = (sum(probs) / len(probs)) if probs else None

    if is_spanish_hallucination(seg_text, avg_prob):
      continue

    # Each merged entry is a word-in-progress dict:
    #   text: concatenated token pieces; start_ms/end_ms: word span in ms;
    #   prob_sum/count: running sum used to average token probability.
    merged: list[dict[str, Any]] = []
    for tok in raw_tokens:
      raw_text = _INLINE_SPECIAL_TOKEN_RE.sub("", str(tok.get("text", "")))
      if not raw_text or _SPECIAL_TOKEN_RE.match(raw_text.strip()):
        continue
      if not raw_text.strip():
        continue

      t_start, t_end = _token_time_ms(tok, seg_start_ms)
      prob = float(tok.get("p", 0.95))
      starts_with_space = raw_text[0].isspace()
      piece = raw_text.strip()
      if not piece:
        continue

      prev_ends_sentence = bool(merged) and bool(
          _CLAUSE_END_RE.search(merged[-1]["text"])
      )
      starts_new_clause = piece[0] in ("¿", "¡") and prev_ends_sentence

      if starts_with_space or not merged or starts_new_clause:
        merged.append({
            "text": piece,
            "start_ms": t_start,
            "end_ms": max(t_start + 40, t_end),
            "prob_sum": prob,
            "count": 1,
        })
      else:
        cur = merged[-1]
        cur["text"] += piece
        cur["end_ms"] = max(cur["end_ms"], t_end)
        cur["prob_sum"] += prob
        cur["count"] += 1

    has_replacement_char = any("\ufffd" in item["text"] for item in merged)
    if not merged or has_replacement_char:
      clean_words = seg_text.split()
      seg_words = _map_clean_words_to_spans(
          clean_words, merged, seg_start_ms, seg_end_ms
      )
    else:
      seg_words = [
          WordTiming(
              text=item["text"],
              start_ms=item["start_ms"],
              end_ms=max(item["start_ms"] + 40, item["end_ms"]),
              prob=item["prob_sum"] / max(1, item["count"]),
          )
          for item in merged
      ]

    # Whisper segments can start slightly before the previous segment's
    # last word; push such words after it so starts never go backwards.
    for w in seg_words:
      if words and w.start_ms < words[-1].start_ms:
        w.start_ms = words[-1].end_ms
        w.end_ms = max(w.end_ms, w.start_ms + 40)
      words.append(w)
      last_end_ms = max(last_end_ms, w.end_ms)

  return words


# --- Word cleaning: phrase maps, glossary, and ¿/¡ repair ---

# Splits a token into (leading punctuation, core word, trailing punctuation).
# Leading: opening ¿ ¡ quotes, brackets, dashes. Trailing: closing quotes,
# brackets, dashes, and . , ; : ? ! …
_EDGE_PUNCT_RE = re.compile(
    r"^([¿¡\"'«“‘(\[—–-]*)(.*?)([.,;:?!…\"'»”’)\]—–]*)$"
)


def _strip_edge_punct(token: str) -> tuple[str, str, str]:
  """Splits a token into (leading_punct, core_word, trailing_punct)."""
  m = _EDGE_PUNCT_RE.match(token)
  if not m:
    return "", token, ""
  return m.group(1), m.group(2), m.group(3)


def _apply_phrase_map(
    words: list[WordTiming],
    phrase_map: dict[str, str],
    preserve_sentence_cap: bool = False,
) -> list[WordTiming]:
  """Applies 1..N word case-insensitive phrase replacements on WordTimings.

  Matching rules:
    * Keys and values are split on whitespace, so an N-word key can map to
      an M-word value ("dec bilding" -> "deckbuilding").
    * Comparison uses the lowercased core of each word (edge punctuation
      removed). Leading punctuation of the first word and trailing
      punctuation of the last word are carried over to the replacement.
    * A multi-word match must not cross punctuation: inner words may have
      no leading or trailing punctuation (so "mira, play mat" won't merge
      across the comma).
    * Longest keys are tried first; matching is greedy left to right and
      non-overlapping.

  Timing: the replaced window's span (at least 40 ms per source word) is
  split evenly among the replacement words; the last one ends at the window
  end (or 40 ms after its own start, if later). Confidence is the window's
  minimum `prob`.

  Args:
    words: Input words (not mutated; unmatched words are reused as is).
    phrase_map: Lowercase key phrase -> canonical replacement.
    preserve_sentence_cap: If True and the matched first word is
      capitalized (sentence start), capitalize a lowercase replacement.

  Returns:
    A new list of words with replacements applied.
  """
  if not words or not phrase_map:
    return words

  parsed_terms: list[tuple[list[str], list[str]]] = []
  for raw_key, raw_canon in phrase_map.items():
    key_parts = [p for p in raw_key.strip().split() if p]
    canon_parts = [p for p in raw_canon.strip().split() if p]
    if key_parts and canon_parts:
      parsed_terms.append(([p.lower() for p in key_parts], canon_parts))
  # Longest phrases first so multi-word names match before single words.
  parsed_terms.sort(key=lambda item: len(item[0]), reverse=True)
  if not parsed_terms:
    return words

  # Index terms by their first (lowercase) word so each position only tries
  # the few candidates that can start there instead of the whole map. The
  # stable sort above keeps candidates longest-first within each bucket.
  by_first: dict[str, list[tuple[list[str], list[str]]]] = {}
  for norm_parts, canon_parts in parsed_terms:
    by_first.setdefault(norm_parts[0], []).append((norm_parts, canon_parts))

  # Pre-split every word once: (lead, core, core_lower, trail).
  stripped: list[tuple[str, str, str, str]] = []
  for w in words:
    lead, core, trail = _strip_edge_punct(w.text)
    stripped.append((lead, core, core.lower(), trail))

  out: list[WordTiming] = []
  i = 0
  n = len(words)
  while i < n:
    lead0, first_core, first_lower, trail0 = stripped[i]
    candidates = by_first.get(first_lower)
    matched = False
    if candidates:
      for norm_parts, base_canon_parts in candidates:
        k = len(norm_parts)
        if i + k > n:
          continue
        # Trailing punctuation on the first word ends the phrase early.
        if k > 1 and trail0:
          continue
        # Words 1..k-1 must match exactly, with no punctuation in between
        # (the last word may carry trailing punctuation).
        ok = True
        for j in range(1, k):
          lead_j, _, lower_j, trail_j = stripped[i + j]
          if lead_j or lower_j != norm_parts[j]:
            ok = False
            break
          if j < k - 1 and trail_j:
            ok = False
            break
        if not ok:
          continue

        trail_last = stripped[i + k - 1][3]
        window = words[i : i + k]
        canon_parts = list(base_canon_parts)
        if (
            preserve_sentence_cap
            and first_core[:1].isupper()
            and canon_parts[0].islower()
        ):
          canon_parts[0] = canon_parts[0][:1].upper() + canon_parts[0][1:]
        # Spread the window's time evenly over the replacement words.
        total_span = max(k * 40, window[-1].end_ms - window[0].start_ms)
        step = total_span / float(len(canon_parts))
        for idx, cpart in enumerate(canon_parts):
          piece_text = cpart
          if idx == 0:
            piece_text = f"{lead0}{piece_text}"
          if idx == len(canon_parts) - 1:
            piece_text = f"{piece_text}{trail_last}"
          s_ms = int(round(window[0].start_ms + idx * step))
          e_ms = (
              window[-1].end_ms
              if idx == len(canon_parts) - 1
              else max(
                  s_ms + 40, int(round(window[0].start_ms + (idx + 1) * step))
              )
          )
          out.append(
              WordTiming(
                  text=piece_text,
                  start_ms=s_ms,
                  end_ms=max(s_ms + 40, e_ms),
                  prob=min(w.prob for w in window),
                  speaker=window[0].speaker,
              )
          )
        i += k
        matched = True
        break
    if not matched:
      out.append(words[i])
      i += 1
  return out


def _apply_glossary(
    words: list[WordTiming], glossary: list[str]
) -> list[WordTiming]:
  """Applies 1..N word case-insensitive glossary replacements on WordTimings.

  Each glossary term maps to itself, so matching words are recased to the
  exact glossary spelling (e.g. "ark nova" -> "Ark Nova"). Later duplicate
  terms (case-insensitive) win.

  Args:
    words: Input words.
    glossary: Terms with the desired casing; inner whitespace is collapsed.

  Returns:
    A new list of words (or `words` itself if the glossary is empty).
  """
  phrase_map: dict[str, str] = {}
  for term in glossary:
    cleaned = " ".join(p for p in term.strip().split() if p)
    if cleaned:
      phrase_map[cleaned.lower()] = cleaned
  if not phrase_map:
    return words
  return _apply_phrase_map(words, phrase_map, preserve_sentence_cap=False)


def _repair_inverted_spanish_punctuation(words: list[WordTiming]) -> None:
  """Adds missing opening ¿ or ¡ to Spanish question/exclamation clauses.

  Whisper often emits only the closing "?" or "!". The text is split into
  clauses at sentence-ending punctuation or pauses >= 1400 ms; a clause
  ending in ?/! without its opener gets one on the first meaningful word,
  skipping leading hesitations and discourse markers ("Oye, ¿cómo...?",
  "pero ¿qué...?").

  Args:
    words: Words to fix. Mutated in place (only `text` changes).
  """
  if not words:
    return

  clauses: list[tuple[int, int]] = []
  start_idx = 0
  for i, w in enumerate(words):
    ends_clause = bool(_CLAUSE_END_RE.search(w.text))
    long_pause = i < len(words) - 1 and (
        words[i + 1].start_ms - w.end_ms >= 1400
    )
    if ends_clause or long_pause or i == len(words) - 1:
      clauses.append((start_idx, i))
      start_idx = i + 1

  for c_start, c_end in clauses:
    clause = words[c_start : c_end + 1]
    if not clause:
      continue
    last_text = clause[-1].text
    _, _, trail = _strip_edge_punct(last_text)
    opener: str | None = None
    if "?" in trail and not any("¿" in w.text for w in clause):
      opener = "¿"
    elif "!" in trail and not any("¡" in w.text for w in clause):
      opener = "¡"
    if not opener:
      continue

    target_idx = c_start
    # Skip leading pure hesitations (e.g. "eh Sabías...?") before placing ¿/¡.
    while target_idx < c_end:
      _, cur_core, _ = _strip_edge_punct(words[target_idx].text)
      if _MULETILLA_RE.match(cur_core.lower()):
        target_idx += 1
      else:
        break

    # If clause starts with a discourse word + comma (e.g. "Oye, cómo...?")
    # place ¿ / ¡ immediately after the comma.
    if target_idx < c_end:
      _, first_core, first_trail = _strip_edge_punct(words[target_idx].text)
      if (
          "," in first_trail
          and first_core.lower() in SPANISH_DISCOURSE_PREFIXES
      ):
        target_idx += 1
      elif (
          first_core.lower() in {"pero", "y", "o", "entonces", "pues"}
          and not first_trail
          and (c_end - target_idx) >= 2
      ):
        # Leading conjunction without comma ("pero qué haces?") goes outside
        # the question, but only if at least two words remain after it.
        target_idx += 1

    lead, core, rest_trail = _strip_edge_punct(words[target_idx].text)
    if opener not in lead:
      words[target_idx].text = f"{lead}{opener}{core}{rest_trail}"


def clean_spanish_words(
    words: list[WordTiming], config: SpanishFormatConfig
) -> list[WordTiming]:
  """Cleans muletillas, normalizes domain anglicisms, glossary, and ¿¡.

  Pipeline stage 2 (see module docstring). Steps, in order:
    1. Copy words and drop empty ones (input list is never mutated).
    2. If `remove_muletillas`: drop pure hesitations and an "este" filler
       that trails off ("este…") or is isolated by >= 250 ms on both sides.
    3. Domain `phonetic_fixes` (sentence-start capitalization preserved).
    4. Glossary casing: domain terms first, then `custom_glossary`, so user
       terms override domain spellings.
    5. If `fix_inverted_punctuation`: add missing ¿/¡.
    6. If `uppercase`: uppercase every word.

  Args:
    words: Words from `parse_whisper_ojf` (or a previous cleaning pass).
    config: Formatting/linguistic options.

  Returns:
    New WordTiming objects; word count may change (removals, phrase maps).
  """
  working = [
      WordTiming(
          text=w.text,
          start_ms=w.start_ms,
          end_ms=w.end_ms,
          prob=w.prob,
          speaker=w.speaker,
      )
      for w in words
      if w.text.strip()
  ]

  if config.remove_muletillas:
    filtered: list[WordTiming] = []
    for idx, w in enumerate(working):
      _, core, trail = _strip_edge_punct(w.text)
      core_l = core.lower()
      # Silence (ms) around the word; 500 at the edges counts as isolated.
      prev_gap = w.start_ms - working[idx - 1].end_ms if idx > 0 else 500
      next_gap = (
          working[idx + 1].start_ms - w.end_ms
          if idx < len(working) - 1
          else 500
      )
      is_hesitation = bool(_MULETILLA_RE.match(core_l))
      is_isolated_este = core_l == "este" and (
          "…" in trail
          or "..." in w.text
          or (prev_gap >= 250 and next_gap >= 250)
      )
      if is_hesitation or is_isolated_este:
        continue
      filtered.append(w)
    working = filtered

  domain_profile = DOMAIN_PROFILES.get(config.domain, {})
  phonetic_fixes = domain_profile.get("phonetic_fixes") or {}
  if phonetic_fixes:
    working = _apply_phrase_map(
        working, phonetic_fixes, preserve_sentence_cap=True
    )

  combined_glossary = list(domain_profile.get("glossary_terms") or []) + list(
      config.custom_glossary or []
  )
  if combined_glossary:
    working = _apply_glossary(working, combined_glossary)

  if config.fix_inverted_punctuation:
    _repair_inverted_spanish_punctuation(working)

  if config.uppercase:
    for w in working:
      w.text = w.text.upper()

  return working


# --- Line wrapping (RAE / Netflix) ---


def _last_word_penalty(word: str) -> float:
  """Returns RAE/Netflix line-break penalty for ending line 1 with `word`.

  Penalty constants (score units; compared against 500 per overflow char
  and 1.2 per char of line-length imbalance in `wrap_spanish_lines`):
    +70: short word opening a question/exclamation ("¿Qué" / "¡Ay") left
      dangling at the end of line 1.
    +120: weak word (`SPANISH_WEAK_LINE_ENDINGS`) without punctuation, e.g.
      an article separated from its noun. Worth ~100 chars of imbalance,
      so it is avoided unless the alternative overflows.
    -55: sentence end (. ? ! …): best possible break.
    -45: soft pause (, ; : dash): good break.

  Args:
    word: The candidate last token of line 1, with punctuation.

  Returns:
    Penalty to add to the split score (negative is a bonus).
  """
  if not word:
    return 0.0
  _, core, trail = _strip_edge_punct(word)
  core_l = core.lower()

  penalty = 0.0
  if word.startswith(("¿", "¡")) and len(core_l) <= 4:
    penalty += 70.0
  if core_l in SPANISH_WEAK_LINE_ENDINGS and not trail:
    penalty += 120.0
  if _CLAUSE_END_RE.search(word):
    penalty -= 55.0
  elif _SOFT_BREAK_RE.search(word):
    penalty -= 45.0
  return penalty


def wrap_spanish_lines(
    words_text: list[str], max_cpl: int, max_lines: int
) -> list[str]:
  """Breaks a cue's words into balanced Spanish lines respecting RAE grammar.

  For two lines, every split point is scored and the lowest score wins:
    * 500 per character over `max_cpl` on either line (hard limit),
    * `_last_word_penalty` of the last word of line 1 (grammar),
    * -40 if line 2 starts with ¿/¡ (keeps the question/exclamation whole),
    * 1.2 per character of length difference (balance),
    * +4 if line 1 is longer (prefers the bottom-heavy "pyramid" shape).
  For three lines a simple greedy fill is used.

  Args:
    words_text: Display tokens of the cue in order.
    max_cpl: Maximum characters per line.
    max_lines: Maximum number of lines (1, 2, or 3).

  Returns:
    Lines joined with single spaces. A single line if everything fits in
    `max_cpl`, if only one word, or if `max_lines <= 1` (may exceed
    `max_cpl`; `refresh_cue_metrics` flags it). Empty if no words.
  """
  cleaned = [w.strip() for w in words_text if w.strip()]
  if not cleaned:
    return []
  joined = " ".join(cleaned)
  if max_lines <= 1 or len(cleaned) == 1 or len(joined) <= max_cpl:
    return [joined]

  if max_lines == 2:
    best_split = 1
    best_score = float("inf")
    for split in range(1, len(cleaned)):
      l1 = " ".join(cleaned[:split])
      l2 = " ".join(cleaned[split:])
      overflow = max(0, len(l1) - max_cpl) + max(0, len(l2) - max_cpl)
      score = overflow * 500.0
      score += _last_word_penalty(cleaned[split - 1])
      if cleaned[split].startswith(("¿", "¡")):
        score -= 40.0
      score += abs(len(l1) - len(l2)) * 1.2
      # Prefer slightly bottom-heavy pyramid shape when otherwise tied.
      if len(l1) > len(l2):
        score += 4.0
      if score < best_score:
        best_score = score
        best_split = split
    return [" ".join(cleaned[:best_split]), " ".join(cleaned[best_split:])]

  # 3 lines fallback: greedy fill up to `max_cpl`; the last line takes the
  # remainder. NOTE: unlike the 2-line path it applies no weak-word penalty.
  lines: list[str] = []
  current: list[str] = []
  for word in cleaned:
    candidate = " ".join(current + [word])
    if current and len(candidate) > max_cpl and len(lines) < max_lines - 1:
      lines.append(" ".join(current))
      current = [word]
    else:
      current.append(word)
  if current:
    lines.append(" ".join(current))
  return lines


def refresh_cue_metrics(
    cue: SubtitleCue, config: SpanishFormatConfig | None = None
) -> SubtitleCue:
  """Recomputes CPS and Spanish readability warnings for a cue.

  CPS = characters of the lines joined by spaces / duration in seconds
  (duration floored at 50 ms to avoid division by zero). Warnings are
  user-facing Spanish strings: reading speed above `max_cps`, any line
  longer than `max_cpl`, and multi-line cues shorter than
  `min_duration_ms`.

  Args:
    cue: Cue to update. Mutated in place (`cps` and `warnings`).
    config: Limits to check against; defaults to `SpanishFormatConfig()`.

  Returns:
    The same `cue`, for chaining.
  """
  cfg = config or SpanishFormatConfig()
  joined = " ".join(cue.lines)
  char_count = len(joined.replace("\n", " "))
  duration_s = max(0.05, (cue.end_ms - cue.start_ms) / 1000.0)
  cue.cps = char_count / duration_s

  warnings: list[str] = []
  if cue.cps > cfg.max_cps:
    warnings.append(f"Lectura rápida ({cue.cps:.1f} car/s > {cfg.max_cps:.0f})")
  for line in cue.lines:
    if len(line) > cfg.max_cpl:
      warnings.append(f"Línea larga ({len(line)} > {cfg.max_cpl} car.)")
      break
  if cue.end_ms - cue.start_ms < cfg.min_duration_ms and len(cue.lines) > 1:
    warnings.append("Duración corta")
  cue.warnings = warnings
  return cue


def segment_and_wrap_spanish(
    words: list[WordTiming], config: SpanishFormatConfig | None = None
) -> list[SubtitleCue]:
  """Groups cleaned Spanish WordTimings into broadcast-ready SubtitleCues.

  Pipeline stage 3 (see module docstring); runs `clean_spanish_words`
  first, so pass raw `parse_whisper_ojf` output. Then:
    1. Bucketing: walk the words and start a new cue before the current
       word when (first match wins) the silence before it is
       >= `pause_split_ms`, the speaker changes, the cue already has
       `max_words`, the text would exceed `max_cpl * max_lines`, the
       duration would exceed `max_duration_ms` (cue has >= 2 words), the
       previous word ends a sentence, the previous word ends with a soft
       pause and the cue already fills 72% of a line, or the word opens a
       question/exclamation (¿/¡) after >= 3 words.
    2. Wrapping: `wrap_spanish_lines` on each bucket.
    3. Timing polish: extend cues shorter than `min_duration_ms` without
       running past the next cue (keeps a 10 ms gap), close gaps up to
       `gap_bridge_ms`, and trim any overlap with the next cue.

  Args:
    words: Raw words in time order.
    config: Formatting options; defaults to `SpanishFormatConfig()`.

  Returns:
    Cues numbered from 1, in time order, non-overlapping, with `cps` and
    `warnings` filled. Empty if no words survive cleaning.
  """
  cfg = config or SpanishFormatConfig()
  cleaned = clean_spanish_words(words, cfg)
  if not cleaned:
    return []

  max_total_chars = cfg.max_cpl * cfg.max_lines
  buckets: list[list[WordTiming]] = []
  current: list[WordTiming] = []

  for idx, word in enumerate(cleaned):
    if not current:
      current.append(word)
      continue

    prev = current[-1]
    gap_ms = word.start_ms - prev.end_ms
    candidate_text = " ".join([w.text for w in current] + [word.text])
    candidate_dur = word.end_ms - current[0].start_ms

    should_split = False
    if gap_ms >= cfg.pause_split_ms:
      should_split = True
    elif word.speaker != current[0].speaker:
      should_split = True
    elif len(current) >= cfg.max_words:
      should_split = True
    elif len(candidate_text) > max_total_chars:
      should_split = True
    elif candidate_dur > cfg.max_duration_ms and len(current) >= 2:
      should_split = True
    elif _CLAUSE_END_RE.search(prev.text):
      should_split = True
    elif _SOFT_BREAK_RE.search(prev.text) and len(
        " ".join(w.text for w in current)
    ) >= int(cfg.max_cpl * 0.72):
      should_split = True
    elif word.text.startswith(("¿", "¡")) and len(current) >= 3:
      should_split = True

    # NOTE: no orphan-avoidance logic exists here despite the historical
    # intent ("avoid leaving a single 1-word orphan"); a split is applied
    # as soon as any rule above fires.
    if should_split:
      buckets.append(current)
      current = [word]
    else:
      current.append(word)

  if current:
    buckets.append(current)

  # Build raw cues (minimum 80 ms each before polishing).
  cues: list[SubtitleCue] = []
  for idx, bucket in enumerate(buckets, start=1):
    start_ms = bucket[0].start_ms
    end_ms = max(start_ms + 80, bucket[-1].end_ms)
    lines = wrap_spanish_lines(
        [w.text for w in bucket], cfg.max_cpl, cfg.max_lines
    )
    cues.append(
        SubtitleCue(
            index=idx,
            start_ms=start_ms,
            end_ms=end_ms,
            lines=lines,
            words=bucket,
            speaker=bucket[0].speaker,
        )
    )

  # Polish durations, bridge micro-gaps, and eliminate any overlap.
  for i, cue in enumerate(cues):
    next_start = cues[i + 1].start_ms if i + 1 < len(cues) else None
    cur_dur = cue.end_ms - cue.start_ms
    if cur_dur < cfg.min_duration_ms:
      desired_end = cue.start_ms + cfg.min_duration_ms
      if next_start is not None:
        # Grow toward the minimum but stop 10 ms before the next cue;
        # never shrink below the cue's natural end here.
        cue.end_ms = min(desired_end, max(cue.end_ms, next_start - 10))
      else:
        cue.end_ms = desired_end

    if next_start is not None:
      gap = next_start - cue.end_ms
      if 0 < gap <= cfg.gap_bridge_ms:
        cue.end_ms = next_start
      elif cue.end_ms > next_start:
        cue.end_ms = max(cue.start_ms, next_start)

    refresh_cue_metrics(cue, cfg)

  return cues
