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
