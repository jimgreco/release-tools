"""Tests for ios/scripts/verify-release.py and archive.py (stdlib only).

Run: python3 -m unittest discover -s ios/tests -p 'test_*.py'
"""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v = load_module('verify_release', 'verify-release.py')
archive = load_module('release_archive', 'archive.py')
ALL = sorted(p.stem for p in FIXTURES.glob('*.json'))
SHA, BUILD = 'a' * 40, '999'
CERT = b'synthetic public certificate, not a credential'


def fixture(name):
    p = json.loads((FIXTURES / (name + '.json')).read_text())
    return v.validate_policy(p)


def with_cert(p):
    p = copy.deepcopy(p)
    p['certificateSha256'] = hashlib.sha256(CERT).hexdigest()
    p['certificateSha1'] = hashlib.sha1(CERT).hexdigest().upper()
    return p


class PolicyTests(unittest.TestCase):
    def test_all_fixtures_validate(self):
        for name in ALL:
            with self.subTest(name=name):
                fixture(name)

    def test_policy_is_read_from_env_or_argument_not_a_sibling_file(self):
        path = str(FIXTURES / 'catalyst.json')
        self.assertEqual(v.load_policy(path)['appId'], fixture('catalyst')['appId'])
        with patch.dict(os.environ, {'RELEASE_POLICY': path}):
            self.assertEqual(v.load_policy()['appId'], fixture('catalyst')['appId'])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'RELEASE_POLICY'):
                v.load_policy()
        self.assertFalse((SCRIPTS / 'release-policy.json').exists())

    def test_required_keys(self):
        for key in v.REQUIRED_KEYS:
            with self.subTest(key=key):
                p = copy.deepcopy(fixture('app-and-extension')); del p[key]
                with self.assertRaisesRegex(ValueError, key):
                    v.validate_policy(p)

    def test_legacy_singular_tester_is_rejected(self):
        p = copy.deepcopy(fixture('catalyst')); p['testerEmailSha256'] = p['testerEmailSha256s'][0]
        with self.assertRaisesRegex(ValueError, 'testerEmailSha256s'):
            v.validate_policy(p)

    def test_malformed_values(self):
        cases = [('appId', None), ('appId', 'abc'), ('teamId', 'short'), ('certificateSha1', 'a' * 40),
                 ('certificateSha256', 'A' * 64), ('buildAudience', 'EXTERNAL'), ('marketingVersion', 'v1'),
                 ('infoPlists', ['../escape/Info.plist']), ('infoPlists', ['/abs/Info.plist']), ('apiOrigin', 'http://x'),
                 ('testerEmailSha256s', []), ('testerEmailSha256s', ['a' * 64, 'a' * 64]), ('groups', []),
                 ('allowDottedBuildHistory', 'yes'), ('mainInfoPlist', {'K': 1})]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                p = copy.deepcopy(fixture('catalyst')); p[key] = value
                with self.assertRaises(ValueError):
                    v.validate_policy(p)

    def test_group_and_profile_shape(self):
        p = copy.deepcopy(fixture('plural-testers')); p['groups'][0]['testers'] = 1
        with self.assertRaisesRegex(ValueError, 'exactly the approved testers'):
            v.validate_policy(p)
        p = copy.deepcopy(fixture('catalyst')); p['profiles']['app']['uuid'] = p['profiles']['app']['uuid'].upper()
        with self.assertRaisesRegex(ValueError, 'lowercase UUID'):
            v.validate_policy(p)
        p = copy.deepcopy(fixture('catalyst')); del p['macInstaller']
        with self.assertRaisesRegex(ValueError, 'macInstaller'):
            v.validate_policy(p)
        p = copy.deepcopy(fixture('catalyst')); p['macInstaller']['teamSuffix'] = ' (OTHERTEAM1)'
        with self.assertRaises(ValueError):
            v.validate_policy(p)
        p = copy.deepcopy(fixture('catalyst')); p['macProfiles']['extension'] = p['profiles']['extension']
        with self.assertRaisesRegex(ValueError, 'only the app profile'):
            v.validate_policy(p)

    def test_cli_validate_and_policy_value(self):
        path = str(FIXTURES / 'single-app-internal-only.json')
        out = subprocess.run([sys.executable, str(SCRIPTS / 'verify-release.py'), 'validate-policy', '--policy', path],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)['buildAudience'], 'INTERNAL_ONLY')
        out = subprocess.run([sys.executable, str(SCRIPTS / 'verify-release.py'), 'policy-value', '--key', 'certificateSha1'],
                             capture_output=True, text=True, check=True, env={**os.environ, 'RELEASE_POLICY': path}).stdout
        self.assertEqual(out.strip(), fixture('single-app-internal-only')['certificateSha1'])
        bad = subprocess.run([sys.executable, str(SCRIPTS / 'verify-release.py'), 'validate-policy', '--platform', 'mac', '--policy', path],
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)


class PublicCertificateCommandTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'macOS codesign integration')
    def test_extract_public_certificate_uses_output_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'signing-cert-')
            v.run('codesign', '-d', '--extract-certificates=' + prefix, '/usr/bin/true')
            certificate = Path(prefix + '0')
            if certificate.exists():
                first = certificate.read_bytes()
                self.assertGreater(len(first), 100)
                self.assertEqual(v.signing_certificate('/usr/bin/true', directory), first)
            else:
                with self.assertRaisesRegex(ValueError, 'no embedded public signing certificate'):
                    v.signing_certificate('/usr/bin/true', directory)

    def test_imported_certificate_must_match_both_pins(self):
        p = with_cert(fixture('catalyst'))
        v.verify_certificate(CERT, p)
        with self.assertRaises(ValueError):
            v.verify_certificate(b'other', p)
        q = copy.deepcopy(p); q['certificateSha1'] = 'F' * 40
        with self.assertRaises(ValueError):
            v.verify_certificate(CERT, q)

    def test_command_failures_withhold_output_except_public_tools(self):
        with self.assertRaisesRegex(ValueError, 'command output withheld'):
            v.run(sys.executable, '-c', 'import sys; sys.stderr.write("secret-value"); sys.exit(3)')


class ProfileTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)

    def profile(self, p, name, platform='ios'):
        t = p[v.PLATFORMS[platform]['profiles']][name]
        e = {'application-identifier': p['teamId'] + '.' + t['bundleId'],
             'com.apple.developer.team-identifier': p['teamId'], 'get-task-allow': False,
             'com.apple.security.application-groups': t['groups'],
             'com.apple.developer.applesignin': t['appleSignin']}
        if platform == 'ios':
            e['beta-reports-active'] = True
        if t['push']:
            e['aps-environment'] = t['push']
        if t.get('associatedDomains'):
            e['com.apple.developer.associated-domains'] = ['*']
        for flag, key in (('healthkit', 'com.apple.developer.healthkit'), ('healthkitBackgroundDelivery', 'com.apple.developer.healthkit.background-delivery')):
            if flag in t:
                e[key] = t[flag]
        return dict(UUID=t['uuid'].upper(), Name=t['name'], TeamIdentifier=[p['teamId']],
                    ApplicationIdentifierPrefix=[p['teamId']], Platform=['iOS'] if platform == 'ios' else ['OSX'],
                    CreationDate=dt.datetime(2026, 9, 1), ExpirationDate=dt.datetime(2027, 5, 12),
                    DeveloperCertificates=[CERT], Entitlements=e)

    def verify(self, p, profile, name='app', platform='ios'):
        v.verify_profile(profile, p[v.PLATFORMS[platform]['profiles']][name], CERT, self.now, p, platform)

    def test_all_approved_profiles_for_every_variant(self):
        for fixture_name in ALL:
            p = with_cert(fixture(fixture_name))
            for name in p['profiles']:
                with self.subTest(fixture=fixture_name, profile=name):
                    self.verify(p, self.profile(p, name), name)

    def test_profile_failures(self):
        p = with_cert(fixture('catalyst'))
        for key, value in [('UUID', 'other'), ('Name', 'new'), ('TeamIdentifier', ['other']),
                           ('ApplicationIdentifierPrefix', ['other']), ('Platform', ['macOS']),
                           ('ProvisionedDevices', []), ('ProvisionsAllDevices', True),
                           ('ExpirationDate', dt.datetime(2026, 10, 3, 0, 30)),
                           ('CreationDate', dt.datetime(2030, 1, 1)),
                           ('DeveloperCertificates', [b'different']), ('DeveloperCertificates', [CERT, CERT])]:
            with self.subTest(key=key):
                profile = self.profile(p, 'app'); profile[key] = value
                with self.assertRaises(ValueError):
                    self.verify(p, profile)

    def test_entitlement_failures(self):
        p = with_cert(fixture('catalyst'))
        for key, value in [('application-identifier', 'wrong'), ('get-task-allow', True), ('beta-reports-active', False),
                           ('com.apple.security.application-groups', ['other']), ('com.apple.developer.applesignin', ['other']),
                           ('aps-environment', 'development'), ('com.apple.developer.team-identifier', 'OTHER')]:
            with self.subTest(key=key):
                profile = self.profile(p, 'app'); profile['Entitlements'][key] = value
                with self.assertRaises(ValueError):
                    self.verify(p, profile)

    def test_imported_certificate_must_match(self):
        p = with_cert(fixture('catalyst'))
        with self.assertRaisesRegex(ValueError, 'Imported certificate'):
            v.verify_profile(self.profile(p, 'app'), p['profiles']['app'], b'other', self.now, p)

    def test_healthkit_policy_is_enforced_on_the_profile(self):
        p = with_cert(fixture('single-app-internal-only'))
        for key in ('com.apple.developer.healthkit', 'com.apple.developer.healthkit.background-delivery'):
            for value in (None, False, 'true'):
                with self.subTest(key=key, value=value):
                    profile = self.profile(p, 'app')
                    if value is None:
                        del profile['Entitlements'][key]
                    else:
                        profile['Entitlements'][key] = value
                    with self.assertRaisesRegex(ValueError, 'HealthKit'):
                        self.verify(p, profile)

    def test_associated_domains_profile_wildcard_representations(self):
        p = with_cert(fixture('plural-testers'))
        for value in ['*', ['*'], p['profiles']['app']['associatedDomains'], p['profiles']['app']['associatedDomains'] + ['applinks:extra.invalid']]:
            with self.subTest(value=value):
                profile = self.profile(p, 'app')
                profile['Entitlements']['com.apple.developer.associated-domains'] = value
                self.verify(p, profile)

    def test_associated_domains_rejects_insufficient_or_wrong_types(self):
        p = with_cert(fixture('plural-testers'))
        approved = p['profiles']['app']['associatedDomains']
        for value in [None, False, '', [], approved[:1], ['applinks:other.invalid'], ' '.join(approved), {d: True for d in approved}]:
            with self.subTest(value=value):
                profile = self.profile(p, 'app')
                profile['Entitlements']['com.apple.developer.associated-domains'] = value
                with self.assertRaisesRegex(ValueError, 'Associated Domains'):
                    self.verify(p, profile)

    def test_profile_install_preserves_existing_bytes(self):
        p = with_cert(fixture('catalyst'))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = root / 'source'; src.mkdir(); dest = root / 'cache'; dest.mkdir()
            for name in p['profiles']:
                (src / (name + '.mobileprovision')).write_bytes(name.encode())
            existing = dest / (p['profiles']['app']['uuid'] + '.mobileprovision')
            existing.write_bytes(b'existing cached bytes')
            with patch.object(v, 'decode', lambda path: self.profile(p, path.stem)):
                with self.assertRaises(ValueError):
                    v.install_profiles(src, CERT, dest, p)
            self.assertEqual(existing.read_bytes(), b'existing cached bytes')
            self.assertEqual(len(list(dest.iterdir())), 1)

    def test_profile_install_reuses_identical_cache_and_validates_all_first(self):
        p = with_cert(fixture('catalyst'))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = root / 'source'; src.mkdir(); dest = root / 'cache'
            for name in p['profiles']:
                (src / (name + '.mobileprovision')).write_bytes(name.encode())
            with patch.object(v, 'decode', lambda path: self.profile(p, path.stem)):
                v.install_profiles(src, CERT, dest, p)
                mtimes = {f.name: f.stat().st_mtime_ns for f in dest.iterdir()}
                v.install_profiles(src, CERT, dest, p)
            self.assertEqual(mtimes, {f.name: f.stat().st_mtime_ns for f in dest.iterdir()})
            self.assertEqual({f.stat().st_mode & 0o777 for f in dest.iterdir()}, {0o600})
            bad = root / 'bad-cache'

            def decode(path):
                profile = self.profile(p, path.stem)
                if path.stem == 'extension':
                    profile['Name'] = 'changed'
                return profile
            with patch.object(v, 'decode', decode):
                with self.assertRaises(ValueError):
                    v.install_profiles(src, CERT, bad, p)
            self.assertFalse(bad.exists())


class MacReleaseTests(unittest.TestCase):
    now = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.p = with_cert(fixture('catalyst'))
        self.profile = lambda: ProfileTests.profile(None, self.p, 'app', 'mac')

    def test_pinned_mac_profile_verifies_only_as_mac(self):
        target = self.p['macProfiles']['app']
        v.verify_profile(self.profile(), target, CERT, self.now, self.p, 'mac')
        with self.assertRaises(ValueError):
            v.verify_profile(self.profile(), target, CERT, self.now, self.p, 'ios')
        for key, value in [('Platform', ['iOS']), ('UUID', 'other'), ('DeveloperCertificates', [b'other']), ('ProvisionsAllDevices', True)]:
            with self.subTest(key=key):
                profile = self.profile(); profile[key] = value
                with self.assertRaises(ValueError):
                    v.verify_profile(profile, target, CERT, self.now, self.p, 'mac')

    def signed(self):
        team, ident = self.p['teamId'], self.p['teamId'] + '.' + self.p['bundleId']
        return {'com.apple.security.network.client': True, 'get-task-allow': False, 'com.apple.developer.applesignin': ['Default'],
                'com.apple.developer.team-identifier': team, 'com.apple.security.app-sandbox': True,
                'application-identifier': ident, 'com.apple.developer.aps-environment': 'production',
                'com.apple.application-identifier': ident, 'aps-environment': 'production'}

    def test_signed_mac_entitlements(self):
        target = self.p['macProfiles']['app']
        v.check_mac_entitlements(self.signed(), target, self.p)
        for key, value in [('com.apple.security.app-sandbox', None), ('aps-environment', 'development'), ('get-task-allow', True),
                           ('com.apple.security.network.client', None), ('com.apple.security.application-groups', ['group.other'])]:
            with self.subTest(key=key):
                e = self.signed()
                if value is None:
                    e.pop(key)
                else:
                    e[key] = value
                with self.assertRaisesRegex(ValueError, key.replace('.', '[.]')):
                    v.check_mac_entitlements(e, target, self.p)

    def test_mac_profile_installs_as_provisionprofile(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); src = root / 'source'; src.mkdir(); dest = root / 'cache'
            (src / 'app.provisionprofile').write_bytes(b'mac')
            with patch.object(v, 'decode', lambda path: self.profile()):
                v.install_profiles(src, CERT, dest, self.p, 'mac')
            self.assertEqual([f.name for f in dest.iterdir()], [self.p['macProfiles']['app']['uuid'] + '.provisionprofile'])

    def test_mac_requires_mac_profiles_in_policy(self):
        with self.assertRaisesRegex(ValueError, 'macProfiles'):
            v.export_options(fixture('app-and-extension'), 'mac', 'A' * 40)

    def test_mac_export_is_installer_signed_and_follows_audience(self):
        options = v.export_options(self.p, 'mac', 'a' * 40)
        self.assertIs(options['testFlightInternalTestingOnly'], False)
        self.assertEqual(options['installerSigningCertificate'], 'A' * 40)
        self.assertEqual(options['provisioningProfiles'], {self.p['bundleId']: self.p['macProfiles']['app']['uuid']})
        with self.assertRaises(ValueError):
            v.export_options(self.p, 'mac', None)
        self.assertNotIn('installerSigningCertificate', v.export_options(self.p))

    def test_installer_identity_requires_exactly_one_team_installer(self):
        sha, team = 'B' * 40, self.p['teamId']
        good = f'  1) {"9" * 40} "Apple Distribution: Example ({team})"\n  2) {sha} "3rd Party Mac Developer Installer: Example ({team})"\n'
        self.assertEqual(v.installer_identity(good, self.p), sha)
        self.assertEqual(v.installer_identity(good.replace('3rd Party Mac Developer Installer', 'Mac Installer Distribution'), self.p), sha)
        for identities in ['', good.replace(f'Installer: Example ({team})', 'Installer: Example (OTHERTEAM1)'),
                           good + f'  3) {"C" * 40} "Mac Installer Distribution: Example ({team})"\n']:
            with self.subTest(identities=identities):
                with self.assertRaises(ValueError):
                    v.installer_identity(identities, self.p)


class ArtifactTests(unittest.TestCase):
    def package(self, p, path, change=None, omit_extension=False, extra_extension=False, main_change=None):
        with zipfile.ZipFile(path, 'w') as zf:
            info = {}
            for name, target in p['profiles'].items():
                if name != 'app' and omit_extension:
                    continue
                prefix = 'Payload/Example.app/' + ('' if name == 'app' else 'PlugIns/' + name + '.appex/')
                info = {'CFBundleIdentifier': target['bundleId'], 'CFBundleVersion': BUILD,
                        'CFBundleShortVersionString': p['marketingVersion'], 'ReleaseCommit': SHA, 'DTPlatformName': 'iphoneos'}
                if name == 'app':
                    info.update(v.expected_main_plist(p, SHA, BUILD))
                    info.update(main_change or {})
                info.update(change or {})
                zf.writestr(prefix + 'Info.plist', plistlib.dumps(info))
                zf.writestr(prefix + 'embedded.mobileprovision', b'synthetic-profile')
            if extra_extension:
                zf.writestr('Payload/Example.app/PlugIns/Extra.appex/Info.plist', plistlib.dumps(info))

    def test_complete_metadata_for_every_variant(self):
        for name in ALL:
            p = fixture(name)
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'fixture.ipa'; self.package(p, path)
                self.assertEqual(len(v.inspect_package(path, SHA, BUILD, p)), len(p['profiles']))

    def test_metadata_mismatches(self):
        p = fixture('catalyst')
        changes = {'CFBundleIdentifier': 'other.app', 'CFBundleVersion': '998', 'CFBundleShortVersionString': '9.9',
                   'ReleaseCommit': 'b' * 40, 'DTPlatformName': 'iphonesimulator'}
        with tempfile.TemporaryDirectory() as root:
            for key, value in changes.items():
                with self.subTest(key=key):
                    path = Path(root) / 'fixture.ipa'; self.package(p, path, {key: value})
                    with self.assertRaises(ValueError):
                        v.inspect_package(path, SHA, BUILD, p)

    def test_default_main_plist_checks_api_origin_without_echoing_it(self):
        p = fixture('catalyst')
        self.assertEqual(v.expected_main_plist(p, SHA, BUILD), {'API_BASE_URL': p['apiOrigin']})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.ipa'; self.package(p, path, main_change={'API_BASE_URL': 'https://staging.invalid'})
            with self.assertRaises(ValueError) as error:
                v.inspect_package(path, SHA, BUILD, p)
            self.assertIn('API_BASE_URL', str(error.exception))
            self.assertNotIn('staging.invalid', str(error.exception))

    def test_no_api_origin_means_no_main_plist_expectation(self):
        self.assertEqual(v.expected_main_plist(fixture('plural-testers'), SHA, BUILD), {})

    def test_custom_main_plist_templates(self):
        p = fixture('single-app-internal-only')
        self.assertEqual(v.expected_main_plist(p, SHA, BUILD), {'APIBaseURL': p['apiOrigin'], 'AppBuild': BUILD, 'GitCommitHash': SHA[:7]})
        with tempfile.TemporaryDirectory() as root:
            for key, value in [('APIBaseURL', 'https://other.invalid'), ('AppBuild', '1'), ('GitCommitHash', 'b' * 7)]:
                with self.subTest(key=key):
                    path = Path(root) / 'fixture.ipa'; self.package(p, path, main_change={key: value})
                    with self.assertRaisesRegex(ValueError, key):
                        v.inspect_package(path, SHA, BUILD, p)
        bad = copy.deepcopy(p); bad['mainInfoPlist'] = {'X': '{unknown}'}
        with self.assertRaisesRegex(ValueError, 'template'):
            v.expected_main_plist(bad, SHA, BUILD)

    def test_extension_topology(self):
        p = fixture('catalyst')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.ipa'; self.package(p, path, extra_extension=True)
            with self.assertRaises(ValueError):
                v.inspect_package(path, SHA, BUILD, p)
            self.package(p, path, omit_extension=True)
            with self.assertRaises(ValueError):
                v.inspect_package(path, SHA, BUILD, p)

    def test_extension_bundle_cannot_be_the_main_app(self):
        p = fixture('catalyst')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.ipa'
            with zipfile.ZipFile(path, 'w') as zf:
                for name, target in p['profiles'].items():
                    other = p['profiles']['extension' if name == 'app' else 'app']
                    prefix = 'Payload/Example.app/' + ('' if name == 'app' else 'PlugIns/x.appex/')
                    info = {'CFBundleIdentifier': other['bundleId'], 'CFBundleVersion': BUILD, 'CFBundleShortVersionString': p['marketingVersion'],
                            'ReleaseCommit': SHA, 'DTPlatformName': 'iphoneos', **v.expected_main_plist(p, SHA, BUILD)}
                    zf.writestr(prefix + 'Info.plist', plistlib.dumps(info))
                    zf.writestr(prefix + 'embedded.mobileprovision', b'x')
            with self.assertRaisesRegex(ValueError, 'Main application'):
                v.inspect_package(path, SHA, BUILD, p)

    def test_zip_traversal_and_nested_apps(self):
        p = fixture('catalyst')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.ipa'; self.package(p, path)
            with zipfile.ZipFile(path, 'a') as zf:
                zf.writestr('../escape', b'')
            with self.assertRaises(ValueError):
                v.inspect_package(path, SHA, BUILD, p)
            self.package(p, path)
            with zipfile.ZipFile(path, 'a') as zf:
                zf.writestr('Payload/Example.app/Watch/Other.app/Info.plist', plistlib.dumps({}))
            with self.assertRaises(ValueError):
                v.inspect_package(path, SHA, BUILD, p)

    def test_signed_ios_entitlements(self):
        p = fixture('single-app-internal-only'); target = p['profiles']['app']
        good = {'application-identifier': p['teamId'] + '.' + p['bundleId'], 'com.apple.developer.team-identifier': p['teamId'],
                'com.apple.developer.applesignin': ['Default'], 'com.apple.developer.healthkit': True,
                'com.apple.developer.healthkit.background-delivery': True}
        v.check_signed_ios_entitlements(good, p['bundleId'], target, p, True)
        for key, value in [('com.apple.developer.healthkit', None), ('com.apple.developer.healthkit.background-delivery', False),
                           ('get-task-allow', True), ('com.apple.developer.applesignin', None),
                           ('com.apple.security.application-groups', ['group.x'])]:
            with self.subTest(key=key):
                e = dict(good)
                if value is None:
                    e.pop(key)
                else:
                    e[key] = value
                with self.assertRaises(ValueError):
                    v.check_signed_ios_entitlements(e, p['bundleId'], target, p, True)
        q = fixture('plural-testers'); t = q['profiles']['app']
        e = {'application-identifier': q['teamId'] + '.' + q['bundleId'], 'com.apple.developer.team-identifier': q['teamId'],
             'com.apple.developer.applesignin': ['Default'], 'com.apple.developer.associated-domains': t['associatedDomains']}
        v.check_signed_ios_entitlements(e, q['bundleId'], t, q, True)
        e['com.apple.developer.associated-domains'] = ['*']
        with self.assertRaisesRegex(ValueError, 'Associated Domains'):
            v.check_signed_ios_entitlements(e, q['bundleId'], t, q, True)


class ExportOptionsTests(unittest.TestCase):
    def test_default_audience_is_app_store_eligible(self):
        for name in ALL:
            p = fixture(name)
            with self.subTest(fixture=name):
                options = v.export_options(p)
                self.assertIs(options['testFlightInternalTestingOnly'], p.get('buildAudience') == 'INTERNAL_ONLY')
                self.assertIs(options['manageAppVersionAndBuildNumber'], False)
                self.assertEqual(options['method'], 'app-store-connect')
                self.assertEqual(options['destination'], 'export')
                self.assertEqual(options['signingStyle'], 'manual')
                self.assertEqual(options['teamID'], p['teamId'])
                self.assertEqual(options['signingCertificate'], p['certificateSha1'])
                self.assertEqual(options['provisioningProfiles'], {t['bundleId']: t['uuid'] for t in p['profiles'].values()})

    def test_internal_only_is_policy_driven(self):
        p = copy.deepcopy(fixture('catalyst'))
        self.assertIs(v.export_options(p)['testFlightInternalTestingOnly'], False)
        p['buildAudience'] = 'INTERNAL_ONLY'
        self.assertIs(v.export_options(p)['testFlightInternalTestingOnly'], True)
        self.assertIs(v.export_options(fixture('single-app-internal-only'))['testFlightInternalTestingOnly'], True)

    def test_export_options_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'ExportOptions.plist'
            args = ['export-options', '--policy', str(FIXTURES / 'catalyst.json'), '--output', str(out)]
            v.main(args)
            self.assertIs(plistlib.loads(out.read_bytes())['testFlightInternalTestingOnly'], False)
            with self.assertRaises(FileExistsError):
                v.main(args)


class SourceTests(unittest.TestCase):
    def test_source_ref_and_count(self):
        with patch.object(v, 'run', side_effect=[(SHA + '\n').encode(), b'999\n']):
            v.identity(SHA, BUILD)
        with patch.object(v, 'run', return_value=b'wrong\n'):
            with self.assertRaises(ValueError):
                v.identity(SHA, BUILD)
        with patch.object(v, 'run', side_effect=[(SHA + '\n').encode(), b'998\n']):
            with self.assertRaises(ValueError):
                v.identity(SHA, BUILD)
        for sha, build in [('abc', BUILD), (SHA, '0'), (SHA, '1.2'), (None, BUILD)]:
            with self.assertRaises(ValueError):
                v.identity(sha, build)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_prepare_and_verify_source_in_a_real_repository(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.git(root, 'init', '-q')
            self.git(root, 'config', 'user.email', 'ci@example.invalid'); self.git(root, 'config', 'user.name', 'CI')
            plist = root / 'App' / 'Info.plist'; plist.parent.mkdir()
            plist.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.example', 'API_BASE_URL': ''}))
            self.git(root, 'add', '.'); self.git(root, 'commit', '-qm', 'init')
            sha, build = self.git(root, 'rev-parse', 'HEAD'), self.git(root, 'rev-list', '--count', 'HEAD')
            p = copy.deepcopy(fixture('catalyst')); p['infoPlists'] = ['App/Info.plist']; p['configuredPlistKeys'] = ['API_BASE_URL']
            cwd = os.getcwd()
            os.chdir(root)
            try:
                v.source(sha, build, p, prepare=True)
                self.assertEqual(plistlib.loads(plist.read_bytes())['ReleaseCommit'], sha)
                value = plistlib.loads(plist.read_bytes()); value['API_BASE_URL'] = 'https://configured.invalid'
                plist.write_bytes(plistlib.dumps(value))
                v.source(sha, build, p)
                with self.assertRaisesRegex(ValueError, 'clean'):
                    v.source(sha, build, p, prepare=True)
                value['CFBundleIdentifier'] = 'com.other'; plist.write_bytes(plistlib.dumps(value))
                with self.assertRaisesRegex(ValueError, 'Unexpected source plist change'):
                    v.source(sha, build, p)
                value['CFBundleIdentifier'] = 'com.example'; plist.write_bytes(plistlib.dumps(value))
                (root / 'stray.txt').write_text('x')
                with self.assertRaisesRegex(ValueError, 'untracked'):
                    v.source(sha, build, p)
                (root / 'stray.txt').unlink()
                (root / 'App' / 'Other.swift').write_text('x'); self.git(root, 'add', 'App/Other.swift')
                with self.assertRaisesRegex(ValueError, 'tracked source'):
                    v.source(sha, build, p)
            finally:
                os.chdir(cwd)


class ArchiveCommandTests(unittest.TestCase):
    def command(self, p, extra='', env=None, platform='ios', **kw):
        args = dict(project='ios/App.xcodeproj')
        args.update(kw)
        return archive.archive_command(p, platform, 'App', '/tmp/App.xcarchive', '/tmp/k.keychain-db', BUILD, SHA, extra, env or {}, **args)

    def test_signing_settings_come_from_policy(self):
        p = fixture('app-and-extension')
        cmd = self.command(p)
        self.assertEqual(cmd[:4], ['xcodebuild', 'archive', '-project', 'ios/App.xcodeproj'])
        for setting in ['DEVELOPMENT_TEAM=' + p['teamId'], 'CODE_SIGN_STYLE=Manual', 'CODE_SIGN_IDENTITY=' + p['certificateSha1'],
                        'CURRENT_PROJECT_VERSION=' + BUILD, 'OTHER_CODE_SIGN_FLAGS=--keychain /tmp/k.keychain-db']:
            self.assertIn(setting, cmd)
        self.assertIn('generic/platform=iOS', cmd)

    def test_profile_placeholders_express_each_app_convention(self):
        p = fixture('app-and-extension')
        cmd = self.command(p, 'IOS_PROFILE_NAME=@PROFILE_UUID:app@\nIOS_LIVE_ACTIVITY_PROFILE_NAME=@PROFILE_UUID:extension@\n'
                              '# comment\n\nGIT_COMMIT_HASH=@GIT_SHA7@\nAPP_BUILD=@BUILD_NUMBER@\nRELEASE=@GIT_SHA@')
        self.assertIn('IOS_PROFILE_NAME=' + p['profiles']['app']['uuid'], cmd)
        self.assertIn('IOS_LIVE_ACTIVITY_PROFILE_NAME=' + p['profiles']['extension']['uuid'], cmd)
        self.assertIn('GIT_COMMIT_HASH=' + SHA[:7], cmd)
        self.assertIn('APP_BUILD=' + BUILD, cmd)
        self.assertIn('RELEASE=' + SHA, cmd)

    def test_mac_catalyst_destination_and_profile(self):
        p = fixture('catalyst')
        cmd = self.command(p, 'PROVISIONING_PROFILE_SPECIFIER=@MAC_PROFILE_UUID:app@', platform='mac')
        self.assertIn('generic/platform=macOS,variant=Mac Catalyst', cmd)
        self.assertIn('PROVISIONING_PROFILE_SPECIFIER=' + p['macProfiles']['app']['uuid'], cmd)
        with self.assertRaises(ValueError):
            self.command(fixture('app-and-extension'), platform='mac')

    def test_env_placeholders_and_values_with_spaces_are_single_arguments(self):
        p = fixture('app-and-extension')
        cmd = self.command(p, 'GOOGLE_CLIENT_ID=@ENV:GOOGLE_ID@\nNOTE=a b; rm -rf / $(x)', {'GOOGLE_ID': 'id-123'})
        self.assertIn('GOOGLE_CLIENT_ID=id-123', cmd)
        self.assertIn('NOTE=a b; rm -rf / $(x)', cmd)
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.command(p, 'X=@ENV:MISSING@')

    def test_conditional_setting_names_are_allowed(self):
        p = fixture('catalyst')
        cmd = self.command(p, 'PROVISIONING_PROFILE_SPECIFIER[sdk=iphoneos*]=@PROFILE_UUID:app@')
        self.assertIn('PROVISIONING_PROFILE_SPECIFIER[sdk=iphoneos*]=' + p['profiles']['app']['uuid'], cmd)

    def test_rejections(self):
        p = fixture('app-and-extension')
        for extra in ['CODE_SIGN_IDENTITY=other', 'DEVELOPMENT_TEAM=OTHER', 'OTHER_CODE_SIGN_FLAGS=--keychain x',
                      'CURRENT_PROJECT_VERSION=1', 'CODE_SIGNING_ALLOWED=NO', 'no-equals', '-scheme=x', 'A=1\nA=2',
                      'X=@PROFILE_UUID:missing@', 'X=@UNKNOWN@', 'X=@MAC_PROFILE_UUID:app@']:
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    self.command(p, extra)
        with self.assertRaises(ValueError):
            self.command(p, project=None)
        with self.assertRaises(ValueError):
            self.command(p, workspace='App.xcworkspace')
        cmd = self.command(p, project=None, workspace='App.xcworkspace')
        self.assertEqual(cmd[2:4], ['-workspace', 'App.xcworkspace'])

    def test_dry_run_never_prints_setting_values(self):
        env = {**os.environ, 'RELEASE_EXTRA_BUILD_SETTINGS': 'SECRET_SETTING=@ENV:S@', 'S': 'very-secret-value',
               'RELEASE_POLICY': str(FIXTURES / 'app-and-extension.json')}
        out = subprocess.run([sys.executable, str(SCRIPTS / 'archive.py'), '--dry-run', '--platform', 'ios', '--project', 'A.xcodeproj',
                              '--scheme', 'A', '--archive-path', '/tmp/a', '--keychain', '/tmp/k', '--build', BUILD, '--sha', SHA],
                             capture_output=True, text=True, env=env, check=True).stdout
        self.assertIn('SECRET_SETTING=', out)
        self.assertNotIn('very-secret-value', out)


class ShellHelperTests(unittest.TestCase):
    def sh(self, *args, env=None, cwd=None):
        return subprocess.run(['bash', *args], capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=cwd)

    def test_upload_log_requires_explicit_success_and_no_failure_marker(self):
        script = str(SCRIPTS / 'check-upload-log.sh')
        cases = [('UPLOAD SUCCEEDED with no errors', '0', 0), ('No errors uploading. successfully uploaded', '0', 0),
                 ('nothing useful', '0', 1), ('UPLOAD SUCCEEDED', '1', 1),
                 ('UPLOAD SUCCEEDED\nUPLOAD FAILED', '0', 1), ('received status code 401\nUPLOAD SUCCEEDED', '0', 1),
                 ('NOT_AUTHORIZED', '0', 1), ('Failed to determine the Apple ID', '0', 1), ('Validation failed (409)', '0', 1)]
        with tempfile.TemporaryDirectory() as root:
            for text, status, expected in cases:
                with self.subTest(text=text, status=status):
                    log = Path(root) / 'upload.log'; log.write_text(text)
                    self.assertEqual(self.sh(script, str(log), status).returncode, expected)

    def test_cleanup_restores_keychains_and_removes_only_this_runs_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); bin_dir = root / 'bin'; bin_dir.mkdir(); temp = root / 'release'; (temp / 'private_keys').mkdir(parents=True)
            calls = root / 'calls.txt'
            stub = bin_dir / 'security'
            stub.write_text('#!/bin/sh\necho "$@" >> "' + str(calls) + '"\n'); stub.chmod(0o755)
            (temp / 'original-keychains.txt').write_text('    "/Users/x/Library/Keychains/login.keychain-db"\n')
            keychain = temp / 'signing.keychain-db'; keychain.write_text('k')
            for name in ['cert.p12', 'installer.p12', 'private_keys/AuthKey_ABC.p8', 'keep.txt']:
                (temp / name).write_text('secret')
            result = self.sh(str(SCRIPTS / 'cleanup.sh'), env={'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'], 'RELEASE_SCRIPTS': str(SCRIPTS),
                                                               'RELEASE_TEMP': str(temp), 'KEYCHAIN_PATH': str(keychain)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), ['list-keychains -d user -s /Users/x/Library/Keychains/login.keychain-db',
                                                              'delete-keychain ' + str(keychain)])
            self.assertEqual(sorted(str(p.relative_to(temp)) for p in temp.rglob('*') if p.is_file()),
                             ['keep.txt', 'original-keychains.txt', 'signing.keychain-db'])

    def test_cleanup_is_a_no_op_when_prepare_never_ran(self):
        result = self.sh(str(SCRIPTS / 'cleanup.sh'), env={'RELEASE_SCRIPTS': str(SCRIPTS), 'RELEASE_TEMP': '', 'KEYCHAIN_PATH': ''})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_persist_env_refuses_multiline_values(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / 'env'
            ok = self.sh('-c', '. "$1"; persist_env A "b c"', 'x', str(SCRIPTS / 'lib.sh'), env={'GITHUB_ENV': str(env_file)})
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(env_file.read_text(), 'A=b c\n')
            bad = self.sh('-c', '. "$1"; persist_env A "$(printf "b\\nINJECTED=1")"', 'x', str(SCRIPTS / 'lib.sh'), env={'GITHUB_ENV': str(env_file)})
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(env_file.read_text(), 'A=b c\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
