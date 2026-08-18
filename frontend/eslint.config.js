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
];
