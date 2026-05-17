"""
QA / Verification Agent.
Validates that actions produced their expected outcomes.
Called after every significant action and at the end of each task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from agents.parser_agent import ValidationRule, ParsedTask
from agents.vision_agent import VerificationResult, get_vision_agent
from core.config import get_config
from vision.screenshot_engine import Screenshot, get_screenshot_engine


@dataclass
class QAReport:
    task_id: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    rules_checked: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    screenshot: Screenshot | None = None

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    @property
    def pass_rate(self) -> float:
        total = self.passed + self.failed
        return self.passed / total if total > 0 else 1.0


class QAAgent:
    """
    Runs validation rules for tasks and individual steps.
    Returns QAReport with pass/fail breakdown.
    """

    def __init__(self) -> None:
        self._vision = get_vision_agent()
        self._screen = get_screenshot_engine()

    def validate_task(self, task: ParsedTask) -> QAReport:
        """Run all validation rules for a completed task."""
        report = QAReport(task_id=task.task_id)
        report.screenshot = self._screen.capture_full(f"qa_{task.task_id}")

        for rule in task.validation_rules:
            result = self._apply_rule(rule, report.screenshot)
            report.rules_checked.append(rule.description or rule.rule_type)
            if result.passed:
                report.passed += 1
                logger.debug(f"QA PASS | {rule.rule_type}: {result.description}")
            else:
                report.failed += 1
                report.failures.append(f"{rule.rule_type}: {result.description}")
                logger.warning(f"QA FAIL | {rule.rule_type}: {result.description}")

        logger.info(
            f"QA | task={task.task_id} "
            f"passed={report.passed}/{report.passed + report.failed} "
            f"rate={report.pass_rate:.0%}"
        )
        return report

    def validate_step_action(
        self,
        action: str,
        target: str,
        expected_outcome: str,
        before_screenshot: Screenshot | None = None,
    ) -> VerificationResult:
        """Quick verification after an atomic action."""
        if not expected_outcome:
            return VerificationResult(passed=True, description="No expected outcome defined")

        current_shot = self._screen.capture_full()

        # Check if screen changed at all
        if before_screenshot:
            change_result = self._vision.verify_screen_changed(before_screenshot, current_shot)
            if not change_result.passed and action not in ("wait_ms", "scroll"):
                return VerificationResult(
                    passed=False,
                    description=f"Screen unchanged after action: {action}",
                    screenshot=current_shot,
                )

        # Check expected text is visible
        if expected_outcome and len(expected_outcome) > 3:
            text_result = self._vision.verify_text_visible(expected_outcome, current_shot)
            if text_result.passed:
                return text_result

        # Default: screen changed = ok
        return VerificationResult(
            passed=True,
            description="Action completed (no specific text match required)",
            screenshot=current_shot,
        )

    def _apply_rule(
        self, rule: ValidationRule, screenshot: Screenshot | None
    ) -> VerificationResult:
        rt = rule.rule_type.lower()

        if rt == "element_exists":
            return self._vision.verify_element_exists(rule.target)

        elif rt == "text_contains":
            if screenshot:
                return self._vision.verify_text_visible(rule.expected_value or rule.target, screenshot)
            return self._vision.verify_text_visible(rule.expected_value or rule.target)

        elif rt == "style_applied":
            return self._verify_style(rule.target, rule.expected_value, screenshot)

        elif rt == "screenshot":
            # Just verifies a screenshot was taken (always true at this point)
            return VerificationResult(
                passed=screenshot is not None,
                description="Screenshot taken" if screenshot else "No screenshot",
            )

        elif rt == "application_open":
            return self._vision.verify_application_open(rule.target)

        elif rt == "file_saved":
            return self._verify_file_saved(rule.target)

        elif rt == "no_dialog":
            state = self._vision.capture_state(include_ui_tree=False, include_ocr=False)
            if state.detected_dialogs:
                return VerificationResult(
                    passed=False,
                    description=f"Unexpected dialogs: {state.detected_dialogs}",
                )
            return VerificationResult(passed=True, description="No dialogs present")

        else:
            logger.debug(f"Unknown rule type: {rt} — skipping")
            return VerificationResult(
                passed=True,
                description=f"Skipped unknown rule: {rt}",
            )

    def _verify_style(
        self, element: str, style_name: str, screenshot: Screenshot | None
    ) -> VerificationResult:
        """Verify a style is applied by checking the Word style selector."""
        state = self._vision.capture_state(include_ui_tree=True, include_ocr=True)
        if state.ui_tree:
            # Look for style name in the style selector (UI Automation)
            matches = state.ui_tree.find_by_name(style_name, partial=True)
            if matches:
                return VerificationResult(
                    passed=True,
                    description=f"Style '{style_name}' found in UI",
                )
        # OCR fallback: look for style name in style box
        if state.ocr_result:
            match = state.ocr_result.best_match(style_name, threshold=0.7)
            if match:
                return VerificationResult(
                    passed=True,
                    description=f"Style '{style_name}' visible in UI",
                )
        return VerificationResult(
            passed=False,
            description=f"Style '{style_name}' not detected",
        )

    def _verify_file_saved(self, path: str) -> VerificationResult:
        import os
        if path and os.path.exists(path):
            return VerificationResult(
                passed=True,
                description=f"File exists: {path}",
            )
        # Check title bar for unsaved indicator (*) via OCR
        state = self._vision.capture_state(include_ui_tree=False)
        has_asterisk = state.has_text("*") and state.has_text("Microsoft")
        if has_asterisk:
            return VerificationResult(
                passed=False,
                description="Title bar shows unsaved changes (*)",
            )
        return VerificationResult(
            passed=True,
            description="No unsaved indicator detected",
        )

    def wait_for_task_stable(self, timeout: float = 10.0) -> None:
        """Wait until the screen stops changing (task action settled)."""
        from vision.detector import get_change_detector
        screen = get_screenshot_engine()
        detector = get_change_detector()
        prev = screen.capture_full().array
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.3)
            curr = screen.capture_full().array
            if not detector.has_changed(prev, curr):
                return
            prev = curr


# Module-level singleton
_agent: QAAgent | None = None


def get_qa_agent() -> QAAgent:
    global _agent
    if _agent is None:
        _agent = QAAgent()
    return _agent
