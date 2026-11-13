# Guía para agentes de IA · MTTN AutoSubs Tool

Contexto rápido para asistentes de código que trabajen en `la-matatena/mttn-autosubs-tool`. La fuente de verdad completa de la organización está en [`la-matatena/.github/CONVENTIONS.md`](https://github.com/la-matatena/.github/blob/main/CONVENTIONS.md) y [`la-matatena/.github/AGENTS.md`](https://github.com/la-matatena/.github/blob/main/AGENTS.md).

## Reglas esenciales

- **Producto y marca**: `MTTN AutoSubs Tool` (`la-matatena/mttn-autosubs-tool`), paquete `MTTN_AutoSubs_Tool.zip`, acentos morados `#8367c7` / `#6a4fb3` reservados para acciones primarias y estados activos.
- **Idioma**: interfaz (`ui/`) y `README.md` principal en español; código e identificadores en inglés; `README.en.md` y `site/en/` mantienen paridad en inglés.
- **Español primero**: preserva siempre diacríticos (`á é í ó ú ü ñ`), `¿` `¡`, reglas RAE de corte de línea y glosario de juegos de mesa (`juegos_mesa` / BGG) en `spanish_nlp.py` y `exporters.py`.
- **100 % local y cero dependencias `pip` obligatorias**: biblioteca estándar de Python 3.10+ en el servidor (`pywebview` es opcional en el venv del instalador para ventana nativa de macOS). No añadas servicios en la nube.
- **Hardware y VRAM**: mantén la seguridad en iMac 2017 Intel (`--no-gpu` / `-ng` y `libx264` para proteger 2 GB VRAM AMD Polaris) y la aceleración en Apple Silicon (`Metal` + `h264_videotoolbox`), cediendo núcleos cuando DaVinci Resolve 20 está abierto.
- **DaVinci Resolve 20 Free y Studio**: conserva compatibilidad mediante los tres scripts Lua (`1 - MTTN AutoSubs - ...`) y las cinco plantillas `AutoSubs_ES_*.setting` bajo `Effects > Titles > MTTN AutoSubs`.
- **Estilo y documentación del código**: sigue las convenciones de [`docs/design-doc.md#code-style-and-documentation-conventions`](docs/design-doc.md#code-style-and-documentation-conventions) (docstrings estilo Google en Python, JSDoc en `ui/app.js`, LuaDoc en `lua/`, Google Shell Style, comentarios en inglés y textos de interfaz en español, marcadores `shortcut:`); los commits solo de documentación no deben cambiar el comportamiento.

## Verificación antes de terminar

- Ejecuta siempre la suite completa:
  ```bash
  python3 -B autosubs_es_test.py -v
  ```
- Si cambias el empaquetado, verifica también:
  ```bash
  python3 -B package_bundle.py --output /tmp/MTTN_AutoSubs_Tool.zip
  ```
- Ejecuta `tools/lint.sh` (formato y lint; `tools/lint.sh --fix` aplica los formateadores) y deja todas las comprobaciones en verde.
- Usa Conventional Commits en inglés (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`).
- No publiques releases, tags ni hagas `push` sin confirmación explícita.
