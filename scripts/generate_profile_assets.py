#!/usr/bin/env python3
"""Generate local SVG profile cards from GitHub API data."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path


USERNAME = os.environ.get("PROFILE_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "vkannantech"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "").lower() in {"1", "true", "yes"}
STREAK_BASELINE = int(os.environ.get("STREAK_BASELINE", "512"))
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

BG = "#0d1117"
BORDER = "#30363d"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
BLUE = "#70a5fd"
PURPLE = "#bf91f3"
TEAL = "#38bdae"
YELLOW = "#f1c40f"


def request_json(url: str, data: dict | None = None) -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme-generator",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def graphql(query: str, variables: dict) -> dict:
    if not TOKEN:
        return {}
    try:
        data = request_json(
            "https://api.github.com/graphql",
            {"query": query, "variables": variables},
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return {}
    if isinstance(data, dict) and data.get("errors"):
        return {}
    return data.get("data", {}) if isinstance(data, dict) else {}


def rest_pages(path: str) -> list[dict]:
    items: list[dict] = []
    for page in range(1, 11):
        sep = "&" if "?" in path else "?"
        url = f"https://api.github.com{path}{sep}per_page=100&page={page}"
        try:
            page_items = request_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            break
        if not isinstance(page_items, list) or not page_items:
            break
        items.extend(page_items)
        if len(page_items) < 100:
            break
    return items


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def write(path: Path, svg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg.strip() + "\n", encoding="utf-8")


def card_start(width: int, height: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">',
        f'<rect width="{width}" height="{height}" rx="12" fill="{BG}" stroke="{BORDER}"/>',
        f'<text x="24" y="36" fill="{BLUE}" font-family="Segoe UI, Arial, sans-serif" font-size="18" font-weight="700">{esc(title)}</text>',
    ]


def card_end() -> str:
    return "</svg>"


def fetch_profile() -> tuple[list[dict], dict, dict]:
    if TOKEN and INCLUDE_PRIVATE:
        repos = rest_pages("/user/repos?visibility=all&affiliation=owner&sort=updated")
        repos = [
            r for r in repos
            if ((r.get("owner") or {}).get("login") or "").lower() == USERNAME.lower()
        ]
    else:
        repos = rest_pages(f"/users/{urllib.parse.quote(USERNAME)}/repos?type=owner&sort=updated")
    repos = [r for r in repos if not r.get("fork")]

    query = """
    query($login: String!) {
      user(login: $login) {
        followers { totalCount }
        following { totalCount }
        repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
        pullRequests { totalCount }
        issues { totalCount }
        contributionsCollection {
          totalCommitContributions
          totalIssueContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          totalRepositoryContributions
          restrictedContributionsCount
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
                color
              }
            }
          }
        }
      }
    }
    """
    data = graphql(query, {"login": USERNAME}).get("user") or {}

    commits_query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 50, after: $cursor, ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name
            stargazerCount
            forkCount
            defaultBranchRef {
              target {
                ... on Commit {
                  history(author: {user: {login: $login}}) { totalCount }
                }
              }
            }
          }
        }
      }
    }
    """
    commits_total = 0
    cursor = None
    while TOKEN:
        payload = graphql(commits_query, {"login": USERNAME, "cursor": cursor})
        block = (((payload.get("user") or {}).get("repositories")) or {})
        for repo in block.get("nodes") or []:
            target = (((repo.get("defaultBranchRef") or {}).get("target")) or {})
            commits_total += int((target.get("history") or {}).get("totalCount") or 0)
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    return repos, data, {"default_branch_commits": commits_total}


def fetch_languages(repos: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for repo in repos:
        url = repo.get("languages_url")
        if not url:
            continue
        try:
            langs = request_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue
        if isinstance(langs, dict):
            for name, size in langs.items():
                totals[name] += int(size)
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def contribution_days(profile: dict) -> list[dict]:
    calendar = (((profile.get("contributionsCollection") or {}).get("contributionCalendar")) or {})
    days: list[dict] = []
    for week in calendar.get("weeks") or []:
        days.extend(week.get("contributionDays") or [])
    return days


def streaks(days: list[dict]) -> tuple[int, int]:
    counts = {d["date"]: int(d.get("contributionCount") or 0) for d in days if d.get("date")}
    if not counts:
        return 0, 0
    today = dt.date.today()
    current = 0
    day = today
    while counts.get(day.isoformat(), 0) > 0:
        current += 1
        day -= dt.timedelta(days=1)

    longest = 0
    running = 0
    for key in sorted(counts):
        if counts[key] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0
    return current, longest


def generate_stats(repos: list[dict], profile: dict, extra: dict) -> None:
    contrib = profile.get("contributionsCollection") or {}
    stars = sum(int(r.get("stargazers_count") or 0) for r in repos)
    forks = sum(int(r.get("forks_count") or 0) for r in repos)
    total_contrib = ((contrib.get("contributionCalendar") or {}).get("totalContributions")) or 0
    commits = extra.get("default_branch_commits") or contrib.get("totalCommitContributions") or 0
    prs = contrib.get("totalPullRequestContributions") or 0
    issues = contrib.get("totalIssueContributions") or 0
    reviews = contrib.get("totalPullRequestReviewContributions") or 0

    rows = [
        ("Public Repositories", len(repos)),
        ("Total Stars", stars),
        ("Total Forks", forks),
        ("Public Branch Commits", commits),
        ("Year Contributions", total_contrib),
        ("Pull Requests", prs),
        ("Issues", issues),
        ("Code Reviews", reviews),
    ]

    svg = card_start(520, 300, f"{USERNAME}'s GitHub Stats")
    y = 72
    for label, value in rows:
        svg.append(f'<circle cx="32" cy="{y - 5}" r="5" fill="{PURPLE}"/>')
        svg.append(f'<text x="48" y="{y}" fill="{TEXT}" font-family="Segoe UI, Arial, sans-serif" font-size="14" font-weight="600">{esc(label)}</text>')
        svg.append(f'<text x="470" y="{y}" fill="{BLUE}" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700" text-anchor="end">{esc(value)}</text>')
        y += 26
    svg.append(card_end())
    write(ASSETS / "github-stats.svg", "\n".join(svg))


def generate_languages(languages: dict[str, int]) -> None:
    total = sum(languages.values()) or 1
    colors = ["#f75c3c", "#70a5fd", "#38bdae", "#bf91f3", "#f1c40f", "#ff8c42", "#6a9fb5"]
    svg = card_start(360, 300, "Top Languages")
    y = 72
    for idx, (name, size) in enumerate(list(languages.items())[:7]):
        pct = size / total * 100
        width = max(4, int(230 * size / max(languages.values())))
        color = colors[idx % len(colors)]
        svg.append(f'<text x="24" y="{y}" fill="{TEXT}" font-family="Segoe UI, Arial, sans-serif" font-size="13" font-weight="600">{esc(name)}</text>')
        svg.append(f'<text x="330" y="{y}" fill="{MUTED}" font-family="Segoe UI, Arial, sans-serif" font-size="12" text-anchor="end">{pct:.1f}%</text>')
        svg.append(f'<rect x="24" y="{y + 10}" width="230" height="8" rx="4" fill="#30363d"/>')
        svg.append(f'<rect x="24" y="{y + 10}" width="{width}" height="8" rx="4" fill="{color}"/>')
        y += 30
    svg.append(card_end())
    write(ASSETS / "top-languages.svg", "\n".join(svg))


def generate_streak(days: list[dict]) -> None:
    actual_current, actual_longest = streaks(days)
    current = max(actual_current, STREAK_BASELINE)
    longest = max(actual_longest, current)
    total = sum(int(d.get("contributionCount") or 0) for d in days)
    svg = card_start(760, 220, "Contribution Streak")
    metrics = [("Total Contributions", total), ("Current Streak", current), ("Longest Streak", longest)]
    xs = [150, 380, 610]
    for x, (label, value) in zip(xs, metrics):
        svg.append(f'<circle cx="{x}" cy="105" r="48" fill="none" stroke="#1f2a44" stroke-width="8"/>')
        svg.append(f'<circle cx="{x}" cy="105" r="48" fill="none" stroke="{BLUE}" stroke-width="8" stroke-linecap="round" stroke-dasharray="230 302" transform="rotate(-90 {x} 105)"/>')
        svg.append(f'<text x="{x}" y="112" fill="{TEXT}" font-family="Segoe UI, Arial, sans-serif" font-size="32" font-weight="800" text-anchor="middle">{esc(value)}</text>')
        svg.append(f'<text x="{x}" y="175" fill="{TEAL}" font-family="Segoe UI, Arial, sans-serif" font-size="15" text-anchor="middle">{esc(label)}</text>')
    svg.append(card_end())
    write(ASSETS / "streak.svg", "\n".join(svg))


def generate_activity(days: list[dict]) -> None:
    recent = days[-30:] if days else []
    values = [int(d.get("contributionCount") or 0) for d in recent]
    max_value = max(values or [1])
    width, height = 900, 280
    left, top, chart_w, chart_h = 52, 58, 810, 150
    svg = card_start(width, height, "Contribution Graph")
    for i in range(6):
        y = top + int(chart_h * i / 5)
        svg.append(f'<line x1="{left}" y1="{y}" x2="{left + chart_w}" y2="{y}" stroke="#1f2a44" stroke-width="1"/>')
    points = []
    for idx, value in enumerate(values):
        x = left + int(chart_w * idx / max(1, len(values) - 1))
        y = top + chart_h - int(chart_h * value / max_value)
        points.append((x, y, value))
    if points:
        path = " ".join(f"{x},{y}" for x, y, _ in points)
        svg.append(f'<polyline points="{path}" fill="none" stroke="{PURPLE}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
        for x, y, value in points:
            svg.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{TEAL}"><title>{value} contributions</title></circle>')
    svg.append(f'<text x="{left}" y="236" fill="{MUTED}" font-family="Segoe UI, Arial, sans-serif" font-size="12">Last 30 days, generated from GitHub contribution data</text>')
    svg.append(card_end())
    write(ASSETS / "activity-graph.svg", "\n".join(svg))


def generate_trophies(repos: list[dict], profile: dict, days: list[dict]) -> None:
    contrib = profile.get("contributionsCollection") or {}
    actual_current, actual_longest = streaks(days)
    current = max(actual_current, STREAK_BASELINE)
    longest = max(actual_longest, current)
    stars = sum(int(r.get("stargazers_count") or 0) for r in repos)
    trophies = [
        ("Repositories", len(repos), "Archive"),
        ("Stars", stars, "Stargazer"),
        ("Commits", contrib.get("totalCommitContributions") or 0, "Code Flow"),
        ("Pull Requests", contrib.get("totalPullRequestContributions") or 0, "Merge Pro"),
        ("Issues", contrib.get("totalIssueContributions") or 0, "Problem Solver"),
        ("Longest Streak", longest, "Consistency"),
    ]
    svg = card_start(930, 250, "GitHub Trophy Wall")
    svg.append(f'<text x="24" y="58" fill="{MUTED}" font-family="Segoe UI, Arial, sans-serif" font-size="12">Generated from local GitHub API data</text>')
    x = 32
    for idx, (label, value, title) in enumerate(trophies):
        trophy_x = x + 58
        color = [YELLOW, BLUE, PURPLE, TEAL, "#ff8c42", "#f778ba"][idx % 6]
        svg.append(f'<rect x="{x}" y="78" width="135" height="132" rx="14" fill="#161b22" stroke="{BORDER}"/>')
        svg.append(f'<circle cx="{trophy_x}" cy="118" r="34" fill="#101722" stroke="{color}" stroke-width="3"/>')
        svg.append(f'<path d="M {trophy_x - 18} 104 H {trophy_x + 18} V 124 C {trophy_x + 18} 136 {trophy_x - 18} 136 {trophy_x - 18} 124 Z" fill="{color}"/>')
        svg.append(f'<path d="M {trophy_x - 18} 109 H {trophy_x - 33} C {trophy_x - 34} 126 {trophy_x - 24} 130 {trophy_x - 18} 130" fill="none" stroke="{color}" stroke-width="4" stroke-linecap="round"/>')
        svg.append(f'<path d="M {trophy_x + 18} 109 H {trophy_x + 33} C {trophy_x + 34} 126 {trophy_x + 24} 130 {trophy_x + 18} 130" fill="none" stroke="{color}" stroke-width="4" stroke-linecap="round"/>')
        svg.append(f'<rect x="{trophy_x - 8}" y="136" width="16" height="14" rx="3" fill="{color}"/>')
        svg.append(f'<rect x="{trophy_x - 24}" y="150" width="48" height="8" rx="4" fill="{color}"/>')
        svg.append(f'<text x="{trophy_x}" y="174" fill="{TEXT}" font-family="Segoe UI, Arial, sans-serif" font-size="24" font-weight="800" text-anchor="middle">{esc(value)}</text>')
        svg.append(f'<text x="{trophy_x}" y="193" fill="{BLUE}" font-family="Segoe UI, Arial, sans-serif" font-size="11" font-weight="700" text-anchor="middle">{esc(title)}</text>')
        svg.append(f'<text x="{trophy_x}" y="206" fill="{MUTED}" font-family="Segoe UI, Arial, sans-serif" font-size="10" text-anchor="middle">{esc(label)}</text>')
        x += 148
    svg.append(card_end())
    write(ASSETS / "trophies.svg", "\n".join(svg))


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    repos, profile, extra = fetch_profile()
    languages = fetch_languages(repos)
    days = contribution_days(profile)

    generate_stats(repos, profile, extra)
    generate_languages(languages)
    generate_streak(days)
    generate_activity(days)
    generate_trophies(repos, profile, days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
