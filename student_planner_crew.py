# student_planner_crew.py
from textwrap import dedent
from typing import Dict, Optional, Any
import os
import json
import re

from dotenv import load_dotenv
load_dotenv()

# CrewAI imports (match your earlier sample)
from crewai import Agent, Task, Crew, Process, LLM

# Configure Gemini LLM for CrewAI (reads GEMINI_API_KEY or GOOGLE_API_KEY)
gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
gemini_llm = LLM(
    model="gemini/gemini-2.0-flash",
    api_key=gemini_api_key,
    temperature=0.2,
)
import os
import re
import json
from typing import Any, Dict

# -------------------
# Helpers (paste these near top of your module)
# -------------------

def parse_schedule_from_text(text: str) -> Dict[str, Any]:
    """
    Returns a dict mapping date-string -> topics (string or list).
    Handles common patterns:
     - Markdown lists like "- 2025-12-01: Topic A, Topic B"
     - Table rows like "| 2025-12-01 | Topic A, Topic B |"
     - Lines like "2025-12-01 — Topic A"
     - JSON blob containing "schedule"
    """
    if not text:
        return {}

    # 1) Try to find an embedded JSON with "schedule"
    json_matches = re.findall(r"(\{[\s\S]{0,3000}\})", text)
    for jm in json_matches:
        try:
            obj = json.loads(jm)
            if isinstance(obj, dict) and "schedule" in obj:
                sch = obj.get("schedule") or {}
                # if schedule is list of entries, convert to dict
                if isinstance(sch, list):
                    out = {}
                    for e in sch:
                        if isinstance(e, dict):
                            date = e.get("date") or e.get("day")
                            topics = e.get("topics") or e.get("topic") or e.get("items")
                            if date:
                                out[str(date)] = topics
                    return out
                if isinstance(sch, dict):
                    return {str(k): v for k, v in sch.items()}
        except Exception:
            pass

    out: Dict[str, Any] = {}

    # 2) Markdown/table style rows: capture lines with YYYY-MM-DD
    date_line_re = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\s*[:\-–—]\s*(?P<topics>.+)")
    for m in date_line_re.finditer(text):
        d = m.group("date").strip()
        t = m.group("topics").strip()
        out[d] = t

    # 3) Markdown bullet lines with date + colon
    bullet_re = re.compile(r"^[\-\*\+]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*[:\-–—]?\s*(?P<topics>.+)$", re.MULTILINE)
    for m in bullet_re.finditer(text):
        d = m.group("date").strip()
        t = m.group("topics").strip()
        out[d] = t

    # 4) Table-like rows: | date | topics |
    table_row_re = re.compile(r"\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<topics>[^|\n]+)\|")
    for m in table_row_re.finditer(text):
        d = m.group("date").strip()
        t = m.group("topics").strip()
        out[d] = t

    # 5) Fallback: lines like "Dec 1, 2025 — topics"
    month_line_re = re.compile(r"(?P<date>[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})\s*[:\-–—]\s*(?P<topics>.+)")
    for m in month_line_re.finditer(text):
        d = m.group("date").strip()
        t = m.group("topics").strip()
        out[d] = t

    return out

def ensure_list_or_dict(obj, want="dict"):
    if obj is None:
        return {} if want == "dict" else []
    return obj


class StudentPlannerCrew:
    """
    Crew that orchestrates: Syllabus Parser -> Planner -> Notes -> Quiz creator
    Mirrors your resume matcher sample style and uses Gemini via CrewAI LLM wrapper.
    """

    def __init__(self):
        # Syllabus Parser agent
        self.syllabus_parser = Agent(
            role="Syllabus Parser",
            goal="Extract a concise list of study topics from raw syllabus text.",
            backstory=dedent(
                """
                You are an expert syllabus parser. Read the raw syllabus and return
                a JSON array of short topic strings (3-8 words each). No extra commentary.
                """
            ),
            llm=gemini_llm,
            verbose=True,
            allow_delegation=False,
        )

        # Planner agent
        self.planner = Agent(
            role="Study Planner",
            goal="Generate an actionable day-by-day study schedule mapping ISO dates to topics.",
            backstory=dedent(
                """
                You are a careful study planner. Given a list of topics and either an exam date
                or a number of study days, produce a compact JSON object mapping ISO dates (YYYY-MM-DD)
                to arrays of topic strings. Prefer grouping related topics and 1-3 topics per day.
                """
            ),
            llm=gemini_llm,
            verbose=True,
            allow_delegation=False,
        )

        # Notes creator agent
        self.notes_creator = Agent(
            role="Notes Creator",
            goal="Create readable, multi-section study notes for each topic.",
            backstory=dedent(
                """
                You are an expert teacher and notes-author. For each topic produce sections:
                Summary, Key definitions & formulas, Worked examples, Common mistakes,
                Quick checklist, Practice questions. Return a JSON object {topic: note_text}.
                """
            ),
            llm=gemini_llm,
            verbose=True,
            allow_delegation=False,
        )

        # Quiz creator agent
        self.quiz_creator = Agent(
            role="Quiz Creator",
            goal="Generate multiple-choice quizzes for study topics.",
            backstory=dedent(
                """
                You are a quiz generation expert. For each topic produce a JSON array of MCQs.
                Each MCQ must be {"question":"", "options":[<4 strings>], "answer_index":<0-3>}.
                Return a JSON object {topic: [mcq_objects]}.
                """
            ),
            llm=gemini_llm,
            verbose=True,
            allow_delegation=False,
        )

    def build_crew(self, syllabus_text: str, exam_date: Optional[str] = None, days: Optional[int] = None) -> Crew:
        syllabus_task = Task(
            description=dedent(
                f"""
                Parse the following syllabus text and return a JSON array of concise topic strings (3-8 words each).
                Output must be ONLY a JSON array, e.g. ["Topic A", "Topic B", ...], with no extra commentary.

                Syllabus:
                \"\"\"{syllabus_text}\"\"\"

                Return JSON array.
                """
            ),
            agent=self.syllabus_parser,
            expected_output="JSON array of topic strings.",
        )

        planner_task = Task(
            description=dedent(
                f"""
                Using the JSON array produced earlier (Syllabus Topics), create a day-by-day study schedule.
                Input context includes either an exam_date (ISO YYYY-MM-DD) or number of days.

                Rules:
                - If exam_date is provided, schedule must end on that date (include it).
                - If days is provided, start from today and span that many days.
                - Aim for 1-3 topics per day depending on volume.
                - Return a single JSON object mapping ISO date strings to arrays of topic strings,
                  e.g. {{ "2025-11-30": ["T1","T2"], ... }} and nothing else.

                Context fields provided:
                - exam_date: {exam_date}
                - days: {days}
                """
            ),
            agent=self.planner,
            expected_output="A JSON object mapping dates to lists of topic strings.",
            context=[syllabus_task],
        )

        notes_task = Task(
            description=dedent(
                """
                Using the schedule and the original list of topics, produce study notes for each topic.
                For each topic return a single string containing these sections separated by double newlines:

                Summary:
                Key definitions & formulas:
                Worked examples:
                Common mistakes:
                Quick checklist:
                Practice questions:

                Return a JSON object: { "Topic A": "note text...", "Topic B": "note text...", ... } and nothing else.
                Use plain text (or markdown) inside the note strings.
                """
            ),
            agent=self.notes_creator,
            expected_output="A JSON object mapping each topic to its note text (multi-section).",
            context=[syllabus_task, planner_task],
        )

        quiz_task = Task(
            description=dedent(
                """
                Using the list of topics (and optionally the notes), generate multiple-choice quizzes.
                For each topic produce an array of MCQs in JSON. Each question object must include:
                - question (string)
                - options (array of 4 strings)
                - answer_index (0-3)

                Return a JSON object: { "Topic A": [mcq1, mcq2, ...], ... } and nothing else.
                """
            ),
            agent=self.quiz_creator,
            expected_output="A JSON object mapping each topic to an array of MCQs.",
            context=[syllabus_task, notes_task],
        )

        crew = Crew(
            agents=[
                self.syllabus_parser,
                self.planner,
                self.notes_creator,
                self.quiz_creator,
            ],
            tasks=[syllabus_task, planner_task, notes_task, quiz_task],
            process=Process.sequential,
            verbose=True,
        )
        return crew

    def run(self, syllabus_text: str, exam_date: Optional[str] = None, days: Optional[int] = None) -> Dict[str, Any]:
        """
        Run the pipeline. Returns a dict with:
          - full_markdown: raw crew.kickoff() output as string
          - topics: parsed list or None
          - schedule: parsed dict or None
          - notes: parsed dict or None
          - quizzes: parsed dict or None

        Note: the crew/agents are asked to return JSON; this method tries to extract that JSON
        from the combined textual output. Extraction is best-effort.
        """
        crew = self.build_crew(syllabus_text=syllabus_text, exam_date=exam_date, days=days)
        raw_result = crew.kickoff()

        raw_text = str(raw_result)

        # Try to extract the main JSON blocks in a best-effort manner.
        # We'll attempt to find multiple blocks: topics (array), schedule (object), notes (object), quizzes (object).
        parsed = {
            "full_markdown": raw_text,
            "topics": None,
            "schedule": None,
            "notes": None,
            "quizzes": None,
        }

        # Heuristics: attempt to find a topics array first (the first large JSON array)
        # then subsequent JSON objects for schedule/notes/quizzes.
        # We'll search for JSON array blocks and JSON object blocks and assign by size / order.
        json_blocks = []
        for m in re.finditer(r'(\{(?:[^{}]|\{[^{}]*\})*\}|\[(?:[^\[\]]|\[[^\[\]]*\])*\])', raw_text, flags=re.DOTALL):
            block = m.group(1)
            # try parse
            obj = None
            try:
                obj = json.loads(block)
            except Exception:
                try:
                    obj = json.loads(block.replace("'", '"'))
                except Exception:
                    obj = None
            if obj is not None:
                json_blocks.append(obj)

        # assign by heuristic
        # prefer first list as topics
        for b in json_blocks:
            if parsed["topics"] is None and isinstance(b, list):
                parsed["topics"] = b
                continue
            if parsed["schedule"] is None and isinstance(b, dict):
                # if keys look like dates (YYYY-), treat as schedule
                keys = list(b.keys())
                if keys and all(re.match(r'\d{4}-\d{2}-\d{2}', str(k)) for k in keys):
                    parsed["schedule"] = b
                    continue
                # otherwise, if dict with many string values that are lists -> could be quizzes/notes
                # tentatively set schedule if not set
                if parsed["schedule"] is None:
                    parsed["schedule"] = b
                    continue

        # second pass: fill notes and quizzes from remaining dicts
        remaining_dicts = [b for b in json_blocks if isinstance(b, dict)]
        # try find the biggest dict for notes (long strings); quizzes contain lists of objects
        if remaining_dicts:
            # choose candidate for notes: dict whose values are long strings
            for d in remaining_dicts:
                if parsed["notes"] is None:
                    is_notes_like = all(isinstance(v, str) and len(v) > 80 for v in d.values())
                    if is_notes_like:
                        parsed["notes"] = d
                        continue
                if parsed["quizzes"] is None:
                    is_quiz_like = all(isinstance(v, list) for v in d.values())
                    if is_quiz_like:
                        parsed["quizzes"] = d
                        continue

        # fallback: if only one dict exists and schedule is set but notes/quizzes not, try to fill
        if parsed["notes"] is None or parsed["quizzes"] is None:
            for d in remaining_dicts:
                # skip schedule if equal
                if parsed["schedule"] is not None and d == parsed["schedule"]:
                    continue
                if parsed["notes"] is None and any(isinstance(v, str) for v in d.values()):
                    parsed["notes"] = parsed["notes"] or d
                elif parsed["quizzes"] is None and any(isinstance(v, list) for v in d.values()):
                    parsed["quizzes"] = parsed["quizzes"] or d

        return parsed
