# release-tools

One shared copy of the release procedure for the apps on the shared EC2 host
and in the App Store. Each app keeps only its own policy and manifest files;
everything else lives here, pinned by commit SHA.

| Area | Entry point | Docs |
|---|---|---|
| Server (EC2 / Docker Compose) | `.github/workflows/server-release.yml` (reusable workflow) | [server/README.md](server/README.md) |
| iOS / TestFlight | `ios/prepare`, `ios/upload`, `ios/cleanup` (composite actions) | [ios/README.md](ios/README.md), [ios/MIGRATION.md](ios/MIGRATION.md) |

Pin to a full commit SHA, never a branch:

```yaml
uses: jimgreco/release-tools/.github/workflows/server-release.yml@<sha>
uses: jimgreco/release-tools/ios/prepare@<sha>
```

This repository holds no secrets, tester identities or app policy. Callers
pass credentials as secrets; app-specific values stay in each app's
`.github/release/` files.

## The app release workflow

Every app has the same three workflow files:

| File | Trigger | Contents |
|---|---|---|
| `release.yml` | PRs, push to `main`, manual | Copied verbatim from [`templates/release.yml`](templates/release.yml), with only the pinned commit replaced. Server-only apps delete the `ios` job and input. |
| `verify.yml` | `workflow_call` only | All of the app's own checks (lint, tests, builds, policy validation). This is the only place apps differ. |
| `testflight.yml` | `workflow_call` only | `ios/prepare` → the app's Info.plist step → `ios/upload` → `ios/cleanup`. |

PRs run `verify`. A push to `main` runs `verify`, then releases the server and
uploads to TestFlight. A manual run on `main` releases what its checkboxes
select. Any other workflow in an app is manual tooling (App Store metadata,
screenshots, submit for review, backups, monitors).
