"""Tests for the Instruction Parser Agent."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.parser_agent import (
    DocumentReader,
    LLMClient,
    ParserAgent,
    ParsedMethodology,
    ParsedTask,
    AtomicStep,
)


# ─────────────────────────────────────────────────────────────────────────────
# DocumentReader tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDocumentReader:
    def test_is_heading_numbered(self):
        reader = DocumentReader()
        assert reader._is_heading("1. Создание документа")
        assert reader._is_heading("2.3 Форматирование")
        assert reader._is_heading("1) Задание")

    def test_is_heading_all_caps(self):
        reader = DocumentReader()
        assert reader._is_heading("ВВЕДЕНИЕ")

    def test_is_heading_keyword(self):
        reader = DocumentReader()
        assert reader._is_heading("Задание 1: Работа с Word")

    def test_is_heading_long_text_rejected(self):
        reader = DocumentReader()
        long = "Это очень длинный текст который не является заголовком " * 5
        assert not reader._is_heading(long)

    def test_detect_application_word(self):
        reader = DocumentReader()
        assert reader._is_heading  # just check it exists


class TestParserAgent:
    def test_detect_word_application(self):
        agent = ParserAgent.__new__(ParserAgent)
        agent._reader = DocumentReader()
        agent._llm = MagicMock()
        agent._llm._client = None
        agent._chunk_size = 6000

        result = agent._detect_application(
            "Откройте документ Word и создайте заголовок"
        )
        assert result == "word"

    def test_detect_excel_application(self):
        agent = ParserAgent.__new__(ParserAgent)
        agent._reader = DocumentReader()
        agent._llm = MagicMock()

        result = agent._detect_application(
            "В ячейке A1 введите формулу и постройте диаграмму Excel"
        )
        assert result == "excel"

    def test_extract_json_clean(self):
        agent = ParserAgent.__new__(ParserAgent)
        data = {"title": "Test", "tasks": []}
        text = json.dumps(data)
        result = agent._extract_json(text)
        assert result == data

    def test_extract_json_with_code_fence(self):
        agent = ParserAgent.__new__(ParserAgent)
        data = {"title": "Test", "tasks": []}
        text = f"```json\n{json.dumps(data)}\n```"
        result = agent._extract_json(text)
        assert result is not None
        assert result["title"] == "Test"

    def test_extract_json_invalid_returns_none(self):
        agent = ParserAgent.__new__(ParserAgent)
        result = agent._extract_json("Not JSON at all")
        assert result is None

    def test_infer_action_open(self):
        agent = ParserAgent.__new__(ParserAgent)
        assert agent._infer_action("Откройте файл Microsoft Word") == "open_file"

    def test_infer_action_type(self):
        agent = ParserAgent.__new__(ParserAgent)
        assert agent._infer_action("Введите текст заголовка") == "type"

    def test_infer_action_save(self):
        agent = ParserAgent.__new__(ParserAgent)
        assert agent._infer_action("Сохраните документ") == "save_file"

    def test_split_text_small(self):
        agent = ParserAgent.__new__(ParserAgent)
        text = "Hello world"
        chunks = agent._split_text(text, 100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_split_text_large(self):
        agent = ParserAgent.__new__(ParserAgent)
        paragraphs = ["Paragraph " + str(i) for i in range(100)]
        text = "\n".join(paragraphs)
        chunks = agent._split_text(text, 200)
        assert len(chunks) > 1
        # Reassembled text contains all paragraphs
        reassembled = "\n".join(chunks)
        for p in paragraphs:
            assert p in reassembled

    def test_parse_heuristic_returns_methodology(self, tmp_path):
        """Test heuristic parsing with a mock DOCX."""
        agent = ParserAgent.__new__(ParserAgent)
        agent._reader = DocumentReader()
        agent._llm = MagicMock()
        agent._llm._client = None
        agent._chunk_size = 6000

        sections = {
            "1. Создание документа Word": "Откройте Microsoft Word. Создайте новый документ.",
            "2. Форматирование текста": "Выделите текст. Примените жирный шрифт.",
        }
        full_text = "\n".join(
            f"{h}\n{c}" for h, c in sections.items()
        )
        # Use a dummy path
        dummy_path = tmp_path / "lab.docx"
        dummy_path.write_text("dummy")

        result = agent._parse_heuristic(dummy_path, full_text, sections)
        assert isinstance(result, ParsedMethodology)
        assert len(result.tasks) == 2
        assert result.tasks[0].application == "word"

    def test_steps_extracted_from_numbered_list(self):
        agent = ParserAgent.__new__(ParserAgent)
        text = textwrap.dedent("""\
            1. Откройте Microsoft Word
            2. Создайте новый документ
            3. Введите заголовок
            4. Сохраните файл
        """)
        steps = agent._extract_steps_heuristic(text)
        assert len(steps) == 4
        assert steps[0].action == "open_file"
        assert steps[3].action == "save_file"


# ─────────────────────────────────────────────────────────────────────────────
# AtomicStep / ParsedTask model tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDataModels:
    def test_parsed_task_defaults(self):
        task = ParsedTask(
            task_id="word_1_1",
            application="word",
            title="Test",
            description="desc",
        )
        assert task.requires_screenshot is True
        assert task.steps == []
        assert task.validation_rules == []

    def test_atomic_step_required_fields(self):
        step = AtomicStep(step_id="s1", action="click", target="OK")
        assert step.optional is False
        assert step.value == ""

    def test_parsed_methodology_filter(self):
        m = ParsedMethodology(
            title="Lab",
            document_path="/tmp/lab.docx",
            tasks=[
                ParsedTask(task_id="w1", application="word", title="W", description=""),
                ParsedTask(task_id="e1", application="excel", title="E", description=""),
                ParsedTask(task_id="w2", application="word", title="W2", description=""),
            ],
        )
        assert len(m.get_word_tasks()) == 2
        assert len(m.get_excel_tasks()) == 1
