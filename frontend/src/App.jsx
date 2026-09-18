/**
 * App — providers, router, routes.
 *
 * Pages are code-split. Each becomes its own chunk fetched on first
 * navigation, so the initial load ships the shell and nothing else; the
 * Suspense boundary that covers the fetch lives in Layout, around `<Outlet/>`
 * only, so the sidebar stays usable while a page downloads.
 */
import { lazy } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import ErrorBoundary from './components/ErrorBoundary';
import Layout from './components/Layout';
import { EmptyState, ErrorState, LoadingState } from './components/ui';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { ClusterProvider } from './contexts/ClusterContext';
import { DensityProvider } from './contexts/DensityContext';
import { HealthProvider } from './contexts/HealthContext';
import { NamespaceProvider } from './contexts/NamespaceContext';
import { NotificationProvider } from './contexts/NotificationContext';
import { ThemeProvider } from './contexts/ThemeContext';
import Login from './pages/Login';

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
const NamespaceDetail = lazyPage('NamespaceDetail', () => import('./pages/NamespaceDetail'));
const Workloads = lazyPage('Workloads', () => import('./pages/Workloads'));
const WorkloadDetail = lazyPage('WorkloadDetail', () => import('./pages/WorkloadDetail'));
const Pods = lazyPage('Pods', () => import('./pages/Pods'));
const PodDetail = lazyPage('PodDetail', () => import('./pages/PodDetail'));
const Network = lazyPage('Network', () => import('./pages/Network'));
const RoutesPage = lazyPage('Routes', () => import('./pages/Routes'));
const Config = lazyPage('Config', () => import('./pages/Config'));
const Storage = lazyPage('Storage', () => import('./pages/Storage'));
const Gateway = lazyPage('Gateway', () => import('./pages/Gateway'));
const Access = lazyPage('Access', () => import('./pages/Access'));
const Events = lazyPage('Events', () => import('./pages/Events'));
const Explorer = lazyPage('Explorer', () => import('./pages/Explorer'));
const CustomResources = lazyPage('CustomResources', () => import('./pages/CustomResources'));
const Portal = lazyPage('Operator portal', () => import('./pages/Portal'));
const ClusterStatus = lazyPage('Cluster status', () => import('./pages/ClusterStatus'));
const Disruption = lazyPage('Disruption budgets', () => import('./pages/Disruption'));
const Clusters = lazyPage('Clusters', () => import('./pages/Clusters'));
const Audit = lazyPage('Audit', () => import('./pages/Audit'));
const Users = lazyPage('Users', () => import('./pages/Users'));
const Sessions = lazyPage('Sessions', () => import('./pages/Sessions'));
const IdentityProviders = lazyPage('Identity providers', () => import('./pages/IdentityProviders'));

function NotFound() {
  return (
    <EmptyState
      title="No such page"
      description="That URL does not match any view in this console. Use the navigation on the left."
    />
  );
}

function ConsoleRoutes() {
  return (
    <HealthProvider>
      <ClusterProvider>
        <NamespaceProvider>
          <Routes>
            <Route path="/" element={<Layout />}>
              <Route index element={<Overview />} />

              <Route path="nodes" element={<Nodes />} />
              <Route path="nodes/:name" element={<NodeDetail />} />

              <Route path="namespaces" element={<Namespaces />} />
              {/* §17. One namespace with what governs it — quota usage, limit
                  ranges, Pod Security level, role bindings, network policies.
                  Not a Workloads page filtered by namespace: that list has one
                  home, and this page shows the objects no other page does. */}
              <Route path="namespaces/:name" element={<NamespaceDetail />} />

              <Route path="workloads" element={<Workloads />} />
              {/* The same page with its kind filter in the path, so the
                  sidebar's Jobs entry — and any filtered list somebody sends
                  round during an incident — is a URL rather than a click. */}
              <Route path="workloads/:plural" element={<Workloads />} />
              <Route
                path="workloads/:plural/:namespace/:name"
                element={<WorkloadDetail />}
              />

              <Route path="pods" element={<Pods />} />
              {/* §7.5. The pod's own page: details, metrics, YAML,
                  environment, logs, events, terminal and §7.4's debug, with the
                  active one in `?tab=` so every tab is a link. */}
              <Route path="pods/:namespace/:name" element={<PodDetail />} />
              {/* The four `ResourceTabsPage` pages put their sections in the
                  sidebar rather than in a tab strip, so each section is a URL
                  anybody can send. The bare path is what the sidebar's own
                  section header navigates to and what every older link says:
                  it redirects to the first section rather than serving it
                  under a second path. */}
              <Route path="network" element={<Navigate to="/network/services" replace />} />
              <Route path="network/:section" element={<Network />} />
              {/* §13/§14. "routes" is the feature, not react-router's
                  <Route> — the page component is aliased to RoutesPage so
                  the two names cannot be confused at the point of use. */}
              <Route path="routes" element={<RoutesPage />} />
              <Route path="config" element={<Navigate to="/config/configmaps" replace />} />
              <Route path="config/:section" element={<Config />} />
              <Route path="storage" element={<Navigate to="/storage/pvcs" replace />} />
              <Route path="storage/:section" element={<Storage />} />
              <Route path="gateway" element={<Gateway />} />
              <Route path="access" element={<Navigate to="/access/serviceaccounts" replace />} />
              <Route path="access/:section" element={<Access />} />
              <Route path="events" element={<Events />} />

              {/* One page, two routes: the explorer with no resource chosen is
                  the catalog, and the same page renders a selected listing. */}
              <Route path="explorer" element={<Explorer />} />
              <Route path="explorer/:group/:version/:plural" element={<Explorer />} />

              {/* Custom Resources: the same discovery catalog as the explorer,
                  grouped by API group and filtered down to the ones that are
                  not built into Kubernetes — a curated view for "what CRDs are
                  installed", where the explorer stays the raw, everything
                  browser. */}
              <Route path="custom-resources" element={<CustomResources />} />

              {/* §16. The operator portal sits beside Custom Resources rather
                  than inside it: subscribing is what *installs* the CRDs that
                  page browses, and it is a write, not a browser. */}
              <Route path="portal" element={<Portal />} />

              {/* §19. The control plane's own health. Not under "clusters",
                  which is the registry of clusters this console can reach —
                  this page is about the one it is pointed at right now. */}
              <Route path="cluster-status" element={<ClusterStatus />} />

              {/* §28. In the Cluster section rather than under
                  Configuration: the question it answers is asked before a
                  node drain or an upgrade, which is a cluster operation. */}
              <Route path="disruption" element={<Disruption />} />

              <Route path="clusters" element={<Clusters />} />
              <Route path="audit" element={<Audit />} />
              <Route path="users" element={<Users />} />
              {/* §12.7. Its own URL rather than a tab on Users: the accounts
                  that exist and the sessions that are live are two different
                  questions, and the second one gets asked in a hurry. */}
              <Route path="sessions" element={<Sessions />} />
              {/* §12.8, ADR-0011. Beside Users: the providers are where the
                  accounts on that page come from. */}
              <Route path="identity-providers" element={<IdentityProviders />} />
              <Route path="login" element={<Navigate to="/" replace />} />

              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </NamespaceProvider>
      </ClusterProvider>
    </HealthProvider>
  );
}

function AuthenticationGate() {
  const { enabled, user, loading, error, refresh } = useAuth();
  if (loading) {
    return <LoadingState label="Checking your session…" minHeight="100vh" />;
  }
  if (error) {
    return (
      <div className="admin-auth-state">
        <ErrorState title="Authentication service unavailable" error={error} onRetry={refresh} />
      </div>
    );
  }
  if (enabled && !user) return <Login />;
  return <ConsoleRoutes />;
}

export default function App() {
  return (
    // Outermost boundary, outside every provider, so a provider startup failure
    // still leaves a usable error panel rather than an empty root.
    <ErrorBoundary title="The console failed to start">
      <ThemeProvider>
        {/* Alongside the theme rather than inside the router: both are console
            preferences that belong to the operator, not to a route, and a
            provider under the router would reset the choice on navigation. */}
        <DensityProvider>
          <NotificationProvider>
            <BrowserRouter>
              <AuthProvider>
                <AuthenticationGate />
              </AuthProvider>
            </BrowserRouter>
          </NotificationProvider>
        </DensityProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
