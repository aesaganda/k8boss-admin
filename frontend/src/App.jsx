/**
 * App — providers, router, routes.
 *
 * Pages are code-split. Each becomes its own chunk fetched on first
 * navigation, so the initial load ships the shell and nothing else; the
 * Suspense boundary that covers the fetch lives in Layout, around `<Outlet/>`
 * only, so the sidebar stays usable while a page downloads.
 */
import { lazy } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import ErrorBoundary from './components/ErrorBoundary';
import Layout from './components/Layout';
import { EmptyState, ErrorState } from './components/ui';
import { ClusterProvider } from './contexts/ClusterContext';
import { HealthProvider } from './contexts/HealthContext';
import { NamespaceProvider } from './contexts/NamespaceContext';
import { NotificationProvider } from './contexts/NotificationContext';
import { ThemeProvider } from './contexts/ThemeContext';

/**
 * Lazy-load a page, and degrade to a named failure if its chunk cannot be
 * fetched.
 *
 * This is not hypothetical: after a redeploy, a browser holding the previous
 * `index.html` asks for a hashed chunk filename that no longer exists on the
 * server. The import rejects, React re-throws it out of Suspense, and the
 * operator gets the error boundary's generic crash panel for what is actually
 * "your tab is out of date". Naming the module and offering a reload turns an
 * unexplained blank panel into an instruction. The failure stays contained to
 * the one route, which is the isolation rule: a missing page degrades that
 * page, never the console.
 */
function lazyPage(name, loader) {
  return lazy(() =>
    loader().catch((error) => {
      console.error(`[App] page chunk "${name}" failed to load`, error);
      return {
        default: function PageChunkFailure() {
          return (
            <ErrorState
              title={`The ${name} page could not be loaded`}
              detail={
                'Its JavaScript chunk did not download. This usually means the console was ' +
                'redeployed while this tab was open, and the tab is asking for a file that no ' +
                'longer exists. Reloading fixes it.'
              }
              error={error}
              onRetry={() => window.location.reload()}
            />
          );
        },
      };
    }),
  );
}

const Overview = lazyPage('Overview', () => import('./pages/Overview'));
const Nodes = lazyPage('Nodes', () => import('./pages/Nodes'));
const NodeDetail = lazyPage('NodeDetail', () => import('./pages/NodeDetail'));
const Namespaces = lazyPage('Namespaces', () => import('./pages/Namespaces'));
const Workloads = lazyPage('Workloads', () => import('./pages/Workloads'));
const WorkloadDetail = lazyPage('WorkloadDetail', () => import('./pages/WorkloadDetail'));
const Pods = lazyPage('Pods', () => import('./pages/Pods'));
const Network = lazyPage('Network', () => import('./pages/Network'));
const Config = lazyPage('Config', () => import('./pages/Config'));
const Storage = lazyPage('Storage', () => import('./pages/Storage'));
const Access = lazyPage('Access', () => import('./pages/Access'));
const Events = lazyPage('Events', () => import('./pages/Events'));
const Explorer = lazyPage('Explorer', () => import('./pages/Explorer'));
const Clusters = lazyPage('Clusters', () => import('./pages/Clusters'));
const Audit = lazyPage('Audit', () => import('./pages/Audit'));

function NotFound() {
  return (
    <EmptyState
      title="No such page"
      description="That URL does not match any view in this console. Use the navigation on the left."
    />
  );
}

export default function App() {
  return (
    // Outermost boundary, OUTSIDE every provider. A boundary only catches
    // throws from its own subtree, so the one inside Layout cannot see a
    // provider's render throw — and those are real: ThemeContext and
    // ClusterContext both touch localStorage during render or in a layout
    // effect, and localStorage throws in Safari private mode and under a
    // blocked-cookies policy. Without this, that unmounts the root and leaves a
    // genuinely blank page with no fallback and no reload button.
    <ErrorBoundary title="The console failed to start">
      <ThemeProvider>
        <NotificationProvider>
          <HealthProvider>
            <ClusterProvider>
              <NamespaceProvider>
                <BrowserRouter>
                  <Routes>
                    <Route path="/" element={<Layout />}>
                      <Route index element={<Overview />} />

                      <Route path="nodes" element={<Nodes />} />
                      <Route path="nodes/:name" element={<NodeDetail />} />

                      <Route path="namespaces" element={<Namespaces />} />

                      <Route path="workloads" element={<Workloads />} />
                      <Route
                        path="workloads/:plural/:namespace/:name"
                        element={<WorkloadDetail />}
                      />

                      <Route path="pods" element={<Pods />} />
                      <Route path="network" element={<Network />} />
                      <Route path="config" element={<Config />} />
                      <Route path="storage" element={<Storage />} />
                      <Route path="access" element={<Access />} />
                      <Route path="events" element={<Events />} />

                      {/* One page, two routes: the explorer with no resource
                          chosen is the catalog, and the same page renders a
                          listing once a group/version/plural is in the URL.
                          Splitting them into two components duplicated the
                          catalog fetch and let the two views disagree about
                          which resources this cluster serves. */}
                      <Route path="explorer" element={<Explorer />} />
                      <Route path="explorer/:group/:version/:plural" element={<Explorer />} />

                      <Route path="clusters" element={<Clusters />} />
                      <Route path="audit" element={<Audit />} />

                      <Route path="*" element={<NotFound />} />
                    </Route>
                  </Routes>
                </BrowserRouter>
              </NamespaceProvider>
            </ClusterProvider>
          </HealthProvider>
        </NotificationProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
