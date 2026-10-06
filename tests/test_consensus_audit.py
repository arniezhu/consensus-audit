import argparse
import io
import json
import os
import re
import site
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from skill.scripts import consensus_audit as tc

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skill"

def isolated_install_env(home: Path, dest_root: Path, source: Path) -> dict[str, str]:
    """Isolate skill installs without hiding the invoking Python's user dependencies.

    PyYAML may be installed with --user. Changing HOME alone relocates Python's
    user site and makes that satisfied prerequisite disappear in the subprocess.
    The runbook invokes python3, so keep it on the same interpreter as the suite.
    PYTHONPATH remains available for deliberate missing/broken-module simulations.
    """
    return {
        **os.environ,
        "HOME": str(home),
        "DEST_ROOT": str(dest_root),
        "SOURCE_DIR": str(source),
        "PYTHONUSERBASE": site.getuserbase(),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
    }

def yaml_reading_log_config() -> dict:
    """Minimal reader for the reading_log block of the shipped evidence manifest."""
    text = (ROOT / "skill/config/evidence_manifest.yaml").read_text(encoding="utf-8")
    out: dict = {}
    in_block = False
    for line in text.splitlines():
        if line.startswith("reading_log:"):
            in_block = True
            continue
        if in_block:
            if line and not line.startswith((" ", "\t")):
                break
            stripped = line.split("#", 1)[0].strip()
            if ":" in stripped:
                k, v = (part.strip() for part in stripped.split(":", 1))
                out[k] = int(v) if v.isdigit() else v
    return out


CLAUDE_STYLE_REVIEW = """EFFORT_DECLARATION: high

## Verdict
APPROVE

## Risk Level
MEDIUM

## Blockers
1. None.

## Suggestions
1. Add a focused regression test for the backend path.

## Decisions
- DECISION: same-agent reviewer routing is preserved.
- DECISION: no execution blocker remains.
- DECISION: proceed after validation.

## Consensus Statement
No grounded execution blocker was found.

## READING_LOG
| # | path | total_lines | sha256_first8 | status |
|---|---|---:|---|---|
| 1 | <embedded-review> | 1 | 00000000 | READ |

## INJECTION_FINDINGS
1. None.
"""


# A language-neutral custom catalog, used to exercise --progress-messages-file
# (built-in languages are zh/en only; custom catalogs cover any other tag).
CUSTOM_PROGRESS = {
    "phase_prework": "prework (custom)",
    "phase_postwork": "postwork (custom)",
    "phase_review": "review (custom)",
    "focus_tim-sir": "check protocol, dependencies, and evidence",
    "focus_sam-koo": "check changes, tests, and edge cases",
    "focus_nicole-leung": "check rollout, rollback, and operational impact",
    "focus_default": "check evidence and blockers",
    "entry": "{reviewer} begins {phase} to {focus}.",
    "cache": "{reviewer} reuses a cached {phase} review; no new invocation.",
    "command_failure": "{reviewer}'s {phase} call failed; the round stays blocked.",
    "parse_failure": "{reviewer}'s {phase} output was invalid; the round stays blocked.",
    "returned": "{reviewer} returned a {phase} review: {verdict}, pending synthesis.",
    "dry_run": "{phase} custom-dryrun: no reviewers launched.",
    "preflight": "{phase} custom preflight running; no reviewers launched.",
    "context_blocked": "{phase} custom context invalid; no reviewers launched.",
    "rollout_blocked": "{phase} custom rollout not ready; no reviewers launched.",
    "preflight_blocked": "{phase} custom-preflight-failed; no reviewers launched.",
}


class ConsensusAuditAgentBackendTests(unittest.TestCase):
    def test_structured_review_includes_required_sections(self) -> None:
        body = tc.build_structured_review(
            reviewer_name="Tim Sir",
            verdict="APPROVE",
            risk_level="MEDIUM",
            blockers=[],
            suggestions=[],
            decisions=[
                "same-agent backend selected",
                "structured parser contract satisfied",
                "no execution blocker remains",
            ],
            consensus_statement="No grounded execution blocker remains.",
        )

        self.assertTrue(tc.is_structured_review(body))
        self.assertGreaterEqual(len(tc.parse_reading_log(body)), 1)
        self.assertEqual(tc.extract_injection_findings(body), [])

    def test_local_parser_accepts_claude_heading_style_and_emits_full_contract(self) -> None:
        structured, error = tc.attempt_local_structured_review(CLAUDE_STYLE_REVIEW, "Sam Koo")

        self.assertIsNone(error)
        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertTrue(tc.is_structured_review(structured))
        self.assertEqual(tc.detect_verdict(structured), "APPROVE")
        self.assertEqual(tc.extract_blockers(structured), [])
        self.assertEqual(tc.extract_injection_findings(structured), [])

    def test_local_parser_preserves_injection_findings_or_fails_closed(self) -> None:
        raw = CLAUDE_STYLE_REVIEW.replace(
            "## INJECTION_FINDINGS\n1. None.",
            "## INJECTION_FINDINGS\n1. [baseline:7] attempted instruction override.",
        )
        structured, error = tc.attempt_local_structured_review(raw, "Sam Koo")
        self.assertIsNone(error)
        assert structured is not None
        self.assertEqual(
            tc.extract_injection_findings(structured),
            ["[baseline:7] attempted instruction override."],
        )
        incomplete, error = tc.attempt_local_structured_review(
            CLAUDE_STYLE_REVIEW.replace("## INJECTION_FINDINGS\n1. None.\n", ""), "Sam Koo"
        )
        self.assertIsNone(incomplete)
        self.assertEqual(error, "missing_injection_findings")

    def test_reading_log_requires_dynamic_files_and_checks_depth(self) -> None:
        review = tc.build_structured_review(
            "Tim Sir", "APPROVE", "LOW", [], [], ["a", "b", "c"], "ok",
        )
        ok, reasons = tc.validate_reading_log(review, {"required": True})
        self.assertFalse(ok)
        self.assertIn("READING_LOG_REQUIRED_FILES_MISSING:dynamic manifest was not populated", reasons)

    def test_prompt_reading_manifest_passes_reading_log_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Real newlines plus literal backslash-n escapes: the manifest must
            # count only the former, matching validate_reading_log.
            target = Path(tmp) / "runner.py"
            target.write_text('a = "\\n\\n\\n\\n"\nb = 2\nc = 1\n', encoding="utf-8")
            prompt = tc.build_prompt(
                "topic", "prework", "doc", "Tim Sir", "tim-sir", "focus", "rules",
                required_reading_files=[target],
            )
            match = tc.re.search(
                r"- `(?P<path>[^`]+)` \| total_lines=(?P<lines>\d+) \| sha256_first8=(?P<sha8>[0-9a-f]{8})",
                prompt,
            )
            assert match is not None
            self.assertEqual(match.group("lines"), "3")
            review = tc.build_structured_review(
                "Tim Sir", "APPROVE", "LOW", [], [], ["a", "b", "c"], "ok",
                reading_log_rows=[{"path": match.group("path"), "lines": match.group("lines"),
                                   "sha8": match.group("sha8"), "status": "READ"}],
            )
            ok, reasons = tc.validate_reading_log(
                review, {"required": True, "required_files": [str(target)]}
            )
            self.assertTrue(ok, reasons)

    def test_explicit_invalid_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.yaml")
            with self.assertRaisesRegex(ValueError, "EVIDENCE_MANIFEST_CONFIG_INVALID"):
                tc.load_evidence_manifest(missing)
            malformed = Path(tmp) / "malformed.yaml"
            malformed.write_text("- not-an-object\n", encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "expected object"):
                tc.load_role_config(str(malformed))

    def test_manifest_classification_and_evidence_failure_are_gating(self) -> None:
        manifest = {
            "blocker_classes": {
                "logic_flaw": {"required_evidence": ["affected_code_path", "reasoning_trace"]},
            },
        }
        self.assertEqual(
            tc.classify_blocker_validation_mode("The state transition is unsound.", manifest),
            "logic_flaw",
        )
        self.assertEqual(
            tc.validate_evidence_completeness("x", "unknown", manifest, set()),
            (False, "unknown_evidence_class:unknown"),
        )
        self.assertEqual(
            tc.validate_evidence_completeness("x", "logic_flaw", manifest, set()),
            (False, "missing_evidence:affected_code_path,reasoning_trace"),
        )

    def test_cross_review_persists_converged_outputs(self) -> None:
        output = "- AGREE_PROMOTE_TO_P0 ISSUE-1\nINJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\n"
        result = tc.CommandResult(ok=True, stdout=output, stderr="", command="mock", returncode=0)
        rows = [
            ("Tim Sir", "tim-sir", "model-a", "high", "review a"),
            ("Sam Koo", "sam-koo", "model-b", "high", "review b"),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc, "run_agent_with_retries", return_value=(result, [])):
            promoted, failures = tc.run_cross_review_transition(
                Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", "[x:1] issue")],
                1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
            )
            self.assertFalse(failures)
            self.assertEqual(len(promoted), 2)
            self.assertTrue((Path(tmp) / "45-dispute-matrix.md").is_file())
            self.assertTrue((Path(tmp) / "48-cross-tim-sir.md").is_file())
            self.assertIn("AGREE_PROMOTE_TO_P0", (Path(tmp) / "49-consensus-blockers.md").read_text())

    def test_cross_review_parser_tolerates_markdown_and_fails_closed(self) -> None:
        cases = {
            "**INJECTION_FINDINGS:**\n1. None.\n\n**CONVERGENCE_VOTE:** STAY\n": ([], "STAY"),
            "## INJECTION_FINDINGS\n1. None.\n\n## CONVERGENCE_VOTE\n**STAY**\n": ([], "STAY"),
            "**CONVERGENCE_VOTE: CONVERGED**\n": (None, "CONVERGED"),
            "INJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: maybe\n": ([], None),
            "INJECTION_FINDINGS:\n1. None.\n": ([], None),
            "Nobody should say CONVERGENCE_VOTE: CONVERGED yet.\nINJECTION_FINDINGS:\n1. None.\n": ([], None),
            "Quoted: `CONVERGENCE_VOTE: CONVERGED`\nINJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: STAY\n": ([], "STAY"),
            # Two votes contradict each other; the last one used to win.
            "CONVERGENCE_VOTE: STAY\nINJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\n": ([], None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tc.parse_cross_review_output(text), expected)

    def test_cross_review_invocation_failure_keeps_p0_handoff_blocked(self) -> None:
        ok = tc.CommandResult(ok=True, stdout="INJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\n",
                              stderr="", command="mock", returncode=0)
        failed = tc.CommandResult(ok=False, stdout="", stderr="killed", command="mock", returncode=-9)
        rows = [
            ("Tim Sir", "tim-sir", "model-a", "high", "review a"),
            ("Sam Koo", "sam-koo", "model-b", "high", "review b"),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc, "run_agent_with_retries", side_effect=[(ok, []), (failed, [])]):
            _promoted, failures = tc.run_cross_review_transition(
                Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", "[x:1] issue")],
                1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
            )
            self.assertEqual(failures, ["CROSS_REVIEW_FAIL:sam-koo: invocation failure"])
            self.assertIn("killed", (Path(tmp) / "48-cross-sam-koo.md").read_text())
            self.assertTrue((Path(tmp) / "45-dispute-matrix.md").is_file())
            self.assertIn("CROSS_REVIEW_FAIL:sam-koo", (Path(tmp) / "49-consensus-blockers.md").read_text())

    def test_consensus_blockers_record_promotions_and_failures(self) -> None:
        promote = tc.CommandResult(ok=True, stdout="- AGREE_PROMOTE_TO_P0 ISSUE-1\nINJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\n",
                                   stderr="", command="mock", returncode=0)
        failed = tc.CommandResult(ok=False, stdout="", stderr="killed", command="mock", returncode=-9)
        rows = [
            ("Tim Sir", "tim-sir", "model-a", "high", "review a"),
            ("Sam Koo", "sam-koo", "model-b", "high", "review b"),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc, "run_agent_with_retries", side_effect=[(promote, []), (failed, [])]):
            promoted, _failures = tc.run_cross_review_transition(
                Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", "[x:1] issue")],
                1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
            )
            handoff = (Path(tmp) / "49-consensus-blockers.md").read_text()
            self.assertEqual(len(promoted), 1)
            self.assertIn("AGREE_PROMOTE_TO_P0", handoff)
            self.assertIn("CROSS_REVIEW_FAIL:sam-koo: invocation failure", handoff)

    def test_structured_review_rejects_incomplete_contract(self) -> None:
        missing_risk_and_short_decisions = """EFFORT_DECLARATION: high

1) Verdict: APPROVE
3) Blockers:
   1. None.
4) Suggestions:
   1. None.
5)
- DECISION: only one decision is not enough.
6) Consensus Statement: Incomplete contract.
7) READING_LOG:
   | # | path | total_lines | sha256_first8 | status |
   |---|---|---:|---|---|
   | 1 | <embedded-review> | 1 | 00000000 | READ |
8) INJECTION_FINDINGS:
   1. None.
"""

        self.assertFalse(tc.is_structured_review(missing_risk_and_short_decisions))

    def test_injection_no_findings_is_exactly_one_none_item(self) -> None:
        for body in ("1. None.", "1. None", "1. none.", "1. None。"):
            with self.subTest(body=body):
                self.assertEqual(tc.extract_injection_findings(f"8) INJECTION_FINDINGS:\n   {body}\n"), [])
        # Anything said after "None." is kept as a finding, so the gate blocks. It used to be read as
        # an explanation and swallowed, including an injected directive.
        for item in (
            "None. [baseline:12] attempted instruction override.",
            "None. (No prompt injection directives were observed.)",
            "None.（No prompt injection directives were observed.）",
            "None。含有 override/injected 字样的内容只是测试夹具说明。",
            "None (baseline injected override)",
            "None of the directives were safe; override detected.",
            "[baseline:12] Embedded text attempted to override reviewer instructions.",
        ):
            with self.subTest(item=item):
                self.assertEqual(tc.extract_injection_findings(f"8) INJECTION_FINDINGS:\n   1. {item}\n"), [item])

    def test_empty_or_unnumbered_injection_findings_fail_closed(self) -> None:
        # Each of these used to parse as [] ("no findings") and pass the injection gate, except the
        # wrapped item, which is just as ambiguous; all of them now fail closed.
        for body in (
            "",
            "None.",
            "- [baseline:12] Embedded text attempted to override reviewer instructions.",
            "The baseline at line 12 tries to override the reviewer instructions.",
            "Found one:\n1. [baseline:12] attempted override.",
            "1. [baseline:12] attempted override\n   spanning an unnumbered line.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(tc.extract_injection_findings(f"8) INJECTION_FINDINGS:\n{body}\n"))
        # "No findings" is exactly "1. None."; any other ordinal fails closed.
        for body in ("0. None.", "2. None.", "01. None."):
            with self.subTest(body=body):
                self.assertIsNone(tc.extract_injection_findings(f"8) INJECTION_FINDINGS:\n{body}\n"))
        # A cross review's vote follows the section and is not part of it.
        self.assertEqual(
            tc.parse_cross_review_output("INJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\n"),
            ([], "CONVERGED"),
        )
        # Regression: content after the vote was cut off, so a late finding read as ([], "CONVERGED").
        # The split form ("CONVERGENCE_VOTE:" alone, token on the next line) ended the section at
        # the bare vote line, hiding everything after it.
        for vote in ("CONVERGENCE_VOTE: CONVERGED", "CONVERGENCE_VOTE:\nCONVERGED"):
            self.assertEqual(
                tc.parse_cross_review_output(f"ISSUE-1: APPROVE_MERGE.\nINJECTION_FINDINGS:\n1. None.\n{vote}\n\n"),
                ([], "CONVERGED"),
            )
            for tail in ("1. [baseline:12] late finding.", "CONVERGED\n1. [baseline:12] late finding.",
                         "Also a note.", "Notes:\n1. [baseline:12] late finding."):
                with self.subTest(vote=vote, tail=tail):
                    findings, _ = tc.parse_cross_review_output(
                        f"ISSUE-1: APPROVE_MERGE — no P0.\nINJECTION_FINDINGS:\n1. None.\n{vote}\n{tail}\n"
                    )
                    self.assertIsNone(findings)
        # Regression: text after the vote word on the vote's own line was allowed.
        for line in ("CONVERGENCE_VOTE: CONVERGED 1. [baseline:12] late finding.",
                     "CONVERGENCE_VOTE:\nCONVERGED 1. [baseline:12] late finding.",
                     "CONVERGENCE_VOTE: CONVERGED."):
            with self.subTest(line=line):
                findings, _ = tc.parse_cross_review_output(f"INJECTION_FINDINGS:\n1. None.\n{line}\n")
                self.assertIsNone(findings)
        # INJECTION_FINDINGS is the last section: a later heading no longer hides what follows it.
        self.assertIsNone(tc.extract_injection_findings(
            "8) INJECTION_FINDINGS:\n1. None.\nNotes:\n1. [baseline:12] late finding.\n"))

    def test_claude_conservative_path_uses_local_normalization(self) -> None:
        def fake_run_agent_with_retries(*_args, **_kwargs):
            result = tc.CommandResult(
                ok=True,
                stdout=CLAUDE_STYLE_REVIEW.replace("EFFORT_DECLARATION: high\n\n", ""),
                stderr="",
                command="claude --print",
                returncode=0,
                model=tc.DEFAULT_CLAUDE_MODEL,
                reviewer="Tim Sir",
                backend=tc.CLAUDE_AGENT_BACKEND,
                agent="claude",
            )
            return result, [{"attempt": 1, "returncode": 0}]

        with tempfile.TemporaryDirectory(), \
                mock.patch.object(tc, "run_agent_with_retries", side_effect=fake_run_agent_with_retries):
            body, result, meta = tc.run_peer_review(
                prompt="audit prompt",
                model=tc.DEFAULT_CLAUDE_MODEL,
                effort="adaptive",
                backend=tc.CLAUDE_AGENT_BACKEND,
                reviewer_name="Tim Sir",
                reviewer_slug="tim-sir",
                timeout_sec=1.0,
                peer_retries=0,
                retry_backoff_sec=0.0,
                retry_timeout_multiplier=1.0,
            )

        self.assertEqual(result.parser_path, "local-structured")
        self.assertEqual(meta["gate_path"], "conservative")
        self.assertFalse(meta["shadow_anomaly"])
        self.assertTrue(tc.is_structured_review(body))
        self.assertEqual(tc.extract_injection_findings(body), [])

    def test_failure_reason_codes_distinguish_stagnation_from_cap(self) -> None:
        self.assertEqual(
            tc.derive_failure_reason_codes(
                status="MODERATOR_DECISION_REQUIRED",
                summary_parity_ok=True,
                open_blockers_count=1,
                blocker_stagnated=True,
            ),
            ["OPEN_BLOCKERS", "BLOCKER_STAGNATION"],
        )
        self.assertEqual(
            tc.derive_failure_reason_codes(
                status="MODERATOR_DECISION_REQUIRED",
                summary_parity_ok=True,
                open_blockers_count=1,
                cap_reached=True,
            ),
            ["OPEN_BLOCKERS", "CAP_REACHED"],
        )

    def test_claude_agent_defaults_swap_only_unoverridden_models(self) -> None:
        # An absent flag is None (argparse default); only those take the agent default.
        args = argparse.Namespace(
            agent="claude",
            codex_a_model=None,
            codex_b_model="custom-reviewer",
            codex_c_model=None,
        )

        with mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
             mock.patch.object(tc, "_ENV_REVIEWER_A_MODEL", None), \
             mock.patch.object(tc, "_ENV_REVIEWER_B_MODEL", None), \
             mock.patch.object(tc, "_ENV_REVIEWER_C_MODEL", None), \
             mock.patch.object(tc, "_ENV_REQUIRED_MODELS", None):
            tc.apply_agent_defaults(args)

        self.assertEqual(args.codex_a_model, tc.DEFAULT_CLAUDE_MODEL)
        self.assertEqual(args.codex_b_model, "custom-reviewer")
        self.assertEqual(args.codex_c_model, tc.DEFAULT_CLAUDE_MODEL)


    def test_model_precedence_is_flag_then_env_then_agent_default(self):
        """An env override must survive on every backend, including claude.

        Regression: agent defaults used to be applied by comparing the parsed argument
        against the resolved default constant. Because the constant itself was derived
        from the environment, "flag absent" and "flag equals default" were
        indistinguishable, and every CONSENSUS_AUDIT_* model override was silently
        discarded under --agent claude.
        """
        cases = (
            # agent,    flag,           env_a,        want_a
            ("claude",  None,           "env-model",  "env-model"),
            ("claude",  None,           None,         tc.DEFAULT_CLAUDE_MODEL),
            ("claude",  "flag-model",   "env-model",  "flag-model"),
            ("codex",   None,           "env-model",  "env-model"),
            ("codex",   None,           None,         tc.DEFAULT_REVIEWER_A_MODEL),
            ("codex",   "flag-model",   "env-model",  "flag-model"),
        )
        for agent, flag, env_a, want_a in cases:
            with self.subTest(agent=agent, flag=flag, env_a=env_a), \
                 mock.patch.object(tc, "resolve_agent_request", lambda _req, a=agent: (a, a)), \
                 mock.patch.object(tc, "_ENV_REVIEWER_A_MODEL", env_a), \
                 mock.patch.object(tc, "_ENV_REQUIRED_MODELS", None):
                args = argparse.Namespace(agent=agent, codex_a_model=flag, codex_b_model=None,
                                          codex_c_model=None)
                tc.apply_agent_defaults(args)
                self.assertEqual(args.codex_a_model, want_a)

    def test_daily_models_and_high_effort_reach_both_backend_commands(self):
        """Daily selection must reach the subprocess, without an expensive moderator call."""
        source = (SKILL_DIR / "scripts/consensus_audit.py").read_text(encoding="utf-8")
        defaults = dict(re.findall(r'DEFAULT_REVIEWER_([ABC])_MODEL = _ENV_REVIEWER_\1_MODEL or "([^"]+)"', source))
        self.assertEqual(defaults, {suffix: "gpt-6.1-sol" for suffix in "ABC"})
        self.assertIn('DEFAULT_CLAUDE_MODEL = _ENV_CLAUDE_MODEL or "claude-opus-5-5"', source)
        for agent, model in (("codex", "gpt-6.1-sol"), ("claude", "claude-opus-5-5")):
            with self.subTest(agent=agent), \
                    mock.patch.object(sys, "argv", ["audit", "--topic", "defaults", "--phase", "prework",
                                                  "--baseline-file", "baseline.md"]), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=(agent, agent)), \
                    mock.patch.object(tc, "_ENV_REVIEWER_A_MODEL", None), \
                    mock.patch.object(tc, "_ENV_REVIEWER_B_MODEL", None), \
                    mock.patch.object(tc, "_ENV_REVIEWER_C_MODEL", None), \
                    mock.patch.object(tc, "DEFAULT_REVIEWER_A_MODEL", "gpt-6.1-sol"), \
                    mock.patch.object(tc, "DEFAULT_REVIEWER_B_MODEL", "gpt-6.1-sol"), \
                    mock.patch.object(tc, "DEFAULT_REVIEWER_C_MODEL", "gpt-6.1-sol"), \
                    mock.patch.object(tc, "DEFAULT_CLAUDE_MODEL", "claude-opus-5-5"), \
                    mock.patch.object(tc, "_ENV_REQUIRED_MODELS", None):
                args = tc.parse_args()
                tc.apply_agent_defaults(args)
                for suffix in ("a", "b", "c"):
                    self.assertEqual(getattr(args, f"codex_{suffix}_model"), model)
                    self.assertEqual(getattr(args, f"codex_{suffix}_effort"), "high")
                contract = tc.build_main_codex_contract(args)
                plan = tc.build_required_model_plan(args, True, contract)
                self.assertFalse(any(row["model"] in ("gpt-6-astra", "claude-fable-5-1") for row in plan))
                result = tc.CommandResult(ok=True, stdout="ok", stderr="", command="mock", returncode=0)
                with mock.patch.object(tc, "run_command", return_value=result) as run:
                    tc.run_agent_backend("prompt", args.codex_a_model, args.codex_a_effort,
                                         "Tim Sir", 1.0, tc.backend_for_agent(agent))
                cmd = run.call_args.args[0]
                self.assertEqual(cmd[cmd.index("--model") + 1], model)
                if agent == "codex":
                    self.assertIn('model_reasoning_effort="high"', cmd)
                    self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
                else:
                    self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
                    self.assertEqual(cmd[cmd.index("--tools") + 1], tc.REVIEWER_CLAUDE_TOOLS)

    def test_required_models_is_deprecated_and_does_not_change_preflight(self):
        """--required-models was resolved but never consulted; it must now say so.

        Regression: the README told users preflight would block every run unless they set
        CONSENSUS_AUDIT_REQUIRED_MODELS, while preflight only ever probed the reviewer
        models. Setting it must warn, and the preflight plan must still be exactly the
        reviewer models.
        """
        for flag, env in (("m1,m2", None), (None, "m1,m2")):
            with self.subTest(flag=flag, env=env), \
                 mock.patch.object(tc, "resolve_agent_request", return_value=("codex", "codex")), \
                 mock.patch.object(tc, "_ENV_REVIEWER_A_MODEL", None), \
                 mock.patch.object(tc, "_ENV_REVIEWER_B_MODEL", None), \
                 mock.patch.object(tc, "_ENV_REVIEWER_C_MODEL", None), \
                 mock.patch.object(tc, "_ENV_REQUIRED_MODELS", env):
                args = argparse.Namespace(agent="codex", codex_a_model="a", codex_b_model="b",
                                          codex_c_model="c", required_models=flag)
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    tc.apply_agent_defaults(args)
                self.assertIn("deprecated and ignored", stderr.getvalue())
        with mock.patch.object(tc, "resolve_agent_request", return_value=("codex", "codex")), \
             mock.patch.object(tc, "_ENV_REQUIRED_MODELS", None):
            args = argparse.Namespace(agent="codex", codex_a_model="a", codex_b_model="b",
                                      codex_c_model="c", required_models=None)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                tc.apply_agent_defaults(args)
            self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(hasattr(tc, "REQUIRED_MODELS_DEFAULT"))

    def test_reading_log_accepts_relative_and_trailing_paths(self):
        """A reviewer who relativises a required path must not trip a runner blocker.

        Regression: a live Claude round where both reviewers approved with zero blockers
        still ended NEEDS_ANOTHER_ROUND because one reviewer wrote the required paths
        workspace-relative and the gate compared strings byte for byte.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve() / "ws"
            (workspace / ".discuss/t").mkdir(parents=True)
            baseline = workspace / ".discuss/t/baseline.md"
            baseline.write_text("one\ntwo\n", encoding="utf-8")
            runner = Path(tmp).resolve() / "h/.claude/skills/consensus-audit/scripts/consensus_audit.py"
            runner.parent.mkdir(parents=True)
            runner.write_text("x = 1\n", encoding="utf-8")
            manifest = {"required": True, "required_files": [str(baseline), str(runner)]}

            def review(rows):
                return tc.build_structured_review(
                    "Tim Sir", "APPROVE", "LOW", [], [], ["a", "b", "c"], "ok",
                    reading_log_rows=[
                        {"path": p, "lines": str(tc.reading_log_fingerprint(f.read_bytes())[0]),
                         "sha8": sha or tc.reading_log_fingerprint(f.read_bytes())[1], "status": "READ"}
                        for p, f, sha in rows
                    ],
                )

            accepted = review([
                (".discuss/t/baseline.md", baseline, None),                          # workspace-relative
                ("h/.claude/skills/consensus-audit/scripts/consensus_audit.py", runner, None),  # trailing run
            ])
            ok, reasons = tc.validate_reading_log(accepted, manifest, workspace)
            self.assertTrue(ok, reasons)

            wrong_sha = review([(".discuss/t/baseline.md", baseline, "00000000"),
                                ("scripts/consensus_audit.py", runner, None)])
            ok, reasons = tc.validate_reading_log(wrong_sha, manifest, workspace)
            self.assertFalse(ok)
            self.assertTrue(any(r.startswith("READING_LOG_SHA_MISMATCH") for r in reasons), reasons)

            unrelated = review([("other/baseline.md", baseline, None),
                                ("../scripts/consensus_audit.py", runner, None)])
            ok, reasons = tc.validate_reading_log(unrelated, manifest, workspace)
            self.assertEqual(sum(r.startswith("READING_LOG_MISSING") for r in reasons), 2, reasons)

    def test_runner_is_required_reading_only_inside_the_workspace(self):
        """An installed runner outside --workspace must not be a required READING_LOG row.

        Regression: reviewers start in --workspace and a Claude Code reviewer may be denied
        reads outside it, so requiring ~/.claude/skills/.../consensus_audit.py failed honest
        reviewers who reported it SKIPPED on audits unrelated to the runner.
        """
        runner = Path(tc.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "baseline.md"
            self.assertEqual(tc.reading_log_required_files(baseline, workspace), [baseline])
        checkout = runner.parents[2]  # auditing this skill's own source: the runner is in scope
        self.assertEqual(tc.reading_log_required_files(checkout / "README.md", checkout),
                         [checkout / "README.md", runner])

    def test_one_reading_log_row_cannot_satisfy_two_required_paths(self):
        """Trailing-component matching must consume rows, or one row covers two same-named files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            first, second = root / "a/scripts/run.py", root / "b/scripts/run.py"
            for path in (first, second):
                path.parent.mkdir(parents=True)
                path.write_text("same\n", encoding="utf-8")
            lines, sha8 = tc.reading_log_fingerprint(first.read_bytes())
            manifest = {"required": True, "required_files": [str(first), str(second)]}

            def review(paths):
                return tc.build_structured_review(
                    "Tim Sir", "APPROVE", "LOW", [], [], ["a", "b", "c"], "ok",
                    reading_log_rows=[{"path": p, "lines": str(lines), "sha8": sha8, "status": "READ"}
                                      for p in paths],
                )

            ok, reasons = tc.validate_reading_log(review(["scripts/run.py"]), manifest, None)
            self.assertFalse(ok)
            self.assertIn(f"READING_LOG_MISSING:{second}", reasons)
            ok, reasons = tc.validate_reading_log(review(["scripts/run.py", "b/scripts/run.py"]), manifest, None)
            self.assertTrue(ok, reasons)

    def test_source_rollback_restores_every_tracked_source_path_and_the_install(self):
        """The rollback runbook must not name a hand-picked subset of the change set.

        Regression: the list restored six paths while a change touched eight, and it never
        mentioned the installed copy, which is what an agent actually runs.
        """
        text = (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8")
        section = text.split("## Rollback of this source change", 1)[1].split("\n## ", 1)[0]
        self.assertIn("git restore --staged --worktree -- skill tests README.md README.zh-CN.md", section)
        tracked_top = {line.split("/", 1)[0] for line in subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()}
        restored = {"skill", "tests", "README.md", "README.zh-CN.md"}
        self.assertEqual(tracked_top - restored, {".gitignore", "LICENSE"})
        self.assertIn("git revert <commit>", section)
        self.assertIn("run the install block under [Install](#install) again", section)
        verify = text.split("\n## Verify", 1)[1].split("\n## ", 1)[0]
        self.assertIn("diff -rq -x __pycache__ skill", verify)

    def test_install_verifies_the_stage_before_replacing_and_keeps_no_backup(self):
        """With no backup kept, a broken package must never replace the installed one.

        Regression: a backup directory inside the skills root was registered as a second,
        stale skill with the same triggers, and the smoke block used `status`, which is
        read-only in zsh.
        """
        text = (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8")
        install = text.split("\n## Install", 1)[1].split("\n### ", 1)[0]
        block = re.findall(r"```bash\n(.*?)```", install, re.DOTALL)[0]
        self.assertTrue(block.startswith("bash -c '"))
        swap = block.index('rm -rf "$DEST_DIR"')
        for check in ('"$STAGE_DIR/scripts/consensus_audit.py" --help',
                      '"$STAGE_DIR/scripts/consensus_audit.py" \\\n  --topic install-smoke',
                      'test "$rc" = 2'):
            with self.subTest(check=check):
                self.assertLess(block.index(check), swap)
        self.assertLess(swap, block.index('mv "$STAGE_DIR" "$DEST_DIR"'))
        # A directory inside the skills root is registered as a skill, so the stage lives outside it.
        self.assertIn('STAGE_ROOT="$(dirname "$DEST_ROOT")/', block)
        cleanup = text.split("### Leftovers from earlier versions", 1)[1].split("\n## ", 1)[0]
        for leftover in (".consensus-audit.backup.*", ".consensus-audit.stage.*", ".consensus-audit.last-backup"):
            with self.subTest(leftover=leftover):
                self.assertIn(leftover, cleanup)
        for code in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
            with self.subTest(block=code[:60]):
                self.assertNotIn("BACKUP_DIR", code)
                self.assertNotRegex(code, r"(?m)\bstatus=")

    def test_concurrent_runs_never_share_a_round_directory(self):
        """Regression: scan-then-mkdir(exist_ok=True) let concurrent runs write into one round."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: changed/planned symbols 0 | refs 0 | formal_tests 0 | validation_commands 1 | uncovered 0\n")
            cmd = [sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--topic", "race",
                   "--phase", "prework", "--progress-language", "en", "--baseline-file", str(baseline),
                   "--workspace", str(workspace), "--dry-run", "--peer-retries", "0", "--model-preflight-retries", "0"]
            # Pin the agent context: the suite may itself run inside Codex, Claude Code, or one nested in the other.
            import os
            markers = ("CLAUDECODE", "CLAUDE_CODE", "CLAUDECODE_CWD", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR",
                       "CODEX_CI", "CODEX_HOME", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CONSENSUS_AUDIT_AGENT")
            env = {k: v for k, v in os.environ.items() if k not in markers}
            procs = [subprocess.Popen(cmd, cwd=workspace, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(6)]
            codes = [p.wait() for p in procs]
            self.assertEqual(codes, [2] * 6)
            rounds = sorted(p.name for p in (workspace / ".discuss/race").glob("round-*-prework"))
            self.assertEqual(rounds, [f"round-{n:02d}-prework" for n in range(1, 7)])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "topic"
            first = tc.claim_next_round_dir(base, "postwork")
            # A directory that appears between the scan and the claim is skipped, not reused.
            real_next = tc.next_round_dir
            calls = []

            def racing_next(b, phase):
                candidate = real_next(b, phase)
                if not calls:
                    candidate.mkdir()  # another runner claims it first
                calls.append(candidate)
                return candidate

            with mock.patch.object(tc, "next_round_dir", racing_next):
                second = tc.claim_next_round_dir(base, "postwork")
            self.assertEqual(first.name, "round-01-postwork")
            self.assertEqual(second.name, "round-03-postwork")

    def test_both_agent_markers_without_an_install_path_fail_closed(self):
        """Regression: with Codex and Claude markers both set, detection silently chose Claude."""
        source = ROOT / "skill/scripts/consensus_audit.py"
        both = {"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}
        self.assertEqual(tc.detect_agent_context(source, ROOT, both), tc.AMBIGUOUS_AGENT_CONTEXT)
        self.assertEqual(tc.detect_agent_context(source, ROOT, {"CLAUDECODE": "1"}), "claude")
        self.assertEqual(tc.detect_agent_context(source, ROOT, {"CODEX_THREAD_ID": "t"}), "codex")
        # An install path is trusted over the environment, so installed skills are unaffected.
        installed = Path.home() / ".codex/skills/consensus-audit/scripts/consensus_audit.py"
        self.assertEqual(tc.detect_agent_context(installed, ROOT, both), "codex")

        with mock.patch.object(tc, "detect_agent_context", return_value=tc.AMBIGUOUS_AGENT_CONTEXT):
            with self.assertRaisesRegex(ValueError, "AGENT_CONTEXT_AMBIGUOUS"):
                tc.resolve_agent_request("auto")
            self.assertEqual(tc.resolve_agent_request("codex"), ("codex", "codex"))
            self.assertEqual(tc.resolve_agent_request("claude"), ("claude", "claude"))

    def test_documented_install_block_installs_and_refuses_a_broken_package(self):
        """Run the runbook's install block itself, so a policy change that breaks it fails here."""
        import shutil
        text = (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8")
        install = text.split("\n## Install", 1)[1].split("\n### ", 1)[0]
        block = re.findall(r"```bash\n(.*?)```", install, re.DOTALL)[0]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            dest_root = home / ".claude/skills"

            def install_from(source):
                env = isolated_install_env(home, dest_root, source)
                return subprocess.run(["bash", "-c", block], cwd=ROOT, env=env, capture_output=True, text=True, check=False)

            for attempt in ("first install", "reinstall"):
                result = install_from(SKILL_DIR)
                with self.subTest(attempt=attempt):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(sorted(p.name for p in dest_root.iterdir()), ["consensus-audit"])
                    self.assertEqual(sorted(p.name for p in (home / ".claude").iterdir()), ["skills"])

            broken = home / "broken"
            shutil.copytree(SKILL_DIR, broken, ignore=shutil.ignore_patterns("__pycache__"))
            runner = broken / "scripts/consensus_audit.py"
            runner.write_text(runner.read_text(encoding="utf-8").replace(
                "def main() -> int:\n", "def main() -> int:\n    if '--dry-run' in sys.argv:\n        return 1\n", 1),
                encoding="utf-8")
            installed_runner = dest_root / "consensus-audit/scripts/consensus_audit.py"
            before = installed_runner.read_bytes()
            result = install_from(broken)
            self.assertEqual(result.returncode, 1)
            self.assertIn("installed copy untouched", result.stderr)
            self.assertEqual(installed_runner.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in (home / ".claude").iterdir()), ["skills"])

    def test_missing_pyyaml_is_named_by_the_runner_and_refused_by_the_install(self):
        """Release-audit P1: without PyYAML the documented invocation exited 1 with "expected object
        config", and the install smoke round passed no config, so such a machine installed cleanly."""
        configs = [("--classification-file", "classification.yaml"),
                   ("--evidence-manifest-file", "evidence_manifest.yaml"),
                   ("--postwork-reflow-policy", "postwork_reflow_policy.yaml"),
                   ("--role-config", "role_config.yaml"), ("--provider-config", "provider_config.yaml")]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--dry-run"]
            for flag, name in configs:
                argv += [flag, str(SKILL_DIR / "config" / name)]
            err = io.StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch.object(tc, "HAS_YAML", False), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                    redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(tc.main(), 1)
            self.assertIn("PyYAML is required", err.getvalue())
            self.assertFalse((workspace / ".discuss").exists())
            # A JSON config still loads without PyYAML.
            json_config = workspace / "role.json"
            json_config.write_text(json.dumps({"config_version": 1, "reviewers": {}}), encoding="utf-8")
            with mock.patch.object(tc, "HAS_YAML", False):
                self.assertEqual((tc.load_role_config(str(json_config)) or {}).get("config_version"), 1)
            # A JSON null or an empty file is a bad config, not a missing PyYAML.
            for content in ("null", "", "  \n"):
                json_config.write_text(content, encoding="utf-8")
                with self.subTest(content=content), mock.patch.object(tc, "HAS_YAML", False), \
                        self.assertRaisesRegex(TypeError, "expected object config"):
                    tc.load_role_config(str(json_config))

        text = (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8")
        install = text.split("\n## Install", 1)[1].split("\n### ", 1)[0]
        block = re.findall(r"```bash\n(.*?)```", install, re.DOTALL)[0]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            dest_root = home / ".claude/skills"
            no_yaml = home / "no-yaml"
            no_yaml.mkdir()
            (no_yaml / "yaml.py").write_text('raise ImportError("simulated: PyYAML not installed")\n')
            env = isolated_install_env(home, dest_root, SKILL_DIR)
            installed = subprocess.run(["bash", "-c", block], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_runner = dest_root / "consensus-audit/scripts/consensus_audit.py"
            before = installed_runner.read_bytes()
            result = subprocess.run(["bash", "-c", block], cwd=ROOT, env={**env, "PYTHONPATH": str(no_yaml)},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("PyYAML is required", result.stderr)
            self.assertIn("installed copy untouched", result.stderr)
            self.assertEqual(installed_runner.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in home.iterdir()), [".claude", "no-yaml"])
        for doc in ("README.md", "README.zh-CN.md", "skill/00-READ-FIRST.md", "skill/SKILL.md"):
            with self.subTest(doc=doc):
                self.assertIn("pip install pyyaml", (ROOT / doc).read_text(encoding="utf-8"))

    def test_a_pyyaml_that_raises_on_import_does_not_crash_the_runner(self):
        """Recheck P2: only ImportError was caught, so a broken PyYAML crashed the runner before argparse."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            broken, workspace = root / "broken-yaml", root / "ws"
            broken.mkdir()
            workspace.mkdir()
            (broken / "yaml.py").write_text('raise RuntimeError("half-installed C extension")\n')
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(broken)}
            runner = [sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py")]
            self.assertEqual(subprocess.run(runner + ["--help"], env=env, capture_output=True, check=False).returncode, 0)
            result = subprocess.run(
                runner + ["--topic", "t", "--phase", "prework", "--agent", "claude", "--baseline-file", str(baseline),
                          "--workspace", str(workspace), "--dry-run",
                          "--role-config", str(SKILL_DIR / "config/role_config.yaml")],
                env={**env, "CLAUDECODE": "1"}, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("PyYAML is required", result.stderr)
            self.assertIn("RuntimeError: half-installed C extension", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((workspace / ".discuss").exists())

    def test_documented_config_flags_match_the_shipped_configs(self):
        """Recheck P2: the install smoke round and the documented invocations each name the five config
        files; a rename or a new config must not let one of them drift."""
        shipped = sorted(p.name for p in (SKILL_DIR / "config").glob("*.yaml"))
        install = (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8").split("\n## Install", 1)[1]
        install = re.findall(r"```bash\n(.*?)```", install, re.DOTALL)[0]
        sources = {
            "install block": install,
            "skill/SKILL.md": (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"),
            "consensus_protocol.md": (SKILL_DIR / "references/consensus_protocol.md").read_text(encoding="utf-8"),
        }
        pairs = {}
        for name, text in sources.items():
            pairs[name] = dict(re.findall(r'(--[a-z-]+) "\$(?:C|SKILL_DIR/config)/([a-z_]+\.yaml)"', text))
            with self.subTest(source=name):
                self.assertEqual(sorted(pairs[name].values()), shipped)
        self.assertEqual(pairs["install block"], pairs["skill/SKILL.md"])
        self.assertEqual(pairs["consensus_protocol.md"], pairs["skill/SKILL.md"])
        help_text = subprocess.run([sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--help"],
                                   capture_output=True, text=True, check=True).stdout
        for flag in pairs["skill/SKILL.md"]:
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)

    def test_evidence_record_names_its_blocker_so_new_findings_do_not_stagnate(self):
        """Regression: identical EVIDENCE_COMPLETENESS_FAIL text for two different blockers
        escalated a round to MODERATOR_DECISION_REQUIRED on blocker stagnation."""
        reason = "missing_evidence:expected_artifact_path"
        first = tc.evidence_completeness_blocker({"blocker": "[a.py:1] one finding", "reason": reason})
        second = tc.evidence_completeness_blocker({"blocker": "[b.py:9] a new finding", "reason": reason})
        again = tc.evidence_completeness_blocker({"blocker": "[a.py:1] one finding", "reason": reason})
        self.assertNotEqual(first, second)
        self.assertEqual(first, again)
        record = lambda text: [(tc.SYSTEM_REVIEWER_NAME, "21-evidence-completeness.json", text)]
        prev_rows, _, _ = tc.build_blocker_registry(record(first), None, set(), [], 1, "postwork")
        prev_set = {tc.canonical_blocker(first)}
        new_rows, _, _ = tc.build_blocker_registry(record(second), None, prev_set, prev_rows, 2, "postwork")
        same_rows, _, _ = tc.build_blocker_registry(record(again), None, prev_set, prev_rows, 2, "postwork")
        self.assertEqual(new_rows[0]["stagnation_count"], 1)
        self.assertEqual(same_rows[0]["stagnation_count"], 2)

    def test_reviewer_commands_cannot_write_the_workspace(self):
        """Reviewers start in the workspace under audit; the runner, not operator config, keeps them read-only."""
        seen = []

        def capture(cmd, prompt, timeout_sec=None):
            seen.append(cmd)
            return tc.CommandResult(ok=True, stdout="x", stderr="", command=" ".join(cmd), returncode=0)

        with mock.patch.object(tc, "run_command", capture):
            tc.run_codex("p", "m", "adaptive", "Tim Sir", 1.0)
            tc.run_claude("p", "m", "adaptive", "Tim Sir", 1.0)
        codex_cmd, claude_cmd = seen
        self.assertEqual(codex_cmd[codex_cmd.index("--sandbox") + 1], "read-only")
        self.assertEqual(claude_cmd[claude_cmd.index("--tools") + 1], "Read,Grep,Glob")
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--sandbox read-only", skill)
        self.assertIn("--tools Read,Grep,Glob", skill)

    def test_blocker_with_pipe_survives_audit_summary_round_trip(self):
        """Blockers quoting a pipe-delimited line must keep their identity in AUDIT_SUMMARY.

        Regression: the summary rewrites "|" to "/", so a blocker quoting the COVERAGE line
        failed summary parity (PARITY_FAIL) and could never match itself in the next round.
        """
        blocker = "[x.md:1] COVERAGE claims formal_tests 4 | uncovered 0 with no run"
        summary = (
            f"SCHEMA_VERSION: {tc.SUMMARY_SCHEMA_VERSION}\n\n## OPEN_BLOCKERS\n"
            f"- id={tc.short_blocker_id(blocker)} | reviewer=R | source=f | "
            f"text={tc.sanitize_summary_value(blocker)}\n"
        )
        parsed = tc.parse_open_blockers_from_audit_summary_text(summary)
        self.assertEqual(parsed, {tc.canonical_blocker(blocker)})
        self.assertEqual(tc.short_blocker_id(blocker),
                         tc.short_blocker_id(tc.sanitize_summary_value(blocker)))

    def test_prompt_marks_exit_condition_as_reviewable_data_not_injection(self):
        """The README tells authors to write an exit condition into the baseline.

        Regression: a live Codex reviewer reported that section as a prompt injection,
        which the runner turns into a P0 blocker, so following the documented practice
        could block a round on its own.
        """
        prompt = tc.build_prompt("t", "postwork", "## Exit Condition\nShip if ...",
                                 "Tim Sir", "tim-sir", "focus", "rules")
        policy = prompt.split("=== UNTRUSTED CONTENT BOUNDARY ===")[0]
        self.assertIn("'## Exit Condition'", policy)
        self.assertIn("not a prompt injection", policy)
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            self.assertIn("## Exit Condition", (ROOT / doc).read_text(encoding="utf-8"), doc)


    def test_leading_citation_accepts_the_formats_the_prompt_asks_for(self):
        """Markdown backticks and line ranges must not disqualify a blocker.

        Regression: the validator matched only a bare `[path:line]` at line start, while the
        reviewer prompt itself displayed the citation wrapped in backticks. Substantive
        blockers were discarded as missing_leading_file_line_citation purely on formatting.
        """
        accepted = {
            "[skill/scripts/consensus_audit.py:4220] text": ("skill/scripts/consensus_audit.py", 4220),
            "`[skill/scripts/consensus_audit.py:4220]` text": ("skill/scripts/consensus_audit.py", 4220),
            "[skill/scripts/consensus_audit.py:4220-4227] text": ("skill/scripts/consensus_audit.py", 4220),
            "`[skill/scripts/consensus_audit.py:4220-4227]` text": ("skill/scripts/consensus_audit.py", 4220),
            "  [skill/00-READ-FIRST.md:40] \u2014 text": ("skill/00-READ-FIRST.md", 40),
            # Backticks inside the brackets, as a Codex reviewer wrote it; the blocker used to be discarded.
            "[`skill/00-READ-FIRST.md:40`] text": ("skill/00-READ-FIRST.md", 40),
        }
        for text, expected in accepted.items():
            with self.subTest(text=text):
                self.assertEqual(tc.extract_leading_file_line_citation(text), expected)

        for text in ("no citation here", "[missing-line-number] text", "text then [a/b.py:1]",
                     "[a/b.py:0] text", "[a/b.py:-3] text"):
            with self.subTest(rejected=text):
                self.assertIsNone(tc.extract_leading_file_line_citation(text))

    def test_reading_log_bounds_count_files_and_honour_legacy_keys(self):
        """The bound is a file count, under both the new and the legacy key names."""
        def log(n):
            rows = "\n".join(f"| {i} | f{i}.py | 1 | aaaaaaaa | READ |" for i in range(1, n + 1))
            return "READING_LOG:\n| # | path | total_lines | sha256_first8 | status |\n|---|---|---:|---|---|\n" + rows

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            for i in range(1, 10):   # rows count only for files that exist; distinct contents count apart
                (ws / f"f{i}.py").write_text(f"n = {i}\n", encoding="utf-8")
            for keys in ({"min_files_read": 2, "max_files_read": 4},
                         {"min_chain_depth": 2, "max_chain_depth": 4}):
                with self.subTest(keys=sorted(keys)):
                    manifest = {"required": True, "required_files": [], **keys}
                    # required_files empty -> the count bounds are not the gate; use a populated one
                    manifest["required_files"] = [{"path": str(ws / "f1.py")}]

                    _, failures = tc.validate_reading_log(log(1), manifest, ws)
                    self.assertTrue(any("READING_LOG_TOO_FEW_FILES" in f for f in failures), failures)

                    _, failures = tc.validate_reading_log(log(9), manifest, ws)
                    self.assertTrue(any("READING_LOG_TOO_MANY_FILES:rows=9 maximum=4" in f for f in failures), failures)

                    _, failures = tc.validate_reading_log(log(3), manifest, ws)
                    self.assertFalse([f for f in failures if "FILES" in f], failures)

        # The shipped manifest must not punish a thorough reviewer for breadth.
        shipped = yaml_reading_log_config()
        self.assertGreaterEqual(shipped["max_files_read"], 20)

    def test_verified_citations_count_toward_the_reading_minimum(self):
        """Regression: reviews citing source lines failed READING_LOG_TOO_FEW_FILES for logging only the baseline."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "pkg").mkdir()
            (ws / "pkg/run.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
            (ws / "base.md").write_text("baseline\n", encoding="utf-8")
            (ws / "dup").mkdir()
            (ws / "dup/run.py").write_text("x\n", encoding="utf-8")
            (ws / "only.py").write_text("x\n", encoding="utf-8")
            lines, sha8 = tc.reading_log_fingerprint((ws / "base.md").read_bytes())
            log = ("READING_LOG:\n| # | path | total_lines | sha256_first8 | status |\n|---|---|---:|---|---|\n"
                   f"| 1 | {ws / 'base.md'} | {lines} | {sha8} | READ |\n")
            manifest = {"required": True, "min_files_read": 2, "required_files": [{"path": str(ws / "base.md")}]}

            def too_few(body):
                _, failures = tc.validate_reading_log(body + "\n" + log, manifest, ws)
                return [f for f in failures if "TOO_FEW" in f]

            self.assertEqual(too_few("DECISION: the guard at `pkg/run.py:2` holds."), [])
            self.assertEqual(too_few("see only.py:1"), [])                       # unique bare name
            self.assertTrue(too_few("see run.py:1"))                             # ambiguous bare name
            self.assertTrue(too_few("see pkg/run.py:9"))                         # line out of range
            self.assertTrue(too_few("see pkg/missing.py:1"))                     # no such file
            self.assertTrue(too_few("see ../outside.py:1"))                      # outside the workspace
            self.assertTrue(too_few("see http://example.com:80 and base.md:1"))  # a URL, and the baseline itself
            self.assertTrue(too_few("no citations at all"))
            # The baseline row in a trailing-path form plus a citation of the baseline is still one file.
            short_log = log.replace(f"| {ws / 'base.md'} |", "| base.md |")
            (ws / "sub").mkdir()
            (ws / "sub/base.md").write_text("baseline\n", encoding="utf-8")
            nested = {**manifest, "required_files": [{"path": str(ws / "sub/base.md")}]}
            _, failures = tc.validate_reading_log("see sub/base.md:1\n" + short_log, nested, ws)
            self.assertNotIn(f"READING_LOG_MISSING:{ws / 'sub/base.md'}", failures)  # the short row is the required row
            self.assertTrue([f for f in failures if "TOO_FEW" in f], failures)
            _, failures = tc.validate_reading_log("see sub/base.md:1 and pkg/run.py:2\n" + short_log, nested, ws)
            self.assertFalse([f for f in failures if "TOO_FEW" in f], failures)
            # A byte-identical copy of the baseline (the runner writes one as 10-main-agent.md) is the same file.
            (ws / "round-01").mkdir()
            (ws / "round-01/10-main-agent.md").write_bytes((ws / "base.md").read_bytes())
            self.assertTrue(too_few("see round-01/10-main-agent.md:1"))
            # Without a workspace nothing can be verified, so only rows count.
            _, failures = tc.validate_reading_log("see pkg/run.py:2\n" + log, manifest, None)
            self.assertTrue([f for f in failures if "TOO_FEW" in f])
            # Required files are still checked row by row: a citation is no substitute for the row.
            _, failures = tc.validate_reading_log("see pkg/run.py:2\n", manifest, ws)
            self.assertIn(f"READING_LOG_MISSING:{ws / 'base.md'}", failures)

    def test_declared_evidence_must_be_substantiated_by_the_baseline(self):
        """A bare EVIDENCE_DECLARED line must not switch the evidence gate off.

        Round-02 finding (Tim Sir): available_evidence_ids came from the baseline and were
        accepted by bare set membership, so the audited party could disable the gate
        against itself with one line of free text.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "real.py").write_text("x = 1\n")
            declared = {"affected_code_path", "reasoning_trace", "rollback_plan", "test_output"}

            kept, rejected = tc.substantiate_declared_evidence(declared, "no evidence at all", workspace)
            self.assertEqual(kept, set())
            self.assertEqual(len(rejected), 4)
            self.assertTrue(all(r.startswith("UNSUBSTANTIATED_EVIDENCE_DECLARATION:") for r in rejected))

            # A resolvable workspace path backs the location-style identifiers only.
            kept, rejected = tc.substantiate_declared_evidence(declared, "see `real.py` for it", workspace)
            self.assertEqual(kept, {"affected_code_path", "rollback_plan"})
            self.assertEqual({r.split(":")[1].split(" ")[0] for r in rejected}, {"reasoning_trace", "test_output"})

            # A fenced block backs the captured-content identifiers only.
            kept, _ = tc.substantiate_declared_evidence(declared, "```\nRan 27 tests\n```", workspace)
            self.assertEqual(kept, {"reasoning_trace", "test_output"})

            # Both present -> everything declared is substantiated.
            kept, rejected = tc.substantiate_declared_evidence(
                declared, "`real.py`\n```\noutput\n```", workspace)
            self.assertEqual(kept, declared)
            self.assertEqual(rejected, [])

            # A path that does not exist in the workspace does not count.
            kept, _ = tc.substantiate_declared_evidence({"affected_code_path"}, "`absent.py`", workspace)
            self.assertEqual(kept, set())

            # Regression: a baseline citing code only as path:line had no "resolvable path".
            for text in ("- `real.py:1` handles it", "see real.py:1-2.", "(real.py:1)", "real.py:3 at line start",
                         "trailing real.py"):
                with self.subTest(cited=text):
                    kept, _ = tc.substantiate_declared_evidence({"affected_code_path"}, text, workspace)
                    self.assertEqual(kept, {"affected_code_path"})
            kept, _ = tc.substantiate_declared_evidence({"affected_code_path"}, "`absent.py:12`", workspace)
            self.assertEqual(kept, set())
            kept, _ = tc.substantiate_declared_evidence({"affected_code_path"}, "`../real.py:1`", workspace)
            self.assertEqual(kept, set())


    def test_every_shipped_evidence_identifier_resolves_to_a_kind(self):
        """No manifest identifier may fall through to the unclassified branch.

        Round-03 finding (Sam Koo): `interface_definition` matched neither kind and reached
        a permissive `else` that accepted either backing, which was weaker than the
        two-kind rule the docs state.
        """
        manifest = (ROOT / "skill/config/evidence_manifest.yaml").read_text(encoding="utf-8")
        body = manifest.split("blocker_classes:", 1)[1]
        identifiers = sorted(set(re.findall(r"^\s+-\s+([a-z_]+)\s*(?:#.*)?$", body, re.MULTILINE)))
        self.assertGreaterEqual(len(identifiers), 13, identifiers)
        unclassified = [i for i in identifiers if tc.evidence_kind(i) is None]
        self.assertEqual(unclassified, [], f"unclassified evidence identifiers: {unclassified}")

        # And an unclassified identifier must fail closed rather than be accepted.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "real.py").write_text("x = 1\n")
            kept, rejected = tc.substantiate_declared_evidence(
                {"not_a_known_identifier"}, "`real.py`\n```\nout\n```", workspace)
            self.assertEqual(kept, set())
            self.assertEqual(len(rejected), 1)


class ConsensusAuditProgressTests(unittest.TestCase):
    def test_progress_language_variants_and_independent_catalogs(self):
        # Direct CLI invocation falls back to English; agents always pass the conversation language.
        self.assertEqual(tc.DEFAULT_PROGRESS_LANGUAGE, "en")
        self.assertIn("will enter", tc.load_progress_messages(tc.DEFAULT_PROGRESS_LANGUAGE)["entry"])
        for language in ("en", "EN-us", "en_GB", "zh", "zh-CN", "zh_Hans", "zh-SG"):
            with self.subTest(language=language):
                messages = tc.load_progress_messages(language)
                expected = "will enter" if language.lower().startswith("en") else "将进入"
                self.assertIn(expected, messages["entry"])
                messages["entry"] = "changed by caller"
                self.assertIn(expected, tc.load_progress_messages(language)["entry"])
        for language in ("de", "zh-TW", "zh-Hant", "", "unknown"):
            with self.subTest(language=language), self.assertRaisesRegex(ValueError, "progress-messages-file"):
                tc.load_progress_messages(language)

    def test_invalid_catalogs_fail_before_artifacts_or_model_calls(self):
        invalid_catalogs = [
            [], {}, {**CUSTOM_PROGRESS, "extra": "unexpected"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer} {phase}"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer.name} {phase} {focus}"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer!r} {phase} {focus}"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer:>5} {phase} {focus}"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer} {phase} {focus"},
            {**CUSTOM_PROGRESS, "entry": "{reviewer} {phase} {focus}\n"},
            {**CUSTOM_PROGRESS, "phase_review": ""},
            {**CUSTOM_PROGRESS, "phase_review": 3},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "bad.json"
            argv = ["consensus_audit.py", "--topic", "invalid-language", "--phase", "prework",
                    "--workspace", tmp, "--baseline-file", str(Path(tmp) / "unused.md"),
                    "--progress-language", "custom", "--progress-messages-file", str(catalog)]
            for invalid in invalid_catalogs + ["malformed JSON", "missing file"]:
                with self.subTest(invalid=invalid):
                    if invalid == "missing file":
                        catalog.unlink()
                    else:
                        catalog.write_text(invalid if isinstance(invalid, str) else json.dumps(invalid))
                    err = io.StringIO()
                    with mock.patch.object(sys, "argv", argv), redirect_stderr(err), \
                         mock.patch.object(tc, "run_agent_backend") as backend:
                        self.assertEqual(tc.main(), 1)
                    backend.assert_not_called()
                    self.assertIn("[ERROR]", err.getvalue())
                    self.assertFalse((Path(tmp) / ".discuss").exists())

    def review(self):
        return tc.build_structured_review(
            reviewer_name=tc.REVIEWER_A_DISPLAY_NAME,
            verdict="APPROVE", risk_level="LOW", blockers=[], suggestions=[],
            decisions=["Evidence checked", "No blockers", "Ready for synthesis"],
            consensus_statement="No execution blockers found.",
        )

    def peer_args(self, cache_dir):
        return {
            "prompt": "audit prompt", "model": tc.DEFAULT_REVIEWER_A_MODEL, "effort": "adaptive",
            "backend": tc.DEFAULT_AGENT_BACKEND, "reviewer_name": tc.REVIEWER_A_DISPLAY_NAME,
            "reviewer_slug": tc.REVIEWER_A_SLUG, "timeout_sec": 1.0, "peer_retries": 0,
            "retry_backoff_sec": 0.0, "retry_timeout_multiplier": 1.0, "phase": "postwork",
        }

    def test_live_entry_is_flushed_before_invocation_and_failure_is_distinct(self):
        class ProgressStream(io.StringIO):
            def flush(self):
                self.last_flushed = self.getvalue()

        for language, outcome in ((lang, result) for lang in ("zh", "en")
                                  for result in ("success", "command-failure", "parse-failure")):
            with self.subTest(language=language, outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                err, out = ProgressStream(), io.StringIO()

                def invoke(*args, language=language, err=err, outcome=outcome):
                    entry = "将进入" if language == "zh" else "will enter"
                    self.assertIn(f"Tim Sir (高彦博) {entry} postwork", err.last_flushed)
                    self.assertNotIn("已返回" if language == "zh" else "returned a", err.getvalue())
                    return tc.CommandResult(
                        ok=outcome != "command-failure", stdout=self.review(),
                        stderr="failed" if outcome == "command-failure" else "",
                        command="mock reviewer", returncode=1 if outcome == "command-failure" else 0,
                    ), []

                parser_path = "fail-closed" if outcome == "parse-failure" else "already-structured"
                with redirect_stderr(err), redirect_stdout(out), \
                     mock.patch.object(tc, "run_agent_with_retries", side_effect=invoke), \
                     mock.patch.object(tc, "normalize_agent_review", return_value=(self.review(), parser_path)):
                    args = self.peer_args(tmp)
                    args["progress_messages"] = tc.load_progress_messages(language)
                    tc.run_peer_review(**args)
                self.assertEqual(out.getvalue(), "")
                if outcome == "success":
                    self.assertIn("已返回 postwork" if language == "zh" else "returned a postwork", err.getvalue())
                    self.assertIn("待汇总核验" if language == "zh" else "pending synthesis", err.getvalue())
                else:
                    self.assertIn("fail-closed", err.getvalue())
                    self.assertNotIn("已返回" if language == "zh" else "returned a", err.getvalue())

    def test_runner_progress_respects_phase_active_roles_and_preflight_gates(self):
        cases = (
            ("prework", "low", "live"),
            ("postwork", "high", "live"),
            ("prework", "high", "dry-run"),
            ("postwork", "high", "preflight-failure"),
        )
        for language, (phase, risk, mode) in ((lang, case) for lang in ("en", "custom", "zh") for case in cases):
            with self.subTest(language=language, phase=phase, risk=risk, mode=mode), tempfile.TemporaryDirectory() as tmp:
                baseline = Path(tmp) / "baseline.md"
                baseline.write_text("COVERAGE: changed/planned symbols 1 | refs 1 | formal_tests 1 | validation_commands 1 | uncovered 0\n")
                argv = ["consensus_audit.py", "--topic", "progress", "--phase", phase,
                        "--risk-level", risk, "--workspace", tmp, "--baseline-file", str(baseline),
                        "--role-config", str(SKILL_DIR / "config/role_config.yaml"),
                        "--peer-retries", "0", "--model-preflight-retries", "0"]
                if language == "custom":
                    catalog = Path(tmp) / "custom.json"
                    catalog.write_text(json.dumps(CUSTOM_PROGRESS, ensure_ascii=False), encoding="utf-8")
                    argv.extend(["--progress-language", "custom", "--progress-messages-file", str(catalog)])
                elif language == "zh":
                    argv.extend(["--progress-language", "zh"])
                # English deliberately omits the flag to exercise the default (DEFAULT_PROGRESS_LANGUAGE).
                if mode == "dry-run":
                    argv.append("--dry-run")
                invocations = []

                def backend(prompt, model, effort, reviewer_name, timeout_sec, backend,
                            mode=mode, invocations=invocations):
                    invocations.append(reviewer_name)
                    return tc.CommandResult(
                        ok=mode != "preflight-failure",
                        stdout="OK" if prompt == "Reply exactly OK" else self.review(),
                        stderr="model unavailable" if mode == "preflight-failure" else "",
                        command="mock backend", returncode=1 if mode == "preflight-failure" else 0,
                        model=model, reviewer=reviewer_name, backend=backend, agent="codex",
                    )

                err, out = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(tc, "resolve_agent_request", return_value=("codex", "codex")), \
                     mock.patch.object(tc, "run_agent_backend", side_effect=backend), \
                     redirect_stderr(err), redirect_stdout(out):
                    status = tc.main()
                progress = err.getvalue()
                self.assertNotIn("[PROGRESS]", out.getvalue())
                if mode == "live":
                    self.assertEqual(status, 0)
                    entry = {"zh": "将进入", "en": "will enter", "custom": "begins"}[language]
                    self.assertIn(f"Tim Sir (高彦博) {entry} {phase}", progress)
                    self.assertIn(f"Sam Koo (古泽琛) {entry} {phase}", progress)
                    if language != "zh":
                        self.assertNotIn("检查", progress)
                        self.assertNotIn("将进入", progress)
                    self.assertEqual("Madam Leung (梁小柔)" in progress, risk == "high")
                    if risk == "high":
                        round_dir = next((Path(tmp) / ".discuss/progress").glob("round-*-postwork"))
                        self.assertTrue((round_dir / "35-nicole-leung.md").is_file())
                        prompt = (round_dir / "92-prompt-nicole-leung.txt").read_text()
                        self.assertIn("Madam Leung", prompt)
                        self.assertNotIn("Nicole Leung", prompt)
                else:
                    self.assertEqual(status, 2)
                    for entry in ("将进入", "will enter", "begins"):
                        self.assertNotIn(entry, progress)
                    self.assertTrue(all(name.startswith("required-model-preflight:") for name in invocations))
                    if mode == "dry-run":
                        self.assertEqual(invocations, [])
                        self.assertIn({"zh": "演练模式", "en": "dry-run", "custom": "custom-dryrun"}[language], progress)
                    else:
                        self.assertIn({"zh": "预检未通过", "en": "preflight failed", "custom": "custom-preflight-failed"}[language], progress)


    def test_reviewers_start_in_workspace_not_runner_cwd(self):
        """A Claude Code reviewer can only read files under its working directory.

        Regression: reviewer subprocesses inherited the runner's cwd. An orchestrator that
        ran the documented `python3 scripts/consensus_audit.py` from the skill directory
        left every reviewer unable to open the code under review, with no error raised.
        """
        import os
        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as elsewhere:
            workspace = Path(ws_tmp).resolve()
            baseline = workspace / "baseline.md"
            baseline.write_text("COVERAGE: changed/planned symbols 1 | refs 1 | formal_tests 1 | validation_commands 1 | uncovered 0\n")
            argv = ["consensus_audit.py", "--topic", "cwd", "--phase", "prework",
                    "--workspace", str(workspace), "--baseline-file", str(baseline),
                    "--peer-retries", "0", "--model-preflight-retries", "0"]
            seen_cwds = []
            review = self.review

            class FakePopen:
                def __init__(self, cmd, cwd=None, **_kwargs):
                    seen_cwds.append(cwd)
                    self.returncode = 0
                    self.pid = 0
                    self.stdin_text = None

                def communicate(self, input: str = "", timeout=None):
                    return ("OK" if input == "Reply exactly OK" else review(), "")

            previous_cwd = os.getcwd()
            os.chdir(elsewhere)
            try:
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                     mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                     mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                     redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    status = tc.main()
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(status, 0)
            self.assertTrue(seen_cwds)
            self.assertEqual({Path(c) for c in seen_cwds}, {workspace})

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tc, "REVIEWER_WORKDIR", Path(tmp).resolve()):
            result = tc.run_command([sys.executable, "-c", "import os; print(os.getcwd())"], prompt="")
            self.assertEqual(Path(result.stdout.strip()).resolve(), Path(tmp).resolve())

    def test_round_whose_reviewers_failed_does_not_close_prior_blockers(self):
        """Absence from a round that reviewed nothing is not closure.

        Regression: every reviewer call in a round hit a usage limit, the round's open set held
        only the runner's own failure blockers, and all five findings of the previous round were
        written under CLOSED_BLOCKERS, so a later passing round could report CONSENSUS_READY
        without anyone re-checking them. The same must hold when one reviewer exits 0 but its
        output cannot be parsed and the runner substitutes a fail-closed body.

        A round in between that recorded no ledger must not break the chain either: a --dry-run
        round writes "(none)" as its open blockers, --operator-halt and an interrupted round
        write nothing, and the next round used to inherit an empty set from any of them. Nor
        may reviewer-supplied text in a real round's summary make it look like one of those.
        """
        for failure in ("exit-1", "unparsable-exit-0"):
            for between in (None, "dry-run", "operator-halt", "interrupted", "interrupted-after-registry",
                            "spoofed-summary", "truncated-summary", "corrupt-registry"):
                with self.subTest(failure=failure, between=between):
                    self._assert_prior_blocker_survives_failed_round(failure, between)

    def _assert_prior_blocker_survives_failed_round(self, failure, between):
        import os
        with tempfile.TemporaryDirectory() as ws_tmp:
            workspace = Path(ws_tmp).resolve()
            (workspace / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            baseline = workspace / "baseline.md"
            baseline.write_text("COVERAGE: changed/planned symbols 1 | refs 1 | formal_tests 1 | validation_commands 1 | uncovered 0\n")
            blocker = "[app.py:2] y is assigned 2 but the caller needs 3"
            late_blocker = "[app.py:1] x is never read"
            revise = tc.build_structured_review(
                reviewer_name=tc.REVIEWER_A_DISPLAY_NAME, verdict="REVISE", risk_level="MEDIUM",
                blockers=[blocker], suggestions=[], decisions=["a", "b", "c"], consensus_statement="fix y",
            )
            approve = tc.build_structured_review(
                reviewer_name=tc.REVIEWER_B_DISPLAY_NAME, verdict="APPROVE", risk_level="LOW",
                blockers=[], suggestions=[], decisions=["a", "b", "c"], consensus_statement="ok",
            )
            reviewers_fail = False

            class FakePopen:
                def __init__(self, cmd, cwd=None, **_kwargs):
                    self.returncode = 0
                    self.pid = 0

                def communicate(self, input: str = "", timeout=None):
                    if input == "Reply exactly OK":
                        return ("OK", "")
                    if not reviewers_fail:
                        return (revise, "")
                    if failure == "exit-1":
                        self.returncode = 1
                        return ("You've hit your session limit", "")
                    # Tim Sir answers in prose (also on the reformat retry); the others approve.
                    if (input or "").startswith(("You are Tim Sir", "Reformat the following review")):
                        return ("I looked around and it seems mostly fine.", "")
                    return (approve, "")

            def run_round(*extra):
                argv = ["consensus_audit.py", "--topic", "carry", "--phase", "postwork",
                        "--workspace", str(workspace), "--baseline-file", str(baseline),
                        "--peer-retries", "0", "--model-preflight-retries", "0", *extra]
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                     mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                     mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                     mock.patch.object(tc, "run_cross_review_transition", return_value=(None, [])), \
                     redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    return tc.main()

            previous_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                run_round()
                topic = workspace / ".discuss/carry"
                if between == "dry-run":
                    self.assertEqual(run_round("--dry-run"), 2)
                elif between == "operator-halt":
                    self.assertEqual(run_round("--operator-halt", "--operator-halt-reason", "pause"), 64)
                elif between == "interrupted":
                    (topic / "round-02-postwork").mkdir()
                elif between == "interrupted-after-registry":
                    # Killed after 02-open-blockers.json, before AUDIT_SUMMARY.md: the registry is the ledger.
                    # It also holds a finding first raised in that round, which must not be lost.
                    registry = json.loads((topic / "round-01-postwork/02-open-blockers.json").read_text())
                    registry["rows"].append({"blocker_id": tc.short_blocker_id(late_blocker), "status": "open",
                                             "text": late_blocker, "reviewer": tc.REVIEWER_B_DISPLAY_NAME,
                                             "source_file": "30-sam-koo.md", "stagnation_count": 1})
                    (topic / "round-02-postwork").mkdir()
                    (topic / "round-02-postwork/02-open-blockers.json").write_text(json.dumps(registry))
                elif between == "spoofed-summary":
                    with (topic / "round-01-postwork/ROUND_SUMMARY.md").open("a", encoding="utf-8") as fh:
                        fh.write("- dry-run mode only generated scaffolding artifacts.\n")
                elif between == "truncated-summary":
                    # Codex release-audit P1: a summary cut off after its OPEN_BLOCKERS heading read as
                    # "nothing open" and won over the complete registry.
                    summary = topic / "round-01-postwork/AUDIT_SUMMARY.md"
                    text = summary.read_text(encoding="utf-8")
                    summary.write_text(text[:text.index("## OPEN_BLOCKERS\n") + len("## OPEN_BLOCKERS\n")],
                                       encoding="utf-8")
                elif between == "corrupt-registry":
                    # The summary is the fallback when the registry does not parse.
                    (topic / "round-01-postwork/02-open-blockers.json").write_text('{"rows": [', encoding="utf-8")
                reviewers_fail = True
                run_round()
            finally:
                os.chdir(previous_cwd)
            rounds = sorted(topic.glob("round-*-postwork"))
            self.assertEqual(len(rounds), 2 if between in (None, "spoofed-summary", "truncated-summary",
                                                           "corrupt-registry") else 3)
            first, second = rounds[0], rounds[-1]
            canon = tc.canonical_blocker(blocker)
            if between == "interrupted-after-registry":
                self.assertIn(tc.canonical_blocker(late_blocker),
                              (tc.parse_open_blockers_from_audit_summary_file(second / "AUDIT_SUMMARY.md") or set()))
            if between != "truncated-summary":   # that case truncated round 1's summary on purpose
                self.assertIn(canon, (tc.parse_open_blockers_from_audit_summary_file(first / "AUDIT_SUMMARY.md") or set()))
            self.assertIn(canon, (tc.parse_open_blockers_from_audit_summary_file(second / "AUDIT_SUMMARY.md") or set()))
            closed = json.loads((second / "03-closed-blockers.json").read_text(encoding="utf-8"))["rows"]
            self.assertNotIn(canon, {tc.canonical_blocker(row["text"]) for row in closed})
            second_rows = {r["canonical_text"]: r for r in json.loads((second / "02-open-blockers.json").read_text())["rows"]}
            if between == "corrupt-registry":
                self.assertIn("PREVIOUS_OPEN_SOURCE: AUDIT_SUMMARY_FALLBACK", (second / "AUDIT_SUMMARY.md").read_text())
            else:
                first_rows = {r["canonical_text"]: r
                              for r in json.loads((first / "02-open-blockers.json").read_text())["rows"]}
                self.assertEqual(second_rows[canon]["stagnation_count"], first_rows[canon]["stagnation_count"])
                if between == "truncated-summary":
                    self.assertIn("PREVIOUS_OPEN_SOURCE: BLOCKER_REGISTRY",
                                  (second / "AUDIT_SUMMARY.md").read_text())
            transitions = json.loads((second / "12-blocker-transitions.json").read_text(encoding="utf-8"))["rows"]
            self.assertIn("carried_unreviewed", {row["transition"] for row in transitions})

    def test_skill_docs_do_not_run_the_runner_from_the_skill_directory(self):
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            with self.subTest(doc=doc):
                self.assertNotRegex(text, r"(?m)^python3 scripts/consensus_audit\.py \\$")
                self.assertIn('python3 "$SKILL_DIR/scripts/consensus_audit.py"', text)
                self.assertIn("Run it from the workspace root", text)

    def test_repeated_stay_vote_on_new_issues_is_not_stagnation(self):
        """A STAY vote is scoped to the issues it disputed.

        Regression: CROSS_REVIEW_NOT_CONVERGED carried only the reviewer and vote, so a STAY
        in two consecutive rounds produced identical text and the stagnation counter
        escalated to MODERATOR_DECISION_REQUIRED although every finding was new.
        """
        stay = tc.CommandResult(ok=True, stdout="INJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: STAY\n",
                                stderr="", command="mock", returncode=0)
        rows = [
            ("Tim Sir", "tim-sir", "model-a", "high", "review a"),
            ("Sam Koo", "sam-koo", "model-b", "high", "review b"),
        ]

        def stay_failures(issues):
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(tc, "run_agent_with_retries", return_value=(stay, [])):
                _, failures = tc.run_cross_review_transition(
                    Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", issue) for issue in issues],
                    1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
                )
            return [f for f in failures if f.startswith("CROSS_REVIEW_NOT_CONVERGED:sam-koo:STAY")]

        round_one = stay_failures(["[a.py:1] first finding", "[b.py:2] second finding"])
        round_two = stay_failures(["[a.py:5] a new finding", "[c.py:3] another new one"])
        same_again = stay_failures(["[b.py:2] second finding", "[a.py:1] first finding"])
        self.assertEqual(len(round_one), 1)
        self.assertNotEqual(round_one, round_two)
        self.assertEqual(round_one, same_again)  # the same disputed set, in any order, still stagnates

        record = lambda text: [(tc.SYSTEM_REVIEWER_NAME, "49-consensus-blockers.md", text)]
        prev_rows, _, _ = tc.build_blocker_registry(record(round_one[0]), None, set(), [], 1, "postwork")
        prev_set = {tc.canonical_blocker(round_one[0])}
        new_rows, _, _ = tc.build_blocker_registry(record(round_two[0]), None, prev_set, prev_rows, 2, "postwork")
        stuck_rows, _, _ = tc.build_blocker_registry(record(same_again[0]), None, prev_set, prev_rows, 2, "postwork")
        self.assertEqual(new_rows[0]["stagnation_count"], 1)
        self.assertEqual(stuck_rows[0]["stagnation_count"], 2)

    def test_role_config_models_match_code_defaults_and_are_documented_as_records(self):
        """role_config.yaml's reviewer models are not applied; they must not drift or be sold as knobs."""
        text = (SKILL_DIR / "config/role_config.yaml").read_text(encoding="utf-8")
        models = dict(re.findall(r"(?m)^  (tim-sir|sam-koo|nicole-leung):\n    model: (\S+)", text))
        source = (SKILL_DIR / "scripts/consensus_audit.py").read_text(encoding="utf-8")
        defaults = dict(re.findall(r'DEFAULT_REVIEWER_([ABC])_MODEL = _ENV_REVIEWER_\1_MODEL or "([^"]+)"', source))
        self.assertEqual(models, {"tim-sir": defaults["A"], "sam-koo": defaults["B"], "nicole-leung": defaults["C"]})
        self.assertIn("do not select the model", text)
        for doc in ("README.md", "README.zh-CN.md"):
            readme = (ROOT / doc).read_text(encoding="utf-8")
            with self.subTest(doc=doc):
                self.assertNotIn("Per-role defaults also live in", readme)
                self.assertNotIn("各角色的默认值也可以直接改", readme)

    def _run_single_round(self, workspace, tim_blockers, *extra, tim_verdict=None, stderr=None, baseline_text=""):
        """Run one live round with Tim Sir raising `tim_blockers` and Sam Koo approving.

        Tim Sir's verdict defaults to REVISE when he raises blockers and APPROVE when he raises none,
        so the review is self-consistent unless a test asks for `tim_verdict` explicitly.
        `baseline_text` is appended to the baseline's COVERAGE line.
        """
        import os
        baseline = workspace / "baseline.md"
        baseline.write_text("COVERAGE: changed/planned symbols 1 | refs 1 | formal_tests 1 | validation_commands 1 | uncovered 0\n"
                            + baseline_text)
        revise = tc.build_structured_review(
            reviewer_name=tc.REVIEWER_A_DISPLAY_NAME,
            verdict=tim_verdict or ("REVISE" if tim_blockers else "APPROVE"), risk_level="MEDIUM",
            blockers=tim_blockers, suggestions=[], decisions=["a", "b", "c"], consensus_statement="fix",
        )
        approve = tc.build_structured_review(
            reviewer_name=tc.REVIEWER_B_DISPLAY_NAME, verdict="APPROVE", risk_level="LOW",
            blockers=[], suggestions=[], decisions=["a", "b", "c"], consensus_statement="ok",
        )

        class FakePopen:
            def __init__(self, cmd, cwd=None, **_kwargs):
                self.returncode = 0
                self.pid = 0

            def communicate(self, input: str = "", timeout=None):
                if input == "Reply exactly OK":
                    return ("OK", "")
                # A reformat retry of Tim Sir's review keeps its content, as a real reviewer would.
                if (input or "").startswith("You are Tim Sir") or (
                        (input or "").startswith("Reformat the following review") and "REVISE" in (input or "")):
                    return (revise, "")
                return (approve, "")

        # The shipped blocker classes, without the READING_LOG rules the stub reviews cannot meet.
        shipped = (SKILL_DIR / "config/evidence_manifest.yaml").read_text(encoding="utf-8")
        manifest = workspace / "manifest.yaml"
        manifest.write_text("config_version: 2\n" + shipped[shipped.index("\nblocker_classes:"):], encoding="utf-8")
        argv = ["consensus_audit.py", "--topic", "gate", "--phase", "postwork",
                "--workspace", str(workspace), "--baseline-file", str(baseline),
                "--evidence-manifest-file", str(manifest),
                "--peer-retries", "0", "--model-preflight-retries", "0", *extra]
        previous_cwd = os.getcwd()
        os.chdir(workspace)
        try:
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                 mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                 mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                 redirect_stderr(stderr if stderr is not None else io.StringIO()), redirect_stdout(io.StringIO()):
                code = tc.main()
        finally:
            os.chdir(previous_cwd)
        return code, max((workspace / ".discuss/gate").glob("round-*-postwork"))

    def test_unlisted_empty_or_truncated_blockers_fail_closed(self):
        """Regression: blockers not written as a list, or cut short by a line ending in ":", read as none."""
        section = "## Blockers\n{}\n\n## Suggestions\n1. none\n"
        for ambiguous in ("[app.py:1] x is wrong",                       # a real blocker without a list marker
                          "`app.py:1` x is wrong",
                          "None.\n[app.py:1] x is wrong",               # unlisted None plus unlisted blocker
                          "None.\n- [app.py:1] x is wrong",             # unlisted None plus a listed blocker
                          "See the discussion below.",                   # prose that is neither a list nor "none"
                          "None of the blockers are resolved.",          # contains "none", says the opposite
                          "There are no blockers left except the race below.",
                          "A blocker may remain.\n1. None.",            # a preamble before the none item
                          "1. None.\n2. None.",                          # a repeated none item
                          "None.\nNone.",
                          "1. None.\n   but the race remains",           # a none item that goes on
                          "1. None of the blockers are resolved",
                          "1. [app.py:1] x is wrong\n2. N/A for the rest",
                          "",                                           # empty section
                          "None.\nStill open:\n1. [app.py:1] race",       # a "Still open:" line must not cut the
                          "1. None.\nRemaining:\n2. [app.py:1] race",     # section down to a lone "None."
                          "None.\nRisk: [app.py:1] the race is still unfixed",   # nor may a content line whose
                          "None.\nDecision: defer the race in [app.py:1]",      # first word names a section
                          "None.\nConclusion: [app.py:1] still broken"):
            with self.subTest(body=ambiguous):
                self.assertIsNone(tc.extract_blockers(section.format(ambiguous)))
        for no_blockers in ("None.", "1. None.", "- None", "N/A", "No blockers found in this round.",
                            "1. No blockers found."):
            with self.subTest(body=no_blockers):
                self.assertEqual(tc.extract_blockers(section.format(no_blockers)), [])
        # Only a contract section ends Blockers, so colon-terminated lines stay inside it; and since the
        # section must be a list (or one no-blocker statement), any preamble fails closed.
        for preamble in ("Two issues:\n1. [app.py:1] x\n2. [app.py:2] y",
                         "[app.py:1] the race is unfixed\n1. see above"):
            with self.subTest(preamble=preamble):
                self.assertIsNone(tc.extract_blockers(section.format(preamble)))
        # Neither a blocker item that starts with a section word nor an arbitrary heading ends the section.
        for review in ("3) Blockers:\n1) None.\n2) Conclusion: [a.py:1] the race remains\n4) Suggestions:\n1. x\n",
                       # a numbered item below the Blockers number is an item, whatever section word it opens with
                       "3) Blockers:\n1) None.\n2) Risk Level: [a.py:1] the race remains\n4) Suggestions:\n1. x\n",
                       "3) Blockers:\n1) None.\n2) Verdict: [a.py:1] x\n4) Suggestions:\n1. x\n",
                       "3) Blockers:\n1) None.\n2) Consensus Statement: [a.py:1] x\n4) Suggestions:\n1. x\n",
                       "3) Blockers:\n1) None.\n2) Suggestions:\n[a.py:1] x\n4) Suggestions:\n1. x\n",
                       "## Blockers\n1. None.\n## Still open\n1. [a.py:1] race\n## Suggestions\n1. x\n",
                       "3) Blockers:\n1. None.\n2.\n[a.py:1] race\n4) Suggestions:\n1. x\n",
                       "3) Blockers:\n1) None.\n2)\n[a.py:1] race\n4) Suggestions:\n1. x\n",
                       # under an unnumbered heading every numbered line is an item
                       "## Blockers\n1. None.\n2. Suggestions:\n[a.py:1] race\n## Suggestions\n1. x\n",
                       "## Blockers\n1. None.\n2) Consensus Statement: [a.py:1] x\n## Suggestions\n1. x\n"):
            with self.subTest(review=review):
                self.assertIsNone(tc.extract_blockers(review))
        for verdict, blockers in (("APPROVE", []), ("REVISE", ["[a.py:1] x"])):
            with self.subTest(canonical=verdict):
                review = tc.build_structured_review(reviewer_name="T", verdict=verdict, risk_level="LOW",
                                                    blockers=blockers, suggestions=["s"], decisions=["a", "b", "c"],
                                                    consensus_statement="ok")
                self.assertEqual(tc.extract_blockers(review), blockers)
        # A blocker whose citation is not first would be discarded by the grounding gate, turning
        # REVISE into approval; it is misformatted, so the section fails closed and is reformatted.
        for misplaced in ("1. Race in round claim:\n   [app.py:1] x", "1. Race in [app.py:1] x",
                          "1. [app.py:1] fine\n2. Also `app.py:1` here"):
            with self.subTest(misplaced=misplaced):
                self.assertIsNone(tc.extract_blockers(section.format(misplaced)))
        # Contract sections end Blockers in either numbering style, alone or with an inline value.
        for review in ("3) Blockers:\n   1. None.\n4) Suggestions:\n   1. x\n",
                       "3) Blockers:\n1. None.\n4. Suggestions:\n1. tighten x\n",
                       "3) Blockers:\n1. None.\n7. READING_LOG:\n| # |\n",
                       "3) Blockers:\n   1. None.\n5)\n- DECISION: a\n",
                       "3) Blockers:\n   1. None.\n- DECISION: a\n",
                       "3) Blockers:\n   1. None.\n6) Consensus Statement: fine\n",
                       "## Blockers\n1. None.\n**Suggestions:**\n1. x\n"):
            with self.subTest(review=review):
                self.assertEqual(tc.extract_blockers(review), [])
        self.assertEqual(tc.extract_blockers("3) Blockers:\nNone.\nStill open:\n1. [a.py:1] race\n4) Suggestions:\n1. x\n"),
                         None)
        self.assertEqual(tc.extract_blockers(section.format("1. [app.py:1] x\n2. [app.py:2] y")),
                         ["[app.py:1] x", "[app.py:2] y"])
        # A word that merely begins with "none" is not a no-blocker statement.
        self.assertFalse(tc.opens_with_no_blocker_statement("Nonetheless the race remains"))
        self.assertTrue(tc.opens_with_no_blocker_statement("None of the blockers are resolved"))

    def test_revise_with_an_empty_blockers_list_fails_closed(self):
        """REVISE that lists no blocker contradicts itself; the gate must not read it as effective approval.

        Every way of cutting a Blockers section down to a lone "None." relied on REVISE + [] counting
        as approval, so the gate closes that path whatever the parser does.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            revise_body = tc.build_structured_review(
                reviewer_name=tc.REVIEWER_A_DISPLAY_NAME, verdict="REVISE", risk_level="MEDIUM",
                blockers=[], suggestions=[], decisions=["a", "b", "c"], consensus_statement="fix")
            self.assertEqual(tc.extract_blockers(revise_body), [])  # the parse itself is well formed
            code, round_dir = self._run_single_round(workspace, [], tim_verdict="REVISE")
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertIn(tc.REVISE_WITH_NO_BLOCKERS_TEXT, summary)
            self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
            # A consistent APPROVE with "1. None." still passes.
            code, _ = self._run_single_round(workspace, [])
            self.assertEqual(code, 0)

    def test_revise_with_an_uncited_blocker_fails_closed(self):
        """Regression: an uncited REVISE blocker was discarded by grounding and REVISE read as approval."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["the race in the round claim is still open"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
            self.assertIn(tc.UNCITED_REVISE_BLOCKER_PREFIX, summary)
            self.assertIn("the race in the round claim is still open", summary)
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())
            self.assertNotIn("the race in the round claim is still open", json.dumps(discarded))
            # A cited blocker still gates as a reviewer blocker, not a runner-generated one.
            code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertNotIn(tc.UNCITED_REVISE_BLOCKER_PREFIX, summary)

    def test_approve_with_an_uncited_blocker_fails_closed(self):
        """Release-audit P1: an uncited blocker under APPROVE was discarded, so a self-contradictory
        APPROVE passed as CONSENSUS_READY."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["the race in X is unfixed"], tim_verdict="APPROVE")
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
            self.assertIn(tc.UNCITED_APPROVE_BLOCKER_PREFIX, summary)
            self.assertNotIn("EVIDENCE_COMPLETENESS_FAIL", summary)   # a runner finding, not evidence-checked
            # A citation that does not resolve is still discarded (intentional boundary), under either verdict.
            code, round_dir = self._run_single_round(workspace, ["[missing.py:1] x"], tim_verdict="APPROVE")
            self.assertEqual(code, 0)
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())["rows"]
            self.assertEqual([row["reason"] for row in discarded], ["citation_file_missing"])

    def test_line_col_citation_grounds_the_blocker(self):
        """Release-audit P1: `[path:line:col]` was read as path `path:line`, discarded as a missing file,
        and a REVISE whose only blocker it was passed as CONSENSUS_READY."""
        for text, expected in (("[app.py:1:5] x", ("app.py", 1)), ("`[app.py:1:5]` x", ("app.py", 1)),
                               ("`app.py:3:1` x", ("app.py", 3)), ("[app.py:2-4:7] x", ("app.py", 2))):
            with self.subTest(text=text):
                self.assertEqual(tc.extract_leading_file_line_citation(text), expected)
        for text in ("[app.py:L1] x", "[app.py:1:] x", "[app.py:1:c5] x"):
            with self.subTest(rejected=text):
                self.assertIsNone(tc.extract_leading_file_line_citation(text))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["[app.py:1:5] x is wrong"])
            self.assertNotEqual(code, 0)
            rows = json.loads((round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]
            self.assertIn("[app.py:1:5] x is wrong", [row["text"] for row in rows])
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())["rows"]
            self.assertEqual(discarded, [])

    def test_several_citations_in_one_bracket_ground_the_blocker(self):
        """Release-audit P1: `[a.py:1, a.py:2]` and `` [`a.py`:1] `` parsed as a missing path, so the
        blocker was discarded and a REVISE whose only blocker it was passed as CONSENSUS_READY."""
        for text, expected in (("[app.py:1, app.py:2] x", ("app.py", 1)), ("[app.py:1; README.md:3] x", ("app.py", 1)),
                               ("`app.py:2, app.py:1` x", ("app.py", 2)), ("[`app.py`:1] x", ("app.py", 1)),
                               ("[app.py:1-2, app.py:4:3] x", ("app.py", 1))):
            with self.subTest(text=text):
                self.assertEqual(tc.extract_leading_file_line_citation(text), expected)
        # A bracket that is not a list of citations is misformatted: the Blockers section fails closed.
        section = "## Blockers\n1. {}\n\n## Suggestions\n1. none\n"
        for text in ("[app.py:1, 20] x", "[app.py:1, see above] x", "[app.py:1,] x"):
            with self.subTest(rejected=text):
                self.assertIsNone(tc.extract_leading_file_line_citation(text))
                self.assertIsNone(tc.extract_blockers(section.format(text)))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            for blocker in ("[app.py:1, app.py:2] x and y are wrong", "[`app.py`:2] y is wrong",
                            # Re-review P1: only the first citation was checked, so these were discarded.
                            "[<embedded-baseline-doc>:1, app.py:2] y is wrong",
                            "[missing.py:1; app.py:9, app.py:2] y is wrong"):
                with self.subTest(blocker=blocker):
                    self.assertEqual(tc.verify_file_line_citation(blocker, workspace),
                                     (True, "verified_file_line_citation"))
                    code, round_dir = self._run_single_round(workspace, [blocker])
                    self.assertEqual(code, 2)
                    rows = json.loads((round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]
                    self.assertIn(blocker, [row["text"] for row in rows])
                    discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())["rows"]
                    self.assertEqual(discarded, [])
            # When no citation in the bracket resolves, the blocker is still discarded (the intentional
            # boundary), with the first citation's reason.
            self.assertEqual(tc.verify_file_line_citation("[app.py:9, missing.py:1] x", workspace),
                             (False, "citation_line_out_of_range"))
            self.assertEqual(tc.extract_leading_file_line_citations("[app.py:1-2, b.py:4:3] x"),
                             [("app.py", 1), ("b.py", 4)])
            code, round_dir = self._run_single_round(workspace, ["[<embedded-baseline-doc>:1, missing.py:1] x"])
            self.assertEqual(code, 0)
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())["rows"]
            self.assertEqual([row["reason"] for row in discarded], ["citation_path_not_local_file"])

    def test_non_integer_stagnation_count_in_the_prior_ledger_is_not_a_crash(self):
        """Release-audit P1: a prior 02-open-blockers.json that parsed but held a non-integer
        stagnation_count raised out of build_blocker_registry, leaving a round with no ledger."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            blocker = "[app.py:1] x is wrong"
            code, first = self._run_single_round(workspace, [blocker])
            self.assertEqual(code, 2)
            registry_path = first / "02-open-blockers.json"
            for bad in ("two", None, [1]):
                with self.subTest(bad=bad):
                    registry = json.loads(registry_path.read_text(encoding="utf-8"))
                    for row in registry["rows"]:
                        row["stagnation_count"] = bad
                    registry_path.write_text(json.dumps(registry), encoding="utf-8")
                    code, round_dir = self._run_single_round(workspace, [blocker])
                    self.assertEqual(code, 2)
                    summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
                    self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
                    rows = {r["text"]: r for r in json.loads((round_dir / "02-open-blockers.json").read_text())["rows"]}
                    # The blocker is still open; only its unreadable count restarts.
                    self.assertEqual(rows[blocker]["stagnation_count"], 1)
                    registry_path = round_dir / "02-open-blockers.json"

    def test_operator_halt_without_a_reason_claims_no_round(self):
        """Release-audit P1: the reason was checked after the round directory was claimed, leaving it empty."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--operator-halt"]
            err = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                    redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(tc.main(), 1)
            self.assertIn("--operator-halt requires --operator-halt-reason", err.getvalue())
            self.assertFalse((workspace / ".discuss").exists())

    def test_open_blockers_file_with_non_object_rows_is_rejected_before_any_review(self):
        """Release-audit P1 (cc037de): a row that is not an object raised AttributeError in the ledger
        builder after every reviewer had run, or, in delta-driven mode without the full-history fallback,
        in the context packet right after the round was claimed. Either way the round wrote no ledger."""
        modes = (("--context-assembly-mode", "full-history"),
                 ("--context-assembly-mode", "delta-driven"),
                 ("--context-assembly-mode", "delta-driven", "--no-full-history-fallback-on-integrity-fail"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            open_blockers = workspace / "open.json"
            for rows in (["B-1"], [3], [{"blocker_id": "B-1", "text": "x"}, None]):
                for mode in modes:
                    open_blockers.write_text(json.dumps(rows), encoding="utf-8")
                    argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                            "--baseline-file", str(baseline), "--open-blockers-file", str(open_blockers),
                            *mode, "--peer-retries", "0", "--model-preflight-retries", "0"]
                    err = io.StringIO()
                    with self.subTest(rows=rows, mode=mode), mock.patch.object(sys, "argv", argv), \
                            mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                            mock.patch.object(tc, "run_agent_backend") as backend, \
                            redirect_stderr(err), redirect_stdout(io.StringIO()):
                        self.assertEqual(tc.main(), 1)
                        backend.assert_not_called()
                        self.assertIn("--open-blockers-file must hold a list of objects", err.getvalue())
                        self.assertFalse((workspace / ".discuss").exists())
        # A list of objects still runs, and the round writes its ledger.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            open_blockers = workspace / "open.json"
            open_blockers.write_text(json.dumps([{"blocker_id": "B-1", "text": "[app.py:1] x is wrong"}]),
                                     encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, [], "--open-blockers-file", str(open_blockers))
            self.assertEqual(code, 0)
            self.assertTrue((round_dir / "02-open-blockers.json").is_file())

    def test_suggestions_beyond_five_are_kept_not_truncated(self):
        """Release-audit P1: a review rebuilt from its parts kept only its first five suggestions."""
        suggestions = "\n".join(f"{n}. suggestion {n}." for n in range(1, 8))
        raw = CLAUDE_STYLE_REVIEW.replace("1. Add a focused regression test for the backend path.", suggestions)
        self.assertEqual(len(tc.extract_suggestions(raw)), 7)
        rebuilt, error = tc.attempt_local_structured_review(raw.replace("EFFORT_DECLARATION: high\n\n", ""), "Sam Koo")
        self.assertIsNone(error)
        assert rebuilt is not None
        self.assertEqual(tc.extract_suggestions(rebuilt), [f"suggestion {n}." for n in range(1, 8)])
        # In the contract's own layout Suggestions ends at "5)", not after the DECISION lines and the
        # consensus statement, which the five-item cap used to hide.
        template = tc.build_structured_review("T", "APPROVE", "LOW", [], ["a", "b"], ["d1", "d2", "d3"], "ok")
        self.assertEqual(tc.extract_suggestions(template), ["a", "b"])
        inline = "4) Suggestions:\n1. a\n- DECISION: d1\n6) Consensus Statement: ok\n"
        self.assertEqual(tc.extract_suggestions(inline), ["a"])

    def test_non_positive_peer_timeout_is_rejected(self):
        """Regression: --peer-timeout-sec 0 became "no deadline" and could hang a round forever."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            for value in ("0", "-5"):
                argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                        "--baseline-file", str(baseline), "--dry-run", "--peer-timeout-sec", value]
                err = io.StringIO()
                with self.subTest(value=value), mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                        redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(tc.main(), 1)
                    self.assertIn("--peer-timeout-sec must be > 0", err.getvalue())
                    self.assertFalse((workspace / ".discuss").exists())

    def test_usage_errors_exit_1_not_the_needs_another_round_code(self):
        """Release-audit P1 (67bd7b3): argparse usage errors exited 2, the NEEDS_ANOTHER_ROUND code, so a
        caller reading the exit code alone took a rejected command line for a round that found blockers."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            runner = [sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py")]
            base = ["--phase", "prework", "--workspace", str(workspace), "--baseline-file", str(baseline)]
            for argv in (base,                                                    # missing --topic
                         ["--topic", "t", *base, "--cache-mode", "off"],          # removed flag
                         ["--topic", "t", *base, "--rollout-mode", "canary"],     # removed mode
                         ["--topic", "t", *base, "--peer-retries", "many"],       # malformed value
                         ["--topic", "t", *base, "--no-such-flag"],               # unknown flag
                         ["--topic", "t", *base, "--codex-file", str(baseline)]):  # mutually exclusive
                with self.subTest(argv=argv[-2:]):
                    result = subprocess.run(runner + argv, cwd=workspace, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse((workspace / ".discuss").exists())
            self.assertEqual(subprocess.run(runner + ["--help"], capture_output=True, check=False).returncode, 0)

    def test_stale_stay_threshold_and_stay_count_are_validated_not_rewritten(self):
        """Release-audit P1 (67bd7b3): `stale_stay_threshold or 3` turned an explicit 0 into 3 silently."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            for flag, value, message in (("--stale-stay-threshold", "0", "--stale-stay-threshold must be >= 1"),
                                         ("--stale-stay-threshold", "-2", "--stale-stay-threshold must be >= 1"),
                                         ("--cross-review-stay-count", "-1", "--cross-review-stay-count must be >= 0")):
                argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                        "--baseline-file", str(baseline), "--dry-run", flag, value]
                err = io.StringIO()
                with self.subTest(flag=flag, value=value), mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                        redirect_stderr(err), redirect_stdout(io.StringIO()):
                    self.assertEqual(tc.main(), 1)
                    self.assertIn(message, err.getvalue())
                    self.assertFalse((workspace / ".discuss").exists())
        # A threshold of 1 is used as given, not replaced by the default 3.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong"],
                                                     "--stale-stay-threshold", "1", "--cross-review-stay-count", "1")
            self.assertEqual(code, 4)
            self.assertIn("STATUS: STALE_STAY", (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8"))

    def test_docs_match_the_runner_on_cross_review_passes_phase_and_exit_codes(self):
        """Release-audit P1 (67bd7b3): the docs described an iterated cross-review with -iter<N> files the
        runner never writes, a default --phase the runner does not have, and no exit code for a bad call."""
        protocol = re.sub(r"\s+", " ", (SKILL_DIR / "references/consensus_protocol.md").read_text(encoding="utf-8"))
        skill = re.sub(r"\s+", " ", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        readfirst = re.sub(r"\s+", " ", (SKILL_DIR / "00-READ-FIRST.md").read_text(encoding="utf-8"))
        for text in (protocol, skill):
            self.assertNotIn("iter<N>", text)
            self.assertNotIn("until issues converge or the cap is reached", text)
        self.assertIn("one cross-review pass per round and does not iterate within the round", protocol)
        self.assertIn("There is no second pass within the round", skill)
        self.assertNotIn("`--phase prework|postwork`: default", skill)
        self.assertIn("`--phase prework|postwork`: required", skill)
        help_text = subprocess.run([sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--help"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertNotIn("[--phase", help_text)   # argparse brackets optional flags only
        self.assertIn("usage-error code, 2, is not used", protocol)
        self.assertIn("`1` is an invalid invocation", readfirst)

    def test_none_next_to_real_blockers_fails_closed(self):
        """Regression: a "None." item made extract_blockers drop every real blocker, so REVISE read as approval."""
        section = "## Blockers\n{}\n\n## Suggestions\n1. none\n"
        self.assertIsNone(tc.extract_blockers(section.format("1. None.\n2. [app.py:1] x is wrong")))
        self.assertIsNone(tc.extract_blockers(section.format("1. [app.py:1] x is wrong\n2. None.")))
        self.assertEqual(tc.extract_blockers(section.format("1. None.")), [])
        self.assertEqual(tc.extract_blockers(section.format("1. [app.py:1] x is wrong")), ["[app.py:1] x is wrong"])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["None.", "[app.py:1] x is wrong"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertNotIn("CONSENSUS_READY", summary.split("## OPEN_BLOCKERS")[0])

    def test_removed_rollout_flags_are_rejected_and_shadow_is_a_no_op(self):
        """The canary/full rollout, break-glass mode and shadow parity were removed."""
        base = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--baseline-file", "b.md"]
        for extra in (["--rollout-mode", "canary"], ["--rollout-mode", "full"], ["--cache-mode", "off"],
                      ["--canary-percent", "10"], ["--force-shadow"], ["--break-glass-mode"],
                      ["--shadow-parity-mode", "record"], ["--rollout-readiness-file", "r.json"],
                      ["--no-auto-degrade"], ["--force-lock-steal"]):
            err = io.StringIO()
            with self.subTest(extra=extra), mock.patch.object(sys, "argv", base + extra), \
                    redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                tc.parse_args()
            # Each removed flag names itself and points at the migration notes, not a bare
            # "unrecognized arguments".
            with self.subTest(extra=extra, check="message"):
                self.assertEqual(raised.exception.code, 1)   # not 2, which is NEEDS_ANOTHER_ROUND
                self.assertIn(f"{extra[0]}", err.getvalue())
                self.assertIn("was removed with the canary/full rollout", err.getvalue())
                self.assertIn('"Upgrading" in README.md', err.getvalue())
        for doc in ("README.md", "README.zh-CN.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            with self.subTest(doc=doc):
                for flag in tc.REMOVED_FLAGS:
                    self.assertIn(flag.replace("--no-", "--"), text)
        with mock.patch.object(sys, "argv", base + ["--rollout-mode", "shadow"]):
            self.assertEqual(tc.parse_args().rollout_mode, "shadow")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, [], "--rollout-mode", "shadow")
            self.assertEqual(code, 0)
            removed = {"06-rollout-readiness.json", "15-acceptance-smoke-evidence.json", "16-shadow-parity.json",
                       "17-replay-acceptance.json", "23-degraded-round.json"}
            self.assertFalse(removed & {p.name for p in round_dir.iterdir()})
            topic = round_dir.parent
            self.assertFalse((topic / "80-rollout-state.json").exists())
            self.assertFalse((topic / "80-rollout-state.lock").exists())
            self.assertFalse((topic / ".review-cache").exists())
            metrics = json.loads((round_dir / "70-runtime-metrics.json").read_text(encoding="utf-8"))
            self.assertEqual((metrics["schema_version"], metrics["completed"]), (1, True))
            for row in metrics["reviewers"]:
                self.assertEqual(set(row), {"reviewer", "reviewer_slug", "verdict", "gate_path", "shadow_anomaly",
                                            "parser_path", "timed_out", "returncode", "attempt_count",
                                            "recovered_after_retry", "attempts"})

    def test_docs_do_not_describe_removed_rollout_features_as_present(self):
        """Regression: SKILL.md still pointed readers at "rollout/parity contracts" after their removal."""
        stale = ("rollout/parity", "retries, cache", "rollout state, or parity", "--rollout-mode canary",
                 "canary|full", "does not require a rollout readiness file", "cache reuse", "发布状态、一致性校验")
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md", "skill/00-READ-FIRST.md",
                    "README.md", "README.zh-CN.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            for phrase in stale:
                with self.subTest(doc=doc, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_adaptive_effort_mode_is_a_warned_no_op(self):
        """--adaptive-effort-mode never changed behaviour; its promised two-stage call was dead code."""
        self.assertFalse(hasattr(tc, "run_codex_two_stage_adaptive"))
        # Comments are not name references, so check the text too: nothing may point at removed code.
        source = (SKILL_DIR / "scripts/consensus_audit.py").read_text(encoding="utf-8")
        for removed in ("run_codex_two_stage_adaptive", "run_codex_with_retries", "normalize_codex_review",
                        "DEFAULT_CODEX_MODEL", "CONSENSUS_AUDIT_CODEX_MODEL", "DEFAULT_REASONING_EFFORT",
                        "DEFAULT_MAIN_CODEX_REVISION", "MAIN_CODEX_KEY_NODE_SPECS", "CANONICAL_STATUSES"):
            with self.subTest(removed=removed):
                self.assertFalse(removed in source, f"{removed} is still mentioned in consensus_audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            plain_code, plain_dir = self._run_single_round(workspace, [])
            err = io.StringIO()
            with redirect_stderr(err):
                code, round_dir = self._run_single_round(workspace, [], "--adaptive-effort-mode", "runtime")
            self.assertEqual((plain_code, code), (0, 0))
            for name in ("20-tim-sir.md", "30-sam-koo.md"):
                self.assertEqual((plain_dir / name).read_text(), (round_dir / name).read_text())
        role_config = (SKILL_DIR / "config/role_config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("--adaptive-effort-mode", role_config)

    def test_failed_reviewer_call_is_still_recorded_as_an_anomaly(self):
        """Removing rollout_mode must not flip shadow_anomaly on the command-failure path."""
        failed = tc.CommandResult(ok=False, stdout="", stderr="boom", command="mock", returncode=1)
        with mock.patch.object(tc, "run_agent_with_retries", return_value=(failed, [])), \
                redirect_stderr(io.StringIO()):
            _, result, meta = tc.run_peer_review(
                prompt="p", model="m", effort="adaptive", backend=tc.CLAUDE_AGENT_BACKEND,
                reviewer_name=tc.REVIEWER_A_DISPLAY_NAME, reviewer_slug=tc.REVIEWER_A_SLUG,
                timeout_sec=1.0, peer_retries=0, retry_backoff_sec=0.0, retry_timeout_multiplier=1.0,
            )
        self.assertEqual(result.parser_path, "fail-closed-command")
        self.assertIs(meta["shadow_anomaly"], True)
        self.assertNotIn("canary_hit", meta)

    def test_progress_catalogs_with_legacy_keys_still_load(self):
        """Catalogs written before the cache and rollout gate were removed carry two extra keys."""
        reference = dict(tc.PROGRESS_MESSAGES["en"])
        legacy = {**reference, "cache": "{reviewer} cached {phase}.", "rollout_blocked": "{phase} blocked."}
        with tempfile.TemporaryDirectory() as tmp:
            def load(catalog):
                path = Path(tmp) / "catalog.json"
                path.write_text(json.dumps(catalog), encoding="utf-8")
                return tc.load_progress_messages("fr", str(path))

            self.assertEqual(load(reference), reference)
            self.assertEqual(load(legacy), reference)
            for bad in ({**reference, "unknown_key": "x"}, {k: v for k, v in reference.items() if k != "entry"}):
                with self.subTest(keys=sorted(set(bad) ^ set(reference))), self.assertRaises(ValueError):
                    load(bad)

    def test_reviewer_start_failure_is_a_failed_invocation_not_a_crash(self):
        """Regression: only FileNotFoundError was caught, so PermissionError aborted the run."""
        for error in (PermissionError(13, "Permission denied"), OSError(24, "Too many open files")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(tc.subprocess, "Popen", side_effect=error):
                result = tc.run_command(["claude", "--print"], "prompt", 1.0)
            self.assertFalse(result.ok)
            self.assertIsNone(result.returncode)
            self.assertIn(type(error).__name__, result.stderr)

    def test_blocker_class_comes_from_the_reviewer_tag_not_from_mentioned_words(self):
        """Regression: keyword matching classed blockers by words they only mentioned.

        A blocker saying "the evidence gate reads the schema" or "the rollback note is stale" was
        classed schema_violation / rollback_gap and demanded schema_file or rollback_test_output
        evidence that had nothing to do with it.
        """
        shipped = tc.load_yaml_or_json(SKILL_DIR / "config/evidence_manifest.yaml")
        assert isinstance(shipped, dict)
        self.assertEqual(set(tc.BLOCKER_CLASS_NAMES), set(shipped["blocker_classes"]))
        classify = lambda text: tc.classify_blocker_validation_mode(text, shipped)
        for untagged in ("[a.py:1] the gate reads the schema file before the grounding check",
                         "[a.py:1] this artifact path is written twice",
                         "[a.py:1] the rollback note contradicts the restore block",
                         "[a.py:1] interface test failed on recovery"):
            with self.subTest(untagged=untagged):
                self.assertEqual(classify(untagged), "logic_flaw")
        for tagged, expected in (("[a.py:1] (class: rollback_gap) no restore step", "rollback_gap"),
                                 ("`[a.py:1-4]` (class: Test_Failure) suite fails", "test_failure"),
                                 ("`a.py:1` (class:schema_violation) bad row", "schema_violation")):
            with self.subTest(tagged=tagged):
                self.assertEqual(classify(tagged), expected)
        # The tag counts only right after the citation; elsewhere it is just prose.
        self.assertEqual(classify("[a.py:1] prose (class: rollback_gap)"), "logic_flaw")
        # An unknown tag is kept, so the evidence gate reports it rather than re-classing it.
        self.assertEqual(classify("[a.py:1] (class: made_up) x"), "made_up")
        self.assertEqual(tc.validate_evidence_completeness("x", "made_up", shipped, set())[1],
                         "unknown_evidence_class:made_up")
        prompt = tc.build_prompt("t", "postwork", "doc", tc.REVIEWER_A_DISPLAY_NAME, "l", "f", "r", [])
        self.assertIn("(class: <name>)", prompt)
        for name in tc.BLOCKER_CLASS_NAMES[1:]:
            self.assertIn(name, prompt)

    def test_discarded_blocker_cannot_reenter_through_the_evidence_gate(self):
        """Regression: evidence completeness ran on the pre-grounding blocker list.

        A blocker the grounding gate discarded (here: its citation names a file that does not
        exist) was at the same time promoted back into the open set as an
        EVIDENCE_COMPLETENESS_FAIL system record.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["[missing.py:1] schema mismatch in the emitted row"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotIn("EVIDENCE_COMPLETENESS_FAIL", summary)
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())
            self.assertIn("schema mismatch in the emitted row", json.dumps(discarded))
            self.assertEqual(code, 0)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            # A grounded blocker is still evidence-checked, under the class its reviewer tagged.
            code, round_dir = self._run_single_round(
                workspace, ["[app.py:1] (class: schema_violation) schema mismatch in the emitted row"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("EVIDENCE_COMPLETENESS_FAIL: missing_evidence:schema_file", summary)
            self.assertNotEqual(code, 0)

    def test_unprobeable_citation_path_is_discarded_not_a_crash(self):
        """Regression: a citation with an over-long path component raised ENAMETOOLONG out of the
        grounding gate, crashing the round after the reviewer files and before its blocker ledger."""
        long_name = "a" * 300 + ".py"
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            # From Python 3.14, Path.exists() returns False on ENAMETOOLONG instead of raising, so the over-long
            # name is reported as missing there. Either reason discards the blocker; what matters is no crash.
            unprobeable = {"citation_path_invalid", "citation_file_missing"}
            ok, reason = tc.verify_file_line_citation(f"[{long_name}:1] x", workspace)
            self.assertFalse(ok)
            self.assertIn(reason, unprobeable)
            self.assertEqual(tc.verify_file_line_citation("[a\x00b.py:1] x", workspace), (False, "citation_path_invalid"))
            # Under a missing directory the probe fails with ENOENT first: discarded as missing, also no crash.
            self.assertEqual(tc.verify_file_line_citation(f"[dir/{long_name}:1] x", workspace),
                             (False, "citation_file_missing"))
            self.assertIsNone(tc.resolve_cited_workspace_file(long_name, workspace))
            self.assertEqual(tc.cited_workspace_files(f"see {long_name}:1 and app.py:1", workspace),
                             {workspace / "app.py"})

            # End to end: the blocker is discarded like any unresolvable citation, and the round completes.
            code, round_dir = self._run_single_round(workspace, [f"[{long_name}:1] x is wrong"])
            self.assertEqual(code, 0)
            self.assertTrue((round_dir / "02-open-blockers.json").is_file())
            discarded = json.loads((round_dir / "24-discarded-reviewer-blockers.json").read_text())["rows"]
            self.assertEqual(len(discarded), 1)
            self.assertIn(discarded[0]["reason"], unprobeable)

    def test_unprobeable_baseline_token_is_skipped_not_a_crash(self):
        """Regression: a baseline token with an over-long path component (a JWT quoted in captured
        output) raised ENAMETOOLONG out of the evidence-declaration scan, crashing the round after its
        reviewers ran and before its blocker ledger was written."""
        jwt = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" * 12 + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.assertGreater(len(jwt), 255)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            declared = {"affected_code_path"}
            # The long token is skipped, and a resolvable path after it still substantiates the declaration.
            for text, expected in ((f"Bearer {jwt}\n", set()), (f"Bearer {jwt}\nsee app.py:1\n", declared),
                                   (f"`dir/{jwt}`\nsee app.py:1\n", declared)):
                with self.subTest(text=text[-20:]):
                    kept, _ = tc.substantiate_declared_evidence(declared, text, workspace)
                    self.assertEqual(kept, expected)

            # End to end, on the documented default path (an evidence manifest is loaded): the round completes.
            code, round_dir = self._run_single_round(
                workspace, [], baseline_text=f"```\nAuthorization: Bearer {jwt}\n```\nChanged file: app.py\n")
            self.assertEqual(code, 0)
            for artifact in ("02-open-blockers.json", "AUDIT_SUMMARY.md", "50-consensus.md"):
                self.assertTrue((round_dir / artifact).is_file(), artifact)

    def test_unusable_reading_log_path_is_not_a_crash(self):
        """Regression: a READING_LOG row path naming an unknown user's home ("~nosuchuser/x") raised
        RuntimeError, and one with a NUL byte raised ValueError, out of the READING_LOG check."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            base = ws / "b.md"
            base.write_text("baseline\n", encoding="utf-8")
            for name in ("one.py", "two.py"):   # distinct contents: identical files count once
                (ws / name).write_text(f"name = {name!r}\n", encoding="utf-8")
            lines, sha8 = tc.reading_log_fingerprint(base.read_bytes())
            # Three files minimum, so two rows take the counting path; the baseline row is relative, so
            # required-file matching walks past the bad row instead of matching exactly first.
            manifest = {"required": True, "min_files_read": 3, "required_files": [{"path": str(base)}]}
            for bad in ("~consensus_audit_no_such_user/x.py", "a\x00b.py"):
                with self.subTest(bad=repr(bad)):
                    self.assertIsNone(tc.resolve_reading_log_path(bad, ws))
                    log = ("READING_LOG:\n| # | path | total_lines | sha256_first8 | status |\n|---|---|---:|---|---|\n"
                           f"| 1 | {bad} | 1 | aaaaaaaa | READ |\n| 2 | b.md | {lines} | {sha8} | READ |\n")
                    ok, failures = tc.validate_reading_log(log, manifest, ws)
                    # The unusable row is not a file read, and the baseline row still matches its required file.
                    self.assertFalse(ok)
                    self.assertEqual([f.split(":")[0] for f in failures], ["READING_LOG_TOO_FEW_FILES"])
                    self.assertIn("files_read_or_cited=1 ", failures[0])
                    ok, failures = tc.validate_reading_log(log + "see one.py:1 and two.py:1\n", manifest, ws)
                    self.assertTrue(ok, failures)
        self.assertEqual(tc.resolve_reading_log_path("rel/x.py", None), Path("rel/x.py"))

    def test_skipped_or_nonexistent_rows_do_not_satisfy_the_reading_minimum(self):
        """Regression (release P0): a SKIPPED row, or a READ row naming no existing file, counted as
        the second file read, so a review that read only the baseline passed min_files_read=2, and
        fail_on_skipped was never consulted for rows outside required_files."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            base = ws / "baseline.md"
            base.write_text("baseline\n", encoding="utf-8")
            (ws / "other.py").write_text("x = 1\n", encoding="utf-8")
            lines, sha8 = tc.reading_log_fingerprint(base.read_bytes())
            manifest = {"required": True, "min_files_read": 2, "fail_on_skipped": True,
                        "required_files": [{"path": str(base)}]}

            def log(path, status):
                return ("READING_LOG:\n| # | path | total_lines | sha256_first8 | status |\n|---|---|---:|---|---|\n"
                        f"| 1 | {base} | {lines} | {sha8} | READ |\n| 2 | {path} | 1 | 00000000 | {status} |\n")

            for path, status, expected in (
                ("optional.md", "SKIPPED:unavailable", {"READING_LOG_TOO_FEW_FILES", "READING_LOG_SKIPPED"}),
                ("nonexistent.md", "READ", {"READING_LOG_TOO_FEW_FILES"}),
                ("baseline.md2", "SKIPPED:x", {"READING_LOG_TOO_FEW_FILES", "READING_LOG_SKIPPED"}),
                ("other.py", "SKIPPED:denied", {"READING_LOG_TOO_FEW_FILES", "READING_LOG_SKIPPED"}),
            ):
                with self.subTest(path=path, status=status):
                    ok, failures = tc.validate_reading_log(log(path, status), manifest, ws)
                    self.assertFalse(ok)
                    self.assertEqual({f.split(":")[0] for f in failures}, expected, failures)
                    self.assertTrue(all("files_read_or_cited=1 " in f for f in failures if "TOO_FEW" in f), failures)

            # An honest second row for an existing file still passes.
            ok, failures = tc.validate_reading_log(log("other.py", "READ"), manifest, ws)
            self.assertTrue(ok, failures)
            # With fail_on_skipped off a SKIPPED extra row is tolerated but still not counted as read.
            relaxed = {**manifest, "fail_on_skipped": False}
            _, failures = tc.validate_reading_log(log("optional.md", "SKIPPED:unavailable"), relaxed, ws)
            self.assertEqual([f.split(":")[0] for f in failures], ["READING_LOG_TOO_FEW_FILES"])
            ok, failures = tc.validate_reading_log(
                log("optional.md", "SKIPPED:unavailable") + "see other.py:1\n", relaxed, ws)
            self.assertTrue(ok, failures)
            # A SKIPPED required row is not a read, and its skip is reported once, as the required file's failure.
            skipped_base = log("other.py", "READ").replace(f"| {sha8} | READ |", f"| {sha8} | SKIPPED:x |", 1)
            _, failures = tc.validate_reading_log(skipped_base, manifest, ws)
            self.assertEqual([f.split(":")[0] for f in failures], ["READING_LOG_TOO_FEW_FILES", "READING_LOG_SKIPPED"])
            self.assertEqual(failures[1], f"READING_LOG_SKIPPED:{base}:SKIPPED:x")

    def test_skipped_second_row_does_not_reach_consensus_with_shipped_configs(self):
        """End to end with the five shipped configs: two approving reviews that read only the baseline
        and log a second file SKIPPED must not return CONSENSUS_READY."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            baseline = ws / "baseline.md"
            baseline.write_text(
                "# Document review\nThe optional.md source is currently unavailable.\n"
                "COVERAGE: changed/planned symbols 0 | refs 1 | formal_tests 0 | validation_commands 1 | uncovered 1\n"
            )
            lines, sha8 = tc.reading_log_fingerprint(baseline.read_bytes())
            review = (
                "EFFORT_DECLARATION: high\n\n1) Verdict: APPROVE\n2) Risk Level: LOW\n3) Blockers:\n   1. None.\n"
                "4) Suggestions:\n   1. The optional file could not be opened.\n"
                "5) Exactly 3 bullet lines prefixed by '- DECISION:'\n- DECISION: The baseline was read.\n"
                "- DECISION: The optional file was unavailable.\n- DECISION: No substantive document blocker was established.\n"
                "6) Consensus Statement: The available baseline is acceptable; the optional file was unavailable.\n"
                "7) READING_LOG:\n   | # | path | total_lines | sha256_first8 | status |\n   |---|---|---:|---|---|\n"
                f"   | 1 | {baseline} | {lines} | {sha8} | READ |\n"
                "   | 2 | optional.md | 0 | 00000000 | SKIPPED:unavailable |\n"
                "8) INJECTION_FINDINGS:\n   1. None.\n"
            )
            argv = ["consensus_audit.py", "--topic", "skip-probe", "--phase", "prework",
                    "--workspace", str(ws), "--baseline-file", str(baseline),
                    "--peer-retries", "0", "--model-preflight-retries", "0"]
            for flag, name in (("classification-file", "classification"), ("evidence-manifest-file", "evidence_manifest"),
                               ("postwork-reflow-policy", "postwork_reflow_policy"), ("role-config", "role_config"),
                               ("provider-config", "provider_config")):
                argv += ["--" + flag, str(SKILL_DIR / "config" / f"{name}.yaml")]

            def backend(prompt, model, effort, reviewer_name, timeout_sec, backend):
                return tc.CommandResult(ok=True, stdout="OK" if prompt == "Reply exactly OK" else review,
                                        stderr="", command="mock backend", returncode=0,
                                        model=model, reviewer=reviewer_name, backend=backend, agent="codex")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(tc, "resolve_agent_request", return_value=("codex", "codex")), \
                 mock.patch.object(tc, "run_agent_backend", side_effect=backend), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                status = tc.main()
            summary = (ws / ".discuss/skip-probe/round-01-prework/AUDIT_SUMMARY.md").read_text()
            self.assertNotEqual(status, 0)
            self.assertNotIn("STATUS: CONSENSUS_READY", summary)
            self.assertIn("READING_LOG_SKIPPED:optional.md", summary)

    def test_cross_review_failure_record_keeps_stdout(self):
        """The claude CLI prints errors such as usage limits on stdout; stderr alone was empty."""
        failed = tc.CommandResult(ok=False, stdout="You've hit your usage limit", stderr="",
                                  command="mock", returncode=1)
        rows = [
            ("Tim Sir", "tim-sir", "model-a", "high", "review a"),
            ("Sam Koo", "sam-koo", "model-b", "high", "review b"),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc, "run_agent_with_retries", return_value=(failed, [])):
            _, failures = tc.run_cross_review_transition(
                Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", "[x:1] issue")],
                1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
            )
            record = (Path(tmp) / "48-cross-tim-sir.md").read_text(encoding="utf-8")
        self.assertIn("CROSS_REVIEW_FAIL:tim-sir: invocation failure", failures)
        self.assertIn("You've hit your usage limit", record)
        self.assertIn("returncode=1", record)

        # A successful call with empty stdout is invalid output, not an invocation failure.
        empty = tc.CommandResult(ok=True, stdout="", stderr="", command="mock", returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tc, "run_agent_with_retries", return_value=(empty, [])):
            _, failures = tc.run_cross_review_transition(
                Path(tmp), rows, [("Tim Sir", "20-tim-sir.md", "[x:1] issue")],
                1.0, tc.DEFAULT_AGENT_BACKEND, 0, 0.0, 1.0,
            )
            record = (Path(tmp) / "48-cross-tim-sir.md").read_text(encoding="utf-8")
        self.assertTrue(failures[0].startswith("CROSS_REVIEW_FAIL:tim-sir: invalid output"))
        self.assertTrue(record.startswith("CROSS_REVIEW_EMPTY_OUTPUT"))

    def test_non_utf8_reviewer_output_is_decoded_not_raised(self):
        """Regression: undecodable reviewer bytes raised UnicodeDecodeError and aborted the round."""
        # main() in other tests leaves REVIEWER_WORKDIR at a deleted temp dir; start real processes here.
        self.enterContext(mock.patch.object(tc, "REVIEWER_WORKDIR", None))
        result = tc.run_command(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'Verdict: \\xff\\xfe APPROVE')"], "")
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("�", result.stdout)

    def test_a_raising_backend_is_a_failed_invocation_that_is_not_retried(self):
        """Regression: an exception from a backend aborted the round, and once caught it was retried
        as if transient, repeating a deterministic error and its backoff."""
        # Anything a backend raises becomes a failed invocation, never an exception.
        for error in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__), mock.patch.object(tc, "run_claude", side_effect=error):
                result = tc.run_agent_backend("p", "m", "adaptive", "Tim Sir", 1.0, tc.CLAUDE_AGENT_BACKEND)
            self.assertFalse(result.ok)
            self.assertIsNone(result.returncode)
            self.assertIn(type(error).__name__, result.stderr)
            # A raised backend is an error, not a transient failure: it is not retried.
            self.assertTrue(result.raised)
            self.assertFalse(tc.retryable_command_failure(result))
        calls = []

        def raising_claude(*_args):
            calls.append(1)
            raise RuntimeError("boom")

        with mock.patch.object(tc, "run_claude", side_effect=raising_claude):
            result, attempts = tc.run_agent_with_retries("p", "m", "adaptive", "Tim Sir", 1.0, 3, 0.0, 1.0,
                                                         tc.CLAUDE_AGENT_BACKEND)
        self.assertEqual((len(calls), len(attempts), result.ok), (1, 1, False))
        # A command that failed on its own is still retried.
        self.assertTrue(tc.retryable_command_failure(
            tc.CommandResult(ok=False, stdout="", stderr="", command="c", returncode=1)))

    def test_codex_output_file_with_invalid_utf8_is_decoded_not_raised(self):
        """The Codex backend reads its answer from a file; undecodable bytes there are replaced too."""
        def codex_writes_bytes(cmd, prompt, timeout_sec=None):
            Path(cmd[cmd.index("--output-last-message") + 1]).write_bytes(b"Verdict: \xff APPROVE")
            return tc.CommandResult(ok=True, stdout="", stderr="", command=" ".join(cmd), returncode=0)

        with mock.patch.object(tc, "run_command", side_effect=codex_writes_bytes):
            result = tc.run_codex("p", "m", "adaptive", "Tim Sir", 1.0)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "Verdict: � APPROVE")

    def test_reviewer_thread_exception_fails_closed_and_still_writes_the_round(self):
        """Regression: an exception escaping a reviewer thread crashed main() before any summary."""
        real = tc.run_peer_review

        def peer(prompt, model, effort, backend, reviewer_name, *rest):
            if reviewer_name == tc.REVIEWER_A_DISPLAY_NAME:
                raise RuntimeError("parser exploded")
            return real(prompt, model, effort, backend, reviewer_name, *rest)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            with mock.patch.object(tc, "run_peer_review", side_effect=peer):
                code, round_dir = self._run_single_round(workspace, [])
            self.assertEqual(code, 2)
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
            self.assertIn("reviewer thread raised RuntimeError", (round_dir / "20-tim-sir.md").read_text())
            trace = json.loads((round_dir / "13-peer-execution-trace.json").read_text(encoding="utf-8"))
            parser_paths = {row["reviewer_slug"]: row["parser_path"] for row in trace["rows"]}
            self.assertEqual(parser_paths[tc.REVIEWER_A_SLUG], "fail-closed-command")
            self.assertIn(parser_paths[tc.REVIEWER_A_SLUG], tc.SYNTHETIC_REVIEW_PARSER_PATHS)

    def test_runner_generated_blockers_are_not_evidence_checked_again(self):
        """Regression: a runner blocker filed under a reviewer was classed logic_flaw and spawned
        a derivative EVIDENCE_COMPLETENESS_FAIL naming the runner's own blocker."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["the race in the round claim is still open"])
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertNotEqual(code, 0)
            self.assertIn(tc.UNCITED_REVISE_BLOCKER_PREFIX, summary)
            self.assertNotIn("EVIDENCE_COMPLETENESS_FAIL", summary)
            # A reviewer's grounded blocker is still evidence-checked.
            code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong"])
            self.assertIn("EVIDENCE_COMPLETENESS_FAIL", (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8"))

    def test_prompt_states_reading_minimum_and_a_resolvable_baseline_path(self):
        """Every rule the gate enforces on a review must be in the prompt.

        Regression: READING_LOG's two-file minimum was enforced but never stated, and a
        reviewer given only the embedded baseline could cite it only by a pseudo-path, which
        the grounding gate discards, turning that reviewer's REVISE into effective approval.
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\nline two\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "Prompt Rules", "--phase", "prework", "--workspace",
                    str(workspace), "--baseline-file", str(baseline), "--dry-run",
                    "--evidence-manifest-file", str(SKILL_DIR / "config/evidence_manifest.yaml")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(tc.main(), 2)
            round_dir = workspace / ".discuss/prompt-rules/round-01-prework"
            cited = ".discuss/prompt-rules/round-01-prework/10-main-agent.md"
            for name in ("90-prompt-tim-sir.txt", "91-prompt-sam-koo.txt"):
                prompt = (round_dir / name).read_text(encoding="utf-8")
                with self.subTest(prompt=name):
                    self.assertIn("at least 2 distinct files", prompt)
                    self.assertIn(f"at most {yaml_reading_log_config()['max_files_read']} rows", prompt)
                    self.assertIn(f"[{cited}:<line>]", prompt)
                    self.assertIn("pseudo-path", prompt)
                    # A pseudo-path no longer discards a blocker whose bracket also cites a real line.
                    self.assertIn("unless\n  another citation in the same bracket resolves", prompt)
                    self.assertNotIn("does not resolve and the blocker is discarded", prompt)
                    self.assertIn("line for line", prompt)
            # The docs state the same rule as the prompt (whitespace-normalised, so a reflow does not break it).
            for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
                text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
                with self.subTest(doc=doc):
                    self.assertIn("discarded unless another citation in the same bracket resolves", text)
                    # Every statement of the discard rule allows for several citations in one bracket.
                    for stale in ("cite a `[file:line]` that does not resolve", "whose citation does not resolve"):
                        self.assertNotIn(stale, text)
            # A dry-run never reaches the acceptance step; the documented null is still there.
            appendix = json.loads((round_dir / "14-reproducibility-appendix.json").read_text(encoding="utf-8"))
            self.assertIn("acceptance_outcome", appendix["observed_outcome"])
            self.assertIsNone(appendix["observed_outcome"]["acceptance_outcome"])
            # The named path resolves, so a REVISE blocker citing the baseline gates.
            self.assertEqual(tc.verify_file_line_citation(f"[{cited}:2] claim is false", workspace),
                             (True, "verified_file_line_citation"))
            self.assertFalse(tc.verify_file_line_citation("[<embedded-baseline-doc>:2] x", workspace)[0])

    def test_delta_driven_prompt_does_not_claim_packet_lines_are_baseline_lines(self):
        """Regression: in delta-driven mode the prompt told reviewers to cite the baseline file with
        line numbers they could only read off the context packet, so a citation resolved to an
        unrelated line instead of being discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "delta", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--dry-run", "--context-assembly-mode", "delta-driven",
                    "--no-full-history-fallback-on-integrity-fail"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(tc.main(), 2)
            prompt = (workspace / ".discuss/delta/round-01-prework/90-prompt-tim-sir.txt").read_text(encoding="utf-8")
            self.assertIn("# Delta-Driven Review Packet", prompt)
            self.assertIn("The packet's line numbers are not that file's", prompt)
            self.assertNotIn("line for line", prompt)

    def test_docs_match_the_runner_on_sandbox_and_round_caps(self):
        """Regression: the protocol said Codex reviewers keep the default sandbox (the runner pins
        read-only), and SKILL.md documented a --max-rounds flag the runner does not have."""
        protocol = (SKILL_DIR / "references/consensus_protocol.md").read_text(encoding="utf-8")
        sandbox = protocol.split("## Sandbox and Provider Policy", 1)[1].split("\n## ", 1)[0]
        self.assertIn("--sandbox read-only", sandbox)
        self.assertIn("--tools Read,Grep,Glob", sandbox)
        self.assertNotIn("normal Codex sandbox", sandbox)
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("--max-rounds", skill)
        # The DECISION rule says what the runner does with more than three.
        self.assertIn("the runner keeps any extra ones", skill)
        help_text = subprocess.run([sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--help"],
                                   capture_output=True, text=True, check=True).stdout
        for flag in re.findall(r"--max-[a-z-]+", skill):
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)
        # The manual Codex command is the one the runner builds.
        manual = skill.split("Preferred Codex command shape", 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
        seen = []
        with mock.patch.object(tc, "run_command", lambda cmd, prompt, timeout_sec=None: seen.append(cmd) or
                               tc.CommandResult(ok=True, stdout="x", stderr="", command="", returncode=0)):
            tc.run_codex("p", "gpt-6.1-sol", "high", "Tim Sir", 1.0)
        runner_flags = [token for token in seen[0] if token.startswith("-")]
        manual_flags = re.findall(r"(?<!\S)(--?[a-z][a-z-]*|-)(?=\s)", manual)
        self.assertEqual(manual_flags, runner_flags)

    def test_decisions_beyond_three_are_kept_not_truncated(self):
        """Registered backlog: a fourth DECISION was silently cut, in the parser and again when a
        review was rebuilt from its parts. DECISION does not gate; fewer than three still fails."""
        decisions = "\n".join(f"- DECISION: decision {n}." for n in range(1, 6))
        raw = CLAUDE_STYLE_REVIEW.replace(
            "- DECISION: same-agent reviewer routing is preserved.\n- DECISION: no execution blocker remains.\n"
            "- DECISION: proceed after validation.", decisions)
        self.assertEqual(len(tc.extract_decisions(raw)), 5)
        self.assertTrue(tc.is_structured_review(raw))
        rebuilt, error = tc.attempt_local_structured_review(raw.replace("EFFORT_DECLARATION: high\n\n", ""), "Sam Koo")
        self.assertIsNone(error)
        assert rebuilt is not None
        self.assertEqual(len(tc.extract_decisions(rebuilt)), 5)
        self.assertFalse(tc.is_structured_review(raw.replace("- DECISION: decision 3.\n", "")
                                                 .replace("- DECISION: decision 4.\n", "")
                                                 .replace("- DECISION: decision 5.\n", "")))
        # Under a bare heading the list ends at the next contract section, not after three items.
        headed = "DECISION:\n1. a\n2. b\n6) Consensus Statement: done\n7) READING_LOG:\n"
        self.assertEqual(tc.extract_decisions(headed), ["a", "b"])
        self.assertEqual(tc.ensure_three_decisions(["a", "b", "c", "d"], "T"), ["a", "b", "c", "d"])
        self.assertEqual(len(tc.ensure_three_decisions(["a"], "T")), 3)

    def test_interrupt_terminates_reviewer_process_groups(self):
        """Registered backlog: reviewers run in their own session, so Ctrl-C never reached them and
        an interrupted runner left them running. The runner now stops them before propagating."""
        self.enterContext(mock.patch.object(tc, "REVIEWER_WORKDIR", None))
        seen = []
        real_communicate = subprocess.Popen.communicate

        def interrupted(proc, *args, **kwargs):
            seen.append(proc)
            raise KeyboardInterrupt

        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        with mock.patch.object(subprocess.Popen, "communicate", autospec=True, side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            tc.run_command(sleeper, "")
        self.assertIsNotNone(seen[0].wait(timeout=5))   # the reviewer is gone, not orphaned
        self.assertEqual(tc.LIVE_REVIEWER_PROCS, set())
        self.assertIs(subprocess.Popen.communicate, real_communicate)

        # Reviewers running in worker threads are stopped from the main thread.
        proc = subprocess.Popen(sleeper, start_new_session=True)
        with tc.LIVE_REVIEWER_PROCS_LOCK:
            tc.LIVE_REVIEWER_PROCS.add(proc)
        try:
            tc.terminate_live_reviewers()
            self.assertIsNotNone(proc.wait(timeout=5))
        finally:
            with tc.LIVE_REVIEWER_PROCS_LOCK:
                tc.LIVE_REVIEWER_PROCS.discard(proc)
            if proc.poll() is None:
                proc.kill()
        # A completed call leaves nothing registered.
        tc.run_command([sys.executable, "-c", "print('ok')"], "")
        self.assertEqual(tc.LIVE_REVIEWER_PROCS, set())

    def test_sigterm_stops_the_runner_and_its_reviewers(self):
        """End to end: SIGTERM to a live runner exits 130 and leaves no reviewer process behind."""
        import os
        import signal as sig
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bin_dir, workspace, pids = root / "bin", root / "ws", root / "pids"
            bin_dir.mkdir()
            workspace.mkdir()
            fake = bin_dir / "claude"
            fake.write_text('#!/bin/sh\ninput=$(cat)\nif [ "$input" = "Reply exactly OK" ]; then echo OK; exit 0; fi\n'
                            'echo $$ >> "$FAKE_REVIEWER_PIDS"\nexec sleep 60\n', encoding="utf-8")
            fake.chmod(0o755)
            baseline = workspace / "b.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            markers = ("CLAUDECODE", "CLAUDE_CODE", "CLAUDECODE_CWD", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR",
                       "CODEX_CI", "CODEX_HOME", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CONSENSUS_AUDIT_AGENT")
            env = {k: v for k, v in os.environ.items() if k not in markers}
            env.update({"PATH": f"{bin_dir}:{env['PATH']}", "CLAUDECODE": "1", "FAKE_REVIEWER_PIDS": str(pids)})
            runner = subprocess.Popen(
                [sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--topic", "term", "--phase", "prework",
                 "--baseline-file", str(baseline), "--workspace", str(workspace), "--peer-retries", "0",
                 "--model-preflight-retries", "0"],
                cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and (not pids.exists() or len(pids.read_text().split()) < 2):
                    time.sleep(0.1)
                reviewer_pids = [int(p) for p in pids.read_text().split()]
                self.assertEqual(len(reviewer_pids), 2)
                runner.send_signal(sig.SIGTERM)
                _, err = runner.communicate(timeout=30)
            finally:
                if runner.poll() is None:
                    runner.kill()
            self.assertEqual(runner.returncode, 130, err)
            self.assertIn("[INTERRUPTED]", err)
            for pid in reviewer_pids:
                with self.subTest(pid=pid), self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_cross_review_through_main_gates_on_every_outcome(self):
        """A live round with two grounded blockers runs cross-review, and no outcome of it passes."""
        import os
        tim_review = tc.build_structured_review(tc.REVIEWER_A_DISPLAY_NAME, "REVISE", "MEDIUM", ["[app.py:1] x is wrong"],
                                                [], ["a", "b", "c"], "fix x")
        sam_review = tc.build_structured_review(tc.REVIEWER_B_DISPLAY_NAME, "REVISE", "MEDIUM", ["[app.py:2] y is wrong"],
                                                [], ["a", "b", "c"], "fix y")
        cases = {
            "promote": (("ISSUE-1: AGREE_PROMOTE_TO_P0\nISSUE-2: AGREE_PROMOTE_TO_P0\nINJECTION_FINDINGS:\n1. None.\n"
                         "CONVERGENCE_VOTE: CONVERGED\n"), None),
            "stay": ("ISSUE-1: DEFER\nINJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: STAY\n",
                     "CROSS_REVIEW_NOT_CONVERGED"),
            "invalid": ("INJECTION_FINDINGS:\n1. None.\nCONVERGENCE_VOTE: CONVERGED\nP.S. one more thing\n",
                        "CROSS_REVIEW_FAIL"),
        }
        for name, (cross_output, expected_failure) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                (workspace / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
                baseline = workspace / "baseline.md"
                baseline.write_text("COVERAGE: x\n", encoding="utf-8")

                class FakePopen:
                    def __init__(self, cmd, cwd=None, **_kwargs):
                        self.returncode, self.pid = 0, 0

                    def communicate(self, input: str = "", timeout=None, cross_output=cross_output):
                        if input == "Reply exactly OK":
                            return "OK", ""
                        if "CROSS-REVIEW phase" in input:
                            return cross_output, ""
                        return (tim_review if input.startswith("You are Tim Sir") else sam_review), ""

                argv = ["consensus_audit.py", "--topic", "cross", "--phase", "postwork", "--workspace", str(workspace),
                        "--baseline-file", str(baseline), "--peer-retries", "0", "--model-preflight-retries", "0"]
                previous_cwd = os.getcwd()
                os.chdir(workspace)
                try:
                    with mock.patch.object(sys, "argv", argv), \
                            mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                            mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                            mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                            redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                        code = tc.main()
                finally:
                    os.chdir(previous_cwd)
                round_dir = workspace / ".discuss/cross/round-01-postwork"
                self.assertEqual(code, 2)
                for artifact in ("45-dispute-matrix.md", "48-cross-tim-sir.md", "48-cross-sam-koo.md",
                                 "49-consensus-blockers.md"):
                    self.assertTrue((round_dir / artifact).is_file(), artifact)
                handoff = (round_dir / "49-consensus-blockers.md").read_text(encoding="utf-8")
                summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
                self.assertIn("[app.py:1] x is wrong", summary)
                self.assertIn("[app.py:2] y is wrong", summary)
                if expected_failure is None:
                    self.assertIn("AGREE_PROMOTE_TO_P0", handoff)
                    self.assertNotIn("CROSS_REVIEW_", summary)
                else:
                    self.assertIn(expected_failure, handoff)
                    self.assertIn(expected_failure, summary)

    def test_terminate_without_a_readable_process_group_signals_the_process(self):
        """Regression: with no readable group nothing got SIGTERM, then the full grace period passed."""
        import time
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with mock.patch.object(tc.os, "getpgid", side_effect=ProcessLookupError):
                started = time.monotonic()
                grace_sec = 5.0
                tc.terminate_process_group(proc, grace_sec=grace_sec)
                elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, -tc.signal.SIGTERM)
            self.assertLess(elapsed, grace_sec / 2)   # it did not wait out a grace period
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_main_owns_sigterm_for_the_round_and_restores_the_callers_handler(self):
        """Regression: only the script entry point mapped SIGTERM, so a caller of main() that was
        interrupted left the round's reviewers running."""
        import signal as sig

        def callers_handler(signum, frame):
            pass

        seen = []
        previous = sig.signal(sig.SIGTERM, callers_handler)
        try:
            with mock.patch.object(tc, "_run_round", side_effect=lambda: seen.append(sig.getsignal(sig.SIGTERM)) or 0):
                self.assertEqual(tc.main(), 0)
            self.assertIs(seen[0], tc._raise_keyboard_interrupt)
            self.assertIs(sig.getsignal(sig.SIGTERM), callers_handler)

            # An interrupt inside the round stops its reviewers, then reaches the caller.
            with mock.patch.object(tc, "_run_round", side_effect=KeyboardInterrupt), \
                    mock.patch.object(tc, "terminate_live_reviewers") as terminate, \
                    self.assertRaises(KeyboardInterrupt):
                tc.main()
            terminate.assert_called_once()
            self.assertIs(sig.getsignal(sig.SIGTERM), callers_handler)

            # The script entry point maps SIGTERM before main() runs, closing the start-up window.
            entry = []
            with mock.patch.object(tc, "main", side_effect=lambda: entry.append(sig.getsignal(sig.SIGTERM)) or 0):
                self.assertEqual(tc.run_cli(), 0)
            self.assertIs(entry[0], tc._raise_keyboard_interrupt)
            with mock.patch.object(tc, "main", side_effect=KeyboardInterrupt), redirect_stderr(io.StringIO()):
                self.assertEqual(tc.run_cli(), 130)
        finally:
            sig.signal(sig.SIGTERM, previous)

    def test_unreadable_previous_ledger_blocks_until_explicitly_accepted(self):
        """Regression: a corrupt registry read as [] and, with an unparsable summary, the prior round's
        open blockers were closed without anyone re-checking them. The blocker that records the loss
        then closed by silence one round later; now only an explicit, recorded acceptance closes it."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, first = self._run_single_round(workspace, ["[app.py:1] x is wrong"])
            self.assertEqual(code, 2)
            (first / "AUDIT_SUMMARY.md").write_text("truncated", encoding="utf-8")
            (first / "02-open-blockers.json").write_text('{"rows": [', encoding="utf-8")
            code, second = self._run_single_round(workspace, [])
            summary = (second / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertEqual(code, 2)
            self.assertIn("PREVIOUS_OPEN_SOURCE: LEDGER_UNREADABLE", summary)
            self.assertIn("PREVIOUS_LEDGER_UNREADABLE", summary)
            # The lost blocker is unknown, not closed.
            self.assertNotIn("x is wrong", (second / "03-closed-blockers.json").read_text(encoding="utf-8"))

            # Reviewers approving again do not close it: it is carried and escalates on stagnation.
            code, third = self._run_single_round(workspace, [])
            summary = (third / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertEqual(code, 3)
            self.assertIn("STATUS: MODERATOR_DECISION_REQUIRED", summary)
            self.assertIn("PREVIOUS_LEDGER_UNREADABLE", summary.split("## CLOSED_BLOCKERS")[0])

            # An acceptance needs a reason.
            code, _ = self._run_single_round(workspace, [], "--accept-ledger-loss", "  ")
            self.assertEqual(code, 1)

            # An explicit acceptance closes it, and the round records who accepted it, when and why.
            code, fourth = self._run_single_round(workspace, [], "--accept-ledger-loss", "round-01 records lost; "
                                                  "findings re-checked by hand", "--acceptance-actor", "release-owner")
            summary = (fourth / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertEqual(code, 0, summary)
            self.assertIn("STATUS: CONSENSUS_READY", summary)
            accepted = summary.split("## ACCEPTED_BLOCKERS", 1)[1].split("\n## ", 1)[0]
            self.assertIn("actor=release-owner", accepted)
            self.assertIn("reason=round-01 records lost; findings re-checked by hand", accepted)
            self.assertIn("PREVIOUS_LEDGER_UNREADABLE", accepted)
            record = json.loads((fourth / "61-accepted-risks.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual([row["actor"] for row in record], ["release-owner"])
            transitions = json.loads((fourth / "12-blocker-transitions.json").read_text(encoding="utf-8"))["rows"]
            self.assertIn("accepted", {row["transition"] for row in transitions})
        self.assertIsNone(tc.read_blocker_registry(None))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "02-open-blockers.json"
            for text, expected in (('{"rows": []}', []), ("[]", []), ('{"rows": {}}', None), ("{", None),
                                   ('{"rows": [{"text": "a"}]}', [{"text": "a"}]),
                                   # A damaged row makes the ledger unreadable, never a smaller ledger.
                                   ('{"rows": [3]}', None), ('{"rows": [{"text": "a"}, 3]}', None),
                                   ('{"rows": [{}]}', None), ('{"rows": [{"text": " "}]}', None),
                                   ('{"rows": [{"text": 3}]}', None), ('[null]', None),
                                   ("{}", None), ("3", None), ("null", None)):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text):
                    self.assertEqual(tc.read_blocker_registry(path), expected)

    def _lose_ledger(self, workspace, registry='{"rows": ['):
        """A round with an open blocker whose summary and registry are then both unreadable."""
        (workspace / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        _, first = self._run_single_round(workspace, ["[app.py:1] x is wrong"])
        (first / "AUDIT_SUMMARY.md").write_text("truncated", encoding="utf-8")
        (first / "02-open-blockers.json").write_text(registry, encoding="utf-8")

    def test_registry_with_damaged_rows_is_unreadable_not_empty(self):
        """Regression: rows that were not objects with text were dropped, so a registry damaged to
        valid JSON such as {"rows": [3]} read as an empty ledger and an approving round closed the
        prior open blocker. It must block as an unreadable ledger instead."""
        for registry in ('{"rows": [3]}', '{"rows": [{}]}', '{"rows": [{"text": ""}]}'):
            with self.subTest(registry=registry), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                self._lose_ledger(workspace, registry)
                code, round_dir = self._run_single_round(workspace, [])
                self.assertEqual(code, 2)
                open_texts = [row["text"] for row in json.loads(
                    (round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]]
                self.assertTrue(any(tc.is_acceptance_only_blocker(text) for text in open_texts), open_texts)
                self.assertIn("PREVIOUS_OPEN_SOURCE: LEDGER_UNREADABLE",
                              (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8"))

    def test_ledger_loss_acceptance_closes_only_that_blocker_and_records_it_in_the_same_round(self):
        """Accepting in the round that detects the loss closes that blocker alone, and the closed list,
        the transitions, the acceptance file, the summary and the appendix all record it."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._lose_ledger(workspace)
            other = "[app.py:2] y is wrong"
            code, round_dir = self._run_single_round(workspace, [other], "--accept-ledger-loss", "re-checked by hand")
            self.assertEqual(code, 2)   # the unrelated reviewer blocker still gates
            open_texts = [row["text"] for row in json.loads(
                (round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]]
            # The unrelated blocker and its own evidence record stay open; only the loss was accepted.
            self.assertEqual(open_texts, [other, (f"EVIDENCE_COMPLETENESS_FAIL: missing_evidence:"
                                                  f"affected_code_path,reasoning_trace for {tc.short_blocker_id(other)}")])
            self.assertFalse(any(tc.is_acceptance_only_blocker(text) for text in open_texts))
            closed = json.loads((round_dir / "03-closed-blockers.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual([(tc.is_acceptance_only_blocker(r["text"]), r.get("accepted")) for r in closed],
                             [(True, True)])
            transitions = json.loads((round_dir / "12-blocker-transitions.json").read_text(encoding="utf-8"))["rows"]
            # The accepted loss, plus the unrelated blocker and its evidence record, both new.
            self.assertEqual(sorted(row["transition"] for row in transitions), ["accepted", "new", "new"])
            accepted = json.loads((round_dir / "61-accepted-risks.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual(len(accepted), 1)
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("reason=re-checked by hand", summary.split("## ACCEPTED_BLOCKERS", 1)[1])
            # CLOSED_BLOCKERS lists blockers in canonical (lower-case) form, like every other closure.
            self.assertIn("previous_ledger_unreadable", summary.split("## CLOSED_BLOCKERS", 1)[1].split("\n## ")[0])
            invocation = json.loads((round_dir / "14-reproducibility-appendix.json").read_text())["invocation"]
            self.assertEqual(invocation["accept_ledger_loss"], "re-checked by hand")
            outcome = json.loads((round_dir / "14-reproducibility-appendix.json").read_text())["observed_outcome"]
            self.assertEqual(outcome["acceptance_outcome"], "applied")
            self.assertIn("acceptance_actor", invocation)

    def test_ledger_loss_acceptance_is_refused_in_a_round_that_did_not_review(self):
        """An acceptance in a round whose reviewer failed would record a decision nobody reviewed."""
        real = tc.run_peer_review

        def peer(prompt, model, effort, backend, reviewer_name, *rest):
            if reviewer_name == tc.REVIEWER_B_DISPLAY_NAME:
                raise RuntimeError("usage limit")
            return real(prompt, model, effort, backend, reviewer_name, *rest)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            self._lose_ledger(workspace)
            err = io.StringIO()
            with mock.patch.object(tc, "run_peer_review", side_effect=peer):
                code, round_dir = self._run_single_round(workspace, [], "--accept-ledger-loss", "fine", stderr=err)
            self.assertNotEqual(code, 0)
            self.assertIn("--accept-ledger-loss refused", err.getvalue())
            self.assertFalse((round_dir / "61-accepted-risks.json").exists())
            summary = (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("PREVIOUS_LEDGER_UNREADABLE", summary.split("## CLOSED_BLOCKERS")[0])
            # The refusal is in the round's record, not only on stderr: it reads differently from a
            # round where nobody tried to accept.
            accepted = summary.split("## ACCEPTED_BLOCKERS\n", 1)[1].split("\n## ", 1)[0]
            self.assertTrue(accepted.startswith("- (refused) actor="), accepted)
            self.assertIn("reason=fine", accepted)
            self.assertIn("why=not every active reviewer produced a real review", accepted)
            outcome = json.loads((round_dir / "14-reproducibility-appendix.json").read_text())["observed_outcome"]
            self.assertEqual(outcome["acceptance_outcome"], "refused")

    def test_ledger_loss_acceptance_without_a_loss_is_inert(self):
        """The flag with nothing to accept warns and changes nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            plain_code, plain_dir = self._run_single_round(workspace, [])
            err = io.StringIO()
            code, round_dir = self._run_single_round(workspace, [], "--accept-ledger-loss", "nothing lost", stderr=err)
            self.assertEqual((plain_code, code), (0, 0))
            self.assertIn("has no PREVIOUS_LEDGER_UNREADABLE blocker to accept", err.getvalue())
            self.assertFalse((round_dir / "61-accepted-risks.json").exists())
            for directory, expected in ((plain_dir, None), (round_dir, "nothing_to_accept")):
                self.assertIn("## ACCEPTED_BLOCKERS\n- (none)", (directory / "AUDIT_SUMMARY.md").read_text())
                outcome = json.loads((directory / "14-reproducibility-appendix.json").read_text())["observed_outcome"]
                self.assertEqual(outcome["acceptance_outcome"], expected)
        self.assertTrue(tc.is_acceptance_only_blocker("PREVIOUS_LEDGER_UNREADABLE: round-01 lost"))   # raw text
        self.assertFalse(tc.is_acceptance_only_blocker("[app.py:1] PREVIOUS_LEDGER_UNREADABLE: quoted"))

    def test_non_utf8_baseline_is_an_error_before_any_round_is_claimed(self):
        """Regression: a non-UTF-8 baseline raised UnicodeDecodeError after the round directory was
        claimed, ending in a traceback and an empty round."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "b.md"
            baseline.write_bytes(b"COVERAGE: x\n\xff\xfe latin-1 \xe9\n")
            argv = ["consensus_audit.py", "--topic", "t", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--dry-run"]
            err = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                    redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(tc.main(), 1)
            self.assertIn("is not readable UTF-8 text", err.getvalue())
            self.assertFalse((workspace / ".discuss").exists())

    def test_a_blocker_raised_by_two_reviewers_counts_for_both(self):
        """Regression: identical grounded blockers were counted only for the first reviewer, so the
        second was reported as effectively approving although it raised the same REVISE."""
        import os
        # Tim Sir also repeats the blocker in his own list: it still counts once for him.
        review = lambda name: tc.build_structured_review(
            name, "REVISE", "MEDIUM",
            ["[app.py:1] x is wrong"] * (2 if name == tc.REVIEWER_A_DISPLAY_NAME else 1),
            [], ["a", "b", "c"], "fix x")

        class FakePopen:
            def __init__(self, cmd, cwd=None, **_kwargs):
                self.returncode, self.pid = 0, 0

            def communicate(self, input: str = "", timeout=None):
                if input == "Reply exactly OK":
                    return "OK", ""
                name = tc.REVIEWER_A_DISPLAY_NAME if input.startswith("You are Tim Sir") else tc.REVIEWER_B_DISPLAY_NAME
                return review(name), ""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            baseline = workspace / "baseline.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "dup", "--phase", "postwork", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--peer-retries", "0", "--model-preflight-retries", "0"]
            previous_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                        mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                        mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(tc.main(), 2)
            finally:
                os.chdir(previous_cwd)
            round_dir = workspace / ".discuss/dup/round-01-postwork"
            diff = (round_dir / "40-diff.md").read_text(encoding="utf-8")
            for name in (tc.REVIEWER_A_DISPLAY_NAME, tc.REVIEWER_B_DISPLAY_NAME):
                with self.subTest(reviewer=name):
                    self.assertIn(f"- {name} Gated Blockers: 1", diff)
                    self.assertIn(f"- {name} Effective Approval: NO", diff)
            # The open set still holds the blocker once.
            rows = json.loads((round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual([row["text"] for row in rows], ["[app.py:1] x is wrong"])

    def test_codex_output_file_is_removed_when_the_call_is_interrupted(self):
        """Regression: the temp output file was removed only after run_command returned normally."""
        seen = []

        def interrupted(cmd, prompt, timeout_sec=None):
            seen.append(Path(cmd[cmd.index("--output-last-message") + 1]))
            self.assertTrue(seen[0].exists())
            raise KeyboardInterrupt

        with mock.patch.object(tc, "run_command", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            tc.run_codex("p", "m", "adaptive", "Tim Sir", 1.0)
        self.assertFalse(seen[0].exists())

    def test_reformat_never_supplies_a_missing_section(self):
        """Regression: the reformat prompt told the model to write "1. None." under INJECTION_FINDINGS and an
        <embedded-review> READING_LOG row when the original lacked them, so a review missing either section
        passed the gate once reformatted."""
        no_injection = CLAUDE_STYLE_REVIEW.replace("## INJECTION_FINDINGS\n1. None.\n", "")
        no_reading = CLAUDE_STYLE_REVIEW.replace(
            "| # | path | total_lines | sha256_first8 | status |\n|---|---|---:|---|---|\n"
            "| 1 | <embedded-review> | 1 | 00000000 | READ |\n", "")
        self.assertEqual(tc.sections_missing_from_review(CLAUDE_STYLE_REVIEW), [])
        self.assertEqual(tc.sections_missing_from_review(no_injection), ["INJECTION_FINDINGS"])
        self.assertEqual(tc.sections_missing_from_review(no_reading), ["READING_LOG"])
        empty_heading = CLAUDE_STYLE_REVIEW.replace("## INJECTION_FINDINGS\n1. None.\n", "## INJECTION_FINDINGS\n")
        self.assertEqual(tc.sections_missing_from_review(empty_heading), ["INJECTION_FINDINGS"])
        self.assertEqual(tc.sections_missing_from_review(
            CLAUDE_STYLE_REVIEW.replace("## INJECTION_FINDINGS", "**INJECTION_FINDINGS:**")), [])

        for backend in (tc.CLAUDE_AGENT_BACKEND, tc.DEFAULT_AGENT_BACKEND):
            for raw, missing in ((no_injection, "INJECTION_FINDINGS"), (no_reading, "READING_LOG")):
                with self.subTest(backend=backend, missing=missing), \
                        mock.patch.object(tc, "run_agent_backend") as reformat:
                    body, path = tc.normalize_agent_review(raw, "m", "adaptive", "Tim Sir", 1.0,
                                                           backend == tc.CLAUDE_AGENT_BACKEND, backend)
                    reformat.assert_not_called()
                    self.assertEqual(path, "fail-closed")
                    self.assertIn(f"original_review_missing:{missing}", body)
        # A section that is present but badly written is still left to the reformat.
        bulleted = CLAUDE_STYLE_REVIEW.replace("## INJECTION_FINDINGS\n1. None.", "## INJECTION_FINDINGS\n- None")
        fixed = tc.CommandResult(ok=True, stdout=CLAUDE_STYLE_REVIEW, stderr="", command="m", returncode=0)
        with mock.patch.object(tc, "run_agent_backend", return_value=fixed) as reformat:
            body, path = tc.normalize_agent_review(bulleted, "m", "adaptive", "Tim Sir", 1.0, True,
                                                   tc.CLAUDE_AGENT_BACKEND)
        reformat.assert_called_once()
        self.assertEqual(path, f"{tc.CLAUDE_AGENT_BACKEND}-reformat")

        prompt = tc.build_reformat_prompt("raw", "Tim Sir")
        self.assertNotIn("<embedded-review>", prompt)
        self.assertNotIn("did not report prompt-injection findings", prompt)

        # End to end: a reformatter that would add "1. None." is never asked, and the round does not pass.
        import os
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            baseline = workspace / "baseline.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            asked = []

            class FakePopen:
                def __init__(self, cmd, cwd=None, **_kwargs):
                    self.returncode, self.pid = 0, 0

                def communicate(self, input: str = "", timeout=None):
                    if input == "Reply exactly OK":
                        return "OK", ""
                    if input.startswith("Reformat the following review"):
                        asked.append(input)
                        return CLAUDE_STYLE_REVIEW, ""
                    return (no_injection if input.startswith("You are Tim Sir") else CLAUDE_STYLE_REVIEW), ""

            argv = ["consensus_audit.py", "--topic", "noinj", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--peer-retries", "0", "--model-preflight-retries", "0"]
            previous_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                        mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                        mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    code = tc.main()
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(code, 2)
            self.assertEqual(asked, [])
            summary = (workspace / ".discuss/noinj/round-01-prework/AUDIT_SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("STATUS: NEEDS_ANOTHER_ROUND", summary)
            self.assertIn("original_review_missing:injection_findings", summary.lower())
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            with self.subTest(doc=doc):
                self.assertIn("never supply a missing one", (ROOT / doc).read_text(encoding="utf-8"))

    def test_docs_tell_authors_not_to_address_reviewers_in_the_baseline(self):
        """Codex release-audit P2: imperatives in a baseline were reported as injections and blocked rounds."""
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
            with self.subTest(doc=doc):
                self.assertIn("Write the baseline as a description of the work, not as instructions to the reviewers",
                              text)
                self.assertIn("## Exit Condition", text)

    def test_inheritance_reads_only_the_summary_or_the_registry(self):
        """The raw reviewer-file fallback was unreachable (only rounds with a registry are inherited from)."""
        self.assertFalse(hasattr(tc, "read_round_raw_open_blockers"))
        self.assertFalse(hasattr(tc, "parse_blocker_registry_rows"))  # no callers; it conflated None and []
        self.assertNotIn("RAW_FALLBACK", (SKILL_DIR / "scripts/consensus_audit.py").read_text(encoding="utf-8"))

    def test_rebuilt_review_keeps_the_raw_output_and_the_declared_effort(self):
        """Release-audit P1: a review rebuilt by the local parser, reformatted, or replaced by a fail-closed
        body was persisted only in its replaced form, and the rebuilt one always claimed effort "high"."""
        import os
        raw_tim = "Here is my review.\n" + CLAUDE_STYLE_REVIEW.replace("EFFORT_DECLARATION: high", "EFFORT_DECLARATION: medium")
        sam = tc.build_structured_review(tc.REVIEWER_B_DISPLAY_NAME, "APPROVE", "LOW", [], [], ["a", "b", "c"], "ok")

        def run(sam_fails):
            class FakePopen:
                def __init__(self, cmd, cwd=None, **_kwargs):
                    self.returncode, self.pid = 0, 0

                def communicate(self, input: str = "", timeout=None):
                    if input == "Reply exactly OK":
                        return "OK", ""
                    if input.startswith("You are Tim Sir"):
                        return raw_tim, ""
                    if sam_fails:
                        self.returncode = 1
                        return "You've hit your usage limit", ""
                    return sam, ""

            tmp = self.enterContext(tempfile.TemporaryDirectory())
            workspace = Path(tmp).resolve()
            baseline = workspace / "baseline.md"
            baseline.write_text("COVERAGE: x\n", encoding="utf-8")
            argv = ["consensus_audit.py", "--topic", "raw", "--phase", "prework", "--workspace", str(workspace),
                    "--baseline-file", str(baseline), "--peer-retries", "0", "--model-preflight-retries", "0"]
            previous_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(tc, "resolve_agent_request", return_value=("claude", "claude")), \
                        mock.patch.object(tc.subprocess, "Popen", FakePopen), \
                        mock.patch.object(tc, "REVIEWER_WORKDIR", None), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    code = tc.main()
            finally:
                os.chdir(previous_cwd)
            return code, workspace / ".discuss/raw/round-01-prework"

        code, round_dir = run(sam_fails=False)
        self.assertEqual(code, 0)
        trace = json.loads((round_dir / "13-peer-execution-trace.json").read_text(encoding="utf-8"))
        self.assertEqual({r["reviewer_slug"]: r["parser_path"] for r in trace["rows"]},
                         {tc.REVIEWER_A_SLUG: "local-structured", tc.REVIEWER_B_SLUG: "live-structured"})
        # The rebuilt review carries the reviewer's own effort, and what the reviewer printed is kept beside it.
        self.assertTrue((round_dir / "20-tim-sir.md").read_text(encoding="utf-8").startswith("EFFORT_DECLARATION: medium\n"))
        self.assertEqual((round_dir / "20-tim-sir.raw.md").read_text(encoding="utf-8"), raw_tim)
        # A review persisted as printed has no raw copy.
        self.assertFalse((round_dir / "30-sam-koo.raw.md").exists())

        # A failed call's output (the claude CLI reports usage limits on stdout) is kept too.
        code, round_dir = run(sam_fails=True)
        self.assertEqual(code, 2)
        self.assertEqual((round_dir / "30-sam-koo.raw.md").read_text(encoding="utf-8"), "You've hit your usage limit")

        # Only the reviewer's own declaration line counts, not an effort word elsewhere near the top.
        self.assertIsNone(tc.declared_effort("1) Verdict: REVISE\n2) Risk Level: HIGH\n"))
        self.assertEqual(tc.declared_effort("**EFFORT_DECLARATION:** xhigh\n"), "xhigh")
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            with self.subTest(doc=doc):
                self.assertIn("20-tim-sir.raw.md", (ROOT / doc).read_text(encoding="utf-8"))

    def test_docs_say_the_runner_inherits_from_the_ledger_not_the_cross_review_record(self):
        """Release-audit P1: SKILL.md called 49-consensus-blockers.md the next round's P0 list; the runner
        never reads it, inherits from 02-open-blockers.json, and cross-review never closes a blocker."""
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
            with self.subTest(doc=doc):
                self.assertNotIn("true P0 list", text)
                self.assertNotIn("revise against `49-consensus-blockers.md` if present", text)
                self.assertIn("from `02-open-blockers.json`, not from `49-consensus-blockers.md`", text)

    def test_docs_describe_path_based_agent_detection_as_the_runner_does_it(self):
        """Release-audit P1: the docs said detection checks for ~/.codex/skills or ~/.claude/skills; the runner
        accepts any path with both components, and falls back to the current directory."""
        elsewhere = Path("/srv/build/.claude/tools/skills/consensus-audit/scripts/consensus_audit.py")
        self.assertEqual(tc.detect_agent_context(elsewhere, Path("/"), {}), "claude")
        # The staging copy of the install block is detected the same way.
        staged = Path.home() / ".codex/.consensus-audit-staging.1/skills/consensus-audit/scripts/consensus_audit.py"
        self.assertEqual(tc.detect_agent_context(staged, Path("/"), {"CLAUDECODE": "1"}), "codex")
        # A source checkout run from inside such a directory takes the agent from the current directory.
        self.assertEqual(tc.detect_agent_context(ROOT / "skill/scripts/consensus_audit.py",
                                                 Path("/srv/.codex/skills/x"), {"CLAUDECODE": "1"}), "codex")
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md", "skill/00-READ-FIRST.md"):
            text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
            with self.subTest(doc=doc):
                self.assertNotIn("the runner is not under `~/.codex/skills` or `~/.claude/skills`", text)
                self.assertIn("or failing that the current directory, contains both a `.codex` (or `.claude`) "
                              "directory and a `skills` directory", text)

    def test_local_rebuild_never_fills_in_a_missing_section(self):
        """Release-audit P1: the Claude-backend rebuild padded DECISION to three, set a missing Risk Level to
        MEDIUM, wrote a default consensus statement and an empty Suggestions section, so a review lacking them
        passed the structure check there while the same review on Codex went to the reformat call."""
        raw = CLAUDE_STYLE_REVIEW.replace("EFFORT_DECLARATION: high\n\n", "")   # not live-structured as printed
        self.assertIsNone(tc.attempt_local_structured_review(raw, "Sam Koo")[1])
        for name, removed, error in (
                ("risk", "## Risk Level\nMEDIUM\n\n", "missing_or_invalid_risk_level"),
                ("suggestions", "## Suggestions\n1. Add a focused regression test for the backend path.\n\n",
                 "missing_suggestions_section"),
                ("decision", "- DECISION: proceed after validation.\n", "fewer_than_three_decisions"),
                ("consensus", "## Consensus Statement\nNo grounded execution blocker was found.\n\n",
                 "missing_consensus_statement")):
            incomplete = raw.replace(removed, "")
            self.assertNotEqual(incomplete, raw, name)
            with self.subTest(missing=name):
                self.assertEqual(tc.attempt_local_structured_review(incomplete, "Sam Koo"), (None, error))
                # It takes the reformat path, as on Codex; a reformat that does not fix it fails closed.
                unfixed = tc.CommandResult(ok=True, stdout=incomplete, stderr="", command="m", returncode=0)
                with mock.patch.object(tc, "run_agent_backend", return_value=unfixed) as reformat:
                    body, path = tc.normalize_agent_review(incomplete, "m", "adaptive", "Sam Koo", 1.0, True,
                                                           tc.CLAUDE_AGENT_BACKEND)
                reformat.assert_called_once()
                self.assertEqual(path, "fail-closed")
                self.assertIn(error, body)
        self.assertIn("fewer than three fail the structure check", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    def test_stale_stay_is_emitted_only_with_the_orchestrator_count(self):
        """Release-audit P1: the docs described STALE_STAY as a status the runner reaches by itself; it is
        driven only by --cross-review-stay-count, which no document told the orchestrator to pass."""
        for extra, expected in (((), 2), (("--cross-review-stay-count", "2"), 2),
                                (("--cross-review-stay-count", "3"), 4)):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
                code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong"], *extra)
                self.assertEqual(code, expected)
                if expected == 4:
                    self.assertIn("STATUS: STALE_STAY", (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8"))
        # With nothing open the count does not matter.
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run_single_round(Path(tmp).resolve(), [], "--cross-review-stay-count", "9")
            self.assertEqual(code, 0)
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md", "README.md", "README.zh-CN.md"):
            with self.subTest(doc=doc):
                self.assertIn("--cross-review-stay-count", (ROOT / doc).read_text(encoding="utf-8"))
        for doc in ("skill/SKILL.md", "skill/references/consensus_protocol.md"):
            text = re.sub(r"\s+", " ", (ROOT / doc).read_text(encoding="utf-8"))
            with self.subTest(doc=doc):
                self.assertIn("The runner does not count STAY votes across rounds", text)
                self.assertIn("`CROSS_REVIEW_NOT_CONVERGED:<reviewer>:STAY` line with the same `issues=` digest", text)
        help_text = subprocess.run([sys.executable, str(SKILL_DIR / "scripts/consensus_audit.py"), "--help"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertIn("does not count these itself", re.sub(r"\s+", " ", help_text))

    def test_cross_review_is_skipped_when_a_reviewer_call_failed_as_documented(self):
        """Release-audit P1: the docs gave cross-review trigger and skip conditions the runner does not use."""
        real = tc.run_peer_review

        def peer(prompt, model, effort, backend, reviewer_name, *rest):
            if reviewer_name == tc.REVIEWER_B_DISPLAY_NAME:
                raise RuntimeError("usage limit")
            return real(prompt, model, effort, backend, reviewer_name, *rest)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            with mock.patch.object(tc, "run_peer_review", side_effect=peer), \
                    mock.patch.object(tc, "run_agent_with_retries", wraps=tc.run_agent_with_retries) as calls:
                code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong", "[app.py:2] y is wrong"])
            self.assertEqual(code, 2)
            self.assertIn("Cross-review unavailable", (round_dir / "45-dispute-matrix.md").read_text(encoding="utf-8"))
            self.assertFalse(list(round_dir.glob("48-cross-*.md")))
            self.assertFalse([c for c in calls.call_args_list if "CROSS-REVIEW phase" in c.args[0]])
        # With the failed call's blocker the only one open, neither file is written.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            with mock.patch.object(tc, "run_peer_review", side_effect=peer):
                code, round_dir = self._run_single_round(workspace, [])
            self.assertEqual(code, 2)
            self.assertFalse((round_dir / "45-dispute-matrix.md").exists())
            self.assertFalse((round_dir / "49-consensus-blockers.md").exists())
        # Re-check P1: the count is taken before evidence-completeness, preflight and inherited blockers join
        # the open set, so one reviewer blocker plus its evidence record is two open blockers and no cross-review.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, ["[app.py:1] x is wrong"])
            self.assertEqual(code, 2)
            open_texts = [row["text"] for row in json.loads((round_dir / "02-open-blockers.json").read_text())["rows"]]
            self.assertEqual(len(open_texts), 2)
            self.assertTrue(open_texts[1].startswith("EVIDENCE_COMPLETENESS_FAIL"))
            self.assertFalse((round_dir / "45-dispute-matrix.md").exists())
        skill = re.sub(r"\s+", " ", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        protocol = re.sub(r"\s+", " ", (SKILL_DIR / "references/consensus_protocol.md").read_text(encoding="utf-8"))
        self.assertIn("an active reviewer's call failed, or reviewers were not run because model preflight", skill)
        self.assertIn("Cross-review is skipped when an active reviewer's call failed", protocol)
        for text in (skill, protocol):
            self.assertIn("reviewer blockers that passed the grounding gate, including runner-generated ones filed "
                          "under a reviewer", text)
            self.assertIn("inherited blockers are added after this decision", text)
            self.assertNotIn("count is the whole open set", text)
            self.assertIn("when at least two blockers are open", text)
            self.assertNotIn("every blocker in the round's open set", text)
        self.assertNotIn("active-reviewer surfaces", protocol)

    def test_summary_with_an_empty_open_blockers_section_does_not_parse(self):
        """Codex release-audit P1: an empty OPEN_BLOCKERS section is a truncated summary, not "none open"."""
        head = f"SCHEMA_VERSION: {tc.SUMMARY_SCHEMA_VERSION}\n\n## OPEN_BLOCKERS\n"
        self.assertIsNone(tc.parse_open_blockers_from_audit_summary_text(head))
        self.assertIsNone(tc.parse_open_blockers_from_audit_summary_text(head + "\n## CLOSED_BLOCKERS\n- (none)\n"))
        self.assertEqual(tc.parse_open_blockers_from_audit_summary_text(head + "- (none)\n"), set())

    def test_key_assumptions_keep_wrapped_lines(self):
        """Release-audit P1: an assumption wrapped over two lines was cut mid-sentence in AUDIT_SUMMARY.md."""
        baseline = ("## Assumptions\n\n- The reviewer CLI is `claude` 2.1.289, which accepts `--tools`,\n"
                    "  `--effort` and `--no-session-persistence`.\n1. A second one\non two lines.\n\n"
                    "Prose after the list is not an assumption.\n- A third.\n\n## Gaps\n- not an assumption\n")
        self.assertEqual(tc.extract_key_assumptions(baseline), [
            "The reviewer CLI is `claude` 2.1.289, which accepts `--tools`, `--effort` and `--no-session-persistence`.",
            "A second one on two lines.",
            "A third.",
        ])
        self.assertEqual(len(tc.extract_key_assumptions("## Assumptions\n" + "- a\n" * 9)), 5)

    def test_an_unsubstantiated_evidence_declaration_blocks_the_round_as_documented(self):
        """Release-audit P1: SKILL.md said such a declaration is only "dropped and recorded"; it gates the round."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            code, round_dir = self._run_single_round(workspace, [], baseline_text="EVIDENCE_DECLARED: test_output\n")
            self.assertEqual(code, 2)
            rows = json.loads((round_dir / "02-open-blockers.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual([row["text"].split(" (")[0] for row in rows],
                             ["EVIDENCE_COMPLETENESS_FAIL: UNSUBSTANTIATED_EVIDENCE_DECLARATION:test_output"])
        skill = re.sub(r"\s+", " ", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        self.assertNotIn("dropped and recorded", skill)
        self.assertIn("holds the round open even when every reviewer approves", skill)

    def test_only_a_declaration_that_starts_its_line_declares_evidence(self):
        """Recheck P1 (ab80223): the marker was found anywhere, so a baseline quoting a test that passes
        `baseline_text="EVIDENCE_DECLARED: test_output\\n"` declared `test_output\\n")` and its round
        stayed open on an unsubstantiated declaration although both reviewers approved."""
        for line, expected in (("EVIDENCE_DECLARED: affected_code_path, test_output", "affected_code_path, test_output"),
                               ("- EVIDENCE_DECLARED: test_file", "test_file"),
                               ("**EVIDENCE_DECLARED:** test_file", "test_file"),
                               ("`EVIDENCE_DECLARED: test_file`", "test_file"),
                               ("EVIDENCE_DECLARED：test_file", "test_file")):
            with self.subTest(line=line):
                match = tc.EVIDENCE_DECLARATION_LINE_RE.match(line)
                assert match is not None
                self.assertEqual(match.group(1), expected)
        for line in ('    code = run(baseline_text="EVIDENCE_DECLARED: test_output\\n")',
                     "+EVIDENCE_DECLARED: test_output", " EVIDENCE_DECLARED: test_output",
                     "@@ -139,11 +139,13 @@ EVIDENCE_DECLARED: test_output", "See EVIDENCE_DECLARED: below"):
            with self.subTest(rejected=line):
                self.assertIsNone(tc.EVIDENCE_DECLARATION_LINE_RE.match(line))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
            quoted = ("```diff\n+    code = run(baseline_text=\"EVIDENCE_DECLARED: test_output\\n\")\n"
                      " EVIDENCE_DECLARED: test_output\n```\n")
            code, round_dir = self._run_single_round(workspace, [], baseline_text=quoted)
            self.assertEqual(code, 0, (round_dir / "AUDIT_SUMMARY.md").read_text(encoding="utf-8"))
        self.assertIn("starts a line", re.sub(r"\s+", " ", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
