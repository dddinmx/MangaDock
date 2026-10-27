"""Regression checks for concurrent reading-time reports."""
import threading
import unittest
from unittest.mock import patch

import test_review_regressions as regression_tests


class FullStackFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = regression_tests.ReviewRegressions
        cls.sandbox.setUpClass()
        from mangadock.services import reading
        cls.reading = reading
        cls.app, cls.db, cls.models = cls.sandbox.app, cls.sandbox.db, cls.sandbox.models

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.tearDownClass()

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.db.session.remove)
        self.user_id = self.models.User.query.filter_by(username='admin').first().id

    def check_concurrent_reports(self, initial, reports):
        from sqlalchemy.orm import Query
        name = self._testMethodName
        if initial:
            self.db.session.add(self.models.ReadingSessionState(
                user_id=self.user_id, comic_name=name, session_key='same-session', last_reported_seconds=initial,
            ))
            self.db.session.add(self.models.ReadingTime(
                user_id=self.user_id, comic_name=name, duration=1, duration_seconds=initial,
                read_at=self.reading.datetime.now(self.reading.china_tz),
            ))
            self.db.session.commit()
        self.db.session.remove()
        start = threading.Barrier(len(reports))
        read = threading.Barrier(len(reports))
        original_first = Query.first
        errors = []
        def synchronized_first(query):
            result = original_first(query)
            if (query.column_descriptions[0]['entity'] is self.models.ReadingSessionState
                    and threading.current_thread().name.startswith('reading-race')
                    and not query.session.connection().connection.in_transaction):
                # Reproduce competing reads only when no database write lock is held.
                read.wait(timeout=5)
            return result
        def report(seconds):
            try:
                start.wait(timeout=5)
                self.reading.record_reading_time(name, seconds, self.user_id, 'same-session')
            except Exception as exc:
                errors.append(exc)
        with patch.object(Query, 'first', synchronized_first):
            threads = [threading.Thread(target=report, args=(seconds,), name=f'reading-race-{index}')
                       for index, seconds in enumerate(reports)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        total = self.db.session.query(self.db.func.sum(self.models.ReadingTime.duration_seconds)).filter_by(
            user_id=self.user_id, comic_name=name).scalar()
        state = self.models.ReadingSessionState.query.filter_by(comic_name=name).first()
        self.assertEqual(total, max(reports))
        self.assertEqual(state.last_reported_seconds, max(reports))

    def test_existing_session_does_not_double_count_concurrent_reports(self):
        self.check_concurrent_reports(30, [60, 60])

    def test_out_of_order_cumulative_reports_do_not_lose_or_repeat_time(self):
        self.check_concurrent_reports(30, [60, 90])

    def test_first_concurrent_reports_are_deduplicated(self):
        self.check_concurrent_reports(0, [60, 60])

    def test_capped_report_can_drain_remaining_seconds(self):
        name = self._testMethodName
        for _ in range(3):
            self.reading.record_reading_time(name, 2000, self.user_id, 'same-session')
        total = self.db.session.query(self.db.func.sum(self.models.ReadingTime.duration_seconds)).filter_by(
            comic_name=name).scalar()
        self.assertEqual(total, 2000)


if __name__ == '__main__':
    unittest.main()
