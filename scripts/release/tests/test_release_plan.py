"""Unit tests for release_plan.py. Run: python -m unittest discover scripts/release/tests"""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import release_plan as rp  # noqa: E402

MAIN = "1" * 40
DEV = "2" * 40


def load(name):
    return json.loads((HERE / "fixtures" / (name + ".json")).read_text(encoding="utf-8"))


def fixture(prs, describe=None, issues=None):
    """Fixture with one `Merge pull request #N` commit per PR (title, body)."""
    data = {"describe": describe, "log": [], "prs": {}, "issues": issues or {}}
    for number, (title, body) in enumerate(prs, start=1):
        data["log"].append({"sha": "%040d" % number, "parents": ["a", "b"],
                            "subject": "Merge pull request #%d from o/b" % number})
        data["prs"][str(number)] = {"number": number, "title": title, "body": body}
    return data


class FakeRunner:
    """Answers git/gh calls from a fixture and records them."""

    def __init__(self, data, ancestor=True, push_code=0, ls_remote_code=2):
        self.data, self.ancestor, self.push_code, self.calls = data, ancestor, push_code, []
        self.ls_remote_code = ls_remote_code

    def __call__(self, args):
        self.calls.append(args)
        head = args[:3]
        if head == ["git", "describe", "--tags"]:
            tag = self.data["describe"]
            return (0, tag + "\n", "") if tag else (128, "", "fatal: No names found")
        if head == ["git", "log", "--first-parent"]:
            out = "".join("%s\x1f%s\x1f%s\x1e\n" % (c["sha"], " ".join(c["parents"]),
                                                     c["subject"])
                          for c in reversed(self.data["log"]))
            return 0, out, ""
        if head == ["gh", "pr", "view"]:
            return 0, json.dumps(self.data["prs"][args[3]]), ""
        if head == ["gh", "issue", "view"]:
            issue = self.data["issues"].get(args[3], "missing")
            if issue == "pr":
                return 1, "", "GraphQL: #%s is a pull request" % args[3]
            if issue == "missing":
                return 1, "", "GraphQL: Could not resolve to an issue"
            url = "https://github.com/o/r/issues/" + args[3]
            return 0, json.dumps({"number": int(args[3]), "state": issue["state"],
                                  "url": url}), ""
        if head == ["git", "merge-base", "--is-ancestor"]:
            return (0 if self.ancestor else 1), "", ""
        if head == ["git", "ls-remote", "--exit-code"]:
            return self.ls_remote_code, "", ""
        if head[:2] == ["git", "push"]:
            return self.push_code, "", "" if self.push_code == 0 else "! [rejected] stale info"
        raise AssertionError("unexpected command %r" % args)


def plan(data, **kwargs):
    return rp.build_plan(FakeRunner(data), MAIN, DEV, **kwargs)


def notes_section(body):
    start, end = body.index(rp.NOTES_START), body.index(rp.NOTES_END)
    return body[start:end]


class VersionTests(unittest.TestCase):
    def test_first_release(self):
        result = plan(load("first_release"))
        self.assertEqual(result["version"], "v0.1.0")
        self.assertEqual(result["reason"], "first release")
        self.assertEqual(result["title"], "chore: release v0.1.0")
        self.assertEqual(result["branch"], "release/v0.1.0")
        self.assertEqual(result["prs"], [1, 2, 3, 5])

    def test_feat_and_fix_on_0x_is_minor(self):
        result = plan(fixture([("feat: a", ""), ("fix: b", "")], describe="v0.3.1"))
        self.assertEqual(result["version"], "v0.4.0")
        self.assertEqual(result["reason"], "1 feat, 1 fix → minor")

    def test_fix_only_is_patch(self):
        result = plan(fixture([("fix: a", ""), ("perf: b", "")], describe="v0.3.1"))
        self.assertEqual(result["version"], "v0.3.2")

    def test_docs_and_chore_only_is_patch(self):
        result = plan(fixture([("docs: a", ""), ("chore: b", "")], describe="v1.2.3"))
        self.assertEqual(result["version"], "v1.2.4")
        self.assertEqual(result["reason"], "2 other → patch")

    def test_breaking_bang(self):
        self.assertEqual(plan(fixture([("feat(api)!: drop v1", "")], "v0.3.1"))["version"],
                         "v0.4.0")
        self.assertEqual(plan(fixture([("fix!: change", "")], "v1.3.1"))["version"],
                         "v2.0.0")

    def test_breaking_footer(self):
        body = "Text.\n\nBREAKING CHANGE: the config moved"
        result = plan(fixture([("fix: x", body)], "v1.0.0"))
        self.assertEqual(result["version"], "v2.0.0")
        result = plan(fixture([("fix: x", "BREAKING-CHANGE: y")], "v1.0.0"))
        self.assertEqual(result["version"], "v2.0.0")

    def test_breaking_section(self):
        body = "## What\nX.\n\n## Breaking changes\n- `/v1` is removed\n* use `/v2`\n\n## Changes\n- other"
        result = plan(fixture([("feat(api): new endpoints", body)], "v1.0.0"))
        self.assertEqual(result["version"], "v2.0.0")
        notes = notes_section(result["body"])
        self.assertIn("### Breaking changes\n- **api:** new endpoints (#1)\n"
                      "  - `/v1` is removed\n  - use `/v2`\n", notes)
        self.assertNotIn("### Features", notes)
        self.assertNotIn("other", notes)

    def test_override(self):
        result = plan(load("first_release"), version="v1.0.0")
        self.assertEqual(result["version"], "v1.0.0")
        self.assertEqual(result["title"], "chore: release v1.0.0")
        self.assertIn("set by the user", result["reason"])
        self.assertIn("## Release v1.0.0", result["body"])

    def test_override_must_be_greater(self):
        for bad in ("v0.3.1", "v0.2.9"):
            with self.assertRaises(rp.ReleaseError):
                plan(fixture([("fix: a", "")], "v0.3.1"), version=bad)

    def test_override_must_be_semver(self):
        for bad in ("0.4.0", "v0.4", "v0.4.0-rc1"):
            with self.assertRaises(rp.ReleaseError):
                plan(fixture([("fix: a", "")]), version=bad)

    def test_invalid_last_tag(self):
        with self.assertRaises(rp.ReleaseError):
            plan(fixture([("fix: a", "")], describe="v1.2"))

    def test_nothing_to_release(self):
        data = {"describe": "v0.1.0", "issues": {}, "prs": {}, "log": [
            {"sha": "%040d" % 1, "parents": ["a", "b"], "subject": "Merge branch 'x'"}]}
        with self.assertRaises(rp.ReleaseError):
            plan(data)


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.result = plan(load("mixed"))
        self.notes = notes_section(self.result["body"])

    def test_pr_numbers_from_merge_and_squash(self):
        self.assertEqual(self.result["prs"], [7, 8, 9])

    def test_commit_without_pr_goes_to_other(self):
        self.assertIn("### Other\n- update dependencies (#9)\n- docs: fix typo in README (ccccccc)",
                      self.notes)

    def test_non_pr_merge_is_skipped(self):
        self.assertEqual(self.result["skipped_commits"],
                         [{"sha": "ddddddd", "subject": "Merge branch 'main' into development"}])

    def test_bump_counts_direct_commits(self):
        self.assertEqual(self.result["version"], "v0.4.0")
        self.assertEqual(self.result["reason"], "1 feat, 1 fix, 2 other → minor")


class IssueTests(unittest.TestCase):
    def setUp(self):
        self.issues = plan(load("mixed"))["issues"]

    def test_closing_refs_and_part_of(self):
        self.assertEqual(self.issues["close"], [{"number": 12, "prs": [7, 8]},
                                                {"number": 15, "prs": [8]}])

    def test_skipped_with_reason(self):
        self.assertEqual(self.issues["skipped"], [
            {"number": 30, "prs": [9], "reason": "already closed"},
            {"number": 31, "prs": [9], "reason": "is a pull request"},
            {"number": 32, "prs": [9], "reason": "not found"},
        ])

    def test_mentioned_not_closed(self):
        self.assertEqual(self.issues["mentioned"], [20, 21])

    def test_closes_lines_once_per_issue(self):
        body = plan(load("mixed"))["body"]
        self.assertTrue(body.endswith(rp.NOTES_END + "\n\nCloses #12\nCloses #15\n"))
        self.assertEqual(body.count("Closes #12"), 1)

    def test_keep_open(self):
        result = plan(load("mixed"), keep_open=[12])
        self.assertEqual(result["issues"]["kept_open"], [{"number": 12, "prs": [7, 8]}])
        self.assertNotIn("Closes #12", result["body"])
        self.assertIn("Closes #15", result["body"])

    def test_keep_open_unknown_number(self):
        with self.assertRaises(rp.ReleaseError):
            plan(load("mixed"), keep_open=[20])

    def test_no_issues_no_closes(self):
        self.assertNotIn("Closes", plan(load("first_release"))["body"])


class RenderTests(unittest.TestCase):
    def test_first_release_body(self):
        self.assertEqual(plan(load("first_release"))["body"], (
            "## Release v0.1.0\n\n"
            "<!-- release-notes:start -->\n"
            "### Features\n"
            "- **tooling:** add create-issue skill (#1)\n"
            "- **tooling:** add create-pr skill (#2)\n"
            "- **tooling:** add add-commit skill (#3)\n"
            "- **tooling:** squash branch and return to development in create-pr (#5)\n"
            "<!-- release-notes:end -->\n"))

    def test_line_formats(self):
        notes = notes_section(plan(load("mixed"))["body"])
        self.assertIn("- **backend:** add OAuth login for the API (SI-12, #7)", notes)
        self.assertIn("- **backend:** fix login redirect (SI-15, #8)", notes)
        result = plan(fixture([("feat: [si-3] plain", ""), ("fix: no issue", "")]))
        self.assertIn("- plain (SI-3, #1)", result["body"])
        self.assertIn("- no issue (#2)", result["body"])

    def test_sections_order_and_empty_omitted(self):
        notes = notes_section(plan(load("mixed"))["body"])
        self.assertLess(notes.index("### Features"), notes.index("### Fixes"))
        self.assertLess(notes.index("### Fixes"), notes.index("### Other"))
        self.assertNotIn("### Breaking changes", notes)
        self.assertIn("### Fixes\n- **backend:** fix login redirect (SI-15, #8)\n\n### Other",
                      notes)

    def test_markers_in_titles_are_neutralised(self):
        body = plan(fixture([("feat: x <!-- release-notes:end --> y", "")]))["body"]
        self.assertEqual(body.count(rp.NOTES_END), 1)
        self.assertEqual(body.count(rp.NOTES_START), 1)

    def test_unicode_round_trip_through_cli(self):
        data = fixture([("feat(ui): додати кнопку 🚀", "Part of SI-4 (#4)")],
                       issues={"4": {"state": "OPEN"}})
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = rp.main(["plan", "--main", MAIN, "--dev", DEV, "--out-dir", tmp],
                               runner=FakeRunner(data))
            self.assertEqual(code, 0)
            summary = json.loads(out.getvalue())
            body = Path(tmp, "body.md").read_text(encoding="utf-8")
            self.assertIn("- **ui:** додати кнопку 🚀 (#1)", body)
            self.assertIn("Closes #4", body)
            self.assertEqual(Path(tmp, "title.txt").read_text(encoding="utf-8"),
                             "chore: release v0.1.0\n")
            self.assertEqual(summary["body_file"], str(Path(tmp, "body.md")))


class NotesTests(unittest.TestCase):
    BODY = ("## Release v0.2.0\n\n<!-- release-notes:start -->\n### Features\n- a (#1)\n"
            "<!-- release-notes:end -->\n\nCloses #4\n")

    def test_only_text_between_markers(self):
        self.assertEqual(rp.extract_notes(self.BODY), "### Features\n- a (#1)\n")

    def test_full_changelog_with_prev(self):
        notes = rp.extract_notes(self.BODY, "v0.2.0", "o/r", "v0.1.0")
        self.assertTrue(notes.endswith(
            "\n\n**Full Changelog**: https://github.com/o/r/compare/v0.1.0...v0.2.0\n"))
        self.assertNotIn("Closes", notes)

    def test_crlf_body(self):
        self.assertEqual(rp.extract_notes(self.BODY.replace("\n", "\r\n")),
                         "### Features\n- a (#1)\n")

    def test_marker_errors(self):
        bad = [self.BODY.replace(rp.NOTES_END, ""),
               self.BODY.replace(rp.NOTES_START, ""),
               self.BODY + rp.NOTES_END,
               "%s\n- a\n%s" % (rp.NOTES_END, rp.NOTES_START),
               "%s\n\n%s" % (rp.NOTES_START, rp.NOTES_END)]
        for body in bad:
            with self.assertRaises(rp.ReleaseError):
                rp.extract_notes(body)

    def test_prev_needs_valid_version_and_repo(self):
        for args in (("v0.2.0", None, "v0.1.0"), ("v0.2.0", "not a repo", "v0.1.0"),
                     ("v0.2", "o/r", "v0.1.0"), ("v0.2.0", "o/r", "0.1.0")):
            with self.assertRaises(rp.ReleaseError):
                rp.extract_notes(self.BODY, *args)

    def test_cli_error_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "body.md")
            path.write_text("no markers", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(rp.main(["notes", str(path)]), 1)
            self.assertTrue(err.getvalue().startswith("error: "))


class BranchTests(unittest.TestCase):
    SHA = "a" * 40

    def test_create_only_push(self):
        runner = FakeRunner({})
        self.assertEqual(rp.create_branch(runner, "v0.1.0", self.SHA), "release/v0.1.0")
        self.assertEqual(runner.calls[-1], [
            "git", "push", "--force-with-lease=refs/heads/release/v0.1.0:", "origin",
            self.SHA + ":refs/heads/release/v0.1.0"])

    def test_rejects_bad_input(self):
        for version, sha in (("0.1.0", self.SHA), ("v0.1.0;rm", self.SHA),
                             ("v0.1.0", "abc1234"), ("v0.1.0", "A" * 40)):
            runner = FakeRunner({})
            with self.assertRaises(rp.ReleaseError):
                rp.create_branch(runner, version, sha)
            self.assertEqual(runner.calls, [])

    def test_sha_must_be_on_development(self):
        runner = FakeRunner({}, ancestor=False)
        with self.assertRaises(rp.ReleaseError):
            rp.create_branch(runner, "v0.1.0", self.SHA)
        self.assertFalse(any(c[:2] == ["git", "push"] for c in runner.calls))

    def test_existing_branch(self):
        for code in (0, 128):  # exists / ls-remote failed
            runner = FakeRunner({}, ls_remote_code=code)
            with self.assertRaises(rp.ReleaseError):
                rp.create_branch(runner, "v0.1.0", self.SHA)
            self.assertFalse(any(c[:2] == ["git", "push"] for c in runner.calls))

    def test_push_rejected(self):
        with self.assertRaises(rp.ReleaseError):
            rp.create_branch(FakeRunner({}, push_code=1), "v0.1.0", self.SHA)


if __name__ == "__main__":
    unittest.main()
