"""FastAPI server that exposes the BioExtractAI pipeline as a web UI.

Launch with the ``bioextract-web`` console script, or ``python -m bioextract.server``.

Serves a single-page app from ``bioextract/static`` and a small REST+SSE API:
  - GET  /                       -> the UI
  - GET  /api/config             -> providers + which API keys are detected
  - POST /api/run                -> start a pipeline job (multipart form)
  - GET  /api/jobs/{id}/stream   -> server-sent events for job progress
  - GET  /api/jobs/{id}          -> final JSON result once the job is done
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .adjudicator import adjudicate_all, group_runs_by_question
from .cli import _resolve_provider_model
from .llm import DEFAULT_MODELS, ENV_KEYS, MODEL_CATALOG, SUPPORTED_PROVIDERS, LLMClient
from .loaders import load_paper, load_questions
from .qa import run_qa

# ---------------------------------------------------------------------------
# FastAPI is an optional dependency so `import bioextract` keeps working even
# when the web stack is not installed. We defer the import until a server
# function is actually called.
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "The web UI needs the 'web' extra. Install with:\n"
            "    pip install -e '.[web]'\n"
            "or: pip install fastapi uvicorn python-multipart"
        ) from e


# ---------------------------------------------------------------------------
# In-memory job store. Each job owns its own event queue and final result.
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, job_id: str, config: dict):
        self.job_id = job_id
        self.config = config
        self.events: "queue.Queue[Any]" = queue.Queue()
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.started_at = time.time()

    def emit(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        self.events.put(event)

    def finish(self, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        if error:
            self.error = error
            self.emit({"type": "error", "message": error})
        if result is not None:
            self.result = result
            self.emit({"type": "done", "result": result})
        self.events.put(_SENTINEL)
        self.done.set()


_jobs: Dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _register_job(job: Job) -> None:
    with _jobs_lock:
        _jobs[job.job_id] = job


def _get_job(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return _jobs.get(job_id)


# ---------------------------------------------------------------------------
# Pipeline worker: runs in a background thread per job.
# ---------------------------------------------------------------------------

def _run_pipeline(job: Job) -> None:
    try:
        cfg = job.config
        paper_path = cfg["paper_path"]
        questions = cfg["questions"]
        api_keys = cfg.get("api_keys", {})  # {provider: key}
        job.emit({
            "type": "start",
            "provider": cfg["provider"],
            "model": cfg["model"],
            "adjudicator": (
                None if not cfg["use_adjudicator"]
                else {"provider": cfg["adj_provider"], "model": cfg["adj_model"]}
            ),
            "runs": cfg["runs"],
            "use_domain_agent": cfg["use_domain_agent"],
            "question_count": len(questions),
        })

        paper = load_paper(paper_path)
        job.emit({
            "type": "paper_loaded",
            "path": str(paper_path),
            "char_count": len(paper),
        })

        qa_client = LLMClient(
            provider=cfg["provider"],
            model=cfg["model"],
            api_key=api_keys.get(cfg["provider"]) or None,
            max_tokens=cfg.get("max_tokens", 4096),
        )

        all_runs, briefings = run_qa(
            qa_client,
            paper,
            questions,
            runs=cfg["runs"],
            use_domain_agent=cfg["use_domain_agent"],
            max_searches_per_domain_agent=cfg["max_searches"],
            verbose=False,
            on_event=job.emit,
        )

        grouped = group_runs_by_question(all_runs, n_questions=len(questions))
        adjudicated = None
        if cfg["use_adjudicator"] and cfg["runs"] > 0:
            adj_client = qa_client
            if cfg["adj_provider"] != cfg["provider"] or cfg["adj_model"] != cfg["model"]:
                adj_client = LLMClient(
                    provider=cfg["adj_provider"],
                    model=cfg["adj_model"],
                    api_key=api_keys.get(cfg["adj_provider"]) or None,
                    max_tokens=cfg.get("max_tokens", 4096),
                )
            adjudicated = adjudicate_all(
                adj_client, paper, questions, grouped,
                verbose=False, on_event=job.emit,
            )

        result = {
            "paper": str(paper_path),
            "provider": cfg["provider"],
            "model": cfg["model"],
            "runs": cfg["runs"],
            "domain_agent": {
                "enabled": cfg["use_domain_agent"],
                "max_searches_per_run": cfg["max_searches"],
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
            "adjudicator": (
                None if not cfg["use_adjudicator"]
                else {"provider": cfg["adj_provider"], "model": cfg["adj_model"]}
            ),
            "questions": [
                {
                    "qid": i + 1,
                    "question": q,
                    "runs": grouped[i],
                    "final": (
                        None if adjudicated is None
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
        job.finish(result=result)
    except Exception as e:
        tb = traceback.format_exc()
        job.finish(error=f"{e}\n\n{tb}")


# ---------------------------------------------------------------------------
# FastAPI app factory.
# ---------------------------------------------------------------------------

def create_app():
    _require_fastapi()
    from fastapi import FastAPI, Form, HTTPException, UploadFile, File
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="BioExtractAI", version="0.1.0")

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/config")
    def api_config():
        available = [p for p in SUPPORTED_PROVIDERS if os.environ.get(ENV_KEYS[p])]
        return {
            "providers": list(SUPPORTED_PROVIDERS),
            "default_models": DEFAULT_MODELS,
            "env_keys": ENV_KEYS,
            "available_providers": available,
            "model_catalog": MODEL_CATALOG,
        }

    @app.post("/api/run")
    async def api_run(
        paper_file: Optional[UploadFile] = File(None),
        paper_path: Optional[str] = Form(None),
        questions_text: Optional[str] = Form(None),
        questions_file: Optional[UploadFile] = File(None),
        provider: Optional[str] = Form(None),
        model: Optional[str] = Form(None),
        runs: int = Form(3),
        use_adjudicator: bool = Form(True),
        adjudicator_provider: Optional[str] = Form(None),
        adjudicator_model: Optional[str] = Form(None),
        use_domain_agent: bool = Form(True),
        max_searches: int = Form(5),
        max_tokens: int = Form(4096),
        openai_api_key: Optional[str] = Form(None),
        anthropic_api_key: Optional[str] = Form(None),
        deepseek_api_key: Optional[str] = Form(None),
        api_key: Optional[str] = Form(None),  # deprecated single-key field, kept for compat
    ):
        # ---- Resolve paper source ----
        resolved_paper_path: Optional[Path] = None
        cleanup_paths = []
        if paper_file is not None and paper_file.filename:
            suffix = Path(paper_file.filename).suffix or ".txt"
            data = await paper_file.read()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(data)
            tmp.close()
            resolved_paper_path = Path(tmp.name)
            cleanup_paths.append(resolved_paper_path)
        elif paper_path:
            resolved_paper_path = Path(paper_path).expanduser()
            if not resolved_paper_path.exists():
                raise HTTPException(400, f"paper_path not found on server: {resolved_paper_path}")
        else:
            raise HTTPException(400, "Provide either paper_file or paper_path.")

        # ---- Resolve questions source ----
        questions_list = None
        if questions_text and questions_text.strip():
            lines = [ln for ln in questions_text.splitlines() if ln.strip()]
            questions_list = load_questions(lines)
        elif questions_file is not None and questions_file.filename:
            data = await questions_file.read()
            text = data.decode("utf-8", errors="replace")
            questions_list = load_questions(text.splitlines())
        else:
            raise HTTPException(400, "Provide either questions_text or questions_file.")

        # ---- Resolve provider/model (reuse CLI logic for consistency) ----
        try:
            resolved_provider, resolved_model = _resolve_provider_model(provider, model)
            adj_provider, adj_model = _resolve_provider_model(
                adjudicator_provider, adjudicator_model,
                fallback_provider=resolved_provider, fallback_model=resolved_model,
            )
        except SystemExit as e:
            raise HTTPException(400, str(e))

        # Per-provider API keys. The legacy single `api_key` is applied to
        # whichever provider was resolved for QA (and to the adjudicator when
        # it matches). Per-provider fields always override the legacy one.
        api_keys = {
            "openai": (openai_api_key or "").strip() or None,
            "anthropic": (anthropic_api_key or "").strip() or None,
            "deepseek": (deepseek_api_key or "").strip() or None,
        }
        if api_key and not api_keys[resolved_provider]:
            api_keys[resolved_provider] = api_key.strip()

        cfg = {
            "paper_path": resolved_paper_path,
            "questions": questions_list,
            "provider": resolved_provider,
            "model": resolved_model,
            "runs": max(0, int(runs)),
            "use_adjudicator": bool(use_adjudicator),
            "adj_provider": adj_provider,
            "adj_model": adj_model,
            "use_domain_agent": bool(use_domain_agent),
            "max_searches": max(0, int(max_searches)),
            "max_tokens": max(256, int(max_tokens)),
            "api_keys": api_keys,
            "_cleanup_paths": cleanup_paths,
        }

        job = Job(job_id=str(uuid.uuid4()), config=cfg)
        _register_job(job)
        t = threading.Thread(target=_run_pipeline, args=(job,), daemon=True)
        t.start()

        return {"job_id": job.job_id}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        job = _get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {
            "job_id": job_id,
            "done": job.done.is_set(),
            "error": job.error,
            "result": job.result,
        }

    @app.get("/api/jobs/{job_id}/download.json")
    def api_download_json(job_id: str):
        from fastapi.responses import Response
        job = _get_job(job_id)
        if job is None or job.result is None:
            raise HTTPException(404, "job not found or not finished")
        body = json.dumps(job.result, indent=2, ensure_ascii=False).encode("utf-8")
        filename = _result_filename(job.result, ".json")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/jobs/{job_id}/download.xlsx")
    def api_download_xlsx(job_id: str):
        from fastapi.responses import Response
        job = _get_job(job_id)
        if job is None or job.result is None:
            raise HTTPException(404, "job not found or not finished")
        try:
            body = _result_to_xlsx_bytes(job.result)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        filename = _result_filename(job.result, ".xlsx")
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/jobs/{job_id}/stream")
    def api_stream(job_id: str):
        job = _get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")

        def gen():
            yield f"data: {json.dumps({'type': 'hello', 'job_id': job_id})}\n\n"
            while True:
                try:
                    evt = job.events.get(timeout=30.0)
                except queue.Empty:
                    # heartbeat keeps proxies from buffering
                    yield ": keep-alive\n\n"
                    if job.done.is_set() and job.events.empty():
                        break
                    continue
                if evt is _SENTINEL:
                    break
                yield f"data: {json.dumps(evt, default=str)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering if present
            },
        )

    return app


# ---------------------------------------------------------------------------
# CLI entry point (registered as the ``bioextract-web`` console script).
# ---------------------------------------------------------------------------

def cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bioextract-web",
        description="Run the BioExtractAI pipeline through a local web UI.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=8000,
        help="port to listen on (default 8000; auto-rolls to the next free port if busy, "
             "unless you also pass --strict-port).",
    )
    parser.add_argument(
        "--strict-port", action="store_true",
        help="fail instead of rolling to a higher port when --port is already in use.",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on source changes (dev)")
    args = parser.parse_args(argv)

    _require_fastapi()
    import uvicorn

    port = _pick_port(args.host, args.port, strict=args.strict_port)
    if port is None:
        return 1

    print(f"BioExtractAI web UI: http://{args.host}:{port}")
    if args.reload:
        uvicorn.run(
            "bioextract.server:create_app",
            host=args.host, port=port, reload=True, factory=True,
        )
    else:
        uvicorn.run(create_app(), host=args.host, port=port)
    return 0


def _pick_port(host: str, preferred: int, strict: bool, max_tries: int = 10) -> Optional[int]:
    """Return the first free port at or above ``preferred``.

    If ``strict`` is set and the preferred port is busy, print the conflict and
    return None. Otherwise, scan up to ``max_tries`` ports and report which
    holder is keeping the preferred port so the user can kill it.
    """
    import socket

    def is_free(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
            except OSError:
                return False
            return True

    if is_free(preferred):
        return preferred

    holder = _describe_port_holder(preferred)
    if strict:
        print(f"error: port {preferred} is already in use{holder}", file=__import__("sys").stderr)
        print("       kill the process above or re-run with --port <other>.", file=__import__("sys").stderr)
        return None

    for p in range(preferred + 1, preferred + 1 + max_tries):
        if is_free(p):
            print(f"note: port {preferred} was in use{holder}; using {p} instead.")
            return p

    print(
        f"error: port {preferred} and the next {max_tries} were all busy{holder}. "
        f"Pass --port <other>.",
        file=__import__("sys").stderr,
    )
    return None


def _result_filename(result: dict, ext: str) -> str:
    """Best-effort filename like ``bioextract_20008779.xlsx``."""
    import re
    paper = str(result.get("paper") or "result")
    stem = Path(paper).stem or "result"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:60] or "result"
    return f"bioextract_{stem}{ext}"


def _result_to_xlsx_bytes(result: dict) -> bytes:
    """Serialize a result dict to XLSX (Answers + Domain_Agent sheets).

    Raises ``RuntimeError`` if pandas/openpyxl aren't installed.
    """
    try:
        import pandas as pd  # noqa: F401
        import openpyxl  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "XLSX export needs 'pandas' and 'openpyxl'. Install with: pip install pandas openpyxl"
        ) from e
    import io

    import pandas as pd

    buf = io.BytesIO()
    rows = []
    for q in result["questions"]:
        row = {"QID": q["qid"], "Question": q["question"]}
        for i, run in enumerate(q["runs"], 1):
            row[f"Answer_{i}"] = run.get("answer", "")
            row[f"Evidence_{i}"] = run.get("evidence", "")
            row[f"Rationale_{i}"] = run.get("rationale", "")
        if q.get("final"):
            row["Final_Answer"] = q["final"]["answer"]
            row["Final_Rationale"] = q["final"]["rationale"]
            row["Final_Confidence"] = q["final"]["confidence"]
        rows.append(row)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Answers", index=False)
        domain = result.get("domain_agent") or {}
        if domain.get("enabled"):
            brief_rows = []
            for b in domain.get("briefings") or []:
                if b is None:
                    continue
                brief_rows.append({
                    "Run": b["run"],
                    "Persona": b["persona"],
                    "Used_Web_Search": b["used_web_search"],
                    "Web_Search_Note": b.get("web_search_note") or "",
                    "Search_Queries": "\n".join(b.get("search_queries") or []),
                    "Briefing_Text": b.get("briefing_text") or "",
                })
            if brief_rows:
                pd.DataFrame(brief_rows).to_excel(
                    writer, sheet_name="Domain_Agent", index=False
                )
    return buf.getvalue()


def _describe_port_holder(port: int) -> str:
    """Best-effort ``" (held by python3 PID 1234)"`` suffix for error messages."""
    import shutil
    import subprocess

    if not shutil.which("lsof"):
        return ""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
            stderr=subprocess.DEVNULL, text=True, timeout=1.5,
        )
    except Exception:
        return ""
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("COMMAND")]
    if not lines:
        return ""
    parts = lines[0].split()
    if len(parts) < 2:
        return ""
    return f" (held by {parts[0]} PID {parts[1]})"


if __name__ == "__main__":
    raise SystemExit(cli_main())
