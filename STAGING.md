# Staging

`staging` is the experiment branch for site revamps. It deploys to a Vercel
**preview** URL; `main` remains the only branch that deploys clawbeat.co.

## URLs

| Branch    | Deploys to                                  |
|-----------|---------------------------------------------|
| `main`    | https://clawbeat.co (production)            |
| `staging` | `clawbeat-git-staging-<scope>.vercel.app`   |

The staging URL is stable across pushes — Vercel gives every branch a fixed
`-git-<branch>-` alias in addition to the per-commit deployment URL. Grab it
once from the Vercel dashboard (Deployments → the staging build → Domains) and
bookmark it.

Vercel serves all preview deployments with `X-Robots-Tag: noindex`, so staging
will not be indexed or outrank the real site.

## Daily workflow

```bash
git checkout staging
# ...experiment...
git push                     # → rebuilds the staging preview
```

Ship a revamp when it's ready:

```bash
git checkout main
git merge staging
git push                     # → deploys clawbeat.co
```

## Keeping staging fresh

The `Daily Edition` and `Trace Monthly Issue` workflows auto-commit generated
HTML to `main` (`public/daily/`, `public/trace/`, `public/sitemap.*`). Staging
does not receive those, so it drifts. Pull them in periodically:

```bash
git checkout staging
git merge main
```

Do this before merging staging back into main, so any conflict gets resolved on
the experiment branch rather than on the branch that serves the live site.

## Data

Staging shares the **production Supabase project** — real news, events, and
daily editions render on the preview. It is safe for reads. Do not run writes
from staging:

- Don't `workflow_dispatch` `Daily Edition` or `Trace Monthly Issue` against the
  `staging` ref. Both jobs are guarded with `if: github.ref == 'refs/heads/main'`
  and will skip, but the guard is a backstop, not a reason to try.
- The forge scrapers (`forge.py`, `events_forge.py`, ...) write to prod
  regardless of branch — they're run by cron on `main`, not by the preview.
- `/admin.html` on the staging URL edits **live** data. Treat it as production.

If a revamp needs schema changes, stop and create a separate Supabase project
first — don't migrate prod from a branch.

## Serverless functions

`/api/*` Python functions deploy with each preview and hit prod Supabase. The
Slack ingest/review endpoints are only invoked at the URLs configured in the
Slack app, which point at clawbeat.co — previews won't receive that traffic.

## Admin panel build

`npm run build` runs `scripts/generate-admin.js`, which needs `ADMIN_*` env vars.
Vercel must have these enabled for the **Preview** environment as well as
Production, or the staging build fails. Check
Vercel → Settings → Environment Variables and confirm each `ADMIN_*` var has
Preview ticked.
