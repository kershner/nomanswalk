import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import requests
import pytz

import planet_reports
import post_planets


PLANET = {
    "timestamp": "2026-10-13T22:30:00-04:00",
    "planet_hex": "216FA779F43A",
    "name": "New Hoca",
    "galaxy_name": "Dimonimba",
    "galaxy_index": 83,
    "biome": "Radioactive",
    "weather": "Roaring Nuclear Wind",
    "flora": "Generous",
    "fauna": "High",
    "sentinels": "Irregular Patrols",
    "has_rings": False,
}
COUNTS = {"planets_created": 1, "planets_updated": 0, "visits_created": 1}


class UploadTests(unittest.TestCase):
    def test_manual_entry_point_does_not_import_game_stream_or_scheduler(self):
        result = subprocess.run([
            sys.executable, "-c",
            "import post_planets, sys; "
            "assert not {'nms_bot', 'nms_twitch_bot', 'start_no_mans_walk', "
            "'obsws_python', 'twitchio'}.intersection(sys.modules)",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, filename in (
            ("OUTBOX_FILE", "outbox.json"),
            ("RUNTIME_STATE_FILE", "runtime_state.json"),
            ("PARAMS_FILE", "parameters.json"),
        ):
            patcher = mock.patch.object(planet_reports, name, self.root / filename)
            patcher.start()
            self.addCleanup(patcher.stop)
        planet_reports.PARAMS_FILE.write_text(json.dumps({
            "PLANET_REPORT_ENDPOINT": "https://kershner.org/api/no_mans_walk/planets/",
            "no_mans_walk_api_token": "test-token",
        }))
        self.write_daily([PLANET])

    def write_daily(self, planets):
        planet_reports.RUNTIME_STATE_FILE.write_text(json.dumps({"daily": {
            "date": "2026-10-13",
            "planets": list(map(str, range(len(planets)))),
            "planet_details": {str(i): item for i, item in enumerate(planets)},
        }}))

    def run_command(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = post_planets.main()
        return code, output.getvalue()

    @mock.patch.object(planet_reports.requests, "post")
    def test_manual_command_uses_saved_config_and_prints_response(self, post):
        post.return_value = mock.Mock(status_code=201, text=json.dumps(COUNTS))
        code, output = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("HTTP 201", output)
        self.assertIn('"visits_created": 1', output)
        post.assert_called_once_with(
            "https://kershner.org/api/no_mans_walk/planets/",
            json={"planets": [PLANET]},
            headers={"Authorization": "Bearer test-token"},
            timeout=30,
            allow_redirects=False,
        )
        # A second command does not requeue acknowledged daily records.
        self.assertEqual(self.run_command()[0], 0)
        post.assert_called_once()

    @mock.patch.object(planet_reports.requests, "post")
    def test_empty_batch_skips_request_even_without_credentials(self, post):
        self.write_daily([])
        planet_reports.PARAMS_FILE.write_text('{}')
        code, output = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("skipping", output)
        post.assert_not_called()

    @mock.patch.object(planet_reports.requests, "post")
    def test_failed_http_batch_survives_day_change_and_new_process(self, post):
        post.return_value = mock.Mock(status_code=503, text="Service unavailable")
        code, output = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", output)
        self.assertIn("Service unavailable", output)
        original = post.call_args.kwargs["json"]
        tomorrow = dict(PLANET, timestamp="2026-10-14T22:30:00-04:00")
        self.write_daily([tomorrow])
        # Run an actual fresh interpreter to verify persistence without in-memory state.
        script = (
            "from pathlib import Path; from unittest.mock import patch, Mock; "
            "import planet_reports, post_planets; "
            f"root=Path({str(self.root)!r}); "
            "planet_reports.OUTBOX_FILE=root/'outbox.json'; "
            "planet_reports.RUNTIME_STATE_FILE=root/'runtime_state.json'; "
            "planet_reports.PARAMS_FILE=root/'parameters.json'; "
            "p=patch.object(planet_reports.requests, 'post', return_value=Mock(status_code=200, text='{}')); "
            "send=p.start(); code=post_planets.main(); "
            "(root/'sent.json').write_text(__import__('json').dumps(send.call_args.kwargs['json'])); "
            "raise SystemExit(code)"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads((self.root / "sent.json").read_text())
        self.assertEqual(sent["planets"], original["planets"] + [tomorrow])

    @mock.patch.object(planet_reports.requests, "post")
    def test_lost_response_retry_is_identical_and_prints_zero_counts(self, post):
        post.side_effect = requests.Timeout("response lost after commit")
        self.assertEqual(self.run_command()[0], 1)
        original = post.call_args.kwargs["json"]
        post.side_effect = None
        post.return_value = mock.Mock(status_code=200, text=json.dumps(dict.fromkeys(COUNTS, 0)))
        code, output = self.run_command()
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.kwargs["json"], original)
        self.assertIn('"visits_created": 0', output)

    @mock.patch.object(planet_reports.requests, "post")
    def test_missing_token_fails_without_dropping_batch(self, post):
        planet_reports.PARAMS_FILE.write_text('{"PLANET_REPORT_ENDPOINT":"https://example.test"}')
        code, output = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("no_mans_walk_api_token", output)
        self.assertEqual(len(json.loads(planet_reports.OUTBOX_FILE.read_text())["pending"]), 1)
        post.assert_not_called()

    @mock.patch.object(planet_reports.requests, "post")
    def test_new_observation_during_request_is_not_acknowledged(self, post):
        changed = dict(PLANET, weather="Clear")

        def respond(*args, **kwargs):
            planet_reports.queue_planets([changed])
            return mock.Mock(status_code=200, text=json.dumps(COUNTS))

        post.side_effect = respond
        self.assertEqual(self.run_command()[0], 0)
        pending = json.loads(planet_reports.OUTBOX_FILE.read_text())["pending"]
        self.assertEqual(list(pending.values()), [changed])
        # An older daily snapshot cannot replace the newer pending observation.
        planet_reports.queue_planets([PLANET], only_missing=True)
        self.assertEqual(list(json.loads(planet_reports.OUTBOX_FILE.read_text())["pending"].values()), [changed])


class ScheduledUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_upload_runs_manual_command_and_propagates_failure(self):
        import nms_twitch_bot as bot
        instance = mock.Mock()
        with mock.patch.object(bot.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="HTTP 200\n{}", stderr="")
            self.assertTrue(await bot.NMSBot._post_planet_report(instance))
            args = run.call_args.args[0]
            self.assertEqual(args[0], sys.executable)
            self.assertEqual(Path(args[1]).resolve(), Path(post_planets.__file__).resolve())
            run.return_value = mock.Mock(returncode=1, stdout="HTTP 503", stderr="failed")
            self.assertFalse(await bot.NMSBot._post_planet_report(instance))

    async def test_schedule_starts_ten_minutes_before_shutdown_and_retries(self):
        import nms_twitch_bot as bot
        tz = pytz.timezone("US/Eastern")
        times = [tz.localize(datetime(2026, 10, 17, 23, minute, second))
                 for minute, second in [(49, 1), (50, 0), (51, 0), (52, 0)]]
        instance = mock.Mock()
        instance.get_channel.return_value = None
        instance._refresh_stream_presence = mock.AsyncMock()
        loops = 0
        upload_ticks = []

        async def upload():
            upload_ticks.append(loops)
            return len(upload_ticks) == 2

        instance._post_planet_report = mock.AsyncMock(side_effect=upload)

        async def tick(_seconds):
            nonlocal loops
            loops += 1
            if loops == len(times):
                raise asyncio.CancelledError

        with (
            mock.patch.object(bot, "datetime", wraps=datetime) as clock,
            mock.patch.object(bot.asyncio, "sleep", side_effect=tick),
            mock.patch.object(bot.subprocess, "run") as run,
            mock.patch.object(bot.Config, "SHUTDOWN_HOUR", 0),
            mock.patch.object(bot.Config, "SHUTDOWN_MINUTE", 0),
        ):
            clock.now.side_effect = times
            with self.assertRaises(asyncio.CancelledError):
                await bot.NMSBot._nightly_shutdown_loop(instance)
        self.assertEqual(instance._post_planet_report.await_count, 2)
        self.assertEqual(upload_ticks, [1, 2])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
