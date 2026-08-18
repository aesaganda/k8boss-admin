import process from 'node:process';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Port 5174, not 5173. k8boss (the sibling product this console was split out
// of) owns 5173, and the two are routinely run side by side while porting a
// pattern across. Sharing a port meant the second `npm run dev` silently took
// 5174 anyway and every hardcoded localhost:5173 in a test or a bookmark then
// hit whichever app had started first — a whole class of "why is this showing
// NetworkPolicies" confusion. Pinning it here makes the split explicit.
const DEV_PORT = 5174;

// The backend defaults to 8020 for the same reason: k8boss' backend is on 8010.
const BACKEND = process.env.VITE_BACKEND_URL || 'http://localhost:8020';

const proxy = {
  '/api': {
    target: BACKEND,
    changeOrigin: true,
    // Pod log streaming and exec are WebSockets under /api/ws/... . Without
    // this the upgrade request is proxied as a plain GET and the browser sees
    // a 200 with an HTML body instead of a socket — which surfaces as "the
    // terminal opens and immediately closes", with nothing in the network tab
    // that looks like an error.
    ws: true,
  },
};

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Three vendor chunks with very different change rates. PatternFly is
        // the largest and changes only on a design-system bump; xterm is only
        // needed once a user opens a terminal, so keeping it out of the main
        // chunk keeps the initial load off the exec path entirely.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('@patternfly')) return 'patternfly';
          if (id.includes('@xterm')) return 'xterm';
          if (id.includes('/react-dom/') || id.includes('/react-router') || /\/react\//.test(id)) {
            return 'react';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: DEV_PORT,
    strictPort: true,
    proxy,
  },
  preview: {
    // Playwright's webServer drives `vite preview`; it must land on the same
    // port as dev so baseURL is one constant rather than two that drift.
    port: DEV_PORT,
    strictPort: true,
    proxy,
  },
});
