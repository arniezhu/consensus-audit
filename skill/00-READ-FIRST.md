# Packaging Bootstrap Note

This file is for humans installing this source bundle. It is not the runtime protocol for the skill.

`README.md` is the project front page and lives at the repository root, outside this
package, so it is never installed. This file is the install runbook and ships with the
package. Do not merge the two, and do not rename this file to `README.md` inside the
installed skill package. Skill packages should keep the operational contract in `SKILL.md`
and load detailed references only when needed.

## Install

The runner needs `python3` with PyYAML (`python3 -m pip install pyyaml`): the documented
invocation passes the `config/*.yaml` files, and without PyYAML every round run as documented
exits 1. The smoke round below checks this before anything is replaced.

No backup of the previous version is kept: git history is the rollback target (see
[Rollback](#rollback-of-this-source-change)). Keeping one inside the skills directory is
worse than keeping none, because the agent registers every directory there as a skill, so
a backup becomes a second, stale `consensus-audit` with the same trigger words.

Because nothing is kept, the new copy is verified *before* it replaces the installed one.
The block stages a copy, checks it, runs a dry-run smoke round against it, and only then
swaps it in. If any check fails, or the block is interrupted, the installed copy is
untouched and the stage is removed.

The stage lives in `<parent of $DEST_ROOT>/.consensus-audit-staging.<pid>/skills/consensus-audit`,
not inside `$DEST_ROOT`: a directory inside the skills root is registered as a skill for as
long as it exists, which would be a duplicate for the whole smoke round. The staging path
still contains both `.claude` (or `.codex`) and `skills`, so the runner's path-based agent
detection resolves the staged copy to the same backend as the installed one. It is on the
same filesystem, so the final `mv` is a rename.

The block runs in `bash` explicitly, so it behaves the same when pasted into zsh (the macOS
default shell), where `status` is a read-only variable and other bash idioms differ.

```bash
bash -c '
set -eu
SOURCE_DIR="${SOURCE_DIR:-$(pwd)/skill}"   # run this from the repository root
DEST_ROOT="${DEST_ROOT:-$HOME/.codex/skills}"
DEST_DIR="$DEST_ROOT/consensus-audit"
STAGE_ROOT="$(dirname "$DEST_ROOT")/.consensus-audit-staging.$$"
STAGE_DIR="$STAGE_ROOT/skills/consensus-audit"
SMOKE="$(mktemp -d)"
trap "rm -rf \"$STAGE_ROOT\" \"$SMOKE\"" EXIT
trap "exit 130" INT TERM   # route signals through the EXIT trap

test -f "$SOURCE_DIR/SKILL.md"   # guards against running from the wrong directory
mkdir -p "$DEST_ROOT" "$STAGE_ROOT/skills"
cp -R "$SOURCE_DIR" "$STAGE_DIR"

python3 "$STAGE_DIR/scripts/consensus_audit.py" --help >/dev/null
for root in "$DEST_ROOT" "$HOME/.codex/skills" "$HOME/.claude/skills"; do
  V="$root/.system/skill-creator/scripts/quick_validate.py"
  if test -f "$V"; then python3 "$V" "$STAGE_DIR"; break; fi
done

# --help and quick_validate.py exit 0 on a package that cannot complete a round, so run
# one. A dry run exercises agent detection, config loading, and the artifact writer
# without spending reviewer calls; it exits 2 (NEEDS_ANOTHER_ROUND) by design. It passes
# the shipped configs as SKILL.md does, so a missing PyYAML fails here, before the swap.
printf "COVERAGE: changed/planned symbols 0 | refs 0 | formal_tests 0 | validation_commands 1 | uncovered 0\n" \
  > "$SMOKE/baseline.md"
C="$STAGE_DIR/config"
rc=0
python3 "$STAGE_DIR/scripts/consensus_audit.py" \
  --topic install-smoke --phase prework --risk-level low --progress-language en \
  --baseline-file "$SMOKE/baseline.md" --workspace "$SMOKE" \
  --classification-file "$C/classification.yaml" --evidence-manifest-file "$C/evidence_manifest.yaml" \
  --postwork-reflow-policy "$C/postwork_reflow_policy.yaml" --role-config "$C/role_config.yaml" \
  --provider-config "$C/provider_config.yaml" \
  --dry-run --peer-retries 0 --model-preflight-retries 0 >/dev/null 2>"$SMOKE/stderr" || rc=$?
test "$rc" = 2 || { cat "$SMOKE/stderr" >&2; echo "smoke round exited $rc, expected 2; installed copy untouched" >&2; exit 1; }
test -d "$SMOKE/.discuss/install-smoke" || { echo "smoke round produced no artifacts; installed copy untouched" >&2; exit 1; }

rm -rf "$DEST_DIR"
mv "$STAGE_DIR" "$DEST_DIR"
echo "installed: $DEST_DIR"
'
```

For Claude Code personal skills, install the same folder under:

```bash
export DEST_ROOT="$HOME/.claude/skills"  # then run the install block above
```

Exit codes map to round status: `0` CONSENSUS_READY, `2` NEEDS_ANOTHER_ROUND,
`3` MODERATOR_DECISION_REQUIRED, `4` STALE_STAY, `64` operator halt, `130` interrupted
(Ctrl-C or SIGTERM; in-flight reviewer processes are terminated). `1` is an invalid
invocation (a bad, missing or removed flag, an unreadable baseline or config), rejected
before any round directory is written. A non-zero status is
not automatically a failure, which is why the smoke check asserts the *expected* code
rather than success.

If the block is killed between `rm -rf "$DEST_DIR"` and `mv`, nothing is installed; run it
again. A leftover `.consensus-audit-staging.*` directory next to `$DEST_ROOT` is not a skill
and can be deleted.

### Leftovers from earlier versions of this runbook

Earlier versions kept a `.consensus-audit.backup.*` directory and a
`.consensus-audit.last-backup` record next to the install, and staged inside the skills root,
so an interrupted install could leave a `.consensus-audit.stage.*` directory there. None of
these are used any more, and any directory inside the skills root is registered as a
duplicate skill. Remove them:

```bash
bash -c '
set -eu
DEST_ROOT="${DEST_ROOT:-$HOME/.codex/skills}"
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 \( -name ".consensus-audit.backup.*" -o -name ".consensus-audit.stage.*" \) \
  -print -exec rm -rf {} +
rm -fv "$DEST_ROOT/.consensus-audit.last-backup"
'
```

## Migrating from `tri-consensus-audit`

This skill was previously named `tri-consensus-audit`. A legacy installation sits at a
*different* path (`$DEST_ROOT/tri-consensus-audit`), so the install above does not replace
it. While both exist, both skills are registered with overlapping trigger words (`审计`,
`prework`, `postwork`), and the agent may select either one.

Install `consensus-audit` first, then remove the legacy directory, so a working skill is
installed throughout:

```bash
bash -c '
set -eu
DEST_ROOT="${DEST_ROOT:-$HOME/.codex/skills}"
test -d "$DEST_ROOT/consensus-audit" || { echo "install consensus-audit first" >&2; exit 1; }
test -d "$DEST_ROOT/tri-consensus-audit" || { echo "no legacy install; nothing to migrate"; exit 0; }
rm -rf "$DEST_ROOT/tri-consensus-audit"
echo "legacy install removed"
'
```

## Verify

The install block already ran `--help`, the validator, and the smoke round against the
exact bytes it then moved into place. To check an existing installation later, confirm it
still parses and matches the source you meant to run. Run this from the repository root,
because `skill` in the `diff` is a relative path:

```bash
DEST_ROOT="${DEST_ROOT:-$HOME/.codex/skills}"
python3 "$DEST_ROOT/consensus-audit/scripts/consensus_audit.py" --help >/dev/null && echo "runner parses"
diff -rq -x __pycache__ skill "$DEST_ROOT/consensus-audit" && echo "installed copy matches checkout"
```

Both commands must resolve against `$DEST_ROOT`. Hardcoding one root means an operator
with both backends installed checks the copy they did not touch.

## Rollback of this source change

The runner, its configs, the `SKILL.md` contract, the references, this note, the tests,
and both READMEs change together; restoring a subset leaves the runner and its contract
diverged, which is the failure this section exists to prevent. A hand-maintained file list
drifts from the change set, so restore the whole tracked source instead. This discards
**every** uncommitted edit under these paths, so commit or stash anything unrelated first.
To undo an uncommitted change in this checkout, restore both the index and the working
tree so the staged and unstaged layers cannot diverge, then confirm nothing is left:

```bash
git restore --staged --worktree -- skill tests README.md README.zh-CN.md
git status --short -- skill tests README.md README.zh-CN.md   # expect no output
```

After a commit, use `git revert <commit>` instead of `git restore`.

Either way, git only rolls back the checkout. The copy an agent actually runs is the
installed package, which does not change until you reinstall. Once the checkout holds the
version you want, run the install block under [Install](#install) again, with `DEST_ROOT`
set to the root you installed into, then confirm with the `diff -rq` check under
[Verify](#verify).

Audit history needs no migration in either direction. Current runners no longer write
`.discuss/<topic>/80-rollout-state.json`, `.review-cache/`, or the round files
`06-rollout-readiness.json`, `15-acceptance-smoke-evidence.json`, `16-shadow-parity.json`,
`17-replay-acceptance.json` and `23-degraded-round.json`; copies left by older runners are
ignored and can be deleted. If you roll back to a runner that still has the canary/full
rollout, it reads topics written by the current runner as ordinary shadow-mode history.

By default the runner uses `--agent auto`: an install under `~/.codex/skills` runs Codex reviewers, and an install under `~/.claude/skills` runs Claude Code reviewers. `--agent codex`, `--agent claude`, and `CONSENSUS_AUDIT_AGENT=codex|claude` may only restate the detected current skill context; cross-agent mismatches are rejected. Auto-detection reads paths before the environment: if the runner's own path, or failing that the current directory, contains both a `.codex` (or `.claude`) directory and a `skills` directory anywhere in it, that agent is used. This covers `~/.codex/skills/...`, `~/.claude/skills/...` and the install block's staging copy, and equally any other path with those two components. Only when neither path settles it do the environment markers decide; if both Codex and Claude Code markers are set (for example one agent launched from inside the other, running a source checkout), auto-detection fails closed with `AGENT_CONTEXT_AMBIGUOUS`; pass `--agent codex` or `--agent claude` to name the agent running the skill.

## Canonical Files

- `SKILL.md`: activation rules and compact workflow.
- `references/consensus_protocol.md`: detailed reviewer contract, artifacts, statuses, and cross-review loop.
- `scripts/consensus_audit.py`: optional automation runner.
- `config/`: reviewer scopes, evidence policy, role defaults, provider policy, and postwork reflow rules.

Use `SKILL.md` as the source of truth after installation.
