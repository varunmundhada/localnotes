"""Turn a transcript plus the user's rough notes into meeting notes, with Groq (cloud) or a local Ollama model.

Models listed in cloud.CHAT_MODELS ("qwen/qwen3.8-27b", …) run on Groq; anything else is an Ollama model.
"""
import json
import re

import requests

import cloud

OLLAMA_URL = "http://127.0.0.1:11434"
CHUNK_CHARS = 24000     # ~6k tokens; longer transcripts are condensed in parts first
# Groq's free tier allows ~7K input / 8K total tokens a minute per model: keep each prompt well under.
CLOUD_PROMPT_TOKENS = 4500
CLOUD_RETRIES = 8       # rate-limit waits to sit through in one notes run (each up to two minutes)
# Each model has its own per-minute allowance, so long transcripts are condensed by two models taking turns.
CONDENSE_PARTNER = {"qwen/qwen3.8-27b": "openai/gpt-oss-120b"}
CLOUD_LENGTH = "Keep the notes under 450 words."   # the answer must fit the model's output allowance

_cloud: "cloud.Groq | None" = None


def use_cloud(client: "cloud.Groq | None"):
    global _cloud
    _cloud = client


def is_cloud(model: str) -> bool:
    return model in cloud.CHAT_MODELS


def estimate_tokens(text: str) -> int:
    """Rough upper bound: ~3 chars/token for Latin script, ~2.2 for Devanagari (measured on Groq's models)."""
    latin = sum(1 for c in text if ord(c) < 0x250)
    return int(latin / 3 + (len(text) - latin) / 2.2) + 1


def _budget(model: str, chars: int, tokens: int):
    """How much transcript fits in one request: (limit, measure) — characters locally, tokens in the cloud."""
    return (tokens, estimate_tokens) if is_cloud(model) else (chars, len)


def _tail(text: str, limit: int, measure) -> str:
    """The end of text that fits within limit (by measure), cut at a line break."""
    if measure(text) <= limit:
        return text
    kept, n = [], 0
    for ln in reversed(text.splitlines()):
        n += measure(ln) + 1
        if n > limit:
            break
        kept.append(ln)
    return "…(earlier part omitted; see notes)…\n" + "\n".join(reversed(kept))

SYSTEM = """You write concise, accurate meeting notes from a transcript.
"Me" is the user who recorded the meeting. Other speakers are labelled by name when known,
"Speaker 1", "Speaker 2"… when their voice wasn't recognised, or "Them" for anyone else on the call.
Attribute statements and action items to people by these labels. "Them" is a placeholder, not a person:
never write it as a name or owner — use a real name if the transcript gives one, otherwise "the other side".
Rules:
- Use the user's own notes as the backbone: keep their headings and points, and expand them with specifics from the transcript.
- Only state things supported by the transcript or the user's notes. Never invent names, numbers, or decisions.
- Write in Markdown. Use short bullet points.
- Write action items as a checklist: "- [ ] Owner: task". Only list tasks someone actually agreed or was asked
  to do in the meeting — never suggest tasks of your own. If there are none, write "None mentioned".
- Follow the requested structure exactly; omit a section only where it says it may be omitted."""

CONDENSE = """Condense this part of a meeting transcript into detailed bullet points.
Keep every decision, number, name, date, question, and commitment. Note who said what (Me/Them).
The transcript may be in Hindi, English or a mix; keep names and figures exactly as spoken.

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

Write the meeting notes now. {language}
Start with a one-line summary (no heading), then use this structure (translate the section headings too):
{structure}

Action items must be commitments actually made in the transcript (someone said they will do something, or was
asked to). Do not turn topics, opinions or facts into tasks. If nobody committed to anything, write only
"None mentioned" under the action items heading."""

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
Sign off with my name if known. {language}"""

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
                   tone: str = "friendly", on_token=None, language: str = "") -> str:
    prompt = FOLLOWUP.format(title=title, me_line=f"My name: {me_name}" if me_name else "",
                             tone=TONES.get(tone, TONES["friendly"]), notes=notes or "(no notes)",
                             transcript=_tail(transcript, *_budget(model, 12000, 2500)) or "(empty)",
                             language=language)
    return _clean_greeting(_stream(model, [{"role": "system", "content": FOLLOWUP_SYSTEM},
                                           {"role": "user", "content": prompt}], on_token))


ASK_SYSTEM = """You answer questions about one meeting using only its transcript and notes.
"Me" is the user; "Them" is everyone else. Be direct and concise; use Markdown bullets when listing.
If the answer isn't in the meeting, say so plainly. When asked to draft something (an email, a message), write it ready to send.
The meeting may be in Hindi, English or Hinglish. Answer in the language and script the question is asked in."""

ASK_CONTEXT_CHARS = 40000


def ask(model: str, title: str, user_notes: str, ai_notes: str, transcript: str,
        history: list[dict], question: str, on_token=None) -> str:
    """Answer a question about a meeting. history: prior [{role, content}] turns."""
    transcript = _tail(transcript, *_budget(model, ASK_CONTEXT_CHARS, 3000))
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


def _chat(model: str, prompt: str, on_token=None, on_wait=None) -> str:
    return _stream(model, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], on_token,
                   on_wait)


_thinking_models: dict[str, bool] = {}


def _can_think(model: str) -> bool:
    """Reasoning models (Qwen3, …) spend minutes "thinking" aloud unless told not to."""
    if model not in _thinking_models:
        try:
            info = requests.post(f"{OLLAMA_URL}/api/show", json={"model": model}, timeout=5).json()
            _thinking_models[model] = "thinking" in info.get("capabilities", [])
        except Exception:
            _thinking_models[model] = False
    return _thinking_models[model]


class ThinkFilter:
    """Drops <think>…</think> blocks from a token stream, even when a tag is split across tokens."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = ""
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-len(self.CLOSE):]
                    return out
                self.buf, self.inside = self.buf[i + len(self.CLOSE):], False
            else:
                i = self.buf.find(self.OPEN)
                if i >= 0:
                    out += self.buf[:i]
                    self.buf, self.inside = self.buf[i + len(self.OPEN):], True
                    continue
                j = self.buf.rfind("<")          # hold back what could be the start of "<think>"
                if j >= 0 and self.OPEN.startswith(self.buf[j:]):
                    out, self.buf = out + self.buf[:j], self.buf[j:]
                else:
                    out, self.buf = out + self.buf, ""
                return out

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


def _stream(model: str, messages: list[dict], on_token=None, on_wait=None) -> str:
    if is_cloud(model):
        if _cloud is None:
            raise cloud.AuthError("Cloud AI isn't set up — add a Groq API key in Settings → Cloud AI.")
        # A rate-limit refusal comes before any text is streamed, so retrying never repeats output.
        return cloud.with_retries(lambda: _cloud.chat(model, messages, on_token).strip(),
                                  attempts=CLOUD_RETRIES, on_wait=on_wait)
    payload = {"model": model, "stream": True, "messages": messages,
               "options": {"num_ctx": 16384, "temperature": 0.2}}
    if _can_think(model):
        payload["think"] = False
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=(5, 600))
    resp.raise_for_status()
    out, think = [], ThinkFilter()
    started = False

    def emit(text: str):
        nonlocal started
        if not started:                          # drop the blank lines a model leaves after its thinking
            text = text.lstrip()
            started = bool(text)
        if text:
            out.append(text)
            if on_token:
                on_token(text)

    for line in resp.iter_lines():
        if not line:
            continue
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        piece = msg.get("message", {}).get("content", "")
        if piece:
            emit(think.feed(piece))
        if msg.get("done"):
            break
    emit(think.flush())
    text = "".join(out)
    # Thinking-only models put "<think>" in the prompt template, so the stream carries just the closing
    # tag: everything before it is reasoning, not answer.
    if ThinkFilter.CLOSE in text:
        text = text.rsplit(ThinkFilter.CLOSE, 1)[1].lstrip()
    return text


def generate_notes(model: str, title: str, user_notes: str, transcript: str,
                   on_token=None, on_progress=None, me_name: str = "", structure: str = "",
                   language: str = "") -> str:
    on_progress = on_progress or (lambda s: None)
    if not transcript.strip() and not user_notes.strip():
        return "_Nothing was recorded or written for this meeting._"
    label = "Full transcript"
    limit, measure = _budget(model, CHUNK_CHARS, max(1500, CLOUD_PROMPT_TOKENS - estimate_tokens(user_notes)))

    def wait(seconds):
        on_progress(f"Waiting {seconds:.0f}s for the free-tier rate limit…")

    for _ in range(3):        # condensed parts can themselves be too long for one request: condense again
        if measure(transcript) <= limit:
            break
        chunks = _split(transcript, limit, measure)
        parts = []
        models = [model, CONDENSE_PARTNER.get(model, model)] if is_cloud(model) else [model]
        for i, chunk in enumerate(chunks, 1):
            on_progress(f"Condensing transcript part {i}/{len(chunks)}…")
            parts.append(_chat(models[i % len(models)], CONDENSE.format(i=i, n=len(chunks), chunk=chunk), on_wait=wait))
        transcript = "\n\n".join(parts)
        label = "Condensed transcript (bullet points per part, in order)"
    on_progress("Writing notes…")
    return _chat(
        model,
        FINAL.format(title=title, me_line=f'"Me" in the transcript is {me_name}.\n' if me_name else "",
                     notes=user_notes.strip() or "(none)", label=label,
                     structure=structure.strip() or DEFAULT_STRUCTURE,
                     language=f"{language} {CLOUD_LENGTH}".strip() if is_cloud(model) else language,
                     transcript=transcript or "(no audio transcribed)"),
        on_token=on_token, on_wait=wait,
    )


def _split(text: str, size: int, measure=len) -> list[str]:
    lines, chunks, cur, n = text.splitlines(), [], [], 0
    for ln in lines:
        if n + measure(ln) > size and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += measure(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
