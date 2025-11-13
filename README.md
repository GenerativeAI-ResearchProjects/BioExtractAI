#  AI-Assisted Scientific Paper Understanding and Evaluation

## Overview

This project investigates how artificial intelligence (AI) can **read scientific papers**, **extract key information**, and **answer domain-specific research questions**.  
Our system performs **three independent runs**, each generating detailed answers with **supporting evidence and rationale**.  
An **AI expert adjudicator** then reviews all three runs and selects the **best, most coherent answer** for each question.

The final goal is to assess how closely AI-derived outputs align with a **human-curated gold standard**, and to explore whether an adjudication-based approach can improve scientific comprehension accuracy.

---
## Significance and Objective



---
## Workflow Description

<p align="center">
  <img src="images/workflow_diagram.png" alt="Workflow Diagram" width="600"/>
</p>


### Data Preparation

- Collect and preprocess full-text scientific papers.

- Define structured, domain-specific question sets.

### AI Extraction & QA Runs (x3)

- Perform three independent runs using the same AI model.

- Each run reads the full text of the paper and produces:

    - Answer

    - Evidence (quoted text)

    - Rationale (model reasoning)

### AI Adjudication

- A separate “expert” AI model reviews the three sets of answers.

- It considers both the content and the rationale of each run.

- The adjudicator selects the best-supported or most reasonable answer.

- No scoring is performed — the outcome is a chosen final answer.

### Human Gold Standard Comparison

- The adjudicated answer is compared to the expert-annotated gold standard.

- Agreement rates and qualitative differences are analyzed.

---

## Results