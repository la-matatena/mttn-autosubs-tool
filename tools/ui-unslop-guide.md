# Guía de Unslop para la interfaz de MTTN AutoSubs Tool (`ui/`)

Plan de rediseño y limpieza visual basado en [`heydamir/unslop`](https://github.com/heydamir/unslop) (CC BY 4.0 · Damir Sokolovsky / `@heydamir`) para pulir la aplicación local ([`ui/index.html`](../ui/index.html), [`ui/styles.css`](../ui/styles.css) y las capturas en [`docs/images/`](../docs/images/)) sin romper ningún selector, flujo ni prueba automatizada.

> **Cómo usar este archivo con un asistente de código**
> Puedes pedirle directamente:
> *"Aplica el plan de `tools/ui-unslop-guide.md` sobre `ui/index.html`, `ui/styles.css` y (solo donde se indica) `ui/app.js`, manteniendo intactos todos los IDs del DOM, el tema claro/oscuro, `forced-colors` y dejando `autosubs_es_test.py` y `tools/lint.sh` en verde."*

---

## 1. Dirección (5 líneas de `unslop`)

- **Producto:** Estudio local de subtitulado en español para macOS con Whisper, reglas RAE, forma de onda editable y puente directo a DaVinci Resolve 20.
- **Carácter:** Exacto, silencioso, técnico, de cabina de postproducción.
- **Mundo:** Una suite profesional de edición de audio y vídeo en macOS (Logic / DaVinci / Resolve Fairlight): superficies neutras grafito en oscuro y plata limpia en claro, bordes finos de baja opacidad y máxima prioridad visual para el **Monitor previo**, la **forma de onda** y el **texto de los subtítulos**.
- **Momentos:** (1) El **Monitor previo 16:9 / 9:16** con el subtítulo vivo (y un fotograma real de demostración en Modo Demo ES en lugar de un recuadro negro vacío); (2) la **forma de onda con bloques arrastrables y lectura de CPS** en tipografía monoespaciada tabular.
- **Firma:** Lecturas de código de tiempo (`00:01.20`), métricas `CPS` y atajos de macOS en `<kbd>` alineados en columnas tabulares consistentes.

---

## 2. Auditoría actual (`signs.md` aplicado a `ui/`)

### Superficies y color
- **Exceso de elementos morados compitiendo en el mismo pantallazo** (`ui/styles.css`): conviven simultáneamente 7 acentos morados llenos: `.brand-logo` (l. 293), `.mttn-prefix` (l. 325), `.mode-btn.active` (l. 363), `#btn-transcribe.btn-primary` (l. 767), `#btn-play.btn-primary` (l. 767), `.chip.active` (l. 1314), `#btn-push-resolve.btn-primary`, además de todos los `<input type="range">` y los bloques de la onda. Cuando todo es morado, nada guía la mirada.
- **Halo morado en el logotipo:** `.brand-logo` tiene `box-shadow: 0 2px 8px rgba(131, 103, 199, 0.35)` (`ui/styles.css:294`).
- **Halo morado en tarjetas resaltadas:** `.card-highlight` añade `box-shadow: 0 0 0 1px rgba(131, 103, 199, 0.25)` (`ui/styles.css:619`).
- **Bordes demasiado visibles entre cajas anidadas:** `--border: #2e2e34` y `--border-control: #3a3a44` sobre `--bg: #18181b` y `--card: #232328` crean una cuadrícula densa de recuadros dentro de recuadros (`ui/styles.css:59-70`).
- **Choque cromático del pill `Automático`:** `#perf-mode-pill` usa `.pill-info` con azul cian saturado (`#38bdf8`, `ui/styles.css:91`) justo encima del morado de marca.

### Tipografía
- **Pesos excesivos y mayúsculas con tracking ancho:** `.mttn-prefix` usa `font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase` (`ui/styles.css:328-330`); el título principal y subtítulos usan `font-weight: 700` (`ui/styles.css:311`, `1052`).
- **Tracking negativo en texto pequeño:** `.brand h1` (`-0.01em`, l. 312) y `.card h2` (`-0.005em`, l. 591) aplican tracking negativo en cuerpos de `13–15px`.
- **Números sin `font-variant-numeric: tabular-nums` uniforme** en contadores de subtítulos, `CPS`, porcentajes de opacidad y entradas de tiempo.
- **Cursiva para énfasis:** `<em>(recomendado)</em>` en `#machine-dialog` (`ui/index.html:543`).

### Iconos y símbolos
- **Símbolos Unicode mezclados como iconos:** `★ Anitype Animado (Flipbook)` (`ui/index.html:323`), `▶ Reproducir` (`ui/index.html:270`), `✕` en el botón cerrar del diálogo (`ui/index.html:530`), `✓` en el `<select>` de modelos (`ui/app.js`).
- **Dos estilos de SVG mezclados:** iconos rellenos tipo Heroicons (`fill="currentColor"`, `viewBox="0 0 20 20"`) en la barra superior (`ui/index.html:39-61`) frente a caracteres Unicode en el resto de la app.

### Espaciado, componentes y foco visual
- **Valores fuera de la escala de 4px:** `padding: 10px 18px` en `.topbar` (`ui/styles.css:277`), `border-radius: 3px` (`ui/styles.css:1122`, `1608`), tamaños `11px`/`13px` mezclados sin jerarquía fija.
- **Radios de pastilla (`999px`)** en `.status-btn` y `.pill` (`ui/styles.css:386`, `499`) mezclados con botones de `6px` y `8px`.
- **Monitor previo negro vacío en Modo Demo ES:** en la captura oficial (`docs/images/autosubs-es-overview.png`), el rectángulo 16:9 central es negro puro con una línea punteada de zona segura. Sin un fotograma real de muestra (p. ej. una imagen fija de mesa de juegos de dominio público o generada a medida para el modo Demo), el corazón de la app parece un wireframe inacabado.

---

## 3. Reglas invariables (no romper)

Antes de editar, respeta siempre estas restricciones del repositorio ([`AGENTS.md`](../AGENTS.md)):

1. **Conservar todos los `id`, `data-*`, `name` y clases que lee `ui/app.js` y `tests/js/`.** No renombres botones, inputs ni contenedores; cambia solo estilos, estructura visual no destructiva e iconos internos.
2. **100 % local y sin dependencias externas:** cero Google Fonts ni CDNs dentro de `ui/` (la app debe abrirse y verse perfecta sin conexión a internet). Usa la pila nativa de macOS (`-apple-system`, `"SF Pro Text"`, `"SF Mono"`).
3. **Soporte completo de los 3 modos de tema:** oscuro por defecto, claro automático (`prefers-color-scheme: light`), claro forzado (`data-theme="light"`), más `@media (forced-colors: active)`.
4. **Conservar los 6 pasos numerados y los dos modos** (`App Independiente` y `DaVinci Resolve 20`) y todos los textos en español con diacríticos y signos `¿` `¡`.

---

## 4. Plan en 9 pasadas (en orden)

### Pasada 1 · Espaciado (rejilla de 4px)
- Topbar a `52px` o `56px` de altura con `padding: 8px 16px` y `gap: 12px`.
- Paneles laterales (`280px–300px` en escritorio) con `gap: 12px` entre secciones y `padding: 16px` uniforme dentro de cada bloque.
- Controles estándar a `32px` de alto (`28px` compactos en la barra de transporte/editor), `gap: 8px` en filas de botones, `gap: 4px` entre icono y etiqueta.
- Sustituir todos los radios sueltos (`3px`, `10px`, `999px`) por la escala anidada: `4px` (teclas/inputs internos), `6px` (botones/chips), `10px` (paneles/tarjetas), `12px` (diálogo modal).

### Pasada 2 · Color y jerarquía de acentos (el cambio más importante)
- **Superficies oscuras (rampa grafito neutra con hairlines al 8 %):**
  - `--bg: #121214` (fondo de ventana)
  - `--topbar-bg: #161618`
  - `--panel: #161618`
  - `--card: #1b1b1e`
  - `--surface-inset: #0e0e10` (monitor, forma de onda, inputs)
  - `--surface-elevated: #242428` (botones secundarios, estados activos neutros)
  - `--border: rgba(244, 244, 246, 0.08)` y `--border-control: rgba(244, 244, 246, 0.14)`
- **Superficies claras (plata limpia neutra con hairlines al 8–10 %):**
  - `--bg: #f5f5f7`, `--card: #ffffff`, `--surface-inset: #ededf0`, `--border: rgba(24, 24, 27, 0.09)`.
- **Recortar el morado de marca (`#8367c7` / `#6a4fb3`) a solo 2 lugares principales por pantalla:**
  1. **Acciones primarias de ejecución** (`#btn-transcribe` y `#btn-push-resolve`): usar `#6a4fb3` (`--mttn-purple-deep`, contraste > 6:1 con blanco) con `inset 0 1px 0 rgba(255, 255, 255, 0.12)` y **sin glow**.
  2. **Foco y selección activa sutil:** borde de foco (`:focus-visible`), bloque de subtítulo seleccionado en la forma de onda y barra de progreso en curso.
- **Desaturar los otros 5 controles que hoy son morados:**
  - `.mode-btn.active` (selector `App Independiente | DaVinci Resolve 20`): superficie elevada (`--surface-elevated`) con texto primario y hairline superior, estilo *segmented control* nativo de macOS — **no fondo morado**.
  - `.chip.active` (presets de estilo `Cine Netflix`, etc.): superficie elevada con borde hairline más claro + punto indicador de `6px` en `--mttn-purple`, en lugar de ladrillo morado sólido.
  - `#btn-play` (`Reproducir`): botón neutro de alto contraste (en oscuro: `#ededed` con icono/texto `#121214`; en claro: `#18181b` con blanco) para que el transporte se lea como herramienta de vídeo profesional y no compita con `Transcribir`.
  - `.brand-logo`: quitar el `box-shadow` morado; dejar la marca plana de `28×28` y eliminar la cápsula `.mttn-prefix` morada (dejar `MTTN AutoSubs Tool` como texto limpio en `500`).
  - `#perf-mode-pill` (`Automático`): pasar de pill azul cian brillante a texto monoespaciado tranquilo o etiqueta neutra con hairline (solo colorear si hay aviso real de hardware).

### Pasada 3 · Iconos
- Unificar todos los iconos en **SVG de trazo fino `16×16` (`stroke="currentColor" stroke-width="1.5" fill="none"`)**:
  - Reemplazar los SVG rellenos de la topbar (engranaje, refrescar, sol/luna).
  - Reemplazar `▶` en `#btn-play` por un icono SVG `play` / `pause` (actualizando la referencia en `ui/app.js` si alterna el icono al reproducir).
  - Reemplazar `✕` en `#machine-dialog` por un icono `x` de `16×16`.
  - Quitar `★` de `Anitype Animado (Flipbook)` (texto limpio).
  - Quitar `✓` del texto de las opciones instaladas o reemplazarlo por estado limpio `(instalado)`.

### Pasada 4 · Copy y micro-etiquetas
- Quitar `<em>` en `Automático (recomendado)` y poner texto secundario quieto.
- Acortar textos redundantes donde convenga sin cambiar significado (p. ej. `Buscar` en vez de `Buscar...`, `Reemplazar` en vez de `Reemplazar...`).
- Eliminar puntos finales en etiquetas cortas o subtítulos de una sola línea.

### Pasada 5 · Tipografía
- Pesos: `400` para cuerpo/controles, `500` para cabeceras de sección (`1. Audio de origen`, etc.) y botones primarios, `600` como techo máximo solo en el nombre de la app. Eliminar `700` y `800`.
- `letter-spacing: 0` en toda la interfaz (nada de tracking negativo en `13–15px` ni tracking ancho en mayúsculas).
- Activar `font-variant-numeric: tabular-nums` en `.mono`, `.stat`, `.range-val`, `.alpha-num`, los tiempos de entrada/salida y los valores de `CPS`.

### Pasada 6 · Layout y respiración visual
- Reducir el efecto "caja dentro de caja": en lugar de 6 tarjetas gruesas flotando sobre el fondo, tratar las columnas izquierda y derecha como **paneles laterales continuos divididos por líneas hairline horizontales** (como el Inspector de DaVinci Resolve o Logic Pro), dejando el protagonismo espacial al centro (**Monitor previo + Forma de onda + Editor**).
- Mantener el ancho completo del viewport y el comportamiento responsivo para ventanas medianas.

### Pasada 7 · Detalles
- **Estado activo de subtítulo en el editor:** borde izquierdo fino de `2px` en `--mttn-purple` + ligero paso de superficie (`--surface-elevated`), sin inundar toda la tarjeta de morado.
- **Sliders (`input[type="range"]`):** track delgado neutro de `4px` con relleno sutil y pulgar circular limpio de `12px` en color de texto primario (no cinco barras moradas compitiendo en el panel de colores).
- **Atajos de teclado:** estilizar todos los `<kbd>` (`⌘`, `Esc`) como keycaps uniformes de `4px` de radio con borde hairline.
- **Bloques de la forma de onda (`ui/app.js`):** bloques neutros oscuros con borde hairline; **solo el bloque activo o en arrastre** toma el acento `--mttn-purple`.

### Pasada 8 · Momento visual del Monitor previo (Modo Demo ES + Capturas)
- Añadir una escena/fotograma real de muestra (estático, ligero, `.webp` local en `ui/` o generado en canvas con escena limpia de mesa/estudio) para el **Modo Demo ES** cuando no hay vídeo cargado, manteniendo los 4 fondos de prueba (`Negro`, `Gris`, `Claro`, `Cuadrícula`) para inspeccionar contraste real.
- Así, cuando se vuelvan a tomar las capturas oficiales en `docs/images/` (especialmente `autosubs-es-overview.png` y `autosubs-es-monitor-waveform.png`), el monitor central mostrará un fotograma vivo con subtítulos nítidos en lugar de un hueco negro vacío.

### Pasada 9 · Movimiento
- Curva única `--ease: cubic-bezier(0.22, 1, 0.36, 1)`.
- Transicionar explícitamente `color`, `background-color`, `border-color`, `opacity` y `transform` (`140ms–160ms`). Quitar `box-shadow` de las transiciones y cualquier `transition: all`.
- Feedback de pulsación discreto (`transform: scale(0.98)` en `:active` de botones principales).

---

## 5. Checklist de verificación tras aplicar el unslop a `ui/`

- [ ] Ningún glow morado en `.brand-logo`, `.card-highlight` ni botones.
- [ ] Como máximo 2 superficies rellenas de `--mttn-purple-deep` visibles a la vez (`Transcribir` y `Enviar a Resolve`).
- [ ] Cero emojis o símbolos Unicode (`★`, `▶`, `✕`) usados como iconos.
- [ ] Cero pesos `700`/`800`, cero `text-transform: uppercase` con tracking ancho, cero `<em>` por énfasis.
- [ ] Tema oscuro, tema claro y `forced-colors: active` comprobados visualmente.
- [ ] Tests de Python y JS en verde:
  ```bash
  python3 -B autosubs_es_test.py -v
  tools/lint.sh
  ```
- [ ] Capturas de `docs/images/*.png` actualizadas con la nueva UI y reflejadas automáticamente en la landing al hacer push a `main`.
