"""
Phase 1: Collector & Normalizer
Gathers data from GitHub and Bio Site, calculates significance scores,
and outputs context.json for downstream AI agents.
"""

import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.utils.github_client import GitHubClient, BioSiteClient


class Collector:
    """
    Collects data from three sources:
    1. GitHub REST API (repos, commits, releases, languages)
    2. GitHub Raw Content (README)
    3. Bio Site Repo (existing pages, manual sections, locks)
    
    Outputs: _data/context.json
    """
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        self.gh = GitHubClient()
        self.bio_site = BioSiteClient(
            self.gh,
            self.config["target"]["repo"],
            self.config["target"]["branch"]
        )
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        self.force_update = os.environ.get("FORCE_UPDATE", "false").lower() == "true"
    
    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            return yaml.safe_load(f)
    
    def run(self) -> dict:
        """Execute the collection pipeline."""
        print("🔍 Starting Collector...")
        
        # Step 1: Discover projects
        projects = self._discover_projects()
        print(f"📦 Found {len(projects)} projects to check")
        
        # Step 2: Build Bio Site index
        bio_index = self.bio_site.get_project_index(
            self.config["target"]["output_dir"]
        )
        print(f"📄 Bio Site has {len(bio_index)} existing project pages")
        
        # Step 3: Collect data for each project
        context = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "config_hash": self._hash_config(),
            "projects": [],
            "summary": {
                "total": len(projects),
                "updates": 0,
                "skips": 0,
                "new": 0,
                "locked": 0
            }
        }
        
        for repo_full_name in projects:
            print(f"\n  → Processing {repo_full_name}")
            project_data = self._collect_project(repo_full_name, bio_index)
            context["projects"].append(project_data)
            
            # Update summary
            if project_data["locked"]:
                context["summary"]["locked"] += 1
            elif project_data["status"] == "update":
                context["summary"]["updates"] += 1
            elif project_data["status"] == "new":
                context["summary"]["new"] += 1
            else:
                context["summary"]["skips"] += 1
        
        # Step 4: Save context.json
        self._save_context(context)
        
        # Step 5: Set GitHub Actions output
        has_updates = (context["summary"]["updates"] + context["summary"]["new"]) > 0
        self._set_output("has_updates", str(has_updates).lower())
        
        print(f"\n✅ Collector complete!")
        print(f"   Updates: {context['summary']['updates']}")
        print(f"   New: {context['summary']['new']}")
        print(f"   Skipped: {context['summary']['skips']}")
        print(f"   Locked: {context['summary']['locked']}")
        
        return context
    
    def _discover_projects(self) -> list:
        """Discover projects via GitHub topics or static list."""
        discovery = self.config["discovery"]
        
        if discovery["method"] == "list":
            print("  Using static project list")
            return discovery["fallback_list"]
        
        print(f"  Searching for repos with topic: {discovery['topic_tag']}")
        try:
            repos = self.gh.search_repos_by_topic(
                discovery["owner"],
                discovery["topic_tag"]
            )
            if repos:
                return repos
            print("  ⚠️ No repos found, using fallback list")
            return discovery["fallback_list"]
        except Exception as e:
            print(f"  ⚠️ Discovery failed: {e}, using fallback list")
            return discovery["fallback_list"]
    
    def _collect_project(self, repo_full_name: str, bio_index: dict) -> dict:
        """Collect all data for a single project."""
        owner, repo = repo_full_name.split("/")
        slug = repo  # Use repo name as slug
        
        # Check if page exists in Bio Site
        bio_state = bio_index.get(slug, {
            "exists": False,
            "locked": False,
            "content": None,
            "manual_sections": [],
            "last_deploy": None
        })
        
        # If locked, skip collection
        if bio_state.get("locked", False):
            return {
                "slug": slug,
                "repo": repo_full_name,
                "exists": True,
                "locked": True,
                "status": "skip",
                "change_score": 0,
                "change_reason": "locked"
            }
        
        # Fetch data from GitHub
        lookback_days = self.config.get("collector", {}).get("commits_lookback_days", 30)
        excerpt_length = self.config.get("collector", {}).get("readme_excerpt_length", 500)
        
        try:
            repo_meta = self.gh.get_repo(owner, repo)
            commits = self.gh.get_commits(owner, repo, since_days=lookback_days)
            releases = self.gh.get_releases(owner, repo, limit=5)
            languages = self.gh.get_languages(owner, repo)
            readme = self.gh.get_readme(owner, repo)
        except Exception as e:
            print(f"    ⚠️ Failed to fetch data: {e}")
            return {
                "slug": slug,
                "repo": repo_full_name,
                "exists": bio_state["exists"],
                "locked": False,
                "status": "error",
                "change_score": 0,
                "change_reason": f"fetch_error: {str(e)}"
            }
        
        # Check if README changed since last deploy
        readme_changed = self._check_readme_changed(readme, bio_state)
        
        # Calculate significance score
        significance = self._calculate_significance(
            commits=commits,
            releases=releases,
            readme_changed=readme_changed,
            is_new=not bio_state["exists"]
        )
        
        # Force update if requested
        if self.force_update and significance["status"] == "skip":
            significance["status"] = "update"
            significance["change_reason"] = "force_update"
        
        return {
            "slug": slug,
            "repo": repo_full_name,
            "exists": bio_state["exists"],
            "locked": False,
            "status": significance["status"],
            "change_score": significance["change_score"],
            "change_reason": significance["change_reason"],
            "last_deploy": bio_state.get("last_deploy"),
            "commits": commits[:10],  # Limit for context size
            "languages": languages[:5],  # Top 5 languages
            "readme_excerpt": readme["content"][:excerpt_length] if readme["content"] else "",
            "readme_sha": readme.get("sha"),
            "releases": releases[:3],  # Last 3 releases
            "description": repo_meta.get("description", ""),
            "stars": repo_meta.get("stargazers_count", 0),
            "forks": repo_meta.get("forks_count", 0),
            "current_html": bio_state.get("content"),
            "manual_sections": bio_state.get("manual_sections", [])
        }
    
    def _check_readme_changed(self, readme: dict, bio_state: dict) -> bool:
        """Check if README has changed since last deployment."""
        if not bio_state.get("exists"):
            return True  # New project, treat as changed
        
        # Compare SHA if we stored it
        # For MVP, we'll compare content hash
        current_hash = hashlib.md5(
            (readme.get("content") or "").encode()
        ).hexdigest()[:8]
        
        # We'd need to store this hash in bio site, for now assume changed if content exists
        return bool(readme.get("content"))
    
    def _calculate_significance(
        self,
        commits: list,
        releases: list,
        readme_changed: bool,
        is_new: bool
    ) -> dict:
        """
        Calculate significance score using weighted heuristics.
        
        Scoring:
        - New release: +100
        - README changed: +40
        - feat/refactor commit: +30
        - fix commit: +15
        - docs/style/chore: +0
        - No commits: -999 (skip)
        """
        scoring = self.config["scoring"]
        score = 0
        reasons = []
        
        # New project always gets updated
        if is_new:
            return {
                "change_score": 999,
                "status": "new",
                "change_reason": "new_project"
            }
        
        # No recent activity
        if not commits and not releases:
            return {
                "change_score": scoring["no_commits"],
                "status": "skip",
                "change_reason": "no_activity"
            }
        
        # Check for new releases
        if releases:
            score += scoring["new_release"]
            reasons.append("release_tag")
        
        # Check README changes
        if readme_changed:
            score += scoring["readme_changed"]
            reasons.append("readme_changed")
        
        # Score commits by type
        for commit in commits:
            commit_type = commit.get("type", "other")
            if commit_type == "feat":
                score += scoring["feat_commit"]
                if "feature_commit" not in reasons:
                    reasons.append("feature_commit")
            elif commit_type == "refactor":
                score += scoring["refactor_commit"]
                if "refactor_commit" not in reasons:
                    reasons.append("refactor_commit")
            elif commit_type == "fix":
                score += scoring["fix_commit"]
                if "fix_commit" not in reasons:
                    reasons.append("fix_commit")
            # docs, style, chore, other = 0 points
        
        # Determine status based on threshold
        threshold = scoring["update_threshold"]
        status = "update" if score >= threshold else "skip"
        
        return {
            "change_score": score,
            "status": status,
            "change_reason": reasons[0] if reasons else "low_significance"
        }
    
    def _hash_config(self) -> str:
        """Generate hash of config for change detection."""
        config_str = json.dumps(self.config, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()[:8]
    
    def _save_context(self, context: dict):
        """Save context.json to _data directory."""
        output_dir = Path("_data")
        output_dir.mkdir(exist_ok=True)
        
        output_path = output_dir / "context.json"
        with open(output_path, "w") as f:
            json.dump(context, f, indent=2)
        
        print(f"\n📝 Saved context to {output_path}")
    
    def _set_output(self, name: str, value: str):
        """Set GitHub Actions output variable."""
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"{name}={value}\n")


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="SiteOps Collector")
    parser.add_argument(
        "--config", 
        default="config/settings.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without saving output"
    )
    args = parser.parse_args()
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    
    collector = Collector(args.config)
    context = collector.run()
    
    if args.dry_run:
        print("\n--- DRY RUN: Context would be ---")
        print(json.dumps(context, indent=2))


if __name__ == "__main__":
    main()
