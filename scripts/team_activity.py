#!/usr/bin/env python
"""Globussoft-Technologies — authored-code report.

Requires: gh CLI authenticated (`gh auth login`).

Usage:
  python scripts/team_activity.py                  # yesterday
  python scripts/team_activity.py --days 7         # last 7 days
  python scripts/team_activity.py --since 2026-06-01 --until 2026-06-13
  python scripts/team_activity.py --days 1 --drill 3
"""
import argparse, subprocess, json, sys, os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Orgs scanned by default. Override with --orgs A,B,C
DEFAULT_ORGS = ["Globussoft-Technologies", "EmpCloud"]

LOCK_FILES = {
    "package-lock.json","yarn.lock","pnpm-lock.yaml","composer.lock",
    "Gemfile.lock","Pipfile.lock","poetry.lock","Cargo.lock","go.sum",
    "bun.lockb","packages.lock.json","mix.lock","podfile.lock",
}
VENDOR_DIRS = (
    "/node_modules/","/vendor/","/dist/","/build/","/.next/","/__pycache__/",
    "/.venv/","/venv/","/target/","/bin/","/obj/","/.angular/","/coverage/",
    "/assets/dist/","/public/assets/","/public/build/","/storage/framework/",
    "/.nuxt/","/.expo/","/Pods/","/.gradle/","/.idea/","/.vscode/",
)
BINARY_EXTS = (
    ".svg",".png",".jpg",".jpeg",".gif",".ico",".webp",".bmp",
    ".woff",".woff2",".ttf",".eot",".otf",
    ".pdf",".zip",".tar",".gz",".bz2",".7z",".rar",
    ".mp4",".mp3",".wav",".mov",".avi",".webm",
    ".pyc",".class",".jar",".war",".dll",".so",".exe",".bin",
    ".psd",".ai",".sketch",".fig",
)
MINIFIED = (".min.js",".min.css",".min.map",".bundle.js",".chunk.js")
GENERATED_HINTS = (
    ".generated.",".gen.",".pb.go",".pb.cc",".pb.h",
    "_pb2.py","_pb2_grpc.py","__generated__","schema.graphql.ts",
)
BOTS = {
    "mirror-bot","dependabot","dependabot[bot]","github-actions",
    "github-actions[bot]","renovate","renovate[bot]",
}

# Map alternative identities → canonical login.
# Add a line whenever someone commits under more than one GitHub account / git author.
IDENTITY_ALIASES = {
    "indianbill007":              "sumitglobussoft",
    "Sumit Ghosh":                "sumitglobussoft",
    "suhailkhan@globussoft.in":   "suhailGlobussoft",
}

def is_ignored(path):
    base = path.rsplit("/",1)[-1].lower()
    p_l = path.lower()
    if base in {f.lower() for f in LOCK_FILES}: return "lockfile"
    for d in VENDOR_DIRS:
        if d in "/" + p_l: return "vendor"
    if any(p_l.endswith(ext) for ext in BINARY_EXTS): return "binary"
    if any(p_l.endswith(m) for m in MINIFIED):       return "minified"
    if p_l.endswith(".map"):                          return "sourcemap"
    # Note: GENERATED_HINTS check intentionally disabled — protobuf / gql
    # codegen + tests all count as authored work now. Someone wrote them.
    return ""

_CURRENT_TOKEN = None
def set_token(t):
    """Set the gh-CLI token used by subsequent gh() calls."""
    global _CURRENT_TOKEN
    _CURRENT_TOKEN = t

def token_for_org(org):
    """Resolve which token to use for an org.
    Per-org env var wins; falls back to GH_TOKEN."""
    key = "GH_TOKEN_" + org.upper().replace("-","_").replace(".","_")
    return os.environ.get(key) or os.environ.get("GH_TOKEN") or ""

def gh(*args):
    env = os.environ.copy()
    if _CURRENT_TOKEN:
        env["GH_TOKEN"] = _CURRENT_TOKEN
    r = subprocess.run(["gh","api",*args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return r.stdout if r.returncode == 0 else None

def aggregate_by_author(commit_records, since_dt):
    """Filter flat commit records by author-date, aggregate per login."""
    stats = defaultdict(lambda: {"commits":0,"add":0,"del":0,"repos":set()})
    for c in commit_records:
        cd = datetime.fromisoformat(c["date"].replace("Z","+00:00"))
        if cd < since_dt:
            continue
        s = stats[c["login"]]
        s["commits"] += 1
        s["add"]     += c["add"]
        s["del"]     += c["del"]
        s["repos"].add(c["repo"])
    return stats

def find_pr_signals(orgs, today_utc):
    """For each org, find PRs MERGED on yesterday UTC.
    Returns (first_prs, review_counts).
      first_prs:     list of {author, org, repo, title, url, number}
                     for PRs where author has only one merged PR in the org ever.
      review_counts: dict {reviewer_login: count_of_yesterday_PRs_they_reviewed}
                     (excludes PR author, bots, identity-collapsed)
    """
    yesterday = (today_utc - timedelta(days=1)).isoformat()
    first_prs = []
    review_counts = defaultdict(int)
    for org in orgs:
        set_token(token_for_org(org))
        # Find PRs merged yesterday
        merged_query = f"is:pr is:merged org:{org} merged:{yesterday}"
        result = gh("-X", "GET", "search/issues", "-f", f"q={merged_query}")
        if not result: continue
        try:
            data = json.loads(result)
        except Exception:
            continue
        items = data.get("items", []) or []
        for pr in items:
            user = pr.get("user") or {}
            author = user.get("login") or ""
            if not author: continue
            author = IDENTITY_ALIASES.get(author, author)
            # Get review info for this PR
            pr_api_url = (pr.get("pull_request") or {}).get("url","")
            if pr_api_url:
                # Strip the API host prefix
                reviews_path = pr_api_url.replace("https://api.github.com/","") + "/reviews"
                reviews_raw = gh(reviews_path)
                if reviews_raw:
                    try:
                        review_data = json.loads(reviews_raw)
                        # Count each distinct reviewer once per PR
                        seen_reviewers = set()
                        for r in review_data:
                            ru = r.get("user") or {}
                            reviewer = ru.get("login") or ""
                            if not reviewer: continue
                            if reviewer.endswith("[bot]"): continue
                            if reviewer in BOTS: continue
                            reviewer = IDENTITY_ALIASES.get(reviewer, reviewer)
                            if reviewer == author: continue   # self-review doesn't count
                            if reviewer in seen_reviewers: continue
                            seen_reviewers.add(reviewer)
                            review_counts[reviewer] += 1
                    except Exception:
                        pass
            # Check if this is the author's first ever merged PR in this org
            first_check_query = f"is:pr is:merged org:{org} author:{author}"
            first_result = gh("-X", "GET", "search/issues", "-f", f"q={first_check_query}")
            if first_result:
                try:
                    first_data = json.loads(first_result)
                    if first_data.get("total_count", 0) == 1:
                        repo_name = (pr.get("repository_url") or "").split("/")[-1]
                        first_prs.append({
                            "author": author, "org": org, "repo": repo_name,
                            "title": pr.get("title",""),
                            "url":   pr.get("html_url",""),
                            "number": pr.get("number"),
                        })
                except Exception:
                    pass
    return first_prs, dict(review_counts)

def render_md_first_prs(first_prs):
    if not first_prs: return
    print("## 🎉 First PRs landed yesterday")
    print()
    print("Welcome to the codebase — these engineers just shipped their first merged PR. Buy them a coffee, leave a 👍, send a note.")
    print()
    for pr in first_prs:
        print(f"- [@{pr['author']}](https://github.com/{pr['author']}) — **{pr['org']}/{pr['repo']}** · [{pr['title']}]({pr['url']})")
    print()

def render_md_reviewers(review_counts, top_n=10):
    if not review_counts: return
    print("## 👀 Top reviewers — yesterday")
    print()
    print("Reviewing is half the job. These engineers reviewed PRs that merged yesterday:")
    print()
    print("| # | Reviewer | PRs reviewed |")
    print("|---:|---|---:|")
    ranked = sorted(review_counts.items(), key=lambda x: -x[1])
    for i, (reviewer, count) in enumerate(ranked[:top_n], 1):
        print(f"| {i} | [@{reviewer}](https://github.com/{reviewer}) | {count} |")
    print()

def compute_streaks(commit_records, today_utc):
    """Per-author current shipping streak (consecutive UTC calendar days ending
    yesterday) + longest streak found in the scan window + total active days."""
    by_author = defaultdict(set)
    for c in commit_records:
        cd = datetime.fromisoformat(c["date"].replace("Z","+00:00")).date()
        by_author[c["login"]].add(cd)
    out = {}
    for author, dates in by_author.items():
        if not dates: continue
        # Current streak: walk backward from yesterday
        cur = 0
        check = today_utc - timedelta(days=1)
        while check in dates:
            cur += 1
            check -= timedelta(days=1)
        # Longest streak observed in the window
        longest = 0; run = 1
        sorted_dates = sorted(dates)
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i-1]).days == 1:
                run += 1
            else:
                longest = max(longest, run)
                run = 1
        longest = max(longest, run)
        out[author] = {"current": cur, "longest": longest, "active_days": len(dates)}
    return out

def render_md_streaks(streaks, top_n=15):
    """Render the streaks section."""
    if not streaks:
        return
    # Active streaks: current >= 1, ranked by current desc then longest desc
    active = sorted(((a,s) for a,s in streaks.items() if s["current"] >= 1),
                    key=lambda x: (-x[1]["current"], -x[1]["longest"]))
    if active:
        print("| 🔥 Current streak | Engineer | Days active (last 365) |")
        print("|---:|---|---:|")
        for a, s in active[:top_n]:
            print(f"| {s['current']} | [@{a}](https://github.com/{a}) | {s['active_days']} |")
        print()
    else:
        print("_Nobody on an active streak right now. Push something today and start one._")
        print()
    # Hall of fame: longest streaks in the year, regardless of current state
    hall = sorted(streaks.items(), key=lambda x: -x[1]["longest"])[:5]
    if hall:
        line = " · ".join(f"**[@{a}](https://github.com/{a})** — {s['longest']} days" for a,s in hall)
        print(f"🏆 **Longest streaks (last 365 days)**: {line}")
        print()

def render_md_table(stats, top_n):
    """Emit one markdown leaderboard table from author stats."""
    if not stats:
        print("_No authored commits in this window._")
        return
    print("| # | Engineer | Commits | + | − | Net | Repos |")
    print("|---:|---|---:|---:|---:|---:|---:|")
    ranked = sorted(stats.items(), key=lambda x: -(x[1]["add"]+x[1]["del"]))
    for i, (a, s) in enumerate(ranked[:top_n], 1):
        net  = s["add"] - s["del"]
        sign = "+" if net >= 0 else ""
        print(f"| {i} | [@{a}](https://github.com/{a}) | {s['commits']} | {s['add']:,} | {s['del']:,} | {sign}{net:,} | {len(s['repos'])} |")
    tot_lines   = sum(s['add']+s['del'] for s in stats.values())
    tot_commits = sum(s['commits']      for s in stats.values())
    tot_repos   = len({r for s in stats.values() for r in s["repos"]})
    print()
    print(f"**Totals**: {len(stats)} engineers · {tot_commits} commits · {tot_lines:,} authored lines across {tot_repos} repos.")

def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=1,
                   help="Window size in days back from today (default 1 = yesterday)")
    p.add_argument("--since", help="Window start (YYYY-MM-DD), overrides --days")
    p.add_argument("--until", help="Window end (YYYY-MM-DD), overrides --days")
    p.add_argument("--top", type=int, default=50, help="Limit leaderboard rows")
    p.add_argument("--drill", type=int, default=0,
                   help="Show biggest commits for top N authors")
    p.add_argument("--include-bots", action="store_true",
                   help="Don't filter known bot accounts")
    p.add_argument("--include-vendor", action="store_true",
                   help="Don't filter vendor/lockfile/binary lines")
    p.add_argument("--include-merges", action="store_true",
                   help="Count merge commits (default: skip — credit goes to original branch authors)")
    p.add_argument("--main-only", action="store_true",
                   help="Scan only the default branch (faster, but misses unmerged feature-branch work)")
    p.add_argument("--md", action="store_true",
                   help="Emit Markdown (for org profile README); leaderboard only")
    p.add_argument("--orgs", default=",".join(DEFAULT_ORGS),
                   help="Comma-separated orgs to scan")
    return p.parse_args()

def main():
    args = parse_args()
    # Calendar-day window. --md mode forces a 365-day scan so we can render
    # daily + weekly + monthly + yearly leaderboards from a single fetch.
    if args.since and args.until:
        since = args.since + "T00:00:00Z"
        until = args.until + "T00:00:00Z"
    else:
        today = datetime.now(timezone.utc).date()
        until = today.isoformat() + "T00:00:00Z"
        scan_days = 365 if args.md else args.days
        since = (today - timedelta(days=scan_days)).isoformat() + "T00:00:00Z"
    orgs = [o.strip() for o in args.orgs.split(",") if o.strip()]
    print(f"Window: {since}  →  {until}", file=sys.stderr)
    print(f"Orgs: {', '.join(orgs)}", file=sys.stderr)

    author_stats = defaultdict(lambda: {"commits":0,"add":0,"del":0,
                                        "repos":set(),"ignored_add":0,"ignored_del":0,
                                        "commit_records":[]})
    ignored_breakdown = defaultdict(int)
    all_commit_records = []  # flat list for windowed re-aggregation in --md mode

    for org in orgs:
        set_token(token_for_org(org))
        repos_raw = subprocess.run(
            ["gh","api",f"orgs/{org}/repos","--paginate","-q",".[].name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **({"GH_TOKEN":_CURRENT_TOKEN} if _CURRENT_TOKEN else {})}
        ).stdout.strip()
        if not repos_raw:
            print(f"[{org}] no repos (auth issue?)", file=sys.stderr)
            continue
        repos = repos_raw.split("\n")
        print(f"[{org}] scanning {len(repos)} repos…", file=sys.stderr)

        for i, repo in enumerate(repos, 1):
            repo_qualified = f"{org}/{repo}"
            print(f"  [{i}/{len(repos)}] {repo_qualified}", file=sys.stderr)
            # Gather candidate branches
            if args.main_only:
                branches = [None]  # gh defaults to default branch when sha omitted
            else:
                br_raw = gh(f"repos/{org}/{repo}/branches?per_page=100")
                try:
                    branches = [b["name"] for b in json.loads(br_raw)] if br_raw else [None]
                except Exception:
                    branches = [None]
                if not branches: branches = [None]
            # Dedupe commits by sha across branches
            seen = {}
            for br in branches:
                q = f"since={since}&until={until}&per_page=100"
                if br: q += f"&sha={br}"
                out = gh(f"repos/{org}/{repo}/commits?{q}")
                if not out: continue
                try:
                    cs = json.loads(out)
                except Exception:
                    continue
                if not isinstance(cs, list): continue
                for c in cs:
                    if c["sha"] not in seen:
                        seen[c["sha"]] = c
            for sha, c in seen.items():
                # Skip merge commits unless explicitly included
                if not args.include_merges and len(c.get("parents", [])) > 1:
                    continue
                login = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
                login = IDENTITY_ALIASES.get(login, login)
                msg = c["commit"]["message"].splitlines()[0][:80]
                detail_raw = gh(f"repos/{org}/{repo}/commits/{sha}")
                if not detail_raw: continue
                try:
                    detail = json.loads(detail_raw)
                except Exception:
                    continue
                files = detail.get("files",[])
                real_add = real_del = ign_add = ign_del = 0
                top_files = []
                for f in files:
                    fa = f.get("additions",0); fd = f.get("deletions",0)
                    fn = f.get("filename","")
                    reason = "" if args.include_vendor else is_ignored(fn)
                    if reason:
                        ign_add += fa; ign_del += fd
                        ignored_breakdown[reason] += fa + fd
                    else:
                        real_add += fa; real_del += fd
                        top_files.append((fa+fd, fa, fd, fn))
                s = author_stats[login]
                s["commits"] += 1
                s["add"] += real_add; s["del"] += real_del
                s["ignored_add"] += ign_add; s["ignored_del"] += ign_del
                s["repos"].add(repo_qualified)
                s["commit_records"].append({
                    "repo":repo_qualified,"sha":sha[:7],"msg":msg,
                    "add":real_add,"del":real_del,"top_files":top_files,
                })
                all_commit_records.append({
                    "repo": repo_qualified, "sha": sha, "login": login,
                    "date": c["commit"]["author"]["date"],
                    "add": real_add, "del": real_del,
                })

    if not args.include_bots:
        for b in list(author_stats):
            if b in BOTS or b.lower().endswith("[bot]") or b.lower() == "bot":
                del author_stats[b]
        all_commit_records = [
            c for c in all_commit_records
            if c["login"] not in BOTS
            and not c["login"].lower().endswith("[bot]")
            and c["login"].lower() != "bot"
        ]

    ranked = sorted(author_stats.items(), key=lambda x: -(x[1]["add"]+x[1]["del"]))

    if args.md:
        # Markdown render — for org-profile README.
        # Daily = primary view. Weekly + monthly tucked into <details> below.
        now_utc   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        today_utc = datetime.now(timezone.utc).date()
        yest_str  = (today_utc - timedelta(days=1)).isoformat()
        week_start  = (today_utc - timedelta(days=7)).isoformat()
        month_start = (today_utc - timedelta(days=30)).isoformat()

        year_start  = (today_utc - timedelta(days=365)).isoformat()

        daily_cut   = datetime.combine(today_utc - timedelta(days=1),   datetime.min.time(), tzinfo=timezone.utc)
        weekly_cut  = datetime.combine(today_utc - timedelta(days=7),   datetime.min.time(), tzinfo=timezone.utc)
        monthly_cut = datetime.combine(today_utc - timedelta(days=30),  datetime.min.time(), tzinfo=timezone.utc)
        yearly_cut  = datetime.combine(today_utc - timedelta(days=365), datetime.min.time(), tzinfo=timezone.utc)

        daily_stats   = aggregate_by_author(all_commit_records, daily_cut)
        weekly_stats  = aggregate_by_author(all_commit_records, weekly_cut)
        monthly_stats = aggregate_by_author(all_commit_records, monthly_cut)
        yearly_stats  = aggregate_by_author(all_commit_records, yearly_cut)

        print("<!-- AUTOGENERATED — DO NOT EDIT. See team-pulse repo. -->")
        print()
        print("> [!CAUTION]")
        print("> ## :rotating_light: NOT ON THIS LIST?")
        print(">")
        print("> **If your name is NOT on the leaderboard below, be very careful about your appraisal and future layoffs.**")
        print(">")
        print("> **This data is used to analyze appraisal requests for programmers and developers.**")
        print(">")
        print("> **Code sitting on your laptop, not pushed to GitHub daily, is invisible here — and useless to the organization. Push every day.**")
        print(">")
        print("> Updated nightly at 00:00 UTC. Real shipped code only — merge commits, vendor code, lockfiles, and binaries are excluded. Tests count. AI-assisted code counts. All branches scanned.")
        print()
        # Daily — primary view
        print(f"# 🏆 Top programmers — yesterday ({yest_str} UTC)")
        print()
        render_md_table(daily_stats, args.top)
        print()
        # Shipping streaks — surfaces habits, not single-day spikes
        streaks = compute_streaks(all_commit_records, today_utc)
        print("## 🔥 Active shipping streaks")
        print()
        print("Consecutive UTC days you've pushed code, ending yesterday. Skip a day → streak resets. Push every day.")
        print()
        render_md_streaks(streaks)
        # PR signals: first PRs + top reviewers (yesterday only — search API)
        try:
            first_prs, review_counts = find_pr_signals(orgs, today_utc)
            render_md_first_prs(first_prs)
            render_md_reviewers(review_counts)
        except Exception as e:
            print(f"<!-- PR signals skipped: {e} -->")
            print()
        # Weekly — expandable
        print(f"<details>")
        print(f"<summary><b>📅 Weekly view — last 7 days ({week_start} → {yest_str} UTC)</b></summary>")
        print()
        render_md_table(weekly_stats, args.top)
        print()
        print(f"</details>")
        print()
        # Monthly — expandable
        print(f"<details>")
        print(f"<summary><b>📆 Monthly view — last 30 days ({month_start} → {yest_str} UTC)</b></summary>")
        print()
        render_md_table(monthly_stats, args.top)
        print()
        print(f"</details>")
        print()
        # Yearly — expandable
        print(f"<details>")
        print(f"<summary><b>📈 Yearly view — last 365 days ({year_start} → {yest_str} UTC)</b></summary>")
        print()
        render_md_table(yearly_stats, args.top)
        print()
        print(f"</details>")
        print()
        print("---")
        print()
        print(f"_Last updated: {now_utc}. [Method](https://github.com/Globussoft-Technologies/team-pulse/blob/main/scripts/team_activity.py)._")
        return

    print("\n=== AUTHORED-CODE LEADERBOARD ===")
    if not args.include_vendor:
        print("(vendor / lockfile / binary / minified / generated paths excluded)")
    print(f"\n{'Author':<30} {'Commits':>7} {'Add':>7} {'Del':>7} {'Net':>8} {'Repos':>5}")
    print("-"*72)
    for a, s in ranked[:args.top]:
        net = s["add"]-s["del"]
        print(f"{a[:30]:<30} {s['commits']:>7} {s['add']:>7} {s['del']:>7} {net:>+8} {len(s['repos']):>5}")

    print(f"\nTotal authors: {len(author_stats)}")
    print(f"Total commits: {sum(s['commits'] for s in author_stats.values())}")
    print(f"Total authored lines: {sum(s['add']+s['del'] for s in author_stats.values()):,}")
    if ignored_breakdown:
        print(f"\nIgnored (filtered out):")
        for k,v in sorted(ignored_breakdown.items(), key=lambda x:-x[1]):
            print(f"  {k:<12} {v:>10,}")

    if args.drill > 0:
        print(f"\n=== DRILL: top {args.drill} authors — biggest commits ===")
        for a, s in ranked[:args.drill]:
            print(f"\n--- {a} ({s['commits']} commits, +{s['add']}/−{s['del']}) ---")
            big = sorted(s["commit_records"], key=lambda c:-(c["add"]+c["del"]))[:3]
            for c in big:
                print(f"  [{c['repo']}@{c['sha']}] +{c['add']}/-{c['del']}  {c['msg']}")
                tops = sorted(c["top_files"], reverse=True)[:5]
                for total, fa, fd, fn in tops:
                    print(f"      +{fa:>5}/-{fd:>5}  {fn}")

if __name__ == "__main__":
    main()
