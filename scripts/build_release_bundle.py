#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "gui.py"
APP_NAME = "CodeXRegister"
ASSET_ITEMS = [
    ("codex_register/gui_frontend.html", "codex_register"),
    ("codex_register/gui_frontend_style.css", "codex_register"),
    ("codex_register/gui_frontend_bootstrap.js", "codex_register"),
    ("codex_register/gui_frontend_app.js", "codex_register"),
    ("codex_register/gui_frontend_app_setup.js", "codex_register"),
    ("codex_register/gui_frontend_app_template.html", "codex_register"),
    ("README.md", "."),
    ("LICENSE", "."),
    ("gui_config.example.json", "."),
    ("VERSION", "."),
    ("REPOSITORY", "."),
]


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def write_version(tag: str) -> None:
    version = str(tag or "").strip().lstrip("vV")
    if not version:
        version = "0.0.0-dev"
    (ROOT / "VERSION").write_text(version + "\n", encoding="utf-8")


def write_repository() -> None:
    repo = str(os.getenv("GITHUB_REPOSITORY", "") or "").strip()
    if not repo:
        return
    (ROOT / "REPOSITORY").write_text(repo + "\n", encoding="utf-8")


def build(tag: str, platform: str) -> Path:
    write_version(tag)
    write_repository()

    build_dir = ROOT / "build"
    dist_dir = ROOT / "dist"
    artifact_dir = ROOT / "release-artifacts"
    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(dist_dir, ignore_errors=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        APP_NAME,
    ]

    for name, dest in ASSET_ITEMS:
        src = ROOT / name
        if src.exists():
            cmd.extend(["--add-data", f"{src}{sep}{dest}"])

    cmd.append(str(ENTRY))
    run(cmd)

    bundle_dir = dist_dir / APP_NAME
    if not bundle_dir.exists():
        raise RuntimeError(f"打包失败，未找到目录: {bundle_dir}")

    archive_base = artifact_dir / f"{APP_NAME}-{tag}-{platform}"
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", root_dir=str(dist_dir), base_dir=APP_NAME))
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build release bundle")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.2.3")
    parser.add_argument("--platform", required=True, help="platform name for artifact")
    args = parser.parse_args()

    out = build(tag=str(args.tag), platform=str(args.platform))
    print(out)


if __name__ == "__main__":
    main()
