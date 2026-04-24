"""Prompt templates used by the QA and adjudicator agents."""

QA_PROMPT_WITH_BRIEFING = """You are the QA agent in a two-agent extraction pipeline. A **Domain Research Agent** (your teammate, same underlying model, different persona) has already researched the domain with web search and produced the briefing below to help you read the paper accurately. Use the briefing to calibrate terminology and know what to look for — but base every ANSWER on the paper content itself, not on the briefing.

## Domain briefing from the Domain Research Agent

Persona of this run's domain agent: {persona_name}
{persona_instructions}

---
{briefing_text}
---

## For each question:

Step 1: get the question, store as "question".
Step 2: extract two or three sentences from the "paper content" that can be used to answer the question, separate them using '.', store as 'evidence'.
Step 3: provide the rationale about how you found the answer from the content in details. If the domain briefing informed your interpretation (e.g. disambiguating a term or flagging a pitfall to check), say so explicitly in the rationale. Store as 'rationale'.
Step 4: answer the question, store as 'answer'.
Step 5: format your answer in the format:

\"\"\"
Question: <question>

Evidence: <evidence>

Rationale: <rationale>

Answer: <answer>
\"\"\"

Make sure you answer all the questions.


## Paper content:

```
{paper_content}
```

## Questions:

{questions}
"""


QA_PROMPT = """Read the paper in "Paper content" section, and answer a list of questions in "Questions" section below.

## For each question:

Step 1: get the question, store as "question".
Step 2: extract two or three sentences from the "paper content" that can be used to answer the question, separate them using '.', store as 'evidence'.
Step 3: provide the rationale about how you found the answer from the content in details, store as 'rationale'.
Step 4: answer the question, store as 'answer'.
Step 5: format your answer in the format:

\"\"\"
Question: <question>

Evidence: <evidence>

Rationale: <rationale>

Answer: <answer>
\"\"\"

Make sure you answer all the questions.


## Paper content:

```
{paper_content}
```

## Questions:

{questions}
"""


ADJUDICATOR_PROMPT = """You are an Expert Adjudicator Agent in a virtual scientific lab.
Your task is to synthesize the most accurate, evidence-grounded answer from multiple AI runs that attempted to extract information from a research paper.
Each AI run may have made mistakes. You must reason critically, check for validity, and produce a single best final answer.

---
**Paper content (for re-verification if needed):**
```
{paper_content}
```

**Question:**
{question}

**AI runs:**
{runs_block}

---
### Your Role:
You are both an **Adjudicator** and a **Critic Agent**. Follow these reasoning steps explicitly before deciding:

1. **Check for blank or incomplete answers.**
   - If any answer is missing or blank, attempt to infer the likely answer based on the paper context and other answers.

2. **Evidence validity audit.**
   - Ask yourself: does the cited evidence describe results from *this study*, or is it describing prior work referenced in the paper?
   - Reject or down-weight evidence that refers to other studies.

3. **Domain knowledge check.**
   - Make sure the answer uses correct domain understanding.

4. **Cross-verification.**
   - If the runs agree but are likely wrong in the same way, question the common assumption.
   - If they disagree, analyze which one is more consistent with scientific logic and the context of the question.

5. **Missing evidence.**
   - If an answer has no evidence or rationale, note it unless the conclusion is clearly correct from context.

6. **External verification (conceptual).**
   - For claims that can be checked from general scientific knowledge, verify them mentally.
   - If the answer contains external sources (links, accession numbers, etc.), note that they should be verified separately.

7. **Resolve and produce final synthesis.**
   - Construct the best possible final answer in your own words.
   - Be concise but specific.
   - Provide a short rationale for your adjudication.

---

### Output format:
Respond in **JSON only**, using this schema:

{{
  "Final_Answer": "your single best adjudicated answer",
  "Rationale": "your reasoning and evaluation of the runs, including which one(s) were partly correct or wrong and why",
  "Confidence": 0.0
}}
"""


def build_run_block(runs):
    """Render the per-run section for the adjudicator prompt.

    ``runs`` is a list of dicts with keys: answer, evidence, rationale.
    """
    parts = []
    for i, r in enumerate(runs, 1):
        parts.append(
            "Run {i}:\n"
            "  Answer: {a}\n"
            "  Evidence: {e}\n"
            "  Rationale: {r}".format(
                i=i,
                a=r.get("answer", ""),
                e=r.get("evidence", ""),
                r=r.get("rationale", ""),
            )
        )
    return "\n\n".join(parts)
