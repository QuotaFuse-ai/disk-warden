# -*- coding: utf-8 -*-
"""Job runner state transitions and mutual exclusion (architecture §5.2)."""

import threading
import unittest

from src.app.errors import BridgeError
from src.app.events import EventBus
from src.app.jobs import (
    MIGRATE,
    ROLLBACK,
    SCAN,
    STATE_CANCELLED,
    STATE_CANCELLING,
    STATE_DONE,
    STATE_FAILED,
    JobRunner,
)


class JobRunnerTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.runner = JobRunner(self.bus)
        self.addCleanup(self.runner.shutdown)

    def _run(self, kind, worker, check_busy=True):
        job_id = self.runner.start(kind, worker, check_busy=check_busy)
        return self.runner.wait(job_id, 5)

    def test_successful_job_transitions(self):
        started = threading.Event()

        def worker(job, token, emit):
            started.set()
            emit("job.progress", {"step": "copy", "done": 1, "total": 2})
            return {"migrated": 1}

        job_id = self.runner.start(MIGRATE, worker)
        self.assertTrue(started.wait(5))
        result = self.runner.wait(job_id, 5)

        self.assertEqual(result["state"], STATE_DONE)
        self.assertEqual(result["summary"], {"migrated": 1})
        self.assertEqual(
            result["progress"], {"step": "copy", "done": 1, "total": 2}
        )
        self.assertIsNone(result["error"])
        self.assertTrue(result["started_at"])
        self.assertTrue(result["finished_at"])
        self.assertEqual(self.runner.active_jobs(), [])

        types = [event["type"] for event in self.bus.poll(0)["events"]]
        self.assertIn("job.progress", types)
        self.assertIn("job.done", types)

    def test_bridge_error_marks_the_job_failed(self):
        def worker(job, token, emit):
            raise BridgeError("VERIFY_FAILED", "hash mismatch", {"item_id": "x"})

        result = self._run(MIGRATE, worker)
        self.assertEqual(result["state"], STATE_FAILED)
        self.assertEqual(result["error"]["code"], "VERIFY_FAILED")
        self.assertEqual(result["error"]["message_key"], "error.VERIFY_FAILED")
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(result["error"]["detail"]["item_id"], "x")

    def test_unexpected_exception_marks_the_job_internal(self):
        def worker(job, token, emit):
            raise ValueError("bad state")

        result = self._run(MIGRATE, worker)
        self.assertEqual(result["state"], STATE_FAILED)
        self.assertEqual(result["error"]["code"], "INTERNAL")
        self.assertEqual(result["error"]["detail"]["reason"], "ValueError: bad state")

    def test_cancel_stops_a_running_job(self):
        started = threading.Event()

        def worker(job, token, emit):
            started.set()
            token.wait(5)
            return {"unreachable": True}

        job_id = self.runner.start(MIGRATE, worker)
        self.assertTrue(started.wait(5))
        self.assertTrue(self.runner.cancel(job_id))
        result = self.runner.wait(job_id, 5)
        self.assertEqual(result["state"], STATE_CANCELLED)
        self.assertIsNone(result["error"])
        self.assertFalse(self.runner.cancel(job_id))

    def test_queued_write_cancel_becomes_cancelling(self):
        release = threading.Event()
        first_started = threading.Event()

        def first(job, token, emit):
            first_started.set()
            release.wait(5)
            return {}

        first_id = self.runner.start(MIGRATE, first)
        self.assertTrue(first_started.wait(5))

        second_started = threading.Event()

        def second(job, token, emit):
            second_started.set()
            return {}

        # The write lock serialises writes, so this job is queued.
        second_id = self.runner.start(MIGRATE, second, check_busy=False)
        self.assertTrue(self.runner.cancel(second_id))
        self.assertEqual(
            self.runner.status(second_id)["job"]["state"], STATE_CANCELLING
        )

        release.set()
        self.runner.wait(first_id, 5)
        result = self.runner.wait(second_id, 5)
        self.assertEqual(result["state"], STATE_CANCELLED)
        self.assertFalse(second_started.is_set())

    def test_writes_are_serialised(self):
        order = []
        release = threading.Event()
        first_started = threading.Event()

        def first(job, token, emit):
            order.append("first-start")
            first_started.set()
            release.wait(5)
            order.append("first-end")
            return {}

        second_started = threading.Event()

        def second(job, token, emit):
            order.append("second-start")
            second_started.set()
            return {}

        first_id = self.runner.start(MIGRATE, first)
        self.assertTrue(first_started.wait(5))
        second_id = self.runner.start(ROLLBACK, second, check_busy=False)
        self.assertFalse(second_started.wait(0.2))
        release.set()
        self.runner.wait(first_id, 5)
        self.runner.wait(second_id, 5)
        self.assertEqual(order, ["first-start", "first-end", "second-start"])

    def test_active_job_rejects_new_work(self):
        release = threading.Event()
        started = threading.Event()

        def worker(job, token, emit):
            started.set()
            release.wait(5)
            return {}

        job_id = self.runner.start(SCAN, worker)
        self.assertTrue(started.wait(5))
        for kind in (SCAN, MIGRATE, ROLLBACK):
            with self.assertRaises(BridgeError) as ctx:
                self.runner.start(kind, lambda job, token, emit: {})
            self.assertEqual(ctx.exception.code, "BUSY")
            self.assertTrue(ctx.exception.retryable)
        release.set()
        self.runner.wait(job_id, 5)

    def test_unknown_job_is_not_found(self):
        for action in (
            lambda: self.runner.get("missing"),
            lambda: self.runner.status("missing"),
            lambda: self.runner.cancel("missing"),
            lambda: self.runner.wait("missing", 0.1),
        ):
            with self.assertRaises(BridgeError) as ctx:
                action()
            self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_status_and_list_expose_active_jobs(self):
        release = threading.Event()
        started = threading.Event()

        def worker(job, token, emit):
            started.set()
            release.wait(5)
            return {}

        job_id = self.runner.start(MIGRATE, worker)
        self.assertTrue(started.wait(5))
        status = self.runner.status(job_id)
        self.assertEqual(status["job"]["job_id"], job_id)
        self.assertEqual([item["job_id"] for item in self.runner.list(True)], [job_id])
        self.assertEqual(self.runner.active_jobs([SCAN]), [])

        release.set()
        self.runner.wait(job_id, 5)
        self.assertEqual(self.runner.list(True), [])
        self.assertEqual(len(self.runner.list()), 1)

    def test_finished_jobs_are_trimmed(self):
        runner = JobRunner(self.bus, max_finished=2)
        self.addCleanup(runner.shutdown)
        ids = []
        # Trimming happens on start, so each job must finish before the next
        # one is submitted for the retained set to be deterministic.
        for _ in range(4):
            job_id = runner.start(SCAN, lambda job, token, emit: {})
            runner.wait(job_id, 5)
            ids.append(job_id)

        # Starting job 4 trimmed job 1; the remaining finished jobs are kept.
        remaining = [job["job_id"] for job in runner.list()]
        self.assertNotIn(ids[0], remaining)
        self.assertEqual(remaining[-1], ids[3])
        self.assertEqual(len(remaining), 3)
        with self.assertRaises(BridgeError) as ctx:
            runner.get(ids[0])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
