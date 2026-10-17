"""Turn a transcript plus the user's rough notes into meeting notes via a local Ollama model."""
import json
import re

import requests

OLLAMA_URL = "http://127.0.0.1:11434"
CHUNK_CHARS = 24000     # ~6k tokens; longer transcripts are condensed in parts first

SYSTEM = """You write concise, accurate meeting notes from a transcript.
"Me" is the user who recorded the meeting. Other speakers are labelled by name when known,
"Speaker 1", "Speaker 2"… when their voice wasn't recognised, or "Them" for anyone else on the call.
Attribute statements and action items to people by these labels. "Them" is a placeholder, not a person:
never write it as a name or owner — use a real name if the transcript gives one, otherwise "the other side".
Rules:
- Use the user's own notes as the backbone: keep their headings and points, and expand them with specifics from the transcript.
- Only state things supported by the transcript or the user's notes. Never invent names, numbers, or decisions.
- Write in Markdown. Use short bullet points.
- Write action items as a checklist: "- [ ] Owner: task". If there are none, write "None mentioned".
- Follow the requested structure exactly; omit a section only where it says it may be omitted."""

CONDENSE = """Condense this part of a meeting transcript into detailed bullet points.
Keep every decision, number, name, date, question, and commitment. Note who said what (Me/Them).

Transcript part {i} of {n}:
{chunk}"""

FINAL = """Meeting title: {title}
{me_line}
The user's rough notes taken during the meeting:
<user_notes>
{notes}
</user_notes>

{label}:
<transcript>
{transcript}
</transcript>

Write the meeting notes now. Start with a one-line summary (no heading), then use this structure:
{structure}"""

DEFAULT_STRUCTURE = """## Key points
## Decisions (omit if none)
## Action items"""

FOLLOWUP_SYSTEM = """You write follow-up emails after meetings, on behalf of the user ("Me").
Use only facts from the meeting. Never invent commitments, dates or numbers.
"Them" and "Speaker N" are placeholder labels, not names: never greet or mention them by those labels.
Greet people by real names that appear in the meeting, or with "Hi all" if no names are known.
Output exactly this format and nothing else:
Subject: <subject line>

<email body>"""

FOLLOWUP = """Meeting: {title}
{me_line}
Tone: {tone}

<notes>
{notes}
</notes>

<transcript_excerpt>
{transcript}
</transcript_excerpt>

Write the follow-up email from me to the other attendees: thank them briefly, recap the key points and decisions,
list action items with owners (and dates if mentioned), and state the next step. Keep it under 200 words.
Sign off with my name if known."""

TONES = {
    "friendly": "warm and friendly, plain language",
    "formal": "professional and formal",
    "brief": "very short and to the point — bullet points, no pleasantries beyond one line",
}


PLACEHOLDER = r"(?:Them|Speaker \d+)"


def _clean_greeting(email: str) -> str:
    """Small models still greet the transcript's placeholder labels ("Hi Priya, Them,"); strip them."""
    lines = email.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*(?:Hi|Hello|Hey|Dear)\b)(.*?)([,!:]?\s*)$", line, re.I)
        if not m or not re.search(rf"\b{PLACEHOLDER}\b", m.group(2)):
            continue
        names = [n.strip() for n in re.split(r",|\band\b|&", m.group(2))
                 if n.strip() and not re.fullmatch(PLACEHOLDER, n.strip())]
        who = " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]
        lines[i] = f"{m.group(1)} {who or 'all'}{m.group(3).rstrip() or ','}"
        break
    return "\n".join(lines)


def clean_notes(md: str) -> str:
    """Never present the "Them" placeholder as an owner."""
    md = re.sub(r"(Owner:\s*)Them\b", r"\1the other side", md)
    return re.sub(r"^(\s*-\s*\[[ xX]\]\s*)Them\s*:", r"\1The other side:", md, flags=re.M)


def followup_email(model: str, title: str, notes: str, transcript: str, me_name: str = "",
                   tone: str = "friendly", on_token=None) -> str:
    prompt = FOLLOWUP.format(title=title, me_line=f"My name: {me_name}" if me_name else "",
                             tone=TONES.get(tone, TONES["friendly"]), notes=notes or "(no notes)",
                             transcript=transcript[-12000:] or "(empty)")
    return _clean_greeting(_stream(model, [{"role": "system", "content": FOLLOWUP_SYSTEM},
                                           {"role": "user", "content": prompt}], on_token))


ASK_SYSTEM = """You answer questions about one meeting using only its transcript and notes.
"Me" is the user; "Them" is everyone else. Be direct and concise; use Markdown bullets when listing.
If the answer isn't in the meeting, say so plainly. When asked to draft something (an email, a message), write it ready to send."""

ASK_CONTEXT_CHARS = 40000


def ask(model: str, title: str, user_notes: str, ai_notes: str, transcript: str,
        history: list[dict], question: str, on_token=None) -> str:
    """Answer a question about a meeting. history: prior [{role, content}] turns."""
    if len(transcript) > ASK_CONTEXT_CHARS:
        transcript = "…(earlier part omitted; see notes)…\n" + transcript[-ASK_CONTEXT_CHARS:]
    context = (f"Meeting: {title}\n\n<my_notes>\n{user_notes or '(none)'}\n</my_notes>\n\n"
               f"<ai_notes>\n{ai_notes or '(none)'}\n</ai_notes>\n\n<transcript>\n{transcript or '(empty)'}\n</transcript>")
    messages = [{"role": "system", "content": ASK_SYSTEM}, {"role": "user", "content": context},
                {"role": "assistant", "content": "I've read the meeting. What would you like to know?"}]
    messages += history[-6:] + [{"role": "user", "content": question}]
    return _stream(model, messages, on_token)


def ollama_status(model: str) -> dict:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
    except Exception:
        return {"running": False, "model_ready": False}
    names = {m["name"] for m in tags.get("models", [])}
    ready = model in names or f"{model}:latest" in names
    return {"running": True, "model_ready": ready, "models": sorted(names)}


def _chat(model: str, prompt: str, on_token=None) -> str:
    return _stream(model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], on_token)


def _stream(model: str, messages: list[dict], on_token=None) -> str:
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "stream": True,
            "messages": messages,
            "options": {"num_ctx": 16384, "temperature": 0.2},
        },
        stream=True,
        timeout=(5, 600),
    )
    resp.raise_for_status()
    out = []
    for line in resp.iter_lines():
        if not line:
            continue
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        piece = msg.get("message", {}).get("content", "")
        if piece:
            out.append(piece)
            if on_token:
                on_token(piece)
        if msg.get("done"):
            break
    return "".join(out)


def generate_notes(model: str, title: str, user_notes: str, transcript: str,
                   on_token=None, on_progress=None, me_name: str = "", structure: str = "") -> str:
    on_progress = on_progress or (lambda s: None)
    if not transcript.strip() and not user_notes.strip():
        return "_Nothing was recorded or written for this meeting._"
    label = "Full transcript"
    if len(transcript) > CHUNK_CHARS:
        chunks = _split(transcript, CHUNK_CHARS)
        parts = []
        for i, chunk in enumerate(chunks, 1):
            on_progress(f"Condensing transcript part {i}/{len(chunks)}…")
            parts.append(_chat(model, CONDENSE.format(i=i, n=len(chunks), chunk=chunk)))
        transcript = "\n\n".join(parts)
        label = "Condensed transcript (bullet points per part, in order)"
    on_progress("Writing notes…")
    return _chat(
        model,
        FINAL.format(title=title, me_line=f'"Me" in the transcript is {me_name}.\n' if me_name else "",
                     notes=user_notes.strip() or "(none)", label=label,
                     structure=structure.strip() or DEFAULT_STRUCTURE,
                     transcript=transcript or "(no audio transcribed)"),
        on_token=on_token,
    )


def _split(text: str, size: int) -> list[str]:
    lines, chunks, cur, n = text.splitlines(), [], [], 0
    for ln in lines:
        if n + len(ln) > size and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
