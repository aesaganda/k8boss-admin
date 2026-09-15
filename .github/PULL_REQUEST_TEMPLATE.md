## What changed, and why

<!--
The why matters more than the what here — see CONTRIBUTING.md. If this closes
a gap the safety model describes (an unaudited path, a swallowed error, a
missing preflight), name the failure mode explicitly.
-->

## Checklist

- [ ] I read [`CONTRIBUTING.md`](../CONTRIBUTING.md), and if this changes
      what the console installs or writes on its own, also
      [`docs/adr-0004-shipped-router.md`](../docs/adr-0004-shipped-router.md)
      and [`docs/adr-0008-shipped-olm.md`](../docs/adr-0008-shipped-olm.md)
- [ ] If this adds or changes a write: it goes through
      `backend/app/admin/mutate.py` (gate → preflight → apply → diff →
      audit) — no second path around the funnel
- [ ] If this adds or changes a read: a failed lookup lands in
      `unavailable[]` with `partial: true`, never a silently empty result;
      a number that could not be computed is `null`, never `0`
- [ ] Tests added or updated, and passing locally
      (`cd backend && python -m pytest -q`,
      `cd frontend && npx playwright test`)
- [ ] `npm run lint` passes — no per-line suppressions for a rule that's
      actually wrong; that gets fixed in `eslint.config.js` instead
- [ ] No TODOs, `pass` stubs, or "in a real implementation would…" comments
- [ ] Docs updated if this changes `docs/api-contract.md`,
      `docs/safety-model.md`, `docs/rbac.md`, or the README's environment
      variable table

## How was this tested?

<!-- Commands run, clusters used (kind/minikube/real), manual steps. -->
