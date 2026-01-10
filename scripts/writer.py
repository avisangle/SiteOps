"""
Phase 2: Writer Agent
Generates HTML drafts for project pages using Claude AI.
"""

import os
import sys
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
import anthropic
from jinja2 import Environment, FileSystemLoader

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class WriterAgent:
    """
    AI-powered content writer that generates project page HTML.
    
    Uses Claude to update project descriptions based on GitHub data,
    while preserving manually written sections.
    """
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        self.client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var
        self.jinja_env = Environment(
            loader=FileSystemLoader("prompts"),
            autoescape=False
        )
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
        
        # Track token usage for cost calculation
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
        """Process all projects from context.json and generate drafts."""
        print("✍️  Starting Writer Agent...")
        
        # Load context from Collector
        context = self._load_context()
        if not context:
            print("❌ No context.json found. Run Collector first.")
            return {"drafts": [], "usage": self.usage}
        
        # Filter to only projects needing updates
        projects_to_update = [
            p for p in context["projects"]
            if p["status"] in ("update", "new") and not p.get("locked", False)
        ]
        
        print(f"📝 {len(projects_to_update)} projects to generate drafts for")
        
        results = {
            "drafts": [],
            "errors": [],
            "usage": self.usage
        }
        
        for project in projects_to_update:
            print(f"\n  → Generating draft for {project['slug']}")
            
            try:
                draft = self._generate_draft(project)
                draft_path = self._save_draft(project["slug"], draft)
                
                results["drafts"].append({
                    "slug": project["slug"],
                    "path": str(draft_path),
                    "status": "success",
                    "is_new": project["status"] == "new"
                })
                print(f"    ✓ Draft saved to {draft_path}")
                
            except Exception as e:
                print(f"    ✗ Failed: {e}")
                results["errors"].append({
                    "slug": project["slug"],
                    "error": str(e)
                })
        
        # Save results for downstream phases
        self._save_results(results)
        
        print(f"\n✅ Writer complete!")
        print(f"   Drafts generated: {len(results['drafts'])}")
        print(f"   Errors: {len(results['errors'])}")
        print(f"   Tokens used: {self.usage['input_tokens']} in / {self.usage['output_tokens']} out")
        
        return results
    
    def _load_context(self) -> Optional[dict]:
        """Load context.json from Collector."""
        context_path = Path("_data/context.json")
        if not context_path.exists():
            return None
        
        with open(context_path, "r") as f:
            return json.load(f)
    
    def _generate_draft(self, project: dict) -> str:
        """Generate HTML draft using Claude."""
        # Build the prompt
        prompt = self._build_prompt(project)
        
        # Call Claude
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Track usage
        self.usage["input_tokens"] += response.usage.input_tokens
        self.usage["output_tokens"] += response.usage.output_tokens
        self.usage["requests"] += 1
        
        # Extract HTML from response
        draft = response.content[0].text
        
        # Clean up - sometimes Claude wraps in markdown code blocks
        draft = self._clean_html_response(draft)
        
        # Inject manual sections back
        if project.get("manual_sections"):
            draft = self._inject_manual_sections(draft, project["manual_sections"])
        
        # Add deployment marker
        draft = self._add_deploy_marker(draft)
        
        return draft
    
    def _build_prompt(self, project: dict) -> str:
        """Build the writer prompt using Jinja2 template."""
        template = self.jinja_env.get_template("writer.md")
        
        # If new project, use the base template as current_html
        current_html = project.get("current_html") or self._get_base_template()
        
        return template.render(
            project=project,
            current_html=current_html,
            policy=self.config["policy"]
        )
    
    def _get_base_template(self) -> str:
        """Load the base HTML template for new projects."""
        template_path = Path("templates/project_detail.html")
        if template_path.exists():
            with open(template_path, "r") as f:
                return f.read()
        
        # Fallback minimal template
        return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{PROJECT_NAME}} - Portfolio</title>
</head>
<body>
    <main>
        <h1>{{PROJECT_NAME}}</h1>
        <section id="summary">
            <!-- Project summary goes here -->
        </section>
        <section id="changelog">
            <!-- Recent updates go here -->
        </section>
        <section id="status-badge">
            <!-- Status badge goes here -->
        </section>
        <!-- MANUAL:custom -->
        <!-- Add custom content here -->
        <!-- /MANUAL:custom -->
    </main>
</body>
</html>"""
    
    def _clean_html_response(self, response: str) -> str:
        """Remove markdown code block wrappers if present."""
        # Remove ```html ... ``` wrapper
        response = re.sub(r'^```html?\s*\n', '', response, flags=re.MULTILINE)
        response = re.sub(r'\n```\s*$', '', response, flags=re.MULTILINE)
        return response.strip()
    
    def _inject_manual_sections(self, draft: str, manual_sections: list) -> str:
        """
        Re-inject preserved manual sections into the draft.
        
        Manual sections are marked with:
        <!-- MANUAL:section_name -->
        content
        <!-- /MANUAL:section_name -->
        """
        for section in manual_sections:
            # Extract section name
            match = re.search(r'<!-- MANUAL:(\w+) -->', section)
            if not match:
                continue
            
            section_name = match.group(1)
            
            # Find and replace the placeholder in draft
            placeholder_pattern = rf'<!-- MANUAL:{section_name} -->.*?<!-- /MANUAL:{section_name} -->'
            draft = re.sub(
                placeholder_pattern,
                section,
                draft,
                flags=re.DOTALL
            )
        
        return draft
    
    def _add_deploy_marker(self, html: str) -> str:
        """Add deployment timestamp marker to HTML."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        marker = f"<!-- DEPLOYED: {today} -->"
        
        # Add after <html> tag
        if "<html" in html:
            html = re.sub(
                r'(<html[^>]*>)',
                rf'\1\n{marker}',
                html,
                count=1
            )
        else:
            html = marker + "\n" + html
        
        return html
    
    def _save_draft(self, slug: str, content: str) -> Path:
        """Save draft HTML to drafts directory."""
        drafts_dir = Path("drafts")
        drafts_dir.mkdir(exist_ok=True)
        
        draft_path = drafts_dir / f"{slug}.html"
        
        if not self.dry_run:
            with open(draft_path, "w") as f:
                f.write(content)
        
        return draft_path
    
    def _save_results(self, results: dict):
        """Save writer results for downstream phases."""
        output_dir = Path("_data")
        output_dir.mkdir(exist_ok=True)
        
        output_path = output_dir / "writer_results.json"
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="SiteOps Writer Agent")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without saving drafts"
    )
    args = parser.parse_args()
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    
    writer = WriterAgent(args.config)
    results = writer.run()
    
    if args.dry_run:
        print("\n--- DRY RUN: Would have generated ---")
        for draft in results["drafts"]:
            print(f"  - {draft['slug']}")


if __name__ == "__main__":
    main()
