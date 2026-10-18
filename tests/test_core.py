"""Core regression tests — no audio devices, models or Ollama needed.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sqlite3 as plain_sqlite
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="localnotes-tests-"))
os.environ["LOCALNOTES_DATA"] = str(TMP / "appdata")   # must be set before importing app

import numpy as np  # noqa: E402

import app  # noqa: E402
import hotkeys  # noqa: E402
import speakers  # noqa: E402
import summarize  # noqa: E402
import templates  # noqa: E402
from db import DB, encrypt_existing  # noqa: E402

KEY = bytes(range(32))


def fresh_db(name="t.db") -> DB:
    d = TMP / f"db-{os.urandom(4).hex()}"
    d.mkdir()
    return DB(d / name, KEY)


class TestDatabase(unittest.TestCase):
    def test_meeting_crud_and_search(self):
        db = fresh_db()
        mid = db.create_meeting("Budget review", "zoom", "general")
        db.add_segment(mid, "Them", 0, 2, "We approved forty thousand for the campaign.")
        db.update_meeting(mid, user_notes="campaign budget", ai_notes="## Decisions\n- Budget approved")
        self.assertEqual(db.get_meeting(mid)["template_id"], "general")
        self.assertEqual([m["id"] for m in db.search("campai")], [mid])
        self.assertEqual([m["id"] for m in db.search("approved")], [mid])
        db.delete_meeting(mid)
        self.assertIsNone(db.get_meeting(mid))
        self.assertEqual(db.search("campaign"), [])

    def test_search_survives_odd_queries(self):
        db = fresh_db()
        mid = db.create_meeting("C++ & \"quotes\" review")
        db.reindex(mid)
        for q in ['"', "?", "-", "*", "c++", "AND", "OR NOT", "a\"b", "   ", "(", "✓", "NEAR(", "^x"]:
            with self.subTest(q=q):
                db.search(q)   # must not raise

    def test_update_meeting_ignores_unknown_fields(self):
        db = fresh_db()
        mid = db.create_meeting("x")
        db.update_meeting(mid, title="y", started_at=0, bogus=1)
        m = db.get_meeting(mid)
        self.assertEqual(m["title"], "y")
        self.assertNotEqual(m["started_at"], 0)

    def test_segments_edit_delete_replace(self):
        db = fresh_db()
        mid = db.create_meeting("m")
        s = db.add_segment(mid, "Me", 1, 2, "helo", "Me")
        self.assertEqual(db.update_segment(s["id"], "hello"), mid)
        self.assertEqual(db.get_segment(s["id"])["text"], "hello")
        db.replace_segments(mid, [{"speaker": "Priya", "channel": "Them", "cluster": "Them:0",
                                   "t_start": 0, "t_end": 1, "text": "hi"}])
        segs = db.get_segments(mid)
        self.assertEqual((segs[0]["speaker"], segs[0]["channel"], segs[0]["cluster"]), ("Priya", "Them", "Them:0"))
        self.assertEqual(db.delete_segment(segs[0]["id"]), mid)
        self.assertEqual(db.get_segments(mid), [])
        self.assertIsNone(db.update_segment(999999, "x"))

    def test_interrupted_meetings_get_an_end_time(self):
        d = TMP / "interrupted"
        d.mkdir()
        db = DB(d / "x.db", KEY)
        mid = db.create_meeting("crash")
        db.add_segment(mid, "Me", 0, 42, "…")
        db._conn.close()
        db = DB(d / "x.db", KEY)
        m = db.get_meeting(mid)
        self.assertAlmostEqual(m["ended_at"] - m["started_at"], 42, delta=1)

    def test_speaker_tables_and_rename(self):
        db = fresh_db()
        mid = db.create_meeting("m")
        db.add_segment(mid, "Speaker 1", 0, 2, "hi", "Them")
        db.replace_segments(mid, [{**db.get_segments(mid)[0], "cluster": "Them:0"}])
        fp = np.ones(8, np.float32) / np.sqrt(8)
        db.save_speaker_analysis(mid, [{"channel": "Them", "t_start": 0, "t_end": 2, "cluster": 0}],
                                 [{"cluster": "Them:0", "name": "Speaker 1", "embedding": fp, "seconds": 2, "source": "auto"}])
        db.rename_cluster(mid, "Them:0", "Priya")
        self.assertEqual(db.get_segments(mid)[0]["speaker"], "Priya")
        spk = db.meeting_speakers(mid)[0]
        self.assertEqual((spk["name"], spk["source"]), ("Priya", "user"))
        np.testing.assert_allclose(spk["embedding"], fp)
        db.save_voice("Priya", fp, 1)
        self.assertEqual(db.voice_meeting_counts()["Priya"], 1)
        db.delete_voice("Priya")
        self.assertNotIn("Priya", db.voices())

    def test_templates_crud(self):
        db = fresh_db()
        db.sync_builtin_templates(templates.BUILTIN)
        db.sync_builtin_templates(templates.BUILTIN)          # idempotent
        self.assertEqual(len(db.templates()), len(templates.BUILTIN))
        tid = db.save_template(None, "Client check-in", "desc", "## Mood\n## Asks")
        self.assertFalse(db.template(tid)["builtin"])
        db.save_template("general", "Hacked", "", "x")         # built-ins are read-only
        self.assertEqual(db.template("general")["name"], "General meeting")
        mid = db.create_meeting("m", template_id=tid)
        db.delete_template(tid)
        self.assertIsNone(db.template(tid))
        self.assertIsNone(db.get_meeting(mid)["template_id"])
        db.delete_template("general")                          # built-ins can't be deleted
        self.assertIsNotNone(db.template("general"))


class TestEncryption(unittest.TestCase):
    def test_plain_database_is_migrated_and_unreadable_without_key(self):
        d = TMP / "migrate"
        d.mkdir()
        path = d / "localnotes.db"
        legacy = plain_sqlite.connect(path)
        legacy.executescript("CREATE TABLE meetings (id INTEGER PRIMARY KEY, title TEXT NOT NULL, started_at REAL NOT NULL,"
                             " ended_at REAL, app TEXT, user_notes TEXT NOT NULL DEFAULT '', ai_notes TEXT NOT NULL DEFAULT '');"
                             "INSERT INTO meetings(title, started_at) VALUES ('legacy meeting', 1);")
        legacy.commit()
        legacy.close()
        db = DB(path, KEY)
        self.assertTrue(db.encrypted)
        self.assertEqual(db.list_meetings()[0]["title"], "legacy meeting")
        self.assertNotEqual(path.read_bytes()[:16], b"SQLite format 3\x00")
        self.assertFalse(Path(str(path) + ".encrypting").exists())
        db._conn.close()
        with self.assertRaises(Exception):
            plain_sqlite.connect(path).execute("SELECT * FROM meetings").fetchall()
        with self.assertRaises(Exception):
            DB(path, bytes(32))                               # wrong key

    def test_lost_key_is_reported_not_replaced(self):
        d = TMP / "lostkey"
        d.mkdir()
        old = app.DATA_DIR
        app.DATA_DIR = d
        try:
            app.open_database()._conn.close()                 # first run: creates key + encrypted db
            (d / "storage.key").unlink()
            with self.assertRaises(app.DataLockedError) as cm:
                app.open_database()
            self.assertIn("storage.key", str(cm.exception))
            self.assertFalse((d / "storage.key").exists(), "must not mint a replacement key")
            import securestore
            (d / "storage.key").write_bytes(securestore.protect(bytes(32)))   # a different, wrong key
            with self.assertRaises(app.DataLockedError):
                app.open_database()
            (d / "storage.key").write_bytes(b"not dpapi data")             # unreadable key file
            with self.assertRaises(app.DataLockedError):
                app.open_database()
        finally:
            app.DATA_DIR = old

    def test_dpapi_key_roundtrip(self):
        import securestore
        p = TMP / "k" / "storage.key"
        k1 = securestore.load_or_create_key(p)
        self.assertEqual(len(k1), 32)
        self.assertEqual(securestore.load_or_create_key(p), k1)
        self.assertNotIn(k1, p.read_bytes())


class TestTemplatesAndText(unittest.TestCase):
    def test_template_suggestions(self):
        cases = {"1:1 with Rahul": "one-on-one", "Rahul / Varun 1-1": "one-on-one", "Daily standup": "standup",
                 "Candidate interview — Priya": "interview", "Acme discovery call": "sales",
                 "Sprint review": "project", "Q4 planning offsite": "brainstorm", "Security training": "lecture",
                 "Q3 launch sync": "general", "": "general"}
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(templates.suggest(title), want)

    def test_greeting_cleanup(self):
        g = summarize._clean_greeting
        self.assertEqual(g("Hi Priya, Rahul, and Them,\n\nBody"), "Hi Priya and Rahul,\n\nBody")
        self.assertEqual(g("Hello Them,"), "Hello all,")
        self.assertEqual(g("Hi Speaker 1 and Speaker 2!"), "Hi all!")
        self.assertEqual(g("Dear team,"), "Dear team,")
        self.assertEqual(g("Hi Priya,\nThem said yes."), "Hi Priya,\nThem said yes.")

    def test_notes_cleanup(self):
        out = summarize.clean_notes("- x (Owner: Them)\n- [ ] Them: send deck\n- [x] Priya: review\nThemes matter")
        self.assertIn("Owner: the other side", out)
        self.assertIn("- [ ] The other side: send deck", out)
        self.assertIn("Themes matter", out)

    def test_split_long_transcript(self):
        text = "\n".join(f"[00:{i:02d}] Them: " + "word " * 50 for i in range(100))
        parts = summarize._split(text, 2000)
        self.assertGreater(len(parts), 1)
        self.assertEqual("\n".join(parts), text)

    def test_first_line_summary(self):
        self.assertEqual(app.first_line_summary("### Notes\n\n**Summary:** Launch moves to March.\n- x"), "Launch moves to March.")
        self.assertEqual(app.first_line_summary("- [ ] task only"), "")


class TestLanguages(unittest.TestCase):
    def setUp(self):
        import languages
        self.L = languages
        self._orig = languages.LOCAL_MODEL_DIRS.copy()
        self.models = TMP / f"models-{os.urandom(3).hex()}"
        languages.LOCAL_MODEL_DIRS.update({k: self.models / v.name for k, v in self._orig.items()})

    def tearDown(self):
        self.L.LOCAL_MODEL_DIRS.update(self._orig)

    def install_hinglish(self):
        for d in self.L.LOCAL_MODEL_DIRS.values():
            d.mkdir(parents=True, exist_ok=True)
            (d / "model.bin").write_bytes(b"x")

    def test_english_keeps_english_models(self):
        p = self.L.speech_plan("en", "base.en", "large-v3-turbo")
        self.assertEqual(p, {"live": "base.en", "live_language": "en", "refine": "large-v3-turbo", "refine_language": "en"})

    def test_hindi_switches_to_multilingual_models(self):
        p = self.L.speech_plan("hi", "base.en", "distil-large-v3")
        self.assertEqual((p["live"], p["live_language"], p["refine"], p["refine_language"]), ("base", "hi", "large-v3-turbo", "hi"))
        self.assertEqual(self.L.speech_plan("auto", "small.en", "off")["refine"], "off")
        self.assertIsNone(self.L.speech_plan("auto", "small.en", "off")["live_language"])

    def test_hinglish_uses_oriserve_models_when_installed(self):
        p = self.L.speech_plan("hinglish", "base.en", "large-v3-turbo")
        self.assertEqual((p["live"], p["live_language"], p["refine"]), ("base", "hi", "large-v3-turbo"))   # not installed: Whisper + Hindi
        self.install_hinglish()
        p = self.L.speech_plan("hinglish", "base.en", "large-v3-turbo")
        self.assertEqual(p, {"live": "hinglish-swift", "live_language": "en", "refine": "hinglish-apex", "refine_language": "en"})
        self.assertEqual(self.L.speech_plan("hinglish", "base.en", "off")["refine"], "off")
        self.assertEqual(self.L.speech_plan("bogus", "base.en", "large-v3-turbo")["live_language"], None)   # unknown -> auto

    def test_notes_instructions(self):
        self.assertIn("English", self.L.notes_instruction("en", "hinglish"))
        self.assertIn("Roman script", self.L.notes_instruction("same", "hinglish"))
        self.assertIn("Devanagari", self.L.notes_instruction("hi", "en"))
        self.assertIn("main language", self.L.notes_instruction("same", "auto"))

    def test_nan_hallucination_is_filtered(self):
        from types import SimpleNamespace
        from transcribe import keep_segment
        seg = lambda text: SimpleNamespace(text=text, no_speech_prob=0.1, avg_logprob=-0.3)   # noqa: E731
        self.assertFalse(keep_segment(seg(" nan")))
        self.assertTrue(keep_segment(seg("Nanhi si baat hai.")))

    def test_think_filter_drops_reasoning_even_when_split(self):
        f = summarize.ThinkFilter()
        pieces = ["<thi", "nk>let me plan</th", "ink>\n\nNotes ", "here <b>x</b> <", "thin", "g"]
        self.assertEqual("".join(f.feed(p) for p in pieces) + f.flush(), "\n\nNotes here <b>x</b> <thing")
        f = summarize.ThinkFilter()
        self.assertEqual(f.feed("<think>never closed") + f.flush(), "")

    def test_stream_drops_reasoning_before_unpaired_close_tag(self):
        """Thinking-only models stream "reasoning…</think>answer" with no opening tag."""
        from unittest import mock
        lines = [json.dumps({"message": {"content": c}}).encode() for c in ["We are given", " stuff.\n</th", "ink>\n\n## Notes", "\n- a"]]
        lines.append(json.dumps({"done": True}).encode())
        resp = mock.Mock(iter_lines=lambda: iter(lines), raise_for_status=lambda: None)
        with mock.patch.object(summarize.requests, "post", return_value=resp), \
             mock.patch.object(summarize, "_can_think", return_value=True):
            self.assertEqual(summarize._stream("m", []), "## Notes\n- a")

    def test_set_meeting_language_api(self):
        TestApi.setUpClass()
        api = TestApi.api
        mid = api._db.create_meeting("lang")
        self.assertFalse(api.set_meeting_language(mid, "klingon"))
        self.assertTrue(api.set_meeting_language(mid, "hi"))
        self.assertEqual(api._meeting_language(mid), "hi")
        api._meeting_id = mid                                   # mid-call: live transcriber follows
        try:
            api.save_settings({"engine": "local"})
            api.set_meeting_language(mid, "en")
            self.assertEqual((api._transcriber.model_name, api._transcriber.language), (api._settings["whisper_model"], "en"))
            api.set_meeting_language(mid, "hi")
            self.assertEqual((api._transcriber.model_name, api._transcriber.language), ("base", "hi"))
            self.assertIsNone(api._transcriber.cloud)
            api.save_settings({"engine": "cloud"})              # Groq: Hindi token + code-switched seed for Hinglish
            self.assertIs(api._transcriber.cloud, api._groq)
            api.set_meeting_language(mid, "hinglish")
            self.assertEqual(api._transcriber.language, "hi")
            self.assertIn("meeting", api._transcriber.seed)
            api.set_meeting_language(mid, "auto")
            self.assertEqual((api._transcriber.language, api._transcriber.seed), (None, ""))
            api.set_meeting_language(mid, "hi")
        finally:
            api._meeting_id = None
        api.save_settings({"notes_language": "same"})
        self.assertIn("Devanagari", api._notes_language(mid))     # follows the meeting (Hindi)
        api.save_settings({"notes_language": "en"})
        self.assertIn("English", api._notes_language(mid))
        api.save_settings({"notes_language": "same"})


class TestSpeakersAndHotkeys(unittest.TestCase):
    def test_best_match_threshold_and_margin(self):
        a = np.array([1, 0, 0], np.float32)
        b = np.array([0, 1, 0], np.float32)
        self.assertEqual(speakers.best_match(a, {"A": a, "B": b})[0], "A")
        self.assertIsNone(speakers.best_match(np.array([.7, .7, 0], np.float32), {"A": a, "B": b})[0])   # ambiguous
        self.assertIsNone(speakers.best_match(None, {"A": a})[0])
        self.assertIsNone(speakers.best_match(a, {})[0])

    def test_label_segments_by_overlap(self):
        segs = [{"speaker": "Them", "channel": "Them", "t_start": 0, "t_end": 4},
                {"speaker": "Them", "channel": "Them", "t_start": 10, "t_end": 12},
                {"speaker": "Me", "channel": "Me", "t_start": 0, "t_end": 1}]
        turns = [{"channel": "Them", "t_start": 0, "t_end": 5, "cluster": 0},
                 {"channel": "Them", "t_start": 9, "t_end": 13, "cluster": 1}]
        speakers.label_segments(segs, turns, "Them")
        self.assertEqual([s.get("cluster") for s in segs], ["Them:0", "Them:1", None])

    def test_turn_clips_keep_one_voice_per_clip(self):
        from transcribe import turn_clips
        turns = [{"channel": "Them", "t_start": 0.0, "t_end": 4.0, "cluster": 0},
                 {"channel": "Them", "t_start": 4.3, "t_end": 6.0, "cluster": 0},     # same voice, small gap -> merged
                 {"channel": "Them", "t_start": 6.2, "t_end": 9.0, "cluster": 1},     # different voice -> own clip
                 {"channel": "Them", "t_start": 10.0, "t_end": 75.0, "cluster": 0},   # long turn -> split under 30 s
                 {"channel": "Them", "t_start": 80.0, "t_end": 80.1, "cluster": 1},   # too short -> dropped
                 {"channel": "Me", "t_start": 0.0, "t_end": 3.0, "cluster": 0}]
        clips = turn_clips(turns, "Them", 16000 * 90)
        self.assertEqual(clips[0], {"start": 0, "end": 96000})
        self.assertEqual(clips[1], {"start": int(6.2 * 16000), "end": 144000})
        self.assertTrue(all(c["end"] - c["start"] <= 30 * 16000 for c in clips))
        self.assertEqual(sum(c["end"] - c["start"] for c in clips[2:]), 65 * 16000)
        self.assertIsNone(turn_clips(turns, "Nobody", 16000))
        self.assertEqual(turn_clips(turns, "Me", 16000), [{"start": 0, "end": 16000}])   # clamped to audio length

    def test_merge_profile_stays_normalised(self):
        v, n = speakers.merge_profile(None, 0, np.array([3, 4], np.float32))
        self.assertEqual(n, 1)
        v, n = speakers.merge_profile(v, n, np.array([0, 1], np.float32))
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=5)

    def test_hotkey_parsing(self):
        self.assertEqual(hotkeys.parse("ctrl+alt+n"), (0x3, ord("N")))
        self.assertEqual(hotkeys.parse("CTRL+Shift+F9"), (0x6, 0x78))
        self.assertEqual(hotkeys.parse("win+space"), (0x8, 0x20))
        for bad in ("n", "", "ctrl+", "ctrl+bogus", "foo+n", "ctrl+f25"):
            with self.subTest(bad=bad):
                self.assertIsNone(hotkeys.parse(bad))


class TestApi(unittest.TestCase):
    """Api methods that don't need audio devices, models or Ollama."""
    api = None

    @classmethod
    def setUpClass(cls):
        if cls.api is not None:
            return
        TestApi.api = app.Api()
        TestApi.events = []
        TestApi.api._emit = lambda kind, **k: TestApi.events.append((kind, k))

    def test_settings_roundtrip_and_unknown_keys(self):
        s = self.api.save_settings({"vocabulary": "LogiSys", "not_a_setting": 1, "hotkey_record": "ctrl+alt+n"})
        self.assertEqual(s["vocabulary"], "LogiSys")
        self.assertNotIn("not_a_setting", s)
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["vocabulary"], "LogiSys")
        self.assertEqual(self.api._transcriber.vocabulary, "LogiSys")

    def test_settings_migration_moves_only_the_old_default_model(self):
        original = app.SETTINGS_PATH.read_text() if app.SETTINGS_PATH.exists() else None
        try:
            app.SETTINGS_PATH.write_text(json.dumps({"ollama_model": "llama3.2:3b", "theme": "dark"}))
            s = app.load_settings()
            self.assertEqual((s["ollama_model"], s["theme"], s["settings_version"]), ("qwen3:4b-instruct", "dark", 2))
            app.SETTINGS_PATH.write_text(json.dumps({"ollama_model": "mistral:7b"}))
            self.assertEqual(app.load_settings()["ollama_model"], "mistral:7b")      # deliberate choice kept
            app.SETTINGS_PATH.write_text(json.dumps({"ollama_model": "llama3.2:3b", "settings_version": 2}))
            self.assertEqual(app.load_settings()["ollama_model"], "llama3.2:3b")     # chosen after the upgrade
        finally:
            if original is not None:
                app.SETTINGS_PATH.write_text(original)

    def test_add_vocabulary_dedupes(self):
        self.api.save_settings({"vocabulary": "LogiSys, Priya"})
        self.assertEqual(self.api.add_vocabulary(["logisys", "HubSpot"]), "LogiSys, Priya, HubSpot")

    def test_toggle_task_and_home(self):
        mid = self.api._db.create_meeting("Tasks")
        self.api._db.update_meeting(mid, ai_notes="Summary.\n\n## Action items\n- [ ] A: one\n- [x] B: two\n- [ ] C: three")
        home = self.api.get_home()
        self.assertGreaterEqual(home["stats"]["open_tasks"], 2)
        notes = self.api.toggle_task(mid, 2, True)
        self.assertIn("- [x] C: three", notes)
        notes = self.api.toggle_task(mid, 1, False)
        self.assertIn("- [ ] B: two", notes)
        self.assertIsNone(self.api.toggle_task(987654, 0, True))

    def test_update_segment_suggestions_and_delete(self):
        mid = self.api._db.create_meeting("Edit")
        s = self.api._db.add_segment(mid, "Them", 0, 2, "send the logic sis deck", "Them")
        r = self.api.update_segment(s["id"], "send the  LogiSys   deck")
        self.assertEqual(r["text"], "send the LogiSys deck")
        self.assertNotIn("LogiSys", r["suggest"])            # already in vocabulary from the other test, or suggested
        r = self.api.update_segment(s["id"], "   ")
        self.assertEqual(r, {"deleted": True})
        self.assertIsNone(self.api.update_segment(s["id"], "gone"))

    def test_update_segment_blocked_while_processing(self):
        mid = self.api._db.create_meeting("Busy")
        s = self.api._db.add_segment(mid, "Me", 0, 1, "x", "Me")
        self.api._pipeline[mid] = "refining"
        try:
            self.assertIsNone(self.api.update_segment(s["id"], "y"))
        finally:
            self.api._pipeline.pop(mid)

    def test_template_api(self):
        self.assertIsNone(self.api.save_template({"name": " ", "prompt": "x"}))
        tid = self.api.save_template({"name": "Mine", "prompt": "## A"})
        self.api.save_settings({"default_template": tid})
        self.assertEqual(self.api._pick_template("anything"), tid)
        self.api.delete_template(tid)
        self.assertEqual(self.api._settings["default_template"], "auto")
        self.assertEqual(self.api._pick_template("Daily standup"), "standup")
        mid = self.api._db.create_meeting("t")
        self.assertFalse(self.api.set_meeting_template(mid, "nope"))
        self.assertTrue(self.api.set_meeting_template(mid, "sales"))

    def test_delete_meeting_and_rename_speaker(self):
        mid = self.api._db.create_meeting("Speakers")
        self.api._db.add_segment(mid, "Speaker 2", 0, 2, "hello", "Them")
        self.api._db.replace_segments(mid, [{**self.api._db.get_segments(mid)[0], "cluster": "Them:0"}])
        fp = np.ones(8, np.float32) / np.sqrt(8)
        self.api._db.save_speaker_analysis(mid, [{"channel": "Them", "t_start": 0, "t_end": 2, "cluster": 0}],
                                           [{"cluster": "Them:0", "name": "Speaker 2", "embedding": fp, "seconds": 2, "source": "auto"}])
        self.api._db.update_meeting(mid, ai_notes="- [ ] Speaker 2: send deck")
        m = self.api.rename_speaker(mid, "Them:0", "  Priya  ")
        self.assertEqual(m["segments"][0]["speaker"], "Priya")
        self.assertIn("Priya: send deck", m["ai_notes"])
        self.assertIn("Priya", [v["name"] for v in self.api.list_voices()])
        self.assertIsNone(self.api.rename_speaker(mid, "Them:9", "X"))
        self.assertIsNone(self.api.rename_speaker(mid, "Them:0", "   "))
        self.assertTrue(self.api.delete_voice("Priya"))
        self.assertTrue(self.api.delete_meeting(mid))
        self.assertIsNone(self.api.get_meeting(mid))

    def test_same_hotkey_for_both_actions_is_rejected(self):
        self.api.save_settings({"hotkey_record": "ctrl+alt+n", "hotkey_pause": "ctrl+alt+p"})
        s = self.api.save_settings({"hotkey_record": "ctrl+alt+x", "hotkey_pause": "ctrl+alt+x"})
        self.assertEqual((s["hotkey_record"], s["hotkey_pause"]), ("ctrl+alt+n", "ctrl+alt+p"))

    def test_back_to_back_recordings_keep_the_new_meeting(self):
        """Stopping one call and starting the next must not let the first one's processing clear the second."""
        class FakeRecorder:
            paused, devices = False, {}
            def levels(self): return {}
            def stop(self): pass
        import threading
        import time
        self.api.save_settings({"auto_notes": False, "identify_speakers": False, "refine_model": "off"})
        gate = threading.Event()   # hold the first call's processing until the second call has started
        original_wait = self.api._transcriber.wait_idle
        self.api._transcriber.wait_idle = lambda timeout=600: gate.wait(5)
        self.addCleanup(setattr, self.api._transcriber, "wait_idle", original_wait)
        first = self.api._db.create_meeting("first")
        self.api._meeting_id, self.api._recorder, self.api._rec_started = first, FakeRecorder(), 1.0
        self.api.stop_recording()
        self.assertIsNone(self.api._meeting_id)
        self.assertFalse(self.api.delete_meeting(first), "can't delete while processing")
        second = self.api._db.create_meeting("second")      # what start_recording does next
        self.api._meeting_id, self.api._recorder = second, FakeRecorder()
        gate.set()
        for _ in range(100):
            if first not in self.api._pipeline:
                break
            time.sleep(0.05)
        self.assertNotIn(first, self.api._pipeline)
        self.assertEqual(self.api._meeting_id, second, "the second recording must still be current")
        self.assertIsNotNone(self.api._db.get_meeting(first)["ended_at"])
        self.api._recorder, self.api._meeting_id = None, None
        self.api.save_settings({"auto_notes": True, "identify_speakers": True, "refine_model": "large-v3-turbo"})

    def test_pause_without_recording_is_noop(self):
        self.assertFalse(self.api.pause_recording())
        self.assertFalse(self.api.resume_recording())
        self.assertEqual(self.api._elapsed(), 0.0)
        self.assertEqual(self.api.pill_state(), {"mode": "idle"})


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
