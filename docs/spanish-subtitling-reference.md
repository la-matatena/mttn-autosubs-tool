# Spanish NLP, RAE line-wrapping, and formatting reference

This reference documents how the `spanish_nlp.py` module transforms raw `whisper-cli` JSON tokens into readable Spanish subtitles that follow Real Academia Española (RAE) orthographic rules and Netflix and broadcast timing standards. Video editors and localization leads can use these details to tune domain packs, phonetic repairs, presets, custom glossaries, and dialect prompts.

## Timing and layout presets

MTTN AutoSubs Tool provides four built-in formatting profiles in `FORMAT_PRESETS`. Switching presets and clicking **Aplicar reglas RAE y glosario** recalculates cue boundaries from stored `WordTiming` tokens immediately without re-running Whisper.

| Preset ID | UI label | Max chars per line | Max lines | Max words | Target / max CPS | Duration range (ms) | Pause split / gap bridge (ms) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `youtube_16_9` | `YouTube 16:9 (42 car.)` | 42 | 2 | 14 | 17.0 / 20.0 | 830–6,500 | 650 / 120 |
| `reels_9_16` | `Reels / Shorts 9:16` | 20 | 2 | 4 | 18.0 / 22.0 | 480–3,500 | 450 / 100 |
| `karaoke_impacto` | `Impacto (1–2 pal.)` | 15 | 1 | 2 | 20.0 / 26.0 | 220–2,200 | 320 / 60 |
| `cine_es` | `Cine / Documental` | 40 | 2 | 12 | 15.0 / 18.0 | 1,000–6,000 | 700 / 140 |

The editor flags cues in orange when calculated reading speed (`cps`) exceeds `max_cps` or when any wrapped line exceeds `max_cpl`.

## Spanish dialect prompts, board-game domain, and custom glossary

`build_spanish_initial_prompt` seeds `whisper-cli --prompt` with regional Spanish vocabulary from `SPANISH_DIALECT_PROMPTS`, domain vocabulary from `DOMAIN_PROFILES` (`juegos_mesa` by default), and explicit `¿...?` and `¡...!` punctuation priming. When `whisper-cli` supports `--carry-initial-prompt`, `autosubs_server.py` keeps this prompt active past the initial 30-second segment window with `-mc 64`.

| Key | Category | UI label | Vocabulary and conditioning role |
| :--- | :--- | :--- | :--- |
| `juegos_mesa` | Domain | `Juegos de Mesa + Anglicismos (BGG)` | Board-game mechanics, components, events, and BGG anglicisms (`BoardGameGeek`, `BGG`, `eurogame`, `ameritrash`, `meeples`, `deckbuilding`, `worker placement`, `playmat`, `setup`, `downtime`, `draft`, `Kickstarter`, `Gamefound`). |
| `general` | Domain | `General / Documental` | Clean Spanish punctuation and capitalization without specialized anglicisms. |
| `tech_video` | Domain | `Tech y Edición Vídeo` | Editing and post-production terminology (`DaVinci Resolve`, `Fusion`, `timeline`, `render`, `LUT`, `proxy`, `keyframe`, `SRT`). |
| `neutro` | Dialect | `Español Neutro` | Neutral Spanish orthography, accent marks, and inverted opening punctuation. |
| `es_es` | Dialect | `España (Peninsular)` | Peninsular forms (`vosotros`, `tenéis`, `podéis`, `vale`) and standard punctuation. |
| `es_mx` | Dialect | `México / LatAm` | Latin American and Mexican phrasing (`ustedes`, `qué padre`, `ahorita`, `platicar`). |
| `es_ar` | Dialect | `Río de la Plata` | Rioplatense voseo (`vos tenés`, `mirá`, `che`, `laburo`). |

Domain selection (`SpanishFormatConfig.domain`) is independent from regional accent (`dialect`) so you can combine Peninsular Spanish or Mexican Spanish with `Juegos de Mesa + Anglicismos (BGG)`. Selecting the legacy `tech_spanglish` dialect key maps automatically to `domain = "tech_video"` for backward compatibility.

### Deterministic anglicism repair and glossary precedence

When `juegos_mesa` is active, `clean_spanish_words` applies `BOARDGAME_PHONETIC_FIXES` (~70 multi-word and single-word phonetic repair rules) before custom glossary casing so common Hispanicized Whisper errors turn into standard terminology:

-   Multi-word splits and phonetic approximations: `dec bilding` becomes `deckbuilding`, `worker pleisment` becomes `worker placement`, `be ge ge` becomes `BGG`, `bord gueim guik` becomes `BoardGameGeek`, `geim faund` becomes `Gamefound`, `teibol top` becomes `tabletop`, and `rul buk` becomes `rulebook`.
-   Single-token anglicisms and popular game names: `eurogueim` becomes `eurogame`, `mipel` or `mipels` become `meeple` or `meeples`, `setap` becomes `setup`, `pleimat` becomes `playmat`, `dauntaim` becomes `downtime`, `kikstarter` becomes `Kickstarter`, and `glumjeiven` becomes `Gloomhaven`.

Terms entered in **Glosario personalizado** (or added with **+ Términos BGG** from `BOARDGAME_GLOSSARY_TERMS`) perform two tasks:

1.  **Prompt biasing**: Places your custom glossary terms ahead of the domain list inside the 580-character `whisper-cli --prompt` budget so rare proper nouns are never truncated.
2.  **Deterministic post-processing**: `_apply_glossary` scans normalized word windows from longest phrase to shortest (`k` words down to `1` word) after `BOARDGAME_PHONETIC_FIXES`, restores exact casing (such as `DaVinci Resolve`, `BoardGameGeek`, or `Worker Placement`), and preserves leading or trailing punctuation (`¿`, `¡`, commas, periods, question marks).

## BPE token reconstruction and silence-hallucination filtering

`parse_whisper_ojf` consumes `whisper-cli --output-json-full` (`-ojf`) segments and applies three repair stages:

1.  **Silence-hallucination suppression**: `is_spanish_hallucination` discards segments matching common Whisper Spanish YouTube/subtitling hallucinations (`amara.org`, `Subtítulos realizados por`, `Subtitulado por`, `Transcripción realizada por`, `Suscríbete al canal`, `Gracias por ver el vídeo`, `Dale like y suscríbete`, `[música]`, `[aplausos]`) as well as low-confidence repetition loops (`unique_ratio` at most `0.25` with `avg_prob` under `0.65`).
2.  **Sub-word merging (`-sow`)**: Merges consecutive byte-pair tokens lacking leading whitespace into whole Spanish words, while keeping `¿` and `¡` clause boundaries separated when adjacent to sentence-ending punctuation.
3.  **UTF-8 split-byte recovery**: When Whisper splits a multi-byte accented character (`ñ`, `á`, `é`, `í`, `ó`, `ú`) across two BPE tokens and emits Unicode replacement characters (`\ufffd`), `parse_whisper_ojf` falls back to the clean UTF-8 words in `seg["text"]` and interpolates timestamps across the segment span.

## Inverted punctuation repair and filler cleanup

`clean_spanish_words` applies optional orthographic cleanup before cue segmentation:

-   **Opening `¿` and `¡` insertion**: `_repair_inverted_spanish_punctuation` inspects clauses ending in `?` or `!` (or bounded by pauses of at least 1,400 ms). When the matching opener is missing, the function skips leading discourse markers (`SPANISH_DISCOURSE_PREFIXES` such as `Oye,`, `Mira,`, `Bueno,`, `Vale,`, `Pues,`) and leading hesitation noises (`_MULETILLA_RE`: `eh`, `em`, `mm`, `hm`, `uh`, `ah`) before inserting `¿` or `¡`, transforming `eh Sabías...?` into `eh ¿Sabías...?` and `Oye, cómo funciona?` into `Oye, ¿cómo funciona?`.
-   **Safe filler removal (`remove_muletillas`)**: Removes pure hesitation noises (`eh`, `em`, `mm`, `hm`, `uh`, `ah`) and isolated `"este..."` hesitations flanked by ellipsis or pauses of at least 250 ms, while preserving grammatical demonstratives such as `"Este flujo funciona bien"`.

## RAE and broadcast line-wrapping algorithm

For two-line subtitles, `wrap_spanish_lines` evaluates every possible split point `1 .. N-1` and selects the split with the lowest composite penalty score:

-   **Overflow avoidance**: Heavily penalizes either line exceeding `max_cpl` (`overflow * 500.0`).
-   **RAE weak-ending prohibition**: Checks `SPANISH_WEAK_LINE_ENDINGS` and adds a `+120.0` penalty if line 1 ends with a stranded article (`el`, `la`, `los`, `las`, `un`, `una`), preposition (`a`, `con`, `de`, `en`, `para`, `por`, `sin`), conjunction (`y`, `e`, `o`, `u`, `que`, `pero`), clitic pronoun (`me`, `te`, `se`, `nos`, `le`), possessive (`mi`, `tu`, `su`), or auxiliary `haber`/`ser`/`estar` form, plus a `+70.0` penalty for short tokens opened by `¿` or `¡`.
-   **Grammatical boundary preference**: Rewards splitting after sentence-ending punctuation (`-55.0`), after commas or semicolons (`-45.0`), or immediately before an opening `¿` or `¡` (`-40.0`).
-   **Bottom-heavy pyramid balance**: Minimizes length asymmetry (`abs(len(l1) - len(l2)) * 1.2`) and adds a small `+4.0` preference when line 2 is slightly wider than line 1 for visual stability on screen.

## What's next

-   Follow the editorial workflow in the [user guide](user-guide.md).
-   Review hardware scheduling and HTTP endpoints in the [architecture and 2017 iMac tuning guide](imac-2017-architecture.md).
-   Resolve common macOS or Resolve errors in the [troubleshooting guide](troubleshooting.md).
