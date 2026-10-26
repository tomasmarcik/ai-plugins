#!/usr/bin/env python3
"""Black-box contracts for scripts/publish.py: cases A1 to A11 of the ai-plugins plan.

Run from anywhere: python3 scripts/test-publish.py

publish.py is driven through its CLI in a subprocess and never imported. Each case builds its own bare origin
and clone under this repository's tmp/. A fake claude, first on PATH, passes or fails `plugin validate` on
demand and can push a concurrent commit to the origin while the script validates. Nothing touches the network,
the real claude, the real HOME, or anything outside tmp/. Prints one "ok" or "NOT OK" line per case, then the
counts; exits 1 when any case fails, and then keeps its scratch directory as evidence.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "publish.py"
ALLOW = "allowlist.json"
CL = ".claude-plugin/marketplace.json"
CX = ".agents/plugins/marketplace.json"
LAYOUT = ("README.md", ".gitignore", ".github/workflows/publish.yml", ".github/workflows/verify.yml",
          "scripts/publish.py", "scripts/test-publish.py")
BOT = "github-actions[bot]"
FIXTURE_ID = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
              "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}

# The seeded allowlist and catalogs as main held them before any workflow publish (98bc5cb): kobo 0.4.3.
# Frozen here, so A3's offline G1 and A10's seed state keep meaning the seed after later publishes.
SEED_ALLOWLIST = '{\n  "kobo": "tomasmarcik/kobo"\n}\n'
SEED_CLAUDE = """{
  "description": "Claude and Codex plugin marketplace.",
  "name": "ai-plugins",
  "owner": {
    "name": "Tomas Marcik"
  },
  "plugins": [
    {
      "category": "development",
      "name": "kobo",
      "source": {
        "path": "plugin/kobo",
        "ref": "v0.4.3",
        "sha": "a9127b17172cb1bfa1ce5c23a836adc43ad82d6a",
        "source": "git-subdir",
        "url": "https://github.com/tomasmarcik/kobo.git"
      }
    }
  ]
}
"""
SEED_CODEX = """{
  "interface": {
    "displayName": "AI Plugins"
  },
  "name": "ai-plugins",
  "plugins": [
    {
      "category": "Productivity",
      "name": "kobo",
      "policy": {
        "authentication": "ON_INSTALL",
        "installation": "AVAILABLE"
      },
      "source": {
        "path": "./plugins/kobo",
        "ref": "codex-v0.4.3",
        "source": "git-subdir",
        "url": "https://github.com/tomasmarcik/kobo.git"
      }
    }
  ]
}
"""
SEED_README = "# ai-plugins\n\nFixture readme.\n"
SEED_FILES = {ALLOW: SEED_ALLOWLIST, CL: SEED_CLAUDE, CX: SEED_CODEX, "README.md": SEED_README,
              ".gitignore": "tmp/\n.DS_Store\n"}

SHA_043 = "a9127b17172cb1bfa1ce5c23a836adc43ad82d6a"
SHA_044 = "0123456789abcdef0123456789abcdef01234567"

# The fake claude reads its per-run instructions from fake-claude.json beside itself, so it depends on nothing
# the script under test passes or strips. It logs every call; `plugin validate --strict <dir>` also logs both
# catalogs as the candidate holds them. Mode "fail" fails validation; a "rounds" list makes validate call i push
# round i to the origin first, as a concurrent publisher would.
FAKE_CLAUDE = r'''
import json, os, subprocess, sys

CATALOGS = (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "fake-claude.json")) as handle:
    CONFIG = json.load(handle)


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def git(args, cwd, env):
    return subprocess.run(["git"] + args, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout


def race(call):
    rounds = CONFIG.get("rounds") or []
    if call >= len(rounds):
        return
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(CONFIG["git_env"])
    clone = os.path.join(CONFIG["race_dir"], "round-%d" % call)
    git(["clone", "-q", CONFIG["origin"], clone], None, env)
    for rel, text in rounds[call]["files"].items():
        os.makedirs(os.path.dirname(os.path.join(clone, rel)) or clone, exist_ok=True)
        with open(os.path.join(clone, rel), "w", encoding="utf-8") as handle:
            handle.write(text)
    git(["add", "-A"], clone, env)
    git(["commit", "-qm", rounds[call]["message"]], clone, env)
    git(["push", "-q", "origin", "HEAD:main"], clone, env)
    with open(CONFIG["race_log"], "a") as handle:
        handle.write(git(["rev-parse", "HEAD"], clone, env).strip() + "\n")


def main():
    args = sys.argv[1:]
    entry = {"argv": args, "cwd": os.getcwd()}
    valid = len(args) == 4 and args[:3] == ["plugin", "validate", "--strict"]
    if valid:
        target = os.path.join(os.getcwd(), args[3])
        entry["catalogs"] = {rel: read(os.path.join(target, rel)) for rel in CATALOGS}
    previous = read(CONFIG["log"]) or ""
    call = sum(1 for line in previous.splitlines() if "catalogs" in json.loads(line))
    with open(CONFIG["log"], "a") as handle:
        handle.write(json.dumps(entry) + "\n")
    if not valid:
        print("fake claude: unexpected invocation", file=sys.stderr)
        sys.exit(99)
    if CONFIG.get("mode") == "fail":
        print("fake validate: FAKE-STDOUT-MARKER")
        print("fake validate: FAKE-STDERR-MARKER", file=sys.stderr)
        sys.exit(1)
    race(call)


main()
'''

# Logs every ref update the origin receives, with whether it is a fast-forward, and rejects every push while
# the flag file exists. Runs in the quarantine, so the pushed objects are visible to merge-base.
HOOK = """#!/bin/sh
while read old new ref; do
  kind=ff
  case "$old" in *[!0]*) ;; *) kind=create ;; esac
  case "$new" in *[!0]*) ;; *) kind=delete ;; esac
  if [ "$kind" = ff ] && ! git merge-base --is-ancestor "$old" "$new"; then kind=nonff; fi
  echo "$old $new $ref $kind" >> '%(log)s'
done
if [ -e '%(flag)s' ]; then echo "fixture origin: push rejected" >&2; exit 1; fi
exit 0
"""


class Failure(Exception):
    pass


def expect(condition, message):
    if not condition:
        raise Failure(message)


def equal(actual, expected, what):
    if actual != expected:
        raise Failure("%s: expected %r, got %r" % (what, expected, actual))


def canonical(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def claude_source(source, path, version, sha):
    return {"path": path, "ref": "v" + version, "sha": sha, "source": "git-subdir",
            "url": "https://github.com/%s.git" % source}


def codex_source(source, path, version):
    return {"path": "./" + path, "ref": "codex-v" + version, "source": "git-subdir",
            "url": "https://github.com/%s.git" % source}


def with_source(text, name, source):
    """The catalog text with entry <name>'s source replaced, and nothing else touched."""
    catalog = json.loads(text)
    hits = [entry for entry in catalog["plugins"] if entry["name"] == name]
    assert len(hits) == 1, "fixture error: %s entries named %s" % (len(hits), name)
    hits[0]["source"] = source
    return canonical(catalog)


def appended(text, name, source):
    catalog = json.loads(text)
    catalog["plugins"].append({"name": name, "source": source})
    return canonical(catalog)


def edited(text, change):
    catalog = json.loads(text)
    change(catalog)
    return canonical(catalog)


def kobo(version="0.4.4", sha=SHA_044, **override):
    data = {"PLUGIN": "kobo", "SOURCE": "tomasmarcik/kobo", "VERSION": version, "SHA": sha,
            "CLAUDE_PATH": "plugin/kobo", "CODEX_PATH": "plugins/kobo"}
    data.update(override)
    return data


G1 = kobo("0.4.3", SHA_043)


def one_json(text, stream):
    lines = text.splitlines()
    expect(len(lines) == 1, "%s is not exactly one JSON line: %r" % (stream, text))
    try:
        return json.loads(lines[0])
    except ValueError:
        raise Failure("%s is not JSON: %r" % (stream, text))


class Run(object):
    pass


class Suite(object):
    def __init__(self, scratch):
        self.scratch = scratch
        self.bin = scratch / "bin"
        self.home = scratch / "home"
        self.bin.mkdir()
        self.home.mkdir()
        fake = self.bin / "claude"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_CLAUDE)
        fake.chmod(0o755)
        self.path = str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath)
        found = shutil.which("claude", path=self.path)
        if found is None or Path(found).resolve() != fake.resolve():
            raise SystemExit("the fake claude does not shadow every other claude on PATH: %s" % found)


class Case(object):
    """One case's scratch: a bare origin with a logging hook, a seed repo that makes fixture commits, a clone."""

    def __init__(self, suite, directory):
        self.suite = suite
        self.dir = directory
        self.dir.mkdir()
        self.tmpdir = directory / "tmpdir"
        self.tmpdir.mkdir()
        self.origin = directory / "origin.git"
        self.seed = directory / "seed"
        self.work = directory / "work"
        self.claude_log = directory / "claude.log"
        self.push_log = directory / "push.log"
        self.race_log = directory / "race.log"
        self.reject_flag = directory / "reject-pushes"
        self.fixture_commits = set()
        self.allow_origin = None
        self.allow_work = None
        self.audit_failures = []
        self.audited_runs = 0
        for path in (self.claude_log, self.push_log, self.race_log):
            path.write_text("")

    # environments

    def env(self, extra=None):
        env = {"PATH": self.suite.path, "HOME": str(self.suite.home), "TMPDIR": str(self.tmpdir),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
               "GIT_ALLOW_PROTOCOL": "file", "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra or {})
        return {key: value for key, value in env.items() if value is not None}

    def git(self, cwd, *args):
        proc = subprocess.run(["git"] + list(args), cwd=str(cwd), env=self.env(FIXTURE_ID),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise RuntimeError("fixture git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
        return proc.stdout

    # fixtures

    def build(self, files=None):
        files = dict(SEED_FILES, **(files or {}))
        self.git(self.dir, "init", "-q", "--bare", str(self.origin))
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text(HOOK % {"log": self.push_log, "flag": self.reject_flag})
        hook.chmod(0o755)
        self.git(self.dir, "init", "-q", str(self.seed))
        self.git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.write(self.seed, files)
        self.commit_seed("chore: fixture seed")
        self.git(self.dir, "clone", "-q", str(self.origin), str(self.work))
        self.allow_origin = files[ALLOW]
        self.allow_work = files[ALLOW]
        return self

    def write(self, base, files):
        for rel, text in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit_seed(self, message):
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-qm", message)
        self.git(self.seed, "push", "-q", "origin", "HEAD:main")
        sha = self.git(self.seed, "rev-parse", "HEAD").strip()
        self.fixture_commits.add(sha)
        return sha

    def push_fixture(self, files, message="docs: fixture edit"):
        """A hand commit on origin main, on top of whatever main now is."""
        self.git(self.seed, "fetch", "-q", "origin")
        self.git(self.seed, "reset", "-q", "--hard", "origin/main")
        self.write(self.seed, files)
        sha = self.commit_seed(message)
        if ALLOW in files:
            self.allow_origin = files[ALLOW]
        return sha

    # origin state

    def refs(self):
        out = self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines())

    def tip(self):
        return self.git(self.origin, "rev-parse", "refs/heads/main").strip()

    def show(self, rev, rel):
        return self.git(self.origin, "show", "%s:%s" % (rev, rel))

    def lines(self, path):
        return [line for line in path.read_text().splitlines() if line]

    # runs

    def configure_fake(self, mode="pass", rounds=None):
        config = {"log": str(self.claude_log), "mode": mode, "rounds": rounds or [], "origin": str(self.origin),
                  "race_dir": str(self.dir / "race"), "race_log": str(self.race_log),
                  "git_env": dict(FIXTURE_ID, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                                  GIT_ALLOW_PROTOCOL="file")}
        (self.suite.bin / "fake-claude.json").write_text(json.dumps(config))

    def update(self, data, mode="pass", rounds=None, actions=True, ref="refs/heads/main", extra=None, cwd=None):
        self.configure_fake(mode, rounds)
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REF": ref} if actions else {}
        env.update(data)
        env.update(extra or {})
        run = Run()
        run.before = self.refs()
        run.work_status = self.git(self.work, "status", "--porcelain")
        run.work_head = self.git(self.work, "rev-parse", "HEAD")
        marks = [len(self.lines(path)) for path in (self.claude_log, self.push_log, self.race_log)]
        proc = subprocess.run([sys.executable, str(SCRIPT), "update"], cwd=str(cwd or self.work), env=self.env(env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        run.rc, run.out, run.err = proc.returncode, proc.stdout, proc.stderr
        run.after = self.refs()
        calls = [json.loads(line) for line in self.lines(self.claude_log)[marks[0]:]]
        run.validates = [call for call in calls if "catalogs" in call]
        run.pushes = [line.split() for line in self.lines(self.push_log)[marks[1]:]]
        run.race = self.lines(self.race_log)[marks[2]:]
        self.fixture_commits.update(run.race)
        for index in range(len(run.race)):
            if ALLOW in rounds[index]["files"]:
                self.allow_origin = rounds[index]["files"][ALLOW]
        self.audit_origin()
        unexpected = [call["argv"] for call in calls if "catalogs" not in call]
        expect(not unexpected, "claude called other than for plugin validate: %r" % unexpected)
        for old, new, refname, kind in run.pushes:
            expect(refname == "refs/heads/main", "a push updated %s" % refname)
            expect(kind in ("ff", "create"), "a %s update reached origin main: %s..%s" % (kind, old, new))
        return run

    def audit_origin(self):
        """A11, after every run: allowlist.json on origin and in the clone is the fixture's, byte for byte, and
        no commit the fixtures did not make touches it."""
        self.audited_runs += 1
        problems = []
        if self.show("refs/heads/main", ALLOW) != self.allow_origin:
            problems.append("origin main's allowlist.json changed")
        for sha in self.git(self.origin, "rev-list", "--all").split():
            if sha in self.fixture_commits:
                continue
            names = self.git(self.origin, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha)
            if ALLOW in names.split():
                problems.append("commit %s writes allowlist.json" % sha[:12])
        if (self.work / ALLOW).read_text() != self.allow_work:
            problems.append("the clone's allowlist.json changed")
        self.audit_failures.extend(problems)

    # check

    def tree(self, label, files=None, mutate=None):
        """A committed working tree in the full layout, with the seed's allowlist and catalogs."""
        tree = self.dir / ("tree-" + label)
        self.git(self.dir, "init", "-q", str(tree))
        for rel in LAYOUT:
            if (ROOT / rel).is_file():
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(ROOT / rel), str(tree / rel))
        self.write(tree, {ALLOW: SEED_ALLOWLIST, CL: SEED_CLAUDE, CX: SEED_CODEX})
        self.git(tree, "add", "-A")
        self.git(tree, "commit", "-qm", "chore: fixture layout")
        self.write(tree, files or {})
        if mutate:
            mutate(self, tree)
        return tree

    def check(self, tree, mode="pass"):
        self.configure_fake(mode)
        allow_before = (tree / ALLOW).read_bytes() if (tree / ALLOW).exists() else None
        mark = len(self.lines(self.claude_log))
        proc = subprocess.run([sys.executable, str(SCRIPT), "check"], cwd=str(tree), env=self.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        run = Run()
        run.rc, run.out, run.err = proc.returncode, proc.stdout, proc.stderr
        calls = [json.loads(line) for line in self.lines(self.claude_log)[mark:]]
        run.validates = [call for call in calls if "catalogs" in call]
        self.audited_runs += 1
        allow_after = (tree / ALLOW).read_bytes() if (tree / ALLOW).exists() else None
        if allow_after != allow_before:
            self.audit_failures.append("check changed %s" % (tree / ALLOW))
        unexpected = [call["argv"] for call in calls if "catalogs" not in call]
        expect(not unexpected, "claude called other than for plugin validate: %r" % unexpected)
        return run


# assertions shared by the cases

def no_commit(case, run):
    """Nothing the script made reached origin: every new commit on main is a fixture's, and no ref appeared."""
    equal(sorted(run.after), sorted(run.before), "origin refs")
    new = case.git(case.origin, "rev-list", "%s..%s" % (run.before["refs/heads/main"],
                                                         run.after["refs/heads/main"])).split()
    stray = [sha for sha in new if sha not in run.race]
    expect(not stray, "origin main gained commits the fixtures did not make: %r" % stray)


def result(case, run, status, plugin, version, committed):
    equal(run.rc, 0, "exit code (stderr %r)" % run.err)
    payload = one_json(run.out, "stdout")
    commit = case.tip() if committed else None
    equal(payload, {"status": status, "plugin": plugin, "version": version, "commit": commit}, "result")
    if not committed:
        no_commit(case, run)


def refused(case, run, message):
    equal(run.rc, 1, "exit code (stdout %r)" % run.out)
    equal(run.out, "", "stdout")
    equal(one_json(run.err, "stderr"), {"error": message}, "error")
    no_commit(case, run)


def refused_early(case, run, message):
    """A refusal from the inputs or the state: before validation, and the clone left as it was."""
    refused(case, run, message)
    equal(len(run.validates), 0, "validate calls")
    equal(case.git(case.work, "status", "--porcelain"), run.work_status, "the clone's status")
    equal(case.git(case.work, "rev-parse", "HEAD"), run.work_head, "the clone's HEAD")


def published(case, run, status, data, catalogs, parent=None):
    """One commit on main, by the bot, with the exact message, touching only the changed catalogs, which now
    hold exactly <catalogs>; validate ran on that very candidate."""
    result(case, run, status, data["PLUGIN"], data["VERSION"], True)
    tip = case.tip()
    parent = parent or (run.race[-1] if run.race else run.before["refs/heads/main"])
    equal(case.git(case.origin, "rev-list", "%s..%s" % (parent, tip)).split(), [tip], "new commits on main")
    equal(case.git(case.origin, "rev-list", "--parents", "-n", "1", tip).split(), [tip, parent], "parents")
    message = "chore: publish %s %s\n\nSource: %s v%s %s" % (data["PLUGIN"], data["VERSION"], data["SOURCE"],
                                                             data["VERSION"], data["SHA"])
    equal(case.git(case.origin, "log", "-1", "--format=%B", tip).rstrip("\n"), message, "commit message")
    equal(case.git(case.origin, "log", "-1", "--format=%an|%cn", tip).strip(), BOT + "|" + BOT, "identity")
    before = {rel: case.show(parent, rel) for rel in (CL, CX)}
    changed = sorted(rel for rel in (CL, CX) if catalogs[rel] != before[rel])
    names = case.git(case.origin, "diff-tree", "--no-commit-id", "--name-only", "-r", tip).split()
    equal(sorted(names), changed, "files the commit changes")
    for rel in (CL, CX):
        equal(case.show(tip, rel), catalogs[rel], rel + " on origin main")
    expect(run.validates, "validate never ran")
    equal(run.validates[-1]["catalogs"], {CL: catalogs[CL], CX: catalogs[CX]}, "the validated candidate")


CASES = []


def case(cid, title):
    def register(function):
        CASES.append((cid, title, function))
        return function
    return register


# A1: first publication

ALLOW_NEWBIE = canonical({"kobo": "tomasmarcik/kobo", "newbie": "someone/newbie"})


def newbie(**override):
    data = {"PLUGIN": "newbie", "SOURCE": "someone/newbie", "VERSION": "0.1.0", "SHA": "4" * 40,
            "CLAUDE_PATH": "claude/newbie", "CODEX_PATH": "codex/newbie"}
    data.update(override)
    return data


@case("A1a", "first publication appends {name, source} to both catalogs; the rest is untouched")
def a1a(c):
    c.build({ALLOW: ALLOW_NEWBIE})
    data = newbie()
    run = c.update(data)
    published(c, run, "added", data, {
        CL: appended(SEED_CLAUDE, "newbie", claude_source("someone/newbie", "claude/newbie", "0.1.0", "4" * 40)),
        CX: appended(SEED_CODEX, "newbie", codex_source("someone/newbie", "codex/newbie", "0.1.0"))})


@case("A1b", "first publication for Claude only appends to the Claude catalog alone")
def a1b(c):
    c.build({ALLOW: ALLOW_NEWBIE})
    data = newbie(CODEX_PATH="")
    run = c.update(data)
    published(c, run, "added", data, {
        CL: appended(SEED_CLAUDE, "newbie", claude_source("someone/newbie", "claude/newbie", "0.1.0", "4" * 40)),
        CX: SEED_CODEX})


@case("A1c", "first publication for Codex only appends to the Codex catalog alone")
def a1c(c):
    c.build({ALLOW: ALLOW_NEWBIE})
    data = newbie(CLAUDE_PATH="")
    run = c.update(data)
    published(c, run, "added", data, {
        CL: SEED_CLAUDE, CX: appended(SEED_CODEX, "newbie", codex_source("someone/newbie", "codex/newbie", "0.1.0"))})


# A2: upgrade

def multi_files():
    """kobo between two other plugins, out of name order, with hand fields on every entry."""
    zeta_cl = {"category": "productivity", "name": "zeta",
               "source": claude_source("someone/zeta", "plugins/zeta", "2.0.0", "e" * 40)}
    alpha_cl = {"name": "alpha", "source": claude_source("someone/alpha", "alpha", "1.2.3", "a" * 40)}
    claude = json.loads(SEED_CLAUDE)
    claude["plugins"][0]["keywords"] = ["agents", "workflow"]
    claude["plugins"] = [zeta_cl, claude["plugins"][0], alpha_cl]
    zeta_cx = {"name": "zeta", "policy": {"authentication": "ON_USE", "installation": "AVAILABLE"},
               "source": codex_source("someone/zeta", "codex/zeta", "2.0.0")}
    codex = json.loads(SEED_CODEX)
    codex["plugins"] = [zeta_cx, codex["plugins"][0]]
    return {ALLOW: canonical({"alpha": "someone/alpha", "kobo": "tomasmarcik/kobo", "zeta": "someone/zeta"}),
            CL: canonical(claude), CX: canonical(codex)}


@case("A2a", "upgrade replaces only kobo's sources: hand fields, other entries, order, top level kept")
def a2a(c):
    files = multi_files()
    c.build(files)
    data = kobo()
    run = c.update(data)
    published(c, run, "published", data, {
        CL: with_source(files[CL], "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.4", SHA_044)),
        CX: with_source(files[CX], "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.4"))})


@case("A2b", "upgrade strips a leading ./: Claude path stored bare, Codex path stored with ./")
def a2b(c):
    c.build()
    data = kobo(CLAUDE_PATH="./src/kobo", CODEX_PATH="./native/kobo")
    run = c.update(data)
    published(c, run, "published", data, {
        CL: with_source(SEED_CLAUDE, "kobo", claude_source("tomasmarcik/kobo", "src/kobo", "0.4.4", SHA_044)),
        CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "native/kobo", "0.4.4"))})


SOLO_SHA = "2" * 40


def solo_files():
    claude = appended(SEED_CLAUDE, "solo", claude_source("someone/solo", "plugin", "1.0.0", SOLO_SHA))
    return {ALLOW: canonical({"kobo": "tomasmarcik/kobo", "solo": "someone/solo"}), CL: claude}


def solo(**override):
    data = {"PLUGIN": "solo", "SOURCE": "someone/solo", "VERSION": "1.0.0", "SHA": SOLO_SHA,
            "CLAUDE_PATH": "plugin", "CODEX_PATH": ""}
    data.update(override)
    return data


@case("A2c", "upgrade adding a runner replaces the Claude source and appends the Codex entry")
def a2c(c):
    files = solo_files()
    c.build(files)
    data = solo(VERSION="1.1.0", SHA="3" * 40, CODEX_PATH="codex/solo")
    run = c.update(data)
    published(c, run, "published", data, {
        CL: with_source(files[CL], "solo", claude_source("someone/solo", "plugin", "1.1.0", "3" * 40)),
        CX: appended(SEED_CODEX, "solo", codex_source("someone/solo", "codex/solo", "1.1.0"))})


# A3: unchanged

@case("A3a", "G1 offline: kobo 0.4.3's data against the seeded catalogs is unchanged, no commit")
def a3a(c):
    c.build()
    run = c.update(G1)
    result(c, run, "unchanged", "kobo", "0.4.3", False)
    for rel, text in ((CL, SEED_CLAUDE), (CX, SEED_CODEX)):
        equal(c.show("refs/heads/main", rel), text, rel)


@case("A3b", "the same data again after a publish is unchanged: one publish commit in all")
def a3b(c):
    c.build()
    first = c.update(kobo())
    result(c, first, "published", "kobo", "0.4.4", True)
    tip = c.tip()
    again = c.update(kobo())
    result(c, again, "unchanged", "kobo", "0.4.4", False)
    equal(c.tip(), tip, "origin main")


@case("A3c", "the ./ spelling of the published paths is the same data: unchanged")
def a3c(c):
    c.build(solo_files())
    run = c.update(kobo("0.4.3", SHA_043, CLAUDE_PATH="./plugin/kobo", CODEX_PATH="./plugins/kobo"))
    result(c, run, "unchanged", "kobo", "0.4.3", False)
    run = c.update(solo(CLAUDE_PATH="./plugin"))
    result(c, run, "unchanged", "solo", "1.0.0", False)


# A4: the same version with different data

REWRITE = "kobo 0.4.3 differs from its published %s entry; a published version's pointer is never rewritten"


@case("A4a", "same version, different SHA: refused, no commit")
def a4a(c):
    c.build()
    refused_early(c, c.update(dict(G1, SHA="1" * 40)), REWRITE % "claude")


@case("A4b", "same version, different Claude path: refused, no commit")
def a4b(c):
    c.build()
    refused_early(c, c.update(dict(G1, CLAUDE_PATH="plugins/kobo")), REWRITE % "claude")


@case("A4c", "same version, different Codex path: refused, no commit")
def a4c(c):
    c.build()
    refused_early(c, c.update(dict(G1, CODEX_PATH="plugin/kobo")), REWRITE % "codex")


@case("A4d", "same version naming a runner that version lacks: refused, no commit")
def a4d(c):
    c.build(solo_files())
    refused_early(c, c.update(solo(CODEX_PATH="codex/solo")),
                  "solo 1.0.0 is published without a codex entry; a published version's pointers are never changed")


# A5: older

@case("A5a", "lower version: older, no commit")
def a5a(c):
    c.build()
    result(c, c.update(kobo("0.4.2")), "older", "kobo", "0.4.2", False)


@case("A5b", "lower version with a different SHA and paths: older wins over the rewrite refusal")
def a5b(c):
    c.build()
    run = c.update(kobo("0.3.9", "9" * 40, CLAUDE_PATH="old/claude", CODEX_PATH="old/codex"))
    result(c, run, "older", "kobo", "0.3.9", False)


@case("A5c", "lower version naming a runner the plugin lacks: older")
def a5c(c):
    c.build(solo_files())
    result(c, c.update(solo(VERSION="0.9.0", CODEX_PATH="codex/solo")), "older", "solo", "0.9.0", False)


@case("A5d", "versions compare numerically, not as text")
def a5d(c):
    c.build({CL: with_source(SEED_CLAUDE, "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.9", "9" * 40)),
             CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.9"))})
    for version, status in (("0.4.10", "published"), ("0.4.9", "older"), ("0.10.0", "published"),
                            ("0.9.0", "older"), ("10.0.0", "published"), ("9.99.99", "older")):
        result(c, c.update(kobo(version, "c" * 40)), status, "kobo", version, status == "published")


# A6: refusals, one case each

def refusals(c, variants, message):
    for override in variants:
        data = kobo()
        data.update(override)
        try:
            refused_early(c, c.update(data), message(data) if callable(message) else message)
        except Failure as error:
            raise Failure("%r: %s" % (override, error))


@case("A6a", "unlisted plugin")
def a6a(c):
    c.build()
    refusals(c, [{"PLUGIN": "other"}, {"PLUGIN": "Kobo"}, {"PLUGIN": "kobo "}, {"PLUGIN": ""}],
             lambda data: "plugin %s is not in allowlist.json" % data["PLUGIN"])


@case("A6b", "a plugin allowlisted only in the local checkout, not on origin main")
def a6b(c):
    c.build({ALLOW: SEED_ALLOWLIST})
    c.allow_work = canonical({"kobo": "tomasmarcik/kobo", "other": "someone/other"})
    c.write(c.work, {ALLOW: c.allow_work})
    c.git(c.work, "commit", "-qam", "chore: allow other, locally")
    refusals(c, [{"PLUGIN": "other", "SOURCE": "someone/other"}], "plugin other is not in allowlist.json")


@case("A6c", "source not matching the allowlist")
def a6c(c):
    c.build()
    refusals(c, [{"SOURCE": s} for s in ("tomasmarcik/Kobo", "someone/kobo", "tomasmarcik/kobo.git",
                                         "https://github.com/tomasmarcik/kobo", "tomasmarcik/kobo/", "")],
             lambda data: "source %s is not the allowlisted source of kobo" % data["SOURCE"])


@case("A6d", "bad semver")
def a6d(c):
    c.build()
    refusals(c, [{"VERSION": v} for v in ("0.04.4", "00.4.4", "v0.4.4", "0.4", "0.4.4.1", "0.4.4-rc.1",
                                          "0.4.4+build", " 0.4.4", "0.4.4\n", "")],
             "VERSION must be MAJOR.MINOR.PATCH without leading zeros")


@case("A6e", "bad SHA")
def a6e(c):
    c.build()
    refusals(c, [{"SHA": s} for s in (SHA_044.upper(), SHA_044[:7], SHA_044 + "0", SHA_044[:39],
                                      "g" * 40, SHA_044[:39] + "\n", "")],
             "SHA must be 40 lowercase hex characters")


PATH_RULE = ("%s must be a normalized relative path of [A-Za-z0-9._-] segments, none empty, . or .., "
             "none starting with -")


def path_refusals(c, values):
    variants = [{key: value} for value in values for key in ("CLAUDE_PATH", "CODEX_PATH")]
    for override in variants:
        key = list(override)[0]
        refusals(c, [override], PATH_RULE % key)


@case("A6f", "a path with ..")
def a6f(c):
    c.build()
    path_refusals(c, ("..", "../kobo", "plugin/../kobo", "plugin/..", "./../kobo"))


@case("A6g", "a path with a backslash")
def a6g(c):
    c.build()
    path_refusals(c, ("plugin\\kobo", "\\plugin", "plugin\\"))


@case("A6h", "an absolute path")
def a6h(c):
    c.build()
    path_refusals(c, ("/plugin/kobo", "//plugin/kobo", "/"))


@case("A6i", "a path that is not normalized: ., trailing /, //, ./ alone")
def a6i(c):
    c.build()
    path_refusals(c, (".", "./", "./.", "plugin/kobo/", "plugin//kobo", "plugin/./kobo", "././plugin"))


@case("A6j", "no path at all, empty or unset")
def a6j(c):
    c.build()
    refusals(c, [{"CLAUDE_PATH": "", "CODEX_PATH": ""}, {"CLAUDE_PATH": None, "CODEX_PATH": None}],
             "at least one of CLAUDE_PATH and CODEX_PATH must be set")


@case("A6k", "a present runner missing from the dispatch")
def a6k(c):
    c.build()
    rule = "kobo has a %s entry, so %s must be supplied; dropping a runner is a hand edit"
    refusals(c, [{"CODEX_PATH": ""}, {"CODEX_PATH": None}], rule % ("codex", "CODEX_PATH"))
    refusals(c, [{"CLAUDE_PATH": ""}, {"CLAUDE_PATH": None}], rule % ("claude", "CLAUDE_PATH"))


@case("A6l", "a non-main GITHUB_REF in Actions")
def a6l(c):
    c.build()
    for ref in ("refs/heads/feature", "refs/tags/main", "refs/heads/main-next", "refs/pull/1/merge"):
        run = c.update(kobo(), ref=ref)
        refused_early(c, run, "only refs/heads/main publishes; this run is on %s" % ref)


@case("A6m", "current entries that disagree on the version")
def a6m(c):
    c.build({CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.2"))})
    refusals(c, [{"VERSION": v} for v in ("0.4.1", "0.4.2", "0.4.3", "0.4.4")],
             "kobo's current entries disagree on the version")


@case("A6n", "a path with a character outside [A-Za-z0-9._-], or a segment starting with -")
def a6n(c):
    c.build()
    path_refusals(c, ("plugin/ko bo", " plugin", "plugin\t", "plugin\nkobo", "plugin/kobo\n", "plugin/*",
                      "plugin:kobo", "plugin/kobo?", "plug\u00efn", "-plugin", "plugin/-kobo", "./-kobo"))


@case("A6o", "a non-canonical allowlist or catalog on main: refused, never reformatted")
def a6o(c):
    c.build()
    for files, rel in (({ALLOW: '{"kobo": "tomasmarcik/kobo"}\n'}, ALLOW),
                       ({ALLOW: SEED_ALLOWLIST, CL: json.dumps(json.loads(SEED_CLAUDE), indent=4, sort_keys=True)
                         + "\n"}, CL),
                       ({CL: SEED_CLAUDE.rstrip("\n")}, CL),
                       ({CL: SEED_CLAUDE, CX: unsorted(SEED_CODEX)}, CX)):
        c.push_fixture(files, "chore: hand-format %s" % rel)
        try:
            refused_early(c, c.update(kobo()), "%s is not canonical JSON" % rel)
        except Failure as error:
            raise Failure("%r: %s" % (sorted(files), error))


def state_refusals(c, variants, message):
    """Each variant (catalog, text) pushed to origin main as a hand edit, the other catalog as seeded; a dispatch
    that would otherwise publish is refused with message(catalog)."""
    seeds = {CL: SEED_CLAUDE, CX: SEED_CODEX}
    for index, (rel, text) in enumerate(variants):
        files = dict(seeds, **{rel: text})
        c.push_fixture(files, "chore: hand edit %d" % index)
        try:
            refused_early(c, c.update(kobo()), message(rel))
        except Failure as error:
            raise Failure("%s variant %d: %s" % (rel, index, error))


@case("A6p", "a current entry without a <prefix><semver> ref")
def a6p(c):
    c.build()
    state_refusals(c, [
        (CL, kobo_cl_source(ref="0.4.3")), (CL, kobo_cl_source(ref="v0.04.3")),
        (CL, kobo_cl_source(ref="codex-v0.4.3")), (CL, claude_edit(lambda d: d["plugins"][0]["source"].pop("ref"))),
        (CL, claude_edit(lambda d: d["plugins"][0].pop("source"))), (CX, kobo_cx_source(ref="v0.4.3")),
        (CX, kobo_cx_source(ref="codex-v0.4")), (CX, kobo_cx_source(ref=1))],
        lambda rel: "%s entry kobo has no %s<semver> ref" % (rel, "v" if rel == CL else "codex-v"))


@case("A6q", "a catalog on main with two entries named for the plugin")
def a6q(c):
    c.build()
    state_refusals(c, [(CL, claude_edit(lambda d: d["plugins"].append(dict(d["plugins"][0])))),
                       (CX, codex_edit(lambda d: d["plugins"].insert(0, {"name": "kobo"})))],
                   lambda rel: "%s has more than one entry named kobo" % rel)


@case("A6r", "a catalog on main without a plugins list of named entries")
def a6r(c):
    c.build()
    state_refusals(c, [
        (CL, claude_edit(lambda d: d.pop("plugins"))), (CL, claude_edit(lambda d: d.update(plugins={"kobo": {}}))),
        (CL, claude_edit(lambda d: d["plugins"].append("zeta"))),
        (CL, claude_edit(lambda d: d["plugins"].append({"source": {}}))),
        (CL, claude_edit(lambda d: d["plugins"].append({"name": 7}))), (CL, canonical([json.loads(SEED_CLAUDE)])),
        (CX, codex_edit(lambda d: d.update(plugins=None))), (CX, codex_edit(lambda d: d["plugins"].append([])))],
        lambda rel: "%s must hold a plugins list of named entries" % rel)


@case("A6s", "claude not on PATH: refused at validation, no commit, no attempt scratch left")
def a6s(c):
    c.build()
    trimmed = c.dir / "trimmed-bin"
    trimmed.mkdir()
    os.symlink(shutil.which("git", path=c.suite.path), str(trimmed / "git"))
    expect(shutil.which("claude", path=str(trimmed)) is None, "fixture error: a claude on the trimmed PATH")
    run = c.update(kobo(), extra={"PATH": str(trimmed)})
    refused(c, run, "claude not found on PATH")
    equal(len(run.validates), 0, "validate calls")
    equal(os.listdir(str(c.work / "tmp" / "publish")), [], "attempt scratch left in tmp/publish")


ROOT_RULE = "run from the root of an ai-plugins checkout"


@case("A6t", "update and check not run from the root of the checkout: refused")
def a6t(c):
    c.build()
    for cwd in (c.work / ".claude-plugin", c.work / ".agents" / "plugins", c.dir):
        try:
            refused_early(c, c.update(kobo(), cwd=cwd), ROOT_RULE)
        except Failure as error:
            raise Failure("update from %s: %s" % (cwd.relative_to(c.dir.parent), error))
    tree = c.tree("subdir")
    for cwd in (tree / "scripts", tree / ".github" / "workflows"):
        run = c.check(cwd)
        try:
            equal(run.rc, 1, "exit code (stdout %r)" % run.out)
            equal(run.out, "", "stdout")
            equal(one_json(run.err, "stderr"), {"error": ROOT_RULE}, "error")
            equal(len(run.validates), 0, "validate calls")
        except Failure as error:
            raise Failure("check from %s: %s" % (cwd.relative_to(c.dir.parent), error))


@case("A6u", "a credential in origin's URL reaches neither stdout nor stderr")
def a6u(c):
    c.build()
    # The https URL is blocked offline by GIT_ALLOW_PROTOCOL=file, and git's refusal omits it. git prints the
    # missing local path in full, so that variant fails if git's own output is passed through.
    for url in ("https://user:SECRET@127.0.0.1:1/x.git", str(c.dir / "user:SECRET@missing.git")):
        c.git(c.work, "remote", "set-url", "origin", url)
        run = c.update(kobo())
        try:
            for stream, text in (("stdout", run.out), ("stderr", run.err)):
                expect("SECRET" not in text, "%s carries the credential: %r" % (stream, text))
            refused_early(c, run, "could not fetch main from origin")
        except Failure as error:
            raise Failure("%s: %s" % (url, error))


# A7: concurrent pushes

README_EDIT = SEED_README + "\nA concurrent hand edit.\n"


@case("A7a", "a concurrent push between fetch and push: rebuilt on the new tip, never forced")
def a7a(c):
    c.build()
    data = kobo()
    run = c.update(data, rounds=[{"files": {"README.md": README_EDIT}, "message": "docs: concurrent edit"}])
    equal(len(run.race), 1, "concurrent pushes")
    published(c, run, "published", data, {
        CL: with_source(SEED_CLAUDE, "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.4", SHA_044)),
        CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.4"))},
        parent=run.race[0])
    equal(c.show("refs/heads/main", "README.md"), README_EDIT, "README.md on main")
    equal(len(run.validates), 2, "validate calls, one per attempt")


@case("A7b", "a concurrent hand edit of kobo's entries is kept: state is read again from origin")
def a7b(c):
    c.build()
    claude = edited(SEED_CLAUDE, lambda d: d["plugins"][0].update(category="productivity"))
    codex = edited(SEED_CODEX, lambda d: d["plugins"][0]["policy"].update(installation="INSTALLED_BY_DEFAULT"))
    data = kobo()
    run = c.update(data, rounds=[{"files": {CL: claude, CX: codex}, "message": "chore: recategorize kobo"}])
    published(c, run, "published", data, {
        CL: with_source(claude, "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.4", SHA_044)),
        CX: with_source(codex, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.4"))},
        parent=run.race[0])


@case("A7c", "a concurrent publish of a higher version: the rebuild classifies older, no commit")
def a7c(c):
    c.build()
    higher = {CL: with_source(SEED_CLAUDE, "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.5", "5" * 40)),
              CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.5"))}
    run = c.update(kobo(), rounds=[{"files": higher, "message": "chore: publish kobo 0.4.5"}])
    result(c, run, "older", "kobo", "0.4.4", False)
    equal(c.tip(), run.race[0], "origin main")


@case("A7d", "a concurrent allowlist change: the rebuild reads it and refuses")
def a7d(c):
    c.build()
    moved = canonical({"kobo": "tomasmarcik/kobo-next"})
    run = c.update(kobo(), rounds=[{"files": {ALLOW: moved}, "message": "chore: move kobo"}])
    refused(c, run, "source tomasmarcik/kobo is not the allowlisted source of kobo")
    equal(c.tip(), run.race[0], "origin main")


@case("A7e", "a stale checkout publishes on origin main's tip, not on the checkout")
def a7e(c):
    c.build()
    ahead = c.push_fixture({"README.md": README_EDIT})
    data = kobo()
    run = c.update(data)
    published(c, run, "published", data, {
        CL: with_source(SEED_CLAUDE, "kobo", claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.4", SHA_044)),
        CX: with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.4"))},
        parent=ahead)


# A8: three rejected pushes

PUSH_REJECTED = "main rejected the push on all 3 attempts; nothing was published"

@case("A8a", "origin rejects every push: exit 1 after exactly three attempts, origin unchanged")
def a8a(c):
    c.build()
    c.reject_flag.write_text("")
    run = c.update(kobo())
    refused(c, run, PUSH_REJECTED)
    equal(run.after, run.before, "origin refs")
    equal(len(run.pushes), 3, "push attempts that reached origin")


@case("A8b", "a concurrent push before each of three pushes: exit 1, no publish commit")
def a8b(c):
    c.build()
    rounds = [{"files": {"README.md": SEED_README + "\nedit %d\n" % (n + 1)}, "message": "docs: edit %d" % (n + 1)}
              for n in range(3)]
    run = c.update(kobo(), rounds=rounds)
    refused(c, run, PUSH_REJECTED)
    equal(len(run.race), 3, "concurrent pushes")
    equal(len(run.validates), 3, "validate calls, one per attempt")
    equal(c.tip(), run.race[-1], "origin main")


# A9: validation fails

@case("A9", "claude plugin validate failing on the candidate: exit 1 with its output, no commit")
def a9(c):
    c.build()
    run = c.update(kobo(), mode="fail")
    equal(run.rc, 1, "exit code")
    equal(run.out, "", "stdout")
    payload = one_json(run.err, "stderr")
    equal(sorted(payload), ["detail", "error"], "error keys")
    equal(payload["error"], "claude plugin validate --strict failed", "error")
    for marker in ("FAKE-STDOUT-MARKER", "FAKE-STDERR-MARKER"):
        expect(marker in payload["detail"], "detail lacks %s: %r" % (marker, payload["detail"]))
    no_commit(c, run)
    equal(len(run.validates), 1, "validate calls")
    candidate = run.validates[0]["catalogs"]
    equal(candidate[CL], with_source(SEED_CLAUDE, "kobo",
                                     claude_source("tomasmarcik/kobo", "plugin/kobo", "0.4.4", SHA_044)),
          "the validated Claude catalog")
    equal(candidate[CX], with_source(SEED_CODEX, "kobo", codex_source("tomasmarcik/kobo", "plugins/kobo", "0.4.4")),
          "the validated Codex catalog")


# A10: check

def check_passes(c, tree, mode="pass"):
    run = c.check(tree, mode)
    equal(run.rc, 0, "exit code (stderr %r)" % run.err)
    equal(one_json(run.out, "stdout"), {"plugins": {"kobo": "0.4.3"}, "status": "ok"}, "result")
    equal(len(run.validates), 1, "validate calls")
    equal(os.path.realpath(run.validates[0]["cwd"]), os.path.realpath(str(tree)), "validate cwd")


def check_fails(c, variants, message):
    """Each variant, seeded alone into a fresh layout tree, fails check with exactly <message>."""
    for index, (label, files, mutate) in enumerate(variants):
        tree = c.tree("%d" % index, files, mutate)
        run = c.check(tree)
        try:
            equal(run.rc, 1, "exit code (stdout %r)" % run.out)
            equal(run.out, "", "stdout")
            equal(one_json(run.err, "stderr"), {"error": message(label) if callable(message) else message},
                  "error")
        except Failure as error:
            raise Failure("%s: %s" % (label, error))


def claude_edit(change):
    return edited(SEED_CLAUDE, change)


def codex_edit(change):
    return edited(SEED_CODEX, change)


def kobo_cl_source(**fields):
    return claude_edit(lambda d: d["plugins"][0]["source"].update(fields))


def kobo_cx_source(**fields):
    return codex_edit(lambda d: d["plugins"][0]["source"].update(fields))


def git_steps(*steps):
    def mutate(c, tree):
        for step in steps:
            c.git(tree, *step)
    return mutate


@case("A10a", "check: the seed state passes, and validate runs on the tree")
def a10a(c):
    check_passes(c, c.tree("seed"))


@case("A10b", "check: other entry fields are free")
def a10b(c):
    check_passes(c, c.tree("free", {
        CL: claude_edit(lambda d: d["plugins"][0].update(description="hand", keywords=["a"], category="x")),
        CX: codex_edit(lambda d: d["plugins"][0].update(interface={"displayName": "Kobo"}, category="y"))}))


@case("A10c", "check: ignored files do not count against the layout")
def a10c(c):
    check_passes(c, c.tree("ignored", {"tmp/scratch.txt": "x\n", ".DS_Store": "", "scripts/.DS_Store": "",
                                       "tmp/nested/deep.json": "{}\n"}))


@case("A10d", "check: allowlist.json not canonical")
def a10d(c):
    check_fails(c, [
        ("one line", {ALLOW: '{"kobo": "tomasmarcik/kobo"}\n'}, None),
        ("four spaces", {ALLOW: '{\n    "kobo": "tomasmarcik/kobo"\n}\n'}, None),
        ("no newline", {ALLOW: SEED_ALLOWLIST.rstrip("\n")}, None),
        ("unsorted", {ALLOW: '{\n  "zeta": "someone/zeta",\n  "kobo": "tomasmarcik/kobo"\n}\n'}, None)],
        "allowlist.json is not canonical JSON")


@case("A10e", "check: allowlist.json not valid JSON")
def a10e(c):
    check_fails(c, [("truncated", {ALLOW: "{\n"}, None), ("empty", {ALLOW: ""}, None)],
                "allowlist.json is not valid JSON")


@case("A10f", "check: allowlist.json not mapping names to owner/repo")
def a10f(c):
    check_fails(c, [(label, {ALLOW: canonical(value)}, None) for label, value in (
        ("bare name", {"kobo": "kobo"}), ("number", {"kobo": 1}), ("three parts", {"kobo": "a/b/c"}),
        ("empty repo", {"kobo": "tomasmarcik/"}), ("empty owner", {"kobo": "/kobo"}), ("a list", ["kobo"]))],
        "allowlist.json must map each plugin name to an owner/repo")


@case("A10g", "check: a catalog that is not valid JSON")
def a10g(c):
    check_fails(c, [(CL, {CL: "{\n"}, None), (CX, {CX: "not json\n"}, None)],
                lambda rel: "%s is not valid JSON" % rel)


def unsorted(text):
    value = json.loads(text, object_pairs_hook=OrderedDict)
    return json.dumps(OrderedDict(reversed(list(value.items()))), indent=2) + "\n"


@case("A10h", "check: a catalog that is not canonical")
def a10h(c):
    check_fails(c, [
        (CL + " one line", {CL: json.dumps(json.loads(SEED_CLAUDE), sort_keys=True) + "\n"}, None),
        (CL + " no newline", {CL: SEED_CLAUDE.rstrip("\n")}, None),
        (CX + " four spaces", {CX: json.dumps(json.loads(SEED_CODEX), indent=4, sort_keys=True) + "\n"}, None),
        (CX + " unsorted", {CX: unsorted(SEED_CODEX)}, None)],
        lambda label: "%s is not canonical JSON" % label.split(" ")[0])


@case("A10i", "check: a catalog not named ai-plugins")
def a10i(c):
    check_fails(c, [
        (CL, {CL: claude_edit(lambda d: d.update(name="kobo-claude"))}, None),
        (CX, {CX: codex_edit(lambda d: d.update(name="kobo-codex"))}, None),
        (CL, {CL: claude_edit(lambda d: d.pop("name"))}, None)],
        lambda rel: "%s must be named ai-plugins" % rel)


@case("A10j", "check: duplicate entry names")
def a10j(c):
    check_fails(c, [
        (CL, {CL: claude_edit(lambda d: d["plugins"].append(dict(d["plugins"][0])))}, None),
        (CX, {CX: codex_edit(lambda d: d["plugins"].append(dict(d["plugins"][0])))}, None)],
        lambda rel: "%s has duplicate entry names: kobo" % rel)


@case("A10k", "check: an entry that is not allowlisted")
def a10k(c):
    check_fails(c, [
        (CL, {CL: claude_edit(lambda d: d["plugins"][0].update(name="stray"))}, None),
        (CX, {CX: codex_edit(lambda d: d["plugins"][0].update(name="stray"))}, None)],
        lambda rel: "%s entry stray is not in allowlist.json" % rel)


@case("A10l", "check: an entry url that is not the allowlisted source's")
def a10l(c):
    check_fails(c, [
        (CL, {CL: kobo_cl_source(url="https://github.com/tomasmarcik/other.git")}, None),
        (CX, {CX: kobo_cx_source(url="https://github.com/tomasmarcik/kobo")}, None),
        (CL, {CL: kobo_cl_source(url="http://github.com/tomasmarcik/kobo.git")}, None),
        (CX, {CX: kobo_cx_source(url="https://gitlab.com/tomasmarcik/kobo.git")}, None)],
        lambda rel: "%s entry kobo url is not https://github.com/tomasmarcik/kobo.git" % rel)


@case("A10m", "check: a Claude source without the shape")
def a10m(c):
    def without(key):
        return claude_edit(lambda d: d["plugins"][0]["source"].pop(key))
    check_fails(c, [(label, {CL: text}, None) for label, text in (
        ("no sha", without("sha")), ("no ref", without("ref")), ("no path", without("path")),
        ("upper sha", kobo_cl_source(sha=SHA_043.upper())), ("short sha", kobo_cl_source(sha=SHA_043[:7])),
        ("leading zero", kobo_cl_source(ref="v0.04.3")), ("codex ref", kobo_cl_source(ref="codex-v0.4.3")),
        ("bare ref", kobo_cl_source(ref="0.4.3")), ("kind", kobo_cl_source(source="github")),
        ("dotdot path", kobo_cl_source(path="../kobo")), ("absolute path", kobo_cl_source(path="/plugin/kobo")))],
        ".claude-plugin/marketplace.json entry kobo does not have the claude source shape: "
        "git-subdir, url, normalized path, ref v<semver>, 40-hex sha")


@case("A10n", "check: a Codex source without the shape")
def a10n(c):
    check_fails(c, [(label, {CX: text}, None) for label, text in (
        ("claude ref", kobo_cx_source(ref="v0.4.3")), ("two-part ref", kobo_cx_source(ref="codex-v0.4")),
        ("a sha", kobo_cx_source(sha=SHA_043)), ("path without ./", kobo_cx_source(path="plugins/kobo")),
        ("dotdot path", kobo_cx_source(path="./../kobo")), ("kind", kobo_cx_source(source="local")))],
        ".agents/plugins/marketplace.json entry kobo does not have the codex source shape: "
        "git-subdir, url, normalized path, ref codex-v<semver>, no sha")


@case("A10o", "check: a plugin with a different version in each catalog")
def a10o(c):
    check_fails(c, [("codex ahead", {CX: kobo_cx_source(ref="codex-v0.4.4")}, None),
                    ("claude ahead", {CL: kobo_cl_source(ref="v0.5.0")}, None)],
                "plugins with a different version in each catalog: kobo")


@case("A10p", "check: a file outside the layout, tracked or untracked")
def a10p(c):
    check_fails(c, [
        ("notes.txt", {"notes.txt": "x\n"}, None),
        ("docs/a.md", {"docs/a.md": "x\n"}, git_steps(("add", "docs/a.md"), ("commit", "-qm", "docs: add"))),
        ("scripts/__pycache__/publish.cpython-39.pyc", {"scripts/__pycache__/publish.cpython-39.pyc": "x"}, None),
        (".github/workflows/extra.yml", {".github/workflows/extra.yml": "name: x\n"}, None)],
        lambda rel: "files outside the layout: %s" % rel)


@case("A10q", "check: claude plugin validate failing")
def a10q(c):
    run = c.check(c.tree("validate"), mode="fail")
    equal(run.rc, 1, "exit code")
    equal(run.out, "", "stdout")
    payload = one_json(run.err, "stderr")
    equal(sorted(payload), ["detail", "error"], "error keys")
    equal(payload["error"], "claude plugin validate --strict failed", "error")
    for marker in ("FAKE-STDOUT-MARKER", "FAKE-STDERR-MARKER"):
        expect(marker in payload["detail"], "detail lacks %s: %r" % (marker, payload["detail"]))


@case("A10r", "check: a source path with a character outside [A-Za-z0-9._-], or a segment starting with -")
def a10r(c):
    shape = "%s entry kobo does not have the %s source shape: git-subdir, url, normalized path, ref %s<semver>, %s"
    shapes = {CL: shape % (CL, "claude", "v", "40-hex sha"), CX: shape % (CX, "codex", "codex-v", "no sha")}
    check_fails(c, [("%s %r" % (CL, path), {CL: kobo_cl_source(path=path)}, None) for path in (
        "plugin/ko bo", "plugin/kobo\n", "plugin/*", "plugin:kobo", "-plugin", "plugin/-kobo")] + [
        ("%s %r" % (CX, path), {CX: kobo_cx_source(path=path)}, None) for path in (
            "./plugins/ko bo", "./plugins/*", "./-kobo")],
        lambda label: shapes[label.split(" ")[0]])


def main():
    if not SCRIPT.is_file():
        raise SystemExit("no %s" % SCRIPT)
    tmp = ROOT / "tmp"
    made_tmp = not tmp.exists()
    tmp.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="test-publish-", dir=str(tmp)))
    suite = Suite(scratch)
    passed, failed, audits = 0, 0, []
    for index, (cid, title, function) in enumerate(CASES, 1):
        c = Case(suite, scratch / ("%02d-%s" % (index, cid)))
        try:
            function(c)
            print("ok %s %s" % (cid, title), flush=True)
            passed += 1
        except Failure as error:
            print("NOT OK %s %s: %s" % (cid, title, error), flush=True)
            failed += 1
        except Exception as error:  # a broken fixture is a failure too, never a skip
            print("NOT OK %s %s: %s: %s" % (cid, title, type(error).__name__, error), flush=True)
            failed += 1
        audits.append((cid, c.audited_runs, c.audit_failures))
    unaudited = [cid for cid, runs, _ in audits if runs == 0]
    broken = ["%s: %s" % (cid, "; ".join(problems)) for cid, _, problems in audits if problems]
    title = "allowlist.json is never written, on origin or in the checkout, in any case above"
    if broken or unaudited:
        print("NOT OK A11 %s: %s" % (title, "; ".join(broken + ["%s ran nothing" % u for u in unaudited])))
        failed += 1
    else:
        print("ok A11 %s (%d runs)" % (title, sum(runs for _, runs, _ in audits)))
        passed += 1
    print("%d ok, %d NOT OK" % (passed, failed))
    if failed:
        print("evidence kept in %s" % scratch.relative_to(ROOT))
        return 1
    shutil.rmtree(str(scratch))
    if made_tmp:
        try:
            tmp.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
