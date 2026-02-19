#!/usr/bin/env python3
"""Build Apollo Java release notes and announcement drafts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class ChangeEntry:
    raw_text: str
    summary: str
    pr_url: Optional[str]
    pr_number: Optional[int]
    pr_title: Optional[str] = None
    author_login: Optional[str] = None

    @property
    def display_summary(self) -> str:
        return self.pr_title or self.summary


@dataclass
class HighlightItem:
    title: str
    body: str


def run_json_command(cmd: list[str]) -> object:
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def parse_semver(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError(f"Invalid semantic version: {value}")
    return tuple(int(part) for part in match.groups())


def infer_previous_tag(repo: str, release_version: str) -> Optional[str]:
    release_tuple = parse_semver(release_version)
    payload = run_json_command(["gh", "api", f"repos/{repo}/releases?per_page=100"])
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for item in payload:
        tag = item.get("tag_name", "")
        if not isinstance(tag, str) or not tag.startswith("v"):
            continue
        raw_version = tag[1:]
        if not re.fullmatch(r"\d+\.\d+\.\d+", raw_version):
            continue
        version_tuple = parse_semver(raw_version)
        if version_tuple < release_tuple:
            candidates.append((version_tuple, tag))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _parse_release_changes_block(
    changes_file: Path, release_version: str
) -> tuple[list[str], Optional[str]]:
    lines = changes_file.read_text(encoding="utf-8").splitlines()
    header = f"Apollo Java {release_version}"
    start_index = -1
    for index, line in enumerate(lines):
        if line.strip() == header:
            start_index = index
            break
    if start_index < 0:
        raise ValueError(f"Failed to locate section '{header}' in {changes_file}")

    separator_pattern = re.compile(r"^-{3,}\s*$")
    index = start_index + 1
    while index < len(lines) and not separator_pattern.match(lines[index].strip()):
        index += 1
    if index >= len(lines):
        raise ValueError(f"Failed to locate first separator after '{header}'")

    index += 1
    bullets: list[str] = []
    while index < len(lines):
        stripped = lines[index].strip()
        if separator_pattern.match(stripped):
            break
        if stripped.startswith("*"):
            content = stripped.lstrip("*").strip()
            if content:
                bullets.append(content)
        index += 1

    milestone_line: Optional[str] = None
    for tail_index in range(index + 1, min(index + 12, len(lines))):
        line = lines[tail_index].strip()
        if "milestone/" in line:
            milestone_line = line
            break

    return bullets, milestone_line


def parse_changes_section(changes_file: Path, release_version: str) -> tuple[list[str], Optional[str]]:
    bullets, milestone_line = _parse_release_changes_block(changes_file, release_version)
    return [f"* {item}" for item in bullets], milestone_line


def parse_change_entries(changes_file: Path, release_version: str) -> tuple[list[ChangeEntry], Optional[str]]:
    bullets, milestone_line = _parse_release_changes_block(changes_file, release_version)
    return [_parse_change_entry(item) for item in bullets], milestone_line


def _parse_change_entry(raw_text: str) -> ChangeEntry:
    link_match = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)]+)\)", raw_text.strip())
    if link_match:
        summary = link_match.group(1).strip()
        pr_url = link_match.group(2).strip()
    else:
        url_match = re.search(r"(https?://\S+)", raw_text)
        pr_url = url_match.group(1).strip() if url_match else None
        summary = raw_text.replace(pr_url, "").strip(" -") if pr_url else raw_text.strip()

    return ChangeEntry(
        raw_text=raw_text.strip(),
        summary=summary.strip(),
        pr_url=pr_url,
        pr_number=_extract_pr_number(pr_url),
    )


def _extract_pr_number(pr_url: Optional[str]) -> Optional[int]:
    if not pr_url:
        return None
    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        return None
    return int(match.group(1))


def _sanitize_pr_title(title: str) -> str:
    cleaned = title.strip()
    cleaned = re.sub(r"\(#\d+\)$", "", cleaned).strip()
    cleaned = re.sub(r"^\[issue[-_\s]?\d+\]:?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _format_author_mention(login: Optional[str]) -> Optional[str]:
    if not login:
        return None
    if login == "app/copilot-swe-agent":
        return "@Copilot"
    if login.startswith("app/"):
        return f"@{login.split('/', 1)[1]}"
    return f"@{login}"


def _fetch_pr_metadata(repo: str, pr_number: int) -> Optional[dict[str, str]]:
    try:
        payload = run_json_command(
            ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "title,url,author"]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    author = payload.get("author", {})
    author_login = author.get("login") if isinstance(author, dict) else None
    title = payload.get("title")
    url = payload.get("url")
    metadata: dict[str, str] = {}
    if isinstance(title, str) and title.strip():
        metadata["title"] = _sanitize_pr_title(title)
    if isinstance(url, str) and url.strip():
        metadata["url"] = url.strip()
    if isinstance(author_login, str) and author_login.strip():
        metadata["author_login"] = author_login.strip()
    return metadata


def enrich_change_entries(repo: str, entries: list[ChangeEntry]) -> list[ChangeEntry]:
    enriched: list[ChangeEntry] = []
    for entry in entries:
        updated = ChangeEntry(
            raw_text=entry.raw_text,
            summary=entry.summary,
            pr_url=entry.pr_url,
            pr_number=entry.pr_number,
            pr_title=entry.pr_title,
            author_login=entry.author_login,
        )
        if updated.pr_number is not None:
            metadata = _fetch_pr_metadata(repo, updated.pr_number)
            if metadata:
                updated.pr_title = metadata.get("title") or updated.pr_title
                updated.pr_url = metadata.get("url") or updated.pr_url
                updated.author_login = metadata.get("author_login") or updated.author_login
        enriched.append(updated)
    return enriched


def format_change_lines(entries: Iterable[ChangeEntry]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        summary = entry.display_summary.strip() or entry.raw_text.strip()
        author_mention = _format_author_mention(entry.author_login)

        if entry.pr_url:
            if author_mention:
                lines.append(f"* {summary} by {author_mention} in {entry.pr_url}")
            else:
                lines.append(f"* {summary} in {entry.pr_url}")
            continue

        if author_mention:
            lines.append(f"* {summary} by {author_mention}")
            continue

        lines.append(f"* {summary}")

    return lines


def _clean_summary_for_highlight(summary: str) -> str:
    cleaned = summary.strip()
    cleaned = re.sub(r"^(feat|fix|perf|refactor|docs|test|chore|build|ci)(\([^)]+\))?:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", cleaned).strip()
    cleaned = cleaned.rstrip(".").strip()
    return cleaned


def _score_highlight_candidate(entry: ChangeEntry) -> int:
    original = (entry.display_summary or entry.summary or entry.raw_text).strip().lower()
    text = _clean_summary_for_highlight(entry.display_summary or entry.summary or entry.raw_text).lower()
    if not text:
        return -1

    score = 1
    if re.match(r"^(test|chore|ci|docs|build)(\([^)]+\))?:", original):
        score -= 4
    if any(
        token in text
        for token in [
            "feature",
            "feat",
            "support",
            "add ",
            "added ",
            "provide",
            "new ",
            "enhance",
            "improve",
        ]
    ):
        score += 2
    if any(token in text for token in ["support", "compatible", "compatibility"]):
        score += 1
    if re.search(r"\b\d+\.\d+\b", text):
        score += 1
    if "fix" in text:
        score += 1
    return score


def _ensure_sentence_end(text: str) -> str:
    if text.endswith((".", "!", "?")):
        return text
    return f"{text}."


def _build_highlight_title(summary: str) -> str:
    if not summary:
        return "Release Update"
    if len(summary) <= 72:
        return summary
    return f"{summary[:69].rstrip()}..."


def _build_highlight_body(summary: str, release_version: str) -> str:
    if not summary:
        return f"Apollo Java {release_version} is now available."

    lower = summary.lower()
    if lower.startswith("support "):
        return _ensure_sentence_end(f"Apollo Java client now supports {summary[8:].strip()}")
    if lower.startswith("add "):
        return _ensure_sentence_end(f"Apollo Java client now adds {summary[4:].strip()}")
    if lower.startswith("added "):
        return _ensure_sentence_end(f"Apollo Java client now adds {summary[6:].strip()}")
    if lower.startswith("provide "):
        return _ensure_sentence_end(f"Apollo Java client now provides {summary[8:].strip()}")
    if lower.startswith("fix "):
        return _ensure_sentence_end(f"Apollo Java client now fixes {summary[4:].strip()}")
    if lower.startswith("improve "):
        return _ensure_sentence_end(f"Apollo Java client now improves {summary[8:].strip()}")
    if lower.startswith("enhance "):
        return _ensure_sentence_end(f"Apollo Java client now enhances {summary[8:].strip()}")
    return _ensure_sentence_end(f"Apollo Java client now includes: {summary}")


def build_highlights(
    entries: list[ChangeEntry],
    release_version: str,
    max_items: int = 3,
) -> list[HighlightItem]:
    if max_items <= 0:
        return []
    if not entries:
        return [HighlightItem(title="Release Update", body=f"Apollo Java {release_version} is now available.")]

    ranked = sorted(
        list(enumerate(entries)),
        key=lambda item: (-_score_highlight_candidate(item[1]), item[0]),
    )

    selected: list[ChangeEntry] = []
    seen: set[str] = set()
    for _, entry in ranked:
        summary = _clean_summary_for_highlight(entry.display_summary or entry.summary or entry.raw_text)
        normalized = re.sub(r"\s+", " ", summary.lower()).strip()
        if not normalized or normalized in seen:
            continue

        score = _score_highlight_candidate(entry)
        if score <= 0 and selected:
            continue

        selected.append(entry)
        seen.add(normalized)
        if len(selected) >= max_items:
            break

    if not selected:
        selected = [entries[0]]

    highlights: list[HighlightItem] = []
    for entry in selected:
        summary = _clean_summary_for_highlight(entry.display_summary or entry.summary or entry.raw_text)
        highlights.append(
            HighlightItem(
                title=_build_highlight_title(summary),
                body=_build_highlight_body(summary, release_version),
            )
        )
    return highlights


def extract_section_lines(markdown: str, section_title: str) -> list[str]:
    section_pattern = re.compile(
        rf"^## {re.escape(section_title)}[ \t]*\r?$\n?([\s\S]*?)(?=^## |\Z)",
        re.MULTILINE,
    )
    match = section_pattern.search(markdown)
    if not match:
        return []
    section = match.group(1)
    lines: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if re.match(r"^\*\s+", stripped):
            lines.append(stripped)
    return lines


def extract_full_changelog(markdown: str) -> Optional[str]:
    match = re.search(r"\*\*Full Changelog\*\*:\s*(\S+)", markdown)
    if not match:
        return None
    return match.group(1)


def generate_notes_from_github(
    repo: str,
    release_version: str,
    target_commitish: str,
    previous_tag_name: Optional[str],
) -> dict[str, str]:
    cmd = [
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{repo}/releases/generate-notes",
        "-f",
        f"tag_name=v{release_version}",
        "-f",
        f"target_commitish={target_commitish}",
    ]
    if previous_tag_name:
        cmd.extend(["-f", f"previous_tag_name={previous_tag_name}"])
    payload = run_json_command(cmd)
    return {
        "name": payload.get("name", f"v{release_version}"),
        "body": payload.get("body", ""),
    }


def build_release_markdown(
    release_version: str,
    highlights: Iterable[HighlightItem],
    change_lines: Iterable[str],
    new_contributors: Iterable[str],
    full_changelog_url: Optional[str],
) -> str:
    lines: list[str] = ["## Highlights", ""]

    highlight_list = list(highlights)
    if highlight_list:
        for item in highlight_list:
            lines.extend([f"### {item.title}", item.body, ""])
    else:
        lines.extend([f"### Release Update", f"Apollo Java {release_version} is now available.", ""])

    lines.append("## What's Changed")

    change_list = list(change_lines)
    if change_list:
        lines.extend(change_list)
    else:
        lines.append(f"* Apollo Java {release_version} is now available.")

    contributors = list(new_contributors)
    if contributors:
        lines.extend(["", "## New Contributors"])
        lines.extend(contributors)

    if full_changelog_url:
        lines.extend(["", f"**Full Changelog**: {full_changelog_url}"])

    return "\n".join(lines).rstrip() + "\n"


def build_announcement_markdown(
    release_version: str,
    change_lines: Iterable[str],
    new_contributors: Iterable[str],
    full_changelog_url: Optional[str],
) -> str:
    lines: list[str] = [
        "Hi all,",
        "",
        f"Apollo Team is glad to announce the release of Apollo Java {release_version}.",
        "",
        "This release includes the following changes.",
        "",
    ]

    change_list = list(change_lines)
    if change_list:
        lines.extend(change_list)
    else:
        lines.append("- No user-facing changes were listed in CHANGES.md.")

    contributor_list = list(new_contributors)
    if contributor_list:
        lines.extend(["", "New contributors in this release:"])
        lines.extend(contributor_list)

    if full_changelog_url:
        lines.extend(["", f"Full changelog: {full_changelog_url}"])

    lines.extend(
        [
            "",
            "Apollo website: https://www.apolloconfig.com/",
            "",
            "Maven Artifacts: https://mvnrepository.com/artifact/com.ctrip.framework.apollo",
            "",
            "Apollo Resources:",
            "* GitHub: https://github.com/apolloconfig/apollo-java",
            "* Issue: https://github.com/apolloconfig/apollo-java/issues",
            "* Mailing list: [apollo-config@googlegroups.com](mailto:apollo-config@googlegroups.com)",
            "",
            "Apollo Team",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def build_release_content(
    repo: str,
    release_version: str,
    changes_file: Path,
    target_commitish: str,
    previous_tag_name: Optional[str] = None,
) -> dict[str, object]:
    entries, milestone_line = parse_change_entries(changes_file, release_version)
    entries = enrich_change_entries(repo, entries)
    highlights = build_highlights(entries, release_version, max_items=3)
    change_lines = format_change_lines(entries)
    resolved_previous_tag = previous_tag_name or infer_previous_tag(repo, release_version)
    generated_notes = generate_notes_from_github(
        repo=repo,
        release_version=release_version,
        target_commitish=target_commitish,
        previous_tag_name=resolved_previous_tag,
    )
    generated_body = generated_notes.get("body", "")
    new_contributors = extract_section_lines(generated_body, "New Contributors")
    full_changelog_url = extract_full_changelog(generated_body)

    release_markdown = build_release_markdown(
        release_version=release_version,
        highlights=highlights,
        change_lines=change_lines,
        new_contributors=new_contributors,
        full_changelog_url=full_changelog_url,
    )

    announcement_markdown = build_announcement_markdown(
        release_version=release_version,
        change_lines=change_lines,
        new_contributors=new_contributors,
        full_changelog_url=full_changelog_url,
    )

    return {
        "release_notes": release_markdown,
        "announcement": announcement_markdown,
        "milestone_line": milestone_line,
        "previous_tag": resolved_previous_tag,
        "full_changelog_url": full_changelog_url,
        "new_contributors": new_contributors,
        "highlights_count": len(highlights),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Apollo Java release notes from CHANGES.md")
    parser.add_argument("--repo", default="apolloconfig/apollo-java", help="GitHub repository")
    parser.add_argument("--release-version", required=True, help="Release version, e.g. 2.5.0")
    parser.add_argument("--changes-file", default="CHANGES.md", help="Path to CHANGES.md")
    parser.add_argument("--kind", choices=["release", "announcement"], required=True)
    parser.add_argument("--output", required=True, help="Output markdown file")
    parser.add_argument("--target-commitish", default="main", help="Release target branch/commit")
    parser.add_argument("--previous-tag", default=None, help="Optional previous tag override")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    content = build_release_content(
        repo=args.repo,
        release_version=args.release_version,
        changes_file=Path(args.changes_file),
        target_commitish=args.target_commitish,
        previous_tag_name=args.previous_tag,
    )

    output_text = content["release_notes"] if args.kind == "release" else content["announcement"]
    output_path = Path(args.output)
    output_path.write_text(output_text, encoding="utf-8")

    metadata = {
        "output": str(output_path),
        "kind": args.kind,
        "previous_tag": content["previous_tag"],
        "full_changelog_url": content["full_changelog_url"],
        "new_contributors_count": len(content["new_contributors"]),
    }
    print(json.dumps(metadata, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
