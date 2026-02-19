#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT_PATH = Path(__file__).resolve().parent / "release_flow.py"
SPEC = importlib.util.spec_from_file_location("release_flow", SCRIPT_PATH)
release_flow = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = release_flow
SPEC.loader.exec_module(release_flow)


class ReleaseFlowHelperTests(unittest.TestCase):
    def test_read_chart_meta(self) -> None:
        with TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "Chart.yaml"
            chart_path.write_text(
                textwrap.dedent(
                    """\
                    apiVersion: v2
                    name: apollo-portal
                    version: 0.11.0
                    appVersion: 2.5.0
                    """
                ),
                encoding="utf-8",
            )
            meta = release_flow.read_chart_meta("apollo-portal", chart_path)

        self.assertEqual(meta.version, "0.11.0")
        self.assertEqual(meta.app_version, "2.5.0")

    def test_extract_version_changes_from_diff(self) -> None:
        diff_text = textwrap.dedent(
            """\
            diff --git a/apollo-portal/Chart.yaml b/apollo-portal/Chart.yaml
            index 111..222 100644
            --- a/apollo-portal/Chart.yaml
            +++ b/apollo-portal/Chart.yaml
            @@
            -version: 0.10.0
            +version: 0.11.0
            -appVersion: 2.4.0
            +appVersion: 2.5.0
            diff --git a/apollo-service/Chart.yaml b/apollo-service/Chart.yaml
            index 333..444 100644
            --- a/apollo-service/Chart.yaml
            +++ b/apollo-service/Chart.yaml
            @@
            -version: 0.10.0
            +version: 0.11.0
            """
        )
        changes = release_flow.extract_version_changes_from_diff(diff_text)

        self.assertEqual(changes["apollo-portal"]["version"], ("0.10.0", "0.11.0"))
        self.assertEqual(changes["apollo-portal"]["appVersion"], ("2.4.0", "2.5.0"))
        self.assertEqual(changes["apollo-service"]["version"], ("0.10.0", "0.11.0"))
        self.assertNotIn("appVersion", changes["apollo-service"])

    def test_read_latest_versions_from_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            index_path = Path(temp_dir) / "index.yaml"
            index_path.write_text(
                textwrap.dedent(
                    """\
                    apiVersion: v1
                    entries:
                      apollo-portal:
                        - version: 0.9.0
                        - version: 0.11.0
                        - version: 0.10.0
                      apollo-service:
                        - version: 0.10.0
                        - version: 0.11.0
                    """
                ),
                encoding="utf-8",
            )
            latest = release_flow.read_latest_versions_from_index(index_path)

        self.assertEqual(latest["apollo-portal"], "0.11.0")
        self.assertEqual(latest["apollo-service"], "0.11.0")

    def test_build_release_branch(self) -> None:
        self.assertEqual(
            release_flow.build_release_branch("0.11.0", "0.11.0"),
            "codex/helm-release-0.11.0",
        )
        self.assertEqual(
            release_flow.build_release_branch("0.11.0", "0.11.1"),
            "codex/helm-release-0.11.0-0.11.1",
        )

    def test_whitelist_matching(self) -> None:
        paths = [
            "apollo-portal/Chart.yaml",
            "apollo-service/Chart.yaml",
            "docs/apollo-portal-0.11.0.tgz",
            "docs/apollo-service-0.11.0.tgz",
            "docs/index.yaml",
            "README.md",
        ]
        disallowed = release_flow.find_disallowed_paths(paths)
        self.assertEqual(disallowed, ["README.md"])


if __name__ == "__main__":
    unittest.main()
