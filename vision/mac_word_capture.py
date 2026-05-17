"""
macOS Word screenshot capture.
Opens a .docx in Microsoft Word for Mac, takes screenshots, closes Word.
Uses native screencapture + AppleScript for reliable window targeting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from loguru import logger


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def open_docx_in_word(docx_path: Path) -> bool:
    """Open a .docx file in Microsoft Word for Mac."""
    script = f'''
tell application "Microsoft Word"
    activate
    open POSIX file "{docx_path}"
end tell
'''
    try:
        _run_applescript(script)
        time.sleep(4)  # Wait for Word to load the document
        return True
    except Exception as exc:
        logger.error(f"Failed to open Word: {exc}")
        return False


def close_word_document(docx_path: Path) -> None:
    """Close the document without saving."""
    name = Path(docx_path).name
    script = f'''
tell application "Microsoft Word"
    set docName to "{name}"
    repeat with d in documents
        if name of d contains docName then
            close d saving no
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        _run_applescript(script)
    except Exception:
        pass


def get_word_window_id() -> str | None:
    """Get the window ID of the Microsoft Word window."""
    script = '''
tell application "System Events"
    tell process "Microsoft Word"
        if exists window 1 then
            return id of window 1
        end if
    end tell
end tell
'''
    try:
        result = _run_applescript(script)
        return result if result else None
    except Exception:
        return None


def take_word_screenshot(output_path: Path, scroll_to_page: int = 1) -> bool:
    """Take a screenshot of the active Microsoft Word window."""
    try:
        # Bring Word to front
        _run_applescript('tell application "Microsoft Word" to activate')
        time.sleep(0.5)

        # Use screencapture to grab the frontmost window
        result = subprocess.run(
            ["screencapture", "-x", "-o", "-w", str(output_path)],
            capture_output=True, timeout=10,
        )
        if output_path.exists():
            logger.debug(f"Word screenshot saved: {output_path.name}")
            return True
    except Exception as exc:
        logger.error(f"Screenshot failed: {exc}")
    return False


def capture_word_document_pages(
    docx_path: Path,
    output_dir: Path,
    max_pages: int = 5,
) -> list[Path]:
    """
    Open a docx in Word, screenshot each page, return list of image paths.
    Scrolls through the document using AppleScript page navigation.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshots: list[Path] = []

    logger.info(f"Opening {docx_path.name} in Word for screenshots")
    if not open_docx_in_word(docx_path):
        return screenshots

    try:
        for page in range(1, min(max_pages, 4) + 1):
            # Navigate to page
            if page > 1:
                _run_applescript(f'''
tell application "Microsoft Word"
    activate
    go to page {page} of active document
end tell
''')
                time.sleep(0.8)

            # Take screenshot
            shot_path = output_dir / f"word_page_{page}.png"
            if take_word_screenshot(shot_path):
                screenshots.append(shot_path)
            time.sleep(0.3)

    finally:
        close_word_document(docx_path)

    logger.info(f"Captured {len(screenshots)} Word screenshots")
    return screenshots


def take_single_word_screenshot(
    docx_path: Path,
    output_path: Path,
) -> Path | None:
    """Open docx, take one screenshot of page 1, close. Returns path or None."""
    logger.info(f"Capturing Word screenshot for {docx_path.name}")
    if not open_docx_in_word(docx_path):
        return None
    try:
        time.sleep(1)
        if take_word_screenshot(output_path):
            return output_path
    finally:
        close_word_document(docx_path)
    return None
