/**
 * @fileoverview ESLint flat config for the Studio UI and its Node tests.
 *
 * Dev-only: run through tools/lint.sh (CI runs the same script). It has no
 * third-party imports (no `globals` package, no plugins), so a bare
 * `npm install eslint` is enough; browser and Node globals are listed inline.
 * Formatting is Prettier's job (.prettierrc.json), so only correctness and
 * modern-syntax rules are enabled here.
 */

/** Browser globals used by ui/app.js and ui/app_helpers.js. */
const BROWSER_GLOBALS = {
  window: "readonly",
  document: "readonly",
  navigator: "readonly",
  location: "readonly",
  localStorage: "readonly",
  sessionStorage: "readonly",
  console: "readonly",
  fetch: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  setInterval: "readonly",
  clearInterval: "readonly",
  requestAnimationFrame: "readonly",
  cancelAnimationFrame: "readonly",
  getComputedStyle: "readonly",
  matchMedia: "readonly",
  performance: "readonly",
  globalThis: "readonly",
  AbortController: "readonly",
  Blob: "readonly",
  File: "readonly",
  FileReader: "readonly",
  FormData: "readonly",
  FontFace: "readonly",
  Image: "readonly",
  KeyboardEvent: "readonly",
  MutationObserver: "readonly",
  ResizeObserver: "readonly",
  IntersectionObserver: "readonly",
  URL: "readonly",
  URLSearchParams: "readonly",
  HTMLElement: "readonly",
  HTMLInputElement: "readonly",
  HTMLCanvasElement: "readonly",
  HTMLButtonElement: "readonly",
  HTMLDialogElement: "readonly",
  CSS: "readonly",
  Event: "readonly",
  CustomEvent: "readonly",
  DOMParser: "readonly",
  TextDecoder: "readonly",
  TextEncoder: "readonly",
  alert: "readonly",
  confirm: "readonly",
  prompt: "readonly",
};

/** Node globals used by the tests and by the CommonJS branch of the UMD. */
const NODE_GLOBALS = {
  console: "readonly",
  process: "readonly",
  module: "writable",
  require: "readonly",
  globalThis: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  setInterval: "readonly",
  clearInterval: "readonly",
  queueMicrotask: "readonly",
  setImmediate: "readonly",
  URL: "readonly",
};

/** Correctness and modern-syntax rules shared by every JS file. */
const SHARED_RULES = {
  "no-undef": "error",
  // `_`-prefixed parameters and catch bindings are intentionally unused.
  "no-unused-vars": [
    "error",
    { args: "after-used", argsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
  ],
  "no-var": "error",
  "prefer-const": "error",
  // `x == null` is the deliberate "null or undefined" check in app.js.
  eqeqeq: ["error", "always", { null: "ignore" }],
  "no-implicit-globals": "error",
  "no-redeclare": "error",
  "no-shadow-restricted-names": "error",
  "no-dupe-keys": "error",
  "no-duplicate-case": "error",
  "no-unreachable": "error",
  "no-unsafe-finally": "error",
  "no-self-assign": "error",
  "no-self-compare": "error",
  "no-constant-condition": ["error", { checkLoops: false }],
  "no-empty": ["error", { allowEmptyCatch: true }],
  "no-fallthrough": "error",
  "no-throw-literal": "error",
  "no-useless-escape": "error",
  "use-isnan": "error",
  "valid-typeof": "error",
  // Braceless bodies are fine only on the same line (`if (!el) return;`).
  curly: ["error", "multi-line"],
};

export default [
  {
    // Only first-party JS is linted; everything else is skipped explicitly.
    ignores: ["site/**", "templates/**", "node_modules/**", ".venv-dev/**", "dist/**"],
  },
  {
    // ui/app.js is a classic <script> (one IIFE); app_helpers.js is UMD.
    files: ["ui/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...BROWSER_GLOBALS, module: "readonly" },
    },
    rules: SHARED_RULES,
  },
  {
    files: ["tests/js/**/*.mjs", "eslint.config.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: NODE_GLOBALS,
    },
    rules: SHARED_RULES,
  },
];
