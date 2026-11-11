#!/usr/bin/env node
// Existing Apple resources only. This client intentionally exposes GET only.
// The app's release policy is read from --policy or RELEASE_POLICY.
import { createHash, createPrivateKey, sign, X509Certificate } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export const digest = value => createHash('sha256').update(value).digest('hex');
const check = (ok, message) => { if (!ok) throw new Error(message); };
const validUntil = (value, now) => typeof value === 'string' && Number.isFinite(Date.parse(value)) && Date.parse(value) > now + 3600000;
const origin = 'https://api.appstoreconnect.apple.com';
const HEX64 = /^[a-f0-9]{64}$/;
export const AUDIENCES = ['APP_STORE_ELIGIBLE', 'INTERNAL_ONLY'];
export const DEFAULT_AUDIENCE = 'APP_STORE_ELIGIBLE';
export const buildAudience = p => p.buildAudience ?? DEFAULT_AUDIENCE;

// iOS ships an IPA from the IOS train; Mac Catalyst ships a pkg from the MAC_OS
// train. Both use the same app record, certificate and Git-count build number.
export const platforms = {
  ios: { label: 'iOS', train: 'IOS', profileType: 'IOS_APP_STORE', profilePlatforms: ['IOS'], extension: 'mobileprovision', profiles: p => p.profiles },
  mac: { label: 'Mac', train: 'MAC_OS', profileType: 'MAC_CATALYST_APP_STORE', profilePlatforms: ['IOS', 'MAC_OS', 'UNIVERSAL'], extension: 'provisionprofile', profiles: p => p.macProfiles },
};
const platformFor = name => { const value = platforms[name]; check(value, 'Unknown release platform.'); return value; };

// Mirrors verify-release.py validate_policy for the fields this client reads.
export function validatePolicy(p) {
  check(p && typeof p === 'object' && !Array.isArray(p), 'Release policy must be a JSON object.');
  check(!Object.hasOwn(p, 'testerEmailSha256'), 'Legacy testerEmailSha256 is not accepted; use testerEmailSha256s (a list).');
  const missing = ['appId', 'bundleId', 'teamId', 'certificateSha1', 'certificateSha256', 'profiles', 'infoPlists', 'configuredPlistKeys', 'marketingVersion', 'groups', 'testerEmailSha256s'].filter(k => !Object.hasOwn(p, k));
  check(!missing.length, `Release policy is missing required keys: ${missing.join(', ')}`);
  check(typeof p.appId === 'string' && /^[0-9]+$/.test(p.appId), 'appId must be the numeric App Store Connect app id string.');
  check(typeof p.teamId === 'string' && /^[A-Z0-9]{10}$/.test(p.teamId), 'teamId must be a 10-character team identifier.');
  check(/^[A-F0-9]{40}$/.test(p.certificateSha1) && HEX64.test(p.certificateSha256), 'Certificate pins are malformed.');
  check(AUDIENCES.includes(buildAudience(p)), 'buildAudience must be APP_STORE_ELIGIBLE or INTERNAL_ONLY.');
  const pins = p.testerEmailSha256s;
  check(Array.isArray(pins) && pins.length > 0 && new Set(pins).size === pins.length && pins.every(h => typeof h === 'string' && HEX64.test(h)),
    'testerEmailSha256s must be a non-empty list of distinct sha256 hex digests.');
  check(Array.isArray(p.groups) && p.groups.length > 0, 'groups must list every existing TestFlight group.');
  for (const g of p.groups) {
    check(g && typeof g.id === 'string' && g.id && typeof g.name === 'string' && typeof g.internal === 'boolean'
      && typeof g.automatic === 'boolean' && Number.isInteger(g.testers) && g.testers >= 0, 'Each group needs id, name, internal, testers and automatic.');
    if (g.testers) check(g.internal && g.testers === pins.length, 'Only internal groups may hold testers, and each must hold exactly the approved testers.');
  }
  check(new Set(p.groups.map(g => g.id)).size === p.groups.length, 'Duplicate group ids.');
  check(p.groups.some(g => g.testers > 0), 'At least one internal group must hold the approved testers.');
  for (const h of p.excludedHistoricalTesters ?? []) check(h && typeof h.id === 'string' && h.id && HEX64.test(h.emailSha256 ?? ''), 'excludedHistoricalTesters entries need id and emailSha256.');
  for (const key of ['profiles', 'macProfiles']) {
    if (!Object.hasOwn(p, key)) continue;
    check(p[key] && typeof p[key] === 'object' && p[key].app, `${key} must contain an "app" entry.`);
    for (const [name, t] of Object.entries(p[key])) {
      check(/^[A-Za-z0-9_-]+$/.test(name) && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(t?.uuid ?? '') && typeof t.name === 'string' && t.name
        && typeof t.bundleId === 'string' && t.bundleId, `${key}.${name} needs a lowercase uuid, name and bundleId.`);
    }
    check(p[key].app.bundleId === p.bundleId, `${key}.app.bundleId must equal bundleId.`);
  }
  for (const flag of ['allowDottedBuildHistory', 'allowApprovedIndividualTesters']) check(p[flag] === undefined || typeof p[flag] === 'boolean', `${flag} must be boolean.`);
  return p;
}
export function loadPolicy(path = process.env.RELEASE_POLICY) {
  check(path, 'Release policy path is required (--policy or RELEASE_POLICY).');
  return validatePolicy(JSON.parse(readFileSync(path, 'utf8')));
}

export function token(env = process.env) {
  const kid = env.APP_STORE_CONNECT_KEY_ID, iss = env.APP_STORE_CONNECT_ISSUER_ID, path = env.APP_STORE_CONNECT_API_KEY_PATH;
  check(kid && iss && path, 'Existing app-specific App Store Connect authentication is required.');
  const now = Math.floor(Date.now() / 1000);
  const b64 = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const input = `${b64({ alg: 'ES256', kid, typ: 'JWT' })}.${b64({ iss, aud: 'appstoreconnect-v1', iat: now, exp: now + 600 })}`;
  try {
    return `${input}.${sign('sha256', Buffer.from(input), { key: createPrivateKey(readFileSync(path)), dsaEncoding: 'ieee-p1363' }).toString('base64url')}`;
  } catch { throw new Error('Existing app-specific API authentication could not be loaded.'); }
}
// Reads are GET-only, so retrying a dropped connection, rate limit or Apple
// server error is safe. Client errors such as 401/403/404 are never retried.
const retryable = status => status === 429 || status >= 500;
export function readClient(fetcher = fetch, auth = token, wait = ms => new Promise(resolve => setTimeout(resolve, ms)), attempts = 5) {
  async function get(path) {
    const url = new URL(path, `${origin}/v1/`);
    check(url.origin === origin && url.pathname.startsWith('/v1/') && !url.username && !url.password && !url.hash, 'Untrusted Apple read URL.');
    for (let attempt = 1; ; attempt++) {
      let response;
      try { response = await fetcher(url.href, { method: 'GET', redirect: 'error', signal: AbortSignal.timeout(30000), headers: { Authorization: `Bearer ${auth()}`, Accept: 'application/json' } }); }
      catch {
        if (attempt < attempts) { await wait(2000 * 2 ** (attempt - 1)); continue; }
        throw new Error(`Apple read failed after ${attempts} attempts at ${url.pathname}; no changes attempted.`);
      }
      if (!response.ok && retryable(response.status) && attempt < attempts) { await wait(2000 * 2 ** (attempt - 1)); continue; }
      check(response.ok, `Apple read rejected (${response.status}) at ${url.pathname}; no changes attempted.`);
      try { return await response.json(); }
      catch {
        if (attempt < attempts) { await wait(2000 * 2 ** (attempt - 1)); continue; }
        throw new Error(`Apple read returned an unreadable response at ${url.pathname}; no changes attempted.`);
      }
    }
  }
  async function list(path) {
    const rows = [], seen = new Set();
    while (path) {
      check(typeof path === 'string' && !seen.has(path) && seen.size < 100, 'Invalid Apple pagination.');
      seen.add(path);
      const response = await get(path);
      check(Array.isArray(response.data), 'Missing Apple resource collection.');
      rows.push(...response.data); path = response.links?.next;
    }
    check(new Set(rows.map(row => row.id)).size === rows.length, 'Duplicate Apple resource identifiers.');
    return rows;
  }
  return { get, list };
}

export function validateCertificate(resource, der, now, p) {
  const a = resource?.attributes;
  check(resource?.type === 'certificates' && ['DISTRIBUTION', 'IOS_DISTRIBUTION'].includes(a?.certificateType)
    && a.activated !== false && validUntil(a.expirationDate, now), 'Existing distribution certificate is unavailable, inactive, or expired.');
  check(typeof a.certificateContent === 'string' && digest(Buffer.from(a.certificateContent, 'base64')) === p.certificateSha256
    && digest(der) === p.certificateSha256, 'Signing certificate differs from the approved identity.');
}
export function validateProfile(resource, target, now = Date.now(), platform = 'ios') {
  const a = resource?.attributes, kind = platformFor(platform);
  check(resource?.type === 'profiles' && a?.uuid?.toLowerCase() === target.uuid && a.name === target.name
    && a.profileType === kind.profileType && a.profileState === 'ACTIVE' && kind.profilePlatforms.includes(a.platform)
    && validUntil(a.expirationDate, now), `Pinned existing ${kind.label} profile is missing, changed, inactive, or expired: ` + JSON.stringify({ name: a?.name, profileType: a?.profileType, platform: a?.platform, state: a?.profileState }));
}
export function validateBundle(resource, target, p) {
  const a = resource?.attributes;
  // Apple BundleIdPlatform includes UNIVERSAL. The pinned profile itself must
  // still be the expected type; decoded profile and certificate prove the team.
  // seedId is optional metadata; if supplied it must match the pinned prefix.
  check(resource?.type === 'bundleIds' && a?.identifier === target.bundleId
    && ['IOS', 'UNIVERSAL'].includes(a.platform) && (a.seedId == null || a.seedId === p.teamId),
    'Existing profile bundle metadata differs: ' + JSON.stringify({ type: resource?.type, identifier: a?.identifier, platform: a?.platform, seedId: a?.seedId }));
}
export async function existingProfile(client, name, der, now, p, platform = 'ios') {
  const target = platformFor(platform).profiles(p)?.[name]; check(target, 'Target outside the approved release.');
  const rows = await client.list(`/v1/profiles?filter[name]=${encodeURIComponent(target.name)}&fields[profiles]=name,uuid&limit=200`);
  const matches = rows.filter(row => row.attributes?.uuid?.toLowerCase() === target.uuid);
  check(matches.length === 1, 'Exactly one pinned existing profile is required; no profile will be created. ' + JSON.stringify({ pinnedUuid: target.uuid, pinnedName: target.name, sameNameProfiles: rows.map(row => ({ id: row.id, uuid: row.attributes?.uuid })) }));
  const id = encodeURIComponent(matches[0].id);
  const profile = (await client.get(`/v1/profiles/${id}`)).data;
  validateProfile(profile, target, now, platform);
  const bundle = (await client.get(`/v1/profiles/${id}/bundleId`)).data;
  validateBundle(bundle, target, p);
  const certificates = await client.list(`/v1/profiles/${id}/certificates?limit=200`);
  check(certificates.length === 1, 'Profile must contain exactly the approved signing certificate.');
  // Read the certificate resource now: a stale cached profile alone is insufficient.
  const certificate = (await client.get(`/v1/certificates/${encodeURIComponent(certificates[0].id)}`)).data;
  validateCertificate(certificate, der, now, p);
  const cert = new X509Certificate(der);
  check(validUntil(cert.validTo, now) && Date.parse(cert.validFrom) <= now && !cert.ca
    && cert.subject.split('\n').includes(`OU=${p.teamId}`), 'Local public signing certificate is invalid.');
  check(typeof profile.attributes.profileContent === 'string' && profile.attributes.profileContent.length > 0, 'Profile content unavailable.');
  return Buffer.from(profile.attributes.profileContent, 'base64');
}
const emailDigest = tester => digest(tester.attributes.email.trim().toLowerCase());
export function isApprovedTester(tester, p) {
  return tester?.type === 'betaTesters' && typeof tester.attributes?.email === 'string'
    && p.testerEmailSha256s.includes(emailDigest(tester))
    && tester.attributes.inviteType !== 'PUBLIC_LINK';
}
// Every tester is approved, with distinct record ids and distinct identities.
function distinctApprovedTesters(testers, p) {
  return testers.every(t => typeof t.id === 'string' && t.id && isApprovedTester(t, p))
    && new Set(testers.map(t => t.id)).size === testers.length
    && new Set(testers.map(emailDigest)).size === testers.length;
}
export async function audience(client, p) {
  validatePolicy(p);
  const app = (await client.get(`/v1/apps/${p.appId}`)).data;
  check(app?.type === 'apps' && app.id === p.appId && app.attributes?.bundleId === p.bundleId, 'App Store app identity differs.');
  const groups = await client.list(`/v1/apps/${p.appId}/betaGroups?limit=200`);
  check(groups.length === p.groups.length && groups.every(g => p.groups.some(e => e.id === g.id)), 'TestFlight groups changed.');
  const approvedIds = new Set(), groupedTesterIds = new Set();
  for (const expected of p.groups) {
    const group = groups.find(g => g.id === expected.id), a = group.attributes;
    // Automatic Xcode distribution applies to internal groups. Apple returns
    // null for an excluded external group's optional all-builds attribute.
    const automaticMatches = expected.internal ? a?.hasAccessToAllBuilds === expected.automatic
      : expected.automatic === false && !!a && Object.hasOwn(a, 'hasAccessToAllBuilds') && [false, null].includes(a.hasAccessToAllBuilds);
    check(group.type === 'betaGroups' && a?.name === expected.name && a.isInternalGroup === expected.internal
      && Object.hasOwn(a, 'publicLinkEnabled') && [false, null].includes(a.publicLinkEnabled)
      && !a.publicLink && !a.publicLinkId && automaticMatches, 'TestFlight group scope or automatic distribution changed: ' + JSON.stringify({ id: group.id, name: a?.name, internal: a?.isInternalGroup, publicLinkEnabledPresent: !!a && Object.hasOwn(a, 'publicLinkEnabled'), publicLinkEnabled: a?.publicLinkEnabled, hasPublicLink: !!a?.publicLink, hasPublicLinkId: !!a?.publicLinkId, automatic: a?.hasAccessToAllBuilds, expected }));
    const testers = await client.list(`/v1/betaGroups/${expected.id}/betaTesters?limit=200`);
    for (const tester of testers) groupedTesterIds.add(tester.id);
    check(testers.length === expected.testers, 'TestFlight tester count changed.');
    if (expected.testers) {
      check(expected.internal && testers.length === p.testerEmailSha256s.length && distinctApprovedTesters(testers, p), 'TestFlight tester identity changed.');
      for (const tester of testers) approvedIds.add(tester.id);
    }
    if (!expected.internal) check((await client.list(`/v1/betaGroups/${expected.id}/builds?limit=200`)).length === 0, 'Excluded external group has builds.');
  }
  // Every tester-holding group must hold the same approved records.
  check(approvedIds.size === p.testerEmailSha256s.length, 'Approved tester records differ between groups.');
  const all = await client.list(`/v1/betaTesters?filter[apps]=${p.appId}&limit=200`);
  // App-wide history includes former testers; it is not a receiving group.
  // Only explicitly pinned historical records may remain, outside every group.
  const excluded = p.excludedHistoricalTesters ?? [];
  const matchesHistory = (t, h) => t?.type === 'betaTesters' && t.id === h.id
    && typeof t.attributes?.email === 'string' && emailDigest(t) === h.emailSha256
    && t.attributes.inviteType === 'EMAIL' && !groupedTesterIds.has(t.id);
  const approved = all.filter(t => approvedIds.has(t.id));
  check(all.length === approvedIds.size + excluded.length && approved.length === approvedIds.size && distinctApprovedTesters(approved, p)
    && excluded.every(h => all.some(t => matchesHistory(t, h)))
    && all.every(t => approvedIds.has(t.id) || excluded.some(h => matchesHistory(t, h))),
    'App-level tester history or receiving audience changed: ' + JSON.stringify({ count: all.length, testers: all.map(t => ({ id: t.id, approvedIdentity: isApprovedTester(t, p), grouped: groupedTesterIds.has(t.id) })) }));
  return { appId: p.appId, internalTesters: approvedIds.size, externalTesters: 0, excludedHistoricalTesters: excluded.length };
}
export async function builds(client, p, platform = 'ios') {
  const train = platformFor(platform).train;
  const trains = await client.list(`/v1/preReleaseVersions?filter[app]=${p.appId}&filter[platform]=${train}&limit=200`);
  const rows = [];
  for (const t of trains) {
    check(t.type === 'preReleaseVersions' && t.attributes?.platform === train, 'Unexpected version-train platform.');
    for (const row of await client.list(`/v1/preReleaseVersions/${encodeURIComponent(t.id)}/builds?limit=200`)) rows.push({ ...row, marketingVersion: t.attributes.version });
  }
  return rows;
}
export function releaseIdentity(build, sha) {
  check(/^[1-9][0-9]*$/.test(String(build)) && /^[0-9a-f]{40}$/.test(sha), 'Positive build number and full source SHA required.');
}
// Leading commit count of an existing build, or null if the metadata is not accepted.
export function historicalBuildCount(version, p) {
  const pattern = p.allowDottedBuildHistory ? /^[1-9][0-9]*(\.[0-9]+){0,2}$/ : /^[1-9][0-9]*$/;
  return typeof version === 'string' && pattern.test(version) ? BigInt(version.split('.')[0]) : null;
}
export async function preflight(client, build, sha, p, platform = 'ios') {
  releaseIdentity(build, sha);
  const scope = await audience(client, p);
  // Apple's build identity is scoped by app, platform and marketing-version train.
  // Historical 1.0 releases used eight-digit numbers; newer trains use Git counts.
  const existing = (await builds(client, p, platform)).filter(b => b.marketingVersion === p.marketingVersion);
  const rejected = existing.filter(b => {
    const count = historicalBuildCount(b.attributes?.version, p);
    return b.type !== 'builds' || count === null || count >= BigInt(build);
  });
  check(!rejected.length, `${platformFor(platform).label} build preflight rejected existing metadata: ` + JSON.stringify(rejected.slice(0, 10).map(b => ({ type: b.type, build: b.attributes?.version, marketingVersion: b.marketingVersion }))) + `; total rejected=${rejected.length}.`);
  return { ...scope, platform, audience: buildAudience(p), sha, build: String(build), checkedAt: new Date().toISOString(), existingBuilds: existing.length };
}
export async function verifyUploaded(client, build, sha, p, platform = 'ios') {
  releaseIdentity(build, sha); await audience(client, p);
  const expectedAudience = buildAudience(p);
  const matches = (await builds(client, p, platform)).filter(b => b.marketingVersion === p.marketingVersion && b.attributes?.version === String(build));
  check(matches.length <= 1, 'Uploaded build identity is ambiguous.');
  if (!matches.length || matches[0].attributes.processingState === 'PROCESSING') return null;
  const b = matches[0];
  check(b.type === 'builds' && b.marketingVersion === p.marketingVersion && b.attributes.processingState === 'VALID'
    && b.attributes.expired === false && b.attributes.buildAudienceType === expectedAudience,
    `Uploaded build failed processing or is not the intended ${expectedAudience} ${platformFor(platform).label} version.`);
  // Apple supports GET betaGroups/{id}/builds, not GET builds/{id}/betaGroups.
  // audience() above already proved the complete app group set is exactly pinned.
  const attached = [];
  for (const group of p.groups) {
    const groupBuilds = await client.list(`/v1/betaGroups/${group.id}/builds?limit=200`);
    if (groupBuilds.some(row => row.id === b.id)) attached.push(group.id);
  }
  check(attached.every(id => p.groups.some(e => e.internal && e.id === id)), 'Uploaded build has an unapproved group.');
  const testers = await client.list(`/v1/builds/${encodeURIComponent(b.id)}/individualTesters?limit=200`);
  if (p.allowApprovedIndividualTesters) {
    check(testers.length <= p.testerEmailSha256s.length && distinctApprovedTesters(testers, p), 'Uploaded build has an unapproved individual tester.');
  } else {
    check(testers.length === 0, 'Uploaded build must have no individual testers; approved testers receive through the approved internal group.');
  }
  if (!p.groups.filter(e => e.testers > 0).every(e => attached.includes(e.id))) return null;
  return { platform, sha, build: String(build), appId: p.appId, processing: 'VALID', audience: expectedAudience, approvedGroupAttached: true };
}
// Migration helper: report the existing app record and TestFlight groups for a
// bundle id, using GET only, so they can be pinned in the policy. Refuses any
// audience broader than the approved internal testers. Never changes anything.
export async function discoverAudience(client, p) {
  const apps = await client.list(`/v1/apps?filter[bundleId]=${encodeURIComponent(p.bundleId)}&limit=200`);
  check(apps.length === 1 && apps[0].type === 'apps' && apps[0].attributes?.bundleId === p.bundleId, 'Exactly one existing App Store record is required.');
  const appId = apps[0].id, groups = [];
  for (const group of await client.list(`/v1/apps/${appId}/betaGroups?limit=200`)) {
    const a = group.attributes;
    check(group.type === 'betaGroups' && typeof group.id === 'string' && typeof a?.name === 'string' && typeof a.isInternalGroup === 'boolean'
      && !a.publicLinkEnabled && !a.publicLink, 'Incomplete or public existing group metadata.');
    const testers = await client.list(`/v1/betaGroups/${group.id}/betaTesters?limit=200`);
    check(testers.length === 0 || (a.isInternalGroup && testers.length === p.testerEmailSha256s.length && distinctApprovedTesters(testers, p)),
      'Existing audience is not exactly the approved internal testers; no group will be changed.');
    groups.push({ id: group.id, name: a.name, internal: a.isInternalGroup, testers: testers.length, automatic: a.isInternalGroup ? a.hasAccessToAllBuilds === true : false });
  }
  return { appId, groups };
}

function optionalArg(argv, name, fallback) {
  const i = argv.indexOf(`--${name}`); return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
}
function arg(argv, name) {
  const i = argv.indexOf(`--${name}`); check(i >= 0 && argv[i + 1], `Missing --${name}.`); return argv[i + 1];
}
function gitIdentity(sha, build) {
  check(execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim() === sha, 'Checkout SHA changed.');
  check(execFileSync('git', ['rev-list', '--count', 'HEAD'], { encoding: 'utf8' }).trim() === build, 'Build does not match full Git history.');
}
async function main(argv = process.argv) {
  const mode = argv[2], platform = optionalArg(argv, 'platform', 'ios'), kind = platformFor(platform);
  if (mode === 'discover-audience') {
    // Only bundleId and testerEmailSha256s are needed before appId/groups are known.
    const raw = JSON.parse(readFileSync(optionalArg(argv, 'policy', process.env.RELEASE_POLICY), 'utf8'));
    check(typeof raw.bundleId === 'string' && Array.isArray(raw.testerEmailSha256s) && raw.testerEmailSha256s.length, 'bundleId and testerEmailSha256s are required.');
    console.log(JSON.stringify(await discoverAudience(readClient(), raw), null, 2));
    return;
  }
  const policy = loadPolicy(optionalArg(argv, 'policy', process.env.RELEASE_POLICY));
  if (mode === 'validate-policy') {
    if (platform === 'mac') check(policy.macProfiles, 'Release policy has no macProfiles.');
    console.log(JSON.stringify({ appId: policy.appId, buildAudience: buildAudience(policy) }));
    return;
  }
  const client = readClient();
  if (mode === 'profiles') {
    const der = readFileSync(arg(argv, 'certificate-der')), output = arg(argv, 'directory');
    const targets = kind.profiles(policy);
    check(targets, `Release policy has no profiles for ${platform}.`);
    for (const name of Object.keys(targets)) {
      const content = await existingProfile(client, name, der, Date.now(), policy, platform);
      writeFileSync(`${output}/${name}.${kind.extension}`, content, { mode: 0o600, flag: 'wx' });
    }
    console.log('Approved existing profiles retrieved without Apple resource changes.');
  } else if (mode === 'preflight') {
    const sha = arg(argv, 'sha'), build = arg(argv, 'build');
    gitIdentity(sha, build);
    console.log(JSON.stringify(await preflight(client, build, sha, policy, platform)));
  } else if (mode === 'verify-upload') {
    const sha = arg(argv, 'sha'), build = arg(argv, 'build');
    for (let attempt = 0; attempt < 40; attempt++) {
      const result = await verifyUploaded(client, build, sha, policy, platform);
      if (result) { console.log(JSON.stringify(result)); return; }
      await new Promise(resolve => setTimeout(resolve, 15000));
    }
    throw new Error('Upload occurred but processing/group availability is unverified. Inspect Apple before any retry.');
  } else throw new Error('Expected validate-policy, profiles, preflight, verify-upload, or discover-audience.');
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().catch(error => { console.error(error.message); process.exitCode = 1; });
