#!/usr/bin/env python3
"""Release planning for the release-pr skill and the release workflow.

Subcommands:
  plan    build the release PR title and body from MAIN..DEV (reads git and gh)
  branch  create release/vX.Y.Z at a commit on GitHub, never overwriting
  notes   extract release notes from a release PR body (pure)

Standard library only, Python 3.8+.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TITLE_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<bang>!)?: (?P<desc>.+)$"
)
ISSUE_KEY_RE = re.compile(r"^\[(SI-\d+)\]\s*", re.IGNORECASE)
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
SQUASH_PR_RE = re.compile(r"\(#(\d+)\)$")
KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
CLOSING_RES = [
    re.compile(r"\b" + KEYWORD + r":?\s+#(\d+)\b", re.IGNORECASE),
    re.compile(r"\b" + KEYWORD + r":?\s+SI-\d+\s+\(#(\d+)\)", re.IGNORECASE),
    re.compile(r"\bpart\s+of\s+SI-\d+\s+\(#(\d+)\)", re.IGNORECASE),
]
MENTION_RE = re.compile(r"(?<![\w&/])#(\d+)\b")
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
BREAKING_SECTION_RE = re.compile(r"^##\s+Breaking changes\s*$", re.IGNORECASE)
NOTES_START = "<!-- release-notes:start -->"
NOTES_END = "<!-- release-notes:end -->"
SECTIONS = [
    ("breaking", "Breaking changes"),
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("other", "Other"),
]


class ReleaseError(Exception):
    """Invalid input or a failed command; reported as one line."""


def run(args):
    """Default runner: (returncode, stdout, stderr), UTF-8, no shell."""
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, proc.stdout, proc.stderr


def call(runner, args):
    code, out, err = runner(args)
    if code != 0:
        lines = (err or "").strip().splitlines()
        reason = lines[-1] if lines else "exit code %d" % code
        raise ReleaseError("`%s` failed: %s" % (" ".join(args[:3]), reason))
    return out


def parse_version(text, what="version"):
    match = VERSION_RE.match(text or "")
    if not match:
        raise ReleaseError("%s %r is not vMAJOR.MINOR.PATCH" % (what, text))
    return tuple(int(part) for part in match.groups())


def fmt_version(parts):
    return "v%d.%d.%d" % parts


def clean(text):
    """One-line, marker-safe text from an untrusted title or body line."""
    text = " ".join(text.split())
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


# ---------------------------------------------------------------- collect


def previous_version(runner, main):
    code, out, _ = runner(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", main]
    )
    if code != 0:
        return None
    tag = out.strip()
    parse_version(tag, "last tag")
    return tag


def first_parent_log(runner, main, dev):
    out = call(
        runner,
        ["git", "log", "--first-parent", "--format=%H%x1f%P%x1f%s%x1e",
         "%s..%s" % (main, dev)],
    )
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, subject = record.split("\x1f")
        commits.append({"sha": sha, "parents": parents.split(), "subject": subject})
    commits.reverse()  # oldest first
    return commits


def collect(runner, main, dev):
    """Returns (previous tag or None, PR list, direct commits, skipped merges)."""
    prs, direct, skipped, seen = [], [], [], set()
    for commit in first_parent_log(runner, main, dev):
        subject = commit["subject"]
        if len(commit["parents"]) > 1:
            match = MERGE_PR_RE.match(subject)
        else:
            match = SQUASH_PR_RE.search(subject)
        if match:
            number = int(match.group(1))
            if number not in seen:
                seen.add(number)
                data = json.loads(call(
                    runner,
                    ["gh", "pr", "view", str(number), "--json", "number,title,body"],
                ))
                prs.append(data)
        elif len(commit["parents"]) > 1:
            skipped.append({"sha": commit["sha"][:7], "subject": subject})
        else:
            direct.append({"sha": commit["sha"][:7], "subject": subject})
    return previous_version(runner, main), prs, direct, skipped


# ---------------------------------------------------------------- analyze


def breaking_bullets(body):
    """Bullets of a `## Breaking changes` section, or None without one."""
    bullets, inside, found = [], False, False
    for line in body.splitlines():
        if BREAKING_SECTION_RE.match(line):
            inside, found = True, True
            continue
        if inside and re.match(r"^#{1,2}\s", line):
            inside = False
        if inside:
            match = re.match(r"^\s*[-*]\s+(.*\S)", line)
            if match:
                bullets.append(clean(match.group(1)))
    return bullets if found else None


def pr_entry(pr):
    title, body = pr.get("title") or "", pr.get("body") or ""
    number = pr["number"]
    match = TITLE_RE.match(title)
    if not match:
        return {"kind": "other", "type": None, "breaking": False,
                "line": "%s (#%d)" % (clean(title), number), "details": [],
                "pr": number}
    desc = match.group("desc")
    key = ISSUE_KEY_RE.match(desc)
    issue = key.group(1).upper() if key else None
    if key:
        desc = desc[key.end():]
    bullets = breaking_bullets(body)
    breaking = bool(match.group("bang")) or bool(BREAKING_FOOTER_RE.search(body)) \
        or bullets is not None
    ctype = match.group("type")
    refs = "%s, #%d" % (issue, number) if issue else "#%d" % number
    scope = match.group("scope")
    line = ("**%s:** " % clean(scope) if scope else "") + "%s (%s)" % (clean(desc), refs)
    if breaking:
        kind = "breaking"
    elif ctype == "feat":
        kind = "feat"
    elif ctype in ("fix", "perf"):
        kind = "fix"
    else:
        kind = "other"
    return {"kind": kind, "type": ctype, "breaking": breaking, "line": line,
            "details": bullets or [], "pr": number}


def next_version(previous, entries):
    if previous is None:
        return (0, 1, 0), "first release"
    major, minor, patch = parse_version(previous, "last tag")
    counts = {}
    for entry in entries:
        label = "breaking" if entry["breaking"] else (
            entry["type"] if entry["type"] in ("feat", "fix", "perf") else "other")
        counts[label] = counts.get(label, 0) + 1
    if counts.get("breaking"):
        bump = "minor" if major == 0 else "major"
    elif counts.get("feat"):
        bump = "minor"
    else:
        bump = "patch"
    if bump == "major":
        version = (major + 1, 0, 0)
    elif bump == "minor":
        version = (major, minor + 1, 0)
    else:
        version = (major, minor, patch + 1)
    order = ["breaking", "feat", "fix", "perf", "other"]
    summary = ", ".join("%d %s" % (counts[k], k) for k in order if counts.get(k))
    return version, "%s → %s" % (summary, bump)


def issue_refs(prs):
    """Closing candidates {issue: [PRs]} and mentioned-only numbers."""
    closing, mentioned = {}, set()
    for pr in prs:
        body = pr.get("body") or ""
        found = set()
        for regex in CLOSING_RES:
            found.update(int(n) for n in regex.findall(body))
        for number in found:
            closing.setdefault(number, []).append(pr["number"])
        mentioned.update(int(n) for n in MENTION_RE.findall(body))
    pr_numbers = {pr["number"] for pr in prs}
    return closing, sorted(mentioned - set(closing) - pr_numbers)


def issue_states(runner, closing, keep_open):
    unknown = sorted(set(keep_open) - set(closing))
    if unknown:
        raise ReleaseError("--keep-open %s: not an issue this release would close"
                           % ", ".join("#%d" % n for n in unknown))
    close, kept, skipped = [], [], []
    for number in sorted(closing):
        prs = closing[number]
        code, out, err = runner(
            ["gh", "issue", "view", str(number), "--json", "number,state,url"])
        if code != 0:
            reason = "is a pull request" if "pull request" in (err or "").lower() \
                else "not found"
            skipped.append({"number": number, "prs": prs, "reason": reason})
            continue
        data = json.loads(out)
        if "/pull/" in data.get("url", ""):
            skipped.append({"number": number, "prs": prs, "reason": "is a pull request"})
        elif data.get("state") != "OPEN":
            skipped.append({"number": number, "prs": prs, "reason": "already closed"})
        elif number in keep_open:
            kept.append({"number": number, "prs": prs})
        else:
            close.append({"number": number, "prs": prs})
    return close, kept, skipped


# ---------------------------------------------------------------- render


def render_body(version, entries, direct, close):
    lines = ["## Release %s" % version, "", NOTES_START]
    groups = {key: [] for key, _ in SECTIONS}
    for entry in entries:
        groups[entry["kind"]].append(entry)
    first = True
    for key, heading in SECTIONS:
        rendered = []
        for entry in groups[key]:
            rendered.append("- " + entry["line"])
            rendered.extend("  - " + d for d in entry["details"])
        if key == "other":
            rendered.extend("- %s (%s)" % (clean(c["subject"]), c["sha"]) for c in direct)
        if not rendered:
            continue
        if not first:
            lines.append("")
        lines.append("### " + heading)
        lines.extend(rendered)
        first = False
    lines.append(NOTES_END)
    if close:
        lines.append("")
        lines.extend("Closes #%d" % issue["number"] for issue in close)
    return "\n".join(lines) + "\n"


def build_plan(runner, main, dev, version=None, keep_open=()):
    previous, prs, direct, skipped = collect(runner, main, dev)
    entries = [pr_entry(pr) for pr in prs]
    if not entries and not direct:
        raise ReleaseError("nothing to release: no PRs or commits in %s..%s"
                           % (main[:7], dev[:7]))
    auto, reason = next_version(previous, entries + [
        {"breaking": False, "type": None} for _ in direct])
    if version:
        wanted = parse_version(version)
        if previous is not None and wanted <= parse_version(previous, "last tag"):
            raise ReleaseError("version %s must be greater than the last tag %s"
                               % (version, previous))
        reason = "set by the user (computed: %s, %s)" % (fmt_version(auto), reason)
        final = fmt_version(wanted)
    else:
        final = fmt_version(auto)
    closing, mentioned = issue_refs(prs)
    close, kept, skipped_issues = issue_states(runner, closing, set(keep_open))
    return {
        "version": final,
        "previous": previous,
        "reason": reason,
        "title": "chore: release %s" % final,
        "branch": "release/%s" % final,
        "body": render_body(final, entries, direct, close),
        "prs": [e["pr"] for e in entries],
        "issues": {"close": close, "kept_open": kept, "skipped": skipped_issues,
                   "mentioned": mentioned},
        "skipped_commits": skipped,
    }


# ---------------------------------------------------------------- notes


def extract_notes(body, version=None, repo=None, prev=None):
    body = body.replace("\r\n", "\n")
    if body.count(NOTES_START) != 1 or body.count(NOTES_END) != 1:
        raise ReleaseError("the PR body must contain exactly one %s and one %s"
                           % (NOTES_START, NOTES_END))
    start, end = body.index(NOTES_START), body.index(NOTES_END)
    if end < start:
        raise ReleaseError("release-notes:end comes before release-notes:start")
    notes = body[start + len(NOTES_START):end].strip()
    if not notes:
        raise ReleaseError("the release notes between the markers are empty")
    if prev:
        if not (version and repo):
            raise ReleaseError("--prev needs --version and --repo")
        parse_version(prev, "--prev")
        parse_version(version)
        if not REPO_RE.match(repo):
            raise ReleaseError("--repo %r is not owner/name" % repo)
        notes += "\n\n**Full Changelog**: https://github.com/%s/compare/%s...%s" % (
            repo, prev, version)
    return notes + "\n"


# ---------------------------------------------------------------- branch


def create_branch(runner, version, sha):
    parse_version(version)
    if not SHA_RE.match(sha):
        raise ReleaseError("--sha must be a full 40-character commit hash")
    code, _, _ = runner(["git", "merge-base", "--is-ancestor", sha, "origin/development"])
    if code != 0:
        raise ReleaseError("%s is not on origin/development" % sha[:7])
    ref = "refs/heads/release/%s" % version
    # A push of the same commit to an existing branch is a silent no-op, so check first;
    # the empty lease still guarantees the push never overwrites a branch.
    code, _, _ = runner(["git", "ls-remote", "--exit-code", "--heads", "origin", ref])
    if code == 0:
        raise ReleaseError("release/%s already exists on origin" % version)
    if code != 2:
        raise ReleaseError("cannot check whether release/%s exists on origin" % version)
    code, _, err = runner(
        ["git", "push", "--force-with-lease=%s:" % ref, "origin", "%s:%s" % (sha, ref)])
    if code != 0:
        lines = (err or "").strip().splitlines()
        raise ReleaseError("could not create release/%s (it may already exist): %s"
                           % (version, lines[-1] if lines else "exit code %d" % code))
    return "release/%s" % version


# ---------------------------------------------------------------- CLI


def main(argv=None, runner=run):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="release_plan.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--main", required=True)
    p_plan.add_argument("--dev", required=True)
    p_plan.add_argument("--out-dir", required=True)
    p_plan.add_argument("--version")
    p_plan.add_argument("--keep-open", type=int, action="append", default=[])
    p_branch = sub.add_parser("branch")
    p_branch.add_argument("--version", required=True)
    p_branch.add_argument("--sha", required=True)
    p_notes = sub.add_parser("notes")
    p_notes.add_argument("body")
    p_notes.add_argument("--version")
    p_notes.add_argument("--repo")
    p_notes.add_argument("--prev")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_plan(runner, args.main, args.dev, args.version, args.keep_open)
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "title.txt").write_text(plan["title"] + "\n", encoding="utf-8")
            (out / "body.md").write_text(plan["body"], encoding="utf-8")
            plan["title_file"] = str(out / "title.txt")
            plan["body_file"] = str(out / "body.md")
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        elif args.command == "branch":
            print(create_branch(runner, args.version, args.sha))
        else:
            try:
                body = Path(args.body).read_text(encoding="utf-8")
            except OSError as exc:
                raise ReleaseError("cannot read %s: %s" % (args.body, exc.strerror))
            sys.stdout.write(extract_notes(body, args.version, args.repo, args.prev))
    except ReleaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
