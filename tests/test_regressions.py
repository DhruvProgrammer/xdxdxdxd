import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xdmovies import autodetect, bot_config, bot as bot_module
from xdmovies import cookie_store, scrape_all, search, solve_server
from xdmovies.bot import Bot, Card, Job
from xdmovies.bot_config import BotConfig
from xdmovies.browser_runtime import cancel_tasks, close_browser_objects


class URLValidationTests(unittest.TestCase):
    def test_search_urls_are_restricted_to_catalog_hosts_and_paths(self):
        self.assertEqual(
            search._abs_url("/movies/a-safe-title"),
            "https://top.xdmovies.wtf/movies/a-safe-title",
        )
        self.assertEqual(
            search._abs_url("https://xdmovies.wtf/series/show-1"),
            "https://xdmovies.wtf/series/show-1",
        )
        self.assertEqual(search._abs_url("https://evil.example/movies/a"), "")
        self.assertEqual(search._abs_url("/movies/a?next=1"), "")
        self.assertEqual(search._abs_url("https://top.xdmovies.wtf/admin"), "")

    def test_search_result_parser_caps_limits(self):
        items = [
            {"title": f"Title {i}", "path": f"/movies/title-{i}", "type": "movie"}
            for i in range(10)
        ]
        self.assertEqual(
            len(search.parse_search_items(items * 30, limit=99)),
            search.SEARCH_MAX_RESULTS,
        )
        self.assertEqual(len(search.parse_search_items(items, limit=0)), 1)


class SolveResultValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_transit_is_followed_and_only_approved_url_is_returned(self):
        transit = "https://latestnewsonline.sbs/go/session-1?t=token"

        class Page:
            def __init__(self):
                self.url = "https://latestnewsonline.sbs/r/movie?step=2"
                self.goto_url = ""

            async def goto(self, url, **_kwargs):
                self.goto_url = url
                self.url = "https://hubcloud.one/drive/approved"

        page = Page()
        context = SimpleNamespace(pages=[page])
        result = await solve_server._follow_gate_transit(page, context, transit)
        self.assertEqual(page.goto_url, transit)
        self.assertEqual(result, "https://hubcloud.one/drive/approved")

    async def test_gate_transit_is_not_stored_as_final_result(self):
        transit = "https://latestnewsonline.sbs/go/session-1?t=token"
        token = "a" * 32
        session = solve_server.SolveSession(
            token=token,
            url=transit,
            host="latestnewsonline.sbs",
            user_id=7,
            chat_id=7,
        )
        await solve_server._finish_gate_solve(session, transit, "test")
        self.assertFalse(session.solved)
        self.assertIsNone(session.final_url)
        result = solve_server.get_solved_result(token)
        self.assertEqual(result, None)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_tasks_cancels_and_reaps_tasks(self):
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await started.wait()
        await cancel_tasks([task], timeout=1.0)
        self.assertTrue(task.cancelled())

    async def test_batch_search_cancellation_stops_pending_titles(self):
        event = asyncio.Event()
        started = []

        async def fake_search(title, **kwargs):
            started.append(title)
            await asyncio.sleep(10)
            return []

        with patch.object(search, "search_titles", fake_search):
            task = asyncio.create_task(
                search.search_titles_batch(
                    ["one", "two", "three"],
                    deadline=10,
                    max_concurrency=2,
                    cancel_event=event,
                )
            )
            await asyncio.sleep(0.05)
            event.set()
            result = await asyncio.wait_for(task, timeout=2)
        self.assertTrue(result.cancelled)
        self.assertLessEqual(len(started), 2)


class BatchDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_are_sent_once_without_resending(self):
        sent = []
        edited = []

        class FakeBot:
            async def send_message(self, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(message_id=11)

            async def edit_message_text(self, **kwargs):
                edited.append(kwargs)
                return True

        cfg = BotConfig(token="test", owner_id=7, max_sessions=4)
        instance = Bot(cfg)
        instance._bot = FakeBot()
        job = Job(user_id=7, chat_id=7, url="https://top.xdmovies.wtf/movies/test-1")
        job.cards_total = 1
        result = (Card(title="Test", url="https://hubcloud.one/drive/a"), "https://hubcloud.one/drive/a", None)
        await instance._send_or_edit_batch(job, "Test", [result], in_progress_idx=1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(job.batch_sealed_count, 0)
        self.assertEqual(edited, [])


class CFFreshnessTests(unittest.TestCase):
    def test_expired_cookie_is_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            with patch.object(cookie_store, "STORE_PATH", str(path)):
                cookie_store.set_cookies_for_domain(
                    "https://top.xdmovies.wtf",
                    "cf_clearance=abcdefghijklmnopqrstuvwxyz0123456789",
                )
                self.assertTrue(cookie_store.get_cookies_for_url("https://top.xdmovies.wtf/movies/a-1"))
                data = json.loads(path.read_text(encoding="utf-8"))
                data["xdmovies.wtf"]["_expires_at"] = time.time() - 1
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(cookie_store.get_cookies_for_url("https://top.xdmovies.wtf/movies/a-1"), [])


class CheckpointPaginationTests(unittest.TestCase):
    def test_pagination_metadata_is_order_independent_and_checkpoint_resumes(self):
        self.assertEqual(
            scrape_all.extract_pagination_info(
                "PAGINATION_INFO = { totalPages: 9, currentPage: 4 }"
            ),
            (4, 9),
        )
        self.assertIsNone(
            scrape_all.extract_pagination_info(
                "PAGINATION_INFO = { totalPages: 2, currentPage: 3 }"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(scrape_all, "STATE_DIR", Path(directory)):
                state = scrape_all.ScrapeCheckpoint(
                    kind="movies", next_page=7, pages_completed=6
                )
                scrape_all.save_checkpoint(state)
                loaded = scrape_all.load_checkpoint("movies")
                self.assertEqual(loaded.next_page, 7)
                self.assertEqual(loaded.pages_completed, 6)
                self.assertEqual(
                    scrape_all.catalog_page_url("movies", 7),
                    "https://top.xdmovies.wtf/?type=movies&page=7",
                )


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_resource_closes_once(self):
        class Resource:
            def __init__(self):
                self.closed = 0

            async def close(self):
                self.closed += 1

        resource = Resource()
        await close_browser_objects([resource, resource], timeout=1.0)
        self.assertEqual(resource.closed, 1)

    async def test_hanging_close_does_not_skip_later_browser_adapter(self):
        class HangingPage:
            def __init__(self):
                self.close_started = asyncio.Event()

            async def close(self):
                self.close_started.set()
                await asyncio.Event().wait()

        class Adapter:
            def __init__(self):
                self.closed = False
                self.released = False

            async def close(self):
                try:
                    self.closed = True
                    await asyncio.sleep(0)
                finally:
                    self.released = True

        page = HangingPage()
        adapter = Adapter()
        await close_browser_objects([page, adapter], timeout=0.02)
        self.assertTrue(page.close_started.is_set())
        self.assertTrue(adapter.closed)
        self.assertTrue(adapter.released)


class SolveBusyTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_message_id_is_returned_as_integer(self):
        sent = []

        class FakeBot:
            async def send_message(self, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(message_id=57)

        async def busy(*_args, **_kwargs):
            raise solve_server.SolveBusyError(4, 4)

        instance = Bot(BotConfig(token="test", owner_id=7, max_sessions=4))
        instance._bot = FakeBot()
        with patch.object(solve_server, "create_session", busy):
            token, message_id = await instance._send_solve_link(
                7, "https://latestnewsonline.sbs/r/movie", "latestnewsonline.sbs", 7
            )
        self.assertIsNone(token)
        self.assertEqual(message_id, 57)
        self.assertIsInstance(message_id, int)
        self.assertEqual(len(sent), 1)

    async def test_reported_busy_result_does_not_send_generic_failure(self):
        sent = []

        class FakeBot:
            async def send_message(self, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(message_id=58)

        async def blocked(*_args, **_kwargs):
            raise cookie_store.CFChallengeError(
                "https://xdmovies.wtf/movies/test-1", host="xdmovies.wtf"
            )

        async def reported_busy(*_args, **_kwargs):
            return None, 59

        instance = Bot(BotConfig(token="test", owner_id=7, max_sessions=4))
        instance._bot = FakeBot()
        job = Job(
            user_id=7,
            chat_id=7,
            url="https://xdmovies.wtf/movies/test-1",
            silent=True,
        )
        with patch.object(bot_module, "_load_pipeline"), patch.object(
            bot_module, "_extract_with_diagnostics", blocked
        ), patch.object(instance, "_send_solve_link", reported_busy):
            await instance._run_for_user(job)
        self.assertEqual(sent, [])
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "solve_unavailable:xdmovies.wtf")


class LoggingDirectoryTests(unittest.TestCase):
    def test_existing_log_parent_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "bot.log"
            with patch.object(
                bot_config, "ensure_private_directory", side_effect=AssertionError
            ):
                bot_config._prepare_private_file(str(path))
            self.assertTrue(path.is_file())

    def test_missing_log_parent_is_created_with_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "logs"
            path = parent / "bot.log"
            with patch.object(bot_config.os, "makedirs", wraps=bot_config.os.makedirs) as makedirs:
                bot_config._prepare_private_file(str(path))
            makedirs.assert_called_once_with(str(parent), mode=0o700)
            self.assertTrue(path.is_file())


class AutodetectAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_results_are_not_treated_as_seen(self):
        urls = [
            "https://xdmovies.wtf/movies/enqueued-1",
            "https://xdmovies.wtf/movies/duplicate-1",
            "https://xdmovies.wtf/movies/global-full-1",
            "https://xdmovies.wtf/movies/user-full-1",
        ]
        results = iter([4, -1, -2, -3])

        class FakeBot:
            async def _enqueue(self, _job, dedupe=False):
                self.dedupe = dedupe
                return next(results)

        enqueued, already_active = await autodetect._enqueue_jobs(
            FakeBot(), urls, channel_id=-100, owner_id=7
        )
        self.assertEqual(enqueued, [urls[0]])
        self.assertEqual(already_active, [urls[1]])


class QueueAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_admission_is_globally_and_per_user_bounded(self):
        cfg = BotConfig(token="test", owner_id=7, max_sessions=2)
        instance = Bot(cfg)
        first = await instance._enqueue(Job(user_id=7, chat_id=7, url="https://top.xdmovies.wtf/movies/a-1"))
        second = await instance._enqueue(Job(user_id=7, chat_id=7, url="https://top.xdmovies.wtf/movies/a-2"))
        third = await instance._enqueue(Job(user_id=8, chat_id=8, url="https://top.xdmovies.wtf/movies/a-3"))
        self.assertGreaterEqual(first, 1)
        self.assertGreaterEqual(second, 1)
        self.assertIn(third, (-2, -3))


if __name__ == "__main__":
    unittest.main()
