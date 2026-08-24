from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _bootstrap import load_env

load_env()

import httpx

from github_infrastructure.change_records import (
    GitHubApiError,
    build_change_record,
    send_github_api_request,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test GitHub API access against a (private) repository.")
    parser.add_argument("owner", help="Owner/org name")
    parser.add_argument("repository", help="Repository name")
    parser.add_argument("sha", nargs="?", default="", help="Commit SHA (default: latest single-parent commit)")
    parser.add_argument("--branch", default="", help="Branch label stored on the record")
    args = parser.parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN missing from backend/.env")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }

    whoami = httpx.get("https://api.github.com/user", headers=headers, timeout=30)
    print(f"[auth] token identity: {whoami.json().get('login', '?')} ({whoami.status_code})")

    repo_resp = httpx.get(
        f"https://api.github.com/repos/{args.owner}/{args.repository}", headers=headers, timeout=30
    )
    if repo_resp.status_code != 200:
        print(f"[FAIL] cannot see {args.owner}/{args.repository}: HTTP {repo_resp.status_code}")
        print("       -> 404 usually means the token lacks 'repo' scope / repo not granted to a fine-grained PAT")
        return 1
    info = repo_resp.json()
    print(f"[repo] {info['full_name']} | private={info['private']} | default_branch={info['default_branch']}")
    if not info["private"]:
        print("[note] this repo is public - token scope not actually being exercised")

    sha = args.sha
    if not sha:
        commits = httpx.get(
            f"https://api.github.com/repos/{args.owner}/{args.repository}/commits",
            headers=headers,
            params={"per_page": 20},
            timeout=30,
        )
        commits.raise_for_status()
        for item in commits.json():
            if len(item["parents"]) == 1:
                sha = item["sha"]
                break
        sha = sha or info["default_branch"]
        print(f"[select] using commit: {sha}")

    try:
        record = build_change_record(args.owner, args.repository, sha, branch=args.branch)
    except GitHubApiError as exc:
        print(f"[FAIL] {exc}")
        return 1

    raw = send_github_api_request(args.owner, args.repository, sha)
    print(f"[api] files={len(raw.get('files', []))} stats={raw.get('stats')}")
    print(f"[record] author={record.author} timestamp={record.timestamp.isoformat()}")
    print(f"[record] message={record.commit_message.splitlines()[0][:70]}")
    print(f"[record] components={record.components} change_type={record.change_type}")
    for f in record.changed_files:
        print(f"         - {f.path} [{f.status}] +{f.additions}/-{f.deletions} functions={f.functions}")
    print(f"[record] evidence items={len(record.evidence)}")

    out_path = Path(__file__).resolve().parents[1] / f"change_record_{sha[:8]}.json"
    out_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    print(f"\n[saved] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
