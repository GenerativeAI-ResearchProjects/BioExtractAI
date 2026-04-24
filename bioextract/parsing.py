"""Parse LLM responses into structured records."""

import json
import re
from typing import List, Optional

QA_BLOCK_PATTERN = re.compile(
    r"Question:\s*(.*?)\s*Evidence:\s*(.*?)\s*Rationale:\s*(.*?)\s*Answer:\s*(.*?)(?=\n---|\nQuestion:|\Z)",
    re.DOTALL,
)


def parse_qa_blocks(text: str) -> List[dict]:
    """Extract ``Question / Evidence / Rationale / Answer`` tuples from raw LLM text."""
    cleaned = re.sub(r"^###\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"^Question\s*\d*\s*[:\.\)]?\s*", "Question: ", cleaned, flags=re.MULTILINE)

    records = []
    for q, ev, rat, ans in QA_BLOCK_PATTERN.findall(cleaned):
        records.append(
            {
                "question": q.strip(),
                "evidence": ev.strip(),
                "rationale": rat.strip(),
                "answer": _clean_answer(ans),
            }
        )
    return records


def _clean_answer(s: str) -> str:
    s = s.replace("\n", " ")
    s = re.sub(r'"+', "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def align_to_questions(records: List[dict], questions: List[str]) -> List[dict]:
    """Pair parsed blocks with the input question list by positional order.

    Any missing blocks are filled with empty strings so every input question
    gets a row in the output.
    """
    aligned = []
    for i, q in enumerate(questions):
        if i < len(records):
            rec = dict(records[i])
            rec["question"] = q  # prefer the canonical question string
        else:
            rec = {"question": q, "evidence": "", "rationale": "", "answer": ""}
        rec["qid"] = i + 1
        aligned.append(rec)
    return aligned


def parse_adjudicator_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from adjudicator output, or ``None``."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
