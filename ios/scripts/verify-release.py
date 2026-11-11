#!/usr/bin/env python3
"""Local iOS/Mac Catalyst release checks. Never creates/replaces Apple signing assets.

The app's release policy is read from --policy or the RELEASE_POLICY environment
variable. Standard library only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import subprocess
import tempfile
import zipfile

AUDIENCES = ('APP_STORE_ELIGIBLE', 'INTERNAL_ONLY')
DEFAULT_AUDIENCE = 'APP_STORE_ELIGIBLE'
REQUIRED_KEYS = ('appId', 'bundleId', 'teamId', 'certificateSha1', 'certificateSha256', 'profiles',
                 'infoPlists', 'configuredPlistKeys', 'marketingVersion', 'groups', 'testerEmailSha256s')
UUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
HEX64 = re.compile(r'[a-f0-9]{64}')

PLATFORMS = {
    # Mac Catalyst App Store profiles report OSX and omit beta-reports-active.
    'ios': {'profiles': 'profiles', 'platform': 'iOS', 'extension': '.mobileprovision', 'beta': True},
    'mac': {'profiles': 'macProfiles', 'platform': 'OSX', 'extension': '.provisionprofile', 'beta': False},
}


def check(ok, message):
    if not ok:
        raise ValueError(message)


def validate_policy(p):
    """Fail closed on an incomplete or ambiguous policy before any release step."""
    check(isinstance(p, dict), 'Release policy must be a JSON object.')
    missing = [k for k in REQUIRED_KEYS if k not in p]
    if 'testerEmailSha256' in p:
        raise ValueError('Legacy testerEmailSha256 is not accepted; use testerEmailSha256s (a list).')
    check(not missing, 'Release policy is missing required keys: ' + ', '.join(missing))
    check(isinstance(p['appId'], str) and re.fullmatch(r'[0-9]+', p['appId']), 'appId must be the numeric App Store Connect app id string.')
    check(isinstance(p['teamId'], str) and re.fullmatch(r'[A-Z0-9]{10}', p['teamId']), 'teamId must be a 10-character team identifier.')
    check(isinstance(p['bundleId'], str) and p['bundleId'], 'bundleId is required.')
    check(isinstance(p['certificateSha1'], str) and re.fullmatch(r'[A-F0-9]{40}', p['certificateSha1']), 'certificateSha1 must be 40 uppercase hex characters.')
    check(isinstance(p['certificateSha256'], str) and HEX64.fullmatch(p['certificateSha256']), 'certificateSha256 must be 64 lowercase hex characters.')
    check(isinstance(p['marketingVersion'], str) and re.fullmatch(r'[0-9]+(\.[0-9]+){0,2}', p['marketingVersion']), 'marketingVersion is invalid.')
    check(p.get('buildAudience', DEFAULT_AUDIENCE) in AUDIENCES, 'buildAudience must be APP_STORE_ELIGIBLE or INTERNAL_ONLY.')
    check(isinstance(p['infoPlists'], list) and all(isinstance(x, str) and x and not x.startswith('/') and '..' not in PurePosixPath(x).parts for x in p['infoPlists']),
          'infoPlists must be repository-relative paths.')
    check(isinstance(p['configuredPlistKeys'], list) and all(isinstance(x, str) and x for x in p['configuredPlistKeys']), 'configuredPlistKeys must be a list of keys.')
    check(p.get('apiOrigin') is None or (isinstance(p['apiOrigin'], str) and p['apiOrigin'].startswith('https://')), 'apiOrigin must be null or an https URL.')
    testers = p['testerEmailSha256s']
    check(isinstance(testers, list) and testers and len(set(testers)) == len(testers)
          and all(isinstance(t, str) and HEX64.fullmatch(t) for t in testers), 'testerEmailSha256s must be a non-empty list of distinct sha256 hex digests.')
    groups = p['groups']
    check(isinstance(groups, list) and groups, 'groups must list every existing TestFlight group.')
    for g in groups:
        check(isinstance(g, dict) and isinstance(g.get('id'), str) and g['id'] and isinstance(g.get('name'), str)
              and isinstance(g.get('internal'), bool) and isinstance(g.get('automatic'), bool)
              and isinstance(g.get('testers'), int) and not isinstance(g.get('testers'), bool) and g['testers'] >= 0, 'Each group needs id, name, internal, testers and automatic.')
        if g['testers']:
            check(g['internal'] and g['testers'] == len(testers), 'Only internal groups may hold testers, and each must hold exactly the approved testers.')
    check(len({g['id'] for g in groups}) == len(groups), 'Duplicate group ids.')
    check(any(g['testers'] for g in groups), 'At least one internal group must hold the approved testers.')
    for h in p.get('excludedHistoricalTesters', []):
        check(isinstance(h, dict) and isinstance(h.get('id'), str) and h['id'] and isinstance(h.get('emailSha256'), str)
              and HEX64.fullmatch(h['emailSha256']), 'excludedHistoricalTesters entries need id and emailSha256.')
    for key in ('profiles', 'macProfiles'):
        if key not in p:
            continue
        profiles = p[key]
        check(isinstance(profiles, dict) and 'app' in profiles, key + ' must contain an "app" entry.')
        for name, t in profiles.items():
            check(re.fullmatch(r'[A-Za-z0-9_-]+', name), key + ' names must be simple identifiers.')
            check(isinstance(t, dict) and isinstance(t.get('uuid'), str) and UUID.fullmatch(t['uuid']), key + '.' + name + '.uuid must be a lowercase UUID.')
            check(isinstance(t.get('name'), str) and t['name'], key + '.' + name + '.name is required.')
            check(isinstance(t.get('bundleId'), str) and t['bundleId'], key + '.' + name + '.bundleId is required.')
            check(isinstance(t.get('groups'), list) and isinstance(t.get('appleSignin'), list) and 'push' in t, key + '.' + name + ' needs groups, appleSignin and push.')
            for flag in ('healthkit', 'healthkitBackgroundDelivery'):
                check(flag not in t or isinstance(t[flag], bool), key + '.' + name + '.' + flag + ' must be boolean.')
        check(profiles['app']['bundleId'] == p['bundleId'], key + '.app.bundleId must equal bundleId.')
    if 'macProfiles' in p:
        check(list(p['macProfiles']) == ['app'], 'Mac Catalyst releases support only the app profile (no extensions).')
        rule = p.get('macInstaller')
        check(isinstance(rule, dict) and all(isinstance(rule.get(k), str) and rule[k] for k in ('identityPrefix', 'alternateIdentityPrefix', 'teamSuffix'))
              and rule['teamSuffix'] == ' (' + p['teamId'] + ')', 'macProfiles requires macInstaller with the approved team suffix.')
    plist = p.get('mainInfoPlist')
    check(plist is None or (isinstance(plist, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in plist.items())), 'mainInfoPlist must map keys to string templates.')
    for flag in ('allowDottedBuildHistory', 'allowApprovedIndividualTesters'):
        check(flag not in p or isinstance(p[flag], bool), flag + ' must be boolean.')
    return p


def load_policy(path=None):
    path = path or os.environ.get('RELEASE_POLICY')
    check(path, 'Release policy path is required (--policy or RELEASE_POLICY).')
    return validate_policy(json.loads(Path(path).read_text()))


def audience(policy):
    return policy.get('buildAudience', DEFAULT_AUDIENCE)


def run(*args):
    result = subprocess.run(args, capture_output=True)
    detail = result.stderr.decode(errors='replace')[:2000] if args[0] in ('codesign', 'ditto') else 'command output withheld'
    check(result.returncode == 0, 'Local verification failed (' + ' '.join(args[:2]) + ', exit ' + str(result.returncode) + '): ' + detail)
    return result.stdout


def decode(path):
    return plistlib.loads(run('security', 'cms', '-D', '-i', str(path)))


def platform_config(platform):
    check(platform in PLATFORMS, 'Unknown release platform.')
    return PLATFORMS[platform]


def profiles_for(policy, platform):
    key = platform_config(platform)['profiles']
    check(key in policy, 'Release policy has no ' + key + ' for platform ' + platform + '.')
    return policy[key]


def verify_profile(profile, target, certificate, now, policy, platform='ios'):
    kind = platform_config(platform)
    now = now or dt.datetime.now(dt.timezone.utc)
    e = profile.get('Entitlements', {})
    expiry, created = profile.get('ExpirationDate'), profile.get('CreationDate')
    check(isinstance(expiry, dt.datetime) and expiry.replace(tzinfo=dt.timezone.utc) > now + dt.timedelta(hours=1)
          and isinstance(created, dt.datetime) and created.replace(tzinfo=dt.timezone.utc) <= now, 'Profile validity is insufficient.')
    check(str(profile.get('UUID', '')).lower() == target['uuid'] and profile.get('Name') == target['name']
          and profile.get('TeamIdentifier') == [policy['teamId']]
          and profile.get('ApplicationIdentifierPrefix') == [policy['teamId']]
          and kind['platform'] in profile.get('Platform', []) and 'ProvisionedDevices' not in profile
          and profile.get('ProvisionsAllDevices') is not True, 'Profile identity, platform or distribution type differs.')
    check(e.get('application-identifier') == policy['teamId'] + '.' + target['bundleId']
          and e.get('com.apple.developer.team-identifier') == policy['teamId']
          and e.get('get-task-allow') is False and (not kind['beta'] or e.get('beta-reports-active') is True), 'Profile app/team or distribution entitlement differs.')
    check(e.get('com.apple.security.application-groups', []) == target['groups'], 'Profile App Groups differ.')
    check(e.get('com.apple.developer.applesignin', []) == target['appleSignin'], 'Profile Apple sign-in entitlement differs.')
    check(e.get('aps-environment') == target['push'], 'Profile push entitlement differs.')
    for flag, entitlement in (('healthkit', 'com.apple.developer.healthkit'),
                              ('healthkitBackgroundDelivery', 'com.apple.developer.healthkit.background-delivery')):
        if flag in target:
            check(e.get(entitlement) is target[flag] or (target[flag] is False and entitlement not in e),
                  'Existing profile HealthKit entitlements differ.')
    if target.get('associatedDomains'):
        domains = e.get('com.apple.developer.associated-domains', [])
        # Apple's profile allowlist may encode this wildcard as a scalar string.
        # The signed app still has to claim the concrete approved domains.
        check(domains == '*' or domains == ['*']
              or (isinstance(domains, list) and all(d in domains for d in target['associatedDomains'])),
              'Profile Associated Domains are insufficient.')
    certificates = profile.get('DeveloperCertificates', [])
    check(len(certificates) == 1 and hashlib.sha256(certificates[0]).hexdigest() == policy['certificateSha256'], 'Profile certificate differs.')
    if certificate is not None:
        check(hashlib.sha256(certificate).hexdigest() == policy['certificateSha256'], 'Imported certificate differs.')


def install_profiles(directory, certificate, destination, policy, platform='ios'):
    # Validate every profile first. Never overwrite a cached profile at the same UUID.
    kind = platform_config(platform)
    values = []
    for name, target in profiles_for(policy, platform).items():
        path = directory / (name + kind['extension'])
        content = path.read_bytes()
        verify_profile(decode(path), target, certificate, None, policy, platform)
        dest = destination / (target['uuid'] + kind['extension'])
        if dest.exists():
            check(dest.read_bytes() == content, 'Existing cached profile differs; preserve it and stop.')
        values.append((dest, content))
    destination.mkdir(parents=True, exist_ok=True)
    for path, content in values:
        if not path.exists():
            with path.open('xb') as output:
                output.write(content)
            path.chmod(0o600)


def certificate_fingerprints(der):
    return {'sha1': hashlib.sha1(der).hexdigest().upper(), 'sha256': hashlib.sha256(der).hexdigest()}


def verify_certificate(der, policy):
    found = certificate_fingerprints(der)
    check(found['sha256'] == policy['certificateSha256'] and found['sha1'] == policy['certificateSha1'],
          'Imported distribution certificate differs from the policy pins.')


def identity(sha, build):
    check(isinstance(sha, str) and isinstance(build, str) and re.fullmatch('[0-9a-f]{40}', sha) and re.fullmatch('[1-9][0-9]*', build),
          'Full source SHA and positive build required.')
    check(run('git', 'rev-parse', 'HEAD').decode().strip() == sha, 'Checkout SHA differs.')
    check(run('git', 'rev-list', '--count', 'HEAD').decode().strip() == build, 'Build must equal complete Git commit count.')


def source(sha, build, policy, prepare=False):
    identity(sha, build)
    if prepare:
        check(not run('git', 'status', '--porcelain').strip(), 'Release checkout must start clean.')
        for path in policy['infoPlists']:
            value = plistlib.loads(Path(path).read_bytes())
            value['ReleaseCommit'] = sha
            Path(path).write_bytes(plistlib.dumps(value, sort_keys=False))
    else:
        changed = run('git', 'diff', '--name-only', 'HEAD').decode().splitlines()
        check(set(changed) <= set(policy['infoPlists']), 'Unexpected tracked source changes.')
        check(not run('git', 'ls-files', '--others', '--exclude-standard').strip(), 'Unexpected untracked release source.')
        for path in policy['infoPlists']:
            original = plistlib.loads(run('git', 'show', 'HEAD:' + path))
            current = plistlib.loads(Path(path).read_bytes())
            check(current.get('ReleaseCommit') == sha, 'Missing full source stamp.')
            for key in set(original) | set(current):
                if key not in policy['configuredPlistKeys'] + ['ReleaseCommit']:
                    check(original.get(key) == current.get(key), 'Unexpected source plist change.')


def export_options(policy, platform='ios', installer_sha1=None):
    options = dict(method='app-store-connect', destination='export', teamID=policy['teamId'], signingStyle='manual',
                   signingCertificate=policy['certificateSha1'], manageAppVersionAndBuildNumber=False,
                   testFlightInternalTestingOnly=audience(policy) == 'INTERNAL_ONLY', stripSwiftSymbols=True, uploadSymbols=True,
                   provisioningProfiles={p['bundleId']: p['uuid'] for p in profiles_for(policy, platform).values()})
    if platform == 'mac':
        check(isinstance(installer_sha1, str) and re.fullmatch('[0-9A-Fa-f]{40}', installer_sha1), 'Mac export requires the imported installer identity.')
        options['installerSigningCertificate'] = installer_sha1.upper()
    return options


def installer_identity(identities, policy):
    # `security find-identity -v` output from this run's temporary keychain only.
    rule = policy.get('macInstaller')
    check(rule, 'Release policy has no macInstaller rule.')
    found = []
    for line in identities.splitlines():
        match = re.search(r'\b([0-9A-F]{40}) "([^"]+)"', line)
        if match and match.group(2).startswith((rule['identityPrefix'], rule['alternateIdentityPrefix'])) and match.group(2).endswith(rule['teamSuffix']):
            found.append(match.group(1))
    check(len(set(found)) == 1, 'Expected exactly one Mac installer identity for the approved team.')
    return found[0]


def expected_main_plist(policy, sha, build):
    """Main-bundle Info.plist values beyond version/build/source stamp."""
    templates = policy.get('mainInfoPlist')
    if templates is None:
        templates = {'API_BASE_URL': '{apiOrigin}'} if policy.get('apiOrigin') else {}
    values = {'apiOrigin': policy.get('apiOrigin') or '', 'build': build, 'sha': sha, 'sha7': sha[:7]}
    expected = {}
    for key, template in templates.items():
        try:
            expected[key] = template.format(**values)
        except (KeyError, IndexError, ValueError):
            raise ValueError('mainInfoPlist template for ' + key + ' is invalid.')
    return expected


def check_main_plist(info, policy, sha, build, label='Artifact'):
    wrong = sorted(key for key, value in expected_main_plist(policy, sha, build).items() if info.get(key) != value)
    # Keys only; production configuration values are not echoed.
    check(not wrong, label + ' main bundle configuration differs: ' + ', '.join(wrong))


def check_mac_entitlements(e, target, policy):
    identifier = policy['teamId'] + '.' + target['bundleId']
    expected = {
        'application-identifier': identifier, 'com.apple.application-identifier': identifier,
        'com.apple.developer.team-identifier': policy['teamId'], 'get-task-allow': False,
        'com.apple.developer.applesignin': target['appleSignin'],
        'aps-environment': target['push'], 'com.apple.developer.aps-environment': target['push'],
        **target.get('signedRequired', {}),
    }
    # Entitlement names and values are public signing metadata, safe to report.
    wrong = sorted(key for key, value in expected.items() if e.get(key) != value)
    if e.get('com.apple.security.application-groups', []) != target.get('signedGroups', target['groups']):
        wrong.append('com.apple.security.application-groups')
    check(not wrong, 'Signed Mac artifact entitlements differ: ' + ', '.join(wrong))


def mac_artifact(pkg, sha, build, policy):
    target = profiles_for(policy, 'mac')['app']
    signature = run('pkgutil', '--check-signature', str(pkg)).decode(errors='replace')
    rule = policy['macInstaller']
    check(any((rule['identityPrefix'] in line or rule['alternateIdentityPrefix'] in line) and rule['teamSuffix'] in line
              for line in signature.splitlines()), 'Mac package is not signed by the approved installer identity.')
    with tempfile.TemporaryDirectory(prefix='verify-mac-') as directory:
        expanded = Path(directory) / 'expanded'
        run('pkgutil', '--expand-full', str(pkg), str(expanded))
        apps = [p for p in expanded.rglob('*.app') if p.is_dir() and not any(parent.suffix == '.app' for parent in p.parents)]
        check(len(apps) == 1, 'Expected one Mac application in the package.')
        app = apps[0]
        check(not list(app.rglob('*.appex')), 'Mac package unexpectedly contains app extensions.')
        info = plistlib.loads((app / 'Contents' / 'Info.plist').read_bytes())
        check(info.get('CFBundleIdentifier') == target['bundleId'] and info.get('CFBundleShortVersionString') == policy['marketingVersion']
              and info.get('CFBundleVersion') == build and info.get('ReleaseCommit') == sha and info.get('DTPlatformName') == 'macosx',
              'Mac artifact bundle/version/build/source/platform differs.')
        check_main_plist(info, policy, sha, build, 'Mac artifact')
        profile = app / 'Contents' / 'embedded.provisionprofile'
        check(profile.is_file(), 'Mac artifact has no embedded existing profile.')
        verify_profile(decode(profile), target, None, None, policy, 'mac')
        run('codesign', '--verify', '--deep', '--strict', str(app))
        check(hashlib.sha256(signing_certificate(app, directory)).hexdigest() == policy['certificateSha256'], 'Mac artifact signing identity differs.')
        e = plistlib.loads(run('codesign', '-d', '--entitlements', ':-', str(app)))
        check_mac_entitlements(e, target, policy)
    return {'platform': 'mac', 'sha': sha, 'build': build, 'appId': policy['appId'], 'pkgSha256': hashlib.sha256(Path(pkg).read_bytes()).hexdigest()}


def inspect_package(ipa, sha, build, policy):
    profiles = policy['profiles']
    with zipfile.ZipFile(ipa) as archive:
        names = archive.namelist()
        check(len(names) == len(set(names)) and all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in names), 'Unsafe or duplicate IPA paths.')
        apps = [n for n in names if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist', n)]
        check(len(apps) == 1, 'Expected one iOS application.')
        main = apps[0]
        extension_prefix = main[:-len('Info.plist')] + 'PlugIns/'
        infos = [main] + [n for n in names if n.startswith(extension_prefix) and re.fullmatch(r'[^/]+\.appex/Info\.plist', n[len(extension_prefix):])]
        check(len(infos) == len(profiles), 'Unexpected or missing app extensions.')
        all_bundles = [n for n in names if n.endswith(('.app/Info.plist', '.appex/Info.plist'))]
        check(set(all_bundles) == set(infos), 'Unexpected nested application or extension.')
        bundles, paths = set(), []
        for path in infos:
            info = plistlib.loads(archive.read(path))
            bundle = info.get('CFBundleIdentifier')
            check(bundle not in bundles and bundle in [p['bundleId'] for p in profiles.values()], 'Unexpected artifact bundle.')
            check(path != main or bundle == profiles['app']['bundleId'], 'Main application bundle differs.')
            bundles.add(bundle)
            expected_metadata = {'CFBundleShortVersionString': policy['marketingVersion'], 'CFBundleVersion': build,
                                 'ReleaseCommit': sha, 'DTPlatformName': 'iphoneos'}
            mismatches = {key: {'expected': value, 'actual': info.get(key)}
                          for key, value in expected_metadata.items() if info.get(key) != value}
            check(not mismatches, 'Artifact version/build/source/platform differs: ' + json.dumps(mismatches, default=str))
            if path == main:
                check_main_plist(info, policy, sha, build)
            profile_path = path[:-len('Info.plist')] + 'embedded.mobileprovision'
            check(profile_path in names, 'Artifact has no embedded existing profile.')
            paths.append((path[:-len('/Info.plist')], bundle))
        return paths


def signing_certificate(app, directory):
    # Optional long-option values must use '='; a separate prefix is treated
    # by codesign as another input file. These are public certificates only.
    prefix = str(Path(directory) / 'signing-cert-')
    run('codesign', '-d', '--extract-certificates=' + prefix, str(app))
    certificate = Path(prefix + '0')
    check(certificate.is_file(), 'Signed bundle has no embedded public signing certificate.')
    return certificate.read_bytes()


def check_signed_ios_entitlements(e, bundle, target, policy, is_main):
    check(e.get('application-identifier') == policy['teamId'] + '.' + bundle
          and e.get('com.apple.developer.team-identifier') == policy['teamId']
          and e.get('get-task-allow', False) is False
          and e.get('com.apple.security.application-groups', []) == target['groups'], 'Signed artifact identity or App Groups differ.')
    if target['appleSignin'] and is_main:
        check(e.get('com.apple.developer.applesignin') == ['Default'], 'Signed app lacks Apple sign-in.')
    if target['push']:
        check(e.get('aps-environment') == target['push'], 'Signed app lacks production push.')
    for flag, entitlement in (('healthkit', 'com.apple.developer.healthkit'),
                              ('healthkitBackgroundDelivery', 'com.apple.developer.healthkit.background-delivery')):
        if target.get(flag) is True:
            check(e.get(entitlement) is True, 'Signed app lacks required HealthKit entitlements.')
    if target.get('associatedDomains'):
        check(e.get('com.apple.developer.associated-domains') == target['associatedDomains'], 'Signed app Associated Domains differ.')


def artifact(ipa, sha, build, policy):
    paths = inspect_package(ipa, sha, build, policy)
    with tempfile.TemporaryDirectory(prefix='verify-native-') as directory:
        run('ditto', '-x', '-k', str(ipa), directory)
        for relative, bundle in paths:
            app = Path(directory) / relative
            name, target = next((n, p) for n, p in policy['profiles'].items() if p['bundleId'] == bundle)
            verify_profile(decode(app / 'embedded.mobileprovision'), target, None, None, policy)
            run('codesign', '--verify', '--deep', '--strict', str(app))
            check(hashlib.sha256(signing_certificate(app, directory)).hexdigest() == policy['certificateSha256'], 'Artifact signing identity differs.')
            e = plistlib.loads(run('codesign', '-d', '--entitlements', ':-', str(app)))
            check_signed_ios_entitlements(e, bundle, target, policy, name == 'app')
    return {'platform': 'ios', 'sha': sha, 'build': build, 'appId': policy['appId'], 'audience': audience(policy),
            'ipaSha256': hashlib.sha256(Path(ipa).read_bytes()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['validate-policy', 'prepare-source', 'source', 'profiles', 'certificate',
                                         'export-options', 'artifact', 'installer-identity', 'policy-value'])
    for name in ['policy', 'sha', 'build', 'directory', 'certificate-der', 'destination', 'output', 'ipa', 'pkg',
                 'installer-sha1', 'identities', 'key']:
        parser.add_argument('--' + name)
    parser.add_argument('--platform', choices=sorted(PLATFORMS), default='ios')
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    if args.mode == 'validate-policy':
        if args.platform == 'mac':
            profiles_for(policy, 'mac')
        print(json.dumps({'appId': policy['appId'], 'bundleId': policy['bundleId'], 'buildAudience': audience(policy)}))
    elif args.mode == 'policy-value':
        # Public, non-secret scalar policy values for shell steps.
        check(args.key in ('teamId', 'certificateSha1', 'certificateSha256', 'appId', 'bundleId', 'marketingVersion', 'buildAudience'), 'Unsupported policy key.')
        print(audience(policy) if args.key == 'buildAudience' else policy[args.key])
    elif args.mode in ['prepare-source', 'source']:
        source(args.sha, args.build, policy, args.mode == 'prepare-source')
    elif args.mode == 'certificate':
        verify_certificate(Path(args.certificate_der).read_bytes(), policy)
    elif args.mode == 'profiles':
        install_profiles(Path(args.directory), Path(args.certificate_der).read_bytes(), Path(args.destination), policy, args.platform)
    elif args.mode == 'export-options':
        with open(args.output, 'xb') as file:
            plistlib.dump(export_options(policy, args.platform, args.installer_sha1), file)
    elif args.mode == 'installer-identity':
        print(installer_identity(Path(args.identities).read_text(), policy))
    elif args.platform == 'mac':
        source(args.sha, args.build, policy)
        print(json.dumps(mac_artifact(args.pkg, args.sha, args.build, policy)))
    else:
        source(args.sha, args.build, policy)
        print(json.dumps(artifact(args.ipa, args.sha, args.build, policy)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error))
