import { Component } from 'react';

/**
 * Contains a render crash to its own subtree instead of unmounting the SPA.
 *
 * A class component because React still has no hook equivalent for error
 * boundaries — this is the one place that is required.
 *
 * It is mounted twice on purpose (see App.jsx): once around everything
 * including the providers, and once around `<Outlet/>` only. The inner one
 * keeps the masthead and sidebar alive when a page throws, so the operator can
 * navigate away from the broken page instead of reloading. The outer one exists
 * because a boundary cannot catch a throw from its own ancestors: a provider
 * that throws during render — `localStorage` access does throw, in Safari
 * private mode and under a blocked-cookies policy — takes the whole root down
 * with no fallback at all, which is a genuinely blank page.
 *
 * `resetKey` clears the captured error when it changes. Layout passes the
 * pathname, so navigating away from a crashed page recovers without a reload.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, resetKey: props.resetKey };
  }

  static getDerivedStateFromProps(props, state) {
    if (props.resetKey !== state.resetKey) {
      return { error: null, resetKey: props.resetKey };
    }
    return null;
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // The fallback swallows the crash, so this is the only trace left — and the
    // component stack is what turns "Cannot read properties of undefined" into
    // a locatable bug in a screenshot of a browser console.
    console.error('[ErrorBoundary]', error, info?.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div role="alert" className="admin-crash" data-testid="error-boundary">
        <h2 className="admin-crash__title">{this.props.title || 'This page failed to render'}</h2>
        <p className="admin-crash__message">{error.message || String(error)}</p>
        <p className="admin-crash__hint">
          Nothing was sent to the cluster. Navigating to another page usually clears this; if it does
          not, reload.
        </p>
        <button
          type="button"
          className="pf-v6-c-button pf-m-primary"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    );
  }
}
