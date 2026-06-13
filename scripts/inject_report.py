#!/usr/bin/env python
"""Replace content between LEADERBOARD:START/END markers in a target file.

Usage:
  python scripts/inject_report.py <target_md> <source_md>
"""
import sys, pathlib, re

START = "<!-- LEADERBOARD:START -->"
END   = "<!-- LEADERBOARD:END -->"

def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr); sys.exit(2)
    target = pathlib.Path(sys.argv[1])
    source = pathlib.Path(sys.argv[2])
    body = source.read_text(encoding="utf-8").strip()
    txt  = target.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    new_block = f"{START}\n{body}\n{END}"
    if pattern.search(txt):
        out = pattern.sub(new_block, txt)
    else:
        # No markers yet — prepend block to file
        out = new_block + "\n\n" + txt
    target.write_text(out, encoding="utf-8")
    print(f"Updated {target}")

if __name__ == "__main__":
    main()
