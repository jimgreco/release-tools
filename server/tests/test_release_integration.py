"""End-to-end test of server/release.py against a real Docker daemon.

Skipped when Docker or Compose is unavailable. Uses a throwaway local registry,
a project named `rtest` and a Postgres container named `rtest_shared_db`, so it
never touches a real `deploy` project. Run from the repo root:

    python3 -m unittest discover -s server/tests -p 'test_release_integration.py'
"""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release  # noqa: E402

REGISTRY = "localhost:5055"
REPO = REGISTRY + "/jimgreco/demo"
GOOD, GOOD2, BAD = "1" * 40, "2" * 40, "3" * 40
PROJECT = "rtest"


def sh(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def docker_available():
    return shutil.which("docker") and sh("docker", "info", check=False).returncode == 0 and release.compose_binary()


COMPOSE = """
services:
  db:
    image: postgres:16-alpine
    container_name: rtest_shared_db
    environment:
      POSTGRES_USER: admin
      POSTGRES_PASSWORD: test-only
      POSTGRES_DB: demo
  demo:
    image: ${DEMO_IMAGE:-localhost:5055/jimgreco/demo:base}
    command: ["sh", "-c", "sleep 3600"]
    environment:
      APP_BUILD: ${DEMO_BUILD:-base}
      RUNTIME_SETTING: keep-me
    depends_on: [db]
"""


@unittest.skipUnless(docker_available(), "Docker daemon and Compose are required")
class ReleaseIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rtest-"))
        # Anonymous pulls only: never invoke a desktop credential helper.
        cls.saved_docker_config = os.environ.get("DOCKER_CONFIG")
        (cls.tmp / "docker-config").mkdir()
        (cls.tmp / "docker-config" / "config.json").write_text("{}")
        os.environ["DOCKER_CONFIG"] = str(cls.tmp / "docker-config")
        sh("docker", "rm", "-f", "rtest_registry", check=False)
        sh("docker", "run", "-d", "--name", "rtest_registry", "-p", "5055:5000", "registry:2")
        for tag, healthy in (("base", True), (GOOD, True), (GOOD2, True), (BAD, False)):
            context = cls.tmp / ("img-" + tag[:6])
            context.mkdir()
            (context / "Dockerfile").write_text("FROM busybox\n" + ("RUN touch /ok\n" if healthy else ""))
            sh("docker", "build", "-q", "-t", REPO + ":" + tag, str(context))
            for _ in range(20):
                if sh("docker", "push", "-q", REPO + ":" + tag, check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("could not push to test registry")
            sh("docker", "image", "rm", REPO + ":" + tag)  # force real pulls
        cls.workdir = cls.tmp / "deploy"
        cls.workdir.mkdir()
        (cls.workdir / "docker-compose.yml").write_text(COMPOSE)
        cls.compose = release.compose_binary() + ["-p", PROJECT, "--project-directory", str(cls.workdir),
                                                  "-f", str(cls.workdir / "docker-compose.yml")]
        sh(*cls.compose, "up", "-d")
        for _ in range(60):
            if sh("docker", "exec", "rtest_shared_db", "pg_isready", "-U", "admin", check=False).returncode == 0:
                break
            time.sleep(1)
        sh("docker", "exec", "rtest_shared_db", "psql", "-U", "admin", "-d", "demo", "-c", "create table items (id int)")
        cls.saved = (release.PROJECT, release.IMAGE, release.LOCKS, release.DB_CONTAINER, release.docker_free_bytes)
        release.PROJECT = PROJECT
        release.IMAGE = re.compile(re.escape(REGISTRY) + r"/[a-z0-9_.-]+/[a-z0-9_.-]+")
        release.LOCKS = (cls.tmp / "legacy.lock", cls.workdir / ".app-release.lock")
        release.DB_CONTAINER = "rtest_shared_db"
        release.docker_free_bytes = lambda: 10 * 1024 ** 3

    @classmethod
    def tearDownClass(cls):
        release.PROJECT, release.IMAGE, release.LOCKS, release.DB_CONTAINER, release.docker_free_bytes = cls.saved
        sh(*cls.compose, "down", "-v", check=False)
        sh("docker", "rm", "-f", "rtest_registry", check=False)
        for line in sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}").stdout.split():
            if line.startswith((REPO, "demo-rollback:")):
                sh("docker", "image", "rm", "-f", line, check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        if cls.saved_docker_config is None:
            os.environ.pop("DOCKER_CONFIG", None)
        else:
            os.environ["DOCKER_CONFIG"] = cls.saved_docker_config

    def manifest(self, **extra):
        value = {"app": "demo", "image": REPO,
                 "services": {"demo": {"health": ["sh", "-c", 'test -f /ok && test "$APP_BUILD" = {sha7}'],
                                       "metadata_env": {"APP_BUILD": "{sha7}"}}},
                 "health_attempts": 5}
        value.update(extra)
        return release.load_manifest(base64.b64encode(json.dumps(value).encode()).decode())

    def live(self):
        container = sh(*self.compose, "ps", "-q", "demo").stdout.strip()
        item = json.loads(sh("docker", "inspect", container).stdout)[0]
        env = release.env_map(item["Config"]["Env"])
        return item["Config"]["Image"], env.get("APP_BUILD"), env.get("RUNTIME_SETTING"), item

    def record(self, sha):
        return json.loads((self.workdir / "release-records" / "demo-{}.json".format(sha)).read_text())

    def test_1_release_rollback_and_migration_failure(self):
        db_before = json.loads(sh("docker", "inspect", "rtest_shared_db").stdout)[0]["Id"]

        # Read-only check changes nothing.
        self.assertEqual(release.release(self.manifest(), GOOD, check_only=True)["status"], "preflight-passed")
        self.assertEqual(self.live()[0], REPO + ":base")

        # Good release with a verified backup and a passing migration step.
        summary = release.release(self.manifest(
            backup={"database": "demo", "require_table": "items", "keep": 2},
            preflight=[{"service": "demo", "command": ["test", "-f", "/ok"]}],
            migrate={"commands": [{"service": "demo", "command": ["true"]}]}), GOOD)
        self.assertEqual(summary["status"], "healthy")
        image, build, setting, _ = self.live()
        self.assertEqual((image, build, setting), (REPO + ":" + GOOD, GOOD[:7], "keep-me"))
        self.assertTrue(self.record(GOOD)["backup"].endswith(".dump"))
        self.assertTrue(Path(self.record(GOOD)["backup"]).stat().st_size > 0)
        # --no-deps: the database container was not recreated.
        self.assertEqual(json.loads(sh("docker", "inspect", "rtest_shared_db").stdout)[0]["Id"], db_before)

        # Same SHA again is a no-op.
        self.assertEqual(release.release(self.manifest(), GOOD)["status"], "already-live")

        # A second release works from the previous release's override chain.
        self.assertEqual(release.release(self.manifest(), GOOD2)["status"], "healthy")
        self.assertEqual(self.live()[0], REPO + ":" + GOOD2)
        labels = self.live()[3]["Config"]["Labels"]["com.docker.compose.project.config_files"]
        self.assertNotIn("demo-image-" + GOOD, labels)  # superseded override dropped from chain

        # Unhealthy release rolls back automatically.
        with self.assertRaises(release.ReleaseError):
            release.release(self.manifest(), BAD)
        self.assertEqual(self.live()[0], REPO + ":" + GOOD2)
        self.assertEqual(self.record(BAD)["status"], "failed-rolled-back")

        # Failed migration after stop_first leaves the service stopped, not rolled back.
        with self.assertRaises(release.ReleaseError):
            release.release(self.manifest(migrate={"stop_first": True, "commands": [
                {"service": "demo", "command": ["false"]}]}), GOOD)
        self.assertEqual(sh(*self.compose, "ps", "-q", "demo").stdout.strip(), "")
        self.assertEqual(self.record(GOOD)["status"], "failed-stopped-after-migration")

    def test_2_drift_is_refused(self):
        # Restore a running service, then edit the Compose file by hand.
        sh(*self.compose, "up", "-d", "--no-deps", "demo")
        path = self.workdir / "docker-compose.yml"
        original = path.read_text()
        try:
            path.write_text(original.replace("keep-me", "hand-edited"))
            with self.assertRaises(release.ReleaseError) as caught:
                release.release(self.manifest(), GOOD2)
            self.assertIn("drift", str(caught.exception))
        finally:
            path.write_text(original)


if __name__ == "__main__":
    unittest.main()
