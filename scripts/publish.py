#!/usr/bin/env python3
"""Publish one plugin's release pointers into the ai-plugins catalogs, or check the repository's structure.

update: reads PLUGIN, SOURCE, VERSION, SHA, CLAUDE_PATH and CODEX_PATH from the environment, rewrites only that
plugin's entries on origin's main, and pushes fast-forward. It never reads the plugin's source repository.
check: offline structural check of the working tree, run on every push and pull request.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/(?!\.\.?\Z)[A-Za-z0-9_.-]+\Z")
NAME = "ai-plugins"
ALLOWLIST = "allowlist.json"
CATALOGS = {"claude": ".claude-plugin/marketplace.json", "codex": ".agents/plugins/marketplace.json"}
REF_PREFIX = {"claude": "v", "codex": "codex-v"}
SOURCE_KEYS = {"claude": {"source", "url", "path", "ref", "sha"}, "codex": {"source", "url", "path", "ref"}}
LAYOUT = frozenset({
    "README.md", "allowlist.json", ".gitignore", *CATALOGS.values(), ".github/workflows/publish.yml",
    ".github/workflows/verify.yml", "scripts/publish.py", "scripts/test-publish.py",
})
REMOTE = "origin"
MAIN = "refs/heads/main"
ATTEMPTS = 3
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class PublishError(Exception):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


def require(condition, message):
    if not condition:
        raise PublishError(message)


def canonical(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def parse(text, path):
    try:
        return json.loads(text)
    except ValueError:
        raise PublishError(path + " is not valid JSON") from None


def semver(text):
    match = SEMVER.fullmatch(text) if isinstance(text, str) else None
    return tuple(map(int, match.groups())) if match else None


def ref_version(runner, source):
    ref = source.get("ref") if isinstance(source, dict) else None
    prefix = REF_PREFIX[runner]
    return semver(ref[len(prefix):]) if isinstance(ref, str) and ref.startswith(prefix) else None


def relative_path(text):
    """The normalized relative form of text, without a leading ./, or None."""
    if text.startswith("./"):
        text = text[2:]
    if not text or "\\" in text or any(part in ("", ".", "..") for part in text.split("/")):
        return None
    return text


def source_url(repository):
    return "https://github.com/" + repository + ".git"


def entry_source(runner, repository, version, sha, path):
    if runner == "claude":
        return {"source": "git-subdir", "url": source_url(repository), "path": path, "ref": "v" + version, "sha": sha}
    return {"source": "git-subdir", "url": source_url(repository), "path": "./" + path, "ref": "codex-v" + version}


def has_entry_shape(runner, source):
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS[runner]:
        return False
    if not all(isinstance(value, str) for value in source.values()) or source["source"] != "git-subdir":
        return False
    path = relative_path(source["path"])
    if path is None or source["path"] != ("./" + path if runner == "codex" else path):
        return False
    return ref_version(runner, source) is not None and (runner == "codex" or SHA.fullmatch(source["sha"]) is not None)


def load_allowlist(text):
    allowlist = parse(text, ALLOWLIST)
    require(isinstance(allowlist, dict) and all(name and isinstance(repository, str) and REPOSITORY.fullmatch(repository)
                                                for name, repository in allowlist.items()),
            ALLOWLIST + " must map each plugin name to an owner/repo")
    return allowlist


def entries(catalog, path):
    plugins = catalog.get("plugins") if isinstance(catalog, dict) else None
    require(isinstance(plugins, list) and all(isinstance(entry, dict) and isinstance(entry.get("name"), str)
                                              for entry in plugins),
            path + " must hold a plugins list of named entries")
    return plugins


def find(catalog, plugin, path):
    found = [entry for entry in entries(catalog, path) if entry["name"] == plugin]
    require(len(found) <= 1, path + " has more than one entry named " + plugin)
    return found[0] if found else None


def run(args, cwd=None, data=None, env=None):
    try:
        return subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise PublishError(args[0] + " not found on PATH") from None


class Repository:
    def __init__(self):
        result = run(["git", "rev-parse", "--show-toplevel"])
        require(result.returncode == 0, "run from the root of an ai-plugins checkout")
        self.root = Path(result.stdout.decode().strip()).resolve()
        require(Path.cwd().resolve() == self.root, "run from the root of an ai-plugins checkout")
        self.env = os.environ.copy()

    def git(self, *args, data=None, env=None, message=None):
        # Report the operation, never git's output: a remote URL or header may carry a credential.
        result = run(["git", *args], data=data, env=env or self.env)
        require(result.returncode == 0, message or "git " + args[0] + " failed")
        return result.stdout.decode().strip()

    def validate(self, directory):
        result = run(["claude", "plugin", "validate", "--strict", "."], cwd=directory, env=self.env)
        if result.returncode:
            output = (result.stdout + result.stderr).decode(errors="replace").strip()
            raise PublishError("claude plugin validate --strict failed", output)

    def files(self):
        listing = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], env=self.env)
        require(listing.returncode == 0, "git ls-files failed")
        return [name for name in listing.stdout.decode().split("\0") if name]

    def check(self):
        text = self.read(ALLOWLIST)
        require(text == canonical(parse(text, ALLOWLIST)), ALLOWLIST + " is not canonical JSON")
        allowlist = load_allowlist(text)
        versions = {}
        for runner, path in CATALOGS.items():
            text = self.read(path)
            catalog = parse(text, path)
            require(text == canonical(catalog), path + " is not canonical JSON")
            require(isinstance(catalog, dict) and catalog.get("name") == NAME, path + " must be named " + NAME)
            names = [entry["name"] for entry in entries(catalog, path)]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            require(not duplicates, path + " has duplicate entry names: " + ", ".join(duplicates))
            for entry in catalog["plugins"]:
                name, source = entry["name"], entry.get("source")
                require(name in allowlist, path + " entry " + name + " is not in " + ALLOWLIST)
                require(has_entry_shape(runner, source), path + " entry " + name + " does not have the " + runner
                        + " source shape: git-subdir, url, normalized path, ref " + REF_PREFIX[runner] + "<semver>"
                        + (", 40-hex sha" if runner == "claude" else ", no sha"))
                require(source["url"] == source_url(allowlist[name]),
                        path + " entry " + name + " url is not " + source_url(allowlist[name]))
                versions.setdefault(name, set()).add(ref_version(runner, source))
        split = sorted(name for name, found in versions.items() if len(found) > 1)
        require(not split, "plugins with a different version in each catalog: " + ", ".join(split))
        outside = sorted(set(self.files()) - LAYOUT)
        require(not outside, "files outside the layout: " + ", ".join(outside))
        self.validate(self.root)
        return {"status": "ok",
                "plugins": {name: ".".join(map(str, found.pop())) for name, found in sorted(versions.items())}}

    def read(self, path):
        target = self.root / path
        require(target.is_file(), path + " is missing")
        try:
            return target.read_bytes().decode()
        except ValueError:
            raise PublishError(path + " is not valid JSON") from None

    def blob(self, commit, path):
        result = run(["git", "cat-file", "blob", commit + ":" + path], env=self.env)
        require(result.returncode == 0, path + " is missing on main")
        try:
            return result.stdout.decode()
        except ValueError:
            raise PublishError(path + " is not valid JSON") from None

    def fetch_main(self):
        self.git("fetch", "--quiet", "--no-tags", REMOTE, MAIN, message="could not fetch main from " + REMOTE)
        return self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}")

    def candidate_tree(self, base, files, scratch):
        env = {**self.env, "GIT_INDEX_FILE": str(scratch / "index")}
        self.git("read-tree", base, env=env)
        for path, text in files.items():
            mode = self.git("ls-tree", base, "--", path).split(" ", 1)[0]
            blob = self.git("hash-object", "-w", "--stdin", data=text.encode())
            self.git("update-index", "--cacheinfo", mode, blob, path, env=env)
        tree = self.git("write-tree", env=env)
        changed = set(self.git("diff-tree", "-r", "--name-only", base, tree).splitlines())
        require(changed == set(files) and changed <= set(CATALOGS.values()),
                "candidate changes paths other than its catalogs: " + ", ".join(sorted(changed)))
        # The validator reads files, so the candidate tree is written out in full, away from the working tree.
        self.git("checkout-index", "--all", "--force", "--prefix=" + str(scratch / "tree") + "/", env=env)
        return tree

    def update(self):
        inputs = read_inputs()
        plugin, version = inputs["plugin"], inputs["version"]
        require(REMOTE in self.git("remote").splitlines(), "no " + REMOTE + " remote")
        scratch_root = self.root / "tmp" / "publish"
        scratch_root.mkdir(parents=True, exist_ok=True)
        self.env["TMPDIR"] = str(scratch_root)
        env = dict(self.env)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            env.update(GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL,
                       GIT_COMMITTER_NAME=BOT_NAME, GIT_COMMITTER_EMAIL=BOT_EMAIL)
        for _ in range(ATTEMPTS):
            base = self.fetch_main()
            allowlist = load_allowlist(self.blob(base, ALLOWLIST))
            require(plugin in allowlist, "plugin " + plugin + " is not in " + ALLOWLIST)
            require(inputs["source"] == allowlist[plugin],
                    "source " + inputs["source"] + " is not the allowlisted source of " + plugin)
            catalogs = {runner: parse(self.blob(base, path), path) for runner, path in CATALOGS.items()}
            status, candidate = classify(catalogs, inputs)
            if candidate is None:
                return result(status, inputs)
            files = {}
            for runner, path in CATALOGS.items():
                if candidate[runner] != catalogs[runner]:
                    files[path] = canonical(candidate[runner])
                    assert_scope(catalogs[runner], json.loads(files[path]), plugin, path)
            scratch = Path(tempfile.mkdtemp(prefix="attempt-", dir=scratch_root))
            try:
                tree = self.candidate_tree(base, files, scratch)
                self.validate(scratch / "tree")
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            message = ("chore: publish " + plugin + " " + version + "\n\nSource: " + inputs["source"] + " v" + version
                       + " " + inputs["sha"] + "\n")
            commit = self.git("commit-tree", tree, "-p", base, data=message.encode(), env=env)
            pushed = run(["git", "push", "--quiet", REMOTE, commit + ":" + MAIN], env=env)
            if pushed.returncode == 0:
                return result(status, inputs, commit)
        raise PublishError("main rejected the push on all " + str(ATTEMPTS) + " attempts; nothing was published")


def read_inputs():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        ref = os.environ.get("GITHUB_REF", "")
        require(ref == MAIN, "only " + MAIN + " publishes; this run is on " + (ref or "no ref"))
    inputs = {key.lower(): os.environ.get(key, "") for key in ("PLUGIN", "SOURCE", "VERSION", "SHA")}
    require(semver(inputs["version"]) is not None, "VERSION must be MAJOR.MINOR.PATCH without leading zeros")
    require(SHA.fullmatch(inputs["sha"]) is not None, "SHA must be 40 lowercase hex characters")
    paths = {}
    for runner in CATALOGS:
        key = runner.upper() + "_PATH"
        value = os.environ.get(key, "")
        if value:
            paths[runner] = relative_path(value)
            require(paths[runner] is not None, key + " must be a normalized relative path without .. or backslashes")
    require(paths, "at least one of CLAUDE_PATH and CODEX_PATH must be set")
    inputs["paths"] = paths
    return inputs


def classify(catalogs, inputs):
    """The result for these inputs against the current catalogs, and the candidate catalogs when one is written."""
    plugin, version = inputs["plugin"], inputs["version"]
    desired = {runner: entry_source(runner, inputs["source"], version, inputs["sha"], path)
               for runner, path in inputs["paths"].items()}
    current = {}
    for runner, path in CATALOGS.items():
        entry = find(catalogs[runner], plugin, path)
        if entry is not None:
            current[runner] = entry
    for runner in current:
        require(runner in desired, plugin + " has a " + runner + " entry, so " + runner.upper()
                + "_PATH must be supplied; dropping a runner is a hand edit")
    if current:
        versions = {}
        for runner, entry in current.items():
            versions[runner] = ref_version(runner, entry.get("source"))
            require(versions[runner] is not None,
                    CATALOGS[runner] + " entry " + plugin + " has no " + REF_PREFIX[runner] + "<semver> ref")
        require(len(set(versions.values())) == 1, plugin + "'s current entries disagree on the version")
        published = versions.popitem()[1]
        if published > semver(version):
            return "older", None
        if published == semver(version):
            for runner, source in desired.items():
                require(runner in current, plugin + " " + version + " is published without a " + runner
                        + " entry; a published version's pointers are never changed")
                require(current[runner].get("source") == source, plugin + " " + version + " differs from its published "
                        + runner + " entry; a published version's pointer is never rewritten")
            return "unchanged", None
    candidate = copy.deepcopy(catalogs)
    for runner, source in desired.items():
        entry = find(candidate[runner], plugin, CATALOGS[runner])
        if entry is None:
            candidate[runner]["plugins"].append({"name": plugin, "source": source})
        else:
            entry["source"] = source
    return ("published" if current else "added"), candidate


def assert_scope(parent, child, plugin, path):
    def without_source(catalog):
        rest = {key: value for key, value in catalog.items() if key != "plugins"}
        plugins = [{key: value for key, value in entry.items() if entry["name"] != plugin or key != "source"}
                   for entry in catalog["plugins"]]
        return rest, plugins

    parent_rest, parent_plugins = without_source(parent)
    child_rest, child_plugins = without_source(child)
    appended = parent_plugins + [{"name": plugin}]
    expected = parent_plugins if find(parent, plugin, path) is not None else appended
    require(parent_rest == child_rest and child_plugins == expected,
            path + " candidate changes more than the source of " + plugin)


def result(status, inputs, commit=None):
    return {"status": status, "plugin": inputs["plugin"], "version": inputs["version"], "commit": commit}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("update", help="publish one plugin's release data from the environment")
    commands.add_parser("check", help="offline structural check of the working tree")
    args = parser.parse_args()
    try:
        repository = Repository()
        outcome = repository.update() if args.command == "update" else repository.check()
    except (PublishError, OSError) as error:
        report = {"error": str(error)}
        if getattr(error, "detail", None):
            report["detail"] = error.detail
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
