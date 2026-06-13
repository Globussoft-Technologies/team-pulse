#!/usr/bin/env python
"""Globussoft-Technologies — authored-code report.

Requires: gh CLI authenticated (`gh auth login`).

Usage:
  python scripts/team_activity.py                  # yesterday
  python scripts/team_activity.py --days 7         # last 7 days
  python scripts/team_activity.py --since 2026-06-01 --until 2026-06-13
  python scripts/team_activity.py --days 1 --drill 3
"""
import argparse, subprocess, json, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ORG = "Globussoft-Technologies"

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
    "indianbill007": "sumitglobussoft",
    "Sumit Ghosh":   "sumitglobussoft",
}

def is_ignored(path):
    base = path.rsplit("/",1)[-1].lower()
    p_l = path.lower()
    if base in {f.lower() for f in LOCK_FILES}: return "lockfile"
    for d in VENDOR_DIRS:
        if d in "/" + p_l: return "vendor"
    if any(p_l.endswith(ext) for ext in BINARY_EXTS): return "binary"
    if any(p_l.endswith(m) for m in MINIFIED):       return "minified"
    if any(h in p_l for h in GENERATED_HINTS):       return "generated"
    if p_l.endswith(".map"):                          return "sourcemap"
    return ""

def gh(*args):
    r = subprocess.run(["gh","api",*args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
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
    return p.parse_args()

def main():
    args = parse_args()
    # Calendar-day window. --md mode forces a 30-day scan so we can render
    # daily + weekly + monthly leaderboards from a single fetch.
    if args.since and args.until:
        since = args.since + "T00:00:00Z"
        until = args.until + "T00:00:00Z"
    else:
        today = datetime.now(timezone.utc).date()
        until = today.isoformat() + "T00:00:00Z"
        scan_days = 30 if args.md else args.days
        since = (today - timedelta(days=scan_days)).isoformat() + "T00:00:00Z"
    print(f"Window: {since}  →  {until}", file=sys.stderr)
    print(f"Org: {ORG}", file=sys.stderr)

    repos = subprocess.run(
        ["gh","api",f"orgs/{ORG}/repos","--paginate","-q",".[].name"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip().split("\n")
    print(f"Scanning {len(repos)} repos…\n", file=sys.stderr)

    author_stats = defaultdict(lambda: {"commits":0,"add":0,"del":0,
                                        "repos":set(),"ignored_add":0,"ignored_del":0,
                                        "commit_records":[]})
    ignored_breakdown = defaultdict(int)
    all_commit_records = []  # flat list for windowed re-aggregation in --md mode

    for i, repo in enumerate(repos, 1):
        print(f"[{i}/{len(repos)}] {repo}", file=sys.stderr)
        # Gather candidate branches
        if args.main_only:
            branches = [None]  # gh defaults to default branch when sha omitted
        else:
            br_raw = gh(f"repos/{ORG}/{repo}/branches?per_page=100")
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
            out = gh(f"repos/{ORG}/{repo}/commits?{q}")
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
            detail_raw = gh(f"repos/{ORG}/{repo}/commits/{sha}")
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
            s["repos"].add(repo)
            s["commit_records"].append({
                "repo":repo,"sha":sha[:7],"msg":msg,
                "add":real_add,"del":real_del,"top_files":top_files,
            })
            all_commit_records.append({
                "repo": repo, "sha": sha, "login": login,
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
        # Markdown render — for org-profile README. Three leaderboards from one scan.
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        today_utc = datetime.now(timezone.utc).date()
        print("<!-- AUTOGENERATED — DO NOT EDIT. See team-pulse repo. -->")
        print()
        print("> [!CAUTION]")
        print("> ## :rotating_light: NOT ON THESE LISTS?")
        print(">")
        print("> **If your name is NOT on any leaderboard below, be very careful about your appraisal and future layoffs.**")
        print(">")
        print("> Updated nightly at 00:00 UTC. Real authored code only — merge commits, vendor code, lockfiles, binaries, and generated files are excluded. All branches across the org are scanned, so feature-branch work counts.")
        print()
        for label, n_days in (("Yesterday", 1), ("Last 7 days", 7), ("Last 30 days", 30)):
            if n_days == 1:
                window_str = (today_utc - timedelta(days=1)).isoformat()
                title = f"🏆 Top programmers — yesterday ({window_str} UTC)"
            else:
                start_str = (today_utc - timedelta(days=n_days)).isoformat()
                end_str   = (today_utc - timedelta(days=1)).isoformat()
                title = f"📅 Top programmers — last {n_days} days ({start_str} → {end_str} UTC)"
            cutoff = datetime.combine(today_utc - timedelta(days=n_days),
                                      datetime.min.time(), tzinfo=timezone.utc)
            stats = aggregate_by_author(all_commit_records, cutoff)
            print(f"## {title}")
            print()
            if not stats:
                print("_No authored commits in this window._")
                print()
                print("---")
                print()
                continue
            print("| # | Engineer | Commits | + | − | Net | Repos |")
            print("|---:|---|---:|---:|---:|---:|---:|")
            window_ranked = sorted(stats.items(), key=lambda x: -(x[1]["add"]+x[1]["del"]))
            for i, (a, s) in enumerate(window_ranked[:args.top], 1):
                net = s["add"] - s["del"]
                sign = "+" if net >= 0 else ""
                print(f"| {i} | [@{a}](https://github.com/{a}) | {s['commits']} | {s['add']:,} | {s['del']:,} | {sign}{net:,} | {len(s['repos'])} |")
            tot_lines   = sum(s['add']+s['del'] for s in stats.values())
            tot_commits = sum(s['commits']      for s in stats.values())
            tot_repos   = len({r for s in stats.values() for r in s["repos"]})
            print()
            print(f"**Totals**: {len(stats)} engineers · {tot_commits} commits · {tot_lines:,} authored lines across {tot_repos} repos.")
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
