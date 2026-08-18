import React from 'react';
import ReactDOM from 'react-dom/client';

// PatternFly's base stylesheet, imported exactly once and before the app's own
// sheet. Order matters: index.css overrides a handful of PatternFly tokens, and
// importing base.css from a component instead would let Vite hoist it after
// index.css in the built bundle, silently undoing every override in production
// while dev looked correct.
import '@patternfly/react-core/dist/styles/base.css';

import App from './App';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
