# SiteOps

Automated bio site content update system powered by GitHub Actions and Claude AI.

## Overview

SiteOps treats content updates like a software release cycle:
**Collect → Write (AI) → Edit (AI) → Deploy → Observe**

## Quick Start

1. **Configure secrets** in your GitHub repo settings:
   - `ANTHROPIC_API_KEY` - Claude API key
   - `BIO_SITE_PAT` - Personal Access Token with `repo` scope

2. **Tag your projects** with the `portfolio-project` topic on GitHub

3. **Update `config/settings.yaml`** with your bio site repo name

4. **Run manually** via Actions tab or wait for scheduled run

## Configuration

See [`config/settings.yaml`](config/settings.yaml) for all options:
- `discovery.topic_tag` - GitHub topic to discover projects
- `workflow.mode` - `"auto"` (direct push) or `"manual"` (create PR)
- `scoring.*` - Significance weights for different change types
- `policy.*` - Content rules for the Editor AI

## Workflow Modes

| Mode | Behavior |
|------|----------|
| `manual` | Creates PR for all updates → You review & merge |
| `auto` | Direct push if Editor approves, PR if flagged |

## Architecture

```
Collector → context.json → Writer AI → drafts/ → Editor AI → Deployer → Bio Site
                                                      ↓
                                              Observer → logs/
```

## License

MIT
