# app.py - full file (drop into project root; replaces previous app.py)
import os
import re
import json
import logging
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify, make_response
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# Try to import your crew wrapper
try:
    from student_planner_crew import StudentPlannerCrew
except Exception as e:
    StudentPlannerCrew = None
    logging.exception("Failed to import StudentPlannerCrew: %s", e)

app = Flask(__name__)
planner_crew = StudentPlannerCrew() if StudentPlannerCrew else None

# --------------------
# Helper functions
# --------------------
def try_load_json(x):
    """Try to coerce JSON-like strings into Python objects; otherwise return original value."""
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if not isinstance(x, str):
        return x
    s = x.strip()
    if not s:
        return s
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        # naive fallback: replace single quotes with double quotes
        return json.loads(s.replace("'", '"'))
    except Exception:
        return s

def list_to_date_dict(lst):
    """Convert a list of entries into {date: topics} when possible."""
    out = {}
    if not isinstance(lst, list):
        return out
    for e in lst:
        if isinstance(e, dict):
            date_key = e.get("date") or e.get("day")
            topics = e.get("topics") or e.get("topic") or e.get("items")
            if date_key:
                out[str(date_key)] = topics
        elif isinstance(e, str):
            m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s*[:\-–—]\s*(.+)", e)
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out

def parse_schedule_from_text(text):
    """Extract YYYY-MM-DD -> topics mappings from raw LLM markdown/text."""
    if not text:
        return {}
    out = {}
    # try to find JSON blob with 'schedule' first
    json_matches = re.findall(r"(\{[\s\S]{0,6000}\})", text)
    for jm in json_matches:
        try:
            obj = json.loads(jm)
            if isinstance(obj, dict) and "schedule" in obj:
                sch = obj.get("schedule") or {}
                if isinstance(sch, dict):
                    return {str(k): v for k, v in sch.items()}
                if isinstance(sch, list):
                    return list_to_date_dict(sch)
        except Exception:
            pass

    # simple YYYY-MM-DD: topic lines
    date_line_re = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\s*[:\-–—]\s*(?P<topics>.+)")
    for m in date_line_re.finditer(text):
        out[m.group("date").strip()] = m.group("topics").strip()

    # bullet lines "- 2025-12-01: Topic"
    bullet_re = re.compile(r"^[\-\*\+]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*[:\-–—]?\s*(?P<topics>.+)$", re.MULTILINE)
    for m in bullet_re.finditer(text):
        out[m.group("date").strip()] = m.group("topics").strip()

    # table rows "| 2025-12-01 | Topic |"
    table_row_re = re.compile(r"\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<topics>[^|\n]+)\|")
    for m in table_row_re.finditer(text):
        out[m.group("date").strip()] = m.group("topics").strip()

    return out

def extract_json_from_text(text):
    """Return parsed JSON object found inside text (e.g. inside ```json``` or first JSON-looking object)."""
    if not text:
        return None
    m = re.search(r"```json\s*(\{[\s\S]+\})\s*```", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"(\{[\s\S]{50,10000}\})", text)
    if not m:
        return None
    js = m.group(1)
    try:
        return json.loads(js)
    except Exception:
        try:
            return json.loads(js.replace("'", '"'))
        except Exception:
            return None

def salvage_notes_from_raw(raw):
    """Try to extract a 'Notes' section from raw markdown text."""
    if not raw:
        return {}
    m = re.search(r"(?is)notes\s*[:\-]*\s*(.+?)(?:\n\s*\n\s*(quizzes|schedule|$))", raw)
    if not m:
        return {}
    notes_text = m.group(1).strip()
    per_topic = {}
    # split by double newline or by lines like "TopicName: ..."
    parts = re.split(r"\n{2,}|\n(?=[A-Za-z0-9 \-]{2,}:\s)", notes_text)
    for p in parts:
        if ":" in p:
            tname, body = p.split(":", 1)
            per_topic[tname.strip()] = body.strip()
    return per_topic if per_topic else notes_text

def naive_parse_quizzes_from_text(text):
    """Simple heuristic to convert Q/A formatted text into a quizzes dict."""
    if not text:
        return {}
    q_re = re.compile(r"(?m)^\s*Q[:\.\)]\s*(?P<q>.+)$")
    a_re = re.compile(r"(?m)^\s*A[:\.\)]\s*(?P<a>.+)$")
    q_matches = [m.group("q").strip() for m in q_re.finditer(text)]
    a_matches = [m.group("a").strip() for m in a_re.finditer(text)]
    if not q_matches:
        return {}
    q_list = []
    for i, qtxt in enumerate(q_matches):
        atxt = a_matches[i] if i < len(a_matches) else ""
        q_list.append({"question": qtxt, "options": [], "answer_index": None, "answer_text": atxt})
    return {"auto_parsed": q_list}

# --------------------
# Routes
# --------------------
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/plan", methods=["POST"])
def plan():
    if planner_crew is None:
        logging.error("StudentPlannerCrew not available (import failed).")
        return "StudentPlannerCrew not available (import failed). Check server logs.", 500

    syllabus_text = request.form.get("syllabus_text", "") or request.form.get("syllabus", "")
    exam_date = request.form.get("exam_date") or None
    days = request.form.get("days") or None
    if days:
        try:
            days = int(days)
        except Exception:
            days = None

    try:
        result = planner_crew.run(syllabus_text=syllabus_text, exam_date=exam_date, days=days)
    except Exception as e:
        logging.exception("planner_crew.run() raised an exception")
        result = {
            "topics": [],
            "schedule": {},
            "notes": {},
            "quizzes": {},
            "full_markdown": f"Error running crew: {repr(e)}"
        }

    # === DEBUG: print raw crew result ===
    try:
        print("\n\n=== CREW RESULT (raw dict) ===")
        print(json.dumps(result, indent=2, default=str)[:10000])
        print("=== END CREW RESULT ===\n\n")
    except Exception:
        print("CREW RESULT (non-json):", str(result)[:5000])

    # ---------------------------------------
    # NORMALIZATION (extract JSON-in-markdown if present, then coerce shapes)
    # ---------------------------------------
    raw = result.get("full_markdown") or result.get("raw") or ""

    # If the LLM embedded a JSON block inside the raw markdown, prefer that content
    parsed_blob = extract_json_from_text(raw)
    if parsed_blob and isinstance(parsed_blob, dict):
        for key in ("topics", "schedule", "notes", "quizzes"):
            val = parsed_blob.get(key)
            if val not in (None, [], {}, ""):
                result[key] = val

    # Load/normalize fields from result (coerce JSON-like strings)
    topics = try_load_json(result.get("topics", [])) or []
    schedule = try_load_json(result.get("schedule", {})) or {}
    notes = try_load_json(result.get("notes", {})) or {}
    quizzes = try_load_json(result.get("quizzes", {})) or {}

    # If schedule is a list -> convert to dict keyed by date
    if isinstance(schedule, list):
        schedule = list_to_date_dict(schedule)

    # If schedule values are JSON-strings, decode them
    if isinstance(schedule, dict):
        for k, v in list(schedule.items()):
            schedule[k] = try_load_json(v)

    # If schedule keys are not date-like (the crew returned topic->... mapping), convert to date->topics
    def keys_are_dates(d):
        if not isinstance(d, dict) or not d:
            return False
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        return all(isinstance(k, str) and date_re.match(k) for k in d.keys())

    if not keys_are_dates(schedule):
        # try to extract date->topics from raw text first
        parsed_from_raw = parse_schedule_from_text(raw)
        if parsed_from_raw:
            schedule = parsed_from_raw
        else:
            # fallback: interpret schedule's keys as topic names and produce sequential dates
            topic_keys = []
            if isinstance(schedule, dict) and schedule:
                topic_keys = list(schedule.keys())
            elif isinstance(schedule, list):
                topic_keys = [it for it in schedule if isinstance(it, str)]
            elif isinstance(schedule, str) and schedule.strip():
                topic_keys = [t.strip() for t in re.split(r",\s*|\n", schedule) if t.strip()]

            # fallback: use quizzes keys or topics if still empty
            if not topic_keys:
                if isinstance(quizzes, dict):
                    topic_keys = list(quizzes.keys())
                elif topics:
                    topic_keys = topics if isinstance(topics, list) else [topics]

            if topic_keys:
                new_schedule = {}
                today = date.today()
                for i, tname in enumerate(topic_keys):
                    dstr = (today + timedelta(days=i)).isoformat()
                    val = schedule.get(tname)
                    if isinstance(val, list) and all(isinstance(x, str) for x in val):
                        new_schedule[dstr] = val
                    else:
                        new_schedule[dstr] = [tname]
                schedule = new_schedule

    # If schedule values are unexpected quiz-lists (list of dicts with 'question'), try to map them back to topic names
    if isinstance(schedule, dict):
        for d_key, val in list(schedule.items()):
            if isinstance(val, list) and val and isinstance(val[0], dict) and "question" in val[0]:
                matched_topic = None
                if isinstance(quizzes, dict):
                    for tname, qlist in quizzes.items():
                        if qlist == val:
                            matched_topic = tname
                            break
                schedule[d_key] = [matched_topic] if matched_topic else ["Topic"]

    # If topics missing, derive from schedule values or quizzes keys
    if not topics:
        derived = []
        if isinstance(schedule, dict):
            for v in schedule.values():
                if isinstance(v, str):
                    for part in re.split(r",\s*|\n", v):
                        if part.strip():
                            derived.append(part.strip())
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, str):
                            derived.append(it.strip())
        if not derived and isinstance(quizzes, dict):
            derived = list(quizzes.keys())
        topics = list(dict.fromkeys(derived))

    # If notes missing, try to salvage from raw
    if (not notes or notes == {}) and raw:
        salv = salvage_notes_from_raw(raw)
        if salv:
            notes = salv

    # If quizzes is a list of topic names but parsed_blob has quizzes dict, prefer that
    if isinstance(quizzes, list) and parsed_blob and isinstance(parsed_blob.get("quizzes"), dict):
        quizzes = parsed_blob.get("quizzes")

    # If quizzes still a string, try naive parse
    if isinstance(quizzes, str) and quizzes.strip():
        parsed_q = naive_parse_quizzes_from_text(quizzes)
        if parsed_q:
            quizzes = parsed_q

    # Guarantee safe shapes for template
    if not isinstance(topics, list):
        topics = [topics] if topics else []
    if not isinstance(schedule, dict):
        schedule = {"": schedule} if schedule else {}
    if not isinstance(notes, (dict, str)):
        notes = {"notes": notes} if notes else {}
    if not isinstance(quizzes, dict):
        quizzes = {"quizzes": quizzes} if quizzes else {}

    # Debug logs
    logging.info("Normalized -> topics: %s", topics)
    logging.info("Normalized -> schedule keys: %s", list(schedule.keys())[:20])
    logging.info("Normalized -> notes type: %s", type(notes))
    logging.info("Normalized -> quizzes type: %s", type(quizzes))

    context = {
        "topics": topics,
        "schedule": schedule,
        "notes": notes,
        "quizzes": quizzes,
        "raw": raw,
        "result_json": json.dumps(result, indent=2, default=str)
    }

    # Try rendering template; if Jinja fails, return JSON debug so browser shows output
    try:
        return render_template("plan.html", **context)
    except Exception as e:
        logging.exception("Template rendering failed; returning JSON fallback.")
        return make_response(jsonify({"template_error": str(e), "context": context}), 500)

@app.route("/api/plan", methods=["POST"])
def api_plan():
    if planner_crew is None:
        return jsonify({"error": "StudentPlannerCrew not available"}), 500
    payload = request.get_json() or {}
    try:
        return jsonify(planner_crew.run(
            syllabus_text=payload.get("syllabus_text", ""),
            exam_date=payload.get("exam_date"),
            days=payload.get("days")
        ))
    except Exception as e:
        logging.exception("API plan failed")
        return jsonify({"error": str(e), "full_markdown": ""}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port, host="0.0.0.0")
