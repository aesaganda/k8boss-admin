/**
 * BrandMark — the masthead logomark.
 *
 * A flat hexagon (a nod to Kubernetes' hexagonal badge shape, not its
 * trademarked helm-wheel glyph) with the product's numeral picked out in
 * white. Colors are literal, not `--pf-t--*` tokens: a brand mark keeps one
 * identity across the light/dark toggle, the same way a logo does not
 * repaint itself when a site's theme flips.
 *
 * Kept in sync by hand with `public/favicon.svg` — same geometry, same
 * colors — rather than generating one from the other. Two ten-line SVGs are
 * cheaper than a build step that turns a React component into a static
 * favicon file for a mark that changes maybe once.
 */
export default function BrandMark({ className, size = 28 }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M16 1 L29 8.5 L29 23.5 L16 31 L3 23.5 L3 8.5 Z" fill="#06c" />
      <text
        x="16"
        y="16"
        textAnchor="middle"
        dominantBaseline="central"
        fontFamily="system-ui, -apple-system, 'Segoe UI', sans-serif"
        fontWeight="800"
        fontSize="17"
        fill="#fff"
      >
        8
      </text>
    </svg>
  );
}
