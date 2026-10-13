from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from skill_sync_sidecar import cli
from skill_sync_sidecar import daemon as daemon_module
from skill_sync_sidecar.daemon import run_sync_daemon
from skill_sync_sidecar.fanout import FanoutScheduler, canonical_fingerprint


def _skill(root: Path, name: str, body: str = "body") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n", encoding="utf-8")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FanoutSchedulerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.canonical = Path(self._tmp.name) / "canonical"
        _skill(self.canonical, "a")
        self.clock = FakeClock()
        self.runs = []
        self.failed = 0

    def tearDown(self):
        self._tmp.cleanup()

    def runner(self, canonical, **kwargs):
        self.runs.append(kwargs)
        return {
            "run_id": f"r{len(self.runs)}",
            "record_path": f"/tmp/r{len(self.runs)}.json",
            "tools": [{"executed": [{}], "failed": [{}] * self.failed, "conflicts": []}],
        }

    def scheduler(self, **kwargs):
        return FanoutScheduler(self.canonical, yes=True, interval_seconds=3600, runner=self.runner, clock=self.clock, **kwargs)

    def test_runs_first_time_then_only_when_due(self):
        s = self.scheduler()
        self.assertEqual(s.maybe_run()["reason"], "first run")
        self.assertEqual(s.maybe_run()["status"], "skipped")
        _skill(self.canonical, "a", body="pulled a new version")
        self.assertEqual(s.maybe_run()["reason"], "canonical changed")
        self.assertEqual(s.maybe_run()["status"], "skipped")
        self.clock.now += 3600
        self.assertEqual(s.maybe_run()["reason"], "periodic check")
        self.assertEqual(len(self.runs), 3)

    def test_failures_make_the_next_cycle_retry(self):
        s = self.scheduler()
        self.failed = 1
        self.assertEqual(s.maybe_run()["failed"], 1)
        self.failed = 0
        self.assertEqual(s.maybe_run()["reason"], "previous run had failures")
        self.assertEqual(s.maybe_run()["status"], "skipped")

    def test_exceptions_are_reported_not_raised(self):
        def boom(canonical, **kwargs):
            raise OSError("disk full")

        s = FanoutScheduler(self.canonical, yes=True, runner=boom, clock=self.clock)
        result = s.maybe_run()
        self.assertEqual(result["status"], "error")
        self.assertIn("disk full", result["error"])
        s.runner = self.runner
        self.assertEqual(s.maybe_run()["reason"], "previous run had failures")

    def test_dry_run_only_plans(self):
        def planner(canonical):
            return {"tools": [{"summary": {"ok": 3, "link": 2, "conflict": 1}}]}

        s = FanoutScheduler(self.canonical, yes=False, runner=self.runner, planner=planner, clock=self.clock)
        result = s.maybe_run()
        self.assertEqual(result, {"status": "planned", "reason": "first run", "changes": 2})
        self.assertEqual(self.runs, [])

    def test_fingerprint_tracks_nested_skills_too(self):
        # scanner 的父技能哈希不含嵌套子技能；指纹必须含，否则子技能更新不会触发扇出
        before = canonical_fingerprint(self.canonical)
        _skill(self.canonical / "a" / "skills", "child")
        with_child = canonical_fingerprint(self.canonical)
        self.assertNotEqual(before, with_child)
        _skill(self.canonical / "a" / "skills", "child", body="child updated")
        self.assertNotEqual(with_child, canonical_fingerprint(self.canonical))


class DaemonHookTest(unittest.TestCase):
    def _run(self, statuses):
        results = iter(statuses)
        calls = []

        def fake_cycle(*args, **kwargs):
            status = next(results)
            if status == "raise":
                raise RuntimeError("sync broke")
            return {
                "status": status,
                "reason": "",
                "snapshot_id": "s",
                "sync_plan": {"summary": {}, "blocked": 1 if status == "blocked" else 0},
                "conflicts": None,
                "tombstones": None,
                "apply_result": None,
            }

        def hook():
            calls.append(1)
            return {"status": "ran"}

        with TemporaryDirectory() as tmp, mock.patch.object(daemon_module, "run_sync_cycle", fake_cycle):
            summary = run_sync_daemon(
                Path(tmp), None, "", Path(tmp) / "c", Path(tmp) / "w",
                max_cycles=len(statuses), stop_on_blocked=False, interval_seconds=0,
                sleep_fn=lambda s: None, fanout=hook,
            )
        return summary, calls

    def test_fanout_runs_after_complete_and_blocked_but_not_after_errors(self):
        summary, calls = self._run(["complete", "blocked", "dry_run", "raise"])
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.get("fanout") for c in summary["cycles"]], [{"status": "ran"}, {"status": "ran"}, {"status": "ran"}, None])

    def test_no_hook_means_no_fanout_key(self):
        with TemporaryDirectory() as tmp, mock.patch.object(
            daemon_module,
            "run_sync_cycle",
            lambda *a, **k: {"status": "complete", "reason": "", "snapshot_id": "s", "sync_plan": {"summary": {}, "blocked": 0}, "conflicts": None, "tombstones": None, "apply_result": None},
        ):
            summary = run_sync_daemon(Path(tmp), None, "", Path(tmp) / "c", Path(tmp) / "w", max_cycles=1, sleep_fn=lambda s: None)
        self.assertNotIn("fanout", summary["cycles"][0])


class DaemonCliTest(unittest.TestCase):
    def test_fanout_flags(self):
        args = cli.build_parser().parse_args(
            ["sync-daemon", "--local-root", "/x", "--cache-dir", "/c", "--work-dir", "/w", "--remote", "file:///r", "--dry-run", "--fanout", "--fanout-interval-seconds", "600"]
        )
        self.assertTrue(args.fanout)
        self.assertEqual(args.fanout_interval_seconds, 600.0)
        default = cli.build_parser().parse_args(["sync-daemon", "--local-root", "/x", "--cache-dir", "/c", "--work-dir", "/w", "--remote", "file:///r", "--dry-run"])
        self.assertFalse(default.fanout)


if __name__ == "__main__":
    unittest.main()
