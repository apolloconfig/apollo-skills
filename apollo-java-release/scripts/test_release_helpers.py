#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import release_notes_builder
import release_flow
import workflow_log_validator
from release_flow import ReleaseFlow


class ReleaseNotesBuilderTest(unittest.TestCase):
    def test_parse_semver(self) -> None:
        self.assertEqual(release_notes_builder.parse_semver("2.5.0"), (2, 5, 0))
        with self.assertRaises(ValueError):
            release_notes_builder.parse_semver("2.5")

    def test_parse_changes_section(self) -> None:
        content = """Changes by Version
==================
Release Notes.

Apollo Java 2.5.0

------------------

* [Feature A](https://example.com/pull/1)
* [Feature B](https://example.com/pull/2)

------------------
All issues and pull requests are [here](https://github.com/apolloconfig/apollo-java/milestone/5?closed=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGES.md"
            path.write_text(content, encoding="utf-8")
            bullets, milestone = release_notes_builder.parse_changes_section(path, "2.5.0")

        self.assertEqual(len(bullets), 2)
        self.assertIn("milestone/5", milestone)

    def test_extract_new_contributors_only(self) -> None:
        body = """## What's Changed
* one

## New Contributors
* @foo made first contribution
* @bar made first contribution

**Full Changelog**: https://example.com/compare
"""
        contributors = release_notes_builder.extract_section_lines(body, "New Contributors")
        self.assertEqual(
            contributors,
            [
                "* @foo made first contribution",
                "* @bar made first contribution",
            ],
        )

    def test_format_change_lines_with_author_mentions(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
                pr_title="Support Spring Boot 4.0 bootstrap context package relocation",
                author_login="app/copilot-swe-agent",
            )
        ]

        lines = release_notes_builder.format_change_lines(entries)
        self.assertEqual(
            lines,
            [
                "* Support Spring Boot 4.0 bootstrap context package relocation "
                "by @Copilot in https://github.com/apolloconfig/apollo-java/pull/115"
            ],
        )

    def test_build_highlights_selects_multiple_items(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="feat: provide organization list",
                summary="feat: provide organization list",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/102",
                pr_number=102,
                pr_title="feat: provide organization list",
                author_login="foo",
            ),
            release_notes_builder.ChangeEntry(
                raw_text="Support Spring Boot 4.0 bootstrap context package relocation",
                summary="Support Spring Boot 4.0 bootstrap context package relocation",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/115",
                pr_number=115,
                pr_title="Support Spring Boot 4.0 bootstrap context package relocation",
                author_login="app/copilot-swe-agent",
            ),
            release_notes_builder.ChangeEntry(
                raw_text="fix: deduplicate config listeners by identity",
                summary="fix: deduplicate config listeners by identity",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/121",
                pr_number=121,
                pr_title="fix: deduplicate config listeners by identity",
                author_login="nobodyiam",
            ),
            release_notes_builder.ChangeEntry(
                raw_text="test: overhaul automated compatibility coverage",
                summary="test: overhaul automated compatibility coverage",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/123",
                pr_number=123,
                pr_title="test: overhaul automated compatibility coverage",
                author_login="nobodyiam",
            ),
        ]

        highlights = release_notes_builder.build_highlights(entries, "2.5.0", max_items=3)
        self.assertEqual(len(highlights), 3)
        joined_titles = " ".join(item.title for item in highlights).lower()
        self.assertIn("spring boot 4.0", joined_titles)
        self.assertIn("organization list", joined_titles)
        self.assertIn("deduplicate config listeners", joined_titles)
        self.assertNotIn("automated compatibility coverage", joined_titles)

    def test_build_highlights_not_tied_to_spring_boot(self) -> None:
        entries = [
            release_notes_builder.ChangeEntry(
                raw_text="feat: support incremental sync",
                summary="feat: support incremental sync",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/90",
                pr_number=90,
                pr_title="feat: support incremental sync",
                author_login="jackie-coming",
            ),
            release_notes_builder.ChangeEntry(
                raw_text="feat: add ConfigMap cache support",
                summary="feat: add ConfigMap cache support",
                pr_url="https://github.com/apolloconfig/apollo-java/pull/79",
                pr_number=79,
                pr_title="feat: add ConfigMap cache support",
                author_login="dyx1234",
            ),
        ]

        highlights = release_notes_builder.build_highlights(entries, "2.4.0", max_items=2)
        self.assertEqual(len(highlights), 2)
        self.assertIn("incremental sync", highlights[0].title.lower())
        self.assertIn("configmap cache", highlights[1].title.lower())

    def test_build_release_content_uses_author_mentions(self) -> None:
        content = """Changes by Version
==================
Release Notes.

Apollo Java 2.5.0

------------------

* [Support Spring Boot 4.0 bootstrap context package relocation](https://github.com/apolloconfig/apollo-java/pull/115)

------------------
All issues and pull requests are [here](https://github.com/apolloconfig/apollo-java/milestone/5?closed=1)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGES.md"
            path.write_text(content, encoding="utf-8")

            with mock.patch.object(
                release_notes_builder,
                "_fetch_pr_metadata",
                return_value={
                    "title": "Support Spring Boot 4.0 bootstrap context package relocation",
                    "url": "https://github.com/apolloconfig/apollo-java/pull/115",
                    "author_login": "app/copilot-swe-agent",
                },
            ), mock.patch.object(
                release_notes_builder,
                "infer_previous_tag",
                return_value="v2.4.0",
            ), mock.patch.object(
                release_notes_builder,
                "generate_notes_from_github",
                return_value={"name": "v2.5.0", "body": ""},
            ):
                result = release_notes_builder.build_release_content(
                    repo="apolloconfig/apollo-java",
                    release_version="2.5.0",
                    changes_file=path,
                    target_commitish="main",
                )

        notes = result["release_notes"]
        self.assertIn("### Support Spring Boot 4.0 bootstrap context package relocation", notes)
        self.assertIn("Apollo Java client now supports Spring Boot 4.0", notes)
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


if __name__ == "__main__":
    unittest.main()
