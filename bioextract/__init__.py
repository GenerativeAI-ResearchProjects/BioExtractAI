"""BioExtractAI: multi-run QA + adjudication over scientific papers."""

__version__ = "0.1.0"

from .qa import run_qa
from .adjudicator import adjudicate
from .loaders import load_paper, load_questions

__all__ = ["run_qa", "adjudicate", "load_paper", "load_questions", "__version__"]
