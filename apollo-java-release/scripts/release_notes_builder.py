#!/usr/bin/env python3
"""Build Apollo Java release notes and announcement drafts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class ChangeEntry:
    raw_text: str
    summary: str
    pr_url: Optional[str]
    pr_number: Optional[int]


@dataclass
class HighlightItem:
    title: str
    body: str


@dataclass
class PullRequestContext:
    title: Optional[str]
    body: str
    comments: list[str]
    files: list[str]
    doc_lines: list[str]


@dataclass
class PullRequestMeta:
    title: Optional[str]
    author_login: Optional[str]


USAGE_HEADING_HINTS = [
    "usage",
    "how to use",
    "examples",
    "example",
    "quick start",
]

USAGE_LINE_HINTS = [
    "usage",
    "example",
    "you can",
    "users can",
    "call ",
    "endpoint",
    "api",
    "openapi",
    "spring",
    "configuration",
    "set ",
    "enable",
]

NOISE_LINE_HINTS = [
    "fixes #",
    "checklist",
    "unit test",
    "mvn clean test",
    "spotless",
    "changelog",
    "copilot",
    "mcp server",
    "custom instructions",
    "coding agent tips",
]

BOT_LOGIN_HINTS = [
    "coderabbit",
    "github-actions",
    "dependabot",
    "copilot",
    "renovate",
]

AUTO_SUMMARY_HINTS = [
    "summary by coderabbit",
    "walkthrough",
    "suggested labels",
    "suggested reviewers",
]


def run_json_command(cmd: list[str]) -> object:
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def _sanitize_text_line(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
    cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", cleaned)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _truncate_text(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _is_bot_login(login: str) -> bool:
    lowered = login.lower().strip()
    if not lowered:
        return False
    if lowered.endswith("[bot]") or lowered.endswith("-bot"):
        return True
    return any(hint in lowered for hint in BOT_LOGIN_HINTS)


def _strip_auto_generated_content(markdown_text: str) -> str:
    text = re.sub(
        r"<!--\s*This is an auto-generated comment:[\s\S]*?end of auto-generated comment:[\s\S]*?-->",
        "",
        markdown_text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        lowered = raw_line.strip().lower()
        if any(hint in lowered for hint in AUTO_SUMMARY_HINTS):
            continue
        if lowered.startswith("<details") or lowered.startswith("</details"):
            continue
        if lowered.startswith("<summary") or lowered.startswith("</summary"):
            continue
        cleaned_lines.append(raw_line)
    return "\n".join(cleaned_lines)


def _extract_added_lines_from_patch(patch_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in patch_text.splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        line = raw_line[1:].strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("```") or line.startswith("!"):
            continue
        cleaned = _sanitize_text_line(line)
        if cleaned and len(cleaned) >= 24:
            lines.append(cleaned)
    return lines


def _fetch_doc_patch_lines(repo: str, pr_number: int) -> list[str]:
    try:
        payload = run_json_command(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}/files?per_page=100"]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []

    lines: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            continue
        is_doc_file = filename.startswith("docs/") or filename.lower().endswith(".md")
        if not is_doc_file:
            continue
        patch = item.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            continue
        lines.extend(_extract_added_lines_from_patch(patch))
    return lines


@lru_cache(maxsize=512)
def _fetch_pr_metadata(repo: str, pr_number: int) -> Optional[PullRequestMeta]:
    try:
        payload = run_json_command(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "title,author",
            ]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    author_login: Optional[str] = None
    author = payload.get("author")
    if isinstance(author, dict):
        login = author.get("login")
        if isinstance(login, str) and login.strip():
            author_login = login.strip()

    return PullRequestMeta(title=title, author_login=author_login)


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


def _fetch_pr_context(repo: str, pr_number: int) -> Optional[PullRequestContext]:
    try:
        payload = run_json_command(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "title,body,files,comments",
            ]
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    comments: list[str] = []
    for item in payload.get("comments", []):
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        login = ""
        if isinstance(author, dict):
            login = (author.get("login") or "").lower()
        if _is_bot_login(login):
            continue
        body = item.get("body")
        if isinstance(body, str) and body.strip():
            cleaned = _strip_auto_generated_content(body)
            if cleaned.strip():
                comments.append(cleaned.strip())

    files: list[str] = []
    for item in payload.get("files", []):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            files.append(path.strip())

    body = payload.get("body") if isinstance(payload.get("body"), str) else ""
    cleaned_body = _strip_auto_generated_content(body)
    doc_lines = _fetch_doc_patch_lines(repo, pr_number)

    return PullRequestContext(
        title=payload.get("title") if isinstance(payload.get("title"), str) else None,
        body=cleaned_body,
        comments=comments,
        files=files,
        doc_lines=doc_lines,
    )


def _extract_usage_lines_from_code_blocks(markdown_text: str) -> list[str]:
    lines: list[str] = []
    for match in re.finditer(r"```[A-Za-z0-9_-]*\s*\n([\s\S]*?)```", markdown_text):
        block = match.group(1)
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "curl " in line:
                lines.append(line)
            endpoint_match = re.search(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9._~!$&'()*+,;=:@%/\-{}]+)", line)
            if endpoint_match:
                lines.append(f"{endpoint_match.group(1)} {endpoint_match.group(2)}")
            client_call_match = re.search(r"\bclient\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if client_call_match:
                method = client_call_match.group(1)
                lines.append(f"OpenAPI Java client can call client.{method}()")
    cleaned_lines: list[str] = []
    seen: set[str] = set()
    for item in lines:
        cleaned = _sanitize_text_line(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned_lines.append(cleaned)
    return cleaned_lines


def _split_candidate_lines(markdown_text: str) -> list[str]:
    lines: list[str] = []
    in_code_block = False
    for raw_line in markdown_text.splitlines():
        if raw_line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        line = _sanitize_text_line(raw_line)
        if not line:
            continue
        if line.startswith("#"):
            continue
        if re.match(r"^- \[[ xX]\]", line):
            continue
        if line.startswith(">"):
            continue
        if line.startswith("!["):
            continue
        if line.startswith("<"):
            continue
        if len(line) < 24:
            continue
        lines.append(line)
    return lines


def _extract_usage_lines_from_sections(markdown_text: str) -> list[str]:
    lines = markdown_text.splitlines()
    section_lines: list[str] = []
    collecting = False
    for raw_line in lines:
        stripped = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            heading_text = heading.group(1).strip().lower()
            matched = any(token in heading_text for token in USAGE_HEADING_HINTS)
            if matched:
                collecting = True
                continue
            if collecting:
                break
        if collecting:
            section_lines.append(raw_line)
    return _split_candidate_lines("\n".join(section_lines))


def _score_usage_line(line: str) -> int:
    lowered = line.lower()
    score = 0
    for hint in USAGE_LINE_HINTS:
        if hint in lowered:
            score += 2
    if "users can" in lowered or "you can" in lowered:
        score += 3
    if "curl " in lowered:
        score += 4
    if "openapi java client can call" in lowered:
        score += 4
    if "http" in lowered or "/api" in lowered:
        score += 2
    if re.search(r"\b(get|post|put|delete)\b", lowered):
        score += 1
    for hint in NOISE_LINE_HINTS:
        if hint in lowered:
            score -= 2
    if len(line) > 220:
        score -= 1
    if " " not in line:
        score -= 4
    if re.fullmatch(r"[A-Za-z0-9_.]+", line):
        score -= 5
    if lowered.startswith("fixes #"):
        score -= 4
    if "copilot" in lowered or "mcp" in lowered:
        score -= 6
    return score


def _pick_best_usage_line(lines: list[str]) -> Optional[str]:
    if not lines:
        return None
    scored: list[tuple[int, str]] = []
    for line in lines:
        score = _score_usage_line(line)
        scored.append((score, line))
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    best_score, best_line = scored[0]
    if best_score <= 0:
        return None
    return _truncate_text(best_line, limit=200)


def _summarize_doc_files(files: list[str]) -> Optional[str]:
    doc_files = [path for path in files if path.startswith("docs/") or path.lower().endswith(".md")]
    if not doc_files:
        return None
    names: list[str] = []
    for path in doc_files:
        name = Path(path).name
        if name not in names:
            names.append(name)
        if len(names) >= 2:
            break
    top_paths = ", ".join(f"`{name}`" for name in names)
    return _truncate_text(f"Detailed usage notes are documented in {top_paths}.", limit=170)


def _extract_pr_usage_hint(context: PullRequestContext) -> tuple[Optional[str], Optional[str]]:
    section_lines = _extract_usage_lines_from_sections(context.body)
    best_section_line = _pick_best_usage_line(section_lines)
    if best_section_line:
        return best_section_line, _summarize_doc_files(context.files)

    code_lines = _extract_usage_lines_from_code_blocks(context.body)
    best_code_line = _pick_best_usage_line(code_lines)
    if best_code_line:
        return best_code_line, _summarize_doc_files(context.files)

    body_lines = _split_candidate_lines(context.body)
    best_body_line = _pick_best_usage_line(body_lines)
    if best_body_line:
        return best_body_line, _summarize_doc_files(context.files)

    best_doc_line = _pick_best_usage_line(context.doc_lines)
    if best_doc_line:
        return best_doc_line, _summarize_doc_files(context.files)

    comment_lines: list[str] = []
    for comment in context.comments:
        comment_lines.extend(_extract_usage_lines_from_sections(comment))
        comment_lines.extend(_extract_usage_lines_from_code_blocks(comment))
        comment_lines.extend(_split_candidate_lines(comment))
    best_comment_line = _pick_best_usage_line(comment_lines)
    if best_comment_line:
        return best_comment_line, _summarize_doc_files(context.files)

    return None, _summarize_doc_files(context.files)


def parse_semver(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError(f"Invalid semantic version: {value}")
    return tuple(int(part) for part in match.groups())


def parse_highlight_pr_numbers(raw: str) -> list[int]:
    tokens = [token.strip() for token in re.split(r"[,\s]+", raw.strip()) if token.strip()]
    if not tokens:
        raise ValueError("No PR numbers provided")

    numbers: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not re.fullmatch(r"\d+", token):
            raise ValueError(f"Invalid PR number: {token}")
        value = int(token)
        if value <= 0:
            raise ValueError(f"Invalid PR number: {token}")
        if value in seen:
            continue
        seen.add(value)
        numbers.append(value)
    return numbers


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


def _format_change_summary_text(entry: ChangeEntry) -> str:
    summary = _sanitize_text_line(entry.summary or "")
    if summary:
        return summary
    fallback = _sanitize_text_line(entry.raw_text)
    return fallback or "Release update"


def format_change_lines(entries: Iterable[ChangeEntry], repo: Optional[str] = None) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        summary = _format_change_summary_text(entry)
        pr_url = entry.pr_url.strip() if entry.pr_url else None

        author_mention: Optional[str] = None
        if repo and entry.pr_number:
            meta = _fetch_pr_metadata(repo, entry.pr_number)
            if meta:
                if meta.title:
                    summary = _sanitize_pr_title(meta.title)
                author_mention = _format_author_mention(meta.author_login)

        if pr_url and author_mention:
            lines.append(f"* {summary} by {author_mention} in {pr_url}")
            continue

        if pr_url:
            lines.append(f"* {summary} in {pr_url}")
            continue

        if author_mention:
            lines.append(f"* {summary} by {author_mention}")
            continue

        lines.append(f"* {summary}")

    return lines


def _clean_summary_for_highlight(summary: str) -> str:
    cleaned = summary.strip()
    cleaned = re.sub(
        r"^(feat|fix|perf|refactor|docs|test|chore|build|ci|feature|bugfix|security|optimize)(\([^)]+\))?:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", cleaned).strip()
    cleaned = cleaned.rstrip(".").strip()
    return cleaned


def _ensure_sentence_end(text: str) -> str:
    if text.endswith((".", "!", "?")):
        return text
    return f"{text}."


def _build_highlight_title(summary: str) -> str:
    if not summary:
        return "Release Update"
    if len(summary) <= 110:
        return summary
    return f"{summary[:107].rstrip()}..."


def _normalize_usage_hint(text: str) -> str:
    normalized = _sanitize_text_line(text)
    if not normalized:
        return ""

    lowered = normalized.lower()
    users_idx = lowered.find("users can ")
    if 0 < users_idx < 120:
        normalized = normalized[users_idx:]
        lowered = normalized.lower()

    curl_match = re.search(r"(curl\s+[^\n`]+?)(?:\s+and\s+you\s+can|\s+to\s+|\s*$)", normalized, re.IGNORECASE)
    if curl_match:
        command = curl_match.group(1).strip().rstrip(".,;")
        normalized = f"You can verify this with `{_truncate_text(command, limit=140)}`"
        lowered = normalized.lower()

    client_call = re.search(r"client\.([A-Za-z_][A-Za-z0-9_]*)\(\)", normalized)
    if client_call:
        method = client_call.group(1)
        normalized = f"OpenAPI Java client now supports `client.{method}()` for this scenario"
        lowered = normalized.lower()

    endpoint_match = re.search(
        r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9._~!$&'()*+,;=:@%/\-{}]+)",
        normalized,
        re.IGNORECASE,
    )
    if endpoint_match and "call `" not in lowered:
        method = endpoint_match.group(1).upper()
        path = endpoint_match.group(2)
        normalized = f"Call `{method} {path}` to use this capability"
        lowered = normalized.lower()

    if lowered.startswith("you can "):
        return _truncate_text(_ensure_sentence_end(normalized), limit=220)
    if lowered.startswith("users can "):
        return _truncate_text(_ensure_sentence_end(normalized[0].upper() + normalized[1:]), limit=220)
    if lowered.startswith("call ") or lowered.startswith("openapi java client now supports"):
        return _truncate_text(_ensure_sentence_end(normalized), limit=220)
    return _truncate_text(_ensure_sentence_end(normalized), limit=220)


def _build_summary_sentence(summary: str) -> str:
    lower = summary.lower()
    if lower.startswith("support "):
        return _ensure_sentence_end(f"Apollo Java client now supports {summary[8:].strip()}")
    if lower.startswith("provide "):
        return _ensure_sentence_end(f"Apollo Java client now provides {summary[8:].strip()}")
    if lower.startswith("add "):
        return _ensure_sentence_end(f"Apollo Java client now adds {summary[4:].strip()}")
    if lower.startswith("added "):
        return _ensure_sentence_end(f"Apollo Java client now adds {summary[6:].strip()}")
    if lower.startswith("fix "):
        return _ensure_sentence_end(f"Apollo Java client now fixes {summary[4:].strip()}")
    if lower.startswith("enhance "):
        return _ensure_sentence_end(f"Apollo Java client now enhances {summary[8:].strip()}")
    return _ensure_sentence_end(f"Apollo Java client now includes {summary}")


def _build_highlight_body(
    summary: str,
    release_version: str,
    usage_hint: Optional[str] = None,
    doc_hint: Optional[str] = None,
) -> str:
    if not summary:
        return f"Apollo Java {release_version} is now available."

    if usage_hint:
        normalized = _normalize_usage_hint(usage_hint)
        if normalized:
            return normalized

    if doc_hint:
        normalized_doc_hint = _sanitize_text_line(doc_hint)
        if normalized_doc_hint:
            return _ensure_sentence_end(_truncate_text(normalized_doc_hint, limit=220))

    return _build_summary_sentence(summary)


def build_highlights(
    entries: list[ChangeEntry],
    release_version: str,
    highlight_pr_numbers: list[int],
    repo: Optional[str] = None,
) -> list[HighlightItem]:
    if not highlight_pr_numbers:
        return []

    entry_by_pr: dict[int, ChangeEntry] = {}
    for entry in entries:
        if entry.pr_number is None:
            continue
        entry_by_pr.setdefault(entry.pr_number, entry)

    missing_prs = [pr_number for pr_number in highlight_pr_numbers if pr_number not in entry_by_pr]
    if missing_prs:
        missing_str = ", ".join(f"#{pr_number}" for pr_number in missing_prs)
        raise ValueError(
            f"Selected highlight PRs not found in CHANGES.md for Apollo Java {release_version}: {missing_str}"
        )

    highlights: list[HighlightItem] = []
    for pr_number in highlight_pr_numbers:
        entry = entry_by_pr[pr_number]
        summary = _clean_summary_for_highlight(entry.summary or entry.raw_text)
        if not summary and repo:
            meta = _fetch_pr_metadata(repo, pr_number)
            if meta and meta.title:
                summary = _sanitize_text_line(meta.title)
        if not summary:
            summary = f"PR #{pr_number}"

        usage_hint: Optional[str] = None
        doc_hint: Optional[str] = None
        if repo:
            pr_context = _fetch_pr_context(repo, pr_number)
            if pr_context:
                usage_hint, doc_hint = _extract_pr_usage_hint(pr_context)

        highlights.append(
            HighlightItem(
                title=_build_highlight_title(summary),
                body=_build_highlight_body(
                    summary,
                    release_version,
                    usage_hint=usage_hint,
                    doc_hint=doc_hint,
                ),
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
        lines.extend(["### Release Update", f"Apollo Java {release_version} is now available.", ""])

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
        lines.append(f"* Apollo Java {release_version} is now available.")

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
    highlight_pr_numbers: list[int],
    previous_tag_name: Optional[str] = None,
) -> dict[str, object]:
    if not highlight_pr_numbers:
        raise ValueError("highlight_pr_numbers is required to build Highlights")

    entries, milestone_line = parse_change_entries(changes_file, release_version)
    highlights = build_highlights(
        entries,
        release_version,
        highlight_pr_numbers=highlight_pr_numbers,
        repo=repo,
    )
    change_lines = format_change_lines(entries, repo=repo)

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
        "highlights": [asdict(item) for item in highlights],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Apollo Java release notes from CHANGES.md")
    parser.add_argument("--repo", default="apolloconfig/apollo-java", help="GitHub repository")
    parser.add_argument("--release-version", required=True, help="Release version, e.g. 2.5.0")
    parser.add_argument(
        "--highlight-prs",
        required=True,
        help="Comma-separated PR numbers used for Highlights, e.g. 115,121",
    )
    parser.add_argument("--changes-file", default="CHANGES.md", help="Path to CHANGES.md")
    parser.add_argument("--kind", choices=["release", "announcement"], required=True)
    parser.add_argument("--output", required=True, help="Output markdown file")
    parser.add_argument("--target-commitish", default="main", help="Release target branch/commit")
    parser.add_argument("--previous-tag", default=None, help="Optional previous tag override")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        highlight_pr_numbers = parse_highlight_pr_numbers(args.highlight_prs)
    except ValueError as exc:
        raise SystemExit(f"--highlight-prs is invalid: {exc}") from exc

    content = build_release_content(
        repo=args.repo,
        release_version=args.release_version,
        changes_file=Path(args.changes_file),
        target_commitish=args.target_commitish,
        highlight_pr_numbers=highlight_pr_numbers,
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
        "highlights_count": len(content["highlights"]),
    }
    print(json.dumps(metadata, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
