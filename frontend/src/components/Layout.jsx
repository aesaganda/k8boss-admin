/**
 * Layout — the shell every page plugs into.
 *
 * Masthead (cluster selector, namespace selector, theme toggle, read-only
 * badge) + sidebar + a Suspense'd `<Outlet/>`.
 *
 * The Suspense boundary wraps the outlet and nothing else. Putting it any
 * higher meant that navigating to a not-yet-downloaded page chunk blanked the
 * sidebar and the masthead for the length of the fetch — on a slow link that
 * reads as the app having crashed, and there is nothing left on screen to
 * navigate away with. The same reasoning puts an ErrorBoundary immediately
 * around the outlet: a page that throws must not take the navigation with it.
 */
import { Suspense, lazy, useMemo, useState } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Alert,
  Divider,
  Dropdown,
  DropdownItem,
  DropdownList,
  Label,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadLogo,
  MastheadMain,
  MastheadToggle,
  MenuSearch,
  MenuSearchInput,
  MenuToggle,
  Modal,
  ModalBody,
  ModalHeader,
  Page,
  PageSection,
  PageSidebar,
  PageSidebarBody,
  PageToggleButton,
  SearchInput,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
  Tooltip,
} from '@patternfly/react-core';
import BarsIcon from '@patternfly/react-icons/dist/esm/icons/bars-icon';
import ClusterIcon from '@patternfly/react-icons/dist/esm/icons/cluster-icon';
import CubeIcon from '@patternfly/react-icons/dist/esm/icons/cube-icon';
import MoonIcon from '@patternfly/react-icons/dist/esm/icons/moon-icon';
import PlusIcon from '@patternfly/react-icons/dist/esm/icons/plus-icon';
import SunIcon from '@patternfly/react-icons/dist/esm/icons/sun-icon';
import TerminalIcon from '@patternfly/react-icons/dist/esm/icons/terminal-icon';
import LockIcon from '@patternfly/react-icons/dist/esm/icons/lock-icon';
import SignOutAltIcon from '@patternfly/react-icons/dist/esm/icons/sign-out-alt-icon';
import UserIcon from '@patternfly/react-icons/dist/esm/icons/user-icon';
import AppNav from './AppNav';
import BrandMark from './BrandMark';
import ErrorBoundary from './ErrorBoundary';
import ImportYamlDialog from './ImportYamlDialog';
import { LoadingState } from './ui';
import { wireGroup } from '../api/client';
import { useCluster } from '../contexts/ClusterContext';
import { useHealth } from '../contexts/HealthContext';
import { useNamespace } from '../contexts/NamespaceContext';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';

/**
 * §15's panel, and the reason it is `lazy` rather than a plain import.
 *
 * It pulls in `PodTerminal`, which pulls in xterm.js and its stylesheet. Every
 * other user of that component sits behind a route-level lazy boundary, so xterm
 * has never been in the initial bundle; importing it here — in the shell that
 * renders on every page — would put it there for every operator who never opens
 * a terminal. The chunk only exists as a separate file in a production build,
 * which is exactly why the Playwright suite runs against `vite preview` rather
 * than the dev server.
 */
const CliPanel = lazy(() => import('./CliPanel'));

const STATUS_COLOR = {
  connected: 'var(--pf-t--global--icon--color--status--success--default, #3e8635)',
  unreachable: 'var(--pf-t--global--icon--color--status--danger--default, #c9190b)',
  disconnected: 'var(--pf-t--global--icon--color--status--danger--default, #c9190b)',
};

// Module level, not an inline arrow in the JSX below. An inline
// `component={(props) => <Link .../>}` is a new component type on every render,
// so PatternFly remounts the brand each time and a click that starts before the
// remount lands on a detached node — the logo intermittently does nothing.
function BrandLink(props) {
  return <Link to="/" {...props} />;
}

function StatusDot({ status }) {
  return (
    <span
      aria-hidden="true"
      className="admin-status-dot"
      style={{
        // Anything we do not recognise is amber, not green: an unknown cluster
        // state must not read as a healthy one from across the room.
        background: STATUS_COLOR[status] || 'var(--pf-t--global--icon--color--status--warning--default, #f0ab00)',
      }}
    />
  );
}

function ClusterSelector() {
  const { clusters, activeCluster, activeClusterId, setActiveClusterId, loading, error } = useCluster();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();

  let toggleText = 'No cluster selected';
  if (activeCluster) toggleText = activeCluster.name;
  else if (loading) toggleText = 'Loading clusters…';
  else if (error) toggleText = 'Cluster list unavailable';

  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      onSelect={() => setOpen(false)}
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          onClick={() => setOpen((v) => !v)}
          isExpanded={open}
          icon={<ClusterIcon />}
          data-testid="cluster-selector"
        >
          <StatusDot status={activeCluster?.status} />
          {toggleText}
        </MenuToggle>
      )}
    >
      <DropdownList>
        {error && (
          // The list below is whatever we last managed to fetch. Saying so is
          // the difference between "this cluster is gone" and "we could not ask".
          <DropdownItem isDisabled description={error.message}>
            Cluster list could not be refreshed — showing last known
          </DropdownItem>
        )}
        {clusters.length === 0 && !loading && (
          <DropdownItem isDisabled>No clusters registered</DropdownItem>
        )}
        {clusters.map((cluster) => (
          <DropdownItem
            key={cluster.id}
            isSelected={cluster.id === activeClusterId}
            description={cluster.server_version || cluster.api_server}
            onClick={() => setActiveClusterId(cluster.id)}
          >
            <StatusDot status={cluster.status} />
            {cluster.name}
          </DropdownItem>
        ))}
        <Divider component="li" />
        <DropdownItem onClick={() => navigate('/clusters')}>Manage clusters</DropdownItem>
      </DropdownList>
    </Dropdown>
  );
}

function NamespaceSelector() {
  const { namespaces, selected, setSelected, loading, error, partial } = useNamespace();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');

  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return namespaces;
    return namespaces.filter((ns) => ns.name.toLowerCase().includes(q));
  }, [namespaces, filter]);

  let toggleText = selected || 'All namespaces';
  if (!selected && loading) toggleText = 'Loading namespaces…';

  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      onSelect={() => setOpen(false)}
      isScrollable
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          onClick={() => setOpen((v) => !v)}
          isExpanded={open}
          icon={<CubeIcon />}
          data-testid="namespace-selector"
        >
          {toggleText}
        </MenuToggle>
      )}
    >
      <MenuSearch>
        <MenuSearchInput>
          <SearchInput
            value={filter}
            placeholder="Find a namespace"
            aria-label="Find a namespace"
            onChange={(_event, next) => setFilter(next)}
            onClear={() => setFilter('')}
          />
        </MenuSearchInput>
      </MenuSearch>
      <Divider component="li" />
      <DropdownList>
        <DropdownItem isSelected={!selected} onClick={() => setSelected(null)}>
          All namespaces
        </DropdownItem>
        {error && (
          <DropdownItem isDisabled description={error.message}>
            Namespaces could not be listed
          </DropdownItem>
        )}
        {partial && (
          // §1.2: a short list may be a short cluster or a short permission.
          // The selector has to say which, or an operator concludes a namespace
          // does not exist when they simply cannot see it.
          <DropdownItem isDisabled>
            Some namespaces could not be read — this list is incomplete
          </DropdownItem>
        )}
        {!error && matches.length === 0 && !loading && (
          <DropdownItem isDisabled>{filter ? 'No namespace matches' : 'No namespaces'}</DropdownItem>
        )}
        {matches.map((ns) => (
          <DropdownItem key={ns.name} isSelected={ns.name === selected} onClick={() => setSelected(ns.name)}>
            {ns.name}
          </DropdownItem>
        ))}
      </DropdownList>
    </Dropdown>
  );
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  return (
    <Tooltip content={`Switch to ${next} theme`}>
      <MenuToggle
        variant="plain"
        aria-label={`Switch to ${next} theme`}
        onClick={() => setTheme(next)}
        data-testid="theme-toggle"
      >
        {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
      </MenuToggle>
    </Tooltip>
  );
}

/**
 * The masthead's "+" — the entry point into `ImportYamlDialog`.
 *
 * Gated on a selected cluster, since every write in this console is
 * cluster-scoped (§1.1) and a click with none active would only produce a
 * `409 no_cluster_selected` after a wasted catalog fetch. Disabled with the
 * reason rather than hidden (rule 11.4) — an operator who has not yet chosen a
 * cluster should still see that importing a manifest is something this console
 * can do.
 */
function ImportYamlButton() {
  const { activeClusterId } = useCluster();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const disabledReason = activeClusterId == null ? 'Select a cluster before importing an object' : null;

  return (
    <>
      <Tooltip content={disabledReason || 'Import YAML'}>
        <MenuToggle
          variant="plain"
          aria-label="Import YAML"
          isAriaDisabled={Boolean(disabledReason)}
          onClick={disabledReason ? undefined : () => setOpen(true)}
          data-testid="import-yaml-button"
        >
          <PlusIcon />
        </MenuToggle>
      </Tooltip>
      {open && (
        <ImportYamlDialog
          isOpen
          onClose={() => setOpen(false)}
          onApplied={(result) => {
            setOpen(false);
            // Land on the object that was just created, the way Explorer's own
            // catalog click does — reusing its `?name=&namespace=` convention
            // rather than inventing a second one.
            const target = result?.target;
            if (!target?.resource) return;
            const qs = new URLSearchParams();
            if (target.name) qs.set('name', target.name);
            if (target.namespace) qs.set('namespace', target.namespace);
            const suffix = qs.toString() ? `?${qs.toString()}` : '';
            navigate(
              `/explorer/${encodeURIComponent(wireGroup(target.group))}/${encodeURIComponent(target.version)}/${encodeURIComponent(target.resource)}${suffix}`,
            );
          }}
        />
      )}
    </>
  );
}

/**
 * The masthead's terminal — the entry point into §15's `CliPanel`.
 *
 * Gated on a selected cluster, like the "+" beside it: every CLI pod is created
 * in one cluster (§1.1), and a click with none active would only produce a
 * `409 no_cluster_selected`. Disabled with the reason rather than hidden (rule
 * 11.4).
 *
 * The panel is mounted only while the modal is open, so no `/api/cli` read
 * happens for operators who never press this, and the xterm chunk is not
 * fetched until then either.
 */
function CliButton() {
  const { activeClusterId } = useCluster();
  const [open, setOpen] = useState(false);
  const disabledReason = activeClusterId == null ? 'Select a cluster before opening a terminal' : null;

  return (
    <>
      <Tooltip content={disabledReason || 'Open a CLI session (kubectl)'}>
        <MenuToggle
          variant="plain"
          aria-label="Open a CLI session"
          isAriaDisabled={Boolean(disabledReason)}
          onClick={disabledReason ? undefined : () => setOpen(true)}
          data-testid="cli-button"
        >
          <TerminalIcon />
        </MenuToggle>
      </Tooltip>
      {open && (
        <Modal
          isOpen
          variant="large"
          onClose={() => setOpen(false)}
          aria-label="CLI session"
          data-testid="cli-modal"
        >
          <ModalHeader title="CLI session" />
          <ModalBody>
            <Suspense fallback={<LoadingState label="Loading the terminal…" minHeight={280} />}>
              <CliPanel />
            </Suspense>
          </ModalBody>
        </Modal>
      )}
    </>
  );
}

function ReadOnlyBadge() {
  const { readOnly, reason } = useHealth();
  if (!readOnly) return null;
  return (
    // The span is not decoration: Tooltip attaches a ref to its child, and
    // wrapping guarantees a real DOM node to attach to regardless of whether the
    // PatternFly component forwards one. `tabIndex` makes the explanation
    // reachable without a mouse.
    <Tooltip content={reason}>
      <span tabIndex={0} className="admin-status-badge" data-testid="read-only-badge">
        <Label color="orange" icon={<LockIcon />}>
          Read-only
        </Label>
      </span>
    </Tooltip>
  );
}

function UserMenu() {
  const { enabled, user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  if (!enabled || !user) return null;

  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      onSelect={() => setOpen(false)}
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          variant="plainText"
          icon={<UserIcon />}
          onClick={() => setOpen((value) => !value)}
          isExpanded={open}
          aria-label="User menu"
        >
          {user.display_name || user.username}
        </MenuToggle>
      )}
    >
      <DropdownList>
        <DropdownItem isDisabled description={user.role === 'admin' ? 'Administrator' : 'User'}>
          {user.username}
        </DropdownItem>
        <Divider component="li" />
        <DropdownItem icon={<SignOutAltIcon />} onClick={logout}>Sign out</DropdownItem>
      </DropdownList>
    </Dropdown>
  );
}

function AppMasthead() {
  return (
    <Masthead>
      <MastheadMain>
        <MastheadToggle>
          <PageToggleButton variant="plain" aria-label="Toggle navigation">
            <BarsIcon />
          </PageToggleButton>
        </MastheadToggle>
        <MastheadBrand>
          <MastheadLogo component={BrandLink}>
            <span className="admin-brand">
              <BrandMark className="admin-brand__mark" />
              {/* Two adjacent text nodes, not "k8boss" + " " + "admin": the
                  e2e smoke test does a substring match on "k8boss-admin", and
                  a space or extra element in between would break it while
                  looking identical on screen. */}
              <span className="admin-brand__text">
                <span className="admin-brand__name">k8boss</span>
                <span className="admin-brand__suffix">-admin</span>
              </span>
            </span>
          </MastheadLogo>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar isFullHeight isStatic>
          <ToolbarContent>
            <ToolbarGroup variant="filter-group">
              <ToolbarItem>
                <ClusterSelector />
              </ToolbarItem>
              <ToolbarItem>
                <NamespaceSelector />
              </ToolbarItem>
            </ToolbarGroup>
            <ToolbarGroup align={{ default: 'alignEnd' }}>
              <ToolbarItem>
                <ReadOnlyBadge />
              </ToolbarItem>
              <ToolbarItem>
                <CliButton />
              </ToolbarItem>
              <ToolbarItem>
                <ImportYamlButton />
              </ToolbarItem>
              <ToolbarItem>
                <UserMenu />
              </ToolbarItem>
              <ToolbarItem>
                <ThemeToggle />
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );
}

/**
 * Contract §11.5. Inline and persistent, not a toast: the fact that this
 * deployment cannot write is true for the whole session, and it is the
 * explanation for every greyed-out button on every page below it.
 */
function ReadOnlyBanner() {
  const { readOnly, reason } = useHealth();
  if (!readOnly) return null;
  return (
    <Alert
      isInline
      variant="info"
      title="This console is running read-only"
      className="admin-readonly-banner"
      data-testid="read-only-banner"
    >
      {reason} Write actions are disabled; you can still dry-run any change to see the diff it
      would produce.
    </Alert>
  );
}

export default function Layout() {
  const { activeClusterId } = useCluster();
  const { pathname } = useLocation();

  return (
    <Page
      masthead={<AppMasthead />}
      sidebar={
        <PageSidebar>
          <PageSidebarBody isFilled>
            <AppNav />
          </PageSidebarBody>
        </PageSidebar>
      }
      isManagedSidebar
    >
      <ReadOnlyBanner />
      {/* Keyed on the active cluster so switching clusters remounts the page
          rather than leaving one cluster's rows on screen under another
          cluster's name while a refetch is in flight. It also discards any
          half-edited YAML, which is the point: that YAML was written against
          the other cluster. */}
      <PageSection isFilled key={activeClusterId ?? 'no-cluster'} className="admin-content">
        <ErrorBoundary resetKey={pathname}>
          <Suspense fallback={<LoadingState label="Loading page…" minHeight={280} />}>
            <Outlet />
          </Suspense>
        </ErrorBoundary>
      </PageSection>
    </Page>
  );
}
