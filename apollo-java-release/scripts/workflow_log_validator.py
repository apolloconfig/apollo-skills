#!/usr/bin/env python3
"""Validate Maven upload completeness from GitHub workflow logs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def direct_child_text(element: ET.Element, child_name: str, default: Optional[str] = None) -> Optional[str]:
    for child in list(element):
        if strip_namespace(child.tag) == child_name and child.text is not None:
            return child.text.strip()
    return default


def direct_child_modules(element: ET.Element) -> list[str]:
    modules: list[str] = []
    for child in list(element):
        if strip_namespace(child.tag) != "modules":
            continue
        for module in list(child):
            if strip_namespace(module.tag) == "module" and module.text:
                modules.append(module.text.strip())
    return modules


def parse_pom_file(pom_path: Path) -> tuple[Optional[str], str, list[str]]:
    tree = ET.parse(pom_path)
    root = tree.getroot()
    artifact_id = direct_child_text(root, "artifactId")
    packaging = direct_child_text(root, "packaging", "jar") or "jar"
    modules = direct_child_modules(root)
    return artifact_id, packaging, modules


def collect_non_pom_artifacts(repo_root: Path) -> list[str]:
    artifacts: set[str] = set()
    visited: set[Path] = set()

    def walk(pom_path: Path) -> None:
        resolved = pom_path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        artifact_id, packaging, modules = parse_pom_file(pom_path)
        if artifact_id and packaging != "pom":
            artifacts.add(artifact_id)
        for module in modules:
            module_pom = pom_path.parent / module / "pom.xml"
            if module_pom.exists():
                walk(module_pom)

    walk(repo_root / "pom.xml")
    return sorted(artifacts)


def parse_uploaded_urls(log_text: str, repository_name: str) -> list[str]:
    sanitized = ANSI_ESCAPE_RE.sub("", log_text)
    pattern = re.compile(rf"Uploaded to {re.escape(repository_name)}:\s+(\S+)")
    return sorted(set(pattern.findall(sanitized)))


def artifact_matches(url: str, artifact_id: str, suffix: str) -> bool:
    if f"/{artifact_id}/" not in url:
        return False
    if not url.endswith(suffix):
        return False
    if url.endswith(f"{suffix}.asc"):
        return False
    return True


def validate_uploaded_artifacts(uploaded_urls: list[str], artifact_ids: list[str]) -> dict[str, object]:
    artifact_reports: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []

    for artifact_id in artifact_ids:
        jar_urls = [url for url in uploaded_urls if artifact_matches(url, artifact_id, ".jar")]
        pom_urls = [url for url in uploaded_urls if artifact_matches(url, artifact_id, ".pom")]
        has_jar = len(jar_urls) > 0
        has_pom = len(pom_urls) > 0

        report = {
            "artifact_id": artifact_id,
            "has_jar": has_jar,
            "has_pom": has_pom,
            "jar_urls": jar_urls,
            "pom_urls": pom_urls,
        }
        artifact_reports.append(report)

        missing_fields: list[str] = []
        if not has_jar:
            missing_fields.append("jar")
        if not has_pom:
            missing_fields.append("pom")
        if missing_fields:
            missing.append({"artifact_id": artifact_id, "missing": missing_fields})

    return {
        "artifact_reports": artifact_reports,
        "missing": missing,
        "valid": len(missing) == 0,
    }


def fetch_workflow_log(repo: str, run_id: int) -> str:
    completed = subprocess.run(
        ["gh", "run", "view", str(run_id), "--repo", repo, "--log"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def build_validation_report(
    repo_root: Path,
    uploaded_urls: list[str],
    run_id: Optional[int],
    repository_name: str,
) -> dict[str, object]:
    artifacts = collect_non_pom_artifacts(repo_root)
    validation = validate_uploaded_artifacts(uploaded_urls, artifacts)

    return {
        "run_id": run_id,
        "repository_name": repository_name,
        "uploaded_urls_count": len(uploaded_urls),
        "uploaded_urls": uploaded_urls,
        "artifact_ids": artifacts,
        "artifact_reports": validation["artifact_reports"],
        "missing": validation["missing"],
        "valid": validation["valid"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate uploaded Maven artifacts from workflow logs")
    parser.add_argument("--repo-root", default=".", help="Apollo Java repository root")
    parser.add_argument("--repo", default="apolloconfig/apollo-java", help="GitHub repository")
    parser.add_argument("--run-id", type=int, default=None, help="Workflow run id")
    parser.add_argument("--log-file", default=None, help="Use local log file instead of gh run view")
    parser.add_argument("--repository-name", default="releases", help="Maven repository alias in logs")
    parser.add_argument("--output", default=None, help="Write report json to this file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.run_id is None and args.log_file is None:
        raise SystemExit("Either --run-id or --log-file must be provided")

    if args.log_file:
        log_text = Path(args.log_file).read_text(encoding="utf-8")
    else:
        log_text = fetch_workflow_log(args.repo, args.run_id)

    uploaded_urls = parse_uploaded_urls(log_text, args.repository_name)
    report = build_validation_report(
        repo_root=Path(args.repo_root),
        uploaded_urls=uploaded_urls,
        run_id=args.run_id,
        repository_name=args.repository_name,
    )

    serialized = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
