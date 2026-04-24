"""Command-line entry point: extract answers to a question set from a paper."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .adjudicator import adjudicate_all, group_runs_by_question
from .llm import (
    DEFAULT_MODELS,
    ENV_KEYS,
    SUPPORTED_PROVIDERS,
    LLMClient,
    infer_provider,
)
from .loaders import load_paper, load_questions
from .qa import run_qa


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bioextract",
        description=(
            "Run multi-pass QA + expert adjudication over a scientific paper. "
            "Pass a path to the paper and a questions file; get back structured answers."
        ),
    )
    p.add_argument(
        "--paper",
        "-p",
        required=True,
        help="Path to the paper. Accepts .md, .txt, .pdf, or a directory containing one.",
    )
    p.add_argument(
        "--questions",
        "-q",
        required=True,
        help="Path to a text file with one question per line (optional leading numbering is ignored).",
    )
    p.add_argument(
        "--provider",
        default=None,
        choices=SUPPORTED_PROVIDERS,
        help=(
            "LLM provider. If omitted, it is inferred from --model (e.g. gpt-* → openai, "
            "claude-* → anthropic, deepseek-* → deepseek), otherwise picked from the first "
            "API key set in the environment."
        ),
    )
    p.add_argument(
        "--model",
        default=None,
        help=(
            "Model name. Providing this alone is enough — the provider (and its API key env var) "
            "are inferred from the name. Defaults: openai=gpt-5, anthropic=claude-opus-4-5, "
            "deepseek=deepseek-chat."
        ),
    )
    p.add_argument(
        "--runs",
        "-r",
        type=int,
        default=3,
        help="Number of independent QA passes (default: 3).",
    )
    p.add_argument(
        "--no-adjudicate",
        action="store_true",
        help="Skip the adjudicator and just return the per-run answers.",
    )
    p.add_argument(
        "--adjudicator-provider",
        default=None,
        choices=SUPPORTED_PROVIDERS,
        help="Provider for the adjudicator (default: same as --provider).",
    )
    p.add_argument(
        "--adjudicator-model",
        default=None,
        help="Model for the adjudicator (default: same as --model).",
    )
    p.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Directory to save results as JSON + XLSX. If omitted, results are printed to stdout.",
    )
    p.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="Output format for stdout (default: text). JSON is always written when --output-dir is used.",
    )
    p.add_argument(
        "--no-domain-agent",
        action="store_true",
        help=(
            "Skip the Domain Research Agent step. By default, each QA run is paired with a "
            "domain-agent run (same model, different persona per run) that web-searches the "
            "question topics and produces a briefing consumed by the QA agent."
        ),
    )
    p.add_argument(
        "--max-searches",
        type=int,
        default=5,
        help="Max web searches the domain agent can run per pass (default: 5).",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Per-call max output tokens (applies to Anthropic; default: 4096).",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="Override the API key (otherwise read from the provider's env var).",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print progress as runs complete.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    provider, model = _resolve_provider_model(args.provider, args.model)
    adj_provider, adj_model = _resolve_provider_model(
        args.adjudicator_provider,
        args.adjudicator_model,
        fallback_provider=provider,
        fallback_model=model,
    )

    if args.verbose:
        print(f"Loading paper:     {args.paper}")
    paper = load_paper(args.paper)
    questions = load_questions(args.questions)
    if args.verbose:
        print(f"Loaded {len(questions)} questions; paper is {len(paper):,} chars.")
        print(f"Provider: {provider}   Model: {model}")
        if not args.no_adjudicate:
            print(f"Adjudicator: {adj_provider}:{adj_model}")

    try:
        qa_client = LLMClient(
            provider=provider, model=model, api_key=args.api_key, max_tokens=args.max_tokens
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.verbose:
        print(f"Running {args.runs} QA pass(es)"
              + (" with paired Domain Research Agents..." if not args.no_domain_agent else "..."))
    all_runs, briefings = run_qa(
        qa_client,
        paper,
        questions,
        runs=args.runs,
        use_domain_agent=not args.no_domain_agent,
        max_searches_per_domain_agent=args.max_searches,
        verbose=args.verbose,
    )

    grouped = group_runs_by_question(all_runs, n_questions=len(questions))

    adjudicated = None
    if not args.no_adjudicate and args.runs > 0:
        adj_client = qa_client
        if adj_provider != provider or adj_model != model:
            adj_client = LLMClient(
                provider=adj_provider,
                model=adj_model,
                api_key=args.api_key if adj_provider == provider else None,
                max_tokens=args.max_tokens,
            )
        if args.verbose:
            print("Running adjudicator...")
        adjudicated = adjudicate_all(
            adj_client, paper, questions, grouped, verbose=args.verbose
        )

    result = {
        "paper": str(Path(args.paper).resolve()),
        "provider": provider,
        "model": model,
        "runs": args.runs,
        "domain_agent": {
            "enabled": not args.no_domain_agent,
            "max_searches_per_run": args.max_searches,
            "briefings": [
                None if b is None else {
                    "run": i + 1,
                    "persona": b["persona"],
                    "persona_instructions": b["persona_instructions"],
                    "used_web_search": b["used_web_search"],
                    "web_search_note": b["web_search_note"],
                    "search_queries": b["search_queries"],
                    "search_results": b["search_results"],
                    "briefing_text": b["briefing_text"],
                    "input_tokens": b["input_tokens"],
                    "output_tokens": b["output_tokens"],
                }
                for i, b in enumerate(briefings)
            ],
        },
        "adjudicator": None
        if args.no_adjudicate
        else {"provider": adj_provider, "model": adj_model},
        "questions": [
            {
                "qid": i + 1,
                "question": q,
                "runs": grouped[i],
                "final": (
                    None
                    if adjudicated is None
                    else {
                        "answer": adjudicated[i]["final_answer"],
                        "rationale": adjudicated[i]["rationale"],
                        "confidence": adjudicated[i]["confidence"],
                    }
                ),
            }
            for i, q in enumerate(questions)
        ],
    }

    if args.output_dir:
        _write_outputs(result, Path(args.output_dir))
        if args.verbose:
            print(f"Wrote results to {args.output_dir}")

    if args.format == "json":
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_text(result)
    return 0


def _autodetect_provider() -> str:
    for provider in SUPPORTED_PROVIDERS:
        if os.environ.get(ENV_KEYS[provider]):
            return provider
    raise SystemExit(
        "error: no API key found. Set one of "
        + ", ".join(ENV_KEYS.values())
        + " or pass --provider and --api-key."
    )


def _resolve_provider_model(
    provider: Optional[str],
    model: Optional[str],
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None,
):
    """Resolve ``(provider, model)`` from user input with sensible fallbacks.

    Precedence:
      1. If both are given, return them verbatim.
      2. If only provider is given, use its default model (or ``fallback_model``
         when it matches the provider).
      3. If only model is given, infer provider from the model name; fall back
         to ``fallback_provider`` or env-var autodetect.
      4. If neither is given, use ``fallback_provider`` / ``fallback_model``
         when available, otherwise autodetect provider and use its default model.
    """
    if provider and model:
        return provider, model

    if provider and not model:
        if fallback_model and fallback_provider == provider:
            return provider, fallback_model
        return provider, DEFAULT_MODELS[provider]

    if model and not provider:
        inferred = infer_provider(model)
        if inferred:
            return inferred, model
        if fallback_provider:
            return fallback_provider, model
        # Last resort: autodetect from env, but warn via SystemExit if nothing matches.
        return _autodetect_provider(), model

    # Neither provided.
    if fallback_provider and fallback_model:
        return fallback_provider, fallback_model
    chosen = fallback_provider or _autodetect_provider()
    return chosen, fallback_model or DEFAULT_MODELS[chosen]


def _print_text(result: dict) -> None:
    print()
    print(f"Paper:    {result['paper']}")
    print(f"Provider: {result['provider']} ({result['model']})")
    print(f"Runs:     {result['runs']}")
    if result["adjudicator"]:
        adj = result["adjudicator"]
        print(f"Adjudicator: {adj['provider']} ({adj['model']})")

    # Per-run domain agent → QA agent interaction.
    domain = result.get("domain_agent") or {}
    if domain.get("enabled"):
        print("=" * 72)
        print("Domain Research Agent ↔ QA Agent interactions")
        for b in domain.get("briefings") or []:
            if b is None:
                continue
            print(f"\n  Run {b['run']}: persona = {b['persona']}")
            if b.get("used_web_search") and b.get("search_queries"):
                print(f"    Web searches ({len(b['search_queries'])}):")
                for q in b["search_queries"]:
                    print(f"      → {q}")
            elif b.get("used_web_search"):
                print("    Web search enabled but no queries were issued.")
            else:
                note = b.get("web_search_note") or "no native web search for this provider"
                print(f"    No web search: {note}")
            # Short preview of the briefing so the user can see what was fed into QA.
            from .domain_agent import preview_briefing

            preview = preview_briefing(b.get("briefing_text") or "", max_chars=500)
            if preview:
                print("    Briefing (preview):")
                for line in preview.splitlines():
                    print(f"      │ {line}")
        print()

    print("=" * 72)
    for q in result["questions"]:
        print(f"\nQ{q['qid']}: {q['question']}")
        if q["final"] is not None:
            print(f"  Final:     {q['final']['answer']}")
            if q["final"]["confidence"] is not None:
                print(f"  Confidence: {q['final']['confidence']}")
            if q["final"]["rationale"]:
                print(f"  Rationale: {q['final']['rationale']}")
        else:
            for i, run in enumerate(q["runs"], 1):
                print(f"  Run {i}:  {run['answer']}")


def _write_outputs(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    try:
        import pandas as pd  # optional
    except ImportError:
        return

    rows = []
    for q in result["questions"]:
        row = {"QID": q["qid"], "Question": q["question"]}
        for i, run in enumerate(q["runs"], 1):
            row[f"Answer_{i}"] = run["answer"]
            row[f"Evidence_{i}"] = run["evidence"]
            row[f"Rationale_{i}"] = run["rationale"]
        if q["final"] is not None:
            row["Final_Answer"] = q["final"]["answer"]
            row["Final_Rationale"] = q["final"]["rationale"]
            row["Final_Confidence"] = q["final"]["confidence"]
        rows.append(row)
    try:
        with pd.ExcelWriter(out_dir / "result.xlsx") as writer:
            pd.DataFrame(rows).to_excel(writer, sheet_name="Answers", index=False)
            domain = result.get("domain_agent") or {}
            if domain.get("enabled"):
                brief_rows = []
                for b in domain.get("briefings") or []:
                    if b is None:
                        continue
                    brief_rows.append(
                        {
                            "Run": b["run"],
                            "Persona": b["persona"],
                            "Used_Web_Search": b["used_web_search"],
                            "Web_Search_Note": b["web_search_note"] or "",
                            "Search_Queries": "\n".join(b["search_queries"]),
                            "Briefing_Text": b["briefing_text"],
                        }
                    )
                if brief_rows:
                    pd.DataFrame(brief_rows).to_excel(
                        writer, sheet_name="Domain_Agent", index=False
                    )
    except Exception as e:  # openpyxl missing, etc.
        print(f"warning: could not write XLSX ({e}); JSON was saved.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
