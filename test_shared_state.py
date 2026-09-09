import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


# shared_state only needs ModState at import time. Avoid pyMHF's interactive
# first-run prompt when exercising this file helper in an ordinary test process.
_real_pymhf = sys.modules.get("pymhf")
_fake_pymhf = types.ModuleType("pymhf")
_fake_pymhf.ModState = type("ModState", (), {})
sys.modules["pymhf"] = _fake_pymhf
try:
    from nmspy_mods import shared_state
finally:
    if _real_pymhf is None:
        sys.modules.pop("pymhf", None)
    else:
        sys.modules["pymhf"] = _real_pymhf


class StateWriterTests(unittest.TestCase):
    def test_transient_replace_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            real_replace = os.replace
            calls = 0

            def flaky_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("busy")
                real_replace(source, destination)

            with (
                mock.patch.object(shared_state, "_base_dir", directory),
                mock.patch.object(shared_state.os, "replace", side_effect=flaky_replace),
                mock.patch.object(shared_state.time, "sleep"),
            ):
                self.assertTrue(shared_state._write_state({"state": "ON_FOOT"}))

            self.assertEqual(calls, 2)
            with open(os.path.join(directory, "nms_state.json"), encoding="utf-8") as file:
                self.assertEqual(json.load(file)["state"], "ON_FOOT")

    def test_persistent_replace_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(shared_state, "_base_dir", directory),
                mock.patch.object(shared_state.os, "replace", side_effect=PermissionError("busy")),
                mock.patch.object(shared_state.time, "sleep"),
            ):
                self.assertFalse(shared_state._write_state({"state": "ON_FOOT"}))


if __name__ == "__main__":
    unittest.main()
