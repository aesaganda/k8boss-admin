/**
 * PageHeader / SectionHeader.
 *
 * Title and subtitle on the left, actions on the right, breadcrumbs above.
 * Every page uses the same one so the action strip lands in the same place on
 * every screen — the mutating buttons in this console live there, and an
 * operator should never have to hunt for where "Delete" is on this page.
 */
import { Breadcrumb, BreadcrumbItem, Flex, FlexItem, Title } from '@patternfly/react-core';
import { Link } from 'react-router-dom';

export function PageHeader({ title, subtitle, actions, breadcrumbs, badge, className }) {
  return (
    <div className={`admin-page-header ${className || ''}`.trim()}>
      {Array.isArray(breadcrumbs) && breadcrumbs.length > 0 && (
        <Breadcrumb className="admin-page-header__crumbs">
          {breadcrumbs.map((crumb, i) => {
            const isLast = i === breadcrumbs.length - 1;
            return (
              <BreadcrumbItem key={`${crumb.label}-${i}`} isActive={isLast}>
                {crumb.to && !isLast ? <Link to={crumb.to}>{crumb.label}</Link> : crumb.label}
              </BreadcrumbItem>
            );
          })}
        </Breadcrumb>
      )}
      <Flex
        justifyContent={{ default: 'justifyContentSpaceBetween' }}
        alignItems={{ default: 'alignItemsFlexStart' }}
        flexWrap={{ default: 'wrap' }}
        gap={{ default: 'gapMd' }}
      >
        <FlexItem>
          <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }}>
            <FlexItem>
              <Title headingLevel="h1" size="2xl">
                {title}
              </Title>
            </FlexItem>
            {badge && <FlexItem>{badge}</FlexItem>}
          </Flex>
          {subtitle && <p className="admin-page-header__subtitle">{subtitle}</p>}
        </FlexItem>
        {actions && (
          <FlexItem>
            <Flex gap={{ default: 'gapSm' }} flexWrap={{ default: 'wrap' }}>
              {Array.isArray(actions)
                ? actions.map((action, i) => <FlexItem key={i}>{action}</FlexItem>)
                : <FlexItem>{actions}</FlexItem>}
            </Flex>
          </FlexItem>
        )}
      </Flex>
    </div>
  );
}

export function SectionHeader({ title, description, actions, headingLevel = 'h2', className }) {
  return (
    <div className={`admin-section-header ${className || ''}`.trim()}>
      <Flex
        justifyContent={{ default: 'justifyContentSpaceBetween' }}
        alignItems={{ default: 'alignItemsCenter' }}
        gap={{ default: 'gapSm' }}
      >
        <FlexItem>
          <Title headingLevel={headingLevel} size="lg">
            {title}
          </Title>
          {description && <p className="admin-section-header__description">{description}</p>}
        </FlexItem>
        {actions && <FlexItem>{actions}</FlexItem>}
      </Flex>
    </div>
  );
}
