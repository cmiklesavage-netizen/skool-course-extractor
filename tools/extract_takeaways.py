"""
Reads .tmp/course_data.json and calls OpenRouter to extract 5-7 key takeaways
per video. Saves results to .tmp/course_takeaways.json.

Usage:
    python tools/extract_takeaways.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

TMP_DIR = Path(__file__).parent.parent / ".tmp"
INPUT_FILE = TMP_DIR / "course_data.json"
OUTPUT_FILE = TMP_DIR / "course_takeaways.json"

SYSTEM_PROMPT = (
    "You are an expert course analyst. Extract the 5 to 7 most important, "
    "actionable takeaways from the video transcript below. "
    "Return ONLY a JSON array of strings — each string is one concise takeaway. "
    "No preamble, no explanation, no markdown fences. Just the raw JSON array."
)


def get_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set in .env")
        sys.exit(1)
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def extract_for_lesson(client: OpenAI, lesson: dict, model: str) -> list[str]:
    transcript = lesson.get("transcript", "").strip()
    if not transcript:
        return []

    user_msg = f"Video title: {lesson['lesson_title']}\n\nTranscript:\n{transcript}"
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = response.choices[0].message.content.strip()
        # Strip accidental markdown fences if the model adds them anyway
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"    WARNING: Could not parse JSON response for '{lesson['lesson_title']}': {e}")
        return ["[Extraction failed — retry manually]"]
    except Exception as e:
        print(f"    WARNING: API error for '{lesson['lesson_title']}': {e}")
        return ["[Extraction failed — retry manually]"]


def main():
    if not INPUT_FILE.exists():
        print(f"ERROR: {INPUT_FILE} not found. Run scrape_skool_course.py first.")
        sys.exit(1)

    data = json.loads(INPUT_FILE.read_text(encoding="utf-8"))
    lessons = data.get("lessons", [])

    if not lessons:
        print("ERROR: No lessons found in course_data.json")
        sys.exit(1)

    model = os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-4-5")
    client = get_client()

    print(f"Extracting takeaways using {model}...")
    print(f"Processing {len(lessons)} lessons...\n")

    skipped = 0
    for i, lesson in enumerate(lessons):
        title = lesson.get("lesson_title", f"Lesson {i+1}")
        method = lesson.get("transcript_method", "none")

        if method == "none" or not lesson.get("transcript", "").strip():
            print(f"  [{i+1}/{len(lessons)}] SKIP (no transcript): {title}")
            lessons[i]["takeaways"] = []
            skipped += 1
            continue

        print(f"  [{i+1}/{len(lessons)}] {title}")
        takeaways = extract_for_lesson(client, lesson, model)
        lessons[i]["takeaways"] = takeaways

    data["processed_at"] = datetime.now(timezone.utc).isoformat()
    data["lessons"] = lessons

    OUTPUT_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    processed = len(lessons) - skipped
    print(f"\nDone. Processed {processed}/{len(lessons)} lessons.")
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
