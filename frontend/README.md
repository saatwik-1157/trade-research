# frontend

The web interface. Next.js (App Router) + Tailwind v4 + TanStack Query,
talking to the FastAPI backend through `src/lib/services.ts`.

```bash
npm install
npm run dev        # http://localhost:3000, proxied through nginx at :8080
```

The API it talks to must be running; see the repository `DEPLOYMENT.md`.

## Checks

```bash
npm run typecheck
npm run lint
npm test
npm run build
```

All four run in CI. `npm test` includes `contrast.test.ts`, which recomputes
every colour pair in the palette from `globals.css` and fails if one drops
below its floor.

## Where things are

| path | what |
|---|---|
| `src/app/` | one directory per route |
| `src/components/ui/` | the primitives: `Button`, `Badge`, `DataTable`, `Input`, `Panel`, `StatTile`, `States` |
| `src/components/` | feature components, each owning one question |
| `src/lib/services.ts` | every backend call, one function per endpoint |
| `src/lib/nav.ts` | `NAV` — the single source for both the desktop rail and the mobile drawer |
| `src/app/globals.css` | the palette and the design tokens |

## The rules this UI is written to

**A dash is never a zero.** Monetary fields arrive as strings or `null`.
`StatTile` renders `null` as `—`. A balance showing `0.00` for an account that
could not be read is a measurement the platform did not make.

**Loading, empty and error are three states, not one.** An empty positions
table when the broker is unreachable is not a flat book.

**Never render a state the backend did not report.** `StatusDot` has an
`UNKNOWN` that is not a synonym for disconnected.

**A capability notice must be true.** `Unavailable` names the level that builds
the thing and what does the job today. When the backend starts serving it, the
panel gets wired — a notice nobody revisits is how the front page ended up
several levels behind the platform.

**One implementation per thing.** Search before adding. The nav list is shared
between the rail and the drawer for this reason.

Design decisions and the measured contrast figures are in the repository's
`INTERFACE.md`.
