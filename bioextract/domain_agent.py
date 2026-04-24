"""Expert Domain Agent: researches the question set with web search and produces a briefing.

One domain agent runs per QA pass, using the SAME LLM as the QA agent. A different
persona is used for each run so the three briefings surface different facets of the
domain (literal definitions vs. comparative context vs. skeptical failure modes),
which in turn gives the three QA runs genuinely different reasoning paths.
"""

from typing import List

from .llm import LLMClient, LLMResponse
from .loaders import format_questions_block


# Each persona steers both *what* the domain agent searches for and *what* it
# emphasizes in its briefing, so the three runs disagree in useful ways rather
# than collapsing to the same answer.
PERSONAS = [
    {
        "name": "Literal domain expert",
        "instructions": (
            "Prioritize precise terminology, standard definitions, and authoritative "
            "sources (peer-reviewed papers, curated databases, WHO/CDC). "
            "Spell out acronyms the QA agent is likely to encounter."
        ),
    },
    {
        "name": "Comparative context expert",
        "instructions": (
            "Prioritize how the concepts compare across the field: typical variations, "
            "synonyms, adjacent methodologies. Prefer review articles and comparative "
            "studies. Call out 'the usual way this is reported' so the QA agent can "
            "recognize it even when phrasing differs from the paper."
        ),
    },
    {
        "name": "Skeptical methods critic",
        "instructions": (
            "Prioritize failure modes: what do papers commonly under-report, misreport, "
            "or omit for these questions? What sanity checks should the QA agent apply "
            "before answering? Give concrete red flags."
        ),
    },
]


DOMAIN_AGENT_PROMPT = """You are a Domain Research Agent in a multi-agent literature-extraction pipeline.

A downstream QA agent will answer the {n} questions below against a single paper. BEFORE it does, your job is to prepare a short domain briefing so the QA agent reads the paper with the right terminology, context, and pitfalls in mind.

## Your persona for this run: {persona_name}
{persona_instructions}

## How to work
1. Scan the questions and decide which topics are worth researching for domain context (not paper-specific facts — the QA agent will handle those).
2. Use web search LIBERALLY — but stay focused. Prefer a handful of well-targeted queries over many shallow ones.
3. Do NOT attempt to answer the questions yourself — you have not seen the paper. Your only job is to make the QA agent more literate in the domain.

## Required output format

Respond in exactly two sections, in this order:

### Section 1 — Research trace
For each question (or group of related questions) you searched on, report:
- QID(s): e.g. "Q3, Q8"
- Queries: the actual search strings you ran
- Findings: 1–3 sentences summarizing what you learned that is relevant to the QA agent

If you decided a question needs no web research (e.g. it's a simple yes/no with obvious domain meaning), say so and move on.

### Section 2 — Domain briefing for the QA agent
For EVERY question, produce a briefing entry in this exact format (one per line or short paragraph):

Q<number>: <2–5 sentences covering key terms, what to look for in the paper, and any common pitfalls or variations>

## Questions
{questions_block}
"""


def run_domain_agent(
    client: LLMClient,
    questions: List[str],
    run_index: int,
    max_searches: int = 5,
    verbose: bool = False,
    on_event=None,
) -> dict:
    """Run one domain agent pass and return the briefing + search trace.

    ``run_index`` is 0-based; the persona is chosen as ``PERSONAS[run_index % 3]``.
    ``on_event`` is an optional callback receiving dicts that describe pipeline
    progress (used by the web UI to stream updates).
    """
    persona = PERSONAS[run_index % len(PERSONAS)]
    prompt = DOMAIN_AGENT_PROMPT.format(
        n=len(questions),
        persona_name=persona["name"],
        persona_instructions=persona["instructions"],
        questions_block=format_questions_block(questions),
    )

    if verbose:
        print(f"    Domain agent persona: {persona['name']}")
        print(f"    Calling {client.provider}:{client.model} with web search (max {max_searches}) ...")
    if on_event:
        on_event({
            "type": "domain_start",
            "run": run_index + 1,
            "persona": persona["name"],
            "persona_instructions": persona["instructions"],
            "max_searches": max_searches,
        })

    resp: LLMResponse = client.research(prompt, max_searches=max_searches)

    if verbose:
        if resp.used_web_search and resp.search_queries:
            print(f"    Domain agent ran {len(resp.search_queries)} search(es):")
            for q in resp.search_queries[:max_searches]:
                print(f"      → {q}")
        elif resp.used_web_search:
            print("    Domain agent had web search enabled but issued no queries.")
        else:
            print(f"    Domain agent: {resp.web_search_note or 'no web search used'}")
        # Hint at the briefing size so the user sees the agent actually produced one.
        print(f"    Briefing size: {len(resp.text):,} chars")

    briefing = {
        "persona": persona["name"],
        "persona_instructions": persona["instructions"],
        "briefing_text": resp.text,
        "search_queries": resp.search_queries,
        "search_results": resp.search_results,
        "used_web_search": resp.used_web_search,
        "web_search_note": resp.web_search_note,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
    }

    if on_event:
        on_event({
            "type": "domain_done",
            "run": run_index + 1,
            "persona": briefing["persona"],
            "search_queries": briefing["search_queries"],
            "search_results": briefing["search_results"],
            "used_web_search": briefing["used_web_search"],
            "web_search_note": briefing["web_search_note"],
            "briefing_preview": preview_briefing(briefing["briefing_text"], max_chars=800),
            "briefing_text": briefing["briefing_text"],
            "input_tokens": briefing["input_tokens"],
            "output_tokens": briefing["output_tokens"],
        })

    return briefing


def preview_briefing(briefing_text: str, max_chars: int = 400) -> str:
    """Return a short preview suitable for verbose/text output."""
    t = briefing_text.strip()
    if len(t) <= max_chars:
        return t
    return t[:max_chars].rstrip() + "…"
