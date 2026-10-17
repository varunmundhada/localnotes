"""Built-in note templates and a title-based guess for which one fits a meeting."""
import re

BUILTIN = [
    {
        "id": "general", "name": "General meeting", "icon": "file",
        "description": "Summary, discussion, decisions and action items.",
        "prompt": """Sections, in this order:
## Key points — the main topics, grouped under short sub-headings if there were several
## Decisions — what was agreed (omit if nothing was decided)
## Open questions — unresolved items (omit if none)
## Action items""",
    },
    {
        "id": "one-on-one", "name": "1:1", "icon": "users",
        "description": "Updates, wins, blockers, feedback and follow-ups.",
        "prompt": """This is a 1:1 between two people. Sections, in this order:
## Updates — what each person is working on
## Wins — things that went well
## Blockers & concerns — problems raised, and any help asked for
## Feedback — feedback given in either direction (omit if none)
## Career & growth — only if discussed
## Action items""",
    },
    {
        "id": "sales", "name": "Customer / sales call", "icon": "chat",
        "description": "Customer needs, pains, budget, objections, next steps.",
        "prompt": """This is a call with a customer or prospect. Sections, in this order:
## Customer context — who they are, team size, current setup
## Needs & pain points — in the customer's own words where possible
## Budget, timeline & decision process — who decides, when, and with what budget (say "not discussed" if absent)
## Objections & concerns
## What we committed to
## Next steps — dated where possible
## Action items""",
    },
    {
        "id": "standup", "name": "Standup", "icon": "clock",
        "description": "Per person: yesterday, today, blockers.",
        "prompt": """This is a team standup. Write one short section per person (use their speaker label as the heading):
- **Done:** …
- **Next:** …
- **Blockers:** … (or "None")
Then:
## Team blockers — anything needing someone outside the speaker
## Action items""",
    },
    {
        "id": "interview", "name": "Interview", "icon": "users",
        "description": "Candidate background, answers, strengths, concerns.",
        "prompt": """This is a job interview. Sections, in this order:
## Candidate background
## Questions & answers — each question asked, with a one-to-two line summary of the answer
## Strengths — with evidence from the conversation
## Concerns — with evidence; do not speculate beyond what was said
## Candidate's questions
## Next steps
Do not make a hiring recommendation.""",
    },
    {
        "id": "project", "name": "Project review", "icon": "lines",
        "description": "Status, progress, risks, decisions, owners.",
        "prompt": """This is a project status review. Sections, in this order:
## Status — one line: on track / at risk / off track, and why
## Progress since last time
## Risks & issues — with owner and mitigation where mentioned
## Decisions
## Timeline changes — dates that moved, old → new
## Action items""",
    },
    {
        "id": "brainstorm", "name": "Brainstorm / planning", "icon": "sparkle",
        "description": "Ideas grouped by theme, favourites, next experiments.",
        "prompt": """This is a brainstorm or planning session. Sections, in this order:
## Goal — what the session was trying to solve
## Ideas — grouped by theme, one bullet per idea with who suggested it
## Favourites — ideas that got the most support, and why
## Parked — ideas set aside
## Next steps
## Action items""",
    },
    {
        "id": "lecture", "name": "Lecture / training", "icon": "file",
        "description": "Concepts, explanations, examples, questions.",
        "prompt": """This is a lecture, webinar or training session. Sections, in this order:
## Topics covered
## Key concepts — each with a plain-language explanation
## Examples given
## Questions asked — with the answers
## To review or try""",
    },
]

_RULES = [
    ("one-on-one", r"\b1\s*[:/-]\s*1\b|\bone[- ]on[- ]one\b|\bcheck[- ]?in\b|\bsync with\b"),
    ("standup", r"\bstand[- ]?up\b|\bdaily\b|\bscrum\b"),
    ("interview", r"\binterview|\bcandidate\b|\bscreen(ing)?\b"),
    ("sales", r"\bdemo\b|\bdiscovery\b|\bcustomer\b|\bclient\b|\bprospect\b|\bsales\b|\bcall with\b"),
    ("project", r"\breview\b|\bstatus\b|\bretro\b|\bsprint\b|\bproject\b"),
    ("brainstorm", r"\bbrainstorm|\bplanning\b|\bideation\b|\bworkshop\b|\boffsite\b"),
    ("lecture", r"\blecture\b|\bwebinar\b|\btraining\b|\bclass\b|\bcourse\b"),
]


def suggest(title: str, default: str = "general") -> str:
    t = title.lower()
    for tid, pattern in _RULES:
        if re.search(pattern, t):
            return tid
    return default
