import unittest
from unittest.mock import Mock

from breadboard_mcp.client import BreadboardClient


class BindingGuardTest(unittest.TestCase):
    def test_selection_verifies_live_event_tracker(self):
        client = object.__new__(BreadboardClient)
        client._request = Mock()
        client._ok_json = Mock(return_value={
            "selectedExperiment": {"id": 33},
            "experimentInstanceId": 354,
        })
        client.execute_script = Mock(return_value={"output": "probe\n\n==>true", "error": ""})

        self.assertTrue(client.get_current_selection()["runtimeBindingVerified"])

    def test_stop_refuses_unrelated_instance(self):
        client = object.__new__(BreadboardClient)
        client.get_current_selection = Mock(return_value={
            "experimentInstanceId": 354,
            "runtimeBindingVerified": True,
        })
        client._request = Mock()

        with self.assertRaisesRegex(RuntimeError, "Refusing to stop"):
            client.stop_game(353)
        client._request.assert_not_called()

    def test_launch_requires_verified_new_binding(self):
        client = object.__new__(BreadboardClient)
        client.get_current_selection = Mock(side_effect=[
            {"selectedExperiment": {"id": 33}},
            {"experimentInstanceId": 354, "runtimeBindingVerified": False},
        ])
        client._request = Mock()
        client._ok_json = Mock(return_value={"experimentInstanceId": 354})

        with self.assertRaisesRegex(RuntimeError, "did not bind"):
            client.launch_game("lab-test-3")


if __name__ == "__main__":
    unittest.main()
