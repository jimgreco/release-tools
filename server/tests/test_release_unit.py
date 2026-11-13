"""Unit tests for server/release.py logic that needs no Docker daemon."""
import base64
import copy
import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release  # noqa: E402

SHA = "a" * 40


def manifest(**extra):
    value = {
        "app": "demo",
        "image": "ghcr.io/jimgreco/demo",
        "services": {"demo": {"health": ["true"], "metadata_env": {"APP_BUILD": "{sha7}"}}},
    }
    value.update(extra)
    return value


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_loads(self):
        self.assertEqual(release.load_manifest(encoded(manifest()))["app"], "demo")

    def test_rejects_non_ghcr_image(self):
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(manifest(image="docker.io/library/demo")))

    def test_rejects_service_without_health(self):
        bad = manifest(services={"demo": {}})
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(bad))

    def test_rejects_unknown_command_service(self):
        bad = manifest(migrate={"commands": [{"service": "other", "command": ["x"]}]})
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(bad))

    def test_rejects_bad_backup(self):
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(manifest(backup={"database": "x; drop", "require_table": "t"})))

    def test_rejects_bad_interpolation(self):
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(manifest(interpolate_from_live={"X": "shell:rm"})))

    def test_rejects_non_boolean_refuse_backwards(self):
        with self.assertRaises(release.ReleaseError):
            release.load_manifest(encoded(manifest(refuse_backwards="yes")))

    def test_live_build_reads_image_tags(self):
        value = manifest(services={"api": {"health": ["true"], "tag_suffix": "api"}, "web": {"health": ["true"], "tag_suffix": "web"}})
        live = {"api": {"Config": {"Image": "ghcr.io/jimgreco/demo:" + SHA + "-api"}},
                "web": {"Config": {"Image": "ghcr.io/jimgreco/demo:" + SHA + "-web"}}}
        self.assertEqual(release.live_build(value, live), SHA)

    def test_live_build_refuses_unreadable_or_mixed_state(self):
        other = "b" * 40
        two = manifest(services={"a": {"health": ["true"]}, "b": {"health": ["true"]}})
        cases = [
            (manifest(), {}),
            (manifest(), {"demo": {"Config": {"Image": "demo:" + SHA}}}),
            (manifest(), {"demo": {"Config": {"Image": "ghcr.io/jimgreco/demo:latest"}}}),
            (manifest(), {"demo": {"Config": {"Image": "ghcr.io/jimgreco/demo:" + SHA + "-api"}}}),
            (two, {"a": {"Config": {"Image": "ghcr.io/jimgreco/demo:" + SHA}},
                   "b": {"Config": {"Image": "ghcr.io/jimgreco/demo:" + other}}}),
        ]
        for value, live in cases:
            with self.assertRaises(release.ReleaseError):
                release.live_build(value, live)

    def test_target_image_suffix(self):
        value = manifest(services={"api": {"health": ["true"], "tag_suffix": "api"}})
        self.assertEqual(release.target_image(value, "api", SHA), "ghcr.io/jimgreco/demo:" + SHA + "-api")
        self.assertEqual(release.target_image(manifest(), "demo", SHA), "ghcr.io/jimgreco/demo:" + SHA)

    def test_substitute(self):
        now = datetime.datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(release.substitute(["{sha}", "{sha7}", "t={time}"], SHA, now),
                         [SHA, SHA[:7], "t=2026-01-02T03:04:05Z"])


class CandidateTests(unittest.TestCase):
    def original(self):
        return {"services": {
            "demo": {"image": "old", "build": {"context": "/x"}, "environment": {"DATABASE_URL": "pg://", "APP_BUILD": "old"}},
            "db": {"image": "postgres"}}}

    def candidate(self):
        value = self.original()
        value["services"]["demo"]["image"] = "ghcr.io/jimgreco/demo:" + SHA
        value["services"]["demo"]["environment"]["APP_BUILD"] = SHA[:7]
        return value

    def test_image_and_metadata_change_passes(self):
        release.validate_candidate(self.original(), self.candidate(), manifest(),
                                   {"demo": "ghcr.io/jimgreco/demo:" + SHA}, {"demo": {"APP_BUILD": SHA[:7]}})

    def test_runtime_env_change_fails(self):
        candidate = self.candidate()
        candidate["services"]["demo"]["environment"]["DATABASE_URL"] = "pg://elsewhere"
        with self.assertRaises(release.ReleaseError):
            release.validate_candidate(self.original(), candidate, manifest(),
                                       {"demo": "ghcr.io/jimgreco/demo:" + SHA}, {"demo": {"APP_BUILD": SHA[:7]}})

    def test_other_service_change_fails(self):
        candidate = self.candidate()
        candidate["services"]["db"]["image"] = "postgres:17"
        with self.assertRaises(release.ReleaseError):
            release.validate_candidate(self.original(), candidate, manifest(),
                                       {"demo": "ghcr.io/jimgreco/demo:" + SHA}, {"demo": {"APP_BUILD": SHA[:7]}})

    def test_wrong_image_fails(self):
        candidate = self.candidate()
        candidate["services"]["demo"]["image"] = "ghcr.io/jimgreco/demo:latest"
        with self.assertRaises(release.ReleaseError):
            release.validate_candidate(self.original(), candidate, manifest(),
                                       {"demo": "ghcr.io/jimgreco/demo:" + SHA}, {"demo": {"APP_BUILD": SHA[:7]}})

    def test_dropping_legacy_metadata_env_is_allowed_only_for_declared_keys(self):
        original = self.original()
        original["services"]["demo"]["environment"]["GIT_COMMIT"] = "abc"
        candidate = self.candidate()
        with self.assertRaises(release.ReleaseError):
            release.validate_candidate(original, candidate, manifest(),
                                       {"demo": "ghcr.io/jimgreco/demo:" + SHA}, {"demo": {"APP_BUILD": SHA[:7]}})

    def test_removable_overrides(self):
        value = manifest(legacy_overrides=[r"app-release-.*\.json"])
        self.assertTrue(release.removable("/d/releases/demo-image-" + "b" * 40 + ".json", value))
        self.assertTrue(release.removable("/d/app-release-xyz.json", value))
        self.assertFalse(release.removable("/d/docker-compose.override.yml", value))
        self.assertFalse(release.removable("/d/releases/other-image-" + "b" * 40 + ".json", value))


class DependencyTests(unittest.TestCase):
    model = {"services": {
        "web": {"depends_on": {"api": {"condition": "service_started"}, "db": {"condition": "service_started"}}},
        "api": {"depends_on": ["db"]},
        "db": {}}}

    def test_scope_keeps_internal_edges(self):
        scoped = release.scope_dependencies(self.model, ["web", "api"])
        self.assertEqual(scoped["services"]["web"]["depends_on"], {"api": {"condition": "service_started"}})
        self.assertNotIn("depends_on", scoped["services"]["api"])
        self.assertIn("db", self.model["services"]["web"]["depends_on"])  # input untouched

    def test_scope_with_recorded_set(self):
        scoped = release.scope_dependencies(self.model, ["web"], {"web": {"db"}})
        self.assertEqual(scoped["services"]["web"]["depends_on"], {"db": {"condition": "service_started"}})


class FakeCompose:
    """Fake `execute` answering config/hash calls from a dict of models."""

    def __init__(self, files_hash, resolved_hash, scoped_hash, lossy=False):
        self.files_hash, self.resolved_hash, self.scoped_hash, self.lossy = files_hash, resolved_hash, scoped_hash, lossy
        self.model = {"services": {"demo": {"image": "x", "depends_on": {"db": {}}}, "db": {}}}

    def __call__(self, args, input_text=None, json_output=False, env=None):
        if args[-2:] == ["--format", "json"]:
            if input_text is None:
                return copy.deepcopy(self.model)
            rendered = json.loads(input_text)
            if self.lossy:
                rendered["services"]["demo"].pop("image", None)
            return rendered
        if "--hash" in args:
            if input_text is None:
                value = self.files_hash
            else:
                model = json.loads(input_text)
                value = self.scoped_hash if "depends_on" not in model["services"]["demo"] else self.resolved_hash
            return "demo " + value
        raise AssertionError(args)


def live(hash_value, depends_on=None):
    labels = {"com.docker.compose.config-hash": hash_value}
    if depends_on is not None:
        labels["com.docker.compose.depends_on"] = depends_on
    return {"demo": {"Config": {"Labels": labels}}}


class HashTests(unittest.TestCase):
    A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64

    def test_direct_match(self):
        release.verify_live_hashes(["compose", "-f", "x"], live(self.A), ["demo"], FakeCompose(self.A, self.B, self.C))

    def test_resolved_model_match(self):
        release.verify_live_hashes(["compose", "-f", "x"], live(self.B), ["demo"], FakeCompose(self.A, self.B, self.C))

    def test_no_deps_match(self):
        release.verify_live_hashes(["compose", "-f", "x"], live(self.C), ["demo"], FakeCompose(self.A, self.B, self.C))

    def test_drift_fails(self):
        with self.assertRaises(release.ReleaseError):
            release.verify_live_hashes(["compose", "-f", "x"], live(self.D, ""), ["demo"], FakeCompose(self.A, self.B, self.C))

    def test_lossy_rendering_fails_closed(self):
        with self.assertRaises(release.ReleaseError):
            release.verify_live_hashes(["compose", "-f", "x"], live(self.C), ["demo"], FakeCompose(self.A, self.B, self.C, lossy=True))


class ComposeFileTests(unittest.TestCase):
    def container(self, workdir, files):
        return {"Config": {"Labels": {"com.docker.compose.project": "deploy",
                                      "com.docker.compose.project.working_dir": workdir,
                                      "com.docker.compose.project.config_files": ",".join(files)}}}

    def test_prefix_chains_use_longest(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.yml", "b.yml"):
                Path(directory, name).write_text("services: {}\n")
            a, b = str(Path(directory, "a.yml")), str(Path(directory, "b.yml"))
            workdir, files = release.compose_files({"x": self.container(directory, [a]), "y": self.container(directory, [a, b])})
            self.assertEqual((workdir, files), (directory, [a, b]))

    def test_diverging_chains_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.yml", "b.yml", "c.yml"):
                Path(directory, name).write_text("services: {}\n")
            a, b, c = (str(Path(directory, n)) for n in ("a.yml", "b.yml", "c.yml"))
            with self.assertRaises(release.ReleaseError):
                release.compose_files({"x": self.container(directory, [a, b]), "y": self.container(directory, [a, c])})

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(release.ReleaseError):
                release.compose_files({"x": self.container(directory, [str(Path(directory, "gone.yml"))])})


if __name__ == "__main__":
    unittest.main()
