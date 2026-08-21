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
import { useState } from 'react';
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
      return localStorage.getItem(`k8boss-admin.nav.${id}`) !== 'collapsed';
    } catch {
      return true;
    }
  });
  const containsActive = routes.some((route) => pathname === route || pathname.startsWith(`${route}/`));
  return (
    <NavExpandable
      title={title}
      groupId={id}
      isActive={containsActive}
      // Auto-expand for the active route without overwriting the stored
      // preference, so collapsing a section does not fight the router.
      isExpanded={expanded || containsActive}
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
    <Nav aria-label="Console navigation">
      <NavList>
        <RouterNavItem to="/" end>
          Overview
        </RouterNavItem>

        <NavSection id="cluster" title="Cluster" routes={['/nodes', '/namespaces', '/events']}>
          <RouterNavItem to="/nodes">Nodes</RouterNavItem>
          <RouterNavItem to="/namespaces">Namespaces</RouterNavItem>
          <RouterNavItem to="/events">Events</RouterNavItem>
        </NavSection>

        <NavSection id="workloads" title="Workloads" routes={['/workloads', '/pods']}>
          <RouterNavItem to="/workloads">Workloads</RouterNavItem>
          <RouterNavItem to="/pods">Pods</RouterNavItem>
        </NavSection>

        {/* Storage, Network, Gateway and Configuration are each one page whose
            resource types are tabs inside it (`ResourceTabsPage`), not one
            route apiece — so each gets a single top-level link, the way "API
            explorer" always has, rather than an expandable section holding
            only itself. */}
        <RouterNavItem to="/storage">Storage</RouterNavItem>
        <RouterNavItem to="/network">Network</RouterNavItem>
        {/* Gateway API resources are CRD-backed and often absent — "(beta)"
            in the label is the same signal §1.2 gives every unsupported entry:
            a cluster with none of this installed is a normal cluster, not a
            broken console. */}
        <RouterNavItem to="/gateway">Gateway (beta)</RouterNavItem>
        <RouterNavItem to="/config">Configuration</RouterNavItem>

        <NavSection id="access" title="Access control" routes={['/access']}>
          <RouterNavItem to="/access">Roles and bindings</RouterNavItem>
        </NavSection>

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
