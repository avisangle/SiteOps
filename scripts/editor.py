"""
Phase 3: Editor Agent
Reviews AI-generated drafts against policy and source of truth.
"""

import os
import sys
import json
import re
from pathlib import Path
from typing import Optional
from difflib import unified_diff

import yaml
import anthropic
from jinja2 import Environment, FileSystemLoader

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class EditorAgent:
    """
    AI-powered reviewer that validates drafts against:
    1. Source of truth (GitHub data)
    2. Content policy (forbidden words, tone, length)
    3. HTML structure integrity
    
    Outputs: reviews/{slug}_verdict.json
    """
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        self.client = anthropic.Anthropic()
        self.jinja_env = Environment(
            loader=FileSystemLoader("prompts"),
            autoescape=False
        )
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        
        # Track token usage
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 0
        }
    
    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            return yaml.safe_load(f)
    
    def run(self) -> dict:
        """Review all drafts from Writer Agent."""
        print("🔍 Starting Editor Agent...")
        
        # Load context and writer results
        context = self._load_context()
        writer_results = self._load_writer_results()
        
        if not writer_results or not writer_results.get("drafts"):
            print("❌ No drafts to review. Run Writer first.")
            return {"verdicts": [], "usage": self.usage}
        
        print(f"📋 {len(writer_results['drafts'])} drafts to review")
        
        results = {
            "verdicts": [],
            "approved": 0,
            "flagged": 0,
            "rejected": 0,
            "usage": self.usage
        }
        
        for draft_info in writer_results["drafts"]:
            slug = draft_info["slug"]
            print(f"\n  → Reviewing {slug}")
            
            # Load the draft
            draft_content = self._load_draft(draft_info["path"])
            if not draft_content:
                print(f"    ✗ Draft file not found")
                continue
            
            # Find matching project context
            project_context = self._find_project_context(context, slug)
            if not project_context:
                print(f"    ✗ No context found for {slug}")
                continue
            
            # Run review
            try:
                verdict = self._review_draft(
                    draft_content,
                    project_context.get("current_html", ""),
                    project_context
                )
                
                # Supplement with deterministic checks
                verdict = self._add_deterministic_checks(verdict, draft_content)
                
                # Save verdict
                verdict_path = self._save_verdict(slug, verdict)
                
                results["verdicts"].append({
                    "slug": slug,
                    "path": str(verdict_path),
                    **verdict
                })
                
                # Update counts
                status = verdict["status"]
                if status == "APPROVE":
                    results["approved"] += 1
                elif status == "FLAGGED":
                    results["flagged"] += 1
                else:
                    results["rejected"] += 1
                
                print(f"    {self._status_icon(status)} {status}: {verdict['reason']}")
                
            except Exception as e:
                print(f"    ✗ Review failed: {e}")
                results["verdicts"].append({
                    "slug": slug,
                    "status": "ERROR",
                    "reason": str(e)
                })
        
        # Save results
        self._save_results(results)
        
        print(f"\n✅ Editor complete!")
        print(f"   Approved: {results['approved']}")
        print(f"   Flagged: {results['flagged']}")
        print(f"   Rejected: {results['rejected']}")
        
        return results
    
    def _load_context(self) -> Optional[dict]:
        """Load context.json from Collector."""
        context_path = Path("_data/context.json")
        if not context_path.exists():
            return None
        with open(context_path, "r") as f:
            return json.load(f)
    
    def _load_writer_results(self) -> Optional[dict]:
        """Load writer results."""
        results_path = Path("_data/writer_results.json")
        if not results_path.exists():
            return None
        with open(results_path, "r") as f:
            return json.load(f)
    
    def _load_draft(self, path: str) -> Optional[str]:
        """Load draft content from file."""
        draft_path = Path(path)
        if not draft_path.exists():
            return None
        with open(draft_path, "r") as f:
            return f.read()
    
    def _find_project_context(self, context: dict, slug: str) -> Optional[dict]:
        """Find project data in context by slug."""
        if not context:
            return None
        for project in context.get("projects", []):
            if project["slug"] == slug:
                return project
        return None
    
    def _review_draft(self, draft: str, current: str, context: dict) -> dict:
        """Run AI review on draft."""
        prompt = self._build_prompt(draft, current, context)
        
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Track usage
        self.usage["input_tokens"] += response.usage.input_tokens
        self.usage["output_tokens"] += response.usage.output_tokens
        self.usage["requests"] += 1
        
        # Parse JSON response
        response_text = response.content[0].text
        
        # Clean up markdown code blocks if present
        response_text = re.sub(r'^```json?\s*\n', '', response_text, flags=re.MULTILINE)
        response_text = re.sub(r'\n```\s*$', '', response_text, flags=re.MULTILINE)
        
        try:
            verdict = json.loads(response_text)
        except json.JSONDecodeError:
            # If parsing fails, flag for human review
            verdict = {
                "status": "FLAGGED",
                "reason": "Failed to parse AI review response",
                "issues": ["AI response was not valid JSON"],
                "diff_summary": "Unknown",
                "change_percentage": 0
            }
        
        return verdict
    
    def _build_prompt(self, draft: str, current: str, context: dict) -> str:
        """Build the editor review prompt."""
        template = self.jinja_env.get_template("editor.md")
        
        # Create a clean context for the prompt (remove large HTML)
        clean_context = {k: v for k, v in context.items() if k != "current_html"}
        
        return template.render(
            draft_html=draft,
            current_html=current or "(No existing page)",
            context=clean_context,
            policy=self.config["policy"]
        )
    
    def _add_deterministic_checks(self, verdict: dict, draft: str) -> dict:
        """Add deterministic policy checks to AI verdict."""
        issues = verdict.get("issues", [])
        policy = self.config["policy"]
        
        # Check 1: Forbidden words
        draft_lower = draft.lower()
        found_forbidden = []
        for word in policy["forbidden_words"]:
            if word.lower() in draft_lower:
                found_forbidden.append(word)
        
        if found_forbidden:
            issues.append(f"Forbidden words found: {', '.join(found_forbidden)}")
            if verdict["status"] == "APPROVE":
                verdict["status"] = "FLAGGED"
                verdict["reason"] = f"Contains forbidden words: {', '.join(found_forbidden)}"
        
        # Check 2: HTML validity (basic)
        if not self._is_valid_html(draft):
            issues.append("HTML structure appears invalid")
            verdict["status"] = "REJECT"
            verdict["reason"] = "Invalid HTML structure"
        
        # Check 3: Required sections
        for section in policy.get("required_sections", []):
            if f'id="{section}"' not in draft and f"id='{section}'" not in draft:
                issues.append(f"Missing required section: {section}")
        
        verdict["issues"] = issues
        return verdict
    
    def _is_valid_html(self, html: str) -> bool:
        """Basic HTML validity check."""
        # Check for matching html/head/body tags
        has_html = "<html" in html.lower() and "</html>" in html.lower()
        has_body = "<body" in html.lower() and "</body>" in html.lower()
        
        # Check for unclosed tags (basic)
        open_tags = len(re.findall(r'<[a-z]', html.lower()))
        close_tags = len(re.findall(r'</[a-z]', html.lower()))
        
        # Allow some flexibility (self-closing tags, etc.)
        return has_html and has_body and abs(open_tags - close_tags) < 10
    
    def _status_icon(self, status: str) -> str:
        """Get icon for status."""
        icons = {
            "APPROVE": "✓",
            "FLAGGED": "⚠",
            "REJECT": "✗",
            "ERROR": "💥"
        }
        return icons.get(status, "?")
    
    def _save_verdict(self, slug: str, verdict: dict) -> Path:
        """Save verdict to reviews directory."""
        reviews_dir = Path("reviews")
        reviews_dir.mkdir(exist_ok=True)
        
        verdict_path = reviews_dir / f"{slug}_verdict.json"
        
        if not self.dry_run:
            with open(verdict_path, "w") as f:
                json.dump(verdict, f, indent=2)
        
        return verdict_path
    
    def _save_results(self, results: dict):
        """Save editor results for downstream phases."""
        output_path = Path("_data/editor_results.json")
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="SiteOps Editor Agent")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without saving verdicts"
    )
    args = parser.parse_args()
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    
    editor = EditorAgent(args.config)
    results = editor.run()


if __name__ == "__main__":
    main()
