#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import release_flow
import release_notes_builder
from release_flow import ReleaseFlow


class ReleaseNotesBuilderTest(unittest.TestCase):
    def test_parse_semver(self) -> None:
        self.assertEqual(release_notes_builder.parse_semver("2.5.0"), (2, 5, 0))
        with self.assertRaises(ValueError):
            release_notes_builder.parse_semver("2.5")

    def test_parse_highlight_pr_numbers(self) -> None:
        self.assertEqual(
            release_notes_builder.parse_highlight_pr_numbers("5336, 5361 5365,5336"),
            [5336, 5361, 5365],
        )
        with self.assertRaises(ValueError):
            release_notes_builder.parse_highlight_pr_numbers("5336,abc")

    def test_parse_change_entries(self) -> None:
        content = """Changes by Version
==================
Release Notes.

Apollo 2.5.0

------------------
* [Feature A](https://github.com/apolloconfig/apollo/pull/1)
* [Fix B](https://github.com/apolloconfig/apollo/pull/2)

------------------
All issues and pull requests are [here](https://github.com/apolloconfig/apollo/milestone/16?closed=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGES.md"
            path.write_text(content, encoding="utf-8")
            entries, milestone = release_notes_builder.parse_change_entries(path, "2.5.0")

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].summary, "Feature A")
        self.assertIn("milestone/16", milestone)

    def test_build_highlights_uses_selected_prs(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Feature: Support incremental sync",
                summary="Feature: Support incremental sync",
                pr_url="https://github.com/apolloconfig/apollo/pull/11",
                pr_number=11,
            ),
            release_notes_builder.ChangeEntry(
                raw_text="Fix: permission validation",
                summary="Fix: permission validation",
                pr_url="https://github.com/apolloconfig/apollo/pull/12",
                pr_number=12,
            ),
            release_notes_builder.ChangeEntry(
                raw_text="Docs: update guide",
                summary="Docs: update guide",
                pr_url="https://github.com/apolloconfig/apollo/pull/13",
                pr_number=13,
            ),
        ]

        highlights = release_notes_builder.build_highlights(
            entries,
            "2.5.0",
            highlight_pr_numbers=[12],
        )
        self.assertEqual(len(highlights), 1)
        self.assertIn("permission validation", highlights[0].title.lower())

    def test_format_change_lines_uses_changes_summary_and_author(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Feature: Support incremental sync",
                summary="Feature: Support incremental sync",
                pr_url="https://github.com/apolloconfig/apollo/pull/11",
                pr_number=11,
            )
        ]
        meta = release_notes_builder.PullRequestMeta(
            title="feat: support incremental sync",
            author_login="alice",
        )
        with mock.patch.object(release_notes_builder, "_fetch_pr_metadata", return_value=meta):
            lines = release_notes_builder.format_change_lines(entries, repo="apolloconfig/apollo")

        self.assertEqual(
            lines,
            ["* Feature: Support incremental sync by @alice in https://github.com/apolloconfig/apollo/pull/11"],
        )

    def test_format_change_lines_issue_link_without_author(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Bugfix: Prevent accidental cache deletion https://github.com/apolloconfig/apollo/issues/5502",
                summary="Bugfix: Prevent accidental cache deletion",
                pr_url="https://github.com/apolloconfig/apollo/issues/5502",
                pr_number=None,
            )
        ]
        lines = release_notes_builder.format_change_lines(entries, repo="apolloconfig/apollo")
        self.assertEqual(
            lines,
            ["* Bugfix: Prevent accidental cache deletion in https://github.com/apolloconfig/apollo/issues/5502"],
        )

    def test_build_highlights_uses_pr_usage_hint(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Feature: Support importing configurations",
                summary="Feature: Support importing configurations",
                pr_url="https://github.com/apolloconfig/apollo/pull/88",
                pr_number=88,
            )
        ]
        context = release_notes_builder.PullRequestContext(
            title="Support importing configurations",
            body=(
                "## How to use\n"
                "Call POST /openapi/v1/envs/{env}/apps/{appId}/clusters/{clusterName}/namespaces/{namespaceName}/items:import "
                "to import namespace items in batch.\n"
            ),
            comments=[],
            files=["docs/en/portal/apollo-open-api-platform.md"],
            doc_lines=[],
        )

        with mock.patch.object(release_notes_builder, "_fetch_pr_context", return_value=context):
            highlights = release_notes_builder.build_highlights(
                entries,
                "2.5.0",
                highlight_pr_numbers=[88],
                repo="apolloconfig/apollo",
            )

        self.assertEqual(len(highlights), 1)
        self.assertIn("POST /openapi/v1/envs", highlights[0].body)

    def test_build_highlights_rejects_missing_pr(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Feature: Support importing configurations",
                summary="Feature: Support importing configurations",
                pr_url="https://github.com/apolloconfig/apollo/pull/88",
                pr_number=88,
            )
        ]
        with self.assertRaises(ValueError):
            release_notes_builder.build_highlights(
                entries,
                "2.5.0",
                highlight_pr_numbers=[99],
                repo="apolloconfig/apollo",
            )

    def test_strip_auto_generated_content(self) -> None:
        text = (
            "Useful content line\n"
            "<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\n"
            "## Summary by CodeRabbit\n"
            "Noisy line\n"
            "<!-- end of auto-generated comment: release notes by coderabbit.ai -->\n"
            "Another useful line\n"
        )
        cleaned = release_notes_builder._strip_auto_generated_content(text)
        self.assertIn("Useful content line", cleaned)
        self.assertIn("Another useful line", cleaned)
        self.assertNotIn("CodeRabbit", cleaned)

    def test_extract_usage_lines_from_code_blocks(self) -> None:
        body = (
            "## Brief changelog\n"
            "```java\n"
            "client.getOrganization().forEach(System.out::println);\n"
            "```\n"
            "```bash\n"
            "curl 127.0.0.1:8080/prometheus | grep -E 'instance_cache|instance_config_cache'\n"
            "```\n"
        )
        lines = release_notes_builder._extract_usage_lines_from_code_blocks(body)
        merged = "\n".join(lines)
        self.assertIn("client.getOrganization()", merged)
        self.assertIn("curl 127.0.0.1:8080/prometheus", merged)

    def test_build_highlight_body_normalizes_usage_sentence(self) -> None:
        body = release_notes_builder._build_highlight_body(
            summary="Provide a new configfiles API",
            release_version="2.5.0",
            usage_hint=(
                "In some application scenarios, if Apollo can provide an API to directly return the raw content of "
                "configuration files, users can reference a ConfigService URL directly without the need for an additional proxy URL."
            ),
        )
        self.assertIn("users can reference a configservice url directly", body.lower())
        self.assertNotIn("Usage:", body)

    def test_build_upgrade_section_no_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src/delta").mkdir(parents=True)
            (root / "profiles/delta").mkdir(parents=True)

            section = release_notes_builder.build_upgrade_section(
                repo="apolloconfig/apollo",
                release_version="2.5.0",
                previous_tag="v2.4.0",
                delta_src_root=root / "src/delta",
                profiles_delta_root=Path("scripts/sql/profiles/mysql-default/delta"),
            )

        self.assertIn("There is no schema change between v2.4.0 and v2.5.0", section)
        self.assertIn("apollo-configservice", section)

    def test_build_upgrade_section_with_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "src/delta/v240-v250"
            folder.mkdir(parents=True)
            (folder / "apolloconfigdb-v240-v250.sql").write_text("-- sql", encoding="utf-8")
            (folder / "apolloportaldb-v240-v250.sql").write_text("-- sql", encoding="utf-8")

            section = release_notes_builder.build_upgrade_section(
                repo="apolloconfig/apollo",
                release_version="2.5.0",
                previous_tag="v2.4.0",
                delta_src_root=root / "src/delta",
                profiles_delta_root=Path("scripts/sql/profiles/mysql-default/delta"),
            )

        self.assertIn("How to upgrade from v2.4.0 to v2.5.0", section)
        self.assertIn("apolloconfigdb-v240-v250.sql", section)
        self.assertIn("apolloportaldb-v240-v250.sql", section)
        self.assertIn("scripts/sql/profiles/mysql-default/delta/v240-v250", section)


class ReleaseFlowHelpersTest(unittest.TestCase):
    def test_checkpoint_list_contains_required_items(self) -> None:
        self.assertIn("TRIGGER_PACKAGE_WORKFLOW", release_flow.CHECKPOINTS)
        self.assertIn("MANAGE_MILESTONES", release_flow.CHECKPOINTS)

    def test_release_flow_requires_highlight_prs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--next-snapshot",
                    "2.6.0-SNAPSHOT",
                    "--state-file",
                    "state.json",
                    "--dry-run",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=Path(tmp)):
                with self.assertRaises(release_flow.ReleaseFlowError):
                    ReleaseFlow(args)

    def test_normalize_github_slug(self) -> None:
        cases = {
            "https://github.com/apolloconfig/apollo.git": "apolloconfig/apollo",
            "https://github.com/apolloconfig/apollo": "apolloconfig/apollo",
            "git@github.com:apolloconfig/apollo.git": "apolloconfig/apollo",
            "ssh://git@github.com/apolloconfig/apollo.git": "apolloconfig/apollo",
        }
        for raw, expected in cases.items():
            self.assertEqual(ReleaseFlow._normalize_github_slug(raw), expected)

    def test_expected_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = release_flow.parse_args(
                [
                    "run",
                    "--release-version",
                    "2.5.0",
                    "--next-snapshot",
                    "2.6.0-SNAPSHOT",
                    "--highlight-prs",
                    "5336,5361,5365",
                    "--state-file",
                    "state.json",
                    "--dry-run",
                ]
            )
            with mock.patch("pathlib.Path.cwd", return_value=Path(tmp)):
                flow = ReleaseFlow(args)

            assets = flow._expected_release_assets()
            self.assertEqual(
                assets,
                sorted(
                    [
                        "apollo-adminservice-2.5.0-github.zip",
                        "apollo-adminservice-2.5.0-github.zip.sha1",
                        "apollo-configservice-2.5.0-github.zip",
                        "apollo-configservice-2.5.0-github.zip.sha1",
                        "apollo-portal-2.5.0-github.zip",
                        "apollo-portal-2.5.0-github.zip.sha1",
                    ]
                ),
            )

    def test_render_announcement_from_release_notes(self) -> None:
        release_notes = """## Highlights

### Key Update
Some key point.

## What's Changed
* [Feature A](https://github.com/apolloconfig/apollo/pull/1)
* [Fix B](https://github.com/apolloconfig/apollo/pull/2)

**Full Changelog**: https://github.com/apolloconfig/apollo/compare/v2.4.0...v2.5.0
"""
        body = ReleaseFlow._render_announcement_from_release_notes(
            "2.5.0",
            release_notes,
            release_url="https://github.com/apolloconfig/apollo/releases/tag/v2.5.0",
        )
        self.assertIn("- [Feature A](https://github.com/apolloconfig/apollo/pull/1)", body)
        self.assertIn("https://github.com/apolloconfig/apollo/releases/tag/v2.5.0", body)


if __name__ == "__main__":
    unittest.main()
