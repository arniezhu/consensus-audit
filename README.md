# Consensus Audit

English | [简体中文](README.zh-CN.md)

An agent skill that gates non-trivial work behind **independent, adversarial review**
instead of the agent's own self-assessment.

Before an agent commits to a design or ships an implementation, `consensus-audit`
dispatches two or three reviewers that each see the work independently, parses their
structured verdicts, converges the findings, and decides whether the work may proceed.
It gates only on *grounded, reproducible, execution-blocking* blockers — and it fails
closed: an unparseable reviewer output or a failed invocation is a blocker, never a pass.

Runs on **Codex** (`codex exec`) or **Claude Code** (`claude --print`).

> **Before your first run.** Reviewer models default to the tiers this skill was developed
> against (`gpt-6.1-sol`). Model preflight fails closed, so if your account
> does not have them, **every run is blocked until you set the reviewer models** — see
> [Configuring models](#configuring-models). On the Claude Code backend the defaults
> resolve to a Claude model automatically and this usually does not apply.
>
> **A round is not cheap.** At `--risk-level high` one round is three independent reviewer
> sessions, and six when two or more blockers are open, because each reviewer then also runs a
> cross-review pass. Add one preflight probe per distinct reviewer model, and a reformat call for
> any review that does not parse. The standard tier is two sessions, or four with cross-review.
> Budget accordingly before pointing it at a large workspace.

## Why

Current frontier models routinely produce working, well-structured code in a single pass.
What capability does not change is that a model reviewing its own output inherits the
reasoning that produced it, so the errors most likely to survive review are the ones that
looked right on the way in. Asking the same agent "are you sure?" mostly produces
agreement — and a more capable agent produces a more convincing one.

This skill replaces that with separate reviewer sessions that never see each other's
output until cross-review, each with a different mandate, and a moderator that can only
block on evidence it can reproduce.

## How it works

Two phases, both persisted under `.discuss/<topic-slug>/round-<NN>-<phase>/`:

- **prework** — run before non-trivial implementation or before finalizing a design.
- **postwork** — run after implementation, before final delivery.

Each round produces a main-agent baseline, independent reviewer outputs, and a consensus
decision; a cross-review pass runs when two or more blockers are open, and a moderator
decision is requested when the round hits its cap or a blocker stagnates. Every artifact is
written to disk, so a decision can be audited after the fact rather than taken on trust.

Reviewers scale with declared risk:

| Reviewer | Mandate | Active |
|---|---|---|
| Tim Sir | protocol integrity, dependency coherence, architecture fit, evidence-gate logic | always |
| Sam Koo | forensic evidence, reproducibility, plan-vs-actual proof, edge-case traceability | always |
| Madam Leung | rollout safety, rollback, guardrails, operational blast radius | `--risk-level high\|critical` |

## Install

**Requirements:** `python3` with PyYAML (`python3 -m pip install pyyaml`). The runner reads
its `config/*.yaml` files with it; without it every round run as documented exits 1.

The installable package is the `skill/` directory. Copy it to your agent's skills
directory **under the name `consensus-audit`**:

**First install:**

```bash
# Claude Code
cp -r skill ~/.claude/skills/consensus-audit

# Codex
cp -r skill ~/.codex/skills/consensus-audit
```

**Upgrading an existing install — `cp -r` alone is not safe here.** POSIX `cp -R src dir`
copies *into* an existing directory, so it produces
`~/.claude/skills/consensus-audit/skill/` while the previous `SKILL.md` and `scripts/`
stay at the top level. The agent keeps loading the old version and the upgrade looks like
it succeeded. Replace the tree instead:

```bash
rm -rf ~/.claude/skills/consensus-audit
cp -r skill ~/.claude/skills/consensus-audit
```

That removes the old copy before the new one is checked, so a broken copy is what stays
installed. For an installation you depend on, use the staged install in
[`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md) instead: it runs a smoke round against a
staged copy and replaces the installed one only if that passes. No backup is kept; git
history is the rollback target.

The destination name matters: the install and verification procedures refer to
`consensus-audit`. Copying without it (`cp -r skill ~/.claude/skills/`) lands the package
at `~/.claude/skills/skill/` and those procedures will no longer match.

Which backend runs the reviewers is decided by where you install: `--agent auto` uses
Codex reviewers under `~/.codex/skills` and Claude Code reviewers under `~/.claude/skills`.
`--agent codex` and `--agent claude` may only restate what was detected; a mismatch is rejected
rather than silently honoured.

For an install that stages a copy, verifies it, and only then replaces the installed one,
follow [`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md).

## Usage

Installing is all the setup there is. After that the skill fires two ways:

- **The agent triggers it** when it hits a design proposal, an architecture decision, a
  refactor, a code change, a critical documentation change, or work that touches security,
  data, or production.
- **You ask for it** — say "consensus audit", "run prework on this", or "postwork".
  Chinese triggers work too: 「审计」「共识审计」「三方审计」.

When a round finishes, the verdict and every artifact behind it are under
`.discuss/<topic-slug>/`, ready to be read rather than taken on faith.

**`NEEDS_ANOTHER_ROUND` does not mean "do not ship."** It means open blockers remain, and
which of them actually block you is your call.

**Do not treat the round number as a convergence signal.** Fixing findings surfaces new
ones, including ones the fixes themselves introduce. This repository's own pre-release
audit went 5 open blockers, then 4, then 10 — every round closed what the previous round
raised and opened more, and not one blocker was ever carried between rounds. Rounds went
up while residual risk went up with them. The built-in `stagnation_count` and `STALE_STAY`
signals track a blocker that *repeats*, so in a run shaped like that one they never fire.
(`STALE_STAY` is not counted by the runner at all: it fires only when the orchestrator passes
`--cross-review-stay-count`; see the statuses in [`skill/SKILL.md`](skill/SKILL.md).)

Decide the exit condition **before** the round, in the baseline's `## Exit Condition`
section, while you still do not know what will be found: name what must hold to ship — for
example, no open blocker touching install, rollback, or data-deletion paths — and afterwards
check the round against it rather than re-reading the condition in light of the findings.
Declared in advance it is a judgement; declared afterwards it is a rationalisation, and the
party making it is the one whose work is under review.

**The exit condition is a floor, not a ceiling.** It names what definitely blocks; it does
not define the set of things allowed to block. A blocker that lands outside it — because
you did not anticipate that category — is not thereby cleared. Shipping past it takes an
explicit acceptance, written down in the round's record: this was found, this is why it is
acceptable, this is who decided. That turns an omission in your criteria from a silent
pass into a visible one. Nothing here stops a developer writing weak criteria and
accepting everything; the point is that the acceptance leaves a trace, and the criteria
are themselves part of what the reviewers read and can raise a blocker against.

When a round does raise new blockers, look at where they point. A finding in code the
previous round's fix touched is a regression you just caused, and another round of the
same is likely to cause another. A finding in code no previous round reached is depth,
and is worth paying for. `AUDIT_SUMMARY.md` separates `OPEN_BLOCKERS` from
`DISCARDED_REVIEWER_BLOCKERS`; read the discarded list too, because a blocker can be
dropped on a contract technicality while being substantively right.

## Configuring models

Reviewer models default to the tiers this skill was developed against. Override them per invocation with
`--reviewer-a-model` / `--reviewer-b-model` / `--reviewer-c-model`, or set them once:

```bash
export CONSENSUS_AUDIT_REVIEWER_A_MODEL=...   # Tim Sir
export CONSENSUS_AUDIT_REVIEWER_B_MODEL=...   # Sam Koo
export CONSENSUS_AUDIT_REVIEWER_C_MODEL=...   # Madam Leung
export CONSENSUS_AUDIT_CLAUDE_MODEL=...       # Claude Code backend
```

Precedence is explicit: the command-line flag wins, then the environment variable, then the
backend's own default. An environment override is honoured on both backends.

Model preflight probes exactly the reviewer models a round will use and fails closed, so if
your account does not have the default tiers, set the reviewer models above or preflight
will block every run. `CONSENSUS_AUDIT_REQUIRED_MODELS` and `--required-models` are
deprecated and ignored. The `model` entries in
[`skill/config/role_config.yaml`](skill/config/role_config.yaml) record the Codex defaults and
feed the provider check; editing them does not change which model runs.

All three daily reviewers default to `gpt-6.1-sol` on Codex and `claude-opus-5-5`
on Claude Code, with explicit `high` effort. Override effort with
`--reviewer-a-effort`, `--reviewer-b-effort`, or `--reviewer-c-effort`.
An explicit `adaptive` value only requests protocol-level self-declaration and
inherits the CLI's runtime effort setting.

Ordinary moderation uses the current main session. Difficult grounded disagreements
at `MODERATOR_DECISION_REQUIRED` may receive a separate read-only adjudication using `gpt-6-astra` on Codex or
`claude-fable-5-1` on Claude Code, with `high` effort. These models are not routine
reviewers or part of routine preflight. The skill orchestrator invokes and records
that conditional review; the runner does not launch it. `--main-moderator-model`
records the actual main session and does not switch models. See the
[adjudication policy](skill/SKILL.md#difficult-case-adjudication).

## Language

Narration and progress output follow the conversation. Write to the agent in Chinese and both
switch to Chinese; write in English and both stay English. There is nothing to configure — the
agent passes the conversation language to the runner on every invocation, and re-evaluates it
each turn.

Invoking the CLI directly, with no agent in the loop, falls back to English; pass
`--progress-language zh` for Chinese. Regional tags such as `en-US` and `zh-CN` are accepted.
`zh-TW` and `zh-Hant` are rejected rather than silently rendered as simplified Chinese — supply a
translated catalog via `--progress-messages-file` instead.

Reviewer verdicts, protocol identifiers such as `prework` and `APPROVE`, and the audit artifacts
under `.discuss/` are deliberately never translated, so parsing stays stable and rounds remain
comparable across languages.

## Layout

`skill/` is the install unit; everything beside it is repository-only and never ships.

| Path | Role |
|---|---|
| `skill/SKILL.md` | activation rules and compact workflow — the source of truth after install |
| `skill/references/consensus_protocol.md` | full reviewer contract, artifact schemas, failure codes |
| `skill/scripts/consensus_audit.py` | optional automation runner (retries, fail-closed parsing, artifact generation) |
| `skill/config/` | reviewer scopes, evidence policy, role defaults, provider and reflow rules |
| `skill/00-READ-FIRST.md` | install / rollback runbook for humans |
| `tests/` | test suite — repository-only |

## Upgrading

Commit `b3d4400` removed the opt-in canary/full optimization rollout, the review cache, `--break-glass-mode`
and the shadow-parity tools. Every round now runs every active reviewer live. If you call the runner from
scripts, update them:

| Removed flag | What to do |
|---|---|
| `--rollout-mode` with `canary` or `full` | Drop the flag. `--rollout-mode shadow` still runs, with a deprecation warning. |
| `--cache-mode`, `--canary-percent`, `--canary-seed`, `--auto-degrade` / `--no-auto-degrade`, `--metrics-window`, `--threshold-parse-fail-rate`, `--threshold-timeout-rate`, `--threshold-fallback-revise-rate`, `--force-shadow`, `--state-lock-timeout-sec`, `--state-lock-stale-sec`, `--force-lock-steal`, `--parity-verdict-threshold`, `--parity-blocker-symdiff-threshold`, `--parity-reason-code-threshold`, `--shadow-baseline-round-dir`, `--positive-smoke-round-dir`, `--failclosed-smoke-round-dir` | Delete the flag. It tuned or switched off something that no longer exists. |
| `--rollout-readiness-file`, `--shadow-parity-mode`, `--break-glass-mode` | Delete the flag, and do not rely on the gate it requested: that gate no longer exists. |

A removed flag stops the runner with an error naming the flag and the fix, before any round directory
is written. `--adaptive-effort-mode` never changed behaviour; it is now accepted with a deprecation warning.
Old `.discuss/` history needs no migration in either direction, and going back is `git revert` plus the
install block in [`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md). That path was rehearsed: the previous
runner read topics written by the current one as ordinary history.

## Tests

Run from the repository root:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## Notice

Reviewer role names are an homage to the TVB series *Forensic Heroes*
(《法證先鋒》). This project is unaffiliated with, and not endorsed by, TVB.

## License

MIT — see [LICENSE](LICENSE).
