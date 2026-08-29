from __future__ import annotations

import os
from typing import List

import httpx

from github_infrastructure.config import settings

GITHUB_API_BASE = "https://api.github.com"


class GitHubService:
    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT") or ""
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def fetch_pr_commits(self, owner: str, repository: str, pr_number: int) -> List[str]:
        import asyncio

        def _sync_fetch() -> List[str]:
            url = f"{GITHUB_API_BASE}/repos/{owner}/{repository}/pulls/{pr_number}/commits"
            headers = self._headers()
            params = {"per_page": 100}
            shas: List[str] = []
            while url:
                resp = httpx.get(url, headers=headers, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                for item in data:
                    sha = item.get("sha")
                    if sha:
                        shas.append(sha)
                # pagination via Link header
                link = resp.headers.get("Link", "")
                next_url = None
                if 'rel="next"' in link:
                    for part in link.split(","):
                        if 'rel="next"' in part:
                            next_url = part.split(";")[0].strip().strip("<>").strip()
                            break
                url = next_url
                params = {}
            return shas

        return await asyncio.to_thread(_sync_fetch)

    def fetch_pr_commits_sync(self, owner: str, repository: str, pr_number: int) -> List[str]:
        import httpx

        url = f"{GITHUB_API_BASE}/repos/{owner}/{repository}/pulls/{pr_number}/commits"
        headers = self._headers()
        resp = httpx.get(url, headers=headers, params={"per_page": 100}, timeout=self.timeout)
        resp.raise_for_status()
        return [item["sha"] for item in resp.json() if item.get("sha")]
