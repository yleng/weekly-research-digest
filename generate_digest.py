#!/usr/bin/env python3
"""
Weekly Research Digest Generator for Yan Leng.

Searches arXiv, Semantic Scholar, and GitHub for papers and repos
relevant to active research projects. Outputs a readable Markdown digest.

Usage:
    python generate_digest.py                  # Generate this week's digest
    python generate_digest.py --date 2026-04-20  # Generate for a specific date
    python generate_digest.py --days 14          # Look back 14 days instead of 7

Works standalone (no API keys required) or with optional GITHUB_TOKEN for
higher rate limits.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

CFG = load_config()

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def fetch_json(url: str, headers: dict | None = None, retries: int = 3) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt == retries - 1:
                print(f"  [WARN] Failed to fetch {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None


def fetch_xml(url: str, retries: int = 3) -> ET.Element | None:
    req = urllib.request.Request(url)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return ET.fromstring(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt == retries - 1:
                print(f"  [WARN] Failed to fetch {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None

# ---------------------------------------------------------------------------
# arXiv search
# ---------------------------------------------------------------------------

ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

def search_arxiv(query: str, categories: list[str], max_results: int = 30,
                 days_back: int = 7) -> list[dict]:
    """Search arXiv API for recent papers."""
    cat_filter = "+OR+".join(f"cat:{c}" for c in categories)
    encoded_q = urllib.parse.quote(query)
    url = (
        f"http://export.arxiv.org/api/query?"
        f"search_query=({encoded_q})+AND+({cat_filter})"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )
    root = fetch_xml(url)
    if root is None:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=days_back + 3)  # buffer
    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        published = entry.findtext("atom:published", "", ARXIV_NS)
        try:
            pub_date = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if pub_date < cutoff:
            continue

        arxiv_id = entry.findtext("atom:id", "", ARXIV_NS).split("/abs/")[-1]
        title = re.sub(r"\s+", " ", entry.findtext("atom:title", "", ARXIV_NS)).strip()
        summary = re.sub(r"\s+", " ", entry.findtext("atom:summary", "", ARXIV_NS)).strip()
        authors = [a.findtext("atom:name", "", ARXIV_NS)
                    for a in entry.findall("atom:author", ARXIV_NS)]
        categories_found = [c.get("term", "")
                            for c in entry.findall("atom:category", ARXIV_NS)]

        papers.append({
            "source": "arXiv",
            "id": arxiv_id,
            "title": title,
            "authors": authors[:5],
            "date": pub_date.strftime("%Y-%m-%d"),
            "summary": summary[:500],
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "categories": categories_found,
        })
    return papers

# ---------------------------------------------------------------------------
# Semantic Scholar search
# ---------------------------------------------------------------------------

def search_semantic_scholar(query: str, max_results: int = 10,
                            year_range: str | None = None) -> list[dict]:
    """Search Semantic Scholar for recent papers from specific venues."""
    params = {
        "query": query,
        "limit": str(max_results),
        "fields": "title,authors,year,venue,externalIds,abstract,citationCount,url,publicationDate",
    }
    if year_range:
        params["year"] = year_range
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    data = fetch_json(url)
    if not data or "data" not in data:
        return []

    papers = []
    for p in data["data"]:
        if not p.get("title"):
            continue
        authors = [a.get("name", "") for a in (p.get("authors") or [])[:5]]
        doi = (p.get("externalIds") or {}).get("DOI", "")
        papers.append({
            "source": "Semantic Scholar",
            "title": p["title"],
            "authors": authors,
            "venue": p.get("venue", ""),
            "year": p.get("year", ""),
            "date": p.get("publicationDate", ""),
            "citations": p.get("citationCount", 0),
            "summary": (p.get("abstract") or "")[:500],
            "url": p.get("url", ""),
            "doi": doi,
        })
    return papers

# ---------------------------------------------------------------------------
# GitHub trending
# ---------------------------------------------------------------------------

def search_github_repos(topics: list[str], days_back: int = 7,
                        max_results: int = 20) -> list[dict]:
    """Search GitHub for recently created/updated repos."""
    since = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    all_repos: dict[str, dict] = {}

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"

    for topic in topics:
        q = urllib.parse.quote(f"{topic} pushed:>{since}")
        url = (
            f"https://api.github.com/search/repositories?"
            f"q={q}&sort=stars&order=desc&per_page=10"
        )
        data = fetch_json(url, headers=headers)
        if not data or "items" not in data:
            continue
        for r in data["items"]:
            name = r["full_name"]
            if name not in all_repos or r["stargazers_count"] > all_repos[name].get("stars", 0):
                all_repos[name] = {
                    "name": name,
                    "description": (r.get("description") or "")[:200],
                    "stars": r["stargazers_count"],
                    "url": r["html_url"],
                    "language": r.get("language", ""),
                    "updated": r.get("pushed_at", "")[:10],
                    "topics": r.get("topics", []),
                }
        time.sleep(1)  # rate limit courtesy

    repos = sorted(all_repos.values(), key=lambda x: x["stars"], reverse=True)
    return repos[:max_results]

# ---------------------------------------------------------------------------
# Relevance scoring (keyword-based, fast)
# ---------------------------------------------------------------------------

THEME_KEYWORDS: dict[str, list[str]] = {
    "Cooperation_PNAS": ["cooperation", "prosocial", "game theory", "evolutionary dynamics", "public goods", "intervention", "social dilemma", "cooperation network", "collective action"],
    "Adaptive_Monoculture": ["monoculture", "homogenization", "cultural", "adaptive reserve", "diversity loss", "AI steering", "echo chamber", "opinion dynamics", "consensus"],
    "Curation_Divergence": ["curation", "filter bubble", "belief fragmentation", "intermediary", "misinformation", "polarization", "recommendation", "information diet"],
    "Structural_Targeting": ["bridging capital", "structural", "targeting", "algorithmic fairness", "network inequality", "social capital", "bridge", "brokerage"],
    "AI_Governance_PNAS": ["governance", "polycentric", "public goods", "AI policy", "institutional design", "rate-distortion", "regulation", "AI safety"],
    "Provenance_Preference": ["mechanistic interpretability", "preference", "causal tracing", "economic behavior", "training dynamics", "probing", "circuit", "feature attribution"],
    "Constrained_Causal_Abstraction": ["causal abstraction", "causal discovery", "structural constraints", "circuit", "interchange intervention", "abstraction"],
    "Inf_Steering": ["activation steering", "activation editing", "sparse autoencoder", "logit", "influence", "minimum-norm", "representation engineering", "steering vector"],
    "Net_DPO": ["personalization", "consistency", "DPO", "preference optimization", "network", "knowledge work", "alignment", "RLHF"],
    "LLMMeasurements": ["LLM measurement", "text as data", "econometric", "AIPW", "calibration", "doubly robust", "audited", "annotation", "text classification"],
    "LLM_Prompt_Variations": ["prompt sensitivity", "prompt robustness", "prompt variation", "treatment effect", "specification", "prompt engineering", "in-context learning"],
    "Visible_Context_Thresholds": ["tool use", "context window", "phase transition", "emergent", "cognitive bottleneck", "agent", "tool-use", "planning"],
    "Visual_Evidence_Response": ["multimodal", "grounding", "visual", "vision-language", "evidence curve", "hallucination", "VLM"],
    "Preference_Geometry": ["risk preference", "social preference", "preference geometry", "behavioral economics", "decision making", "utility"],
    "Hypergraph_Interpretability": ["hypergraph", "higher-order", "interpretability", "graph neural"],
    "GPT_Pricing_Bias": ["pricing bias", "market", "discrimination", "LLM bias", "fairness", "audit"],
}


def score_paper(paper: dict) -> list[tuple[str, float]]:
    """Return (project_name, score) pairs for a paper."""
    text = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    matches = []
    for project, keywords in THEME_KEYWORDS.items():
        score = sum(1.0 for kw in keywords if kw.lower() in text)
        if score >= 1.0:  # lowered threshold to catch more relevant papers
            matches.append((project, score))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[:3]

# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def format_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) <= 3:
        return ", ".join(authors)
    return f"{authors[0]}, {authors[1]}, ... ({len(authors)} authors)"


def generate_digest(papers: list[dict], repos: list[dict],
                    digest_date: str, days_back: int) -> str:
    """Generate the full markdown digest."""
    # Score and sort papers
    scored = []
    for p in papers:
        matches = score_paper(p)
        if matches:
            p["project_matches"] = matches
            p["top_score"] = matches[0][1]
            scored.append(p)

    # Deduplicate by title similarity
    seen_titles: set[str] = set()
    unique = []
    for p in scored:
        title_key = re.sub(r"[^a-z0-9]", "", p["title"].lower())[:60]
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(p)
    scored = unique

    scored.sort(key=lambda x: x["top_score"], reverse=True)

    # Top picks for the at-a-glance table
    top_picks = scored[:8]

    # Group remaining by source type
    journal_papers = [p for p in scored if p["source"] == "Semantic Scholar"]
    arxiv_papers = [p for p in scored if p["source"] == "arXiv"]

    week_start = (datetime.strptime(digest_date, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%b %d")
    week_end = datetime.strptime(digest_date, "%Y-%m-%d").strftime("%b %d, %Y")

    lines = []
    lines.append(f"# Weekly Research Digest — {week_start}–{week_end}\n")
    lines.append(f"> **For:** {CFG['researcher']} | **Focus:** {', '.join(CFG['research_themes'][:5])}")
    lines.append(f"> **Read time:** ~12 min | Papers sorted by relevance to active projects\n")
    lines.append("---\n")

    # At-a-glance table
    lines.append("## At a Glance\n")
    lines.append("| # | Paper | Why it matters | Project link |")
    lines.append("|---|---|---|---|")
    for i, p in enumerate(top_picks, 1):
        title_short = p["title"][:80] + ("..." if len(p["title"]) > 80 else "")
        proj_links = ", ".join(m[0] for m in p["project_matches"][:2])
        # First sentence of summary as "why"
        why = p.get("summary", "")[:120].rstrip(".") + "."
        lines.append(f"| {i} | {title_short} | {why} | {proj_links} |")
    lines.append("")

    lines.append("---\n")

    # Journal papers section
    if journal_papers:
        lines.append("## Journal Papers\n")
        for p in journal_papers[:15]:
            venue = p.get("venue", "Unknown venue")
            year = p.get("year", "")
            lines.append(f"### {p['title']}")
            lines.append(f"**{venue}** {year} | {format_authors(p['authors'])}")
            if p.get("url"):
                lines.append(f"[Link]({p['url']})")
            lines.append("")
            if p.get("summary"):
                lines.append(f"{p['summary'][:300]}\n")
            # Your angle
            matches = p.get("project_matches", [])
            if matches:
                proj_names = ", ".join(m[0] for m in matches)
                lines.append(f"> **Your angle:** Relevant to **{proj_names}**. {CFG['active_projects'].get(matches[0][0], '')}\n")
            lines.append("---\n")

    # arXiv papers section
    if arxiv_papers:
        lines.append("## arXiv Preprints\n")
        for p in arxiv_papers[:15]:
            lines.append(f"### {p['title']}")
            lines.append(f"**arXiv {p.get('id', '')}** | {p.get('date', '')} | {format_authors(p['authors'])}")
            if p.get("url"):
                lines.append(f"[Link]({p['url']})")
            lines.append("")
            if p.get("summary"):
                lines.append(f"{p['summary'][:300]}\n")
            matches = p.get("project_matches", [])
            if matches:
                proj_names = ", ".join(m[0] for m in matches)
                lines.append(f"> **Your angle:** Relevant to **{proj_names}**. {CFG['active_projects'].get(matches[0][0], '')}\n")
            lines.append("---\n")

    # GitHub repos section
    if repos:
        lines.append("## Trending GitHub Repos\n")
        lines.append("| Repo | Stars | Description | Language |")
        lines.append("|---|---|---|---|")
        for r in repos[:12]:
            desc = r["description"][:100] + ("..." if len(r["description"]) > 100 else "")
            lines.append(f"| [{r['name']}]({r['url']}) | {r['stars']:,} | {desc} | {r.get('language', '')} |")
        lines.append("")

    # Synthesis
    lines.append("---\n")
    lines.append("## One-Paragraph Synthesis\n")
    project_counts: dict[str, int] = {}
    for p in scored:
        for m in p.get("project_matches", []):
            project_counts[m[0]] = project_counts.get(m[0], 0) + 1
    hot_projects = sorted(project_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    if hot_projects:
        hot_str = ", ".join(f"**{p}** ({c} papers)" for p, c in hot_projects)
        lines.append(
            f"This week's papers cluster around: {hot_str}. "
            f"Total papers found: {len(scored)} relevant out of {len(papers)} scanned. "
            f"Top GitHub repos: {len(repos)} trending in your areas.\n"
        )
    else:
        lines.append("Quiet week across target venues. Check back next week.\n")

    lines.append("---\n")
    lines.append(f"*Generated on {digest_date} by weekly-research-digest*")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate weekly research digest")
    parser.add_argument("--date", default=datetime.now(UTC).strftime("%Y-%m-%d"),
                        help="Digest date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=7,
                        help="Look-back window in days")
    parser.add_argument("--no-github", action="store_true",
                        help="Skip GitHub search")
    parser.add_argument("--no-copy", action="store_true",
                        help="Skip copying to local directory")
    args = parser.parse_args()

    print(f"Generating digest for {args.date} (looking back {args.days} days)...")

    # --- arXiv searches ---
    all_papers: list[dict] = []
    arxiv_queries = [
        "LLM agent cooperation game theory network",
        "mechanistic interpretability sparse autoencoder steering",
        "causal inference text measurement econometric LLM",
        "collective behavior social network AI governance",
        "human AI decision making fairness alignment",
        "prompt sensitivity robustness LLM evaluation",
        "multimodal grounding vision language model",
        "preference optimization DPO alignment",
    ]
    for q in arxiv_queries:
        print(f"  arXiv: {q[:50]}...")
        results = search_arxiv(q, CFG["arxiv_categories"],
                               max_results=15, days_back=args.days)
        all_papers.extend(results)
        time.sleep(1)  # be nice to arXiv API

    # --- Semantic Scholar searches ---
    current_year = datetime.now(UTC).year
    year_range = f"{current_year - 1}-{current_year}"
    ss_queries = CFG["semantic_scholar_fields"]
    for q in ss_queries:
        print(f"  Semantic Scholar: {q}...")
        results = search_semantic_scholar(q, max_results=8, year_range=year_range)
        all_papers.extend(results)
        time.sleep(3)  # Semantic Scholar rate limit: ~1 req/sec for unauthenticated

    print(f"  Total papers fetched: {len(all_papers)}")

    # --- GitHub repos ---
    repos: list[dict] = []
    if not args.no_github:
        print("  Searching GitHub...")
        repos = search_github_repos(CFG["github_topics"], days_back=args.days)
        print(f"  Found {len(repos)} repos")

    # --- Generate digest ---
    digest = generate_digest(all_papers, repos, args.date, args.days)

    # --- Write output ---
    output_dir = SCRIPT_DIR / CFG["output_dir"]
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"digest-{args.date}.md"
    output_file.write_text(digest)
    print(f"  Written to {output_file}")

    # --- Copy to local dir if available ---
    if not args.no_copy:
        local_dir = Path(CFG["local_copy_dir"])
        if local_dir.exists():
            local_file = local_dir / f"digest-{args.date}.md"
            shutil.copy2(output_file, local_file)
            print(f"  Copied to {local_file}")
        else:
            print(f"  Local dir {local_dir} not found, skipping copy")

    print("Done!")
    return str(output_file)


if __name__ == "__main__":
    main()
