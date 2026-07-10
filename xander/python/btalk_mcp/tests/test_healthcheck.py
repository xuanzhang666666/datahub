import unittest

import scripts.healthcheck as healthcheck


class HealthcheckTest(unittest.TestCase):
    def test_latest_message_time_uses_the_latest_session_timestamp(self) -> None:
        sessions = {
            "ok": True,
            "sessions": [
                {"id": "quiet", "lastMsgTime": 1000},
                {"id": "active", "lastMsgTime": 3000},
            ],
        }

        self.assertEqual(healthcheck.latest_message_time(sessions), 3000)

    def test_latest_message_time_accepts_compact_daemon_probe_summary(self) -> None:
        self.assertEqual(
            healthcheck.latest_message_time(
                {"ok": True, "session_count": 1167, "latest_message_time": 3000}
            ),
            3000,
        )

    def test_stale_message_issue_is_reported_after_threshold(self) -> None:
        issue = healthcheck.stale_message_issue(
            latest_message_time_ms=1_000,
            now_ms=1_000 + 31 * 60 * 1000,
            stale_minutes=30,
        )

        self.assertEqual(issue["code"], "message_stale")

    def test_stale_message_issue_is_not_reported_for_recent_activity(self) -> None:
        issue = healthcheck.stale_message_issue(
            latest_message_time_ms=1_000,
            now_ms=1_000 + 29 * 60 * 1000,
            stale_minutes=30,
        )

        self.assertIsNone(issue)

    def test_failure_threshold_requires_two_consecutive_checks(self) -> None:
        self.assertFalse(healthcheck.should_alert([{"code": "status"}], 1))
        self.assertTrue(healthcheck.should_alert([{"code": "status"}], 2))

    def test_crash_and_stale_issues_alert_immediately(self) -> None:
        self.assertTrue(healthcheck.should_alert([{"code": "database_crash"}], 1))
        self.assertTrue(healthcheck.should_alert([{"code": "message_stale"}], 1))


if __name__ == "__main__":
    unittest.main()
