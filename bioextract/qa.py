"""Run independent QA passes over a paper, each paired with a Domain Research Agent."""

from typing import List, Optional, Tuple

from .domain_agent import preview_briefing, run_domain_agent
from .llm import LLMClient
from .loaders import format_questions_block
from .parsing import align_to_questions, parse_qa_blocks
from .prompts import QA_PROMPT, QA_PROMPT_WITH_BRIEFING


def run_qa(
    client: LLMClient,
    paper_content: str,
    questions: List[str],
    runs: int = 3,
    use_domain_agent: bool = True,
    max_searches_per_domain_agent: int = 5,
    verbose: bool = False,
    on_event=None,
) -> Tuple[List[List[dict]], List[Optional[dict]]]:
    """Run ``runs`` independent QA passes.

    Each pass is a two-agent interaction:
      1. A Domain Research Agent (same model, run-specific persona) researches
         the question set with web search and produces a briefing.
      2. The QA agent reads the paper and the briefing, then answers.

    Returns ``(all_runs, briefings)`` where:
      - ``all_runs[i]`` is the list of QA records for run ``i+1`` (aligned to
        ``questions``), which keeps the shape the adjudicator expects.
      - ``briefings[i]`` is the domain-agent briefing dict for run ``i+1``
        (or ``None`` when ``use_domain_agent=False``).
    """
    questions_block = format_questions_block(questions)

    all_runs: List[List[dict]] = []
    briefings: List[Optional[dict]] = []

    for i in range(1, runs + 1):
        if verbose:
            print(f"\n  ── Run {i}/{runs} " + "─" * 48)
        if on_event:
            on_event({"type": "run_start", "run": i, "total_runs": runs})

        briefing = None
        if use_domain_agent:
            if verbose:
                print(f"  [Step 1] Domain Research Agent (run {i})")
            briefing = run_domain_agent(
                client,
                questions,
                run_index=i - 1,
                max_searches=max_searches_per_domain_agent,
                verbose=verbose,
                on_event=on_event,
            )
            if verbose:
                preview = preview_briefing(briefing["briefing_text"])
                print("    Briefing preview:")
                for line in preview.splitlines():
                    print(f"      │ {line}")

        if verbose:
            step_label = "[Step 2]" if use_domain_agent else "[QA]"
            print(f"  {step_label} QA agent ({client.provider}:{client.model}) reading paper + briefing …"
                  if briefing else
                  f"  {step_label} QA agent ({client.provider}:{client.model}) reading paper …")
        if on_event:
            on_event({
                "type": "qa_start",
                "run": i,
                "has_briefing": briefing is not None,
                "persona": briefing["persona"] if briefing else None,
            })

        if briefing is not None:
            prompt = QA_PROMPT_WITH_BRIEFING.format(
                persona_name=briefing["persona"],
                persona_instructions=briefing["persona_instructions"],
                briefing_text=briefing["briefing_text"],
                paper_content=paper_content,
                questions=questions_block,
            )
        else:
            prompt = QA_PROMPT.format(
                paper_content=paper_content,
                questions=questions_block,
            )

        resp = client.complete(prompt)
        records = parse_qa_blocks(resp.text)
        aligned = align_to_questions(records, questions)
        for rec in aligned:
            rec["run"] = i
            rec["_raw_tokens_in"] = resp.input_tokens
            rec["_raw_tokens_out"] = resp.output_tokens

        all_runs.append(aligned)
        briefings.append(briefing)

        parsed = sum(1 for r in aligned if r["answer"])
        if verbose:
            print(f"    QA agent produced {parsed}/{len(questions)} answers "
                  f"(tokens: in={resp.input_tokens:,}, out={resp.output_tokens:,})")
        if on_event:
            on_event({
                "type": "qa_done",
                "run": i,
                "answered": parsed,
                "total": len(questions),
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "records": aligned,
            })

    return all_runs, briefings
