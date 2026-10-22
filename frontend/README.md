# Skill Router — frontend

Next.js 15 (App Router) admin console for the Skill Router backend, built against
`docs/CONTRACT.md` §2 (§error envelope), §4 (status vocabulary) and §5 (admin API).

```bash
cd frontend
cp .env.example .env.local     # SR_API_ORIGIN=http://127.0.0.1:8000
npm install
npm run dev                    # http://localhost:3000
npm run build && npm run lint  # must both pass
npm run typecheck              # tsc --noEmit
```

`next.config.mjs` rewrites `/api/:path*` to `${SR_API_ORIGIN ?? 'http://127.0.0.1:8000'}/api/:path*`
so the session cookie and the `sr_csrf` cookie stay same-origin. `output: 'standalone'`
is on for Docker.

## What exists in this milestone

Pages (owned here): `/login`, `/` (overview), `/providers`, `/providers/[id]`,
`/capabilities`, `/capabilities/[id]`.

`/skills`, `/access`, `/records`, `/settings`, `/settings/engine`, `/account` and the
floating agent panel are **not implemented here** — they belong to the next milestone.
The sidebar links to them and `app/not-found.tsx` explains the gap honestly; those
URLs return HTTP 404 today.

## Architecture

```
app/
  layout.tsx            root html/body + AppProviders
  globals.css           --sr-* design tokens, base styles, focus rings, reduced motion
  not-found.tsx         honest 404 for not-yet-built routes
  login/page.tsx        sign-in, lockout surfacing, first-run password change
  (dashboard)/          authenticated route group (AppShell)
    page.tsx            overview
    providers/page.tsx  providers list
    providers/[id]/     provider detail + credentials
    capabilities/       list + the capability workbench
components/
  ui/                   design system primitives (barrel: components/ui/index.ts)
  shell/                AppShell, NavList, AccountMenu
  providers/            ProviderFormDialog
  credentials/          CredentialFormDialog, CredentialsSection
  capabilities/         StateChips, CapabilityFormDialog, config-editor panels,
                        TestRunner, RoutesPanel, VersionsPanel
  account/              ChangePasswordForm (used by /login and the shell banner)
lib/
  api.ts                typed fetch wrapper + endpoint builders
  types.ts              CONTRACT §5 payload types + §4 status vocabularies
  format.ts             times, durations, bytes, currency, status label/tone
  config.ts             immutable deep get/set for the version config
  json.ts               JSON parse helpers + dirty comparison
  queries.ts            react-query hooks, keys, invalidation
  auth.tsx              SessionProvider / useSession
  navigation-guard.tsx  unsaved-changes guard (in-app + beforeunload)
  query-provider.tsx    QueryClient
  hooks/                useFocusTrap, useDebouncedValue
```

### API client (`lib/api.ts`)

* relative `/api/...` paths only, `credentials: 'include'`, `cache: 'no-store'`;
* mutations echo `x-csrf-token` read from the `sr_csrf` cookie (fallback: the token
  from `GET /api/auth/session`);
* `401` → `window.location.assign('/login?next=<path>')` unless the caller passes
  `skipAuthRedirect` (only the session probe does, because it decides itself);
* every failure becomes `ApiError { code, message, details, requestId, actions }`,
  parsed from the §2 envelope; transport failures become `network_error`;
* helpers: `queryString`, `describeError`, `errorActions`, `errorRequestId`, `endpoints.*`.

Pass `skipAuthRedirect` only when you handle the anonymous state yourself.

### Data hooks (`lib/queries.ts`)

`useSessionQuery`, `useOverviewQuery`, `useOverviewProblemsQuery`, `useProvidersQuery`,
`useProviderQuery`, `useCreate/Update/DeleteProviderMutation`, `useCredentialsQuery`,
`useCredentialQuery`, `useCreate/Update/DeleteCredentialMutation`,
`useVerifyCredentialMutation`, `useCapabilitiesQuery`, `useCapabilityQuery`,
`useCreateCapabilityMutation`, `useUpdateCapabilityMutation`, `useVersionsQuery`,
`useVersionQuery`, `useUpdateVersionMutation`, `useCreateVersionMutation`,
`useValidateVersionMutation`, `useTestVersionMutation`, `usePublishVersionMutation`,
`useDeprecateVersionMutation`, `useRollbackMutation`, `useTestsQuery`, `useDiffQuery`,
`useInvokeMutation`, `useRoutesQuery`, `useCreateRouteMutation`,
`useUpdateRouteMutation`, `useDeleteRouteMutation`, `useHealthCheckMutation`,
`useRoutePreviewMutation`.

Mutations invalidate exactly the affected keys (`queryKeys`). Query retries stop on
4xx so failures stay visible instead of being hidden by retries.

## Design system

Tokens in `app/globals.css` (`--sr-bg`, `--sr-surface`, `--sr-border-strong`,
`--sr-text`, `--sr-muted`, `--sr-accent: #2563eb`, `--sr-danger`, `--sr-success`,
`--sr-warning`, soft variants, `--sr-radius-control: 6px`, `--sr-radius-card: 8px`,
`--sr-space-1..6` on an 8px grid, `--sr-shadow-overlay`). Neutral slate + one accent,
no gradients, subtle shadow only on overlays, system font stack (no network fonts),
13/14/16/20/24 type scale, 150 ms transitions, `:focus-visible` ring everywhere, and
`prefers-reduced-motion` disables animation.

Narrow layouts: the sidebar is fixed ≥1024px and collapses to a top bar + Drawer below
that; tables always scroll horizontally (`.sr-scroll-x`).

## UI primitives (`components/ui`, import from `@/components/ui`)

| Primitive | Notes |
|---|---|
| `Button`, `IconButton` | variants `primary/secondary/ghost/danger`, sizes `sm/md`, `loading` sets `aria-busy` + disables |
| `Input`, `Textarea` | `invalid` → `aria-invalid` + danger border, `iconLeft` slot |
| `Select` | native select + `aria-invalid`, `options`, `placeholder` |
| `Checkbox`, `Switch` | real focusable controls, `role="switch"` + `aria-checked` |
| `Field`, `FormRow`, `FormSection` | label + hint + inline error + required marker, `fieldIds()` for `aria-describedby` |
| `Badge`, `StatusBadge` | tone from `statusLabel`/`statusTone`; `StatusBadge status="published"` |
| `Card`, `CardHeader`, `CardBody`, `CardFooter` | bordered surface, 8px radius |
| `Table`, `THead`, `TBody`, `TR`, `TH`, `TD`, `TableEmpty`, `SortableTH` | caption required, horizontal scroll, `aria-sort` on sortable headers |
| `Dialog`, `ConfirmDialog` | portal, `aria-modal`, focus trap, Esc, focus restore; `ConfirmDialog` renders `impact` |
| `Drawer` | right/left panel with the same a11y guarantees (`widthClassName` for 420px panels) |
| `Tabs`, `TabPanel` | arrow/Home/End keyboard support, `aria-selected` + panels |
| `Tooltip` | hover **and** focus, `aria-describedby` |
| `ToastProvider`, `useToast` | `success/error/warning/info`, `toast.apiError(error, title)` shows message + request id + actions; errors persist |
| `Banner`, `InlineAlert` | inline error with `actions`, `requestId`, retry `primaryAction` |
| `EmptyState` | icon, title, description, primary/secondary action |
| `Skeleton`, `SkeletonBlock`, `SkeletonStats`, `SkeletonTable` | `role="status"` once per block |
| `CodeBlock`, `prettyJson` | copy button with real success/failure, `notice` slot; never pass secrets — the API only returns a masked `hint` |
| `SearchInput` | debounced (default 300 ms), labelled, clear button |
| `Pagination` | `total/limit/offset/onChange`, honest range label |
| `PageHeader`, `SectionHeading` | title, description, meta, actions, optional back link |
| `Stat`, `StatGrid` | metric tile, `null` renders `—` instead of a fake 0 |
| `Stepper` | vertical/horizontal, `complete/current/upcoming/blocked` |
| `JsonEditor` | live JSON validation, Format button, `requireObject`, `externalError` |
| `TimeAgo` | absolute UTC on first paint, relative after mount (no hydration mismatch) |

Notes for the next milestone: use `useGuardedRouter().push()` for navigation inside an
editor, `useRegisterUnsavedChanges({what, isDirty, save})` to plug the guard, and
`toast.apiError` for every mutation failure. `AppShell` already renders the mobile
Drawer, account menu and the `must_change_password` banner + change-password dialog.

## Behaviour guarantees

* every mutation: pending state on the button, success toast **only** after a 2xx,
  failure toast **with request id and server `actions`**, plus an inline banner;
* never renders a secret — credential rows only show the masked `hint` and field names;
* the four capability states (saved / test passed / published / callable) are separate
  chips fed from `capability.verification`, never collapsed into one badge;
* publish stays clickable so a real `409 test_evidence_missing` (or `422
  validation_failed`) surfaces, with a banner explaining the blocker in advance;
* validate/test/publish run against the **stored** version, so they are disabled while
  the editor is dirty and the UI says why;
* a failed request never produces a "saved" claim.

## Deviations from CONTRACT §9 (and why)

1. **Top bar title.** The shell's top bar shows the current section name + description
   and the account menu; the page-level title and primary action live in `PageHeader`
   immediately below it (one title per screen instead of a duplicated one).
2. **Client-side sorting.** The list endpoints carry no `sort` parameter, so sortable
   headers order the rows of the loaded page and the UI states that explicitly.
3. **`/capabilities/[id]` tab routes.** The workbench is one route with a tab strip
   (`?tab=` is read for deep links such as `/capabilities/{id}?tab=routes`), not one
   route per tab.
4. **`/account` is not implemented** (owned by the next milestone). The first-run
   password change renders `components/account/change-password-form.tsx` inline on
   `/login` and in a dialog from the shell banner, so the flow is complete without it.
5. **Capability detail fields for provider/credential** are edited as ids (`prov_…`,
   `cred_…`) in the Overview tab, because no picker endpoint exists beyond the list
   endpoints; the create dialog uses real selects.

## Not available yet (honest gaps, no mock data)

* `GET /api/admin/capabilities/{id}/tests` returns test rows **without** request /
  response bodies; the bodies are only shown in the Test dialog result.
* No endpoint lists capability-level "callable as agent" checks per agent, so the
  readiness card shows the `verification.checks` the API returns and nothing more.
* No version-publish audit trail endpoint is used; version rows show what
  `serialize_version` provides.
* Route `conditions` are preserved verbatim but have no structured editor.

If an endpoint is missing at integration time the page shows the real error envelope
(code, message, request id, actions) with a retry button — it never substitutes
placeholder data.

## Smoke test (this milestone)

Verified against a live backend on a copy of `data/skillrouter.db`:

* `POST /api/auth/login`, `GET /api/auth/session`, `POST /api/auth/change-password` — 200;
* `GET /api/admin/overview`, `/overview/problems`, `/providers`, `/credentials`,
  `/capabilities`, `/capabilities/{id}`, `.../versions`, `.../versions/{id}`,
  `.../tests`, `.../routes`, `/invocations`, `/skills`, `/agents`, `/settings` — 200;
* `POST /api/admin/providers`, `/credentials`, `.../verify`, `/capabilities` — 201/200
  (mutations require the `x-csrf-token` header, which `lib/api.ts` sends);
* `POST .../versions/{id}/publish` on a fresh draft — `422 validation_failed` (blocking
  config issues come before the missing-test check, as the backend orders them);
* through the Next server: `/login` renders the form server-side, `/`, `/providers`,
  `/capabilities`, `/providers/[id]`, `/capabilities/[id]` return 200 (client-side data
  fetch), `/skills`, `/access`, `/records`, `/settings`, `/account` return 404 with the
  custom not-found page;
* `/api/*` through `:3000` returns the same payloads as the backend (rewrite + cookies
  work).
