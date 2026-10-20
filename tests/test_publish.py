"""Reject wrong repository metadata and mismatched release tags before publishing."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_repository", ROOT / "tools/configure_repository.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "version": "0.1.0", "documentation": "https://github.com/sample/apartment",
            "issue_tracker": "https://github.com/sample/apartment/issues", "codeowners": ["@sample"],
        }

    def test_github_url_normalization(self):
        self.assertEqual(publish.repository_name("https://github.com/sample/apartment.git"), "sample/apartment")

    def test_non_github_and_nested_paths_rejected(self):
        for value in ("https://other.test/sample/apartment", "sample/apartment/tree/main", "../apartment"):
            with self.assertRaises(ValueError):
                publish.repository_name(value)

    def test_matching_repository_and_release(self):
        publish.check_metadata(self.manifest, "sample/apartment", "v0.1.0")

    def test_release_version_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            publish.check_metadata(self.manifest, "sample/apartment", "v0.2.0")

    def test_build_identifiers_are_not_release_versions(self):
        for version in ("main", "bbe7f8a", "0.2.0+build.42", "01.2.0"):
            with self.assertRaises(ValueError):
                publish.check_metadata(self.manifest | {"version": version}, "sample/apartment")

    def test_runtime_version_matches_manifest(self):
        import json
        from custom_components.apartment_comfort.version import VERSION
        manifest = json.loads(publish.MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(VERSION, manifest["version"])

    def test_wrong_repository_rejected(self):
        with self.assertRaises(ValueError):
            publish.check_metadata(self.manifest, "other/apartment")

    def test_missing_owner_rejected(self):
        self.manifest["codeowners"] = []
        with self.assertRaises(ValueError):
            publish.check_metadata(self.manifest, "sample/apartment")
