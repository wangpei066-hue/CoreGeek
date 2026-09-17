#!/usr/bin/env python3
"""Fetch the newest GitHub issue whose title contains a given phrase.

The result is stored as Markdown under logs/RealWar.  The script uses only
Python's standard library so it can run in the competition environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_REPOSITORY = "wangpei066-hue/Coding-Competition"
DEFAULT_KEYWORD = "自进化任务分析"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "logs" / "RealWar"


def matches_keyword(issue: dict, keyword: str) -> bool:
    """Match the keyword in the Issue's Markdown heading/body, or web title."""
    if keyword in issue.get("title", ""):
        return True
    body = issue.get("body") or ""
    heading_pattern = rf"(?m)^\s{{0,3}}#{{1,6}}\s+.*{re.escape(keyword)}.*$"
    return re.search(heading_pattern, body) is not None


def fetch_issue(repository: str, keyword: str, token: str | None = None, issue_number: int | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "realwar-issue-fetcher/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # The search endpoint can return 422 for otherwise valid non-ASCII
    # queries.  Listing issues and filtering locally is more predictable and
    # also lets us explicitly exclude pull requests (the list endpoint mixes
    # them with issues).
    if issue_number is not None:
        url = f"https://api.github.com/repos/{quote(repository, safe='/')}/issues/{issue_number}"
    else:
        params = urlencode({"state": "all", "sort": "created", "direction": "desc", "per_page": 2})
        url = f"https://api.github.com/repos/{quote(repository, safe='/')}/issues?{params}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", exc.reason)
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = exc.reason
        raise RuntimeError(f"GitHub API 请求失败（HTTP {exc.code}）：{detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 GitHub：{exc.reason}") from exc
    items = [payload] if issue_number is not None else payload
    for item in items:
        if "pull_request" not in item and matches_keyword(item, keyword):
            return item
    if issue_number is not None and items and "pull_request" not in items[0]:
        return items[0]
    raise RuntimeError(f"没有找到标题包含“{keyword}”的 Issue。")


def render_issue(issue: dict, repository: str, keyword: str) -> str:
    body = issue.get("body") or "（Issue 没有正文）"
    labels = ", ".join(label.get("name", "") for label in issue.get("labels", []))
    return (
        f"# {issue.get('title', '').strip()}\n\n"
        f"- 仓库：`{repository}`\n"
        f"- Issue：#{issue['number']}\n"
        f"- 状态：{issue.get('state', 'unknown')}\n"
        f"- 作者：{issue.get('user', {}).get('login', 'unknown')}\n"
        f"- 创建时间：{issue.get('created_at', '')}\n"
        f"- 更新时间：{issue.get('updated_at', '')}\n"
        f"- 标签：{labels or '无'}\n"
        f"- 链接：{issue.get('html_url', '')}\n"
        f"- 匹配字段：`{keyword}`\n\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def write_result(issue: dict, content: str, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = output_dir / f"issue_{issue['number']}.md"
    latest = output_dir / "latest.md"
    for path in (snapshot, latest):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return latest, snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY), help="owner/name")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD, help="标题必须包含的字段")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token（也可用 GITHUB_TOKEN）")
    parser.add_argument("--issue", type=int, help="直接抓取指定 Issue 编号（测试用，不进行标题筛选）")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[^/\\]+/[^/\\]+", args.repo):
        parser.error("--repo 必须是 owner/name 格式")
    try:
        issue = fetch_issue(args.repo, args.keyword, args.token, args.issue)
        content = render_issue(issue, args.repo, args.keyword)
        latest, snapshot = write_result(issue, content, args.output_dir)
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"已保存最新 Issue #{issue['number']}：{latest}")
    print(f"已保存快照：{snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
