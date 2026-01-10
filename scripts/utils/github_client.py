"""
GitHub API Client Utilities
Handles all interactions with GitHub REST API and raw content.
"""

import os
import re
import base64
from datetime import datetime, timedelta
from typing import Optional
import requests


class GitHubClient:
    """Wrapper for GitHub REST API with rate limit handling."""
    
    BASE_URL = "https://api.github.com"
    RAW_URL = "https://raw.githubusercontent.com"
    
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
    
    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make an authenticated request to GitHub API."""
        url = f"{self.BASE_URL}{endpoint}" if not endpoint.startswith("http") else endpoint
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response
    
    def get_repo(self, owner: str, repo: str) -> dict:
        """Fetch repository metadata."""
        return self._request("GET", f"/repos/{owner}/{repo}").json()
    
    def get_commits(self, owner: str, repo: str, since_days: int = 30) -> list:
        """Fetch recent commits with conventional commit parsing."""
        since = (datetime.utcnow() - timedelta(days=since_days)).isoformat() + "Z"
        response = self._request(
            "GET", 
            f"/repos/{owner}/{repo}/commits",
            params={"since": since, "per_page": 100}
        )
        
        commits = []
        for commit in response.json():
            message = commit["commit"]["message"].split("\n")[0]  # First line only
            commits.append({
                "sha": commit["sha"][:7],
                "date": commit["commit"]["author"]["date"][:10],
                "message": message,
                "type": self._parse_commit_type(message),
                "author": commit["commit"]["author"]["name"]
            })
        return commits
    
    def _parse_commit_type(self, message: str) -> str:
        """Extract conventional commit type from message."""
        # Match patterns like "feat:", "fix(scope):", "chore!:", etc.
        match = re.match(r"^(\w+)(?:\([^)]+\))?!?:", message.lower())
        if match:
            commit_type = match.group(1)
            # Normalize common types
            type_map = {
                "feat": "feat",
                "feature": "feat",
                "fix": "fix",
                "bugfix": "fix",
                "docs": "docs",
                "doc": "docs",
                "style": "style",
                "refactor": "refactor",
                "perf": "perf",
                "test": "test",
                "tests": "test",
                "chore": "chore",
                "build": "chore",
                "ci": "chore",
            }
            return type_map.get(commit_type, "other")
        return "other"
    
    def get_releases(self, owner: str, repo: str, limit: int = 5) -> list:
        """Fetch recent releases."""
        response = self._request(
            "GET",
            f"/repos/{owner}/{repo}/releases",
            params={"per_page": limit}
        )
        
        releases = []
        for release in response.json():
            releases.append({
                "tag": release["tag_name"],
                "name": release["name"] or release["tag_name"],
                "date": release["published_at"][:10] if release["published_at"] else None,
                "notes": release["body"][:500] if release["body"] else "",
                "prerelease": release["prerelease"],
                "draft": release["draft"]
            })
        return releases
    
    def get_languages(self, owner: str, repo: str) -> list:
        """Fetch repository languages sorted by usage."""
        response = self._request("GET", f"/repos/{owner}/{repo}/languages")
        languages = response.json()
        # Sort by bytes and return names only
        sorted_langs = sorted(languages.items(), key=lambda x: x[1], reverse=True)
        return [lang for lang, _ in sorted_langs]
    
    def get_readme(self, owner: str, repo: str, branch: str = "main") -> dict:
        """Fetch README content and metadata."""
        try:
            # Get README metadata from API
            meta_response = self._request("GET", f"/repos/{owner}/{repo}/readme")
            meta = meta_response.json()
            
            # Get raw content
            raw_url = f"{self.RAW_URL}/{owner}/{repo}/{branch}/README.md"
            content_response = self.session.get(raw_url)
            content_response.raise_for_status()
            
            return {
                "content": content_response.text,
                "sha": meta["sha"],
                "size": meta["size"],
                "last_modified": None  # Would need a separate commit query
            }
        except requests.exceptions.HTTPError:
            return {"content": "", "sha": None, "size": 0, "last_modified": None}
    
    def get_issues_count(self, owner: str, repo: str) -> dict:
        """Fetch issue counts (open/closed)."""
        repo_data = self.get_repo(owner, repo)
        return {
            "open": repo_data.get("open_issues_count", 0),
            "has_issues": repo_data.get("has_issues", False)
        }
    
    def search_repos_by_topic(self, owner: str, topic: str) -> list:
        """Search for repos with a specific topic."""
        response = self._request(
            "GET",
            "/search/repositories",
            params={"q": f"topic:{topic} user:{owner}", "per_page": 100}
        )
        return [repo["full_name"] for repo in response.json().get("items", [])]
    
    def get_file_content(self, owner: str, repo: str, path: str, branch: str = "main") -> Optional[str]:
        """Fetch a specific file's content."""
        try:
            raw_url = f"{self.RAW_URL}/{owner}/{repo}/{branch}/{path}"
            response = self.session.get(raw_url)
            response.raise_for_status()
            return response.text
        except requests.exceptions.HTTPError:
            return None


class BioSiteClient:
    """Client for reading the Bio Site repository state."""
    
    def __init__(self, github_client: GitHubClient, repo: str, branch: str = "main"):
        self.gh = github_client
        self.owner, self.repo = repo.split("/")
        self.branch = branch
    
    def get_project_index(self, output_dir: str = "projects/") -> dict:
        """Build index of existing project pages."""
        index = {}
        
        try:
            # List files in output directory
            response = self.gh._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/contents/{output_dir}",
                params={"ref": self.branch}
            )
            
            for item in response.json():
                if item["type"] == "file" and item["name"].endswith(".html"):
                    slug = item["name"].replace(".html", "")
                    content = self.gh.get_file_content(
                        self.owner, self.repo, item["path"], self.branch
                    )
                    
                    index[slug] = {
                        "exists": True,
                        "path": item["path"],
                        "sha": item["sha"],
                        "content": content,
                        "manual_sections": self._extract_manual_sections(content) if content else [],
                        "locked": self._check_lock(content) if content else False,
                        "last_deploy": self._extract_deploy_date(content) if content else None
                    }
        except requests.exceptions.HTTPError:
            # Directory doesn't exist yet
            pass
        
        return index
    
    def _extract_manual_sections(self, html: str) -> list:
        """Extract <!-- MANUAL:xxx -->...<!-- /MANUAL:xxx --> blocks."""
        pattern = r'(<!-- MANUAL:(\w+) -->.*?<!-- /MANUAL:\2 -->)'
        matches = re.findall(pattern, html, re.DOTALL)
        return [match[0] for match in matches]
    
    def _check_lock(self, html: str) -> bool:
        """Check if page has <!-- LOCK --> marker."""
        return "<!-- LOCK -->" in html
    
    def _extract_deploy_date(self, html: str) -> Optional[str]:
        """Extract last deploy date from <!-- DEPLOYED: YYYY-MM-DD --> comment."""
        match = re.search(r'<!-- DEPLOYED: (\d{4}-\d{2}-\d{2}) -->', html)
        return match.group(1) if match else None
