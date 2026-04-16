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
        if score >= 1.0:
            matches.append((project, score))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[:3]


# Venue quality tiers (impact factor / prestige proxy)
VENUE_TIER: dict[str, int] = {
    # Tier 1: top general science (5 stars)
    "nature": 5, "science": 5, "pnas": 5,
    # Tier 2: top field journals (4 stars)
    "nature communications": 4, "science advances": 4,
    "nature human behaviour": 4, "nature machine intelligence": 4,
    "management science": 4, "information systems research": 4,
    "marketing science": 4, "mis quarterly": 4,
    "journal of marketing research": 4, "msom": 4,
    # Tier 3: top ML conferences (4 stars)
    "neurips": 4, "icml": 4, "iclr": 4, "nips": 4,
    "aaai": 3, "acl": 3, "emnlp": 3,
    # Tier 2-3: good journals
    "nature reviews": 4, "scientific reports": 3,
    "plos one": 3, "behavior research methods": 3,
    # Preprints
    "arxiv": 2, "arxiv.org": 2,
}


def _venue_stars(paper: dict) -> int:
    """Rate venue quality 1-5 stars."""
    venue = paper.get("venue", "").lower()
    source = paper.get("source", "").lower()
    # Check venue name against known tiers
    for name, stars in VENUE_TIER.items():
        if name in venue:
            return stars
    # Fallback: arXiv papers get 2, unknown venues get 2
    if source == "arxiv" or "arxiv" in venue:
        return 2
    return 2


def _relevance_stars(paper: dict) -> int:
    """Rate relevance to active projects 1-5 stars."""
    matches = paper.get("project_matches", [])
    if not matches:
        return 1
    top_score = matches[0][1]
    num_projects = len(matches)
    # High keyword overlap + multiple projects = very relevant
    if top_score >= 4 and num_projects >= 2:
        return 5
    if top_score >= 3 or (top_score >= 2 and num_projects >= 2):
        return 4
    if top_score >= 2:
        return 3
    if num_projects >= 2:
        return 3
    return 2


def _impact_stars(paper: dict) -> int:
    """Rate potential impact 1-5 stars based on citations, venue, recency."""
    citations = paper.get("citations", 0) or 0
    venue_q = _venue_stars(paper)

    # High citations for a recent paper = high impact
    if citations >= 50:
        return 5
    if citations >= 20:
        return 4
    if citations >= 5:
        return 3

    # No citations yet (new paper) — use venue as proxy
    if venue_q >= 4:
        return 4
    if venue_q >= 3:
        return 3
    return 2


def rate_paper(paper: dict) -> dict:
    """Add quality, relevance, and impact ratings to a paper."""
    paper["rating_quality"] = _venue_stars(paper)
    paper["rating_relevance"] = _relevance_stars(paper)
    paper["rating_impact"] = _impact_stars(paper)
    paper["rating_overall"] = round(
        (paper["rating_quality"] * 0.3
         + paper["rating_relevance"] * 0.4
         + paper["rating_impact"] * 0.3), 1
    )
    return paper


def stars(n: int) -> str:
    """Render star rating as emoji string."""
    return "+" * n + "." * (5 - n)

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
    # Score, rate, and sort papers
    scored = []
    for p in papers:
        matches = score_paper(p)
        if matches:
            p["project_matches"] = matches
            p["top_score"] = matches[0][1]
            rate_paper(p)
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

    # Sort by overall rating (weighted: relevance 40%, quality 30%, impact 30%)
    scored.sort(key=lambda x: x["rating_overall"], reverse=True)

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

    # Rating legend
    lines.append("## Rating Guide\n")
    lines.append("> **Quality** = venue prestige | **Relevance** = match to your projects | **Impact** = citations + venue")
    lines.append("> Scale: `+++++` (5/5) to `+....` (1/5) | Papers sorted by overall score\n")

    # At-a-glance table
    lines.append("## At a Glance\n")
    lines.append("| # | Paper | Quality | Relevance | Impact | Project link |")
    lines.append("|---|---|---|---|---|---|")
    for i, p in enumerate(top_picks, 1):
        title_short = p["title"][:70] + ("..." if len(p["title"]) > 70 else "")
        url = p.get("url", "")
        title_cell = f"[{title_short}]({url})" if url else title_short
        proj_links = ", ".join(m[0] for m in p["project_matches"][:2])
        q = stars(p["rating_quality"])
        r = stars(p["rating_relevance"])
        im = stars(p["rating_impact"])
        lines.append(f"| {i} | {title_cell} | `{q}` | `{r}` | `{im}` | {proj_links} |")
    lines.append("")

    lines.append("---\n")

    # Journal papers section (no cap — show all relevant)
    if journal_papers:
        lines.append("## Journal Papers\n")
        for p in sorted(journal_papers, key=lambda x: x["rating_overall"], reverse=True):
            venue = p.get("venue", "Unknown venue")
            year = p.get("year", "")
            url = p.get("url", "")
            if url:
                lines.append(f"### [{p['title']}]({url})")
            else:
                lines.append(f"### {p['title']}")
            lines.append(f"**{venue}** {year} | {format_authors(p['authors'])}")
            # Ratings bar
            q = stars(p["rating_quality"])
            r = stars(p["rating_relevance"])
            im = stars(p["rating_impact"])
            lines.append(f"Quality `{q}` | Relevance `{r}` | Impact `{im}` | Overall **{p['rating_overall']}/5**")
            lines.append("")
            summary = p.get("summary", "")
            if summary:
                lines.append(f"**Summary:** {summary[:400]}\n")
            matches = p.get("project_matches", [])
            if matches:
                proj_names = ", ".join(m[0] for m in matches)
                proj_desc = CFG['active_projects'].get(matches[0][0], '')
                lines.append(f"> **What you can learn → {proj_names}:** {proj_desc}\n")
            lines.append("---\n")

    # arXiv papers section (no cap — show all relevant)
    if arxiv_papers:
        lines.append("## arXiv Preprints\n")
        for p in sorted(arxiv_papers, key=lambda x: x["rating_overall"], reverse=True):
            url = p.get("url", "")
            if url:
                lines.append(f"### [{p['title']}]({url})")
            else:
                lines.append(f"### {p['title']}")
            lines.append(f"**arXiv {p.get('id', '')}** | {p.get('date', '')} | {format_authors(p['authors'])}")
            q = stars(p["rating_quality"])
            r = stars(p["rating_relevance"])
            im = stars(p["rating_impact"])
            lines.append(f"Quality `{q}` | Relevance `{r}` | Impact `{im}` | Overall **{p['rating_overall']}/5**")
            lines.append("")
            summary = p.get("summary", "")
            if summary:
                lines.append(f"**Summary:** {summary[:400]}\n")
            matches = p.get("project_matches", [])
            if matches:
                proj_names = ", ".join(m[0] for m in matches)
                proj_desc = CFG['active_projects'].get(matches[0][0], '')
                lines.append(f"> **What you can learn → {proj_names}:** {proj_desc}\n")
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
# Audio generation
# ---------------------------------------------------------------------------

def _proj_friendly(name: str) -> str:
    """Convert project key to natural spoken name."""
    friendly = {
        "Cooperation_PNAS": "your cooperation on networks paper",
        "Adaptive_Monoculture": "your adaptive monoculture paper",
        "Curation_Divergence": "your curation divergence paper",
        "Structural_Targeting": "your structural targeting paper",
        "AI_Governance_PNAS": "your AI governance paper",
        "Provenance_Preference": "your preference provenance paper for NeurIPS",
        "Constrained_Causal_Abstraction": "your causal abstraction paper for NeurIPS",
        "Inf_Steering": "your influence steering paper for ICML",
        "Net_DPO": "your Net DPO paper for Management Science",
        "LLMMeasurements": "your LLM measurements paper",
        "LLM_Prompt_Variations": "your prompt variations paper for Management Science",
        "Visible_Context_Thresholds": "your visible context thresholds paper for Nature Machine Intelligence",
        "Visual_Evidence_Response": "your visual evidence response paper for Nature Machine Intelligence",
        "Preference_Geometry": "your preference geometry paper",
        "Hypergraph_Interpretability": "your hypergraph interpretability paper",
        "GPT_Pricing_Bias": "your pricing bias paper",
    }
    return friendly.get(name, name.replace("_", " "))


def _summarize_for_speech(summary: str) -> str:
    """Extract first 2 clean sentences from abstract for spoken delivery."""
    if not summary:
        return ""
    # Clean up artifacts
    text = re.sub(r'\s+', ' ', summary).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = " ".join(sentences[:2])
    # Limit length for listening
    if len(result) > 350:
        result = " ".join(sentences[:1])
    return result


def _transition(i: int) -> str:
    """Conversational transition phrases between papers."""
    phrases = [
        "Let's start with something interesting.",
        "Next up.",
        "Here's another one worth noting.",
        "This next one is quite relevant.",
        "Moving on.",
        "Here's something a bit different.",
        "Now, this one caught my attention.",
        "And another.",
    ]
    return phrases[i % len(phrases)]


def _rating_word(score: float) -> str:
    """Convert numeric rating to spoken description."""
    if score >= 4.0:
        return "very strong"
    if score >= 3.5:
        return "strong"
    if score >= 3.0:
        return "solid"
    if score >= 2.5:
        return "moderate"
    return "worth a look"


def digest_to_speech_text(scored: list[dict], repos: list[dict],
                          digest_date: str, days_back: int) -> str:
    """Convert digest data into a conversational podcast script."""
    week_start = (datetime.strptime(digest_date, "%Y-%m-%d") - timedelta(days=days_back)).strftime("%B %d")
    week_end = datetime.strptime(digest_date, "%Y-%m-%d").strftime("%B %d, %Y")

    parts = []

    # --- Intro ---
    parts.append(f"Hey Yan. Welcome to your weekly research digest, covering {week_start} through {week_end}.")
    parts.append("")

    # Count what we found
    project_counts: dict[str, int] = {}
    for p in scored:
        for m in p.get("project_matches", []):
            project_counts[m[0]] = project_counts.get(m[0], 0) + 1
    hot = sorted(project_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    parts.append(f"I scanned the latest publications and found {len(scored)} papers relevant to your work.")
    if hot:
        hot_names = ", ".join(_proj_friendly(p) for p, _ in hot[:2])
        parts.append(f"The biggest clusters this week are around {hot_names}.")
    parts.append("")
    parts.append("Let me walk you through the highlights.")
    parts.append("")

    # --- Deep dive: top 5 papers ---
    top = scored[:5]
    for i, p in enumerate(top):
        title = p["title"]
        venue = p.get("venue", "") or p.get("source", "")
        overall = p.get("rating_overall", 0)
        matches = p.get("project_matches", [])
        proj = _proj_friendly(matches[0][0]) if matches else "your research"
        summary = _summarize_for_speech(p.get("summary", ""))
        rating_desc = _rating_word(overall)

        parts.append(_transition(i))
        parts.append("")

        # Title — conversational
        if venue and venue != "arXiv" and venue != "arxiv.org":
            parts.append(f"A paper in {venue} titled: {title}.")
        else:
            parts.append(f"A new preprint titled: {title}.")
        parts.append("")

        # What they found — the key content
        if summary:
            parts.append(f"Here's what they found. {summary}")
            parts.append("")

        # Why you care — the connection
        parts.append(f"Why this matters to you: this connects to {proj}.")
        if len(matches) > 1:
            other = _proj_friendly(matches[1][0])
            parts.append(f"It's also relevant to {other}.")

        # Rating — brief
        parts.append(f"I'd rate this one {rating_desc}, {overall} out of 5 overall.")
        parts.append("")

    # --- Quick hits: next 5-8 papers ---
    quick = scored[5:12]
    if quick:
        parts.append("Now, a few quick hits. These are worth knowing about but I'll keep them brief.")
        parts.append("")

        for p in quick:
            title = p["title"]
            matches = p.get("project_matches", [])
            proj = _proj_friendly(matches[0][0]) if matches else "your work"
            overall = p.get("rating_overall", 0)
            summary = _summarize_for_speech(p.get("summary", ""))

            parts.append(f"{title}.")
            if summary:
                # Just one sentence for quick hits
                one_sentence = re.split(r'(?<=[.!?])\s+', summary)[0]
                parts.append(one_sentence)
            parts.append(f"Relevant to {proj}. Rated {overall} out of 5.")
            parts.append("")

    # How many more in written version
    remaining = len(scored) - 12
    if remaining > 0:
        parts.append(f"There are {remaining} more papers in the written digest if you want to go deeper.")
        parts.append("")

    # --- GitHub repos ---
    if repos:
        parts.append("Switching gears to GitHub.")
        parts.append("")
        top_repos = repos[:3]
        for r in top_repos:
            name = r["name"].split("/")[-1].replace("-", " ")
            desc = r["description"][:120] if r["description"] else ""
            parts.append(f"{name}, with {r['stars']:,} stars. {desc}")
        parts.append("")

    # --- Closing synthesis ---
    parts.append("So, stepping back, what's the big picture this week?")
    parts.append("")
    if hot:
        if len(hot) >= 2:
            parts.append(
                f"The field is moving on two fronts that matter for you. "
                f"First, {_proj_friendly(hot[0][0])}, where {hot[0][1]} papers appeared this week. "
                f"And second, {_proj_friendly(hot[1][0])}, with {hot[1][1]} papers."
            )
        else:
            parts.append(
                f"Most of the action this week is around {_proj_friendly(hot[0][0])}, "
                f"with {hot[0][1]} papers touching that area."
            )
    parts.append("")
    parts.append("Check the written digest for links, ratings, and full summaries. That's all for this week. Talk to you next Sunday.")

    return "\n".join(parts)


async def generate_audio(text: str, output_path: str,
                         voice: str = "en-US-AriaNeural") -> str:
    """Generate MP3 audio from text using edge-tts."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate="+10%")
    await communicate.save(output_path)
    return output_path


def make_audio(scored: list[dict], repos: list[dict],
               digest_date: str, days_back: int, output_dir: Path) -> str | None:
    """Generate audio digest. Returns path to MP3 or None on failure."""
    try:
        import asyncio
        speech = digest_to_speech_text(scored, repos, digest_date, days_back)

        # Save speech script for reference
        script_path = output_dir / f"digest-{digest_date}-script.txt"
        script_path.write_text(speech)

        audio_path = str(output_dir / f"digest-{digest_date}.mp3")
        asyncio.run(generate_audio(speech, audio_path))
        return audio_path
    except ImportError:
        print("  [WARN] edge-tts not installed, skipping audio (pip install edge-tts)")
        return None
    except Exception as e:
        print(f"  [WARN] Audio generation failed: {e}")
        return None

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
    parser.add_argument("--audio", action="store_true", default=True,
                        help="Generate MP3 audio version (default: on)")
    parser.add_argument("--no-audio", action="store_true",
                        help="Skip audio generation")
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

    # --- Score papers for shared use by digest + audio ---
    scored = []
    for p in all_papers:
        matches = score_paper(p)
        if matches:
            p["project_matches"] = matches
            p["top_score"] = matches[0][1]
            rate_paper(p)
            scored.append(p)
    # Deduplicate
    seen_titles: set[str] = set()
    unique_scored = []
    for p in scored:
        title_key = re.sub(r"[^a-z0-9]", "", p["title"].lower())[:60]
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_scored.append(p)
    unique_scored.sort(key=lambda x: x["rating_overall"], reverse=True)

    # --- Generate digest ---
    digest = generate_digest(all_papers, repos, args.date, args.days)

    # --- Write output ---
    output_dir = SCRIPT_DIR / CFG["output_dir"]
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"digest-{args.date}.md"
    output_file.write_text(digest)
    print(f"  Written to {output_file}")

    # --- Generate audio ---
    if args.audio and not args.no_audio:
        print("  Generating audio...")
        audio_path = make_audio(unique_scored, repos, args.date, args.days, output_dir)
        if audio_path:
            print(f"  Audio: {audio_path}")
            # Copy audio to local dir too
            if not args.no_copy:
                local_dir = Path(CFG["local_copy_dir"])
                if local_dir.exists():
                    shutil.copy2(audio_path, local_dir / f"digest-{args.date}.mp3")
                    script_src = output_dir / f"digest-{args.date}-script.txt"
                    if script_src.exists():
                        shutil.copy2(script_src, local_dir / f"digest-{args.date}-script.txt")

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
