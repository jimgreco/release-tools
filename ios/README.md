# iOS TestFlight release engine

One canonical copy of the release guards and TestFlight upload path shared by the
iOS apps. Each app keeps only:

1. its `release-policy.json` (schema below),
2. its Xcode project/workspace and scheme, and
3. one app-specific "configure Info.plist" step that runs between `prepare` and `upload`.

Apps reference the actions pinned by commit SHA:

```yaml
- uses: jimgreco/release-tools/ios/prepare@<sha>
- run: ./configure-plist.sh            # app-specific, optional
- uses: jimgreco/release-tools/ios/upload@<sha>
- uses: jimgreco/release-tools/ios/cleanup@<sha>
  if: always()
```

## Security posture

- **GET only.** `scripts/asc-readonly.mjs` is the only App Store Connect client. It
  issues `GET` requests to `https://api.appstoreconnect.apple.com/v1/...` only,
  refuses redirects, cross-origin pagination links and credentials in URLs, and
  never retries a 4xx. It never creates, replaces or edits certificates, profiles,
  bundle ids, apps, groups or testers.
- **One Apple write.** The only write is `xcrun altool --upload-app`, and it runs
  only after: source binding (SHA + Git commit count), the GET-only audience and
  build-number preflight, the pinned-certificate check, pinned-profile fetch and
  local decode/verification, archive, export with generated `ExportOptions.plist`,
  full signed-artifact verification, and a second fetch-and-compare of the
  profiles plus a second preflight immediately before upload.
- **Policy-owned signing.** `DEVELOPMENT_TEAM`, `CODE_SIGN_STYLE=Manual`,
  `CODE_SIGN_IDENTITY` (= `certificateSha1`), `CURRENT_PROJECT_VERSION` (= Git
  commit count) and `OTHER_CODE_SIGN_FLAGS=--keychain <temp keychain>` come from the
  engine; callers cannot override them through `extra-build-settings`.
- **No secrets printed.** Secrets reach scripts only as environment variables from
  action inputs (GitHub masks them). Command failures from `security`/`git` withhold
  output; `archive.py --dry-run` prints setting names only; plist mismatches report
  key names, not configured values.
- **Isolated credentials.** The `.p8` is written under a per-run `mktemp -d`
  (`private_keys/`, mode 0600); the identity is imported into a temporary keychain
  which `cleanup` deletes, restoring the original search list.
- **Cached profiles are never overwritten.** An existing installed profile with the
  same UUID must be byte-identical or the run stops.

## Actions

### `ios/prepare`

| input | required | notes |
|---|---|---|
| `policy` | yes | path to the app's `release-policy.json` |
| `platform` | no | `ios` (default) or `mac` (Mac Catalyst; IPA vs pkg, IOS vs MAC_OS train) |
| `sha` | no | defaults to `github.sha`; must equal checked-out `HEAD` |
| `app-store-connect-key-id`, `app-store-connect-issuer-id`, `app-store-connect-private-key` | yes | private key may be PEM, `\n`-escaped PEM, or base64 of the PEM |
| `dist-cert-p12`, `dist-cert-password` | yes | base64 `.p12` of the **existing** Apple Distribution identity |
| `keychain-password` | no | random per run when empty |
| `installer-cert-p12`, `installer-cert-password` | mac only | existing Mac installer identity |
| `xcode-candidates` | no | whitespace-separated; first installed wins. Default: Xcode 26.4 → 26.0, `Xcode_26`, `Xcode-26`, `Xcode.app` |

Steps: validate policy (Python + Node) → select Xcode → `BUILD_NUMBER = git rev-list
--count HEAD`, `prepare-source` (clean checkout required; stamps `ReleaseCommit`
into each `infoPlists` entry) → write API key → GET-only preflight → extract the
public certificate from the `.p12` and check **both** `certificateSha1` and
`certificateSha256` → temp keychain + import → `find-identity` must show
`certificateSha1` → (mac) resolve exactly one approved installer identity →
fetch, verify and install the pinned profiles.

Exports for later steps: `RELEASE_POLICY`, `RELEASE_PLATFORM`, `RELEASE_SHA`,
`RELEASE_BUILD_NUMBER`, `IOS_BUILD_NUMBER`, `CI_BUILD_NUMBER`, `MAC_BUILD_NUMBER`
(mac), `RELEASE_TEMP`, `KEYCHAIN_PATH`, `RELEASE_CERTIFICATE_DER`,
`APP_STORE_CONNECT_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`,
`APP_STORE_CONNECT_API_KEY_PATH`, `INSTALLER_SHA1` (mac).

### App step: configure Info.plist

Runs after `prepare`. It may change only keys listed in `configuredPlistKeys` of
files listed in `infoPlists`; `upload` re-verifies that nothing else changed.

### `ios/upload`

| input | required | notes |
|---|---|---|
| `project` / `workspace` | exactly one | |
| `scheme` | yes | |
| `configuration` | no | default `Release` |
| `extra-build-settings` | no | one `KEY=VALUE` per line, `#` comments allowed |

Placeholders in `extra-build-settings` values are expanded by `scripts/archive.py`
(never by a shell, so values with spaces or `$` are passed verbatim as one argument):

| placeholder | value |
|---|---|
| `@BUILD_NUMBER@` | Git commit count |
| `@GIT_SHA@`, `@GIT_SHA7@` | release SHA, first 7 characters |
| `@PROFILE_UUID:<name>@` | `profiles.<name>.uuid` from the policy |
| `@MAC_PROFILE_UUID:<name>@` | `macProfiles.<name>.uuid` |
| `@ENV:<NAME>@` | a non-empty environment variable (set `env:` on the upload step, e.g. from secrets) |

This is how each app's profile-name convention is expressed, e.g.
`IOS_PROFILE_NAME=@PROFILE_UUID:app@`, `APP_STORE_PROFILE=@PROFILE_UUID:app@` or
`PROVISIONING_PROFILE_SPECIFIER=@MAC_PROFILE_UUID:app@` — the UUIDs live only in the policy.

The platform is the one `prepare` used. Upload then: `source` verify → archive
(`generic/platform=iOS` or `generic/platform=macOS,variant=Mac Catalyst`) →
export (`method=app-store-connect`, manual signing, pinned profiles,
`testFlightInternalTestingOnly` from `buildAudience`, installer identity for mac) →
exactly one `.ipa`/`.pkg` → artifact verify → re-fetch profiles and `cmp` → preflight
again → `altool --upload-app --type ios|macos` (fails on non-zero exit, on any known
failure marker, or without an explicit success marker) → `verify-upload` (polls up to
10 minutes for `VALID`, policy audience, attachment to every tester group, no
unapproved group or individual tester).

### `ios/cleanup`

No inputs. Restores the keychain search list, deletes the temp keychain and removes
`cert.p12`, `installer.p12` and the `.p8`. Always call with `if: always()`; it is a
no-op if `prepare` never got that far.

## Policy schema (`release-policy.json`)

```json
{
  "app": "example",
  "appId": "1234567890",
  "bundleId": "com.example.app",
  "teamId": "ABCDE12345",
  "certificateSha1": "963A7A5107DB8D4C141371C285F71CCAE84A5452",
  "certificateSha256": "7fb57dbf0cd7923f9ba97d8801824de79e1ba235dbe78d4a821f8195c30f0bf8",
  "testerEmailSha256s": ["<sha256 of lowercased tester email>"],
  "groups": [
    { "id": "<betaGroup id>", "name": "Family", "internal": true, "testers": 1, "automatic": true },
    { "id": "<betaGroup id>", "name": "External", "internal": false, "testers": 0, "automatic": false }
  ],
  "profiles": {
    "app": {
      "uuid": "<lowercase profile uuid>",
      "name": "Example App Store",
      "bundleId": "com.example.app",
      "groups": ["group.com.example.app"],
      "appleSignin": ["Default"],
      "push": null
    },
    "extension": {
      "uuid": "<lowercase profile uuid>",
      "name": "Example Widget App Store",
      "bundleId": "com.example.app.widget",
      "groups": ["group.com.example.app"],
      "appleSignin": [],
      "push": null
    }
  },
  "marketingVersion": "1.4",
  "infoPlists": ["ios/App/Info.plist", "ios/Widget/Info.plist"],
  "configuredPlistKeys": ["API_BASE_URL"],
  "apiOrigin": "https://example.com",
  "buildAudience": "APP_STORE_ELIGIBLE"
}
```

### Required keys

| key | meaning |
|---|---|
| `appId` | numeric App Store Connect app id (string) |
| `bundleId` | main app bundle id; must equal `profiles.app.bundleId` |
| `teamId` | 10-character team id |
| `certificateSha1` | uppercase SHA-1 of the existing distribution certificate (the `CODE_SIGN_IDENTITY`) |
| `certificateSha256` | lowercase SHA-256 of the same certificate (pinned in profiles, Apple's certificate resource, the signed artifact) |
| `profiles` | pinned **existing** App Store profiles keyed by name; must contain `app`; every other entry is one app extension. Each entry: `uuid` (lowercase), `name`, `bundleId`, `groups`, `appleSignin`, `push` (`null` or `"production"`), optional `associatedDomains`, optional `healthkit` / `healthkitBackgroundDelivery` booleans |
| `infoPlists` | repo-relative plists stamped with `ReleaseCommit` and allowed to change (may be `[]` when a build phase stamps `ReleaseCommit`) |
| `configuredPlistKeys` | keys the configure step may change in those plists |
| `marketingVersion` | `CFBundleShortVersionString` and the TestFlight train compared against |
| `groups` | **every** existing TestFlight group of the app; any added, removed, renamed or public group fails the run |
| `testerEmailSha256s` | sha256 of each approved tester's trimmed, lowercased email |

### Optional keys

| key | default | meaning |
|---|---|---|
| `apiOrigin` | `null` | production origin; by default the main bundle must have `API_BASE_URL == apiOrigin` |
| `mainInfoPlist` | `{"API_BASE_URL": "{apiOrigin}"}` if `apiOrigin`, else `{}` | exact main-bundle Info.plist values to verify in the artifact. Templates: `{apiOrigin}`, `{build}`, `{sha}`, `{sha7}`. Replaces the default when set |
| `buildAudience` | `APP_STORE_ELIGIBLE` | `APP_STORE_ELIGIBLE` (`testFlightInternalTestingOnly=false`) or `INTERNAL_ONLY` |
| `macProfiles` | — | Mac Catalyst profile, `{"app": {...}}` only (no extensions); also `signedGroups` (expected signed App Groups if they differ from the profile) and `signedRequired` (exact extra signed entitlements, e.g. sandbox) |
| `macInstaller` | — | required with `macProfiles`: `identityPrefix`, `alternateIdentityPrefix`, `teamSuffix` (`" (<teamId>)"`) |
| `excludedHistoricalTesters` | `[]` | `[{id, emailSha256}]` app-level tester records allowed to remain **outside every group** (former testers) |
| `allowDottedBuildHistory` | `false` | accept existing builds like `240.1` in the train (leading count must still be below this build) |
| `allowApprovedIndividualTesters` | `false` | allow the uploaded build to carry individual testers, if they are a distinct subset of the approved testers |

### Schema decisions

- **Testers are a list.** `testerEmailSha256s` is the only accepted form. The legacy
  singular `testerEmailSha256` is rejected with a migration message rather than
  silently normalised, so there is one spelling in every repo.
- **Group/tester invariants.** Only internal groups may hold testers, and each group
  that holds testers must hold exactly the full approved set (the same records
  across groups). The app-wide tester list must be exactly those records plus any
  pinned `excludedHistoricalTesters`, which must not be in any group.
- **Audience is policy, not a workflow input.** The per-workflow `distribution` input
  is gone; `buildAudience` decides both `testFlightInternalTestingOnly` at export and
  the `buildAudienceType` that `verify-upload` requires.
- **Strict policy validation** runs first (in both Python and Node): unknown audience,
  malformed hashes, uppercase UUIDs, non-relative plist paths, missing `macInstaller`
  for Catalyst, etc. all stop the run before any Apple call.

## Engine CLIs

```
python3 ios/scripts/verify-release.py <mode> [--policy PATH] [--platform ios|mac] ...
  validate-policy | policy-value --key K | prepare-source | source | certificate |
  profiles | export-options | installer-identity | artifact
node ios/scripts/asc-readonly.mjs <mode> [--policy PATH] [--platform ios|mac] ...
  validate-policy | profiles | preflight | verify-upload | discover-audience
python3 ios/scripts/archive.py --platform ios|mac --project|--workspace ... [--dry-run]
```

`discover-audience` is a migration helper: given a policy with `bundleId` and
`testerEmailSha256s`, it prints the existing `appId` and groups (GET only) and
refuses if the existing audience is broader than the approved internal testers.

## Tests

```
node --test ios/tests/*.test.mjs
python3 -m unittest discover -s ios/tests -p 'test_*.py'
shellcheck -x ios/scripts/*.sh
```

Fixtures in `ios/tests/fixtures/` are synthetic and cover: single app with
HealthKit + custom `mainInfoPlist` + `INTERNAL_ONLY`; plural testers with dotted build
history, associated domains and approved individual testers; app + extension +
Mac Catalyst + external group + historical tester; app + extension default.
CI: `.github/workflows/test-ios.yml` (ubuntu; Apple tools are mocked).
