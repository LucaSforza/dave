import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.session import Session


class RuntimeRegressions(unittest.TestCase):
    def test_original_gui_has_distinct_ros_name_and_is_supervised(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root, "00000000-0000-0000-0000-000000000001", 1)
            session.display = ":90"
            session.xauthority = "/private/.Xauthority"
            session.request = {}
            session.command = Mock()
            session.wait_for_health = Mock()
            session.wait_processes = Mock()
            adapter = Mock()
            with patch("runtime.session.RexrovAdapter", return_value=adapter):
                session.start_simulation()
            commands = [call.args[0] for call in session.command.call_args_list]
            gui = next(command for command in commands if command[0].endswith("/gzclient"))
            self.assertIn("__name:=gazebo_gui", gui)
            self.assertEqual(commands[0][0], "/opt/dave/cluster-launch")


if __name__ == "__main__":
    unittest.main()
