#!/usr/bin/env python
"""Globussoft-Technologies — authored-code report.

Requires: gh CLI authenticated (`gh auth login`).

Usage:
  python scripts/team_activity.py                  # yesterday
  python scripts/team_activity.py --days 7         # last 7 days
  python scripts/team_activity.py --since 2026-06-01 --until 2026-06-13
  python scripts/team_activity.py --days 1 --drill 3
  python scripts/team_activity.py --repos globusphone --days 2   # single repo
  python scripts/team_activity.py --md --cache-file commit-cache/details.json

Reliability: the scan is rate-limit-aware (waits + retries on 403/429),
paginates every list endpoint, and caches immutable per-SHA diff stats so
repeat runs stay well under GitHub's 5000 req/hr limit. Diffs are hydrated
newest-first, so daily/weekly/monthly windows are always complete even if the
deep-history tail is deferred to a later run.
"""
import argparse, subprocess, json, sys, os, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Orgs scanned by default. Override with --orgs A,B,C
DEFAULT_ORGS = ["Globussoft-Technologies", "EmpCloud", "Build-With-Sumit"]

# Bump whenever is_ignored() (the vendor/lockfile/binary filter) changes — it
# invalidates every cached per-commit diff stat so they get recomputed.
CACHE_VERSION = 1

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
    # This report's own daily README commit: its email
    # team-pulse@users.noreply.github.com resolves to the Team-Pulse account,
    # which then ranked on the board and held the longest streak.
    "Team-Pulse",
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

def _run_gh(args, token):
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    return subprocess.run(["gh","api",*args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)

def _rate_reset_seconds(token):
    """Seconds to wait for the core rate-limit to reset (0 if budget remains).
    The rate_limit endpoint itself does not count against the core budget."""
    r = _run_gh(["rate_limit"], token)
    try:
        core = json.loads(r.stdout)["resources"]["core"]
    except Exception:
        return 60
    if core.get("remaining", 0) > 0:
        return 0
    return max(5, int(core.get("reset", 0) - time.time()) + 5)

_THROTTLE = 0.0   # adaptive inter-request delay; ramps up after a secondary-limit hit

def gh(*args, retries=8):
    """`gh api` wrapper that SURVIVES rate limits instead of silently failing.

    The old version returned None on any non-zero exit, so a 403 (rate limit)
    made the caller drop the commit — which is exactly how active repos fell off
    the leaderboard. Here we distinguish the two GitHub limits:

      * PRIMARY  (core budget exhausted): the reset timestamp tells us exactly
        how long to wait, so we sleep until then.
      * SECONDARY / abuse (core budget still remaining, yet 403/429): there is
        no reset timestamp, so we back off exponentially (60s, 120s, …) and also
        ramp a small global inter-request throttle to stop re-tripping it.

    None is returned only after all retries fail on a genuine, non-rate error."""
    global _THROTTLE
    for attempt in range(retries):
        if _THROTTLE:
            time.sleep(_THROTTLE)
        r = _run_gh(list(args), _CURRENT_TOKEN)
        if r.returncode == 0:
            # Decay the throttle on sustained success so it spikes right after a
            # secondary-limit hit, then relaxes — keeps cold runs from paying a
            # flat per-call tax for their whole duration.
            if _THROTTLE:
                _THROTTLE = max(0.0, _THROTTLE - 0.05)
            return r.stdout
        err = (r.stderr or "").lower()
        is_rate = ("rate limit" in err or "secondary rate" in err
                   or "was submitted too quickly" in err or "429" in err
                   or "you have exceeded" in err)
        if is_rate:
            reset_wait = _rate_reset_seconds(_CURRENT_TOKEN)
            if reset_wait > 0:
                wait = reset_wait                       # primary: wait for core reset
                kind = "primary"
            else:
                wait = min(60 * (2 ** attempt), 900)    # secondary/abuse: exp. backoff
                _THROTTLE = min(_THROTTLE + 0.25, 1.0)   # slow the baseline rate (capped)
                kind = "secondary"
            wait = min(max(wait, 5), 3600)
            print(f"  {kind} rate limit — sleeping {wait}s then retrying "
                  f"(attempt {attempt+1}/{retries}, throttle={_THROTTLE:.2f}s)",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        # 404 / 409 / 422 give the same answer every time ("No common ancestor",
        # "Git Repository is empty"); retrying them only burns calls and sleep,
        # and on a repo like dominator (200+ unrelated branches) the burst trips
        # the secondary rate limit.
        final = any(f"(HTTP {c})" in (r.stderr or "") for c in (404, 409, 422))
        if attempt < retries - 1 and not final:
            time.sleep(2 * (attempt + 1))               # transient-error backoff
            continue
        print(f"  gh api error [{' '.join(str(a) for a in args)}]: "
              f"{(r.stderr or '').strip()[:160]}", file=sys.stderr)
        return None
    return None

def gh_paginate(path, per_page=100, extra=""):
    """Yield ALL items from a paginated list endpoint. The old code fetched a
    single per_page=100 page with no pagination, silently truncating any repo or
    branch with >100 commits in the window. `path` must not already contain a
    query string; pass query params via `extra`."""
    page = 1
    while True:
        q = f"{path}?per_page={per_page}&page={page}"
        if extra:
            q += f"&{extra}"
        out = gh(q)
        if not out:
            return
        try:
            items = json.loads(out)
        except Exception:
            return
        if not isinstance(items, list) or not items:
            return
        for it in items:
            yield it
        if len(items) < per_page:
            return
        page += 1

_HEADS_Q = """query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    refs(refPrefix:"refs/heads/",first:100,after:$cursor){
      pageInfo{hasNextPage endCursor}
      nodes{name target{oid ... on Commit{committedDate}}}
    }
  }
}"""

def gh_branch_heads(org, repo):
    """[(branch, head_sha, head_committed_date)] for every branch, 100 per
    GraphQL call; REST /branches gives no date. None if GraphQL fails, so the
    caller can fall back to REST."""
    heads, cursor = [], None
    while True:
        args = ["graphql", "-f", f"query={_HEADS_Q}",
                "-f", f"owner={org}", "-f", f"name={repo}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        out = gh(*args)
        try:
            refs = json.loads(out)["data"]["repository"]["refs"]
        except Exception:
            return None
        for n in refs.get("nodes") or []:
            t = n.get("target") or {}
            heads.append((n.get("name"), t.get("oid"), t.get("committedDate")))
        if not refs["pageInfo"]["hasNextPage"]:
            return heads
        cursor = refs["pageInfo"]["endCursor"]

def gh_compare_commits(org, repo, base_sha, head_sha, per_page=100):
    """Commits reachable from head_sha but not from base_sha: what a branch adds
    on top of the default branch. One call for a typical branch, where listing
    the branch re-reads the whole shared history (globussoft-crm has 300+
    branches; doing that for each one ran the scan past its 3h timeout).

    Returns None when the compare is unusable (no common ancestor, an error, or
    fewer commits than GitHub says the branch is ahead by) so the caller falls
    back to a full listing rather than undercount."""
    commits, ahead_by, page = [], None, 1
    while True:
        out = gh(f"repos/{org}/{repo}/compare/{base_sha}...{head_sha}"
                 f"?per_page={per_page}&page={page}")
        if not out:
            return None
        try:
            data = json.loads(out)
        except Exception:
            return None
        items = data.get("commits")
        if not isinstance(items, list):
            return None
        if ahead_by is None:
            ahead_by = data.get("ahead_by")
        commits.extend(items)
        if len(items) < per_page:
            break
        page += 1
    if ahead_by is not None and len(commits) != ahead_by:
        return None
    return commits

def load_cache(path):
    """Load the per-SHA diff-stat cache. Commit diffs are immutable, so once a
    SHA is fetched it never needs re-fetching — this is what keeps daily runs
    under the rate limit after the first warm-up."""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("v") == CACHE_VERSION:
                return d.get("c", {})
        except Exception:
            pass
    return {}

def save_cache(path, cache):
    if not path:
        return
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": CACHE_VERSION, "c": cache}, f)
    os.replace(tmp, path)

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
    p.add_argument("--repos", default="",
                   help="Comma-separated repo names to restrict the scan to "
                        "(useful for testing a single repo)")
    p.add_argument("--cache-file", default=os.environ.get("COMMIT_CACHE", ""),
                   help="JSON cache of per-SHA diff stats. Immutable by SHA, so "
                        "repeat runs only fetch NEW commits — keeps us under the "
                        "5000 req/hr limit. Env: COMMIT_CACHE")
    p.add_argument("--max-details", type=int,
                   default=int(os.environ.get("MAX_DETAILS", "3500") or "3500"),
                   help="Max FRESH per-commit diff fetches per run (rate-limit "
                        "budget). Newest commits fetched first; the deep-history "
                        "tail fills in across subsequent runs as the cache warms. "
                        "<=0 means unlimited (relies on rate-limit back-off).")
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

    only_repos = {r.strip() for r in args.repos.split(",") if r.strip()} or None

    # ── Phase 1: gather commit STUBS (cheap paginated LIST calls only) ─────────
    # A stub is everything known before the per-commit diff: sha, repo, author
    # login, author date, subject. Merge commits are dropped here. Deduped by
    # sha globally (SHAs are globally unique).
    #
    # The default branch is listed in full; every other branch contributes only
    # the commits it adds on top of it (gh_compare_commits). A branch whose head
    # was already reached by an earlier walk is skipped outright: its history is
    # a subset of that walk. Compare returns commits regardless of date, so the
    # window is applied here, on the committer date the commits API's
    # since/until filter on.
    stubs = {}
    seen = set()   # every sha already walked, merges included
    for org in orgs:
        set_token(token_for_org(org))
        repos = [r for r in gh_paginate(f"orgs/{org}/repos") if r.get("name")]
        if not repos:
            print(f"[{org}] no repos (auth issue?)", file=sys.stderr)
            continue
        if only_repos:
            repos = [r for r in repos if r["name"] in only_repos]
        print(f"[{org}] scanning {len(repos)} repos…", file=sys.stderr)

        for i, repo_obj in enumerate(repos, 1):
            repo = repo_obj["name"]
            default = repo_obj.get("default_branch")
            repo_qualified = f"{org}/{repo}"
            if args.main_only:
                branches = [(None, None)]  # gh defaults to default branch when sha omitted
            else:
                blist = gh_branch_heads(org, repo)
                if blist is None:
                    blist = [(b.get("name"), (b.get("commit") or {}).get("sha"), None)
                             for b in gh_paginate(f"repos/{org}/{repo}/branches")]
                # A head last committed before the window adds nothing to it
                # (0 commits lost on crm/adsgpt/videoraiq/dominator vs git log),
                # and comparing unrelated histories, which most of dominator's
                # 211 old branches are, trips GitHub's CPU-time rate limit.
                blist = [b for b in blist
                         if b[0] == default or not b[2] or b[2] >= since]
                blist.sort(key=lambda b: b[0] != default)   # default first
                branches = [(name, sha) for name, sha, _ in blist] or [(None, None)]
            print(f"  [{i}/{len(repos)}] {repo_qualified} ({len(branches)} branches)",
                  file=sys.stderr)
            base_sha = None
            for br, head in branches:
                if head and head in seen:
                    continue
                commits = None
                if base_sha and head:
                    commits = gh_compare_commits(org, repo, base_sha, head)
                in_window = commits is None   # a listing is already windowed
                if commits is None:
                    extra = f"since={since}&until={until}"
                    if br:
                        extra += f"&sha={br}"
                    commits = gh_paginate(f"repos/{org}/{repo}/commits", extra=extra)
                if br == default:
                    base_sha = head
                if head:
                    seen.add(head)   # another branch at this head adds nothing
                for c in commits:
                    sha = c.get("sha")
                    if not sha:
                        continue
                    seen.add(sha)
                    if sha in stubs:
                        continue
                    if not in_window:
                        cdate = ((c.get("commit") or {}).get("committer") or {}).get("date") or ""
                        if not (since <= cdate <= until):
                            continue
                    # Skip merge commits unless explicitly included
                    if not args.include_merges and len(c.get("parents", [])) > 1:
                        continue
                    login = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
                    login = IDENTITY_ALIASES.get(login, login)
                    msg = (c["commit"]["message"].splitlines() or [""])[0][:80]
                    stubs[sha] = {
                        "sha": sha, "org": org, "repo": repo_qualified,
                        "login": login, "date": c["commit"]["author"]["date"],
                        "msg": msg,
                    }

    # ── Phase 2: hydrate diffs — NEWEST FIRST, cache-backed, budget-bounded ────
    # Newest-first is the key correctness guarantee: even if we exhaust the API
    # budget on the deep (yearly) tail, the daily/weekly/monthly windows are
    # always fully hydrated. Deferred deep-history commits fill in on later runs
    # as the cache warms. --include-vendor changes the numbers, so it bypasses
    # the cache to avoid poisoning normal runs.
    use_cache = bool(args.cache_file) and not args.include_vendor
    cache = load_cache(args.cache_file) if use_cache else {}
    order = sorted(stubs.values(), key=lambda st: st["date"], reverse=True)
    cache_hits = fresh = deferred = failed = 0

    for st in order:
        sha, org, repo_qualified = st["sha"], st["org"], st["repo"]
        cached = cache.get(sha) if use_cache else None
        top_files = []
        if isinstance(cached, list) and len(cached) == 4:
            real_add, real_del, ign_add, ign_del = cached
            cache_hits += 1
        else:
            if args.max_details > 0 and fresh >= args.max_details:
                # Out of budget this run — skip (don't record) so numbers stay
                # clean; the cache warms and this commit is counted next run.
                deferred += 1
                continue
            set_token(token_for_org(org))
            repo_name = repo_qualified.split("/", 1)[1]
            detail_raw = gh(f"repos/{org}/{repo_name}/commits/{sha}")
            fresh += 1
            real_add = real_del = ign_add = ign_del = 0
            if not detail_raw:
                # Hard failure even after retries: record the commit with 0 lines
                # so the author still shows up (commit count) — never vanish.
                failed += 1
                print(f"  WARN diff fetch failed, counting 0 lines: "
                      f"{repo_qualified}@{sha[:7]}", file=sys.stderr)
            else:
                try:
                    detail = json.loads(detail_raw)
                except Exception:
                    detail = {}
                for f in detail.get("files", []):
                    fa = f.get("additions", 0); fd = f.get("deletions", 0)
                    fn = f.get("filename", "")
                    reason = "" if args.include_vendor else is_ignored(fn)
                    if reason:
                        ign_add += fa; ign_del += fd
                        ignored_breakdown[reason] += fa + fd
                    else:
                        real_add += fa; real_del += fd
                        top_files.append((fa+fd, fa, fd, fn))
                if use_cache:
                    cache[sha] = [real_add, real_del, ign_add, ign_del]

        login = st["login"]
        s = author_stats[login]
        s["commits"] += 1
        s["add"] += real_add; s["del"] += real_del
        s["ignored_add"] += ign_add; s["ignored_del"] += ign_del
        s["repos"].add(repo_qualified)
        s["commit_records"].append({
            "repo": repo_qualified, "sha": sha[:7], "msg": st["msg"],
            "add": real_add, "del": real_del, "top_files": top_files,
        })
        all_commit_records.append({
            "repo": repo_qualified, "sha": sha, "login": login,
            "date": st["date"], "add": real_add, "del": real_del,
        })

    if use_cache:
        save_cache(args.cache_file, cache)
    print(f"[hydrate] {len(order)} commits · {cache_hits} cached · {fresh} fetched "
          f"· {failed} failed · {deferred} deferred (cold-cache tail, fills next run)",
          file=sys.stderr)

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
