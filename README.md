# Diff Demon

Pull **every commit diff** from any GitHub repository into formats that an AI
agent (or a human) can easily search and reason about.

Diff Demon clones the repo locally with `git`, so there are **no API rate
limits** and you get **complete diffs** for the full history. From that it
produces structured, plain-text artifacts an LLM can grep, embed, or read
directly.

## Why

Use the pulled data to:

- Understand how a repo evolved over time (per-commit diffs + chronological index).
- Hunt for **secrets/credentials** accidentally committed at any point in history
  — including creds that were later *removed* but may still be live.
- Feed a focused, plain-text corpus to an AI agent instead of crawling the GitHub UI.

## Requirements

- Python 3.10+
- A local `git` install on your `PATH`

No third-party Python packages are needed.

## Usage

```powershell
# Full URL
python diff_demon.py https://github.com/<owner>/<repo>

# Shorthand owner/name
python diff_demon.py <owner>/<repo>

# Limit history size while experimenting
python diff_demon.py <owner>/<repo> --max-commits 200

# Custom output folder, skip secret scan
python diff_demon.py <repo> --output ./data --no-secrets
```

### Options

| Flag | Description |
| --- | --- |
| `-o, --output DIR` | Output directory (default `./output`). |
| `--cache DIR` | Where to cache the mirror clone (default: system temp). Re-runs fetch updates only. |
| `--branch REF` | Limit to a single branch/ref (default: all refs). |
| `--max-commits N` | Process at most N commits. |
| `--no-secrets` | Skip credential scanning. |
| `--no-markdown` | Skip per-commit Markdown + index (JSONL only). |
| `--clean` | Delete existing output for this repo before running. |

## Output layout

```
output/<owner>__<repo>/
├── commits.jsonl     # one JSON object per commit (metadata + full diff)
├── commits/          # one Markdown file per commit (00001_<sha>.md ...)
├── index.md          # chronological table of every commit
├── secrets.jsonl     # potential credential leaks (one JSON object per finding)
├── secrets.md        # human-readable secret-scan summary, grouped by severity
└── summary.json      # repo-wide stats + run metadata
```

### `commits.jsonl` schema (per line)

```json
{
  "index": 1,
  "sha": "…",
  "short_sha": "…",
  "author_name": "…",
  "author_email": "…",
  "author_date": "2020-01-01T12:00:00+00:00",
  "committer_name": "…",
  "committer_email": "…",
  "committer_date": "…",
  "parents": ["…"],
  "subject": "Commit subject line",
  "body": "Full commit body",
  "files_changed": 3,
  "additions": 42,
  "deletions": 7,
  "files": [
    {"path": "src/app.js", "old_path": null, "status": "modified", "additions": 10, "deletions": 2}
  ],
  "diff": "diff --git a/src/app.js b/src/app.js\n…"
}
```

## Feeding it to an AI agent

`commits.jsonl` is the primary AI-facing artifact — newline-delimited JSON is
trivial to stream, filter, and chunk. Some patterns:

- **"What changed over time?"** Point the agent at `index.md` for a map, then have
  it open specific `commits/NNNNN_<sha>.md` files for detail.
- **"Find committed credentials."** Start from `secrets.md` / `secrets.jsonl`, then
  pivot to the referenced commit for context. Findings are redacted previews — the
  full value is in the corresponding diff.
- **Programmatic filtering**, e.g. all commits touching auth code:

  ```powershell
  Get-Content output\<owner>__<repo>\commits.jsonl |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object { $_.diff -match '(?i)password|secret|token' } |
    Select-Object short_sha, author_date, subject
  ```

## Secret scanning notes

- Scans **both added and removed** diff lines. Each finding records a
  `change_type` of `added` or `removed`.
  - **added** — the moment a value entered the codebase.
  - **removed** — a credential that was deleted from the code. These are still
    reported because a deleted secret is often **never rotated**, so it may still
    be live.
- Regex heuristics cover AWS keys, GitHub/Slack/Stripe/Google/SendGrid/Twilio
  tokens, private-key blocks, JWTs, basic-auth URLs, generic `key=…`
  assignments, and DB connection-string passwords.
- Expect **false positives** — every finding should be reviewed. Matches are
  shown redacted in reports; the raw value is recoverable from the diff.
- A finding means the secret was present in history at some point; treat any real
  match as **potentially still live** and rotate it.

## How it works

1. Parse the repo spec (URL or `owner/name`) → clone URL.
2. `git clone --mirror` into a cache dir (or `git remote update` if cached).
3. A single `git log --reverse -p` pass streams every commit with its patch,
   delimited by control characters for reliable parsing; per-file status and
   add/del counts are derived from the patch.
4. Each commit is written to JSONL + Markdown, and added/removed lines are
   scanned for secrets.
