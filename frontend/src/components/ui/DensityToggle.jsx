/**
 * DensityToggle — the Comfy/Compact segmented control above a table.
 *
 *   const { density, setDensity } = useDensity();
 *   <DensityToggle value={density} onChange={setDensity} />
 *
 * Controlled, and it reads no context of its own: everything in
 * `components/ui/` is a pure design-system piece that renders what it is
 * handed, and a component that reached into `DensityContext` could not be
 * rendered by a page that wanted a table density the operator had not chosen.
 * The pages wire it to the shared preference; see `contexts/DensityContext.jsx`.
 *
 * A segmented control rather than the radio cards Teams uses in its settings
 * panel: this one lives in the toolbar of the table it changes, where the
 * result of a click is visible the instant it happens, and a card grid of
 * miniature table drawings would be larger than the toolbar it sits in. The
 * naming is Teams' own, because that is the vocabulary the request arrived in.
 */
import { ToggleGroup, ToggleGroupItem, Tooltip } from '@patternfly/react-core';

const OPTIONS = [
  { value: 'comfy', label: 'Comfy' },
  { value: 'compact', label: 'Compact' },
];

export function DensityToggle({
  value = 'comfy',
  onChange,
  ariaLabel = 'Row density',
  tooltip = 'Row density. Compact fits every row on one line and tightens the spacing; Comfy lets a long name, status or image wrap onto as many lines as it needs.',
}) {
  const group = (
    <ToggleGroup aria-label={ariaLabel} data-testid="density-toggle">
      {OPTIONS.map((option) => (
        <ToggleGroupItem
          key={option.value}
          text={option.label}
          // The id lands on the button; `data-testid` would land on the wrapper
          // PatternFly puts around it, where `aria-pressed` — the only thing
          // that says which density is selected — is not.
          buttonId={`density-${option.value}`}
          isSelected={value === option.value}
          // PatternFly fires this for a click on the already-selected item too,
          // and re-setting the same density would write localStorage and
          // re-render every row for nothing.
          onChange={() => value !== option.value && onChange?.(option.value)}
        />
      ))}
    </ToggleGroup>
  );

  // The span is not decoration: Tooltip attaches a ref to its child, and the
  // group is a PatternFly component that may not forward one.
  return (
    <Tooltip content={tooltip}>
      <span>{group}</span>
    </Tooltip>
  );
}

export default DensityToggle;
