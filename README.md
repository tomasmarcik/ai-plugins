# ai-plugins

A catalog-only plugin marketplace for Claude and Codex. It holds two catalogs and no plugin content: each entry points
at a plugin's own source repository by an immutable release tag.

## Install a plugin

Replace `<plugin>` with a plugin name from the catalogs, for example `kobo`.

- Each plugin is fetched from its own source repository. Claude Code and Codex fetch it with your Git credentials,
  which must be able to read it; how the Claude app fetches a private source is not yet confirmed.
- Use one install path: an install from the Claude app also appears in Claude Code.

### Claude app

1. **Add marketplace**: `tomasmarcik/ai-plugins`.
2. Find the plugin and choose **Add**.
3. **Sync automatically** or **Check for updates** pulls later catalog changes.

### Claude Code CLI

```sh
claude plugin marketplace add tomasmarcik/ai-plugins
claude plugin install <plugin>@ai-plugins
```

Automatic updates are off by default for an added marketplace. Turn them on in `/plugin` > **Marketplaces** >
**ai-plugins** > **Enable auto-update**, or refresh by hand:

```sh
claude plugin marketplace update ai-plugins
claude plugin update <plugin>@ai-plugins
```

Claude Code fetches a plugin's source with your Git credentials.

### Codex

```sh
codex plugin marketplace add https://github.com/tomasmarcik/ai-plugins.git
codex plugin add <plugin>@ai-plugins
```

Refresh the catalog with `codex plugin marketplace upgrade ai-plugins`; `codex plugin list` shows what is installed.
Codex fetches a plugin's source with your Git credentials.

## How publishing works

A source repository's release job sends its release data to this repository's `publish.yml`. ai-plugins never reads
the source.

- **Entries are release pointers.** Claude entries name the tag `v<version>` and its commit SHA; Codex entries name the
  tag `codex-v<version>`.
- **The allowlist decides who publishes.** `allowlist.json` maps each plugin name to its one source repository. It
  changes only by hand, in a reviewed commit or pull request. The workflow reads it and never writes it.
- **The dispatch is data only:** `plugin`, `source`, `version`, `sha`, `claude-path` and `codex-path`. A path is empty
  when the plugin ships nothing for that runner. Otherwise it is relative, and each of its segments is made of
  `[A-Za-z0-9._-]`, is not `.` or `..`, and does not start with `-`.
- **Only `source` is written.** `scripts/publish.py update` rewrites that plugin's `source` objects and nothing else,
  then pushes one commit, `chore: publish <plugin> <version>`, to `main`. Other entry fields, such as the Claude
  `category` and the Codex `policy` and `category`, are edited by hand and kept.
- **A first publication** appends `name` and `source` entries. Add their `category`, and any Codex `policy`, by hand.

| Result      | When                                   | Commit |
|-------------|----------------------------------------|--------|
| `added`     | the plugin has no entries yet          | yes    |
| `published` | the version is above the published one | yes    |
| `unchanged` | the same version, with the same data   | no     |
| `older`     | the version is below the published one | no     |
| refused     | any of the cases below                 | no     |

A refused run exits 1 with the error on stderr. It is refused for:

- input: a version that is not strict semver, a SHA that is not 40 lowercase hex, a bad path, or no path at all;
- the allowlist: a plugin not in `allowlist.json`, or a `source` other than its allowlisted one;
- a missing runner: the plugin has an entry for a runner whose path the dispatch leaves empty;
- a ref other than `main`: only `main` publishes;
- the state on `main`: a malformed or non-canonical allowlist or catalog, or entries for the plugin that lack a
  version or disagree on it;
- a rewrite: the same version, with a different SHA, path or set of runners;
- validation: `claude plugin validate --strict` failing on the candidate.

A run that is not started from the root of an ai-plugins checkout, cannot fetch `main`, or has its push rejected on all
three attempts also exits 1 and publishes nothing; more than three simultaneous publishes can exhaust those attempts,
which shows as a failed run to rerun.

`verify.yml` runs `scripts/test-publish.py` and `scripts/publish.py check` on every push and pull request, except a
publish commit: a push made with the workflow's `GITHUB_TOKEN` starts no workflow. `update` checks its own candidate
instead: the scope of the change, then `claude plugin validate --strict`.

To publish by hand with your own GitHub login, leaving out a path the plugin does not ship:

```sh
gh workflow run publish.yml --repo tomasmarcik/ai-plugins -f plugin=<plugin> -f source=<owner>/<repo> \
  -f version=<version> -f sha=<sha> -f claude-path=<claude-path> -f codex-path=<codex-path>
```

## Source release contract

Before it dispatches, a source repository publishing into ai-plugins guarantees:

1. An annotated tag `v<version>` (strict semver) on GitHub, pointing at commit `<sha>`, never moved.
2. Claude: at `<sha>`, `<claude-path>/.claude-plugin/plugin.json` has `name` `<plugin>` and `version` `<version>`.
3. Codex: an immutable tag `codex-v<version>` on GitHub, whose tree holds the native plugin at `<codex-path>`. Its
   `.codex-plugin/plugin.json` has the same name and version.
4. The release passed the source's own verification. ai-plugins checks none of this, because it never reads the source.
5. Claude Code and Codex installers can read the source repository with their Git credentials: public, or private and
   shared with each of them. How the Claude app fetches a private source is not yet confirmed.
6. Versions only rise, and each package manifest pins `version`. A dispatch at or below the published version changes
   nothing.
7. The source dispatches once, at the end of its release job, with the dispatch token: a fine-grained token with
   Actions write on `tomasmarcik/ai-plugins` only, kept as a repository secret. It can start the workflow; it cannot
   push.
