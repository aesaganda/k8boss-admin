import js from '@eslint/js';
import globals from 'globals';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

// Flat config. The plugins are wired by hand rather than through their exported
// flat presets: the preset export names moved between plugin majors
// (`configs.recommended` -> `configs.flat.recommended` ->
// `configs['recommended-latest']`), and an eslint config that throws on load
// makes `npm run lint` fail with a module error that reads nothing like a lint
// problem. Spreading `.rules` is stable across all of them.
export default [
  {
    ignores: ['dist', 'playwright-report', 'test-results', 'node_modules'],
  },
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.es2021,
      },
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    plugins: {
      react,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Load-bearing, not cosmetic. `ecmaFeatures.jsx` lets ESLint *parse* JSX,
      // but core `no-unused-vars` does not treat `<Foo/>` as a reference to
      // `Foo` — that marking is this rule's entire job. Without it every
      // component imported to be rendered is reported as unused, which is 529
      // false positives on this codebase and, worse, a lint run whose advice is
      // "delete the imports the app renders from". A linter that confidently
      // reports a working file as broken is the defect standard applied to our
      // own tooling.
      'react/jsx-uses-vars': 'error',
      // The rule that catches a component used in JSX and never imported. Core
      // `no-undef` cannot: eslint-scope creates no reference for a JSXIdentifier,
      // so `<Tooltip/>` with no import is invisible to it — which is why
      // `jsx-uses-vars` above has to exist at all. Vite does not catch it either
      // (an undefined global is legal JavaScript), so the whole toolchain was
      // silent about two pages that threw ReferenceError on first render and
      // showed the error boundary instead of their content.
      'react/jsx-no-undef': 'error',
      // And the same failure outside JSX. A call to a function that was never
      // imported is legal JavaScript, so Vite builds it and the error arrives
      // as a ReferenceError during a render — the error boundary in place of a
      // dialog, with the operator's manifest inside it. `globals` above is what
      // makes this rule usable rather than 500 false positives.
      'no-undef': 'error',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Contexts intentionally export both a provider component and its hook
      // from one file; that is the pattern the whole app consumes.
      'no-unused-vars': ['error', { varsIgnorePattern: '^_', argsIgnorePattern: '^_' }],
    },
  },
  {
    // Playwright specs run in Node, not the browser, and use its globals.
    files: ['tests/**/*.js', 'playwright.config.js', 'vite.config.js'],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
  {
    // The tour tooling in `tools/` is Node, and `.mjs` matches neither block
    // above — so without this it lints with no globals at all and reports
    // `console` as undefined. Both global sets, not just Node: these scripts
    // drive a browser, and the callbacks they hand to `page.addInitScript` and
    // `page.evaluate` are executed in the page, where `localStorage` and `URL`
    // are exactly the right things to reach for.
    files: ['tools/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
  },
];
