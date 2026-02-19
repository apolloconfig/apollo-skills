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
import workflow_log_validator
from release_flow import ReleaseFlow


class ReleaseNotesBuilderTest(unittest.TestCase):
    def test_parse_semver(self) -> None:
        self.assertEqual(release_notes_builder.parse_semver("2.5.0"), (2, 5, 0))
        with self.assertRaises(ValueError):
            release_notes_builder.parse_semver("2.5")

    def test_parse_highlight_pr_numbers(self) -> None:
        self.assertEqual(
            release_notes_builder.parse_highlight_pr_numbers("115, 121 123,115"),
            [115, 121, 123],
        )
        with self.assertRaises(ValueError):
            release_notes_builder.parse_highlight_pr_numbers("115,abc")

    def test_parse_changes_section(self) -> None:
        content = """Changes by Version
==================
Release Notes.

Apollo Java 2.5.0

------------------
* [Feature A](https://github.com/apolloconfig/apollo-java/pull/1)
* [Feature B](https://github.com/apolloconfig/apollo-java/pull/2)

------------------
All issues and pull requests are [here](https://github.com/apolloconfig/apollo-java/milestone/5?closed=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGES.md"
            path.write_text(content, encoding="utf-8")
            bullets, milestone = release_notes_builder.parse_changes_section(path, "2.5.0")

        self.assertEqual(len(bullets), 2)
        self.assertIn("milestone/5", milestone)

    def test_build_highlights_uses_selected_prs(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Feature: support incremental sync",
                summary="Feature: support incremental sync",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/90",
                pr_number=90,
            ),
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
            ),
            release_notes_builder.ChangeEntry(
                raw_text="fix: deduplicate config listeners by identity",
                summary="fix: deduplicate config listeners by identity",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/121",
                pr_number=121,
            ),
        ]

        highlights = release_notes_builder.build_highlights(
            entries,
            "2.5.0",
            highlight_pr_numbers=[115, 121],
        )
        self.assertEqual(len(highlights), 2)
        self.assertIn("spring boot 4.0", highlights[0].title.lower())
        self.assertIn("deduplicate", highlights[1].title.lower())

    def test_build_highlights_rejects_missing_pr(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
            )
        ]
        with self.assertRaises(ValueError):
            release_notes_builder.build_highlights(
                entries,
                "2.5.0",
                highlight_pr_numbers=[115, 121],
            )

    def test_format_change_lines_with_author_mentions(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
            )
        ]
        meta = release_notes_builder.PullRequestMeta(
            title="Support Spring Boot 4.0 bootstrap context package relocation",
            author_login="app/copilot-swe-agent",
        )
        with mock.patch.object(release_notes_builder, "_fetch_pr_metadata", return_value=meta):
            lines = release_notes_builder.format_change_lines(entries, repo="apolloconfig/apollo-java")

        self.assertEqual(
            lines,
            [
                "* Support Spring Boot 4.0 bootstrap context package relocation "
                "by @Copilot in https://github.com/apolloconfig/apollo-java/pull/115"
            ],
        )

    def test_build_highlights_uses_pr_usage_hint(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
            )
        ]
        context = release_notes_builder.PullRequestContext(
            title="Support Spring Boot 4.0 bootstrap context package relocation",
            body=(
                "## How to use\n"
                "Call GET /configfiles/json/{appId}/{clusterName}/{namespaceName} "
                "to verify config file retrieval in integration tests.\n"
            ),
            comments=[],
            files=["docs/en/client/java-sdk-user-guide.md"],
            doc_lines=[],
        )

        with mock.patch.object(release_notes_builder, "_fetch_pr_context", return_value=context):
            highlights = release_notes_builder.build_highlights(
                entries,
                "2.5.0",
                highlight_pr_numbers=[115],
                repo="apolloconfig/apollo-java",
            )

        self.assertEqual(len(highlights), 1)
        self.assertIn("GET /configfiles/json", highlights[0].body)

    def test_build_release_content_uses_selected_highlights_and_authors(self) -> None:
        content = """Changes by Version
==================
Release Notes.

Apollo Java 2.5.0

------------------
* [Support Spring Boot 4.0 bootstrap context package relocation](https://github.com/apolloconfig/apollo-java/pull/115)
* [Fix change listener de-duplication by identity](https://github.com/apolloconfig/apollo-java/pull/121)

------------------
All issues and pull requests are [here](https://github.com/apolloconfig/apollo-java/milestone/5?closed=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGES.md"
            path.write_text(content, encoding="utf-8")

            with mock.patch.object(
                release_notes_builder,
                "infer_previous_tag",
                return_value="v2.4.0",
            ), mock.patch.object(
                release_notes_builder,
                "generate_notes_from_github",
                return_value={"name": "v2.5.0", "body": ""},
            ), mock.patch.object(
                release_notes_builder,
                "_fetch_pr_metadata",
                side_effect=[
                    release_notes_builder.PullRequestMeta(
                        title="Support Spring Boot 4.0 bootstrap context package relocation",
                        author_login="app/copilot-swe-agent",
                    ),
                    release_notes_builder.PullRequestMeta(
                        title="fix: deduplicate config listeners by identity",
                        author_login="nobodyiam",
                    ),
                    release_notes_builder.PullRequestMeta(
                        title="Support Spring Boot 4.0 bootstrap context package relocation",
                        author_login="app/copilot-swe-agent",
                    ),
                ],
            ), mock.patch.object(
                release_notes_builder,
                "_fetch_pr_context",
                return_value=None,
            ):
                result = release_notes_builder.build_release_content(
                    repo="apolloconfig/apollo-java",
                    release_version="2.5.0",
                    changes_file=path,
                    target_commitish="main",
                    highlight_pr_numbers=[115],
                )

        notes = result["release_notes"]
        self.assertIn("### Support Spring Boot 4.0 bootstrap context package relocation", notes)
        self.assertIn("by @Copilot in https://github.com/apolloconfig/apollo-java/pull/115", notes)


class WorkflowLogValidatorTest(unittest.TestCase):
    def test_parse_uploaded_urls(self) -> None:
        log = """
Uploaded to releases: https://central.sonatype.com/repository/releases/com/ctrip/framework/apollo/apollo-core/2.5.0/apollo-core-2.5.0.jar
Uploaded to releases: https://central.sonatype.com/repository/releases/com/ctrip/framework/apollo/apollo-core/2.5.0/apollo-core-2.5.0.pom
"""
        urls = workflow_log_validator.parse_uploaded_urls(log, "releases")
        self.assertEqual(len(urls), 2)


class ReleaseFlowHelpersTest(unittest.TestCase):
    def test_normalize_github_slug(self) -> None:
        cases = {
            "https://github.com/apolloconfig/apollo-java.git": "apolloconfig/apollo-java",
            "https://github.com/apolloconfig/apollo-java": "apolloconfig/apollo-java",
            "git@github.com:apolloconfig/apollo-java.git": "apolloconfig/apollo-java",
            "ssh://git@github.com/apolloconfig/apollo-java.git": "apolloconfig/apollo-java",
        }
        for raw, expected in cases.items():
            self.assertEqual(ReleaseFlow._normalize_github_slug(raw), expected)

    def test_checkpoint_list_without_sonatype_publish(self) -> None:
        self.assertNotIn("TRIGGER_SONATYPE_PUBLISH", release_flow.CHECKPOINTS)

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

    def test_render_announcement_from_release_notes_keeps_whats_changed_format(self) -> None:
        release_notes = """## Highlights

### Key Update
Some key point.

## What's Changed
* feat: one change by @foo in https://github.com/apolloconfig/apollo-java/pull/1
* fix: another change by @bar in https://github.com/apolloconfig/apollo-java/pull/2

## New Contributors
* @foo made their first contribution in https://github.com/apolloconfig/apollo-java/pull/1

**Full Changelog**: https://github.com/apolloconfig/apollo-java/compare/v2.4.0...v2.5.0
"""
        body = ReleaseFlow._render_announcement_from_release_notes("2.5.0", release_notes)
        self.assertIn(
            "* feat: one change by @foo in https://github.com/apolloconfig/apollo-java/pull/1",
            body,
        )
        self.assertIn(
            "* fix: another change by @bar in https://github.com/apolloconfig/apollo-java/pull/2",
            body,
        )
        self.assertIn(
            "Full changelog: https://github.com/apolloconfig/apollo-java/compare/v2.4.0...v2.5.0",
            body,
        )

    def test_render_announcement_from_release_notes_fallback_uses_star_bullet(self) -> None:
        release_notes = """## Highlights

### Key Update
Some key point.
"""
        body = ReleaseFlow._render_announcement_from_release_notes("2.5.0", release_notes)
        self.assertIn("* No user-facing changes were listed in release notes.", body)


if __name__ == "__main__":
    unittest.main()
