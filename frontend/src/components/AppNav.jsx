/**
 * AppNav — the sidebar.
 *
 * Grouped the way an operator thinks about a cluster rather than the way the
 * API groups it: what runs on it, what it exposes, what it stores, who can do
 * what to it. Cluster and namespace are global context and live in the masthead
 * instead, so they never appear twice with two answers.
 *
 * `RouterNavItem` navigates through the router rather than letting the anchor
 * do a full page load. The inline-arrow `component` prop is deliberately not
 * used: it mints a new component type on every render, so React remounts the
 * link mid-click and the navigation silently never fires — a bug that presents
 * as "the sidebar sometimes does nothing".
 */
import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Nav, NavExpandable, NavItem, NavList } from '@patternfly/react-core';
import { useAuth } from '../contexts/AuthContext';

function RouterNavItem({ to, end = false, children }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const isActive = end ? pathname === to : pathname === to || pathname.startsWith(`${to}/`);
  return (
    <NavItem
      itemId={to}
      to={to}
      isActive={isActive}
      onClick={(event) => {
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </NavItem>
  );
}

// Module level, not defined inside AppNav: a component declared during render
// is a new type each time, and every section would remount — collapsing itself
// on every keystroke anywhere in the app.
function NavSection({ id, title, routes, children }) {
  const { pathname } = useLocation();
  const [expanded, setExpanded] = useState(() => {
    try {
      const stored = localStorage.getItem(`k8boss-admin.nav.${id}`);
      return stored === 'expanded';
    } catch {
      return false;
    }
  });
  const containsActive = routes.some((route) => pathname === route || pathname.startsWith(`${route}/`));

  // Arriving in a section opens it, so a deep link or a reload shows where in
  // the tree the page is. It runs on the *transition* into the section rather
  // than on every render, which is what leaves the header free to close a
  // section the operator is standing in: `isExpanded={expanded || containsActive}`
  // recomputed that on every render instead, so the one section you were most
  // likely to want out of the way was the one that sprang open again.
  useEffect(() => {
    if (containsActive) setExpanded(true);
  }, [containsActive]);

  return (
    <NavExpandable
      title={title}
      groupId={id}
      isActive={containsActive}
      isExpanded={expanded}
      // The header is a toggle and nothing else. It used to navigate to the
      // section's first page as well, which made the click that folds a
      // section away also a click that left the page you were reading.
      onExpand={(_event, value) => {
        setExpanded(value);
        try {
          localStorage.setItem(`k8boss-admin.nav.${id}`, value ? 'expanded' : 'collapsed');
        } catch {
          /* the section still toggles for this session */
        }
      }}
    >
      {children}
    </NavExpandable>
  );
}

export default function AppNav() {
  const { enabled, user } = useAuth();
  const canManageUsers = enabled && user?.role === 'admin';
  const administrationRoutes = ['/clusters', '/audit', ...(canManageUsers ? ['/users'] : [])];

  return (
    <Nav aria-label="Console navigation" className="admin-nav">
      <NavList>
        <RouterNavItem to="/" end>
          Overview
        </RouterNavItem>

        <NavSection
          id="cluster"
          title="Cluster"
          routes={['/cluster-status', '/nodes', '/disruption', '/namespaces', '/events']}
        >
          {/* §19. First in the section and above Nodes: it is the page that
              answers "is the control plane itself all right", and the honest
              order is to establish that before reading anything the control
              plane told us about the nodes. */}
          <RouterNavItem to="/cluster-status">Status</RouterNavItem>
          <RouterNavItem to="/nodes">Nodes</RouterNavItem>
          {/* §28. Directly after Nodes, because the question it answers —
              can these pods actually be evicted — is the one asked between
              picking a node and draining it. */}
          <RouterNavItem to="/disruption">Disruption</RouterNavItem>
          <RouterNavItem to="/namespaces">Namespaces</RouterNavItem>
          <RouterNavItem to="/events">Events</RouterNavItem>
        </NavSection>

        <NavSection id="workloads" title="Workloads" routes={['/workloads', '/pods']}>
          <RouterNavItem to="/workloads">Workloads</RouterNavItem>
          <RouterNavItem to="/pods">Pods</RouterNavItem>
        </NavSection>

        {/* Storage, Network, Configuration and Access control are each one
            `ResourceTabsPage`, and their listings are links here rather than
            tabs inside the page: a row of tabs above a table is a selector
            nobody can send a link to, and a dozen of them is a selector nobody
            can read either. The bare paths (`/network`, `/storage`, …) still
            resolve — they redirect to the first listing — so older links and
            bookmarks keep working. */}
        <NavSection id="storage" title="Storage" routes={['/storage']}>
          <RouterNavItem to="/storage/pvcs">PersistentVolumeClaims</RouterNavItem>
          <RouterNavItem to="/storage/pvs">PersistentVolumes</RouterNavItem>
          <RouterNavItem to="/storage/storageclasses">StorageClasses</RouterNavItem>
          <RouterNavItem to="/storage/volumesnapshots">Volume Snapshots</RouterNavItem>
          <RouterNavItem to="/storage/volumesnapshotclasses">Snapshot Classes</RouterNavItem>
          <RouterNavItem to="/storage/volumeattributesclasses">Volume Attributes Classes</RouterNavItem>
        </NavSection>
        <NavSection
          id="network"
          title="Network"
          routes={['/network', '/routes', '/gateway']}
        >
          <RouterNavItem to="/network/services">Services</RouterNavItem>
          <RouterNavItem to="/network/ingresses">Ingresses</RouterNavItem>
          <RouterNavItem to="/network/endpoints">Endpoints</RouterNavItem>
          <RouterNavItem to="/network/endpointslices">Endpoint Slices</RouterNavItem>
          <RouterNavItem to="/network/ingressclasses">Ingress Classes</RouterNavItem>
          <RouterNavItem to="/network/networkpolicies">Network Policies</RouterNavItem>
          <RouterNavItem to="/network/isolation">Pod Isolation</RouterNavItem>
          <RouterNavItem to="/routes">Routes</RouterNavItem>
          {/* Gateway API resources are CRD-backed and often absent. */}
          <RouterNavItem to="/gateway">Gateway (beta)</RouterNavItem>
        </NavSection>
        <NavSection id="config" title="Configuration" routes={['/config']}>
          <RouterNavItem to="/config/configmaps">ConfigMaps</RouterNavItem>
          <RouterNavItem to="/config/secrets">Secrets</RouterNavItem>
          <RouterNavItem to="/config/hpas">HPAs</RouterNavItem>
          <RouterNavItem to="/config/vpas">VPAs</RouterNavItem>
          <RouterNavItem to="/config/poddisruptionbudgets">Pod Disruption Budgets</RouterNavItem>
          <RouterNavItem to="/config/resourcequotas">Resource Quotas</RouterNavItem>
          <RouterNavItem to="/config/limitranges">Limit Ranges</RouterNavItem>
          <RouterNavItem to="/config/priorityclasses">Priority Classes</RouterNavItem>
          <RouterNavItem to="/config/runtimeclasses">Runtime Classes</RouterNavItem>
          <RouterNavItem to="/config/leases">Leases</RouterNavItem>
          <RouterNavItem to="/config/mutatingwebhookconfigurations">Mutating Webhook Configurations</RouterNavItem>
          <RouterNavItem to="/config/validatingwebhookconfigurations">
            Validating Webhook Configurations
          </RouterNavItem>
        </NavSection>

        <NavSection id="access" title="Access control" routes={['/access']}>
          <RouterNavItem to="/access/serviceaccounts">ServiceAccounts</RouterNavItem>
          <RouterNavItem to="/access/roles">Roles</RouterNavItem>
          <RouterNavItem to="/access/clusterroles">ClusterRoles</RouterNavItem>
          <RouterNavItem to="/access/rolebindings">RoleBindings</RouterNavItem>
          <RouterNavItem to="/access/clusterrolebindings">ClusterRoleBindings</RouterNavItem>
          <RouterNavItem to="/access/csrs">Certificate requests</RouterNavItem>
          <RouterNavItem to="/access/review">Access review</RouterNavItem>
        </NavSection>

        {/* §16. Above Custom Resources, not inside it: an operator is the
            thing that installs the CRDs that section browses, so the order on
            screen is the order of the work — install the operator, then look at
            what it added. It is also a write surface, and the section below it
            is a browser. */}
        <RouterNavItem to="/portal">Operator portal</RouterNavItem>

        <NavSection id="explorer" title="Custom Resources" routes={['/explorer', '/custom-resources']}>
          <RouterNavItem to="/custom-resources">Instances</RouterNavItem>
          <RouterNavItem to="/explorer">API explorer</RouterNavItem>
        </NavSection>

        <NavSection id="administration" title="Administration" routes={administrationRoutes}>
          <RouterNavItem to="/clusters">Clusters</RouterNavItem>
          <RouterNavItem to="/audit">Audit log</RouterNavItem>
          {canManageUsers && <RouterNavItem to="/users">Users</RouterNavItem>}
        </NavSection>
      </NavList>
    </Nav>
  );
}
