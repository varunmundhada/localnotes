"""Cloud mode (Groq) regression tests — all network calls are mocked; no key or internet needed.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="localnotes-cloudtests-"))
os.environ.setdefault("LOCALNOTES_DATA", str(TMP / "appdata"))   # must be set before importing app

import numpy as np  # noqa: E402
import requests  # noqa: E402

import app  # noqa: E402
import cloud  # noqa: E402
import languages  # noqa: E402
import summarize  # noqa: E402
import transcribe  # noqa: E402
from audio import _Segmenter, _WavTrack  # noqa: E402


def resp(status=200, body=None, headers=None, lines=None):
    r = mock.Mock(status_code=status, headers=headers or {}, text=json.dumps(body or {}))
    r.json = lambda: body or {}
    r.iter_lines = lambda: iter(lines or [])
    return r


def keystore(key="gsk_test_key_0123456789abcd") -> cloud.KeyStore:
    d = TMP / f"keys-{os.urandom(4).hex()}"
    d.mkdir()
    ks = cloud.KeyStore(d)
    if key:
        ks.set(key)
    return ks


def tone(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class FakeGroq:
    """Stands in for cloud.Groq. fail: an exception to raise from every call, or None to answer."""

    SENTENCES = ["The budget is approved for next quarter.", "I will send the deck by Friday.",
                 "Can we move the launch to March?", "Priya owns the hiring plan.", "Let's meet again on Monday."]

    def __init__(self, fail=None):
        self.fail = fail
        self.transcribed, self.chats = [], []

    def _check(self):
        if self.fail:
            raise self.fail

    def transcribe(self, audio, language=None, prompt=None):
        self._check()
        self.transcribed.append((len(audio) / 16000, language, prompt))
        return [{"start": 0.5, "end": 2.0, "text": " " + self.SENTENCES[(len(self.transcribed) - 1) % 5],
                 "no_speech_prob": 0.01, "avg_logprob": -0.2}]

    def chat(self, model, messages, on_token=None, **kw):
        self._check()
        self.chats.append((model, messages))
        out = "Quick summary.\n## Action items\nNone mentioned"
        if on_token:
            on_token(out)
        return out

    def ping(self):
        self._check()
        return {"ok": True, "transcribe": True, "chat_models": list(cloud.CHAT_MODELS)}


class TestKeyStore(unittest.TestCase):
    def test_import_encrypts_and_deletes_plain_file(self):
        ks = keystore(key=None)
        ks.import_path.write_text("  gsk_abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
        self.assertTrue(ks.import_plaintext())
        self.assertFalse(ks.import_path.exists())
        self.assertEqual(ks.get(), "gsk_abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn(b"gsk_", ks.path.read_bytes())          # stored encrypted, not in the clear
        self.assertEqual(ks.hint(), "…wxyz")
        ks.clear()
        self.assertIsNone(ks.get())
        self.assertFalse(ks.import_plaintext())                   # nothing to import


class TestGroqClient(unittest.TestCase):
    def setUp(self):
        self.g = cloud.Groq(keystore())

    def test_errors_map_to_what_the_caller_should_do(self):
        cases = [(resp(429, headers={"retry-after": "7"}), cloud.RateLimited), (resp(401), cloud.AuthError),
                 (resp(503), cloud.Offline), (resp(400, {"error": {"message": "bad"}}), cloud.CloudError)]
        for r, exc in cases:
            with self.subTest(status=r.status_code), mock.patch.object(cloud.requests, "post", return_value=r):
                with self.assertRaises(exc) as cm:
                    self.g.transcribe(tone(1))
                if exc is cloud.RateLimited:
                    self.assertEqual(cm.exception.retry_after, 7.0)
        # A request bigger than a minute's allowance is refused for good: report it, don't wait and retry.
        too_large = resp(429, {"error": {"message": "Request too large for model on output tokens per minute (OTPM): "
                                                    "Limit 1000, Requested 1268."}}, {"x-should-retry": "false"})
        with mock.patch.object(cloud.requests, "post", return_value=too_large):
            with self.assertRaises(cloud.CloudError) as cm:
                self.g.chat("qwen/qwen3.8-27b", [])
            self.assertNotIsInstance(cm.exception, cloud.RateLimited)
        with mock.patch.object(cloud.requests, "post", side_effect=requests.ConnectionError("dns")):
            self.assertRaises(cloud.Offline, self.g.transcribe, tone(1))
        with mock.patch.object(cloud.requests, "post", side_effect=requests.Timeout()):
            self.assertRaises(cloud.Offline, self.g.chat, "qwen/qwen3.8-27b", [])
        self.assertRaises(cloud.AuthError, cloud.Groq(keystore(key=None)).transcribe, tone(1))

    def test_wait_time_read_from_the_message_when_there_is_no_header(self):
        cases = {"Please try again in 1m2.5s.": 62.5, "Please try again in 850ms.": 0.85,
                 "Please try again in 12.34s.": 12.34, "Rate limit reached.": 30.0}
        for msg, secs in cases.items():
            with self.subTest(msg=msg), mock.patch.object(cloud.requests, "post",
                                                          return_value=resp(429, {"error": {"message": msg}})):
                with self.assertRaises(cloud.RateLimited) as cm:
                    self.g.chat("openai/gpt-oss-20b", [])
                self.assertAlmostEqual(cm.exception.retry_after, secs)

    def test_transcribe_splits_long_audio_and_offsets_times(self):
        calls = []

        def post(url, **kw):
            calls.append(kw)
            return resp(body={"segments": [{"start": 1.0, "end": 2.0, "text": " hi"}]})

        with mock.patch.object(cloud.requests, "post", side_effect=post):
            segs = self.g.transcribe(tone(cloud.MAX_UPLOAD_S + 30), language="hi", prompt="x" * 2000)
        self.assertEqual(len(calls), 2)
        self.assertEqual([s["start"] for s in segs], [1.0, cloud.MAX_UPLOAD_S + 1.0])
        self.assertEqual(calls[0]["data"]["language"], "hi")
        self.assertLessEqual(len(calls[0]["data"]["prompt"]), 800)
        self.assertEqual(calls[0]["data"]["model"], cloud.TRANSCRIBE_MODEL)

    def test_chat_streams_and_turns_off_reasoning(self):
        lines = [b"", b": keep-alive"] + [
            b"data: " + json.dumps({"choices": [{"delta": {"content": c}}]}).encode() for c in ["## No", "tes"]
        ] + [b"data: [DONE]"]
        bodies, tokens = [], []

        def post(url, json=None, **kw):
            bodies.append(json)
            return resp(lines=lines)

        with mock.patch.object(cloud.requests, "post", side_effect=post):
            self.assertEqual(self.g.chat("qwen/qwen3.8-27b", [], tokens.append), "## Notes")
            self.g.chat("openai/gpt-oss-120b", [])
        self.assertEqual(tokens, ["## No", "tes"])
        self.assertEqual(bodies[0]["reasoning_effort"], "none")
        self.assertFalse(bodies[1]["include_reasoning"])
        self.assertEqual(bodies[0]["max_tokens"], 1000)    # Qwen's free tier refuses requests that may output more
        self.assertLessEqual(bodies[1]["max_tokens"], cloud.TOKENS_PER_MINUTE)

    def test_with_retries_waits_out_short_limits_only(self):
        n = {"calls": 0}

        def flaky():
            n["calls"] += 1
            if n["calls"] < 3:
                raise cloud.RateLimited(2)
            return "ok"

        with mock.patch.object(cloud.time, "sleep") as sleep:
            self.assertEqual(cloud.with_retries(flaky), "ok")
            self.assertEqual(sleep.call_count, 2)

            def long_wait():
                raise cloud.RateLimited(500)
            self.assertRaises(cloud.RateLimited, cloud.with_retries, long_wait)

            def offline():
                raise cloud.Offline("x")
            self.assertRaises(cloud.Offline, cloud.with_retries, offline)
            self.assertEqual(sleep.call_count, 2)    # offline is never slept on here; the queue handles it


class TestCloudTranscription(unittest.TestCase):
    def test_merge_and_split_ranges(self):
        gaps = [{"channel": "Me", "t_start": 0, "t_end": 10}, {"channel": "Me", "t_start": 10.5, "t_end": 30},
                {"channel": "Them", "t_start": 5, "t_end": 20}, {"channel": "Me", "t_start": 100, "t_end": 110}]
        merged = transcribe.merge_gaps(gaps)
        self.assertEqual([(g["channel"], g["t_start"], g["t_end"]) for g in merged],
                         [("Me", 0, 30), ("Me", 100, 110), ("Them", 5, 20)])
        self.assertEqual(len(transcribe.merge_gaps([{"channel": "Me", "t_start": i * 20, "t_end": i * 20 + 20}
                                                    for i in range(6)])), 2)   # capped at a minute each
        parts = transcribe.split_ranges([{"channel": "Me", "t_start": 0, "t_end": 1500}], 600)
        self.assertEqual([(p["t_start"], p["t_end"]) for p in parts], [(0, 600), (600, 1200), (1200, 1500)])

    def test_live_chunks_become_gaps_while_offline(self):
        gaps, states, segs = [], [], []
        tr = transcribe.Transcriber("base.en", lambda *a: segs.append(a))
        tr.on_gap = lambda *a: gaps.append(a)
        tr.on_cloud = lambda ok, reason: states.append(ok)
        fake = FakeGroq(fail=cloud.Offline("no network"))
        tr.cloud = fake
        tr.submit(1, "Them", 0.0, tone(12))
        tr.submit(1, "Them", 12.0, tone(12))      # inside the back-off window: queued without trying
        tr.wait_idle(10)
        self.assertEqual([(g[1], g[2], round(g[3])) for g in gaps], [("Them", 0.0, 12), ("Them", 12.0, 24)])
        self.assertEqual(states, [False])
        fake.fail = None                           # connection back, confirmed by the catch-up worker
        tr.cloud_reachable()
        tr.seed = "Haan, theek hai."
        tr.submit(1, "Them", 24.0, tone(12))
        tr.wait_idle(10)
        self.assertEqual(states, [False, True])
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][2], 24.5)                       # chunk start + segment start
        self.assertIn("Haan", fake.transcribed[0][2])             # first chunk is primed with the seed

    def test_cloud_fill_from_saved_audio(self):
        d = TMP / f"fill-{os.urandom(4).hex()}"
        tracks = {}
        for ch in ("Me", "Them"):
            t = _WavTrack(d / f"{ch}.wav")
            t.write(tone(40), 0.0)
            t.close()
            tracks[ch] = d / f"{ch}.wav"
        fake = FakeGroq()
        gaps = [{"channel": "Them", "t_start": 10, "t_end": 25}, {"channel": "Me", "t_start": 5, "t_end": 15}]
        out = transcribe.cloud_fill(fake, tracks, gaps, "hi", "Acme", seed="Haan")
        self.assertEqual([(o["channel"], o["t_start"]) for o in out], [("Them", 10.5), ("Me", 5.5)])
        self.assertTrue(all(lang == "hi" for _, lang, _ in fake.transcribed))
        self.assertIn("Acme", fake.transcribed[0][2])
        # the mic repeating what came out of the speakers is echo, not a second speaker
        fake2 = FakeGroq()
        fake2.transcribe = lambda audio, language=None, prompt=None: [
            {"start": 0.5, "end": 2.0, "text": "same words exactly", "no_speech_prob": 0.0, "avg_logprob": -0.1}]
        out = transcribe.cloud_fill(fake2, tracks, [{"channel": "Them", "t_start": 0, "t_end": 10},
                                                    {"channel": "Me", "t_start": 1, "t_end": 10}], None)
        self.assertEqual([o["channel"] for o in out], ["Them"])

    def test_segmenter_chunk_length_is_configurable(self):
        chunks = []
        seg = _Segmenter("Me", lambda sp, t0, a: chunks.append(len(a) / 16000), (10.0, 30.0))
        speech = tone(0.5)
        quiet = np.zeros(8000, np.float32)
        t = 0.0
        for _ in range(8):         # 8 s of speech with pauses: too short for a cloud chunk
            seg.push(speech, t)
            seg.push(quiet, t + 0.5)
            t += 1.0
        self.assertEqual(chunks, [])
        for _ in range(4):
            seg.push(speech, t)
            seg.push(quiet, t + 0.5)
            t += 1.0
        self.assertEqual(len(chunks), 1)
        self.assertGreaterEqual(chunks[0], 10.0)

    def test_cloud_language_plan(self):
        self.assertEqual(languages.cloud_plan("hinglish")["language"], "hi")
        self.assertTrue(languages.cloud_plan("hinglish")["seed"])
        self.assertEqual(languages.cloud_plan("hi"), {"language": "hi", "seed": ""})
        self.assertEqual(languages.cloud_plan("en"), {"language": "en", "seed": ""})
        self.assertEqual(languages.cloud_plan("auto"), {"language": None, "seed": ""})
        self.assertEqual(languages.cloud_plan("klingon"), {"language": None, "seed": ""})


class TestCloudNotes(unittest.TestCase):
    def tearDown(self):
        summarize.use_cloud(None)

    def test_token_estimate_counts_devanagari_heavier(self):
        self.assertLess(summarize.estimate_tokens("a" * 300), summarize.estimate_tokens("क" * 300))
        self.assertAlmostEqual(summarize.estimate_tokens("hello world " * 100), 400, delta=40)

    def test_long_transcript_is_condensed_into_requests_that_fit_the_rate_limit(self):
        fake = FakeGroq()
        summarize.use_cloud(fake)
        transcript = "\n".join(f"[00:{i:02d}] Them: बजट पर चर्चा हुई और हमने तय किया कि launch March में होगा {i}"
                               for i in range(600))
        notes = summarize.generate_notes("qwen/qwen3.8-27b", "Budget", "", transcript)
        self.assertIn("summary", notes)
        self.assertGreater(len(fake.chats), 2)            # condensed in parts, then the final notes
        for model, messages in fake.chats:
            tokens = sum(summarize.estimate_tokens(m["content"]) for m in messages)
            self.assertLess(tokens + cloud.max_output(model), cloud.TOKENS_PER_MINUTE)
        used = [model for model, _ in fake.chats]
        self.assertEqual(set(used[:-1]), {"qwen/qwen3.8-27b", "openai/gpt-oss-120b"})   # two quotas take turns
        self.assertEqual(used[-1], "qwen/qwen3.8-27b")    # the notes themselves come from the chosen model
        self.assertIn(summarize.CLOUD_LENGTH, fake.chats[-1][1][1]["content"])

    def test_ask_and_followup_trim_to_the_budget(self):
        fake = FakeGroq()
        summarize.use_cloud(fake)
        transcript = "\n".join(f"Them: line number {i} about the roadmap and the hiring plan" for i in range(3000))
        summarize.ask("openai/gpt-oss-120b", "T", "", "", transcript, [], "What was decided?")
        summarize.followup_email("openai/gpt-oss-120b", "T", "notes", transcript)
        for model, messages in fake.chats:
            tokens = sum(summarize.estimate_tokens(m["content"]) for m in messages)
            self.assertLess(tokens + cloud.max_output(model), cloud.TOKENS_PER_MINUTE)
        self.assertIn("line number 2999", fake.chats[0][1][1]["content"])   # keeps the most recent part

    def test_cloud_model_without_client_asks_for_a_key(self):
        self.assertRaises(cloud.AuthError, summarize._stream, "qwen/qwen3.8-27b", [])

    def test_ollama_models_never_go_to_the_cloud(self):
        fake = FakeGroq()
        summarize.use_cloud(fake)
        self.assertFalse(summarize.is_cloud("qwen3:4b-instruct"))
        r = resp(lines=[json.dumps({"message": {"content": "local"}, "done": True}).encode()])
        r.raise_for_status = lambda: None
        with mock.patch.object(summarize.requests, "post", return_value=r), \
             mock.patch.object(summarize, "_can_think", return_value=False):
            self.assertEqual(summarize._stream("qwen3:4b-instruct", []), "local")
        self.assertEqual(fake.chats, [])


class TestOfflineQueue(unittest.TestCase):
    """A meeting recorded without internet keeps its audio, then finishes itself when the cloud is back."""

    @classmethod
    def setUpClass(cls):
        cls.api = app.Api()
        cls.api._quitting = True             # stop the background catch-up: tests drive it directly
        cls.api._catchup_wake.set()
        time.sleep(0.3)                      # let the worker thread see it and exit
        cls.api._quitting = False
        cls.events = []
        cls.api._emit = lambda kind, **k: cls.events.append((kind, k))

    def use(self, fake):
        self.api._groq = fake
        self.api._apply_engine()

    def tearDown(self):
        self.api._groq = cloud.Groq(self.api._keys)
        summarize.use_cloud(None)

    def recorded_offline(self, title="Offline call") -> int:
        api = self.api
        api.save_settings({"engine": "cloud", "auto_notes": True, "keep_audio": False, "identify_speakers": False})
        mid = api._db.create_meeting(title)
        api._db.set_cloud_pending(mid, 1)
        for ch in ("Me", "Them"):
            t = _WavTrack(app.AUDIO_DIR / str(mid) / f"{ch}.wav")
            t.write(tone(30, 200 if ch == "Me" else 300), 0.0)
            t.close()
        api._db.add_segment(mid, "Them", 0.5, 2.0, "said while online", "Them")
        api._on_gap(mid, "Them", 10.0, 22.0)
        api._on_gap(mid, "Me", 12.0, 24.0)
        return mid

    def test_offline_meeting_waits_then_finishes(self):
        api, mid = self.api, self.recorded_offline()
        self.use(FakeGroq(fail=cloud.Offline("no network")))
        self.assertFalse(api._cloud_complete(mid))
        m = api.get_meeting(mid)
        self.assertTrue(m["queued"])
        self.assertIn("internet", m["queued_reason"])
        self.assertEqual(len(api._db.gaps(mid)), 2)
        self.assertTrue(api._tracks_for(mid))                      # audio kept for later
        self.assertIn(("pipeline", {"meeting_id": mid, "stage": "waiting", "reason": m["queued_reason"]}), self.events)
        self.assertNotIn(mid, api._pipeline)                       # usable (and deletable) meanwhile
        self.assertIn(mid, api._db.cloud_pending_meetings())

        fake = FakeGroq()
        self.use(fake)
        self.assertTrue(api._cloud_complete(mid))
        m = api.get_meeting(mid)
        self.assertFalse(m["queued"])
        self.assertEqual(api._db.gaps(mid), [])
        self.assertEqual([s["t_start"] for s in m["segments"]], [0.5, 10.5, 12.5])
        self.assertIn("summary", m["ai_notes"])
        self.assertEqual(fake.chats[0][0], api._settings["cloud_model"])
        self.assertFalse(api._tracks_for(mid))                     # audio removed once done
        self.assertNotIn(mid, api._db.cloud_pending_meetings())

    def test_missing_key_keeps_meeting_queued(self):
        api, mid = self.api, self.recorded_offline("No key")
        self.use(FakeGroq(fail=cloud.AuthError("No Groq API key — add one in Settings → Cloud.")))
        self.assertFalse(api._cloud_complete(mid))
        self.assertIn("API key", api.get_meeting(mid)["queued_reason"])
        self.assertTrue(api._tracks_for(mid))
        api.delete_meeting(mid)                                    # a queued meeting can still be deleted
        self.assertFalse(api._tracks_for(mid))
        self.assertNotIn(mid, api._db.cloud_pending_meetings())

    def test_notes_requested_offline_are_written_later(self):
        api = self.api
        api.save_settings({"engine": "cloud", "auto_notes": False})
        mid = api._db.create_meeting("Manual notes")
        api._db.add_segment(mid, "Them", 0, 2, "We will ship on Friday.", "Them")
        self.use(FakeGroq(fail=cloud.Offline("x")))
        with mock.patch.object(api._keys, "get", return_value="gsk_fake"):
            self.assertTrue(api.generate_notes(mid))
        for _ in range(100):
            if mid not in api._busy_notes:
                break
            time.sleep(0.05)
        self.assertEqual(api._db.get_meeting(mid)["cloud_pending"], app.NOTES_REQUESTED)
        self.assertTrue(any(k == "notes_queued" and e["meeting_id"] == mid for k, e in self.events))
        self.use(FakeGroq())
        self.assertTrue(api._cloud_complete(mid))                  # written even with auto notes off
        self.assertIn("summary", api._db.get_meeting(mid)["ai_notes"])
        self.assertEqual(api._db.get_meeting(mid)["cloud_pending"], 0)
        api.save_settings({"auto_notes": True})

    def test_catchup_backs_off_offline_and_stays_quiet_on_device(self):
        api, mid = self.api, self.recorded_offline("Catch-up")
        fake = FakeGroq(fail=cloud.Offline("x"))
        fake.pings = 0
        ping = fake.ping
        fake.ping = lambda: (setattr(fake, "pings", fake.pings + 1), ping())[1]
        self.use(fake)
        with mock.patch.object(api._keys, "get", return_value="gsk_fake"):
            self.assertEqual(api._catchup_once(30), 60)                # offline: wait twice as long
            self.assertEqual(api._catchup_once(500), app.CATCHUP_MAX_S)
            api.save_settings({"engine": "local"})
            self.assertEqual(api._catchup_once(30), app.CATCHUP_EVERY_S)
            self.assertEqual(fake.pings, 2)                            # this-PC-only mode never contacts Groq
            api.save_settings({"engine": "cloud"})
            fake.fail = None
            self.assertEqual(api._catchup_once(60), app.CATCHUP_EVERY_S)
        self.assertNotIn(mid, api._db.cloud_pending_meetings())       # finished once reachable

    def test_ai_needs_a_key_in_cloud_mode(self):
        api = self.api
        api.save_settings({"engine": "cloud"})
        mid = api._db.create_meeting("No key")
        self.assertFalse(api.ask(mid, "What happened?", []))
        self.assertIn("API key", self.events[-1][1]["error"])

    def test_set_cloud_key_validates_and_never_echoes_it(self):
        api = self.api
        self.assertFalse(api.set_cloud_key("not a key")["ok"])
        self.use(FakeGroq(fail=cloud.AuthError("Groq rejected the API key")))
        r = api.set_cloud_key("gsk_rejected_key_000000000000")
        self.assertFalse(r["ok"] or r["saved"])
        self.assertFalse(r["has_key"])                             # a rejected key isn't kept
        self.use(FakeGroq(fail=cloud.Offline("x")))
        r = api.set_cloud_key("gsk_offline_key_00000000000000")
        self.assertTrue(r["saved"] and not r["ok"])                # can't check it offline: kept, checked later
        self.assertIn("internet", r["error"])
        self.use(FakeGroq())
        r = api.set_cloud_key("gsk_good_key_1111111111112222")
        self.assertTrue(r["ok"])
        self.assertEqual(r["hint"], "…2222")
        self.assertNotIn("gsk_good_key", json.dumps(r) + json.dumps(api.get_state()))
        self.assertFalse(api.remove_cloud_key()["has_key"])

    def test_settings_reject_unknown_engine_and_model(self):
        api = self.api
        api.save_settings({"engine": "cloud", "cloud_model": "qwen/qwen3.8-27b"})
        api.save_settings({"engine": "quantum", "cloud_model": "made-up/model"})
        self.assertEqual((api._settings["engine"], api._settings["cloud_model"]), ("cloud", "qwen/qwen3.8-27b"))


if __name__ == "__main__":
    unittest.main()
