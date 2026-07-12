import unittest

from breadboard_mcp.client import BreadboardClient


class LaunchGameTest(unittest.TestCase):
    def test_stops_active_instances_before_launch(self):
        client = BreadboardClient.__new__(BreadboardClient)
        client.get_current_selection = lambda: {"selectedExperiment": {"id": 1}}
        client.list_experiments = lambda: [{"id": 1}, {"id": 2}]
        client.list_instances = lambda experiment_id: {
            1: [{"id": 10, "status": "RUNNING"}, {"id": 11, "status": "STOPPED"}],
            2: [{"id": 20, "status": "TESTING"}],
        }[experiment_id]

        stopped = []
        client.stop_game = lambda instance_id: stopped.append(instance_id)
        client._request = lambda method, path, **kwargs: (method, path, kwargs)
        client._ok_json = lambda response: response

        result = client.launch_game("new-run")

        self.assertEqual(stopped, [10, 20])
        self.assertEqual(result, ("POST", "/debug/launch-game", {"json": {"name": "new-run"}}))


if __name__ == "__main__":
    unittest.main()
