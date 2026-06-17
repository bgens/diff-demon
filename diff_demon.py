#!/usr/bin/env python3
"""
Diff Demon — Pull every commit diff from a GitHub repository into formats that
are easy for an AI agent (or a human) to search and reason about.

It clones the repository locally (so there are no API rate limits and you get
complete diffs), walks the full commit history, and emits:

  output/<repo>/commits.jsonl        One JSON object per commit (metadata + diff).
  output/<repo>/commits/*.md         One Markdown file per commit (AI/human friendly).
  output/<repo>/index.md             Chronological index of every commit.
  output/<repo>/secrets.jsonl        Potential leaked credentials found in diffs.
  output/<repo>/secrets.md           Human-readable secret-scan summary.
  output/<repo>/summary.json         Repo-wide statistics + run metadata.

Only the standard library is required (plus a local `git` install).

Examples
--------
  python diff_demon.py https://github.com/<owner>/<repo>
  python diff_demon.py <owner>/<repo> --max-commits 200
  python diff_demon.py <url> --output ./data --no-secrets
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

# Control characters used as field/record delimiters in the `git log` format.
# They effectively never appear in source code, so parsing stays reliable.
RS = "\x1e"  # record separator (between commits)
US = "\x1f"  # unit separator (between fields)
RECORD_TAG = f"{RS}COMMIT{US}"

# %H hash, %an/%ae author, %aI author date (ISO), %cn/%ce committer,
# %cI committer date, %P parents, %s subject, %b body.
LOG_FORMAT = (
    f"{RECORD_TAG}%H{US}%an{US}%ae{US}%aI{US}%cn{US}%ce{US}%cI{US}%P{US}%s{US}%b{RS}"
)


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command and return stdout as text, raising on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        sys.exit("error: `git` was not found on PATH. Install Git and try again.")


# ---------------------------------------------------------------------------
# repo spec parsing
# ---------------------------------------------------------------------------


@dataclass
class RepoSpec:
    owner: str
    name: str
    clone_url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}__{self.name}"


def parse_repo(spec: str) -> RepoSpec:
    """Accept a full GitHub URL, an SSH URL, or a bare `owner/name` slug."""
    spec = spec.strip()

    # owner/name shorthand
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", spec)
    if m:
        owner, name = m.group(1), m.group(2)
        name = re.sub(r"\.git$", "", name)
        return RepoSpec(owner, name, f"https://github.com/{owner}/{name}.git")

    # https / http URL
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", spec)
    if m:
        owner, name = m.group(1), m.group(2)
        return RepoSpec(owner, name, f"https://github.com/{owner}/{name}.git")

    raise ValueError(
        f"could not parse repository spec: {spec!r}. "
        "Use a GitHub URL or 'owner/name'."
    )


# ---------------------------------------------------------------------------
# clone / update cache
# ---------------------------------------------------------------------------


def clone_or_update(repo: RepoSpec, cache_dir: Path) -> Path:
    """Mirror-clone the repo into the cache (or fetch updates if present)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_path = cache_dir / f"{repo.slug}.git"

    if repo_path.exists():
        print(f"  updating cached clone: {repo_path}")
        try:
            _run_git(["remote", "update", "--prune"], cwd=repo_path)
            return repo_path
        except RuntimeError as exc:
            print(f"  warning: update failed ({exc}); re-cloning")
            shutil.rmtree(repo_path, ignore_errors=True)

    print(f"  cloning {repo.clone_url}")
    _run_git(["clone", "--mirror", "--quiet", repo.clone_url, str(repo_path)])
    return repo_path


# ---------------------------------------------------------------------------
# commit model + extraction
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    path: str
    old_path: str | None
    status: str  # added / modified / deleted / renamed / etc.
    additions: int
    deletions: int


@dataclass
class Commit:
    index: int
    sha: str
    short_sha: str
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str
    parents: list[str]
    subject: str
    body: str
    diff: str
    files: list[FileChange] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "sha": self.sha,
            "short_sha": self.short_sha,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "author_date": self.author_date,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "committer_date": self.committer_date,
            "parents": self.parents,
            "subject": self.subject,
            "body": self.body,
            "files_changed": len(self.files),
            "additions": self.additions,
            "deletions": self.deletions,
            "files": [
                {
                    "path": f.path,
                    "old_path": f.old_path,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                }
                for f in self.files
            ],
            "diff": self.diff,
        }


def _strip_prefix(path: str) -> str:
    """Drop the leading a/ or b/ that git puts on diff header paths."""
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_patch(diff: str) -> list[FileChange]:
    """Derive per-file status and add/del counts directly from a unified diff."""
    changes: list[FileChange] = []
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    for section in sections:
        if not section.startswith("diff --git "):
            continue
        lines = section.splitlines()
        header = lines[0]

        status = "modified"
        old_path: str | None = None
        rename_to: str | None = None
        minus_path: str | None = None
        plus_path: str | None = None
        adds = dels = 0
        in_hunk = False

        for line in lines[1:]:
            if line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "deleted"
            elif line.startswith("rename from "):
                status = "renamed"
                old_path = line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                rename_to = line[len("rename to "):].strip()
            elif line.startswith("copy from "):
                status = "copied"
                old_path = line[len("copy from "):].strip()
            elif line.startswith("copy to "):
                rename_to = line[len("copy to "):].strip()
            elif line.startswith("--- "):
                minus_path = _strip_prefix(line[4:].strip())
            elif line.startswith("+++ "):
                plus_path = _strip_prefix(line[4:].strip())
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk:
                if line.startswith("+"):
                    adds += 1
                elif line.startswith("-"):
                    dels += 1

        # Resolve the canonical path for this file change.
        if plus_path and plus_path != "/dev/null":
            path = plus_path
        elif rename_to:
            path = rename_to
        elif minus_path and minus_path != "/dev/null":
            path = minus_path
        else:
            m = re.match(r"diff --git a/(.+?) b/(.+)$", header)
            path = m.group(2) if m else header

        if old_path is None and minus_path and minus_path != "/dev/null" and status in ("renamed", "copied"):
            old_path = minus_path

        changes.append(FileChange(path, old_path, status, adds, dels))
    return changes


def iter_commits(
    repo_path: Path, branch: str | None, max_commits: int | None
) -> Iterator[Commit]:
    """Stream commits (oldest-first) with full diffs from a single git log pass."""
    args = [
        "log",
        "--reverse",            # oldest first -> natural chronological reading
        "--no-color",
        "--no-merges",          # diffs for merges are noisy/ambiguous; skip
        f"--format={LOG_FORMAT}",
        "-p",                   # include patch (stats are derived from it)
    ]
    if max_commits:
        args.append(f"--max-count={max_commits}")
    args.append(branch if branch else "--all")

    raw = _run_git(args, cwd=repo_path)
    records = raw.split(RECORD_TAG)

    index = 0
    for rec in records:
        if not rec.strip():
            continue
        header, _, rest = rec.partition(RS)
        fields = header.split(US)
        if len(fields) < 10:
            continue
        (
            sha,
            an,
            ae,
            aI,
            cn,
            ce,
            cI,
            parents,
            subject,
            body,
        ) = fields[:10]

        # With `-p` only, `rest` is just the patch (possibly empty).
        diff = rest.lstrip("\n")
        files = _parse_patch(diff)

        index += 1
        yield Commit(
            index=index,
            sha=sha,
            short_sha=sha[:10],
            author_name=an,
            author_email=ae,
            author_date=aI,
            committer_name=cn,
            committer_email=ce,
            committer_date=cI,
            parents=parents.split() if parents else [],
            subject=subject,
            body=body.strip("\n"),
            diff=diff.strip("\n"),
            files=files,
        )


# ---------------------------------------------------------------------------
# secret / credential scanning
# ---------------------------------------------------------------------------


@dataclass
class SecretRule:
    name: str
    pattern: re.Pattern
    severity: str


def _build_secret_rules() -> list[SecretRule]:
    raw_rules: list[tuple[str, str, str]] = [
        ("AWS Access Key ID", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b", "high"),
        ("AWS Secret Access Key", r"(?i)aws.{0,20}?(?:secret|key).{0,5}['\"=:\s]([0-9a-zA-Z/+]{40})\b", "high"),
        ("GitHub Token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,255}\b", "high"),
        ("GitHub Fine-grained PAT", r"\bgithub_pat_[0-9A-Za-z_]{22,255}\b", "high"),
        ("Google API Key", r"\bAIza[0-9A-Za-z\-_]{35}\b", "high"),
        ("Slack Token", r"\bxox[baprs]-[0-9A-Za-z-]{10,72}\b", "high"),
        ("Slack Webhook", r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+", "medium"),
        ("Stripe Secret Key", r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b", "high"),
        ("Twilio API Key", r"\bSK[0-9a-fA-F]{32}\b", "high"),
        ("SendGrid API Key", r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b", "high"),
        ("npm Token", r"\bnpm_[0-9A-Za-z]{36}\b", "high"),
        ("Private Key Block", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----", "high"),
        ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "medium"),
        ("Generic API Key Assignment", r"(?i)\b(?:api[_-]?key|apikey|secret|token|passwd|password|pwd|access[_-]?token)\b\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]", "medium"),
        ("Bearer Token", r"(?i)\bbearer\s+[0-9A-Za-z._\-]{20,}\b", "low"),
        ("Connection String Password", r"(?i)(?:password|pwd)=[^;'\"\s]{4,}", "medium"),
        ("Basic Auth in URL", r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@[^/\s]+", "high"),
        ("Heroku API Key", r"(?i)heroku.{0,20}?\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "medium"),
    ]
    return [SecretRule(n, re.compile(p), s) for n, p, s in raw_rules]


def _redact(value: str, keep: int = 4) -> str:
    value = value.strip()
    if len(value) <= keep * 2:
        return value[:keep] + "…"
    return f"{value[:keep]}…{value[-keep:]} (len={len(value)})"


def scan_commit_for_secrets(commit: Commit, rules: list[SecretRule]) -> list[dict]:
    """Scan both *added* and *removed* diff lines for credential-like content.

    Removed lines matter: a credential that was deleted from the code may still
    be live (it was never rotated), so leaks in `-` lines are reported too.
    """
    findings: list[dict] = []
    current_file = "?"
    for line in commit.diff.splitlines():
        if line.startswith("diff --git "):
            # `diff --git a/path b/path`
            m = re.search(r" b/(.+)$", line)
            current_file = m.group(1) if m else "?"
            continue
        # Skip file headers and hunk markers (these start with +++/---/@@).
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            change_type = "added"
        elif line.startswith("-"):
            change_type = "removed"
        else:
            continue
        content = line[1:]
        for rule in rules:
            m = rule.pattern.search(content)
            if not m:
                continue
            matched = m.group(1) if m.groups() else m.group(0)
            findings.append(
                {
                    "rule": rule.name,
                    "severity": rule.severity,
                    "change_type": change_type,
                    "sha": commit.sha,
                    "short_sha": commit.short_sha,
                    "author": f"{commit.author_name} <{commit.author_email}>",
                    "date": commit.author_date,
                    "file": current_file,
                    "match_redacted": _redact(matched),
                    "line_preview": content.strip()[:200],
                }
            )
    return findings


# ---------------------------------------------------------------------------
# output writers
# ---------------------------------------------------------------------------


def _md_escape(text: str) -> str:
    return text.replace("\r", "")


def write_commit_markdown(commit: Commit, out_dir: Path) -> Path:
    fname = f"{commit.index:05d}_{commit.short_sha}.md"
    path = out_dir / fname
    files_table = "\n".join(
        f"| `{f.path}` | {f.status} | +{f.additions} | -{f.deletions} |"
        for f in commit.files
    ) or "| _(no file changes detected)_ | | | |"

    body = commit.body.strip()
    body_section = f"\n\n{_md_escape(body)}\n" if body else "\n"

    content = f"""# Commit {commit.short_sha} — {_md_escape(commit.subject)}

- **Full SHA:** `{commit.sha}`
- **Author:** {commit.author_name} <{commit.author_email}>
- **Author date:** {commit.author_date}
- **Committer:** {commit.committer_name} <{commit.committer_email}>
- **Committer date:** {commit.committer_date}
- **Parents:** {", ".join(f"`{p[:10]}`" for p in commit.parents) or "_(root commit)_"}
- **Files changed:** {len(commit.files)} (+{commit.additions} / -{commit.deletions})

## Message
{body_section}
## Files

| File | Status | Added | Removed |
| --- | --- | --- | --- |
{files_table}

## Diff

```diff
{_md_escape(commit.diff)}
```
"""
    path.write_text(content, encoding="utf-8")
    return path


def write_index(commits: list[dict], repo: RepoSpec, out_dir: Path) -> None:
    lines = [
        f"# Commit index — {repo.owner}/{repo.name}",
        "",
        f"Total commits: **{len(commits)}**",
        "",
        "| # | Date | SHA | Author | Files | +/- | Subject |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in commits:
        rel = f"commits/{c['index']:05d}_{c['short_sha']}.md"
        subj = c["subject"].replace("|", "\\|")[:80]
        lines.append(
            f"| {c['index']} | {c['author_date'][:10]} | "
            f"[`{c['short_sha']}`]({rel}) | "
            f"{c['author_name'].replace('|', '\\|')} | {c['files_changed']} | "
            f"+{c['additions']}/-{c['deletions']} | {subj} |"
        )
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_secrets_markdown(findings: list[dict], repo: RepoSpec, out_dir: Path) -> None:
    by_sev = {"high": [], "medium": [], "low": []}
    for f in findings:
        by_sev.setdefault(f["severity"], []).append(f)

    lines = [
        f"# Potential secrets — {repo.owner}/{repo.name}",
        "",
        f"Total findings: **{len(findings)}** "
        f"(high: {len(by_sev['high'])}, medium: {len(by_sev['medium'])}, low: {len(by_sev['low'])})",
        "",
        "> These are regex-based heuristics. Review each finding; expect false positives.",
        "> A match means the value appeared in a diff line at some point in history.",
        "> **Added** lines show when a secret entered the code; **removed** lines are",
        "> still reported because a deleted credential may never have been rotated and",
        "> could still be live.",
        "",
    ]
    for sev in ("high", "medium", "low"):
        items = by_sev[sev]
        if not items:
            continue
        lines.append(f"## {sev.title()} severity ({len(items)})")
        lines.append("")
        lines.append("| Rule | Change | SHA | File | Match | Author | Date |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for f in items:
            rel = f"commits/{_commit_link_for(f, out_dir)}"
            lines.append(
                f"| {f['rule']} | {f.get('change_type', 'added')} | "
                f"[`{f['short_sha']}`]({rel}) | `{f['file']}` | "
                f"`{f['match_redacted']}` | {f['author']} | {f['date'][:10]} |"
            )
        lines.append("")
    (out_dir / "secrets.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


_LINK_CACHE: dict[str, str] = {}


def _commit_link_for(finding: dict, out_dir: Path) -> str:
    # Best-effort link to the per-commit markdown file by short sha.
    short = finding["short_sha"]
    if short in _LINK_CACHE:
        return _LINK_CACHE[short]
    matches = list((out_dir / "commits").glob(f"*_{short}.md"))
    rel = matches[0].name if matches else f"../{short}"
    _LINK_CACHE[short] = rel
    return rel


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    ensure_git_available()
    repo = parse_repo(args.repo)
    print(f"Repository: {repo.owner}/{repo.name}")

    out_dir = Path(args.output) / repo.slug
    commits_dir = out_dir / "commits"
    if out_dir.exists() and args.clean:
        shutil.rmtree(out_dir)
    commits_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache) if args.cache else Path(tempfile.gettempdir()) / "github_diff_cache"
    repo_path = clone_or_update(repo, cache_dir)

    rules = _build_secret_rules() if not args.no_secrets else []

    started = time.time()
    commit_dicts: list[dict] = []
    all_findings: list[dict] = []

    jsonl_path = out_dir / "commits.jsonl"
    print("Extracting commits...")
    with jsonl_path.open("w", encoding="utf-8") as jf:
        for commit in iter_commits(repo_path, args.branch, args.max_commits):
            d = commit.to_dict()
            jf.write(json.dumps(d, ensure_ascii=False) + "\n")
            commit_dicts.append(
                {k: d[k] for k in (
                    "index", "sha", "short_sha", "author_name", "author_date",
                    "subject", "files_changed", "additions", "deletions",
                )}
            )
            if not args.no_markdown:
                write_commit_markdown(commit, commits_dir)
            if rules:
                all_findings.extend(scan_commit_for_secrets(commit, rules))
            if commit.index % 100 == 0:
                print(f"  ...{commit.index} commits")

    print(f"  extracted {len(commit_dicts)} commits")

    if not args.no_markdown:
        write_index(commit_dicts, repo, out_dir)

    if rules:
        with (out_dir / "secrets.jsonl").open("w", encoding="utf-8") as sf:
            for f in all_findings:
                sf.write(json.dumps(f, ensure_ascii=False) + "\n")
        write_secrets_markdown(all_findings, repo, out_dir)
        print(f"  secret findings: {len(all_findings)}")

    elapsed = time.time() - started
    summary = {
        "repository": f"{repo.owner}/{repo.name}",
        "clone_url": repo.clone_url,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit_count": len(commit_dicts),
        "total_additions": sum(c["additions"] for c in commit_dicts),
        "total_deletions": sum(c["deletions"] for c in commit_dicts),
        "secret_findings": len(all_findings),
        "secret_findings_by_severity": _count_by(all_findings, "severity"),
        "branch": args.branch or "all",
        "elapsed_seconds": round(elapsed, 2),
        "outputs": {
            "commits_jsonl": str(jsonl_path.name),
            "commits_markdown_dir": "commits/",
            "index": "index.md",
            "secrets_jsonl": "secrets.jsonl" if rules else None,
            "secrets_markdown": "secrets.md" if rules else None,
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\nDone in {elapsed:.1f}s. Output written to: {out_dir}")
    print(f"  - commits.jsonl ({len(commit_dicts)} records)")
    if not args.no_markdown:
        print("  - commits/*.md + index.md")
    if rules:
        print(f"  - secrets.jsonl / secrets.md ({len(all_findings)} findings)")
    print("  - summary.json")
    return 0


def _count_by(items: Iterable[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it[key]] = out.get(it[key], 0) + 1
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diff_demon",
        description="Diff Demon — pull all GitHub commit diffs into AI-searchable formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("repo", help="GitHub URL or 'owner/name' (e.g. <owner>/<repo>)")
    p.add_argument("-o", "--output", default="output", help="Output directory (default: ./output)")
    p.add_argument("--cache", default=None, help="Directory for cached clones (default: system temp)")
    p.add_argument("--branch", default=None, help="Limit to a branch/ref (default: all refs)")
    p.add_argument("--max-commits", type=int, default=None, help="Limit number of commits processed")
    p.add_argument("--no-secrets", action="store_true", help="Skip credential scanning")
    p.add_argument("--no-markdown", action="store_true", help="Skip per-commit Markdown + index")
    p.add_argument("--clean", action="store_true", help="Delete existing output for this repo first")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
