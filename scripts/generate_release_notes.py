#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def run_git(args: list[str]) -> str:
    try:
        out = subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL, text=True)
        return str(out or "").strip()
    except Exception:
        return ""


def get_tag_message(tag: str) -> str:
    return run_git(["for-each-ref", f"refs/tags/{tag}", "--format=%(contents)"])


def get_previous_tag(tag: str) -> str:
    tags = [x.strip() for x in run_git(["tag", "--sort=-creatordate"]).splitlines() if x.strip()]
    for t in tags:
        if t != tag:
            return t
    return ""


def get_commit_summary(tag: str, previous_tag: str) -> str:
    if previous_tag:
        rng = f"{previous_tag}..{tag}"
    else:
        rng = tag
    txt = run_git(["log", "--pretty=- %s", rng])
    if not txt:
        return "- 无可用提交记录"
    return txt


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate release notes markdown")
    parser.add_argument("--tag", required=True, help="release tag")
    parser.add_argument("--out", required=True, help="output markdown path")
    args = parser.parse_args()

    tag = str(args.tag or "").strip()
    out_path = Path(str(args.out))
    repo = str(os.getenv("GITHUB_REPOSITORY", "")).strip()
    sha = str(os.getenv("GITHUB_SHA", "")).strip()

    tag_msg = get_tag_message(tag)
    prev_tag = get_previous_tag(tag)
    commits = get_commit_summary(tag, prev_tag)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if not tag_msg:
        tag_msg = "- 版本更新，详见下方提交记录。"

    lines = [
        "## 更新信息",
        tag_msg,
        "",
        "## 构建信息",
        f"- 版本标签: `{tag}`",
        f"- 构建时间: `{now_utc}`",
        f"- 提交: `{sha or '-'}`",
        "- 平台包: Windows / macOS / Linux",
        "",
        "## 主要提交",
        commits,
    ]

    if repo:
        lines.extend([
            "",
            "## 链接",
            f"- 仓库: https://github.com/{repo}",
            f"- 版本页: https://github.com/{repo}/releases/tag/{tag}",
        ])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
