# Weekly Research Digest

Automated weekly paper + repo monitor for Yan Leng's research.

## What it does

Every Sunday, searches for new papers across:
- **General science:** PNAS, Nature, Science, Nature Communications, Science Advances
- **Behavior & AI:** Nature Human Behaviour, Nature Machine Intelligence
- **Management/IS:** Management Science, Marketing Science, ISR, MISQ, JMR, MSOM
- **ML conferences:** ICML, ICLR, NeurIPS
- **Preprints:** arXiv (cs.MA, cs.CL, cs.AI, cs.SI, cs.LG, econ.EM, stat.ML)
- **GitHub:** Trending repos in Claude, Codex, LLM agents, AI productivity

Each paper is scored against active research projects and presented in a scannable digest.

## Quick start

```bash
# Generate this week's digest
python generate_digest.py

# Custom date and lookback
python generate_digest.py --date 2026-04-20 --days 14

# Skip GitHub (faster)
python generate_digest.py --no-github
```

No API keys required. Set `GITHUB_TOKEN` env var for higher GitHub rate limits.

## Running with Codex

```
codex "cd /path/to/weekly-research-digest && python generate_digest.py"
```

## Output

Digests are written to `digests/digest-YYYY-MM-DD.md` and optionally copied to
the local monitoring directory.

## Automation

A Claude Code scheduled trigger runs every Sunday at 9am ET, generating the
digest via web search (richer results than API-only) and committing to this repo.
