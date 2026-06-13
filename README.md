# team-pulse

Org-wide daily team-activity reporter for [Globussoft Technologies](https://github.com/Globussoft-Technologies).

## What it does

Every day at **00:00 UTC**, a scheduled GitHub Actions workflow:

1. Scans every repo in the `Globussoft-Technologies` org
2. Pulls yesterday's commits across **all branches** (catches feature-branch work, not just merges to `main`)
3. Filters out: merge commits, vendor code, lockfiles, binaries, minified/generated paths, known bot accounts
4. Aggregates by author into a leaderboard
5. Updates the section between `<!-- LEADERBOARD:START -->` and `<!-- LEADERBOARD:END -->` in [Globussoft-Technologies/.github-private/profile/README.md](https://github.com/Globussoft-Technologies/.github-private/blob/main/profile/README.md)

The rendered page is visible to **org members only**.

## Files

| Path | Purpose |
|---|---|
| `.github/workflows/daily-report.yml` | The cron workflow (00:00 UTC daily + manual trigger) |
| `scripts/team_activity.py` | The scanner. Aggregates commits → markdown leaderboard. |
| `scripts/inject_report.py` | Idempotently replaces content between markers in a target file. |

## How to add a new identity alias

Some engineers commit under more than one GitHub account / git author. To merge them in the leaderboard, edit `IDENTITY_ALIASES` at the top of `scripts/team_activity.py`:

```python
IDENTITY_ALIASES = {
    "indianbill007": "sumitglobussoft",
    "Sumit Ghosh":   "sumitglobussoft",
    "alt-account":   "primary-account",
}
```

Then merge to `main`. Next run picks it up.

## Manual trigger

Actions tab → Daily team activity report → Run workflow.
