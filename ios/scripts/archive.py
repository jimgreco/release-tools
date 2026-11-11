#!/usr/bin/env python3
"""Run `xcodebuild archive` with signing settings derived from the release policy.

Caller build settings come from RELEASE_EXTRA_BUILD_SETTINGS, one KEY=VALUE per
line. Values may use these placeholders (expanded here, never by a shell):
  @BUILD_NUMBER@  @GIT_SHA@  @GIT_SHA7@
  @PROFILE_UUID:<name>@      uuid of policy.profiles.<name>
  @MAC_PROFILE_UUID:<name>@  uuid of policy.macProfiles.<name>
  @ENV:<NAME>@               value of a non-empty environment variable
Signing-critical settings are owned by the engine and cannot be overridden.
Setting values are never printed by this script.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

spec = importlib.util.spec_from_file_location('verify_release', Path(__file__).with_name('verify-release.py'))
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

RESERVED = {'DEVELOPMENT_TEAM', 'CODE_SIGN_STYLE', 'CODE_SIGN_IDENTITY', 'CURRENT_PROJECT_VERSION', 'OTHER_CODE_SIGN_FLAGS',
            'CODE_SIGNING_ALLOWED', 'CODE_SIGNING_REQUIRED', 'CODE_SIGN_ENTITLEMENTS', 'CODE_SIGN_KEYCHAIN',
            'PROVISIONING_PROFILE', 'DEVELOPMENT_TEAM_ID'}
LINE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(\[[A-Za-z0-9_=*.,]+\])?=(.*)')
PLACEHOLDER = re.compile(r'@([A-Z][A-Z0-9_]*)(?::([A-Za-z0-9_-]+))?@')
DESTINATIONS = {'ios': 'generic/platform=iOS', 'mac': 'generic/platform=macOS,variant=Mac Catalyst'}


def expand(value, policy, build, sha, env):
    def replace(match):
        kind, arg = match.group(1), match.group(2)
        if kind == 'BUILD_NUMBER' and arg is None:
            return build
        if kind == 'GIT_SHA' and arg is None:
            return sha
        if kind == 'GIT_SHA7' and arg is None:
            return sha[:7]
        if kind in ('PROFILE_UUID', 'MAC_PROFILE_UUID') and arg:
            profiles = policy.get('profiles' if kind == 'PROFILE_UUID' else 'macProfiles') or {}
            v.check(arg in profiles, 'Unknown profile placeholder: ' + arg)
            return profiles[arg]['uuid']
        if kind == 'ENV' and arg:
            v.check(env.get(arg), 'Build setting placeholder environment variable is empty: ' + arg)
            return env[arg]
        raise ValueError('Unknown build setting placeholder: ' + match.group(0))
    return PLACEHOLDER.sub(replace, value)


def parse_settings(text, policy, build, sha, env):
    settings = []
    seen = set()
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = LINE.fullmatch(line)
        v.check(match, 'Build settings must be KEY=VALUE lines with a valid setting name: ' + line.split('=', 1)[0][:80])
        key, value = match.group(1) + (match.group(2) or ''), match.group(3)
        v.check(match.group(1) not in RESERVED, 'Build setting is owned by the release engine: ' + match.group(1))
        v.check(key not in seen, 'Duplicate build setting: ' + key)
        seen.add(key)
        settings.append(key + '=' + expand(value, policy, build, sha, env))
    return settings


def archive_command(policy, platform, scheme, archive_path, keychain, build, sha, extra='', env=None,
                    project=None, workspace=None, configuration='Release'):
    v.check(platform in DESTINATIONS, 'Unknown release platform.')
    v.profiles_for(policy, platform)
    v.check(bool(project) != bool(workspace), 'Exactly one of project or workspace is required.')
    v.check(scheme and keychain and archive_path, 'Scheme, keychain and archive path are required.')
    v.check(re.fullmatch('[0-9a-f]{40}', sha or '') and re.fullmatch('[1-9][0-9]*', build or ''), 'Full source SHA and positive build required.')
    command = ['xcodebuild', 'archive']
    command += ['-project', project] if project else ['-workspace', workspace]
    command += ['-scheme', scheme, '-configuration', configuration, '-destination', DESTINATIONS[platform],
                '-archivePath', archive_path,
                'DEVELOPMENT_TEAM=' + policy['teamId'], 'CODE_SIGN_STYLE=Manual',
                'CODE_SIGN_IDENTITY=' + policy['certificateSha1'], 'CURRENT_PROJECT_VERSION=' + build,
                'OTHER_CODE_SIGN_FLAGS=--keychain ' + keychain]
    return command + parse_settings(extra, policy, build, sha, env if env is not None else {})


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['policy', 'platform', 'project', 'workspace', 'scheme', 'configuration', 'archive-path', 'keychain', 'build', 'sha']:
        parser.add_argument('--' + name)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    policy = v.load_policy(args.policy)
    command = archive_command(policy, args.platform or 'ios', args.scheme, args.archive_path, args.keychain, args.build, args.sha,
                              os.environ.get('RELEASE_EXTRA_BUILD_SETTINGS', ''), dict(os.environ),
                              args.project or None, args.workspace or None, args.configuration or 'Release')
    if args.dry_run:
        # Setting names only; values may contain configuration secrets.
        print(' '.join(command[:2] + [c.split('=', 1)[0] + '=…' if '=' in c else c for c in command[2:]]))
        return
    sys.stdout.flush()
    result = subprocess.run(command)
    v.check(result.returncode == 0, 'xcodebuild archive failed (exit ' + str(result.returncode) + ').')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error))
