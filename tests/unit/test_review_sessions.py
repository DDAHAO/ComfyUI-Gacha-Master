"""Durable, resumable result-review sessions.

These tests use the real SQLite schema/service with the fake engine.  The
large-navigation test inserts a clearly-labelled fixture (draws/attempts/OUTs)
instead of running hundreds of GPU jobs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from harness import Harness, make_png  # noqa: E402
from comfyui_gacha_master.backend import review as review_module  # noqa: E402
from comfyui_gacha_master.backend.errors import GachaError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class ReviewSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = Harness(outputs_per_attempt=2)
        await self.harness.start()
        self.svc = self.harness.runtime.service
        self.job = self.harness.create_job(
            count=2, names=("a.png", "b.png", "c.png")
        )
        self.svc.start_job(self.job["id"], expected_version=self.job["row_version"])
        self.harness.wait_job(self.job["id"], timeout=30)

    async def asyncTearDown(self):
        self.harness.cleanup()

    # ----------------------------------------------------------- helpers
    def _versions(self, source_id):
        return self.svc.review.source_state(source_id)["versions"]

    def _session(self):
        return self.svc.review.get_session(self.job["id"])["session"]

    def _decide_body(self, source_id, decision, session, *, offset=0):
        versions = self._versions(source_id) if offset == 0 else offset
        return {
            "source_id": source_id,
            "decision": decision,
            "expected_session_version": session["version"],
            "expected_result_revision": versions["result_revision"],
            "expected_selection_version": versions["selection_version"],
            "expected_plan_version": versions["plan_version"],
        }

    def _select(self, source_id, count=1):
        outputs = self.svc.list_outputs(source_id, limit=50)["items"]
        for output in outputs[:count]:
            self.svc.set_selection(
                output["id"], True, expected_version=output["selection_version"]
            )
        return outputs

    def test_open_is_idempotent_and_returns_first_pending_group(self):
        first = self.svc.review.open_session(self.job["id"])
        self.assertEqual(first["resume"]["mode"], "IN_GROUP")
        self.assertEqual(first["resume"]["source"]["source_order"], 1)
        self.assertEqual(first["summary"]["pending_sources"], 3)
        again = self.svc.review.open_session(self.job["id"])
        self.assertEqual(again["session"]["id"], first["session"]["id"])
        self.assertEqual(again["session"]["version"], first["session"]["version"])
        # Opening a group never creates a review or changes the job.
        self.assertEqual(
            self.harness.runtime.db.query_one(
                "SELECT COUNT(*) FROM source_reviews"
            )[0],
            0,
        )

    def test_position_save_is_versioned_and_never_confirms(self):
        opened = self.svc.review.open_session(self.job["id"])
        session = opened["session"]
        source_two = self.svc.list_sources(self.job["id"], limit=10)["items"][1]
        saved = self.svc.review.save_position(
            session["id"],
            {
                "expected_version": session["version"],
                "current_source_id": source_two["id"],
                "result_offset": 0,
            },
        )
        self.assertEqual(saved["session"]["version"], session["version"] + 1)
        self.assertEqual(saved["session"]["current"]["source_order"], 2)
        self.assertEqual(
            [item["source_order"] for item in saved["session"]["history"]], [1]
        )
        self.assertEqual(
            self.harness.runtime.db.query_one("SELECT COUNT(*) FROM source_reviews")[0],
            0,
        )
        with self.assertRaises(GachaError) as caught:
            self.svc.review.save_position(
                session["id"],
                {
                    "expected_version": session["version"],
                    "current_source_id": source_two["id"],
                },
            )
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        # A stale write did not move the boundary.
        self.assertEqual(
            self._session()["current"]["source_order"], 2
        )
        # Back reopens the previous group without revoking its (absent) review.
        back = self.svc.review.back(
            session["id"], {"expected_version": saved["session"]["version"]}
        )
        self.assertEqual(back["session"]["current"]["source_order"], 1)
        self.assertEqual(back["resume"]["mode"], "IN_GROUP")

    def test_selected_decision_commits_review_and_boundary_together(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        outputs = self._select(source["id"], 2)
        body = self._decide_body(source["id"], "SELECTED", opened["session"])
        result = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-selected"
        )
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["review"]["selected_count"], 2)
        self.assertEqual(result["session"]["break_type"], "AFTER_CONFIRMED")
        self.assertEqual(result["next"]["source"]["source_order"], 2)
        review = self.harness.runtime.db.query_one(
            "SELECT * FROM source_reviews WHERE source_id=?", (source["id"],)
        )
        self.assertIsNotNone(review)
        self.assertEqual(review["decision"], "SELECTED")
        # The exact selected OUT identities, not just a count.
        self.assertEqual(
            sorted(result["review"]["selected_output_ids"]),
            sorted(output["id"] for output in outputs[:2]),
        )
        self.assertEqual(result["summary"]["selected_groups"], 1)
        self.assertEqual(result["summary"]["pending_sources"], 2)
        # A restart only reads the server session and continues at group 2.
        resumed = self.svc.review.get_session(self.job["id"])
        self.assertEqual(resumed["resume"]["mode"], "NEXT_PENDING")
        self.assertEqual(resumed["resume"]["source"]["source_order"], 2)

    def test_decision_rules_require_saved_selection_and_zero_for_skip(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                opened["session"]["id"],
                self._decide_body(source["id"], "SELECTED", opened["session"]),
                "action-empty-selected",
            )
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        self._select(source["id"], 1)
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                opened["session"]["id"],
                self._decide_body(source["id"], "SKIPPED", opened["session"]),
                "action-skip-with-selection",
            )
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        self.assertIn("已选 1 张", caught.exception.message)
        self.assertEqual(
            self.harness.runtime.db.query_one("SELECT COUNT(*) FROM source_reviews")[0],
            0,
        )
        # The failed attempts did not advance the session.
        self.assertEqual(self._session()["version"], opened["session"]["version"])

    def test_all_expected_versions_are_required_and_zero_is_legitimate(self):
        from comfyui_gacha_master.backend.review import _int_field

        # Version 0 is a value, not a missing field.
        self.assertEqual(_int_field({"v": 0}, "v"), 0)
        with self.assertRaises(GachaError) as missing:
            _int_field({}, "v")
        self.assertEqual(missing.exception.code, "BAD_REQUEST")
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        versions = self._versions(source["id"])
        self.assertEqual(versions["selection_version"], 0)
        # A real decision with expected_selection_version=0 is valid.
        result = self.svc.review.decide_from_body(
            opened["session"]["id"],
            self._decide_body(source["id"], "SKIPPED", opened["session"]),
            "action-zero",
        )
        self.assertEqual(result["review"]["decision"], "SKIPPED")
        self.assertEqual(result["review"]["selection_version"], 0)
        # result_revision=0 is parsed as a number and compared, not rejected
        # as a missing field: the server reports a version conflict.
        next_source = result["next"]["source"]
        bad_zero = {
            "source_id": next_source["id"],
            "decision": "SKIPPED",
            "expected_session_version": result["session"]["version"],
            "expected_result_revision": 0,
            "expected_selection_version": 0,
            "expected_plan_version": 1,
        }
        with self.assertRaises(GachaError) as zero_conflict:
            self.svc.review.decide_from_body(
                result["session"]["id"], bad_zero, "zero-revision-conflict"
            )
        self.assertEqual(zero_conflict.exception.code, "VERSION_CONFLICT")
        # Missing or partial version envelopes are rejected.
        for missing in (
            "expected_session_version",
            "expected_result_revision",
            "expected_selection_version",
            "expected_plan_version",
        ):
            with self.subTest(missing=missing):
                body = self._decide_body(
                    result["next"]["source"]["id"], "SKIPPED", result["session"]
                )
                body.pop(missing)
                with self.assertRaises(GachaError) as caught:
                    self.svc.review.decide_from_body(
                        result["session"]["id"], body, f"missing-{missing}"
                    )
                self.assertEqual(caught.exception.code, "BAD_REQUEST")
        # Boolean versions are not integers here.
        body = self._decide_body(
            result["next"]["source"]["id"], "SKIPPED", result["session"]
        )
        body["expected_selection_version"] = False
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                result["session"]["id"], body, "bool-version"
            )
        self.assertEqual(caught.exception.code, "BAD_REQUEST")

    def test_stale_business_versions_conflict_without_writing(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        self._select(source["id"], 1)
        versions = self._versions(source["id"])
        body = {
            "source_id": source["id"],
            "decision": "SELECTED",
            "expected_session_version": opened["session"]["version"],
            "expected_result_revision": versions["result_revision"] + 5,
            "expected_selection_version": versions["selection_version"],
            "expected_plan_version": versions["plan_version"],
        }
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                opened["session"]["id"], body, "stale-revision"
            )
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")
        self.assertEqual(
            self.harness.runtime.db.query_one("SELECT COUNT(*) FROM source_reviews")[0],
            0,
        )
        self.assertEqual(self._session()["version"], opened["session"]["version"])

    def test_same_decision_is_idempotent_and_cannot_advance_twice(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        self._select(source["id"], 1)
        body = self._decide_body(source["id"], "SELECTED", opened["session"])
        first = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-idem"
        )
        version = first["session"]["version"]
        replay = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-idem"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["session"]["version"], version)
        # Same decision/versions under a new action id is also a replay, not a
        # second advance.
        replay_two = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-idem-2"
        )
        self.assertTrue(replay_two["idempotent"])
        self.assertEqual(replay_two["session"]["version"], version)
        # Reusing the key for a different decision is refused.
        changed = dict(body, decision="SKIPPED")
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                opened["session"]["id"], changed, "action-idem"
            )
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._session()["version"], version)

    def test_transaction_rolls_back_when_fault_happens_before_commit(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        self._select(source["id"], 1)
        body = self._decide_body(source["id"], "SELECTED", opened["session"])

        def fault(name):
            if name == "R01":
                raise RuntimeError("injected before commit")

        with mock.patch.object(review_module, "fault_point", fault):
            with self.assertRaises(RuntimeError):
                self.svc.review.decide_from_body(
                    opened["session"]["id"], body, "action-r01"
                )
        self.assertEqual(
            self.harness.runtime.db.query_one("SELECT COUNT(*) FROM source_reviews")[0],
            0,
        )
        self.assertEqual(self._session()["version"], opened["session"]["version"])
        self.assertEqual(self._session()["break_type"], "IN_GROUP")
        # Selection facts written before the decision stay committed.
        state = self.svc.review.source_state(source["id"])
        self.assertEqual(state["selected"], 1)
        # Retrying the same action after the simulated crash succeeds.
        retried = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-r01"
        )
        self.assertEqual(retried["review"]["decision"], "SELECTED")

    def test_response_loss_after_commit_is_recovered_not_replayed(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        self._select(source["id"], 1)
        body = self._decide_body(source["id"], "SELECTED", opened["session"])

        def fault(name):
            if name == "R02":
                raise RuntimeError("injected after commit, before response")

        with mock.patch.object(review_module, "fault_point", fault):
            with self.assertRaises(RuntimeError):
                self.svc.review.decide_from_body(
                    opened["session"]["id"], body, "action-r02"
                )
        # Both the review and the continuation boundary are committed.
        self.assertEqual(
            self.harness.runtime.db.query_one("SELECT COUNT(*) FROM source_reviews")[0],
            1,
        )
        resumed = self.svc.review.get_session(self.job["id"])
        self.assertEqual(resumed["session"]["break_type"], "AFTER_CONFIRMED")
        self.assertEqual(resumed["resume"]["mode"], "NEXT_PENDING")
        self.assertEqual(resumed["resume"]["source"]["source_order"], 2)
        # A retry of the lost response returns the receipt and does not move
        # the session a second time.
        replay = self.svc.review.decide_from_body(
            opened["session"]["id"], body, "action-r02"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["session"]["version"], resumed["session"]["version"])

    def test_removed_current_source_relocates_by_stable_order(self):
        opened = self.svc.review.open_session(self.job["id"])
        session = opened["session"]
        sources = self.svc.list_sources(self.job["id"], limit=10)["items"]
        with self.harness.runtime.db.tx() as conn:
            conn.execute(
                "UPDATE sources SET status='REMOVED_BY_USER', removed_at=? WHERE id=?",
                ("2026-11-16T00:00:00Z", sources[0]["id"]),
            )
        resumed = self.svc.review.get_session(self.job["id"])
        self.assertEqual(resumed["resume"]["mode"], "NEXT_PENDING")
        self.assertEqual(resumed["resume"]["source"]["source_order"], 2)
        self.assertNotEqual(resumed["resume"]["source"]["id"], sources[0]["id"])
        # Saving a position to the removed source is refused.
        with self.assertRaises(GachaError) as caught:
            self.svc.review.save_position(
                session["id"],
                {
                    "expected_version": session["version"],
                    "current_source_id": sources[0]["id"],
                },
            )
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")

    def test_backup_restore_keeps_review_session_and_boundary(self):
        opened = self.svc.review.open_session(self.job["id"])
        sources = self.svc.list_sources(self.job["id"], limit=10)["items"]
        session = opened["session"]
        saved = self.svc.review.save_position(
            session["id"],
            {
                "expected_version": session["version"],
                "current_source_id": sources[1]["id"],
                "result_offset": 0,
            },
        )
        backup = self.svc.create_backup("review-session-test")
        self.assertIsNotNone(backup)
        advanced = self.svc.review.save_position(
            session["id"],
            {
                "expected_version": saved["session"]["version"],
                "current_source_id": sources[2]["id"],
            },
        )
        self.assertEqual(advanced["session"]["current"]["source_order"], 3)
        restored = self.svc.restore_backup(backup["id"])
        self.assertEqual(restored["schema_version"], 4)
        after = self.svc.review.get_session(self.job["id"])
        self.assertEqual(after["session"]["version"], saved["session"]["version"])
        self.assertEqual(after["session"]["current"]["source_order"], 2)
        self.assertEqual(after["resume"]["mode"], "IN_GROUP")

    def test_decision_rejects_a_group_with_no_available_results(self):
        opened = self.svc.review.open_session(self.job["id"])
        now = "2026-11-16T00:00:00Z"
        with self.harness.runtime.db.tx() as conn:
            conn.execute(
                "INSERT INTO sources(id, job_id, source_order, original_name, size, sha256, "
                "width, height, image_format, status, created_at) "
                "VALUES ('fixture-empty-source', ?, 99, 'empty.png', 1, ?, 1, 1, 'PNG', "
                "'ACTIVE', ?)",
                (self.job["id"], "c" * 64, now),
            )
        with self.assertRaises(GachaError) as caught:
            self.svc.review.decide_from_body(
                opened["session"]["id"],
                {
                    "source_id": "fixture-empty-source",
                    "decision": "SKIPPED",
                    "expected_session_version": opened["session"]["version"],
                    "expected_result_revision": 0,
                    "expected_selection_version": 0,
                    "expected_plan_version": opened["plan_version"],
                },
                "empty-group-action",
            )
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        self.assertIn("没有可用结果", caught.exception.message)
        self.assertEqual(
            self.harness.runtime.db.query_one(
                "SELECT COUNT(*) FROM source_reviews WHERE source_id='fixture-empty-source'"
            )[0],
            0,
        )

    def test_two_jobs_keep_independent_boundaries(self):
        opened = self.svc.review.open_session(self.job["id"])
        source = opened["resume"]["source"]
        self._select(source["id"], 1)
        self.svc.review.decide_from_body(
            opened["session"]["id"],
            self._decide_body(source["id"], "SELECTED", opened["session"]),
            "job1-action",
        )
        second = self.harness.create_job(count=1, names=("d.png",))
        self.svc.start_job(second["id"], expected_version=second["row_version"])
        self.harness.wait_job(second["id"], timeout=30)
        second_open = self.svc.review.open_session(second["id"])
        self.assertEqual(second_open["resume"]["source"]["source_order"], 1)
        second_source = second_open["resume"]["source"]
        self.svc.review.decide_from_body(
            second_open["session"]["id"],
            self._decide_body(second_source["id"], "SKIPPED", second_open["session"]),
            "job2-action",
        )
        first_resume = self.svc.review.get_session(self.job["id"])
        second_resume = self.svc.review.get_session(second["id"])
        self.assertEqual(first_resume["summary"]["selected_groups"], 1)
        self.assertEqual(first_resume["summary"]["skipped_groups"], 0)
        self.assertEqual(second_resume["summary"]["selected_groups"], 0)
        self.assertEqual(second_resume["summary"]["skipped_groups"], 1)
        self.assertEqual(first_resume["resume"]["source"]["source_order"], 2)
        self.assertEqual(second_resume["resume"]["mode"], "FINISHED")


class ReviewNavigationFixtureTests(unittest.TestCase):
    """Stable 40/200 source navigation using an explicit fixture."""

    def setUp(self):
        self.h = Harness(outputs_per_attempt=1)
        _run(self.h.start())
        self.addCleanup(self.h.cleanup)
        names = tuple(f"scale_{index:04d}.png" for index in range(205))
        # Harness.make_sources writes tiny files; creation still uses the real
        # plan/copy path.  Outputs below are a fixture, not generated results.
        for index, name in enumerate(names):
            make_png(
                os.path.join(self.h.input_root, name),
                color=((index * 7) % 255, (index * 11) % 255, 90),
            )
        self.job = self.h.create_job(count=1, names=names)
        self.svc = self.h.runtime.service
        self.sources = []
        after = 0
        while True:
            page = self.svc.list_sources(self.job["id"], limit=200, after_order=after)
            if not page["items"]:
                break
            self.sources.extend(page["items"])
            if page["next_after_order"] is None:
                break
            after = page["next_after_order"]
        self.assertEqual(len(self.sources), 205)
        self._seed_outputs()

    def _seed_outputs(self):
        now = "2026-11-16T00:00:00Z"
        db = self.h.runtime.db
        with db.tx() as conn:
            for source in self.sources:
                draw = conn.execute(
                    "SELECT * FROM draws WHERE source_id=? ORDER BY draw_no LIMIT 1",
                    (source["id"],),
                ).fetchone()
                attempt_id = f"att-{draw['id']}"
                output_id = f"out-{draw['id']}"
                conn.execute(
                    "INSERT INTO attempts(id, draw_id, attempt_no, expected_prompt_id, "
                    "boot_id, status, execution_hash, source_hash, attempt_dir, created_at, updated_at) "
                    "VALUES (?,?,1,?,?, 'SUCCEEDED','fixture','fixture','fixture',?,?)",
                    (attempt_id, draw["id"], f"prompt-{draw['id']}", "fixture", now, now),
                )
                conn.execute(
                    "INSERT INTO outputs(id, attempt_id, out_no, rel_path, sha256, size, "
                    "width, height, health, created_at, verified_at) "
                    "VALUES (?,?,1,?,?,100,10,10,'VERIFIED',?,?)",
                    (
                        output_id,
                        attempt_id,
                        f"fixture/{output_id}.png",
                        "a" * 64,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE draws SET status='SUCCEEDED', accepted_attempt_id=?, "
                    "succeeded_at=?, updated_at=? WHERE id=?",
                    (attempt_id, now, now, draw["id"]),
                )
                conn.execute(
                    "INSERT INTO selections(output_id, selected, updated_at, version) "
                    "VALUES (?,0,?,0)",
                    (output_id, now),
                )
                conn.execute(
                    "UPDATE sources SET result_revision=result_revision+1 WHERE id=?",
                    (source["id"],),
                )

    def test_source_navigation_pages_at_40_41(self):
        page = self.svc.review.list_sources(self.job["id"], scope="pending", limit=40)
        self.assertEqual(len(page["items"]), 40)
        self.assertEqual(
            [item["source_order"] for item in page["items"]], list(range(1, 41))
        )
        self.assertEqual(page["next_after_order"], 40)
        second = self.svc.review.list_sources(
            self.job["id"], scope="pending", after_order=page["next_after_order"], limit=40
        )
        self.assertEqual(second["items"][0]["source_order"], 41)
        self.assertIsNotNone(second["prev_before_order"])
        self.assertEqual(second["prev_before_order"], 41)
        back = self.svc.review.list_sources(
            self.job["id"],
            scope="pending",
            before_order=second["items"][0]["source_order"],
            limit=40,
        )
        self.assertEqual(back["items"][-1]["source_order"], 40)

    def test_boundary_200_201_resumes_at_next_pending_group(self):
        # Fixture reviews for groups 1..200, then a real decision on group 200.
        now = "2026-11-16T00:00:00Z"
        with self.h.runtime.db.tx() as conn:
            for source in self.sources[:200]:
                conn.execute(
                    "INSERT INTO source_reviews(id, source_id, result_revision, "
                    "selection_version, plan_version, decision, confirmed_at) "
                    "SELECT ?, id, result_revision, selection_version, 1, 'SKIPPED', ? "
                    "FROM sources WHERE id=?",
                    (f"review-{source['id']}", now, source["id"]),
                )
        opened = self.svc.review.open_session(self.job["id"])
        # Pending scope now starts at 201.
        self.assertEqual(opened["resume"]["source"]["source_order"], 201)
        self.assertEqual(opened["summary"]["pending_sources"], 5)
        page = self.svc.review.list_sources(
            self.job["id"], scope="pending", after_order=200, limit=40
        )
        self.assertEqual(
            [item["source_order"] for item in page["items"]], [201, 202, 203, 204, 205]
        )
        # Reopen group 200 explicitly and confirm it through the real
        # transaction; the continuation boundary must be 201.
        source_200 = self.sources[199]
        saved = self.svc.review.save_position(
            opened["session"]["id"],
            {
                "expected_version": opened["session"]["version"],
                "current_source_id": source_200["id"],
            },
        )
        versions = self.svc.review.source_state(source_200["id"])["versions"]
        result = self.svc.review.decide_from_body(
            opened["session"]["id"],
            {
                "source_id": source_200["id"],
                "decision": "SKIPPED",
                "expected_session_version": saved["session"]["version"],
                "expected_result_revision": versions["result_revision"],
                "expected_selection_version": versions["selection_version"],
                "expected_plan_version": versions["plan_version"],
            },
            "boundary-200",
        )
        self.assertEqual(result["next"]["source"]["source_order"], 201)
        resumed = self.svc.review.get_session(self.job["id"])
        self.assertEqual(resumed["resume"]["mode"], "NEXT_PENDING")
        self.assertEqual(resumed["resume"]["source"]["source_order"], 201)

    def test_same_group_result_offset_restores_page_for_1000_outputs(self):
        # One fixture source with 1000 healthy OUTs spanning result pages.
        source = self.sources[0]
        now = "2026-11-16T00:00:00Z"
        with self.h.runtime.db.tx() as conn:
            block = conn.execute(
                "SELECT * FROM blocks WHERE source_id=? LIMIT 1", (source["id"],)
            ).fetchone()
            attempt_id = f"att-big-{source['id']}"
            draw_id = f"draw-big-{source['id']}"
            conn.execute(
                "INSERT INTO draws(id, job_id, source_id, block_id, draw_no, status, "
                "accepted_attempt_id, created_at, updated_at) "
                "VALUES (?,?,?,?,99,'SUCCEEDED',?,?,?)",
                (draw_id, self.job["id"], source["id"], block["id"], attempt_id, now, now),
            )
            conn.execute(
                "INSERT INTO attempts(id, draw_id, attempt_no, expected_prompt_id, "
                "boot_id, status, execution_hash, source_hash, attempt_dir, created_at, updated_at) "
                "VALUES (?,?,1,?,?, 'SUCCEEDED','fixture','fixture','fixture',?,?)",
                (attempt_id, draw_id, f"prompt-big-{source['id']}", "fixture", now, now),
            )
            for index in range(1, 1001):
                output_id = f"out-big-{source['id']}-{index:04d}"
                conn.execute(
                    "INSERT INTO outputs(id, attempt_id, out_no, rel_path, sha256, size, "
                    "width, height, health, created_at, verified_at) "
                    "VALUES (?,?,?,?,?,100,10,10,'VERIFIED',?,?)",
                    (
                        output_id,
                        attempt_id,
                        index,
                        f"fixture/{output_id}.png",
                        "b" * 64,
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE sources SET result_revision=result_revision+1 WHERE id=?",
                (source["id"],),
            )
        first = self.svc.list_outputs(source["id"], limit=50)
        # 1 OUT seeded at draw 1 plus 1000 fixture OUTs at draw 99.
        self.assertEqual(first["total"], 1001)
        self.assertEqual(first["offset"], 0)
        page_50 = self.svc.list_outputs(source["id"], limit=50, offset=50)
        self.assertEqual(page_50["offset"], 50)
        self.assertEqual(page_50["items"][0]["out_no"], 50)
        # Offset 1000 reaches the last fixture OUT without scanning pages.
        last = self.svc.list_outputs(source["id"], limit=50, offset=1000)
        self.assertEqual(last["offset"], 1000)
        self.assertEqual(len(last["items"]), 1)
        self.assertEqual(last["items"][0]["out_no"], 1000)


if __name__ == "__main__":
    unittest.main()
