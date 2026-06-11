"""CV/JD extraction and interview scoring — JSON-producing LLM calls."""

from typing import Optional

from core.llm_json import json_completion
from core.prompts import load_prompt
from schemas.analysis import ExtractCVJDRequest, ScoreInterviewRequest


def extract_cvjd(client, req: ExtractCVJDRequest) -> Optional[str]:
    """Analyze CV and JD to extract structured profile, ATS score, and gap analysis."""
    user_prompt = f"""Analyze the following CV and Job Description, then return a JSON object with these exact fields:

{{
  "fullName": "candidate's full name from CV",
  "targetPosition": "position title from JD",
  "techStack": ["list of technical skills from CV"],
  "mustHaveSkills": ["required skills from JD"],
  "educationLevel": "education requirement from JD",
  "keyResponsibilities": ["3 key responsibilities from JD"],
  "atsComponents": {{
    "S_skill": <0-100 score based on how well CV skills match JD requirements>,
    "S_exp": <0-100 score based on experience level match>,
    "S_edu": <0-100 score based on education match>,
    "S_sem": <0-100 semantic similarity between CV and JD>
  }},
  "readinessSignals": [
    {{"key": "domain_knowledge", "label": "Domain Knowledge", "dimension": "Domain & Technical", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "technical_stack_match", "label": "Technical Stack Match", "dimension": "Domain & Technical", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "experience_relevance", "label": "Experience Relevance", "dimension": "Domain & Technical", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "seniority_signal", "label": "Seniority Signal", "dimension": "Domain & Technical", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "communication_exposure", "label": "Communication Exposure", "dimension": "Communication", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "structured_output_signal", "label": "Structured Output Signal", "dimension": "Structured Thinking", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "initiative_signal", "label": "Initiative Signal", "dimension": "Initiative & Ownership", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}},
    {{"key": "collaboration_signal", "label": "Collaboration Signal", "dimension": "Team Collaboration", "score": <0-100>, "status": "strong|moderate|weak|missing", "cvEvidence": "CV evidence", "jdRequirement": "JD requirement"}}
  ],
  "baselineDimensions": {{
    "D1_technical": <0-100 score from domain, stack, experience, and seniority signals>,
    "D2_communication": <0-100 score from communication exposure>,
    "D3_problem_solving": <0-100 score from structured output/project signal>,
    "D4_attitude": <0-100 score from initiative and ownership signal>,
    "D5_cultural_fit": <0-100 score from collaboration/team/process fit signal>
  }},
  "gapAnalysis": [
    {{
      "category": "Domain & Technical",
      "score": <0-100>,
      "skills": [
        {{"skill": "skill name", "status": "matched|partial|missing", "cvEvidence": "evidence from CV", "jdRequirement": "requirement from JD"}}
      ]
    }},
    {{
      "category": "Communication",
      "score": <0-100>,
      "skills": [...]
    }},
    {{
      "category": "Structured Thinking",
      "score": <0-100>,
      "skills": [...]
    }},
    {{
      "category": "Initiative & Ownership",
      "score": <0-100>,
      "skills": [...]
    }},
    {{
      "category": "Team Collaboration",
      "score": <0-100>,
      "skills": [...]
    }}
  ]
}}

Gap analysis rules:
- Use exactly these final report categories: Domain & Technical, Communication, Structured Thinking, Initiative & Ownership, Team Collaboration.
- Do not create separate "Education", "Experience", "Soft Skills", or "Experience & Education" gap categories.
- Education is assessed only through atsComponents.S_edu and the educationLevel field.
- Experience should be captured through experience_relevance and seniority_signal, then rolled into Domain & Technical.
- Structured Thinking should assess project structure, output clarity, decomposition, data/API flow, and evidence of systematic work.
- Initiative & Ownership should assess self-learning, side projects, leadership, delivery ownership, and follow-through.
- Team Collaboration should assess team/project collaboration, stakeholder/client exposure, adaptability, and process fit.
- If a signal is not present in the CV, score it low and mark it missing or weak instead of inventing evidence.

CV Content:
{req.cv_text}

JD Content:
{req.jd_text}

Return ONLY the JSON object."""

    return json_completion(client, load_prompt("cv_extractor"), user_prompt, max_tokens=3000)


def score_interview(client, req: ScoreInterviewRequest) -> Optional[str]:
    """Score interview Q&A pairs across 5 dimensions and return detailed analysis."""
    qa_text = "\n\n".join(
        f"Q{i + 1} [{qa.get('timestamp', '00:00')}]: {qa['question']}\nA{i + 1}: {qa['answer']}"
        for i, qa in enumerate(req.qa_pairs)
    )
    event_text = "\n".join(
        f"{i + 1}. {getattr(event, 'role', 'user')}: {getattr(event, 'text', '')}"
        for i, event in enumerate(req.transcript_events or [])
    )

    user_prompt = f"""Analyze these interview Q&A pairs for a "{req.position}" position and return a JSON object.

Interview Transcript:
{qa_text}

Realtime Event Transcript:
{event_text or "No separate event transcript was provided."}

Return this exact JSON structure:
{{
  "dimensions": {{
    "D1_technical": <0-100 domain and technical depth score>,
    "D2_communication": <0-100 communication clarity score>,
    "D3_problem_solving": <0-100 structured thinking score>,
    "D4_attitude": <0-100 initiative and ownership score>,
    "D5_cultural_fit": <0-100 team collaboration score>
  }},
  "phase2Signals": [
    {{"key": "conceptual_depth", "label": "Conceptual Depth", "dimension": "Domain & Technical", "score": <0-100>, "evidence": "specific transcript evidence"}},
    {{"key": "coding_assessment", "label": "Coding Assessment", "dimension": "Domain & Technical", "score": <0-100>, "evidence": "specific transcript evidence"}},
    {{"key": "communication_clarity", "label": "Communication Clarity", "dimension": "Communication", "score": <0-100>, "evidence": "specific transcript evidence"}},
    {{"key": "structured_thinking", "label": "Structured Thinking", "dimension": "Structured Thinking", "score": <0-100>, "evidence": "specific transcript evidence"}},
    {{"key": "ownership_evidence", "label": "Ownership Evidence", "dimension": "Initiative & Ownership", "score": <0-100>, "evidence": "specific transcript evidence"}},
    {{"key": "collaboration_story", "label": "Conflict & Collaboration Story", "dimension": "Team Collaboration", "score": <0-100>, "evidence": "specific transcript evidence"}}
  ],
  "scoredQA": [
    {{"question": "...", "answer": "...", "timestamp": "...", "score": <0-100>}}
  ],
  "interviewQuestions": [
    {{
      "id": <number>,
      "question": "the question",
      "userAnswer": "the answer",
      "strengths": ["strength1", "strength2", "strength3"],
      "weaknesses": ["weakness1", "weakness2"],
      "optimalAnswer": "what an ideal answer would include"
    }}
  ]
}}

Dimension rules:
- D1 Domain & Technical: conceptual correctness, coding/system understanding, domain relevance.
- D2 Communication: clarity, concision, structure, ambiguity handling.
- D3 Structured Thinking: decomposition, tradeoffs, examples, debugging or problem-solving process.
- D4 Initiative & Ownership: self-direction, follow-through, responsibility, learning behavior.
- D5 Team Collaboration: teamwork, conflict handling, stakeholder awareness, process fit.
- Use realtime events to preserve exact conversation order. Use Q&A pairs as a compact fallback.

Return ONLY the JSON object."""

    return json_completion(client, load_prompt("interview_scorer"), user_prompt, max_tokens=3000)
