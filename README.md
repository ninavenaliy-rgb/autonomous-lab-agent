# Autonomous Lab Agent

Production-grade autonomous desktop AI agent that fully completes Microsoft Word and Excel educational laboratory assignments without human intervention.

## Architecture

```
Parse → Plan → Execute → Validate → Screenshot → Report
         ↑                                          |
         └──────────── Recovery Loop ───────────────┘
```

### Layered system (10 modules)

| Layer | Module | Responsibility |
|-------|--------|----------------|
| 1 | `agents/parser_agent.py` | Read DOCX/PDF methodology, extract task graph via LLM |
| 2 | `core/planner.py` | Convert tasks to atomic action sequences |
| 3 | `agents/vision_agent.py` | Perceive desktop: UIA + OCR + CV |
| 4 | `agents/gui_agent.py` | Execute actions: UIA → keyboard → coordinates |
| 5 | `agents/qa_agent.py` | Validate results, verify UI state |
| 6 | `vision/screenshot_engine.py` | Capture, crop, optimize screenshots |
| 7 | `agents/report_agent.py` | Assemble DOCX report with figures + captions |
| 8 | `agents/recovery_agent.py` | Coordinate crash recovery + popup handling |
| 9 | `core/state_manager.py` | SQLite-backed state machine with checkpoints |
| 10 | `core/orchestrator.py` | Central coordinator and event loop |

## Requirements

- **Windows 11**
- **Python 3.12+**
- **Microsoft Office 365** (Word + Excel)
- **Tesseract OCR** (optional fallback): https://github.com/UB-Mannheim/tesseract/wiki
- **Anthropic or OpenAI API key**

## Installation

```bash
git clone <repo>
cd autonomous-lab-agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY or OPENAI_API_KEY
```

## Usage

### Run on a methodology file
```bash
python app/main.py run path/to/methodology.docx
```

### Run with student info (title page)
```bash
python app/main.py run lab1.docx \
  --student "Иванов И.И." \
  --group "ИС-42" \
  --teacher "Петрова А.В." \
  --university "Технический Университет" \
  --lab-number 1 \
  --output reports/lab1_report.docx
```

### Resume an interrupted session
```bash
python app/main.py run lab1.docx --session-id <session-id>
```

### Parse-only (no GUI execution)
```bash
python app/main.py run lab1.docx --dry-run
```

### Inspect methodology structure
```bash
python app/main.py inspect lab1.docx
```

### List sessions with checkpoints
```bash
python app/main.py list-sessions
```

### Generate sample methodology for testing
```bash
python examples/example_workflow.py --create-sample
python app/main.py run examples/sample_lab.docx --dry-run
```

## Configuration

All settings via `.env` or environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Claude API key (required) |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model |
| `VISION_OCR_BACKEND` | `easyocr` | `easyocr`, `tesseract`, or `both` |
| `UI_BACKEND` | `uia` | Windows UI Automation backend |
| `RECOVERY_MAX_CRASH_RESTARTS` | `3` | Max app restarts before giving up |
| `RECOVERY_HANG_THRESHOLD_SECONDS` | `60` | Seconds before hang detection triggers |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Action Execution Hierarchy

Every GUI action follows three tiers:

```
1. UIA Native (pywinauto)   — most reliable, element-based
        ↓ fails
2. Keyboard Shortcuts        — Ctrl+S, Alt+N, etc.
        ↓ fails
3. OCR-Located Coordinates   — find text → click center
```

## Supported Actions

| Action | Description |
|--------|-------------|
| `open_word` / `open_excel` | Launch Office application |
| `click` | Click UI element by name |
| `type` | Type text at element or cursor |
| `hotkey` | Send keyboard shortcut |
| `apply_style` | Apply Word paragraph style |
| `format_text` | Bold, italic, underline |
| `set_font` / `set_font_size` | Font settings |
| `align` | Text alignment (left/center/right/justify) |
| `insert_table` | Insert NxM table |
| `insert_image` | Insert image from path |
| `insert_toc` | Insert automatic Table of Contents |
| `save_file` / `save_as` | Save document |
| `click_cell` | Navigate to Excel cell (A1, B3, etc.) |
| `enter_formula` | Enter formula in Excel cell |
| `create_chart` | Create chart from selected data |
| `auto_sum` | Apply AutoSum |
| `menu_navigate` | Navigate menu path (Файл/Сохранить) |

## Output Structure

Every run produces:

```
reports/
  lab_report_<timestamp>.docx   ← Formatted DOCX with all content

logs/
  agent_<timestamp>.log         ← Full execution log
  failures_<date>.log           ← Error-only log
  screenshots/
    final_word_1_1.png           ← Task result screenshots
    FAILURE_<context>.png        ← Diagnostic failure captures

storage/
  agent.db                      ← SQLite: sessions, tasks, steps, logs
  checkpoints/
    <session>_<task>.json        ← Recovery checkpoints
```

## Report Format

Generated DOCX follows ГОСТ 7.32-2017 conventions:
- Times New Roman 14pt body, 16pt headings
- 1.5 line spacing, 1.25cm paragraph indent
- 3cm left / 1.5cm right / 2cm top-bottom margins
- Automatic Table of Contents (updates on Word open)
- Sequential figure captions: "Рисунок N — Description"
- Sequential table captions: "Таблица N — Description"
- LLM-generated section descriptions in academic Russian

## Recovery Capabilities

| Scenario | Strategy |
|----------|----------|
| App crash | Kill + restart + resume from checkpoint |
| App hang (60s no heartbeat) | Escape key → kill + restart |
| Popup dialog | Background scanner dismisses automatically |
| Element not found | Retry with UIA → OCR → coordinates |
| Action failure | Replan with alternative steps |
| Too many failures | Circuit breaker: 60s pause + reset |

## Testing

```bash
# Unit tests (no Windows required)
pytest tests/ -v

# With coverage
pytest tests/ --cov --cov-report=html
```

Tests cover parser, vision, planner, state manager, and checkpoint system.
All tests mock Windows-specific APIs so they run cross-platform.

## Project Structure

```
autonomous-lab-agent/
├── app/main.py                  CLI entry point (typer)
├── core/
│   ├── config.py                Pydantic settings
│   ├── orchestrator.py          Central coordinator
│   ├── planner.py               Task → steps planning
│   ├── executor.py              Step execution engine
│   └── state_manager.py         SQLite state machine
├── agents/
│   ├── parser_agent.py          LLM methodology parser
│   ├── vision_agent.py          Desktop perception
│   ├── gui_agent.py             GUI execution (UIA+OCR+coords)
│   ├── qa_agent.py              Result validation
│   ├── report_agent.py          DOCX report assembler
│   └── recovery_agent.py        Recovery coordinator
├── ui/
│   ├── ui_inspector.py          Windows UIA inspection
│   ├── window_manager.py        Window lifecycle (Win32)
│   └── controls.py              Action execution
├── vision/
│   ├── screenshot_engine.py     MSS capture + optimization
│   ├── ocr_engine.py            EasyOCR + Tesseract pipeline
│   └── detector.py              Template matching + change detection
├── report/
│   ├── docx_builder.py          DOCX construction (python-docx)
│   ├── caption_manager.py       Figure/table numbering
│   └── toc_manager.py           TOC field injection
├── recovery/
│   ├── watchdog.py              Heartbeat + hang detection
│   ├── crash_recovery.py        App restart + session restore
│   └── popup_handler.py         Background dialog dismisser
├── storage/
│   ├── database.py              SQLAlchemy async ORM
│   └── checkpoints.py           JSON checkpoint files
├── tests/                       pytest test suite
├── examples/                    Example workflows + sample docx
├── requirements.txt
├── pyproject.toml
└── .env.example
```
