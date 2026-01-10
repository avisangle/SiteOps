"""
Phase 5: Observer
Centralized logging, cost tracking, and dashboard updates.
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class Observer:
    """
    Observability layer that:
    1. Aggregates results from all phases
    2. Calculates costs
    3. Generates summary reports
    4. Updates the dashboard
    5. Creates failure alerts if needed
    """
    
    # Claude Sonnet pricing (as of 2024)
    PRICE_INPUT_PER_1K = 0.003   # $3 per million input tokens
    PRICE_OUTPUT_PER_1K = 0.015  # $15 per million output tokens
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config = self._load_config(config_path)
        self.run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        self.dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    
    def _load_config(self, path: str) -> dict:
        """Load configuration from YAML file."""
        with open(path, "r") as f:
            return yaml.safe_load(f)
    
    def run(self) -> dict:
        """Aggregate all phase results and generate reports."""
        print("📊 Starting Observer...")
        
        # Load results from all phases
        context = self._load_json("_data/context.json")
        writer_results = self._load_json("_data/writer_results.json")
        editor_results = self._load_json("_data/editor_results.json")
        deployer_results = self._load_json("_data/deployer_results.json")
        
        # Build run log
        run_log = {
            "run_id": self.run_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "dry_run": self.dry_run,
            "phases": {
                "collector": self._summarize_collector(context),
                "writer": self._summarize_writer(writer_results),
                "editor": self._summarize_editor(editor_results),
                "deployer": self._summarize_deployer(deployer_results)
            },
            "cost": self._calculate_cost(writer_results, editor_results),
            "success": self._determine_success(deployer_results)
        }
        
        # Save run log
        self._save_run_log(run_log)
        
        # Generate summary report
        self._generate_summary_report(run_log, context, deployer_results)
        
        # Update dashboard
        self._update_dashboard(run_log)
        
        print(f"\n✅ Observer complete!")
        print(f"   Run ID: {self.run_id}")
        print(f"   Total cost: {run_log['cost']['total_formatted']}")
        print(f"   Status: {'Success' if run_log['success'] else 'Partial/Failed'}")
        
        return run_log
    
    def _load_json(self, path: str) -> Optional[dict]:
        """Load JSON file if exists."""
        file_path = Path(path)
        if not file_path.exists():
            return None
        with open(file_path, "r") as f:
            return json.load(f)
    
    def _summarize_collector(self, context: Optional[dict]) -> dict:
        """Summarize collector results."""
        if not context:
            return {"status": "not_run", "projects": 0}
        
        summary = context.get("summary", {})
        return {
            "status": "success",
            "total_projects": summary.get("total", 0),
            "updates": summary.get("updates", 0),
            "new": summary.get("new", 0),
            "skipped": summary.get("skips", 0),
            "locked": summary.get("locked", 0)
        }
    
    def _summarize_writer(self, results: Optional[dict]) -> dict:
        """Summarize writer results."""
        if not results:
            return {"status": "not_run", "drafts": 0}
        
        return {
            "status": "success" if not results.get("errors") else "partial",
            "drafts": len(results.get("drafts", [])),
            "errors": len(results.get("errors", [])),
            "tokens_in": results.get("usage", {}).get("input_tokens", 0),
            "tokens_out": results.get("usage", {}).get("output_tokens", 0)
        }
    
    def _summarize_editor(self, results: Optional[dict]) -> dict:
        """Summarize editor results."""
        if not results:
            return {"status": "not_run", "reviewed": 0}
        
        return {
            "status": "success",
            "reviewed": len(results.get("verdicts", [])),
            "approved": results.get("approved", 0),
            "flagged": results.get("flagged", 0),
            "rejected": results.get("rejected", 0),
            "tokens_in": results.get("usage", {}).get("input_tokens", 0),
            "tokens_out": results.get("usage", {}).get("output_tokens", 0)
        }
    
    def _summarize_deployer(self, results: Optional[dict]) -> dict:
        """Summarize deployer results."""
        if not results:
            return {"status": "not_run", "deployed": 0}
        
        return {
            "status": "success" if not results.get("errors") else "partial",
            "pushed": len(results.get("pushed", [])),
            "prs_created": len(results.get("prs", [])),
            "skipped": len(results.get("skipped", [])),
            "errors": len(results.get("errors", []))
        }
    
    def _calculate_cost(
        self,
        writer_results: Optional[dict],
        editor_results: Optional[dict]
    ) -> dict:
        """Calculate total API cost."""
        total_input = 0
        total_output = 0
        
        if writer_results:
            usage = writer_results.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
        
        if editor_results:
            usage = editor_results.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
        
        input_cost = (total_input / 1000) * self.PRICE_INPUT_PER_1K
        output_cost = (total_output / 1000) * self.PRICE_OUTPUT_PER_1K
        total_cost = input_cost + output_cost
        
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "input_cost": round(input_cost, 4),
            "output_cost": round(output_cost, 4),
            "total": round(total_cost, 4),
            "total_formatted": f"${total_cost:.4f}"
        }
    
    def _determine_success(self, deployer_results: Optional[dict]) -> bool:
        """Determine if the run was successful."""
        if not deployer_results:
            return False
        
        # Success if we deployed something and had no errors
        pushed = len(deployer_results.get("pushed", []))
        prs = len(deployer_results.get("prs", []))
        errors = len(deployer_results.get("errors", []))
        
        return (pushed > 0 or prs > 0) and errors == 0
    
    def _save_run_log(self, run_log: dict):
        """Save detailed run log."""
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        
        log_path = logs_dir / f"run-{self.run_id}.json"
        with open(log_path, "w") as f:
            json.dump(run_log, f, indent=2)
        
        print(f"\n📝 Run log saved to {log_path}")
    
    def _generate_summary_report(
        self,
        run_log: dict,
        context: Optional[dict],
        deployer_results: Optional[dict]
    ):
        """Generate human-readable summary report."""
        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        
        report_path = reports_dir / f"summary-{self.run_id}.md"
        
        # Build report content
        phases = run_log["phases"]
        cost = run_log["cost"]
        
        report = f"""# SiteOps Run Summary

**Run ID**: {self.run_id}
**Timestamp**: {run_log['timestamp']}
**Status**: {'✅ Success' if run_log['success'] else '⚠️ Partial/Failed'}
**Dry Run**: {'Yes' if run_log['dry_run'] else 'No'}

## Phase Summary

| Phase | Status | Details |
|-------|--------|---------|
| Collector | {phases['collector'].get('status', 'N/A')} | {phases['collector'].get('total_projects', 0)} projects, {phases['collector'].get('updates', 0)} updates |
| Writer | {phases['writer'].get('status', 'N/A')} | {phases['writer'].get('drafts', 0)} drafts generated |
| Editor | {phases['editor'].get('status', 'N/A')} | {phases['editor'].get('approved', 0)} approved, {phases['editor'].get('flagged', 0)} flagged, {phases['editor'].get('rejected', 0)} rejected |
| Deployer | {phases['deployer'].get('status', 'N/A')} | {phases['deployer'].get('pushed', 0)} pushed, {phases['deployer'].get('prs_created', 0)} PRs |

## Cost Breakdown

| Metric | Value |
|--------|-------|
| Input Tokens | {cost['input_tokens']:,} |
| Output Tokens | {cost['output_tokens']:,} |
| **Total Cost** | **{cost['total_formatted']}** |

"""
        
        # Add deployment details
        if deployer_results:
            if deployer_results.get("pushed"):
                report += "## Direct Pushes\n"
                for item in deployer_results["pushed"]:
                    report += f"- ✅ {item['slug']}\n"
                report += "\n"
            
            if deployer_results.get("prs"):
                report += "## Pull Requests Created\n"
                for item in deployer_results["prs"]:
                    report += f"- 📝 [{item['slug']}]({item['url']})\n"
                report += "\n"
            
            if deployer_results.get("skipped"):
                report += "## Skipped\n"
                for item in deployer_results["skipped"]:
                    report += f"- ⊘ {item['slug']}: {item['reason']}\n"
                report += "\n"
            
            if deployer_results.get("errors"):
                report += "## Errors\n"
                for item in deployer_results["errors"]:
                    report += f"- ❌ {item['slug']}: {item['error']}\n"
                report += "\n"
        
        report += "---\n*Generated by SiteOps Observer*\n"
        
        with open(report_path, "w") as f:
            f.write(report)
        
        print(f"📄 Summary report saved to {report_path}")
    
    def _update_dashboard(self, run_log: dict):
        """Update the cumulative dashboard."""
        dashboard_path = Path("dashboard.json")
        
        # Load or initialize dashboard
        if dashboard_path.exists():
            with open(dashboard_path, "r") as f:
                dashboard = json.load(f)
        else:
            dashboard = {
                "total_runs": 0,
                "successful_runs": 0,
                "failed_runs": 0,
                "total_projects_updated": 0,
                "total_prs_created": 0,
                "total_direct_pushes": 0,
                "total_cost_usd": 0.0,
                "last_run": None,
                "runs": []
            }
        
        # Update stats
        dashboard["total_runs"] += 1
        if run_log["success"]:
            dashboard["successful_runs"] += 1
        else:
            dashboard["failed_runs"] += 1
        
        deployer = run_log["phases"]["deployer"]
        dashboard["total_projects_updated"] += deployer.get("pushed", 0) + deployer.get("prs_created", 0)
        dashboard["total_prs_created"] += deployer.get("prs_created", 0)
        dashboard["total_direct_pushes"] += deployer.get("pushed", 0)
        dashboard["total_cost_usd"] += run_log["cost"]["total"]
        dashboard["last_run"] = run_log["timestamp"]
        
        # Add run summary (keep last 20)
        dashboard["runs"].insert(0, {
            "run_id": run_log["run_id"],
            "timestamp": run_log["timestamp"],
            "success": run_log["success"],
            "cost": run_log["cost"]["total_formatted"],
            "updates": deployer.get("pushed", 0) + deployer.get("prs_created", 0)
        })
        dashboard["runs"] = dashboard["runs"][:20]
        
        # Format totals
        dashboard["total_cost_formatted"] = f"${dashboard['total_cost_usd']:.2f}"
        
        # Save
        with open(dashboard_path, "w") as f:
            json.dump(dashboard, f, indent=2)
        
        print(f"📊 Dashboard updated")


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="SiteOps Observer")
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to config file"
    )
    args = parser.parse_args()
    
    observer = Observer(args.config)
    run_log = observer.run()


if __name__ == "__main__":
    main()
