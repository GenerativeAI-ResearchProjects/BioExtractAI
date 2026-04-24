"""Load paper content and question sets from disk."""

from pathlib import Path
from typing import List, Union

PathLike = Union[str, Path]


def load_paper(path: PathLike) -> str:
    """Return paper text from a file or directory.

    Supported inputs:
      - .md / .txt: read as UTF-8 text
      - .pdf: extract text via ``pypdf``
      - directory: prefer ``*.checked.md``, then ``*.md``, then ``*.txt``, then
        ``*.pdf`` (first match wins).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Paper path not found: {p}")

    if p.is_dir():
        p = _resolve_dir(p)

    suffix = p.suffix.lower()
    if suffix in {".md", ".txt"}:
        return p.read_text(encoding="utf-8")
    if suffix == ".pdf":
        return _read_pdf(p)
    raise ValueError(
        f"Unsupported paper format: {p.suffix}. Use .md, .txt, .pdf, or a directory."
    )


def _resolve_dir(d: Path) -> Path:
    for pattern in ("*.checked.md", "*.md", "*.txt", "*.pdf"):
        matches = sorted(d.glob(pattern))
        if matches:
            return matches[0]
    raise FileNotFoundError(
        f"No paper file found in {d} (looked for *.checked.md, *.md, *.txt, *.pdf)"
    )


def _read_pdf(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - optional dep
        raise RuntimeError(
            "Reading PDFs requires the 'pypdf' package. Install with: pip install pypdf"
        ) from e
    reader = PdfReader(str(p))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n\n".join(pages).strip()


def load_questions(source: Union[PathLike, List[str]]) -> List[str]:
    """Return a list of question strings.

    Accepts either a path to a text file (one question per line) or an already-
    parsed list of strings. Leading numbering like ``"1:"`` or ``"Q1."`` is
    stripped but preserved in the display order.
    """
    if isinstance(source, list):
        raw_lines = source
    else:
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"Questions file not found: {p}")
        raw_lines = p.read_text(encoding="utf-8").splitlines()

    questions = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        questions.append(_strip_leading_number(line))
    if not questions:
        raise ValueError("No questions found (file was empty or all comments).")
    return questions


def _strip_leading_number(line: str) -> str:
    """Remove leading ``"12:"`` / ``"Q12."`` / ``"12. "`` style numbering."""
    import re

    return re.sub(r"^\s*[Qq]?\d+\s*[:.\)]\s*", "", line).strip()


def format_questions_block(questions: List[str]) -> str:
    """Render questions as ``"1: ..."`` lines for the QA prompt."""
    return "\n".join(f"{i}: {q}" for i, q in enumerate(questions, 1))
