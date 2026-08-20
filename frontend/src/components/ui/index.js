/*
 * The k8boss-admin design system — the whole surface, one import.
 *
 *   import { PageHeader, DataTable, NullableCell, PartialBanner } from '../components/ui';
 *
 * Two of these are not decoration and are not optional:
 *
 *   PartialBanner  renders contract rule 11.1 — a persistent inline banner
 *                  naming every `unavailable` entry. Every page that reads a
 *                  list envelope renders one above its data.
 *   NullableCell   renders rule 11.2 — a `null` number is an em dash with a
 *                  tooltip, never a zero. Every nullable number in this app
 *                  goes through it.
 *
 * They exist as components rather than as prose in a review checklist because a
 * checklist does not fail a build.
 */
export { PageHeader, SectionHeader } from './PageHeader';
export { DataTable } from './DataTable';
export { DensityToggle } from './DensityToggle';
export { EmptyState, ErrorState, LoadingState, Skeleton } from './states';
export { StatusBadge } from './StatusBadge';
export { MetricCard, StatGrid } from './MetricCard';
export { DescriptionList, InfoGrid } from './DescriptionList';
export { SearchInput, FilterBar, Toolbar } from './inputs';
export { CodeBlock } from './CodeBlock';
export { ConfirmDialog } from './ConfirmDialog';
export { PartialBanner } from './PartialBanner';
export { NullableCell, AgeCell, ResourceLink } from './cells';
