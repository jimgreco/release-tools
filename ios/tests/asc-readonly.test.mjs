import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import {
  digest, readClient, audience, preflight, verifyUploaded, validateCertificate, validateProfile, validateBundle,
  releaseIdentity, validatePolicy, loadPolicy, discoverAudience, historicalBuildCount, buildAudience,
} from '../scripts/asc-readonly.mjs';

const clone = value => structuredClone(value);
const fixturesDir = new URL('./fixtures/', import.meta.url);
const load = name => JSON.parse(readFileSync(new URL(`${name}.json`, fixturesDir), 'utf8'));
const FIXTURES = readdirSync(fixturesDir).filter(f => f.endsWith('.json')).map(f => f.slice(0, -5));
const EMAILS = ['Owner-One@Example.Invalid', 'tester-two@example.invalid'];
const SHA = 'a'.repeat(40);

function fixture(name = 'catalyst') {
  const p = load(name);
  const testers = p.testerEmailSha256s.map((hash, i) => {
    assert.equal(digest(EMAILS[i].toLowerCase()), hash, 'fixture tester pins use the synthetic emails');
    return { type: 'betaTesters', id: `tester-${i}`, attributes: { email: EMAILS[i], inviteType: 'EMAIL' } };
  });
  const historical = (p.excludedHistoricalTesters ?? []).map(h => ({ type: 'betaTesters', id: h.id, attributes: { email: 'former@example.invalid', inviteType: 'EMAIL' } }));
  const routes = {
    [`/v1/apps/${p.appId}`]: { data: { type: 'apps', id: p.appId, attributes: { bundleId: p.bundleId } } },
    [`/v1/apps/${p.appId}/betaGroups?limit=200`]: { data: p.groups.map(g => ({ type: 'betaGroups', id: g.id, attributes: { name: g.name, isInternalGroup: g.internal, publicLinkEnabled: false, hasAccessToAllBuilds: g.automatic } })) },
    [`/v1/betaTesters?filter[apps]=${p.appId}&limit=200`]: { data: [...testers, ...historical].map(clone) },
    [`/v1/preReleaseVersions?filter[app]=${p.appId}&filter[platform]=IOS&limit=200`]: { data: [{ type: 'preReleaseVersions', id: 'ios-train', attributes: { platform: 'IOS', version: p.marketingVersion } }] },
    '/v1/preReleaseVersions/ios-train/builds?limit=200': { data: [{ type: 'builds', id: 'previous', attributes: { version: '1' } }] },
  };
  for (const group of p.groups) {
    routes[`/v1/betaGroups/${group.id}/betaTesters?limit=200`] = { data: group.testers ? testers.map(clone) : [] };
    routes[`/v1/betaGroups/${group.id}/builds?limit=200`] = { data: [] };
  }
  const requested = [];
  const client = {
    async get(path) { requested.push(path); assert.ok(routes[path], `Unexpected API path: ${path}`); return clone(routes[path]); },
    async list(path) { return (await this.get(path)).data; },
  };
  const testerGroup = p.groups.find(g => g.testers > 0);
  return { p, routes, client, testers, historical, testerGroup, requested };
}
function uploaded(f, attributes = {}, train = 'ios-train', id = 'uploaded') {
  f.routes[`/v1/preReleaseVersions/${train}/builds?limit=200`] = { data: [{ type: 'builds', id, attributes: { version: '3', processingState: 'VALID', expired: false, buildAudienceType: buildAudience(f.p), ...attributes } }] };
  for (const g of f.p.groups.filter(g => g.testers)) f.routes[`/v1/betaGroups/${g.id}/builds?limit=200`] = { data: [{ id, type: 'builds' }] };
  f.routes[`/v1/builds/${id}/individualTesters?limit=200`] = { data: [] };
  return id;
}

// ---- Policy schema ----
test('every fixture policy validates', () => {
  for (const name of FIXTURES) validatePolicy(load(name));
});
test('policy loads from RELEASE_POLICY path only', () => {
  const path = new URL('catalyst.json', fixturesDir).pathname;
  assert.equal(loadPolicy(path).appId, load('catalyst').appId);
  assert.throws(() => loadPolicy(undefined), /RELEASE_POLICY/);
});
test('legacy singular tester pin is rejected with a migration hint', () => {
  const p = load('catalyst'); p.testerEmailSha256 = p.testerEmailSha256s[0];
  assert.throws(() => validatePolicy(p), /testerEmailSha256s/);
});
for (const key of ['appId', 'bundleId', 'teamId', 'certificateSha1', 'certificateSha256', 'profiles', 'infoPlists', 'configuredPlistKeys', 'marketingVersion', 'groups', 'testerEmailSha256s']) {
  test(`policy requires ${key}`, () => {
    const p = load('app-and-extension'); delete p[key];
    assert.throws(() => validatePolicy(p), new RegExp(key));
  });
}
test('malformed or duplicate tester pins fail closed', () => {
  for (const pins of [undefined, [], ['x'], [digest('a'), digest('a')], 'abc']) {
    const p = load('plural-testers'); p.testerEmailSha256s = pins;
    assert.throws(() => validatePolicy(p), /testerEmailSha256s/);
  }
});
test('policy groups must be internal when holding testers and hold every approved tester', () => {
  const p = load('plural-testers'); p.groups[0].testers = 1;
  assert.throws(() => validatePolicy(p), /exactly the approved testers/);
  const q = load('catalyst'); q.groups[1].testers = 1;
  assert.throws(() => validatePolicy(q), /Only internal groups/);
  const r = load('catalyst'); r.groups[0].testers = 0;
  assert.throws(() => validatePolicy(r), /At least one internal group/);
});
test('buildAudience defaults to APP_STORE_ELIGIBLE and accepts only known values', () => {
  assert.equal(buildAudience(load('catalyst')), 'APP_STORE_ELIGIBLE');
  assert.equal(buildAudience(load('single-app-internal-only')), 'INTERNAL_ONLY');
  const p = load('catalyst'); p.buildAudience = 'EXTERNAL';
  assert.throws(() => validatePolicy(p), /buildAudience/);
});
test('profile pins must be lowercase UUIDs and the app profile must be the app bundle', () => {
  const p = load('catalyst'); p.profiles.app.uuid = p.profiles.app.uuid.toUpperCase();
  assert.throws(() => validatePolicy(p), /lowercase uuid/);
  const q = load('catalyst'); q.profiles.app.bundleId = 'com.other';
  assert.throws(() => validatePolicy(q), /bundleId/);
});

// ---- GET-only client ----
test('Apple client permits only same-origin GET and refuses redirects', async () => {
  const requests = [];
  const client = readClient(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ data: [] }) };
  }, () => 'synthetic-token');
  await client.list('/v1/apps');
  assert.equal(requests[0].options.method, 'GET'); assert.equal(requests[0].options.redirect, 'error');
  assert.equal(Object.hasOwn(requests[0].options, 'body'), false);
  for (const url of ['https://example.invalid/v1/apps', 'http://api.appstoreconnect.apple.com/v1/apps', 'https://user:password@api.appstoreconnect.apple.com/v1/apps', '/v2/apps', '/v1/apps#fragment']) {
    await assert.rejects(() => client.get(url));
  }
  assert.equal(requests.length, 1);
});
test('the engine source contains no Apple write verbs', () => {
  const source = readFileSync(new URL('../scripts/asc-readonly.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /method:\s*'(POST|PATCH|PUT|DELETE)'/);
  assert.equal((source.match(/method:/g) ?? []).length, 1);
});
test('pagination checks every page and fails on loops or duplicate IDs', async () => {
  let count = 0;
  const client = readClient(async () => ({ ok: true, json: async () => ++count === 1
    ? { data: [{ id: 'a' }], links: { next: '/v1/page2' } } : { data: [{ id: 'b' }] } }), () => 'fake');
  assert.equal((await client.list('/v1/page1')).length, 2);
  const loop = readClient(async () => ({ ok: true, json: async () => ({ data: [], links: { next: '/v1/loop' } }) }), () => 'fake');
  await assert.rejects(() => loop.list('/v1/loop'), /pagination/);
  const duplicates = readClient(async () => ({ ok: true, json: async () => ({ data: [{ id: 'a' }, { id: 'a' }] }) }), () => 'fake');
  await assert.rejects(() => duplicates.list('/v1/apps'), /Duplicate/);
});
test('cross-origin next-page URL never receives authorization', async () => {
  let requests = 0;
  const client = readClient(async () => { requests++; return { ok: true, json: async () => ({ data: [], links: { next: 'https://example.invalid/v1/stolen' } }) }; }, () => 'fake');
  await assert.rejects(() => client.list('/v1/apps')); assert.equal(requests, 1);
});
test('API errors do not emit private response contents', async () => {
  const client = readClient(async () => ({ ok: false, status: 401, text: async () => 'private-response' }), () => 'fake');
  await assert.rejects(() => client.get('/v1/apps'), error => error.message.includes('401') && !error.message.includes('private-response'));
});
test('transient Apple read failures are retried; client errors are not', async () => {
  const waits = [];
  let calls = 0;
  const flaky = readClient(async () => {
    calls++;
    if (calls === 1) throw new Error('socket hang up');
    if (calls === 2) return { ok: false, status: 503 };
    if (calls === 3) return { ok: false, status: 429 };
    return { ok: true, json: async () => ({ data: { id: 'app' } }) };
  }, () => 'fake', async ms => waits.push(ms));
  assert.deepEqual(await flaky.get('/v1/apps/1'), { data: { id: 'app' } });
  assert.deepEqual(waits, [2000, 4000, 8000]);
  for (const status of [401, 403, 404]) {
    let tries = 0;
    const denied = readClient(async () => { tries++; return { ok: false, status }; }, () => 'fake', async () => {});
    await assert.rejects(() => denied.get('/v1/apps'), new RegExp(String(status)));
    assert.equal(tries, 1);
  }
  let down = 0;
  const outage = readClient(async () => { down++; throw new Error('offline'); }, () => 'fake', async () => {});
  await assert.rejects(() => outage.get('/v1/apps'), /after 5 attempts.*no changes attempted/);
  assert.equal(down, 5);
});

// ---- Audience (all policy variants) ----
for (const name of FIXTURES) {
  test(`${name}: approved audience passes`, async () => {
    const f = fixture(name), result = await audience(f.client, f.p);
    assert.equal(result.internalTesters, f.p.testerEmailSha256s.length);
    assert.equal(result.externalTesters, 0);
  });
  for (const change of ['app', 'group-id', 'extra-group', 'public', 'public-link', 'automatic', 'tester-count', 'tester-identity', 'app-tester', 'public-invite']) {
    test(`${name}: audience fails closed on ${change}`, async () => {
      const f = fixture(name), groups = f.routes[`/v1/apps/${f.p.appId}/betaGroups?limit=200`].data;
      const index = f.p.groups.indexOf(f.testerGroup);
      const testers = f.routes[`/v1/betaGroups/${f.testerGroup.id}/betaTesters?limit=200`].data;
      if (change === 'app') f.routes[`/v1/apps/${f.p.appId}`].data.attributes.bundleId = 'other.app';
      if (change === 'group-id') groups[index].id = 'changed';
      if (change === 'extra-group') groups.push(clone(groups[index]));
      if (change === 'public') groups[index].attributes.publicLinkEnabled = true;
      if (change === 'public-link') groups[index].attributes.publicLink = 'https://testflight.apple.com/join/x';
      if (change === 'automatic') groups[index].attributes.hasAccessToAllBuilds = false;
      if (change === 'tester-count') testers.push({ ...clone(testers[0]), id: 'extra' });
      if (change === 'tester-identity') testers[0].attributes.email = 'someone@example.invalid';
      if (change === 'app-tester') f.routes[`/v1/betaTesters?filter[apps]=${f.p.appId}&limit=200`].data.push({ ...f.testers[0], id: 'another' });
      if (change === 'public-invite') testers[0].attributes.inviteType = 'PUBLIC_LINK';
      await assert.rejects(() => audience(f.client, f.p));
    });
  }
  test(`${name}: empty groups cannot silently acquire testers or builds`, async () => {
    for (const group of load(name).groups.filter(g => !g.testers)) {
      const f = fixture(name); f.routes[`/v1/betaGroups/${group.id}/betaTesters?limit=200`].data.push(f.testers[0]);
      await assert.rejects(() => audience(f.client, f.p));
      if (!group.internal) {
        const b = fixture(name); b.routes[`/v1/betaGroups/${group.id}/builds?limit=200`].data.push({ id: 'unexpected' });
        await assert.rejects(() => audience(b.client, b.p));
      }
    }
  });
}
test('plural testers: group and app totals must contain every distinct pinned identity', async () => {
  for (const target of ['group', 'app']) {
    for (const change of ['missing-second', 'replacement', 'duplicate-email', 'duplicate-id', 'public-invite', 'different-app-id']) {
      const f = fixture('plural-testers');
      const path = target === 'group' ? `/v1/betaGroups/${f.p.groups[0].id}/betaTesters?limit=200` : `/v1/betaTesters?filter[apps]=${f.p.appId}&limit=200`;
      const testers = f.routes[path].data;
      if (change === 'missing-second') testers.pop();
      if (change === 'replacement') testers[1].attributes.email = 'someone@example.invalid';
      if (change === 'duplicate-email') testers[1].attributes.email = testers[0].attributes.email;
      if (change === 'duplicate-id') testers[1].id = testers[0].id;
      if (change === 'public-invite') testers[1].attributes.inviteType = 'PUBLIC_LINK';
      if (change === 'different-app-id') testers[1].id = 'another-record';
      await assert.rejects(() => audience(f.client, f.p), undefined, `${target}: ${change}`);
    }
  }
});
test('catalyst: pinned historical app records do not become receiving testers', async () => {
  const f = fixture('catalyst'), result = await preflight(f.client, '3', SHA, f.p);
  assert.equal(result.internalTesters, 1);
  assert.equal(result.excludedHistoricalTesters, 1);
  for (const group of f.p.groups) {
    const g = fixture('catalyst');
    g.routes[`/v1/betaGroups/${group.id}/betaTesters?limit=200`].data.push(g.historical[0]);
    await assert.rejects(() => audience(g.client, g.p));
  }
  for (const field of ['id', 'email', 'inviteType', 'missing']) {
    const h = fixture('catalyst'), rows = h.routes[`/v1/betaTesters?filter[apps]=${h.p.appId}&limit=200`].data;
    if (field === 'id') rows[1].id = 'different-record';
    if (field === 'email') rows[1].attributes.email = 'different@example.invalid';
    if (field === 'inviteType') rows[1].attributes.inviteType = 'PUBLIC_LINK';
    if (field === 'missing') rows.pop();
    await assert.rejects(() => audience(h.client, h.p), undefined, field);
  }
});
test('history records are refused for a policy that pins none', async () => {
  const f = fixture('app-and-extension');
  f.routes[`/v1/betaTesters?filter[apps]=${f.p.appId}&limit=200`].data.push({ type: 'betaTesters', id: 'old', attributes: { email: 'former@example.invalid', inviteType: 'EMAIL' } });
  await assert.rejects(() => audience(f.client, f.p), /App-level tester history/);
});
test('excluded external group accepts null all-builds metadata while internal distribution stays required', async () => {
  const f = fixture('catalyst'), groups = f.routes[`/v1/apps/${f.p.appId}/betaGroups?limit=200`].data;
  const external = groups.find(g => !g.attributes.isInternalGroup);
  external.attributes.hasAccessToAllBuilds = null;
  await audience(f.client, f.p);
  external.attributes.hasAccessToAllBuilds = true;
  await assert.rejects(() => audience(f.client, f.p));
  delete external.attributes.hasAccessToAllBuilds;
  await assert.rejects(() => audience(f.client, f.p));
  external.attributes.hasAccessToAllBuilds = null;
  groups.find(g => g.attributes.isInternalGroup).attributes.hasAccessToAllBuilds = null;
  await assert.rejects(() => audience(f.client, f.p));
});

// ---- Build numbers ----
test('build check accepts advancing integer commit counts only (default)', async () => {
  const f = fixture('app-and-extension'); assert.equal((await preflight(f.client, '3', SHA, f.p)).build, '3');
  for (const version of ['3', '4', '1.2', '2.1', 'unknown', undefined]) {
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].attributes.version = version;
    await assert.rejects(() => preflight(f.client, '3', SHA, f.p), undefined, String(version));
  }
});
test('allowDottedBuildHistory accepts dotted history only below the commit count', async () => {
  const f = fixture('plural-testers');
  for (const version of ['3', '4', '3.1', '1..2', '1.2.3.4', 'unknown']) {
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].attributes.version = version;
    await assert.rejects(() => preflight(f.client, '3', SHA, f.p), undefined, version);
  }
  for (const version of ['2.1', '1.2.3', '2']) {
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].attributes.version = version;
    assert.equal((await preflight(f.client, '3', SHA, f.p)).build, '3');
  }
  assert.equal(historicalBuildCount('2.1', { allowDottedBuildHistory: false }), null);
});
test('compares builds within the pinned marketing version only', async () => {
  const f = fixture('app-and-extension');
  f.routes[`/v1/preReleaseVersions?filter[app]=${f.p.appId}&filter[platform]=IOS&limit=200`].data.push({ type: 'preReleaseVersions', id: 'old', attributes: { platform: 'IOS', version: '0.9' } });
  f.routes['/v1/preReleaseVersions/old/builds?limit=200'] = { data: [{ type: 'builds', id: 'old-high', attributes: { version: '900' } }] };
  assert.equal((await preflight(f.client, '3', SHA, f.p)).build, '3');
  f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data.push({ type: 'builds', id: 'same-train-high', attributes: { version: '4' } });
  await assert.rejects(() => preflight(f.client, '3', SHA, f.p));
});
test('source and build identifiers are strict', () => {
  for (const pair of [['0', SHA], ['3', 'abc123'], ['3.1', SHA], ['3', 'A'.repeat(40)]]) assert.throws(() => releaseIdentity(...pair));
});

// ---- Signing assets ----
test('profile UUID, name, platform, state, type and expiry are pinned', () => {
  const target = load('catalyst').profiles.app, now = Date.parse('2026-10-03T00:00:00Z');
  const resource = { type: 'profiles', attributes: { uuid: target.uuid, name: target.name, profileType: 'IOS_APP_STORE', profileState: 'ACTIVE', platform: 'IOS', expirationDate: '2027-05-12T18:58:45Z' } };
  validateProfile(resource, target, now);
  for (const [key, value] of Object.entries({ uuid: 'wrong', name: 'new name', profileType: 'IOS_APP_DEVELOPMENT', profileState: 'INVALID', platform: 'MAC_OS', expirationDate: '2026-10-03T00:30:00Z' })) {
    const changed = clone(resource); changed.attributes[key] = value; assert.throws(() => validateProfile(changed, target, now));
  }
});
test('Mac profile must be the pinned Mac Catalyst App Store profile', () => {
  const target = load('catalyst').macProfiles.app, now = Date.parse('2026-11-04T00:00:00Z');
  const resource = { type: 'profiles', attributes: { uuid: target.uuid.toUpperCase(), name: target.name, profileType: 'MAC_CATALYST_APP_STORE', profileState: 'ACTIVE', platform: 'IOS', expirationDate: '2027-05-12T18:58:45Z' } };
  validateProfile(resource, target, now, 'mac');
  assert.throws(() => validateProfile(resource, target, now, 'ios'), /iOS profile/);
  for (const [key, value] of Object.entries({ profileType: 'MAC_APP_STORE', profileState: 'INVALID', uuid: 'other', expirationDate: '2026-01-01T00:00:00Z' })) {
    const changed = clone(resource); changed.attributes[key] = value; assert.throws(() => validateProfile(changed, target, now, 'mac'));
  }
});
test('certificate uses pinned public bytes and rejects expired/inactive/wrong type', () => {
  const der = Buffer.from('synthetic-public-certificate'), p = { certificateSha256: digest(der) }, now = Date.parse('2026-10-03T00:00:00Z');
  const resource = { type: 'certificates', attributes: { certificateType: 'DISTRIBUTION', certificateContent: der.toString('base64'), expirationDate: '2027-05-12T18:58:45Z' } };
  validateCertificate(resource, der, now, p);
  for (const [key, value] of Object.entries({ certificateType: 'DEVELOPMENT', activated: false, expirationDate: '2020-01-01', certificateContent: 'different' })) {
    const changed = clone(resource); changed.attributes[key] = value; assert.throws(() => validateCertificate(changed, der, now, p));
  }
  assert.throws(() => validateCertificate(resource, Buffer.from('other'), now, p));
});
test('profiles may belong to universal app IDs with optional seed metadata', () => {
  const p = load('catalyst'), target = p.profiles.app;
  for (const platform of ['IOS', 'UNIVERSAL']) {
    for (const seedId of [p.teamId, null, undefined]) validateBundle({ type: 'bundleIds', attributes: { identifier: target.bundleId, platform, seedId } }, target, p);
  }
});
test('bundle relationship rejects another app, platform or supplied team', () => {
  const p = load('catalyst'), target = p.profiles.app;
  const resource = { type: 'bundleIds', attributes: { identifier: target.bundleId, platform: 'UNIVERSAL', seedId: p.teamId } };
  for (const [key, value] of Object.entries({ identifier: 'other.app', platform: 'MAC_OS', seedId: 'OTHERTEAM' })) {
    const changed = clone(resource); changed.attributes[key] = value;
    assert.throws(() => validateBundle(changed, target, p), /metadata differs/);
  }
  assert.throws(() => validateBundle({ ...resource, type: 'profiles' }, target, p));
});

// ---- Upload verification and audience ----
for (const name of FIXTURES) {
  test(`${name}: processing success requires policy audience, version and every tester group`, async () => {
    const f = fixture(name), id = uploaded(f);
    const result = await verifyUploaded(f.client, '3', SHA, f.p);
    assert.equal(result.approvedGroupAttached, true);
    assert.equal(result.audience, buildAudience(f.p));
    const other = buildAudience(f.p) === 'INTERNAL_ONLY' ? 'APP_STORE_ELIGIBLE' : 'INTERNAL_ONLY';
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].attributes.buildAudienceType = other;
    await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p), new RegExp(buildAudience(f.p)));
    uploaded(f);
    f.routes[`/v1/betaGroups/${f.testerGroup.id}/builds?limit=200`].data = [];
    assert.equal(await verifyUploaded(f.client, '3', SHA, f.p), null);
    uploaded(f);
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].attributes.processingState = 'PROCESSING';
    assert.equal(await verifyUploaded(f.client, '3', SHA, f.p), null);
    for (const [key, value] of [['processingState', 'INVALID'], ['expired', true]]) {
      uploaded(f, { [key]: value });
      await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p));
    }
    uploaded(f);
    f.routes['/v1/preReleaseVersions/ios-train/builds?limit=200'].data[0].type = 'other';
    await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p));
    assert.ok(id);
  });
}
test('default policy refuses any individual tester on the uploaded build', async () => {
  const f = fixture('catalyst'), id = uploaded(f);
  f.routes[`/v1/builds/${id}/individualTesters?limit=200`].data = [f.testers[0]];
  await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p), /no individual testers/);
});
test('allowApprovedIndividualTesters permits only a distinct subset of approved testers', async () => {
  const f = fixture('plural-testers'), id = uploaded(f), path = `/v1/builds/${id}/individualTesters?limit=200`;
  const [owner, second] = f.testers;
  for (const testers of [[], [owner], [second], [owner, second]]) {
    f.routes[path] = { data: testers };
    assert.equal((await verifyUploaded(f.client, '3', SHA, f.p)).approvedGroupAttached, true);
  }
  for (const testers of [
    [owner, second, { ...owner, id: 'third' }],
    [{ ...second, attributes: { email: 'someone@example.invalid', inviteType: 'EMAIL' } }],
    [owner, { ...owner, id: 'duplicate-email' }],
    [owner, { ...second, id: owner.id }],
  ]) {
    f.routes[path] = { data: testers };
    await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p), /individual tester/);
  }
});
test('upload attached to an external group is refused', async () => {
  const f = fixture('catalyst'), id = uploaded(f);
  const external = f.p.groups.find(g => !g.internal);
  // audience() refuses an external group with builds before group attachment is evaluated.
  f.routes[`/v1/betaGroups/${external.id}/builds?limit=200`].data = [{ id, type: 'builds' }];
  await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p));
});

// ---- Mac Catalyst ----
test('Mac releases read only the MAC_OS train and use the policy audience', async () => {
  const f = fixture('catalyst');
  f.routes[`/v1/preReleaseVersions?filter[app]=${f.p.appId}&filter[platform]=MAC_OS&limit=200`] = { data: [{ type: 'preReleaseVersions', id: 'mac-train', attributes: { platform: 'MAC_OS', version: f.p.marketingVersion } }] };
  f.routes['/v1/preReleaseVersions/mac-train/builds?limit=200'] = { data: [{ type: 'builds', id: 'mac-previous', attributes: { version: '2' } }] };
  assert.equal((await preflight(f.client, '3', SHA, f.p, 'mac')).platform, 'mac');
  assert.ok(!f.requested.some(path => path.includes('filter[platform]=IOS')));
  await assert.rejects(() => preflight(f.client, '2', SHA, f.p, 'mac'), /Mac build preflight/);
  uploaded(f, {}, 'mac-train', 'mac-uploaded');
  assert.equal((await verifyUploaded(f.client, '3', SHA, f.p, 'mac')).audience, 'APP_STORE_ELIGIBLE');
  f.routes['/v1/preReleaseVersions/mac-train/builds?limit=200'].data[0].attributes.buildAudienceType = 'INTERNAL_ONLY';
  await assert.rejects(() => verifyUploaded(f.client, '3', SHA, f.p, 'mac'), /Mac version/);
  await assert.rejects(() => preflight(f.client, '3', SHA, f.p, 'watchos'), /Unknown release platform/);
});

// ---- Discovery helper (migration only) ----
test('discovery reports the existing app and approved internal groups without adopting a broader audience', async () => {
  const f = fixture('single-app-internal-only'), p = f.p;
  f.routes[`/v1/apps?filter[bundleId]=${p.bundleId}&limit=200`] = { data: [{ type: 'apps', id: p.appId, attributes: { bundleId: p.bundleId } }] };
  const found = await discoverAudience(f.client, p);
  assert.deepEqual(found, { appId: p.appId, groups: p.groups });
  for (const change of ['no-app', 'duplicate-app', 'external-tester', 'public-link', 'stranger']) {
    const g = fixture('single-app-internal-only');
    const apps = [{ type: 'apps', id: p.appId, attributes: { bundleId: p.bundleId } }];
    g.routes[`/v1/apps?filter[bundleId]=${p.bundleId}&limit=200`] = { data: change === 'no-app' ? [] : change === 'duplicate-app' ? [...apps, { ...apps[0], id: '2' }] : apps };
    const group = g.routes[`/v1/apps/${p.appId}/betaGroups?limit=200`].data[0];
    if (change === 'external-tester') group.attributes.isInternalGroup = false;
    if (change === 'public-link') group.attributes.publicLinkEnabled = true;
    if (change === 'stranger') g.routes[`/v1/betaGroups/${group.id}/betaTesters?limit=200`].data[0].attributes.email = 'x@example.invalid';
    await assert.rejects(() => discoverAudience(g.client, p), undefined, change);
  }
});
