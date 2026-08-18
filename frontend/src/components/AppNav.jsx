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

        <NavSection id="platform" title="Platform" routes={['/network', '/config', '/storage']}>
          <RouterNavItem to="/network">Networking</RouterNavItem>
          <RouterNavItem to="/config">Configuration</RouterNavItem>
          <RouterNavItem to="/storage">Storage</RouterNavItem>
        </NavSection>

        <NavSection id="access" title="Access control" routes={['/access']}>
          <RouterNavItem to="/access">Roles and bindings</RouterNavItem>
        </NavSection>

        <RouterNavItem to="/explorer">API explorer</RouterNavItem>

        <NavSection id="administration" title="Administration" routes={['/clusters', '/audit']}>
          <RouterNavItem to="/clusters">Clusters</RouterNavItem>
          <RouterNavItem to="/audit">Audit log</RouterNavItem>
        </NavSection>
      </NavList>
    </Nav>
  );
}
