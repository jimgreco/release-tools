#!/usr/bin/env python3
"""Release one app on the shared EC2 Docker Compose host from a prebuilt image.

Runs on the host (Amazon Linux python3: keep Python 3.9 syntax, stdlib only).
The CI workflow pipes this file over the pinned SSH connection:

    ssh host "python3 - --sha <40-hex> --manifest-b64 <base64 JSON>" < release.py

What it does, in order, and only for the services the manifest names:

 1. Takes both shared-host release locks (the legacy /tmp lock and
    ~/deploy/.app-release.lock), so it never overlaps another app release.
 2. Rebuilds the exact Compose invocation recorded on the running containers
    (project, working dir, ordered files) and refuses to continue unless each
    service's configuration hash matches the live container. Hand edits must be
    reconciled first; this never "fixes" drift.
 3. Pulls the exact-commit image(s) using the host's existing registry login.
 4. Writes a small override file that changes only each service's `image` and
    declared metadata environment (e.g. APP_BUILD), and proves the resulting
    model differs from the live one in nothing else.
 5. Tags the previous images for rollback, optionally takes and verifies a
    pg_dump of the app's own database, runs optional preflight and migration
    commands from the new image, then recreates only those services with
    --no-deps --no-build.
 6. Waits for the manifest's health command to pass inside each container. On
    failure it rolls back to the previous images, unless migrations already ran
    (then it stops the services, or rolls back only if the manifest allows it).

It never edits .env or shared Compose files, creates roles, prunes images, or
prints Compose/inspect output (resolved configuration contains credentials).
"""

import argparse
import base64
import copy
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")
IMAGE = re.compile(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+")
PROJECT = "deploy"
LOCKS = (Path("/tmp/codex-shared-host-release.lock"), Path("~/deploy/.app-release.lock"))
LOCK_WAIT_SECONDS = 15 * 60
MIN_FREE_BYTES = 1024 ** 3
KEEP_OVERRIDES = 5
DB_CONTAINER = "shared_db"


class ReleaseError(Exception):
    pass


def check(ok, message):
    if not ok:
        raise ReleaseError(message)


# ── subprocess helpers ────────────────────────────────────────────────────────

def run(args, input_text=None, json_output=False, env=None):
    """Run a command whose output may contain credentials: never echo it."""
    result = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise ReleaseError("Command failed: {} (output withheld)".format(" ".join(args[:3])))
    if not json_output:
        return result.stdout.strip()
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ReleaseError("Command returned invalid JSON: {}".format(" ".join(args[:3]))) from None


def run_visible(args, env=None):
    """Run an app-authored command (migration/preflight) with its own output shown."""
    sys.stdout.flush()
    return subprocess.run(args, stdin=subprocess.DEVNULL, env=env).returncode == 0


def run_quiet(args, env=None):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env).returncode == 0


def log(message):
    print(message, flush=True)


# ── manifest ─────────────────────────────────────────────────────────────────

def substitute(value, sha, now):
    if isinstance(value, list):
        return [substitute(item, sha, now) for item in value]
    if not isinstance(value, str):
        raise ReleaseError("Manifest values must be strings")
    return (value.replace("{sha}", sha).replace("{sha7}", sha[:7])
            .replace("{time}", now.strftime("%Y-%m-%dT%H:%M:%SZ")))


def load_manifest(raw):
    try:
        manifest = json.loads(base64.b64decode(raw, validate=True).decode())
    except (ValueError, TypeError):
        raise ReleaseError("Manifest must be base64-encoded JSON") from None
    check(isinstance(manifest, dict), "Manifest must be an object")
    check(isinstance(manifest.get("app"), str) and NAME.fullmatch(manifest["app"]), "Manifest needs a lowercase app name")
    check(isinstance(manifest.get("image"), str) and IMAGE.fullmatch(manifest["image"]),
          "Manifest image must be ghcr.io/<owner>/<name>")
    services = manifest.get("services")
    check(isinstance(services, dict) and services, "Manifest needs at least one service")
    for name, spec in services.items():
        check(NAME.fullmatch(name) and isinstance(spec, dict), "Invalid service entry")
        check(isinstance(spec.get("health"), list) and spec["health"], "Service {} needs a health command".format(name))
        check(re.fullmatch(r"[a-z0-9-]*", spec.get("tag_suffix", "")), "Invalid tag_suffix for " + name)
        for key in spec.get("metadata_env", {}):
            check(re.fullmatch(r"[A-Z][A-Z0-9_]*", key), "Invalid metadata variable " + key)
    profile = manifest.get("profile")
    check(profile is None or (isinstance(profile, str) and NAME.fullmatch(profile)), "Invalid profile")
    for key, source in manifest.get("interpolate_from_live", {}).items():
        check(re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and isinstance(source, str)
              and re.fullmatch(r"(image|env):[a-z0-9_.-]+(:[A-Z][A-Z0-9_]*)?", source), "Invalid interpolate_from_live " + key)
    for pattern in manifest.get("legacy_overrides", []):
        re.compile(pattern)
    backup = manifest.get("backup")
    if backup is not None:
        check(isinstance(backup, dict) and re.fullmatch(r"[a-z_][a-z0-9_]*", backup.get("database", ""))
              and re.fullmatch(r"[a-z_][a-z0-9_]*", backup.get("require_table", "")), "Backup needs database and require_table")
    for group in ("preflight", "migrate"):
        for step in manifest.get(group, {}).get("commands", []) if group == "migrate" else manifest.get(group, []):
            check(isinstance(step, dict) and step.get("service") in services and isinstance(step.get("command"), list),
                  "Invalid {} command".format(group))
    check(manifest.get("on_failure_after_migration", "stop") in ("stop", "rollback"), "Invalid on_failure_after_migration")
    check(isinstance(manifest.get("refuse_backwards", False), bool), "refuse_backwards must be true or false")
    return manifest


def target_image(manifest, service, sha):
    suffix = manifest["services"][service].get("tag_suffix", "")
    return "{}:{}{}".format(manifest["image"], sha, "-" + suffix if suffix else "")


def live_build(manifest, live):
    """The one commit every service runs, read from its exact-commit image tag."""
    shas = set()
    for service, spec in manifest["services"].items():
        check(service in live, service + " is not running; there is no live build to compare")
        suffix = spec.get("tag_suffix", "")
        pattern = re.escape(manifest["image"]) + r":([0-9a-f]{40})" + (re.escape("-" + suffix) if suffix else "")
        match = re.fullmatch(pattern, live[service]["Config"]["Image"])
        check(match, service + " does not run an exact-commit image of " + manifest["image"])
        shas.add(match.group(1))
    check(len(shas) == 1, "Services run different commits; reconcile before release")
    return shas.pop()


# ── locking ──────────────────────────────────────────────────────────────────

def acquire_locks(deadline):
    handles = []
    for path in LOCKS:
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise ReleaseError("Another release holds {}; retry later".format(path))
                time.sleep(5)
        handles.append(handle)
    return handles


# ── live state and Compose reconstruction ────────────────────────────────────

def env_map(values):
    return dict(item.split("=", 1) for item in values or [] if "=" in item)


def live_containers(services, execute=run):
    live = {}
    for service in services:
        ids = execute(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT,
                       "--filter", "label=com.docker.compose.service=" + service]).split()
        check(len(ids) <= 1, "More than one running container for " + service)
        if ids:
            live[service] = execute(["docker", "inspect", ids[0]], json_output=True)[0]
    return live


def compose_files(live):
    """Return the working dir and the longest recorded file chain (others must be prefixes)."""
    workdirs, chains = set(), []
    for service, item in live.items():
        labels = item["Config"].get("Labels") or {}
        check(labels.get("com.docker.compose.project") == PROJECT, service + " is not in the deploy Compose project")
        workdir = labels.get("com.docker.compose.project.working_dir")
        files = [name for name in labels.get("com.docker.compose.project.config_files", "").split(",") if name]
        check(workdir and files, service + " is missing its Compose labels")
        workdirs.add(workdir)
        chains.append([str(Path(name) if Path(name).is_absolute() else Path(workdir) / name) for name in files])
    check(len(workdirs) == 1, "Services were created from different Compose working directories")
    longest = max(chains, key=len)
    check(all(longest[:len(chain)] == chain for chain in chains), "Live Compose file chains diverge; reconcile before release")
    for path in longest:
        check(Path(path).is_file(), "A Compose file recorded on the live containers is missing")
    return workdirs.pop(), longest


def compose_binary():
    # Prefer the standalone binary the host used; config hashes depend on it.
    return ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]


def compose_command(workdir, files, profile):
    command = compose_binary() + ["-p", PROJECT, "--project-directory", workdir]
    if profile:
        command += ["--profile", profile]
    for path in files:
        command += ["-f", path]
    return command


def stdin_command(compose):
    command, parts = [], iter(compose)
    for part in parts:
        if part == "-f":
            next(parts)
        else:
            command.append(part)
    return command + ["-f", "-"]


def service_hashes(command, services, execute=run, model=None, env=None):
    result = {}
    for service in services:
        raw = execute(command + ["config", "--hash", service], input_text=None if model is None else json.dumps(model), env=env)
        fields = raw.split()
        check(len(fields) == 2 and fields[0] == service and HASH.fullmatch(fields[1]),
              "Compose returned an invalid configuration hash for " + service)
        result[service] = fields[1]
    return result


def scope_dependencies(model, services, recorded=None):
    """Mirror Compose 2.26.1 `up --no-deps`: drop depends_on edges outside the released set."""
    scoped = copy.deepcopy(model)
    for service in services:
        definition = scoped["services"][service]
        dependencies = definition.get("depends_on")
        if dependencies is None:
            continue
        keep = set(services) if recorded is None else recorded[service]
        if isinstance(dependencies, dict):
            retained = {name: value for name, value in dependencies.items() if name in keep}
        elif isinstance(dependencies, list):
            retained = [name for name in dependencies if name in keep]
        else:
            raise ReleaseError("Unsupported depends_on for " + service)
        if retained:
            definition["depends_on"] = retained
        else:
            definition.pop("depends_on")
    return scoped


def verify_live_hashes(compose, live, services, execute=run, env=None):
    expected = {s: (live[s]["Config"].get("Labels") or {}).get("com.docker.compose.config-hash") for s in services}
    check(all(value and HASH.fullmatch(value) for value in expected.values()), "Live containers lack configuration hashes")
    original = service_hashes(compose, services, execute, env=env)
    if original == expected:
        return
    model = execute(compose + ["config", "--format", "json"], json_output=True, env=env)
    rendered = stdin_command(compose)
    # Resolved models travel by stdin only. The rendering must be lossless, or a
    # dropped field could hide drift behind a matching hash.
    check(execute(rendered + ["config", "--format", "json"], input_text=json.dumps(model), json_output=True, env=env) == model,
          "Compose JSON rendering is not lossless; reconcile before release")
    # `up` hashes the resolved model (env_file already merged into environment),
    # and Compose 2.26.1 `up --no-deps` also drops dependencies outside the set.
    if service_hashes(rendered, services, execute, model, env) == expected:
        return
    if service_hashes(rendered, services, execute, scope_dependencies(model, services), env) == expected:
        return
    recorded = {}
    for service in services:
        labels = live[service]["Config"].get("Labels") or {}
        check("com.docker.compose.depends_on" in labels, "Live Compose configuration drift for " + service)
        recorded[service] = {part.split(":", 1)[0] for part in labels["com.docker.compose.depends_on"].split(",") if part}
    if service_hashes(rendered, services, execute, scope_dependencies(model, services, recorded), env) == expected:
        return
    raise ReleaseError("Live Compose configuration drift; reconcile before release")


def interpolation_env(manifest, live):
    env = dict(os.environ)
    for key, source in manifest.get("interpolate_from_live", {}).items():
        parts = source.split(":")
        item = live.get(parts[1])
        check(item is not None, "Interpolation source service {} is not running".format(parts[1]))
        if parts[0] == "image":
            env[key] = item["Config"]["Image"]
        else:
            check(len(parts) == 3, "env interpolation needs env:<service>:<NAME>")
            value = env_map(item["Config"].get("Env")).get(parts[2])
            check(value is not None, "Live {} has no {}".format(parts[1], parts[2]))
            env[key] = value
    return env


# ── candidate model ──────────────────────────────────────────────────────────

def own_override_name(app, sha):
    return "{}-image-{}.json".format(app, sha)


def removable(path, manifest):
    name = Path(path).name
    if re.fullmatch(re.escape(manifest["app"]) + r"-image-[0-9a-f]{40}\.json", name):
        return True
    return any(re.fullmatch(pattern, name) for pattern in manifest.get("legacy_overrides", []))


def normalized(model, manifest):
    """Drop exactly what a release may change: image, build and metadata env of released services."""
    result = copy.deepcopy(model)
    for service, spec in manifest["services"].items():
        definition = result["services"].get(service, {})
        definition.pop("image", None)
        definition.pop("build", None)
        environment = definition.get("environment")
        if isinstance(environment, dict):
            for key in spec.get("metadata_env", {}):
                environment.pop(key, None)
            if not environment:
                definition.pop("environment")
    return result


def validate_candidate(original, candidate, manifest, images, metadata):
    check(normalized(candidate, manifest) == normalized(original, manifest),
          "Candidate Compose model changes more than image and release metadata")
    for service in manifest["services"]:
        definition = candidate["services"][service]
        check(definition.get("image") == images[service], "Candidate image for {} is not the release image".format(service))
        environment = definition.get("environment") or {}
        for key, value in metadata[service].items():
            check(environment.get(key) == value, "Candidate metadata {} for {} is wrong".format(key, service))


# ── database backup ──────────────────────────────────────────────────────────

def backup_database(spec, app, sha, workdir):
    database, table = spec["database"], spec["require_table"]
    directory = Path(workdir) / "backups" / app
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    size = run(["docker", "exec", DB_CONTAINER, "psql", "-U", "admin", "-d", database, "-Atc",
                "select pg_database_size('{}')".format(database)])
    check(size.isdigit(), "Could not read the {} database size".format(database))
    stats = os.statvfs(str(directory))
    check(stats.f_bavail * stats.f_frsize > int(size) * 3 + 256 * 1024 ** 2, "Not enough free disk for a pre-release backup")
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = directory / "{}-{}-before-{}.dump".format(app, stamp, sha)
    partial = Path(str(target) + ".partial")
    old_umask = os.umask(0o077)
    try:
        with partial.open("wb") as output:
            result = subprocess.run(["docker", "exec", DB_CONTAINER, "pg_dump", "-U", "admin", "-d", database, "-Fc"],
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL)
    finally:
        os.umask(old_umask)
    if result.returncode:
        partial.unlink()
        raise ReleaseError("Pre-release backup of {} failed; nothing was changed".format(database))
    with partial.open("rb") as archive:
        listing = subprocess.run(["docker", "exec", "-i", DB_CONTAINER, "pg_restore", "--list"], stdin=archive,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if listing.returncode or not re.search(r"TABLE public {} ".format(re.escape(table)), listing.stdout):
        partial.unlink()
        raise ReleaseError("Pre-release backup of {} is unreadable or lacks {}; nothing was changed".format(database, table))
    partial.rename(target)
    keep = int(spec.get("keep", 20))
    for old in sorted(directory.glob("{}-*-before-*.dump".format(app)), reverse=True)[keep:]:
        old.unlink()
    log("Verified pre-release backup: {} ({} bytes)".format(target, target.stat().st_size))
    return str(target)


def docker_free_bytes():
    stats = os.statvfs(run(["docker", "info", "--format", "{{.DockerRootDir}}"]))
    return stats.f_bavail * stats.f_frsize


# ── release ──────────────────────────────────────────────────────────────────

def wait_healthy(compose, manifest, sha, now, env, attempts):
    for service, spec in manifest["services"].items():
        command = compose + ["exec", "-T", service] + substitute(spec["health"], sha, now)
        for attempt in range(attempts):
            if run_quiet(command, env=env):
                break
            if attempt == attempts - 1:
                return service
            time.sleep(2)
    return None


def running_images(compose, services, env):
    images = {}
    for service in services:
        container = run(compose + ["ps", "-q", service], env=env)
        check(container, service + " is not running after recreation")
        images[service] = run(["docker", "inspect", "--format", "{{.Image}}", container])
    return images


def prune_overrides(workdir, app, live_files):
    directory = Path(workdir) / "releases"
    if not directory.is_dir():
        return
    in_use = set(live_files)
    for path in sorted(directory.glob("{}-image-*.json".format(app)), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_OVERRIDES:]:
        if str(path) not in in_use:
            path.unlink()


def release(manifest, sha, check_only=False):
    services = list(manifest["services"])
    now = datetime.datetime.utcnow()
    live = live_containers(services)
    if not live:
        check(manifest.get("allow_not_running", False), "None of {} is running".format(", ".join(services)))
        log("{} is not running yet; first activation is a reviewed manual step. Nothing to deploy.".format(manifest["app"]))
        return {"status": "not-running"}
    check(set(live) == set(services), "Only some of {} are running; reconcile before release".format(", ".join(services)))

    workdir, files = compose_files(live)
    installed = run(compose_binary() + ["version", "--short"]).lstrip("v")
    for service, item in live.items():
        recorded = (item["Config"].get("Labels") or {}).get("com.docker.compose.version")
        check(recorded == installed, "Installed Compose differs from the one that created " + service)

    env = interpolation_env(manifest, live)
    compose = compose_command(workdir, files, manifest.get("profile"))
    verify_live_hashes(compose, live, services, env=env)
    images = {service: target_image(manifest, service, sha) for service in services}
    if all(live[s]["Config"]["Image"] == images[s] for s in services):
        log("{} already runs {}.".format(manifest["app"], sha))
        return {"status": "already-live"}
    if check_only:
        log(json.dumps({"app": manifest["app"], "preflight": "passed", "services": services}))
        return {"status": "preflight-passed"}

    check(docker_free_bytes() >= MIN_FREE_BYTES, "Less than 1 GiB free for Docker; no cleanup is implicit")

    # Existing host registry login only; never a runner credential.
    pulled = {}
    for service in services:
        run(["docker", "pull", "-q", images[service]])
        pulled[service] = run(["docker", "image", "inspect", "--format", "{{.Id}}", images[service]])
        check(IMAGE_ID.fullmatch(pulled[service]), "Pulled image has no immutable ID")

    metadata = {s: {k: substitute(v, sha, now) for k, v in manifest["services"][s].get("metadata_env", {}).items()}
                for s in services}
    releases = Path(workdir) / "releases"
    releases.mkdir(exist_ok=True)
    override = releases / own_override_name(manifest["app"], sha)
    overlay = {"services": {s: dict({"image": images[s]}, **({"environment": metadata[s]} if metadata[s] else {}))
                            for s in services}}
    override.write_text(json.dumps(overlay, indent=2) + "\n")
    candidate_files = [path for path in files if not removable(path, manifest)] + [str(override)]
    candidate = compose_command(workdir, candidate_files, manifest.get("profile"))
    validate_candidate(run(compose + ["config", "--format", "json"], json_output=True, env=env),
                       run(candidate + ["config", "--format", "json"], json_output=True, env=env),
                       manifest, images, metadata)

    records = Path(workdir) / "release-records"
    records.mkdir(exist_ok=True)
    record_path = records / "{}-{}.json".format(manifest["app"], sha)
    rollback_tags = {}
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for service in services:
        tag = "{}-rollback:{}-{}".format(service.replace("_", "-"), stamp, live[service]["Id"][:12])
        run(["docker", "image", "tag", live[service]["Image"], tag])
        rollback_tags[service] = tag
    record = {"app": manifest["app"], "sha": sha, "status": "prepared", "previousFiles": files,
              "candidateFiles": candidate_files,
              "previous": {s: {"container": live[s]["Id"], "image": live[s]["Image"], "imageName": live[s]["Config"]["Image"],
                               "rollbackTag": rollback_tags[s]} for s in services},
              "release": {s: {"image": images[s], "imageId": pulled[s]} for s in services}}

    def save(status, **extra):
        record.update(status=status, **extra)
        record_path.write_text(json.dumps(record, indent=2) + "\n")

    save("prepared")
    log("Previous images retained: " + ", ".join(rollback_tags.values()))

    if manifest.get("backup"):
        save("prepared", backup=backup_database(manifest["backup"], manifest["app"], sha, workdir))

    for step in manifest.get("preflight", []):
        command = candidate + ["run", "--rm", "--no-deps", "-T", step["service"]] + substitute(step["command"], sha, now)
        check(run_visible(command, env=env), "Preflight command failed; the current build keeps serving")

    # Re-read live state: nothing may have changed while we prepared.
    current = live_containers(services)
    check({s: current[s]["Id"] for s in current} == {s: live[s]["Id"] for s in services},
          "Live containers changed during preflight; stop and reconcile")

    migrated = False
    migrate = manifest.get("migrate", {})
    if migrate.get("commands"):
        if migrate.get("stop_first", False):
            run(compose + ["stop", "-t", "60"] + services, env=env)
        save("migrating")
        migrated = True
        for step in migrate["commands"]:
            command = candidate + ["run", "--rm", "--no-deps", "-T", step["service"]] + substitute(step["command"], sha, now)
            if not run_visible(command, env=env):
                return fail(compose, candidate, services, env, manifest, migrated, save, "Migration failed")

    run(candidate + ["up", "-d", "--no-deps", "--no-build", "--force-recreate"] + services, env=env)
    save("replaced-awaiting-health")
    running = running_images(candidate, services, env)
    if running != pulled:
        return fail(compose, candidate, services, env, manifest, migrated, save, "Recreated containers do not run the pulled images")
    unhealthy = wait_healthy(candidate, manifest, sha, now, env, int(manifest.get("health_attempts", 60)))
    if unhealthy:
        return fail(compose, candidate, services, env, manifest, migrated, save, unhealthy + " failed its health check")
    save("healthy")
    prune_overrides(workdir, manifest["app"], candidate_files + files)
    summary = {"status": "healthy", "app": manifest["app"], "sha": sha, "record": str(record_path),
               "rollbackImages": rollback_tags}
    log(json.dumps(summary))
    return summary


def fail(compose, candidate, services, env, manifest, migrated, save, reason):
    if migrated and manifest.get("on_failure_after_migration", "stop") == "stop":
        run_quiet(candidate + ["stop", "-t", "60"] + services, env=env)
        save("failed-stopped-after-migration", reason=reason)
        raise ReleaseError(reason + ". Migrations ran, so {} is stopped: deploy a compatible fixed build. "
                                    "Previous images are tagged for forensics.".format(", ".join(services)))
    # The previous model still names the previous images, which the rollback tags keep present.
    rolled_back = run_quiet(compose + ["up", "-d", "--no-deps", "--no-build", "--force-recreate"] + services, env=env)
    save("failed-rolled-back" if rolled_back else "failed-rollback-failed", reason=reason)
    raise ReleaseError(reason + ("; rolled back to the previous images." if rolled_back
                                 else "; ROLLBACK ALSO FAILED - inspect the release record."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True, help="Exact 40-character commit SHA")
    parser.add_argument("--manifest-b64", required=True, help="Base64 of the app's server release manifest JSON")
    parser.add_argument("--check", action="store_true", help="Read-only preflight: verify live config, change nothing")
    parser.add_argument("--live-build", action="store_true",
                        help="Read-only: print the commit the services run now (from their image tags), change nothing")
    args = parser.parse_args(argv)
    check(SHA.fullmatch(args.sha), "Release requires an exact lowercase 40-character commit SHA")
    manifest = load_manifest(args.manifest_b64)
    if args.live_build:
        print(live_build(manifest, live_containers(list(manifest["services"]))), flush=True)
        return
    handles = acquire_locks(time.time() + LOCK_WAIT_SECONDS)
    try:
        release(manifest, args.sha, check_only=args.check)
    finally:
        for handle in reversed(handles):
            handle.close()


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        print("release failed: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        # Never dump exception text: it can include command input or resolved config.
        print("release failed ({}); inspect ~/deploy/release-records".format(type(error).__name__), file=sys.stderr)
        sys.exit(1)
