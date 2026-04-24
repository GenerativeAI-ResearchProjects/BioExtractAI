"""Expert adjudicator that fuses multiple QA runs into a single final answer."""

from typing import List, Optional

from .llm import LLMClient
from .parsing import parse_adjudicator_json
from .prompts import ADJUDICATOR_PROMPT, build_run_block


def adjudicate(
    client: LLMClient,
    paper_content: str,
    question: str,
    runs: List[dict],
) -> dict:
    """Adjudicate one question given per-run ``{answer, evidence, rationale}`` dicts."""
    prompt = ADJUDICATOR_PROMPT.format(
        paper_content=paper_content,
        question=question,
        runs_block=build_run_block(runs),
    )
    resp = client.complete(prompt)
    parsed = parse_adjudicator_json(resp.text)
    if parsed is None:
        return {
            "final_answer": resp.text.strip(),
            "rationale": "",
            "confidence": None,
            "raw": resp.text,
        }
    return {
        "final_answer": parsed.get("Final_Answer", "").strip(),
        "rationale": parsed.get("Rationale", "").strip(),
        "confidence": parsed.get("Confidence"),
        "raw": resp.text,
    }


def adjudicate_all(
    client: LLMClient,
    paper_content: str,
    questions: List[str],
    runs_by_question: List[List[dict]],
    verbose: bool = False,
    on_event=None,
) -> List[dict]:
    """Adjudicate every question. ``runs_by_question[i]`` holds the run records for question i."""
    if on_event:
        on_event({"type": "adj_start", "total": len(questions)})
    results = []
    for i, q in enumerate(questions):
        if verbose:
            print(f"  [Adjudicator {i + 1}/{len(questions)}] {q[:60]}...")
        runs = runs_by_question[i]
        result = adjudicate(client, paper_content, q, runs)
        result["qid"] = i + 1
        result["question"] = q
        results.append(result)
        if on_event:
            on_event({
                "type": "adj_progress",
                "qid": result["qid"],
                "question": q,
                "final_answer": result["final_answer"],
                "rationale": result["rationale"],
                "confidence": result["confidence"],
                "done": i + 1,
                "total": len(questions),
            })
    if on_event:
        on_event({"type": "adj_done"})
    return results


def group_runs_by_question(all_runs: List[List[dict]], n_questions: int) -> List[List[dict]]:
    """Transpose ``[run][question]`` into ``[question][run]``."""
    grouped: List[List[dict]] = [[] for _ in range(n_questions)]
    for run_records in all_runs:
        for rec in run_records:
            qidx = rec["qid"] - 1
            if 0 <= qidx < n_questions:
                grouped[qidx].append(
                    {
                        "answer": rec.get("answer", ""),
                        "evidence": rec.get("evidence", ""),
                        "rationale": rec.get("rationale", ""),
                    }
                )
    return grouped
