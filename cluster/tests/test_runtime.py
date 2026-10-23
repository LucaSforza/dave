import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from runtime import session


class FakeAdapter:
    def __init__(self): self.paused = False
    def is_paused(self): return self.paused
    def validate_health(self, allow_paused=False): pass
    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def stop_input(self): pass
    def snapshot(self, pause_state): return {"schema": 1, "model": "rexrov", "pause_state": pause_state}
    def reset(self): pass


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.job = str(uuid.uuid4())
        self.alloc = os.path.join(self.root, "jobs", self.job, "allocations", "1")
        os.makedirs(self.alloc)
        self.s = session.Session(self.root, self.job, 1)
        self.s.adapter = FakeAdapter()

    def tearDown(self): self.temp.cleanup()

    def test_checkpoint_manifest_is_complete_and_rollover_scoped(self):
        self.s.rollover = True
        self.s.checkpoint()
        manifest = self.s.read_manifest()["checkpoint"]
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["job_id"], self.job)
        self.assertEqual(manifest["scenario"], "uw_lidar")
        self.assertEqual(manifest["allocation_index"], 1)
        self.assertEqual(manifest["fidelity"], "explicit_state_only")
        self.assertTrue(manifest["rollover"])
        with open(os.path.join(self.root, "jobs", self.job, "checkpoints", manifest["manifest_path"])) as f:
            self.assertEqual(json.load(f), manifest)

    def test_periodic_checkpoint_does_not_set_rollover_and_preserves_paused_state(self):
        self.s.adapter.paused = True
        self.s.checkpoint()
        self.assertTrue(self.s.adapter.paused)
        manifest = self.s.read_manifest()["checkpoint"]
        self.assertFalse(manifest["rollover"])

    def test_successor_restore_requires_exact_previous_manifest_and_hash(self):
        self.s.rollover = True
        self.s.checkpoint()
        checkpoint = self.s.read_manifest()["checkpoint"]
        alloc2 = os.path.join(self.root, "jobs", self.job, "allocations", "2")
        os.makedirs(alloc2)
        with open(os.path.join(alloc2, "resume.json"), "w") as f: json.dump(checkpoint, f)
        successor = session.Session(self.root, self.job, 2)
        successor.restore()
        self.assertTrue(successor.pending_restore.endswith(checkpoint["snapshot"]))
        with open(successor.pending_restore, "a") as f: f.write("corrupt")
        with self.assertRaises(RuntimeError): successor.restore()
        # Restore the snapshot and verify the independently scoped manifest checks too.
        with open(successor.pending_restore, "w") as f: json.dump({"schema": 1, "model": "rexrov"}, f)
        checkpoint["allocation_index"] = 0
        with open(os.path.join(alloc2, "resume.json"), "w") as f: json.dump(checkpoint, f)
        with self.assertRaises(RuntimeError): successor.restore()

    def test_safety_deadline_is_planned_rollover(self):
        self.s.stop.clear()
        self.s.deadline = session.time.monotonic() - 1
        self.s.safety_timer()
        self.assertTrue(self.s.rollover)
        self.assertTrue(self.s.stop.is_set())

    def test_dynamic_ports_are_distinct_loopback_allocations(self):
        port1 = self.s.available_port()
        port2 = self.s.available_port(exclude={port1})
        self.assertTrue(1 <= port1 <= 65535)
        self.assertNotEqual(port1, port2)

    def test_ros_masters_use_dynamic_loopback_ports(self):
        old = {key: os.environ.get(key) for key in ("ROS_MASTER_URI", "GAZEBO_MASTER_URI", "ROS_IP", "ROS_HOSTNAME")}
        try:
            with mock.patch.object(self.s, "available_port", side_effect=[51131, 51145]):
                self.s.configure_masters()
            self.assertEqual(os.environ["ROS_MASTER_URI"], "http://127.0.0.1:51131")
            self.assertEqual(os.environ["GAZEBO_MASTER_URI"], "http://127.0.0.1:51145")
            self.assertEqual(os.environ["ROS_IP"], "127.0.0.1")
            self.assertNotIn("ROS_HOSTNAME", os.environ)
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value

    def test_periodic_checkpoint_timestamp_changes_only_when_checkpoint_runs(self):
        self.s.config = {"checkpoint_interval_seconds": 60}
        calls = []
        self.s.checkpoint = lambda: calls.append(True)
        self.assertTrue(self.s.maybe_periodic_checkpoint(now=60))
        self.assertFalse(self.s.maybe_periodic_checkpoint(now=61))
        self.assertTrue(self.s.maybe_periodic_checkpoint(now=120))
        self.assertEqual(len(calls), 2)

    def test_sensor_health_wait_retries_using_wall_clock(self):
        calls = []
        def validate(allow_paused=False):
            calls.append(allow_paused)
            if len(calls) < 3: raise RuntimeError("sensor is still starting")
        self.s.adapter.validate_health = validate
        self.s.wait_for_health(timeout=1, allow_paused=True)
        self.assertEqual(calls, [True, True, True])

    def test_xauthority_cookie_is_private_and_not_an_argument(self):
        self.s.display = ":123"
        with mock.patch.object(session.subprocess, "run") as run:
            path = self.s.create_xauthority()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(run.call_args.args[0], ["xauth", "-f", path])
        cookie = run.call_args.kwargs["input"].split()[-1]
        self.assertNotIn(cookie, run.call_args.args[0])

    def test_control_failure_is_persisted_not_swallowed(self):
        def fail(): raise RuntimeError("pause service unavailable")
        self.s.adapter.pause = fail
        self.s.apply_control(4, "pause")
        result = self.s.read_manifest()["control_results"]["4"]
        self.assertEqual(result["status"], "error")
        self.assertIn("pause service unavailable", result["error"])
        self.assertTrue(os.path.isfile(os.path.join(self.alloc, "commands", "results", "00000000000000000004.json")))

    def test_host_supervisor_runs_python_inside_one_singularity_exec(self):
        path = os.path.join(os.path.dirname(__file__), "..", "runtime", "runtime.sh")
        with open(path) as stream: content = stream.read()
        self.assertIn('"$CONTAINER" exec', content)
        self.assertIn("--contain --no-home --home", content)
        self.assertIn("source /opt/ros/noetic/setup.bash", content)
        self.assertIn("exec python3 \"$1/runtime/session.py\"", content)
        self.assertIn('kill -"$signal" "$CONTAINER_PID"', content)
        self.assertNotIn('python3 "$(dirname "$0")/session.py"', content)

    def test_launch_uses_verified_demo_package_and_has_teleop_shortcut(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "image", "cluster-launch")) as stream: launch = stream.read()
        self.assertIn("roslaunch dave_demo_launch dave_uw_lidar_demo.launch", launch)
        with open(os.path.join(root, "runtime", "session.py")) as stream: runtime = stream.read()
        self.assertIn('"xterm"', runtime)
        self.assertIn("keyboard_teleop.py", runtime)
        self.assertIn('"-forever", "-shared"', runtime)
        self.assertIn('"-auth", env["XAUTHORITY"]', runtime)
        self.assertIn('env["XAUTHORITY"] = self.xauthority', runtime)
        self.assertIn('"-nolisten", "tcp"', runtime)
        self.assertNotIn('"-ac"', runtime)
        self.assertIn("available_display", runtime)
        self.assertIn("available_port", runtime)


class RosTopicTests(unittest.TestCase):
    def test_ros_adapter_initializes_node_and_subscribes_to_noetic_topics(self):
        subs, init = [], []
        class Rospy(types.ModuleType):
            def __init__(self): super().__init__("rospy")
            def init_node(self, *args, **kwargs): init.append((args, kwargs))
            def ServiceProxy(self, *args): return lambda *a, **k: None
            def Publisher(self, *args, **kwargs): return types.SimpleNamespace(publish=lambda msg: None)
            def Subscriber(self, topic, msg_type, callback):
                subs.append((topic, msg_type, callback)); return types.SimpleNamespace()
        class ModelStates: pass
        class Clock: pass
        class Image: pass
        class PointCloud2: pass
        class JointState: pass
        class Twist: pass
        class SetModelStateRequest: pass
        def module(name, **attrs):
            value = types.ModuleType(name)
            for key, val in attrs.items(): setattr(value, key, val)
            return value
        modules = {
            "rospy": Rospy(),
            "gazebo_msgs": module("gazebo_msgs"),
            "gazebo_msgs.msg": module("gazebo_msgs.msg", ModelStates=ModelStates),
            "gazebo_msgs.srv": module("gazebo_msgs.srv", GetModelState=object, SetModelState=object,
                                       GetPhysicsProperties=object, SetModelStateRequest=SetModelStateRequest,
                                       SetModelConfiguration=object),
            "geometry_msgs": module("geometry_msgs"),
            "geometry_msgs.msg": module("geometry_msgs.msg", Twist=Twist),
            "rosgraph_msgs": module("rosgraph_msgs"),
            "rosgraph_msgs.msg": module("rosgraph_msgs.msg", Clock=Clock),
            "sensor_msgs": module("sensor_msgs"),
            "sensor_msgs.msg": module("sensor_msgs.msg", Image=Image, PointCloud2=PointCloud2, JointState=JointState),
        }
        old = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        try:
            from runtime.ros_adapter import RexrovAdapter
            RexrovAdapter()
        finally:
            for name, previous in old.items():
                if previous is None: sys.modules.pop(name, None)
                else: sys.modules[name] = previous
        self.assertTrue(init)
        by_topic = {topic: msg for topic, msg, _ in subs}
        self.assertIs(by_topic["/clock"], Clock)
        self.assertIn("/rexrov/rexrov/camera/camera_image", by_topic)
        self.assertIs(by_topic["/rexrov/rexrov/camera/camera_image"], Image)
        self.assertIs(by_topic["/rexrov/sl3_points"], PointCloud2)
        self.assertIs(by_topic["/rexrov/joint_states"], JointState)

    def test_pause_state_is_read_from_gazebo_service_not_cached(self):
        from runtime.ros_adapter import RexrovAdapter
        adapter = object.__new__(RexrovAdapter)
        adapter.GetPhysicsProperties = object
        adapter.paused = False
        adapter.service_call = lambda name, service, *args: types.SimpleNamespace(pause=True)
        self.assertTrue(adapter.is_paused())

    def test_snapshot_uses_live_get_model_state_service(self):
        from runtime.ros_adapter import RexrovAdapter
        adapter = object.__new__(RexrovAdapter)
        adapter.GetModelState = object
        adapter.JOINT_TOPIC = "/rexrov/joint_states"
        adapter.joints = types.SimpleNamespace(name=["joint"], position=[0.25])
        adapter.clock = types.SimpleNamespace(clock=types.SimpleNamespace(to_sec=lambda: 12.0))
        old_pose = types.SimpleNamespace(position=types.SimpleNamespace(x=-10, y=-10, z=-10))
        adapter.model_states = types.SimpleNamespace(pose=[old_pose])
        point = types.SimpleNamespace(x=12.0, y=3.0, z=-2.0)
        orientation = types.SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)
        twist = types.SimpleNamespace(linear=point, angular=point)
        live_pose = types.SimpleNamespace(position=point, orientation=orientation)
        adapter.service_call = lambda name, service, *args: types.SimpleNamespace(success=True, pose=live_pose, twist=twist)
        result = adapter.snapshot(pause_state=True)
        self.assertEqual(result["pose"]["position"], [12.0, 3.0, -2.0])
        self.assertTrue(result["pause_state"])

    def test_restore_applies_state_while_paused_then_restores_saved_pause_mode(self):
        from runtime.ros_adapter import RexrovAdapter
        class Vector:
            def __init__(self): self.x = self.y = self.z = 0.0
        class Quaternion(Vector):
            def __init__(self): super().__init__(); self.w = 1.0
        class ModelState:
            def __init__(self):
                self.model_name = ""; self.reference_frame = ""
                self.pose = types.SimpleNamespace(position=Vector(), orientation=Quaternion())
                self.twist = types.SimpleNamespace(linear=Vector(), angular=Vector())
        class Request:
            def __init__(self): self.model_state = ModelState()
        class Twist: pass
        std_srvs = types.ModuleType("std_srvs.srv")
        std_srvs.Empty = object
        geometry = types.ModuleType("geometry_msgs.msg")
        geometry.Twist = Twist
        gazebo_srv = types.ModuleType("gazebo_msgs.srv")
        gazebo_srv.SetModelConfiguration = object
        parents = {"std_srvs": types.ModuleType("std_srvs"), "std_srvs.srv": std_srvs,
                   "gazebo_msgs": types.ModuleType("gazebo_msgs"), "gazebo_msgs.srv": gazebo_srv,
                   "geometry_msgs": types.ModuleType("geometry_msgs"), "geometry_msgs.msg": geometry}
        old = {name: sys.modules.get(name) for name in parents}
        sys.modules.update(parents)
        try:
            for saved_pause in (False, True):
                adapter = object.__new__(RexrovAdapter)
                physics = {"paused": False}
                calls = []
                adapter.paused = False
                adapter.GetPhysicsProperties = object
                adapter.SetModelState = object
                adapter.SetModelStateRequest = Request
                adapter.zero_pub = types.SimpleNamespace(publish=lambda message: None)
                def service(name, _type, *args):
                    calls.append(name)
                    if name == "/gazebo/get_physics_properties":
                        return types.SimpleNamespace(pause=physics["paused"])
                    if name == "/gazebo/pause_physics":
                        physics["paused"] = True
                    elif name == "/gazebo/unpause_physics":
                        physics["paused"] = False
                    return types.SimpleNamespace(success=True)
                adapter.service_call = service
                state = {"schema": 1, "model": "rexrov", "pause_state": saved_pause,
                         "pose": {"position": [1, 2, 3], "orientation": [0, 0, 0, 1]},
                         "twist": {"linear": [0, 0, 0], "angular": [0, 0, 0]},
                         "joints": {"names": ["j"], "positions": [0.5]}}
                adapter.restore(state)
                first_state = calls.index("/gazebo/set_model_state")
                self.assertTrue(calls.index("/gazebo/pause_physics") < first_state)
                self.assertEqual(physics["paused"], saved_pause)
        finally:
            for name, previous in old.items():
                if previous is None: sys.modules.pop(name, None)
                else: sys.modules[name] = previous


if __name__ == "__main__": unittest.main()
