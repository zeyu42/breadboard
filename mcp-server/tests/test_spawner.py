import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from breadboard_mcp import spawner


class SpawnBreadboardTest(unittest.TestCase):
    def test_uses_spawned_port_for_browser_urls(self):
        process = Mock(pid=123)
        process.poll.return_value = None

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            for name in ("db", "dev", "logs"):
                (workdir / name).mkdir()

            with (
                patch.object(spawner, "_spawned", None),
                patch.object(spawner, "cleanup_orphans"),
                patch.object(spawner, "_staged_binary", return_value=Path("/fake/breadboard")),
                patch.object(spawner, "_pick_free_port", return_value=55436),
                patch.object(spawner, "_create_workdir", return_value=workdir),
                patch.object(spawner.subprocess, "Popen", return_value=process) as popen,
                patch.object(spawner, "_wait_for_ready"),
                patch.object(spawner, "_bootstrap_schema"),
                patch.object(spawner, "_find_english_id", return_value=14),
                patch.object(spawner, "_seed_admin"),
            ):
                info = spawner.spawn_breadboard(repo_root="/repo")
                command = popen.call_args.args[0]
                spawner._spawned["_log_fd"].close()
                spawner._spawned = None

        self.assertEqual(info["port"], 55436)
        self.assertIn("-Dbreadboard.rootUrl=http://127.0.0.1:55436", command)
        self.assertIn("-Dbreadboard.wsUrl=ws://127.0.0.1:55436/connect", command)


if __name__ == "__main__":
    unittest.main()
