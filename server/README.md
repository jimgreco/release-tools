# Server releases on the shared EC2 host

`release.py` replaces only one app's containers on the shared Docker Compose
host with exact-commit images from GHCR. Apps call the reusable workflow
`.github/workflows/server-release.yml`, which builds and pushes the images and
then pipes `release.py` to the host over the pinned SSH connection.

Every app uses the same procedure:

1. Take both host release locks (`/tmp/codex-shared-host-release.lock`, then
   `~/deploy/.app-release.lock`), so no two app releases overlap.
2. Rebuild the Compose invocation recorded on the running containers and
   refuse unless each service's config hash matches the live container.
3. `docker pull` the release image(s) with the host's existing registry login.
4. Write `~/deploy/releases/<app>-image-<sha>.json`, which sets only `image` and
   the declared metadata environment, and prove the new model differs from the
   live one in nothing else.
5. Tag the previous images `<service>-rollback:<time>-<container>`, optionally
   back up the app database, run preflight and migration commands from the
   new image, then `up -d --no-deps --no-build --force-recreate` only those
   services.
6. Run each service's health command inside the container. On failure, roll
   back to the previous images. If migrations already ran, stop the services
   instead (or roll back, if `on_failure_after_migration` is `rollback`).

A record of every release is kept in `~/deploy/release-records/<app>-<sha>.json`.
The script never edits `.env` or shared Compose files, creates roles, or prunes
images, and it never prints Compose or inspect output.

## Recreating a service by hand

After the first release, the app's image is set only by its override file in
`~/deploy/releases/`. A bare `docker-compose up -d <service>` in `~/deploy`
omits that file and recreates the service from its old build or default
image. Reuse the exact `-f` chain in the running container's
`com.docker.compose.project.config_files` label, or release again through the
workflow. Ops scripts that recreate app services must do the same.

## Manifest (`.github/release/server.json` in the app repo)

```json
{
  "app": "macros",
  "image": "ghcr.io/jimgreco/macro_tracker",
  "images": [
    { "context": ".", "dockerfile": "Dockerfile", "build_args": { "APP_BUILD": "{sha7}" } }
  ],
  "services": {
    "macros": {
      "metadata_env": { "APP_BUILD": "{sha7}" },
      "health": ["node", "-e", "fetch('http://127.0.0.1:3000/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
    }
  },
  "legacy_overrides": ["app-release-[a-z0-9_]+\\.json"],
  "backup": { "database": "macro_tracker", "require_table": "users", "keep": 20 },
  "public": { "url": "https://macrovana.com/version", "field": "build", "length": 7 }
}
```

| Key | Meaning |
|---|---|
| `app` | Short lowercase name. Used for override, record and backup file names. |
| `image` | Must be `ghcr.io/<owner>/<repo>` for the calling repository (lowercase). |
| `images[]` | One entry per image to build: `tag_suffix` (default `""`), `context`, `dockerfile`, `target`, `build_args` (`{sha}`/`{sha7}` substituted) and `prebuild` (a shell command run in the checkout before the build, e.g. writing `version.json`). Tags are `<sha>` or `<sha>-<suffix>`. |
| `services.<name>` | The Compose services this release replaces. `tag_suffix` picks the image. `metadata_env` holds environment set by the override (`{sha}`, `{sha7}`, `{time}`); these are the only environment keys a release may change. `health` is the command run with `exec` (all `{…}` placeholders substituted) and must exit 0. |
| `profile` | Compose profile of the services, if any. |
| `interpolate_from_live` | Compose interpolation variables that live deploys used to set in the shell, e.g. `{"COMMON_WEEK_IMAGE": "image:common-week", "COMMON_WEEK_APP_BUILD": "env:common-week:APP_BUILD"}`. Values come from the running container, so the live model can be reproduced. |
| `legacy_overrides` | Regexes of override file names from older release helpers that this release may drop from the chain. Dropping one is still allowed only if the resulting model differs in nothing but image and metadata environment. |
| `allow_not_running` | If `true`, exit 0 when none of the services is running (first activation is a manual step). |
| `backup` | `pg_dump -Fc` of `database` inside `shared_db`, checked readable with `pg_restore --list` and required to contain `require_table`. Keeps the newest `keep` dumps (default 20) in `~/deploy/backups/<app>/`. |
| `preflight[]` | `{service, command}` run as `compose run --rm --no-deps` from the new image before anything stops. |
| `migrate` | `{stop_first, commands[]}`: optionally stop the services, then run each command from the new image. |
| `on_failure_after_migration` | `stop` (default) or `rollback`. |
| `health_attempts` | Health retries, 2 seconds apart (default 60). |
| `public` | Optional: `url` and dotted `field` holding the build, `length` (7 or 40), and `refuse_backwards` (deploy only if the live build is an ancestor of this commit). The workflow checks it before and after the host release. |

## Testing

```bash
python3 -m unittest discover -s server/tests -p 'test_release_unit.py'
python3 -m unittest discover -s server/tests -p 'test_release_integration.py'   # needs Docker
```

The integration test uses a local registry, the Compose project `rtest` and a
Postgres container named `rtest_shared_db`. It covers a release with a
verified backup, an idempotent re-run, a release that follows on from a
previous override, automatic rollback after a failed health check, stopping
after a failed migration, and refusal when the Compose files drift.
