"""
Phase 4: Deployer
Handles the gatekeeper logic and deploys approved changes.
"""

import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import yaml
from github import Github, GithubException, Auth

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class Deployer:
    """
    Gatekeeper that deploys approved changes to Bio Site.
    
    Logic:
    - mode: auto + APPROVE → Direct push to main
    - mode: manual OR FLAGGED → Create Pull Request
    - REJECT → Skip (logged)
    - High risk changes (>30% diff) → Force PR even in auto mode
    """
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        
        # GitHub client for Bio Site repo (using new Auth pattern)
        bio_site_pat = os.environ.get("BIO_SITE_PAT") or os.environ.get("GITHUB_TOKEN")
        auth = Auth.Token(bio_site_pat)
        self.gh = Github(auth=auth)
        
        target = self.config["target"]
        self.target_repo = self.gh.get_repo(target["repo"])
        self.target_branch = target["branch"]
        self.output_dir = target["output_dir"]
        
        # Workflow settings
        workflow = self.config["workflow"]
        self.mode = workflow["mode"]
        self.force_pr_on_high_risk = workflow.get("force_pr_on_high_risk", True)
        self.high_risk_threshold = workflow.get("high_risk_threshold", 30)
        
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    
    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            return yaml.safe_load(f)
    
    def run(self) -> dict:
        """Deploy all approved/flagged drafts."""
        print("🚀 Starting Deployer...")
        
        # Load editor results
        editor_results = self._load_editor_results()
        if not editor_results:
            print("❌ No editor results found. Run Editor first.")
            return {"pushed": [], "prs": [], "skipped": []}
        
        results = {
            "pushed": [],
            "prs": [],
            "skipped": [],
            "errors": []
        }
        
        for verdict in editor_results.get("verdicts", []):
            slug = verdict["slug"]
            status = verdict.get("status", "ERROR")
            
            print(f"\n  → Processing {slug} (Editor: {status})")
            
            # Skip rejected and errored drafts
            if status in ("REJECT", "ERROR"):
                print(f"    ⊘ Skipped: {verdict.get('reason', 'Unknown')}")
                results["skipped"].append({
                    "slug": slug,
                    "reason": verdict.get("reason", "Rejected by Editor")
                })
                continue
            
            # Load the draft
            draft_path = Path(f"drafts/{slug}.html")
            if not draft_path.exists():
                print(f"    ✗ Draft file not found")
                results["errors"].append({"slug": slug, "error": "Draft not found"})
                continue
            
            with open(draft_path, "r") as f:
                draft_content = f.read()
            
            # Check if bio site was modified since our Collector ran
            freshness = self._check_freshness(slug, verdict)
            if freshness["stale"]:
                print(f"    ⚠️  Bio site modified externally since run started")
                print(f"       Expected SHA: {freshness['expected_sha'][:7] if freshness['expected_sha'] else 'N/A'}")
                print(f"       Current SHA:  {freshness['current_sha'][:7] if freshness['current_sha'] else 'N/A'}")
                # Force PR creation to avoid conflict
                should_pr = True
                verdict["_conflict_detected"] = True
            else:
                # Determine deployment method normally
                should_pr = self._should_create_pr(status, verdict)
            
            try:
                if should_pr:
                    pr_url = self._create_pull_request(slug, draft_content, verdict)
                    print(f"    📝 PR created: {pr_url}")
                    results["prs"].append({
                        "slug": slug,
                        "url": pr_url,
                        "reason": "manual mode" if self.mode == "manual" else status
                    })
                else:
                    self._direct_push(slug, draft_content)
                    print(f"    ✓ Pushed to {self.target_branch}")
                    results["pushed"].append({"slug": slug})
                    
            except Exception as e:
                print(f"    ✗ Deploy failed: {e}")
                results["errors"].append({"slug": slug, "error": str(e)})
        
        # Save results
        self._save_results(results)
        
        print(f"\n✅ Deployer complete!")
        print(f"   Pushed: {len(results['pushed'])}")
        print(f"   PRs created: {len(results['prs'])}")
        print(f"   Skipped: {len(results['skipped'])}")
        print(f"   Errors: {len(results['errors'])}")
        
        return results
    
    def _load_editor_results(self) -> Optional[dict]:
        """Load editor results."""
        results_path = Path("_data/editor_results.json")
        if not results_path.exists():
            return None
        with open(results_path, "r") as f:
            return json.load(f)
    
    def _load_context(self) -> Optional[dict]:
        """Load collector context to get original SHAs."""
        context_path = Path("_data/context.json")
        if not context_path.exists():
            return None
        with open(context_path, "r") as f:
            return json.load(f)
    
    def _check_freshness(self, slug: str, verdict: dict) -> dict:
        """
        Check if the bio site file was modified since our Collector ran.
        
        This prevents overwriting manual changes made during our run.
        If stale, we'll force a PR to surface the conflict for human review.
        """
        result = {
            "stale": False,
            "expected_sha": None,
            "current_sha": None
        }
        
        # Load the context to get the SHA we read at collection time
        context = self._load_context()
        if not context:
            return result
        
        # Find the project in context
        project = None
        for p in context.get("projects", []):
            if p["slug"] == slug:
                project = p
                break
        
        if not project or not project.get("exists", False):
            # New file, no conflict possible
            return result
        
        # Get the SHA we had at collection time (from current_html hash)
        if project.get("current_html"):
            result["expected_sha"] = hashlib.sha1(
                project["current_html"].encode()
            ).hexdigest()
        
        # Get the current SHA from GitHub
        file_path = f"{self.output_dir}{slug}.html"
        try:
            current_file = self.target_repo.get_contents(file_path, ref=self.target_branch)
            result["current_sha"] = current_file.sha
            
            # Also compute content hash for comparison
            current_content = current_file.decoded_content.decode('utf-8')
            current_hash = hashlib.sha1(current_content.encode()).hexdigest()
            
            # Compare hashes
            if result["expected_sha"] and current_hash != result["expected_sha"]:
                result["stale"] = True
                
        except GithubException as e:
            if e.status == 404:
                # File was deleted, that's a conflict
                if project.get("exists", False):
                    result["stale"] = True
        
        return result
    
    def _should_create_pr(self, status: str, verdict: dict) -> bool:
        """Determine if we should create a PR instead of direct push."""
        # Manual mode always creates PR
        if self.mode == "manual":
            return True
        
        # FLAGGED status always creates PR
        if status == "FLAGGED":
            return True
        
        # High risk changes force PR
        if self.force_pr_on_high_risk:
            change_pct = verdict.get("change_percentage", 0)
            if change_pct > self.high_risk_threshold:
                return True
        
        # Auto mode + APPROVE = direct push
        return False
    
    def _create_pull_request(self, slug: str, content: str, verdict: dict) -> str:
        """Create a PR with the draft changes."""
        if self.dry_run:
            return "https://github.com/dry-run/pr"
        
        # Create branch name
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        branch_name = f"siteops/update-{slug}-{date_str}"
        
        # Get the base branch ref
        base_ref = self.target_repo.get_git_ref(f"heads/{self.target_branch}")
        base_sha = base_ref.object.sha
        
        # Create new branch
        try:
            self.target_repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=base_sha
            )
        except GithubException as e:
            if e.status == 422:  # Branch already exists
                # Delete and recreate
                try:
                    existing_ref = self.target_repo.get_git_ref(f"heads/{branch_name}")
                    existing_ref.delete()
                except:
                    pass
                self.target_repo.create_git_ref(
                    ref=f"refs/heads/{branch_name}",
                    sha=base_sha
                )
            else:
                raise
        
        # Create/update the file
        file_path = f"{self.output_dir}{slug}.html"
        
        try:
            # Check if file exists
            existing = self.target_repo.get_contents(file_path, ref=branch_name)
            self.target_repo.update_file(
                path=file_path,
                message=f"Update {slug} project page [SiteOps]",
                content=content,
                sha=existing.sha,
                branch=branch_name
            )
        except GithubException as e:
            if e.status == 404:
                # File doesn't exist, create it
                self.target_repo.create_file(
                    path=file_path,
                    message=f"Add {slug} project page [SiteOps]",
                    content=content,
                    branch=branch_name
                )
            else:
                raise
        
        # Create PR
        pr_body = self._build_pr_body(slug, verdict)
        
        pr = self.target_repo.create_pull(
            title=f"🤖 SiteOps: Update {slug}",
            body=pr_body,
            head=branch_name,
            base=self.target_branch
        )
        
        # Add labels if available
        try:
            pr.add_to_labels("automated", "siteops")
        except:
            pass  # Labels might not exist
        
        return pr.html_url
    
    def _build_pr_body(self, slug: str, verdict: dict) -> str:
        """Build the PR description."""
        status = verdict.get("status", "UNKNOWN")
        reason = verdict.get("reason", "N/A")
        diff_summary = verdict.get("diff_summary", "N/A")
        issues = verdict.get("issues", [])
        
        body = f"""## 🤖 AI-Generated Update: {slug}

**Editor Verdict**: {status}
**Reason**: {reason}

### Diff Summary
{diff_summary}

"""
        
        if issues:
            body += "### Issues Noted\n"
            for issue in issues:
                body += f"- {issue}\n"
            body += "\n"
        
        body += """---

**Action Required**: Review the "Files Changed" tab and merge to deploy.

> This PR was automatically generated by [SiteOps](https://github.com/your-username/siteops).
> To disable automation, set `workflow.mode: "manual"` in settings.yaml.
"""
        
        return body
    
    def _direct_push(self, slug: str, content: str):
        """Push changes directly to main branch."""
        if self.dry_run:
            return
        
        file_path = f"{self.output_dir}{slug}.html"
        
        try:
            # Check if file exists
            existing = self.target_repo.get_contents(file_path, ref=self.target_branch)
            self.target_repo.update_file(
                path=file_path,
                message=f"Update {slug} project page [SiteOps]",
                content=content,
                sha=existing.sha,
                branch=self.target_branch
            )
        except GithubException as e:
            if e.status == 404:
                # File doesn't exist, create it
                self.target_repo.create_file(
                    path=file_path,
                    message=f"Add {slug} project page [SiteOps]",
                    content=content,
                    branch=self.target_branch
                )
            else:
                raise
    
    def _save_results(self, results: dict):
        """Save deployer results."""
        output_path = Path("_data/deployer_results.json")
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="SiteOps Deployer")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without making changes"
    )
    args = parser.parse_args()
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    
    deployer = Deployer(args.config)
    results = deployer.run()


if __name__ == "__main__":
    main()
