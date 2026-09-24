"""Tests for the WordPress bridge API (api_store, api_server, bot wiring).

Runs with the repo's stdlib unittest runner:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from xdmovies import api_server, api_store
from xdmovies.bot import Bot, Job, _api_serialize
from xdmovies.bot_config import BotConfig
from xdmovies.extractor import Card


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="apistore_")
        self._orig = api_store.DB_PATH
        api_store.DB_PATH = Path(self._tmp.name) / "api_jobs.db"
        api_store.init_db()

    def tearDown(self):
        api_store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_create_and_get(self):
        row = api_store.create_job("https://top.xdmovies.wtf/movies/a", "all")
        self.assertEqual(row["status"], api_store.STATUS_QUEUED)
        got = api_store.get_job(row["job_id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["url"], "https://top.xdmovies.wtf/movies/a")
        self.assertIsNone(got["result"])

    def test_unknown_job_is_none(self):
        self.assertIsNone(api_store.get_job("does-not-exist"))

    def test_status_transitions(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/b")["job_id"]
        api_store.set_status(jid, api_store.STATUS_RUNNING)
        self.assertEqual(api_store.get_job(jid)["status"], "running")
        api_store.save_result(jid, {"title": "B", "cards": []})
        row = api_store.get_job(jid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"]["title"], "B")
        self.assertIsNotNone(row["finished_at"])

    def test_cancel_flag_roundtrip(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/c")["job_id"]
        self.assertFalse(api_store.cancel_requested(jid))
        api_store.request_cancel(jid)
        self.assertTrue(api_store.cancel_requested(jid))

    def test_dedup_window(self):
        url = "https://top.xdmovies.wtf/movies/dedup"
        jid = api_store.create_job(url)["job_id"]
        api_store.save_result(jid, {"title": "D"})
        self.assertIsNotNone(api_store.find_recent_completed(url, 3600))
        self.assertIsNone(api_store.find_recent_completed(url, 0))
        self.assertIsNone(api_store.find_recent_completed(url + "x", 3600))

    def test_dedup_ignores_unfinished(self):
        url = "https://top.xdmovies.wtf/movies/pending"
        api_store.create_job(url)
        self.assertIsNone(api_store.find_recent_completed(url, 3600))

    def test_requeue_orphans(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/orphan")["job_id"]
        api_store.set_status(jid, api_store.STATUS_RUNNING)
        n = api_store.requeue_orphans()
        self.assertEqual(n, 1)
        self.assertEqual(api_store.get_job(jid)["status"], "failed")

    def test_queue_depths(self):
        api_store.create_job("https://top.xdmovies.wtf/movies/q1")
        api_store.create_job("https://top.xdmovies.wtf/movies/q2")
        d = api_store.queue_depths()
        self.assertEqual(d["queued"], 2)


class UrlAllowlistTests(unittest.TestCase):
    def test_accepts_valid(self):
        for good in (
            "https://top.xdmovies.wtf/movies/baby-123",
            "https://xdmovies.wtf/series/mirzapur-456",
            "top.xdmovies.wtf/movies/no-scheme-789",
        ):
            self.assertTrue(api_server.canonicalize(good), good)

    def test_canonical_form_is_stable(self):
        a = api_server.canonicalize("https://xdmovies.wtf/series/Show-1?utm=x#frag")
        b = api_server.canonicalize("https://top.xdmovies.wtf/series/Show-1")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://top.xdmovies.wtf/series/Show-1")

    def test_rejects_everything_else(self):
        for bad in (
            "",
            "http://top.xdmovies.wtf/movies/x",       # http not https
            "https://evil.example/movies/x",
            "https://top.xdmovies.wtf/",              # home
            "https://top.xdmovies.wtf/admin",
            "https://top.xdmovies.wtf/movies/a/b",     # too deep
            "https://top.xdmovies.wtf/movies/",
            "https://top.xdmovies.wtf.evil.com/movies/x",
            "https://top.xdmovies.wtf:8443/movies/x",
        ):
            self.assertEqual(api_server.canonicalize(bad), "", bad)

    def test_rejects_path_traversal_shape(self):
        self.assertEqual(
            api_server.canonicalize("https://top.xdmovies.wtf/movies/../../admin"), "")


class AuthTests(unittest.TestCase):
    def test_compare_digest_is_used(self):
        with patch.object(api_server, "API_KEY", "s3cret"):
            self.assertTrue(api_server.is_enabled())

    def test_disabled_without_key(self):
        with patch.object(api_server, "API_KEY", ""):
            self.assertFalse(api_server.is_enabled())


class SerializeTests(unittest.TestCase):
    def _result(self):
        return type("R", (), {
            "page_title": "Baby Do Die Do", "audios": "Hindi, Tamil",
            "rating": "7.4", "genres": "Comedy", "air_date": "2026",
            "sources": "WEB-DL",
        })()

    def test_serialize_shape_and_counts(self):
        job = Job(user_id=1, chat_id=0, url="https://top.xdmovies.wtf/movies/baby-1512084",
                  job_type="api", api_job_id="x")
        results = [
            (Card(title="A.mkv", size="1 GB", language="", file_name="", url="u1", kind="pack"),
             "https://hubcloud.ist/drive/a", None),
            (Card(title="B.mkv", size="2 GB", language="", file_name="", url="u2", kind="pack"),
             "", "button flow returned empty"),
        ]
        out = _api_serialize(job, self._result(), results, 12.34, truncated=False)
        self.assertEqual(out["title"], "Baby Do Die Do")
        self.assertEqual(out["language"], "Hindi, Tamil")
        self.assertEqual(out["meta"]["rating"], "7.4")
        self.assertEqual(len(out["cards"]), 2)
        self.assertEqual(out["cards"][0]["status"], "resolved")
        self.assertEqual(out["cards"][0]["url"], "https://hubcloud.ist/drive/a")
        self.assertEqual(out["cards"][1]["status"], "failed")
        self.assertIsNotNone(out["cards"][1]["error"])
        self.assertEqual(out["stats"]["resolved"], 1)
        self.assertEqual(out["stats"]["failed"], 1)
        self.assertEqual(out["stats"]["elapsed_s"], 12.3)
        self.assertFalse(out["stats"]["truncated"])
        json.dumps(out)  # must be serialisable

    def test_raw_url_not_shortened(self):
        job = Job(user_id=1, chat_id=0, url="https://top.xdmovies.wtf/movies/x-1",
                  job_type="api", api_job_id="x")
        results = [(Card(title="A", size="", language="", file_name="", url="u", kind="pack"),
                    "https://hubcloud.ist/drive/RAW", None)]
        out = _api_serialize(job, self._result(), results, 1.0)
        self.assertEqual(out["cards"][0]["url"], "https://hubcloud.ist/drive/RAW")


class QueueLaneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cfg = BotConfig(token="123456789:AAHdqgcv9erry8xMWqq83gWY6qM", owner_id=42)
        self.bot = Bot(cfg)

    def _job(self, n, lane="normal", user=7):
        return Job(user_id=user, chat_id=user, url=f"https://top.xdmovies.wtf/movies/j{n}",
                   job_type="api" if lane == "api" else "resolve", api_job_id=f"job{n}")

    async def _put(self, n, lane="normal", user=7):
        j = self._job(n, lane, user)
        if lane == "api":
            await self.bot._enqueue_api(j)
        elif lane == "urgent":
            await self.bot._enqueue(j, urgent=True)
        else:
            await self.bot._enqueue(j)
        return j

    async def test_three_lanes_exist(self):
        self.assertEqual(self.bot._api_depth(), 0)
        self.assertEqual(self.bot._total_depth(), 0)

    async def test_api_lane_is_lowest_priority(self):
        await self._put(1, "api")
        await self._put(2)
        await self._put(3, "urgent")
        await self._put(4, "api")
        picked = [(await self.bot._pick_next()).url for _ in range(4)]
        self.assertTrue(picked[0].endswith("j3"), picked)   # urgent first
        self.assertTrue(picked[1].endswith("j2"), picked)   # normal second
        self.assertTrue(picked[2].endswith("j1"), picked)   # api third
        self.assertTrue(picked[3].endswith("j4"), picked)

    async def test_api_depth_excluded_from_total(self):
        await self._put(1)
        await self._put(2, "api")
        self.assertEqual(self.bot._total_depth(), 1)
        self.assertEqual(self.bot._api_depth(), 1)

    async def test_pick_next_waits_for_api_only(self):
        await self._put(9, "api")
        got = await self.bot._pick_next()
        self.assertTrue(got.url.endswith("j9"))

    async def test_yield_to_telegram_runs_exactly_one(self):
        ran = []

        async def fake_run(job):
            ran.append(job.url)

        self.bot._run_for_user = fake_run
        parent = self._job(0, "api")
        await self._put(1)
        await self._put(2)
        n = await self.bot._yield_to_telegram(parent)
        self.assertEqual(n, 1)
        self.assertEqual(len(ran), 1)
        self.assertEqual(self.bot._total_depth(), 1)

    async def test_yield_noop_when_lanes_empty(self):
        self.assertEqual(await self.bot._yield_to_telegram(self._job(0, "api")), 0)

    async def test_yield_skips_cancelled_job(self):
        parent = self._job(0, "api")
        j = self._job(1)
        j.cancel_event.set()
        await self.bot._enqueue(j)
        self.assertEqual(await self.bot._yield_to_telegram(parent), 0)

    async def test_yield_prefers_urgent(self):
        order = []

        async def fake_run(job):
            order.append(job.url)

        self.bot._run_for_user = fake_run
        parent = self._job(0, "api")
        await self._put(1)
        await self._put(2, "urgent")
        await self.bot._yield_to_telegram(parent)
        self.assertTrue(order[0].endswith("j2"), order)

    async def test_run_for_user_dispatches_api_jobs(self):
        called = []

        async def fake_api(job):
            called.append(job.api_job_id)

        self.bot._run_api_job = fake_api
        await self.bot._run_for_user(self._job(5, "api"))
        self.assertEqual(called, ["job5"])


class ParkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cfg = BotConfig(token="123456789:AAHdqgcv9erry8xMWqq83gWY6qM", owner_id=42)
        self.bot = Bot(cfg)
        self._tmp = tempfile.TemporaryDirectory(prefix="apipark_")
        self._orig = api_store.DB_PATH
        api_store.DB_PATH = Path(self._tmp.name) / "api_jobs.db"
        api_store.init_db()

    def tearDown(self):
        api_store.DB_PATH = self._orig
        self._tmp.cleanup()

    async def test_park_sets_needs_human_and_releases_slot(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/p")["job_id"]
        job = Job(user_id=42, chat_id=0, url="https://top.xdmovies.wtf/movies/p",
                  job_type="api", api_job_id=jid)

        async def fake_create(url, user_id, host, chat_id=0):
            return "tok123"

        spawned = []

        def fake_create_task(coro):
            spawned.append(coro)
            coro.close()
            return None

        with patch("xdmovies.solve_server.create_session", fake_create), \
             patch("xdmovies.solve_server.get_public_url", lambda t: f"http://vps:8080/solve/{t}"), \
             patch("asyncio.create_task", fake_create_task):
            await self.bot._api_park(job, "https://top.xdmovies.wtf/movies/p", "xdmovies.wtf")

        row = api_store.get_job(jid)
        self.assertEqual(row["status"], "needs_human")
        self.assertEqual(row["solve_url"], "http://vps:8080/solve/tok123")
        self.assertIsNone(row["finished_at"])
        self.assertEqual(job.status, "needs_human")
        self.assertEqual(len(spawned), 1)

    async def test_park_failure_is_terminal(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/q")["job_id"]
        job = Job(user_id=42, chat_id=0, url="https://top.xdmovies.wtf/movies/q",
                  job_type="api", api_job_id=jid)

        async def boom(*a, **k):
            raise RuntimeError("no browser")

        with patch("xdmovies.solve_server.create_session", boom):
            await self.bot._api_park(job, "https://top.xdmovies.wtf/movies/q", "xdmovies.wtf")
        self.assertEqual(api_store.get_job(jid)["status"], "failed")

    async def test_solve_timeout_marks_failed(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/r")["job_id"]
        job = Job(user_id=42, chat_id=0, url="https://top.xdmovies.wtf/movies/r",
                  job_type="api", api_job_id=jid)

        class Ev:
            async def wait(self):
                await asyncio.sleep(3600)

        with patch("xdmovies.solve_server.get_session", return_value=type("S", (), {"event": Ev()})()), \
             patch("xdmovies.solve_server.SESSION_TTL", 1):
            await self.bot._api_await_solve(job, "tok")
        row = api_store.get_job(jid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("timed out", row["error"])


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end through the real HTTP handlers with a fake Bot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="apiint_")
        self._orig_db = api_store.DB_PATH
        api_store.DB_PATH = Path(self._tmp.name) / "api_jobs.db"
        api_store.init_db()
        cfg = BotConfig(token="123456789:AAHdqgcv9erry8xMWqq83gWY6qM", owner_id=42)
        self.bot = Bot(cfg)
        api_server._BOT = self.bot

    def tearDown(self):
        api_store.DB_PATH = self._orig_db
        api_server._BOT = None
        self._tmp.cleanup()

    async def test_create_then_poll_then_result(self):
        created = {}

        async def fake_enqueue_api(job):
            created["job"] = job
            return 1

        self.bot._enqueue_api = fake_enqueue_api
        api_server._BOT = self.bot

        resp = await api_server.handle_create_job(_FakeReq({
            "url": "https://top.xdmovies.wtf/movies/baby-1512084"}))
        self.assertEqual(resp.status, 202)
        body = json.loads(resp.text)
        jid = body["job_id"]
        self.assertEqual(body["status"], "queued")
        self.assertEqual(created["job"].job_type, "api")
        self.assertEqual(created["job"].api_job_id, jid)
        self.assertEqual(created["job"].silent, True)

        api_store.save_result(jid, {
            "title": "Baby Do Die Do", "tmdb_id": None, "language": "Hindi, Tamil",
            "meta": {}, "cards": [{"index": 1, "raw_title": "A", "size": "1 GB",
                                   "language": "", "url": "https://hubcloud.ist/drive/x",
                                   "status": "resolved", "error": None}],
            "stats": {"total": 1, "resolved": 1, "failed": 0, "elapsed_s": 9.0,
                      "truncated": False},
        })
        poll = await api_server.handle_get_job(_FakeReq({}, match={"job_id": jid}))
        self.assertEqual(poll.status, 200)
        pbody = json.loads(poll.text)
        self.assertEqual(pbody["status"], "done")
        self.assertEqual(pbody["result"]["stats"]["resolved"], 1)
        self.assertIn("elapsed_s", pbody)

    async def test_bad_url_rejected(self):
        resp = await api_server.handle_create_job(_FakeReq({"url": "https://evil.example/x"}))
        self.assertEqual(resp.status, 400)

    async def test_dedup_returns_cached_result(self):
        url = "https://top.xdmovies.wtf/movies/dup-1"
        jid = api_store.create_job(url)["job_id"]
        api_store.save_result(jid, {"title": "Cached", "cards": [], "stats": {}})

        async def should_not_run(job):
            raise AssertionError("dedup must not enqueue")

        self.bot._enqueue_api = should_not_run
        api_server._BOT = self.bot
        with patch.object(api_server, "API_DEDUP_SECONDS", 3600.0):
            resp = await api_server.handle_create_job(_FakeReq({"url": url}))
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["deduplicated"])

    async def test_delete_marks_cancel(self):
        jid = api_store.create_job("https://top.xdmovies.wtf/movies/cancel-1")["job_id"]
        await self.bot._enqueue_api(Job(user_id=42, chat_id=0,
                                        url="https://top.xdmovies.wtf/movies/cancel-1",
                                        job_type="api", api_job_id=jid))
        resp = await api_server.handle_delete_job(_FakeReq({}, match={"job_id": jid}))
        self.assertEqual(resp.status, 200)
        self.assertTrue(api_store.cancel_requested(jid))
        self.assertTrue(self.bot._api[0].cancel_event.is_set())

    async def test_delete_unknown_is_404(self):
        resp = await api_server.handle_delete_job(_FakeReq({}, match={"job_id": "nope"}))
        self.assertEqual(resp.status, 404)


class _FakeReq:
    def __init__(self, body, match=None):
        self._body = body
        self.match_info = match or {}
        self.query = {}
        self.path = "/api/v1/jobs"
        self.headers = {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


if __name__ == "__main__":
    unittest.main(verbosity=2)
