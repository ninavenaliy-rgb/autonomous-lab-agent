"""
Application crash recovery.
Detects crashes, restarts Office applications, and restores session state.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import get_config
from storage.checkpoints import AgentCheckpoint, get_checkpoint_manager


@dataclass
class RecoveryAttempt:
    application: str
    attempt_number: int
    success: bool
    strategy: str
    duration_seconds: float
    restored_from_checkpoint: bool = False


class CrashRecoveryManager:
    """
    Detects and recovers from Office application crashes.
    Strategies: restart, restore from last save, recover autosave.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._max_restarts = cfg.recovery.max_crash_restarts
        self._restart_wait = cfg.recovery.restart_wait_seconds
        self._wm = None
        self._inspector = None
        self._restart_counts: dict[str, int] = {"word": 0, "excel": 0}

    def _get_wm(self):
        if self._wm is None:
            from ui.window_manager import get_window_manager
            self._wm = get_window_manager()
        return self._wm

    def is_application_alive(self, app: str) -> bool:
        """Check if Word or Excel is running and responsive."""
        wm = self._get_wm()
        if app == "word":
            win = wm.find_word_window()
        elif app == "excel":
            win = wm.find_excel_window()
        else:
            return False
        if not win:
            return False
        # Check if process is still running
        if win.pid:
            return wm.is_process_alive(win.pid)
        return True

    def recover_application(
        self,
        app: str,
        file_path: str | None = None,
        session_id: str = "",
        task_id: str = "",
    ) -> RecoveryAttempt:
        """Attempt to recover a crashed/frozen application."""
        t0 = time.time()
        attempt_num = self._restart_counts.get(app, 0) + 1

        if attempt_num > self._max_restarts:
            logger.error(f"Max restarts exceeded for {app} ({attempt_num}/{self._max_restarts})")
            return RecoveryAttempt(
                application=app,
                attempt_number=attempt_num,
                success=False,
                strategy="max_exceeded",
                duration_seconds=time.time() - t0,
            )

        self._restart_counts[app] = attempt_num
        logger.warning(f"Recovering {app} (attempt {attempt_num}/{self._max_restarts})")

        # Step 1: Kill the frozen process
        self._kill_application(app)
        time.sleep(2.0)

        # Step 2: Restart
        strategy = "restart"
        success = self._restart_application(app, file_path)

        if success:
            # Step 3: Check for autosave recovery
            if self._wait_for_autosave_dialog(app):
                self._handle_autosave(app)
                strategy = "restart_with_autosave"

            # Step 4: Reset failure counter on success
            logger.info(f"{app} recovered successfully")

        duration = time.time() - t0
        return RecoveryAttempt(
            application=app,
            attempt_number=attempt_num,
            success=success,
            strategy=strategy,
            duration_seconds=duration,
        )

    def _kill_application(self, app: str) -> None:
        wm = self._get_wm()
        process_names = {
            "word": ["WINWORD.EXE", "WINWORD"],
            "excel": ["EXCEL.EXE", "EXCEL"],
        }
        names = process_names.get(app, [])
        for name in names:
            pids = wm.find_process_by_name(name)
            for pid in pids:
                logger.debug(f"Killing {app} PID={pid}")
                wm.kill_process(pid)
        time.sleep(1.0)

    def _restart_application(self, app: str, file_path: str | None = None) -> bool:
        wm = self._get_wm()
        if app == "word":
            proc = wm.launch_word(file_path)
            if not proc:
                return False
            win = wm.wait_for_window("Word", timeout=25.0)
        elif app == "excel":
            proc = wm.launch_excel(file_path)
            if not proc:
                return False
            win = wm.wait_for_window("Excel", timeout=25.0)
        else:
            return False

        if win:
            wm.maximize_window(win.hwnd)
            time.sleep(self._restart_wait)
            return True
        return False

    def _wait_for_autosave_dialog(self, app: str, timeout: float = 10.0) -> bool:
        """Wait for autosave recovery dialog to appear."""
        from recovery.popup_handler import get_popup_handler
        handler = get_popup_handler()
        deadline = time.time() + timeout
        while time.time() < deadline:
            windows = handler._get_all_windows()
            for _, title in windows:
                t_lower = title.lower()
                if any(k in t_lower for k in ["восстановлен", "recovered", "autosave"]):
                    return True
            time.sleep(0.5)
        return False

    def _handle_autosave(self, app: str) -> None:
        """Handle Office document recovery dialog."""
        from recovery.popup_handler import get_popup_handler
        handler = get_popup_handler()
        windows = handler._get_all_windows()
        for hwnd, title in windows:
            if any(k in title.lower() for k in ["восстановлен", "recovered"]):
                # Click "Восстановленный файл" and keep it
                handler._click_button_in_window(hwnd, "Восстановленный файл")
                time.sleep(0.5)
                logger.info(f"Autosave recovery accepted for {app}")
                return

    def handle_hang(self, app: str) -> bool:
        """Handle a hung (unresponsive) application."""
        logger.warning(f"Handling hung {app}")
        # First try: send WM_KEYDOWN Enter to unfreeze
        wm = self._get_wm()
        if app == "word":
            win = wm.find_word_window()
        else:
            win = wm.find_excel_window()

        if win and win.hwnd:
            try:
                import win32con
                import win32api
                win32api.SendMessage(win.hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
                time.sleep(1.0)
                if self.is_application_alive(app):
                    logger.info(f"{app} responded to Escape key")
                    return True
            except Exception:
                pass

        # Second: kill and restart
        result = self.recover_application(app)
        return result.success

    def reset_restart_counter(self, app: str) -> None:
        self._restart_counts[app] = 0


class SessionRecoveryManager:
    """
    Restores agent session from checkpoint after a crash.
    Works with CheckpointManager to find last good state.
    """

    def __init__(self) -> None:
        self._checkpoints = get_checkpoint_manager()

    def can_resume(self, session_id: str) -> bool:
        cp = self._checkpoints.get_resume_point(session_id)
        return cp is not None

    def get_resume_state(self, session_id: str) -> AgentCheckpoint | None:
        return self._checkpoints.get_resume_point(session_id)

    def mark_session_complete(self, session_id: str) -> None:
        self._checkpoints.delete_session(session_id)
        logger.info(f"Session checkpoints cleaned up: {session_id}")


# Module-level singletons
_crash: CrashRecoveryManager | None = None
_session: SessionRecoveryManager | None = None


def get_crash_recovery() -> CrashRecoveryManager:
    global _crash
    if _crash is None:
        _crash = CrashRecoveryManager()
    return _crash


def get_session_recovery() -> SessionRecoveryManager:
    global _session
    if _session is None:
        _session = SessionRecoveryManager()
    return _session
