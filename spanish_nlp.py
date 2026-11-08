"""Spanish NLP engine: whisper.cpp -ojf parsing, RAE wrapping, and repair."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any


@dataclass
class WordTiming:
  """Single word with millisecond timings and Whisper token confidence."""

  text: str
  start_ms: int
  end_ms: int
  prob: float = 1.0
  speaker: str = "Hablante 1"

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any]) -> WordTiming:
    return cls(
        text=str(raw.get("text", "")),
        start_ms=int(raw.get("start_ms", 0)),
        end_ms=int(raw.get("end_ms", 0)),
        prob=float(raw.get("prob", 1.0)),
        speaker=str(raw.get("speaker", "Hablante 1")),
    )


@dataclass
class SubtitleCue:
  """Formatted subtitle cue ready for preview, SRT/ASS/FCPXML, and Resolve."""

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
    return "\n".join(self.lines)

  def to_dict(self) -> dict[str, Any]:
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
    lines = raw.get("lines")
    if not lines:
      text_val = str(raw.get("text", "")).strip()
      lines = [ln.strip() for ln in text_val.splitlines() if ln.strip()]
    words = [WordTiming.from_dict(w) for w in raw.get("words", [])]
    token_count = sum(len(ln.split()) for ln in lines)
    if words and len(words) != token_count:
      words = []
    start_ms = int(raw.get("start_ms", 0))
    end_ms = max(start_ms + 40, int(raw.get("end_ms", start_ms + 500)))
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


@dataclass
class SpanishFormatConfig:
  """Configurable formatting and linguistic rules for Spanish subtitles."""

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
    return asdict(self)

  @classmethod
  def from_dict(cls, raw: dict[str, Any] | None) -> SpanishFormatConfig:
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


# Words that should NEVER stay orphaned at the end of line 1 (RAE / Netflix ES).
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


_SPECIAL_TOKEN_RE = re.compile(r"^\[_.*_\]$")
_MULETILLA_RE = re.compile(r"^(?:eh+|em+|mm+|hm+|uh+|ah+)$", re.IGNORECASE)
_CLAUSE_END_RE = re.compile(r"[.?!…]+[\"'»”’\)\]]*$")
_SOFT_BREAK_RE = re.compile(r"[,;:—–]+[\"'»”’\)\]]*$")


def build_spanish_initial_prompt(
    dialect: str = "neutro",
    glossary_terms: list[str] | None = None,
    domain: str = "juegos_mesa",
) -> str:
  """Builds the whisper-cli --prompt string in Spanish with domain & glossary."""
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
  max_len = 580
  if len(prefix) >= max_len - 8:
    return base[:max_len].rstrip()

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
  """Detects common Whisper Spanish silence hallucinations and loops."""
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


def _token_time_ms(token: dict[str, Any], seg_start_ms: int) -> tuple[int, int]:
  """Extracts millisecond offsets from a whisper.cpp -ojf token."""
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
  """Maps clean UTF-8 words onto token spans when BPE split multi-byte UTF-8."""
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
  """Parses whisper.cpp --output-json-full (-ojf) into clean Spanish WordTimings."""
  words: list[WordTiming] = []
  last_end_ms = 0

  for seg in ojf_data.get("transcription", []):
    seg_text = str(seg.get("text", "")).strip()
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

    merged: list[dict[str, Any]] = []
    for tok in raw_tokens:
      raw_text = str(tok.get("text", ""))
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

    for w in seg_words:
      if words and w.start_ms < words[-1].start_ms:
        w.start_ms = words[-1].end_ms
        w.end_ms = max(w.end_ms, w.start_ms + 40)
      words.append(w)
      last_end_ms = max(last_end_ms, w.end_ms)

  return words


def _strip_edge_punct(token: str) -> tuple[str, str, str]:
  """Splits a token into (leading_punct, core_word, trailing_punct)."""
  m = re.match(r"^([¿¡\"'«“‘(\[—–-]*)(.*?)([.,;:?!…\"'»”’)\]—–]*)$", token)
  if not m:
    return "", token, ""
  return m.group(1), m.group(2), m.group(3)


def _apply_phrase_map(
    words: list[WordTiming],
    phrase_map: dict[str, str],
    preserve_sentence_cap: bool = False,
) -> list[WordTiming]:
  """Applies 1..N word case-insensitive phrase replacements on WordTimings."""
  parsed_terms: list[tuple[list[str], str]] = []
  for raw_key, raw_canon in phrase_map.items():
    key_parts = [p for p in raw_key.strip().split() if p]
    canon_parts = [p for p in raw_canon.strip().split() if p]
    if key_parts and canon_parts:
      parsed_terms.append(
          ([p.lower() for p in key_parts], " ".join(canon_parts))
      )
  # Longest phrases first so multi-word names match before single words.
  parsed_terms.sort(key=lambda item: len(item[0]), reverse=True)
  if not parsed_terms:
    return words

  out: list[WordTiming] = []
  i = 0
  n = len(words)
  while i < n:
    matched = False
    for norm_parts, canonical in parsed_terms:
      k = len(norm_parts)
      if i + k > n:
        continue
      window = words[i : i + k]
      cores = []
      lead0 = ""
      trail_last = ""
      first_core = ""
      ok = True
      for j, w in enumerate(window):
        lead, core, trail = _strip_edge_punct(w.text)
        if j == 0:
          lead0 = lead
          first_core = core
        elif lead:
          ok = False
          break
        if j == k - 1:
          trail_last = trail
        elif trail:
          ok = False
          break
        cores.append(core.lower())
      if ok and cores == norm_parts:
        canon_parts = canonical.split()
        if (
            preserve_sentence_cap
            and first_core[:1].isupper()
            and canon_parts[0].islower()
        ):
          canon_parts[0] = canon_parts[0][:1].upper() + canon_parts[0][1:]
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
  """Applies 1..N word case-insensitive glossary replacements on WordTimings."""
  phrase_map: dict[str, str] = {}
  for term in glossary:
    cleaned = " ".join(p for p in term.strip().split() if p)
    if cleaned:
      phrase_map[cleaned.lower()] = cleaned
  if not phrase_map:
    return words
  return _apply_phrase_map(words, phrase_map, preserve_sentence_cap=False)


def _repair_inverted_spanish_punctuation(words: list[WordTiming]) -> None:
  """Adds missing opening ¿ or ¡ to Spanish question/exclamation clauses."""
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
        target_idx += 1

    lead, core, rest_trail = _strip_edge_punct(words[target_idx].text)
    if opener not in lead:
      words[target_idx].text = f"{lead}{opener}{core}{rest_trail}"


def clean_spanish_words(
    words: list[WordTiming], config: SpanishFormatConfig
) -> list[WordTiming]:
  """Cleans muletillas, normalizes domain anglicisms, applies glossary, and ¿¡."""
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


def _last_word_penalty(word: str) -> float:
  """Returns RAE/Netflix line-break penalty for ending line 1 with `word`."""
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
  """Breaks a cue's words into balanced Spanish lines respecting RAE grammar."""
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

  # 3 lines fallback (greedy with weak-word avoidance).
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
  """Recomputes CPS and Spanish readability warnings for a cue."""
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
  """Groups cleaned Spanish WordTimings into broadcast-ready SubtitleCues."""
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

    # Avoid leaving a single 1-word orphan if the bucket can absorb or wait.
    if should_split:
      buckets.append(current)
      current = [word]
    else:
      current.append(word)

  if current:
    buckets.append(current)

  # Build raw cues.
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
        cue.end_ms = min(desired_end, max(cue.end_ms, next_start - 10))
      else:
        cue.end_ms = desired_end

    if next_start is not None:
      gap = next_start - cue.end_ms
      if 0 < gap <= cfg.gap_bridge_ms:
        cue.end_ms = next_start
      elif cue.end_ms > next_start:
        cue.end_ms = max(cue.start_ms + 40, next_start)

    refresh_cue_metrics(cue, cfg)

  return cues
