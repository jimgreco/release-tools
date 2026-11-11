# Migrating the five apps to `release-tools/ios`

Nothing here has been applied to any app repository. For each app: edit
`.github/scripts/release-policy.json` as listed, replace the TestFlight job with
the sketch, then delete `.github/scripts/{verify-release.py,asc-readonly.mjs,release-guard-tests.mjs,test_release.py}`
(except where noted for macros). `RELEASE_TOOLS_SHA` is the pinned commit of this repo.

## Changes common to every app

- **Tester pins become a list.** Replace `"testerEmailSha256": "<hash>"` with
  `"testerEmailSha256s": ["<same hash>"]`. The singular key is now rejected.
- **Audience default is `APP_STORE_ELIGIBLE`.** Omit `buildAudience` (or set it
  explicitly). `INTERNAL_ONLY` is still available per policy.
- **Engine self-tests leave the release job.** `node --test release-guard-tests.mjs`
  / `test_release.py` now run in release-tools CI (`test-ios.yml`) against the
  pinned SHA, not on every app release.
- **Signing identity is checked by policy, not by a hard-coded hash or name.**
  `find-identity` must show `certificateSha1`; the certificate extracted from the
  `.p12` must match both `certificateSha1` and `certificateSha256`. The
  `security find-certificate -c 'Apple Distribution: …'` lookup is gone.
- **Profile UUIDs live only in the policy.** Archive settings use
  `@PROFILE_UUID:<name>@` / `@MAC_PROFILE_UUID:<name>@` instead of literal UUIDs.
- **Stricter upload log check everywhere** (macros' list): `NOT_AUTHORIZED`,
  `failed to authenticate`, `received status code 401`, `UPLOAD FAILED`,
  `Validation failed`, `Failed to upload package`, `Failed to determine the Apple ID`,
  plus a non-zero altool exit, all fail the job.
- `KEYCHAIN_PASSWORD` may stay; if omitted a random per-run password is used.
- Secrets used by the actions (unchanged names): `APP_STORE_CONNECT_KEY_ID`,
  `APP_STORE_CONNECT_ISSUER_ID`, `APP_STORE_CONNECT_API_KEY`, `IOS_DIST_CERT_P12`,
  `IOS_DIST_CERT_PASSWORD`, `KEYCHAIN_PASSWORD`; planner also
  `MAC_INSTALLER_CERT_P12`, `MAC_INSTALLER_CERT_PASSWORD`.

Shared certificate pins (all five apps use the same distribution identity):

```json
"certificateSha1": "963A7A5107DB8D4C141371C285F71CCAE84A5452",
"certificateSha256": "7fb57dbf0cd7923f9ba97d8801824de79e1ba235dbe78d4a821f8195c30f0bf8",
```

The job skeleton every sketch below uses:

```yaml
    runs-on: macos-latest
    timeout-minutes: 45
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0          # build number = full commit count
      - uses: actions/setup-node@v5
        with:
          node-version: 22
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with:
          policy: .github/scripts/release-policy.json
          app-store-connect-key-id: ${{ secrets.APP_STORE_CONNECT_KEY_ID }}
          app-store-connect-issuer-id: ${{ secrets.APP_STORE_CONNECT_ISSUER_ID }}
          app-store-connect-private-key: ${{ secrets.APP_STORE_CONNECT_API_KEY }}
          dist-cert-p12: ${{ secrets.IOS_DIST_CERT_P12 }}
          dist-cert-password: ${{ secrets.IOS_DIST_CERT_PASSWORD }}
          keychain-password: ${{ secrets.KEYCHAIN_PASSWORD }}
      # <app-specific configure step, if any>
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: <path>.xcodeproj
          scheme: <scheme>
          extra-build-settings: |
            <app-specific>
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

---

## macros (`com.dailymacros.app`)

Policy is the old schema and needs the most work.

```jsonc
{
  "app": "macros",
  "appId": "TODO",                      // not in the repo: run discover-audience (below)
  "bundleId": "com.dailymacros.app",
  "teamId": "V6JPQCD336",
  "certificateSha1": "963A7A5107DB8D4C141371C285F71CCAE84A5452",
  "certificateSha256": "7fb57dbf0cd7923f9ba97d8801824de79e1ba235dbe78d4a821f8195c30f0bf8",
  "testerEmailSha256s": ["<existing testerEmailSha256 value>"],
  "groups": [ /* TODO: from discover-audience */ ],
  "profiles": {
    "app": {
      "uuid": "TODO",                   // from the IOS_PROVISIONING_PROFILE secret
      "name": "TODO",                   // = IOS_PROVISIONING_PROFILE_NAME secret
      "bundleId": "com.dailymacros.app",
      "groups": [],
      "appleSignin": ["Default"],
      "push": null,
      "healthkit": true,
      "healthkitBackgroundDelivery": true
    }
  },
  "marketingVersion": "1.4.0",
  "apiOrigin": "https://macrovana.com",
  "mainInfoPlist": { "APIBaseURL": "{apiOrigin}", "AppBuild": "{build}", "GitCommitHash": "{sha7}" },
  "infoPlists": ["ios/DailyMacros/DailyMacros/Info.plist"],
  "configuredPlistKeys": []
}
```

**TODOs that cannot be derived from the repo:**

1. `appId` and `groups` — macros discovered them at run time (`scope-policy`). Get them
   once, GET-only, with the engine's helper (any machine with the ASC key):
   ```sh
   RELEASE_POLICY=.github/scripts/release-policy.json \
   APP_STORE_CONNECT_KEY_ID=… APP_STORE_CONNECT_ISSUER_ID=… APP_STORE_CONNECT_API_KEY_PATH=…/AuthKey_….p8 \
   node <release-tools>/ios/scripts/asc-readonly.mjs discover-audience
   ```
   It prints `{appId, groups}` and refuses if the audience is broader than the
   approved internal tester. Paste both into the policy.
2. `profiles.app.uuid` / `name` — macros bound these at run time from the
   `IOS_PROVISIONING_PROFILE` / `IOS_PROVISIONING_PROFILE_NAME` secrets. Decode once
   locally: `base64 -d <<<"$IOS_PROVISIONING_PROFILE" > p.mobileprovision && security cms -D -i p.mobileprovision | plutil -extract UUID raw -` (and `Name`). Lowercase the UUID.
   After migration those two secrets and `APPLE_TEAM_ID` are no longer used.

**Behavior changes to accept consciously:**

- **Audience.** Push builds were `INTERNAL_ONLY` and only a dispatched
  `app_store_build` run produced an `APP_STORE_ELIGIBLE` build. With the new default
  every build is App Store-eligible, so the `distribution` input on `testflight.yml`
  and the `distribution: ${{ inputs.app_store_build && 'app-store' || 'internal' }}`
  line in `ci.yml` are removed; the `app_store_build` dispatch input becomes
  unnecessary (the submit flow can use any uploaded build). If macros must keep
  push builds internal-only, set `"buildAudience": "INTERNAL_ONLY"` — but then no
  build is ever submittable without a policy change.
- Pinning `uuid`/`name`/certificate in the policy is stricter than the old run-time
  binding: rotating the profile now requires a policy commit.
- `asc-submit.mjs` imports `policy` and `token` from the local `asc-readonly.mjs`.
  Keep macros' local `.github/scripts/asc-readonly.mjs` (and its
  `release-policy.json` reads) until the submit tooling is migrated; delete only
  `verify-release.py` and `test_release.py`, and trim `release-guard-tests.mjs` to the
  submit tests plus the macros-specific "every Xcode target configuration matches
  the pinned marketing version" test (that check is repo-specific and is not in the
  engine).
- The `IOS_API_BASE_URL` secret is no longer needed: the value is the policy's
  `apiOrigin` and the artifact check enforces `APIBaseURL == apiOrigin`.

```yaml
jobs:
  build:
    if: github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')
    # skeleton …
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with: { policy: .github/scripts/release-policy.json, … }
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: ios/DailyMacros/DailyMacros.xcodeproj
          scheme: DailyMacros
          extra-build-settings: |
            PROVISIONING_PROFILE_SPECIFIER=@PROFILE_UUID:app@
            API_BASE_URL=https://macrovana.com
            APP_BUILD=@BUILD_NUMBER@
            GIT_COMMIT_HASH=@GIT_SHA7@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

No configure-plist step (macros injects values through build settings).

---

## planner / Week of Us (`com.jimgreco.commonweek`, iOS + Mac Catalyst)

Policy changes:

- `testerEmailSha256` → `testerEmailSha256s: ["<existing value>"]`.
- Everything else (appId, groups, certificate pins, `excludedHistoricalTesters`,
  `macProfiles` with `signedGroups`/`signedRequired`, `macInstaller`) is already in
  the new schema. Default audience matches the old hard-coded `false`.

```yaml
jobs:
  ios:
    if: inputs.upload && github.ref == 'refs/heads/main'
    # skeleton …
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with: { policy: .github/scripts/release-policy.json, … }
      - name: Configure production iOS origin
        run: /usr/libexec/PlistBuddy -c 'Set :API_BASE_URL https://weekofus.com' ios/CommonWeek/Info.plist
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: ios/CommonWeek.xcodeproj
          scheme: CommonWeek
          extra-build-settings: |
            APP_PROFILE_NAME=@PROFILE_UUID:app@
            WIDGET_PROFILE_NAME=@PROFILE_UUID:extension@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()

  mac:
    if: inputs.upload && github.ref == 'refs/heads/main'
    # skeleton …
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with:
          policy: .github/scripts/release-policy.json
          platform: mac
          installer-cert-p12: ${{ secrets.MAC_INSTALLER_CERT_P12 }}
          installer-cert-password: ${{ secrets.MAC_INSTALLER_CERT_PASSWORD }}
          # … same ASC / dist-cert / keychain inputs
      - name: Configure production origin
        run: /usr/libexec/PlistBuddy -c 'Set :API_BASE_URL https://weekofus.com' ios/CommonWeek/Info.plist
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: ios/CommonWeek.xcodeproj
          scheme: CommonWeek
          extra-build-settings: |
            PROVISIONING_PROFILE_SPECIFIER=@MAC_PROFILE_UUID:app@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

Behavior changes: stricter altool log check; certificate DER now extracted from
the `.p12` (with `-legacy` fallback) instead of by keychain common name. Mac
`MAC_BUILD_NUMBER` is still exported.

---

## stuff (`com.jimgreco.stufftracker`)

Policy changes:

- Already uses `testerEmailSha256s`; no tester change.
- Add `"allowDottedBuildHistory": true` (the 1.x train contains one-off dotted
  builds like `240.1`; the engine default rejects them).
- Add `"allowApprovedIndividualTesters": true` to keep accepting builds that carry
  the approved testers individually (engine default: none).
- `apiOrigin: null` and `infoPlists: []` stay (a build phase stamps `ReleaseCommit`
  and reads `CI_BUILD_NUMBER`, which `prepare` exports).

```yaml
  build:
    if: (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/main' && needs.test.outputs.run_build == 'true'
    needs: test
    # skeleton …
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with: { policy: .github/scripts/release-policy.json, … }
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: ios/StuffTracker.xcodeproj
          scheme: StuffTracker
          extra-build-settings: |
            APP_STORE_PROFILE=@PROFILE_UUID:app@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

The `test` job (simulator tests) stays in stuff. Xcode selection changes from
"highest installed Xcode 26.x by glob" to the candidate list; pass
`xcode-candidates` if a newer 26.x than 26.4 must be preferred.

Behavior changes: the audience check no longer hard-codes "exactly two" testers —
the count comes from the list length; tester groups must now be internal (they are).

---

## workouts (`com.workoutplanner.ios`)

Policy changes:

- `testerEmailSha256` → `testerEmailSha256s: ["<existing value>"]`.
- Add `"allowApprovedIndividualTesters": true` to keep the old "≤ 1 approved
  individual tester" acceptance (omit it to require none, which is what planner does).
- Audience: unchanged (`APP_STORE_ELIGIBLE`, previously hard-coded).

```yaml
  build:
    if: (github.event_name == 'push' || inputs.upload_testflight) && github.ref == 'refs/heads/main'
    # skeleton …
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with: { policy: .github/scripts/release-policy.json, … }
      - name: Configure iOS app
        env:
          IOS_API_BASE_URL: ${{ secrets.IOS_API_BASE_URL }}
          GOOGLE_IOS_CLIENT_ID: ${{ secrets.GOOGLE_IOS_CLIENT_ID }}
          GOOGLE_IOS_REVERSED_CLIENT_ID: ${{ secrets.GOOGLE_IOS_REVERSED_CLIENT_ID }}
        run: |
          set -euo pipefail
          test "$IOS_API_BASE_URL" = "https://repmixburn.com/api"
          test -n "$GOOGLE_IOS_CLIENT_ID"; test -n "$GOOGLE_IOS_REVERSED_CLIENT_ID"
          PLIST=ios/WorkoutPlanner/Info.plist
          /usr/libexec/PlistBuddy -c "Set :API_BASE_URL $IOS_API_BASE_URL" "$PLIST"
          /usr/libexec/PlistBuddy -c "Set :GOOGLE_IOS_CLIENT_ID $GOOGLE_IOS_CLIENT_ID" "$PLIST"
          /usr/libexec/PlistBuddy -c "Set :GIT_COMMIT_HASH ${RELEASE_SHA:0:7}" "$PLIST"
          /usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLSchemes:0 $GOOGLE_IOS_REVERSED_CLIENT_ID" "$PLIST"
          /usr/libexec/PlistBuddy -c "Delete :ITSAppUsesNonExemptEncryption" "$PLIST" 2>/dev/null || true
          /usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool false" "$PLIST"
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        with:
          project: ios/WorkoutPlanner.xcodeproj
          scheme: WorkoutPlanner
          extra-build-settings: |
            IOS_PROFILE_NAME=@PROFILE_UUID:app@
            IOS_LIVE_ACTIVITY_PROFILE_NAME=@PROFILE_UUID:extension@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

Optional: add `"mainInfoPlist": {"API_BASE_URL": "{apiOrigin}", "GIT_COMMIT_HASH": "{sha7}"}`
to also verify the stamped commit hash in the artifact.

---

## daily-checklist / Ritual (`com.jimgreco.dailychecklist`)

Policy changes:

- `testerEmailSha256` → `testerEmailSha256s: ["<existing value>"]`.
- `buildAudience: "APP_STORE_ELIGIBLE"` may stay or be dropped (it is the default
  now; the old code defaulted to `INTERNAL_ONLY` when absent).
- Add `"allowApprovedIndividualTesters": true` to keep the old "≤ 1 approved
  individual tester" acceptance.

```yaml
  testflight:
    if: (github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && inputs.publish_testflight)) && github.ref == 'refs/heads/main'
    environment: production
    # skeleton …; keep the existing simulator + unit-test steps before prepare
      - uses: jimgreco/release-tools/ios/prepare@RELEASE_TOOLS_SHA
        with: { policy: .github/scripts/release-policy.json, … }
      - uses: jimgreco/release-tools/ios/upload@RELEASE_TOOLS_SHA
        env:
          API_BASE_URL: ${{ secrets.IOS_API_BASE_URL }}
          GOOGLE_CLIENT_ID: ${{ secrets.GOOGLE_IOS_CLIENT_ID }}
          GOOGLE_REVERSED_CLIENT_ID: ${{ secrets.GOOGLE_IOS_REVERSED_CLIENT_ID }}
        with:
          project: Daily.xcodeproj
          scheme: Daily
          extra-build-settings: |
            IOS_PROFILE_NAME=@PROFILE_UUID:app@
            IOS_WIDGET_PROFILE_NAME=@PROFILE_UUID:extension@
            GIT_COMMIT_HASH=@GIT_SHA7@
            API_BASE_URL=@ENV:API_BASE_URL@
            GOOGLE_CLIENT_ID=@ENV:GOOGLE_CLIENT_ID@
            GOOGLE_REVERSED_CLIENT_ID=@ENV:GOOGLE_REVERSED_CLIENT_ID@
      - uses: jimgreco/release-tools/ios/cleanup@RELEASE_TOOLS_SHA
        if: always()
```

Keep the existing "Validate release configuration" step (origin/Google id format
checks) before `prepare`; the artifact check additionally enforces
`API_BASE_URL == apiOrigin` (`https://ritualcue.com`). No configure-plist step.
