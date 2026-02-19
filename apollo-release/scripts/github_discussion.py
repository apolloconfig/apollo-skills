#!/usr/bin/env python3
"""Create GitHub discussions through GraphQL."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


class DiscussionError(RuntimeError):
    """Raised when discussion creation fails."""


def run_graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        cmd.extend(["-F", f"{key}={value}"])
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def load_repository_info(repo: str) -> tuple[str, list[dict[str, str]]]:
    owner, name = repo.split("/", 1)
    query = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    discussionCategories(first: 20) {
      nodes {
        id
        name
        slug
      }
    }
  }
}
"""
    payload = run_graphql(query, {"owner": owner, "name": name})
    repository = payload.get("data", {}).get("repository")
    if not repository:
        raise DiscussionError(f"Failed to load repository metadata for {repo}")
    categories = repository.get("discussionCategories", {}).get("nodes", [])
    return repository["id"], categories


def select_category_id(categories: list[dict[str, str]], category: str) -> str:
    needle = category.strip().lower()
    for item in categories:
        if item.get("name", "").strip().lower() == needle:
            return item["id"]
        if item.get("slug", "").strip().lower() == needle:
            return item["id"]
    raise DiscussionError(f"Category '{category}' not found")


def create_discussion(repo: str, category: str, title: str, body: str) -> str:
    repository_id, categories = load_repository_info(repo)
    category_id = select_category_id(categories, category)

    mutation = """
mutation($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {
    repositoryId: $repositoryId,
    categoryId: $categoryId,
    title: $title,
    body: $body
  }) {
    discussion {
      id
      url
      number
      title
    }
  }
}
"""
    payload = run_graphql(
        mutation,
        {
            "repositoryId": repository_id,
            "categoryId": category_id,
            "title": title,
            "body": body,
        },
    )
    discussion = payload.get("data", {}).get("createDiscussion", {}).get("discussion", {})
    if not discussion or "url" not in discussion:
        raise DiscussionError("GitHub did not return a discussion URL")
    return discussion["url"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create GitHub discussion")
    parser.add_argument("create", nargs="?", default="create")
    parser.add_argument("--repo", default="apolloconfig/apollo")
    parser.add_argument("--category", default="Announcements")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default=None)
    parser.add_argument("--body-file", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.body and args.body_file:
        raise SystemExit("Use either --body or --body-file")
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body:
        body = args.body
    else:
        raise SystemExit("Either --body or --body-file is required")

    try:
        url = create_discussion(
            repo=args.repo,
            category=args.category,
            title=args.title,
            body=body,
        )
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise SystemExit(f"GitHub API call failed: {message}")
    except DiscussionError as exc:
        raise SystemExit(str(exc))

    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
