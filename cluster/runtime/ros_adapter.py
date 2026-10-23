"""RexROV ROS Noetic adapter: explicit state only, not a bitwise checkpoint."""
import math
import queue
import threading
import time


class RexrovAdapter:
    """Pose/twist/joint config snapshot; omits solver/plugin/RNG/contacts/time state."""
    RGB_TOPICS = ("/rexrov/rexrov/camera/camera_image", "/rexrov/image_raw")
    CLOUD_TOPIC = "/rexrov/sl3_points"
    JOINT_TOPIC = "/rexrov/joint_states"

    def __init__(self):
        import rospy
        from gazebo_msgs.msg import ModelStates
        from gazebo_msgs.srv import GetModelState, GetPhysicsProperties, SetModelState, SetModelStateRequest
        from geometry_msgs.msg import Twist
        from rosgraph_msgs.msg import Clock
        from sensor_msgs.msg import Image, JointState, PointCloud2
        rospy.init_node("dave_cluster_runtime", anonymous=True, disable_signals=True)
        self.rospy = rospy
        self.ModelStates = ModelStates
        self.Clock = Clock
        self.GetModelState = GetModelState
        self.GetPhysicsProperties = GetPhysicsProperties
        self.SetModelStateRequest = SetModelStateRequest
        self.SetModelState = SetModelState
        self.JointState = JointState
        self.model_states = self.clock = self.rgb = self.cloud = self.joints = None
        self.received = {}
        self.clock_value = 0.0
        self.clock_advanced = time.monotonic()
        self.paused = False
        self.zero_pub = rospy.Publisher("/rexrov/cmd_vel", Twist, queue_size=1)
        self.subs = [rospy.Subscriber("/gazebo/model_states", ModelStates, self._set("models")),
                     rospy.Subscriber("/clock", Clock, self._clock_callback)]
        self.rgb_subs = [rospy.Subscriber(topic, Image, self._set("rgb")) for topic in self.RGB_TOPICS]
        self.subs.extend(self.rgb_subs)
        self.subs.extend([rospy.Subscriber(self.CLOUD_TOPIC, PointCloud2, self._set("cloud")),
                          rospy.Subscriber(self.JOINT_TOPIC, JointState, self._set("joints"))])

    def _set(self, key):
        def callback(message):
            setattr(self, "model_states" if key == "models" else key, message)
            self.received[key] = time.monotonic()
        return callback

    def _clock_callback(self, message):
        value = message.clock.to_sec()
        if value > self.clock_value:
            self.clock_advanced = time.monotonic()
        self.clock_value = value
        self.clock = message
        self.received["clock"] = time.monotonic()

    def wait_for_ros(self, timeout):
        deadline = time.monotonic() + timeout
        self.rospy.wait_for_service("/gazebo/get_model_state", timeout=timeout)
        while time.monotonic() < deadline:
            if self.model_states is not None and self.clock is not None and self.rgb is not None and self.cloud is not None:
                return
            time.sleep(.1)  # wall time, so paused /clock cannot stall readiness timeout
        raise RuntimeError("required Gazebo/clock/RGB/XYZ messages did not arrive")

    def service_call(self, name, service_type, *args, timeout=15):
        self.rospy.wait_for_service(name, timeout=min(timeout, 10))
        proxy = self.rospy.ServiceProxy(name, service_type)
        result = queue.Queue(maxsize=1)
        def invoke():
            try: result.put((True, proxy(*args)))
            except Exception as error: result.put((False, error))
        threading.Thread(target=invoke, daemon=True).start()
        try:
            ok, value = result.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("ROS service {} timed out".format(name))
        if not ok:
            raise value
        return value

    @staticmethod
    def finite(values):
        return all(math.isfinite(float(value)) for value in values)

    def _fresh(self, key, seconds=5):
        if time.monotonic() - self.received.get(key, 0) > seconds:
            raise RuntimeError("{} sensor/state message is stale".format(key))

    def validate_health(self, allow_paused=False):
        paused = self.is_paused()
        if paused and not allow_paused:
            raise RuntimeError("Gazebo is paused")
        if not paused:
            self._fresh("clock")
            self._fresh("rgb")
            self._fresh("cloud")
        elif any(key not in self.received for key in ("clock", "rgb", "cloud")):
            raise RuntimeError("paused simulator has not produced required sensor messages")
        if self.clock is None:
            raise RuntimeError("ROS clock message is missing")
        model_state = self.service_call("/gazebo/get_model_state", self.GetModelState, "rexrov", "world")
        if not model_state.success:
            raise RuntimeError("RexROV missing from Gazebo: {}".format(model_state.status_message))
        xyz = (model_state.pose.position.x, model_state.pose.position.y, model_state.pose.position.z)
        if not self.finite(xyz):
            raise RuntimeError("RexROV pose contains nonfinite xyz")
        if self.clock is None or not math.isfinite(self.clock.clock.to_sec()) or self.clock.clock.to_sec() <= 0:
            raise RuntimeError("ROS clock is not finite/valid")
        if not paused and time.monotonic() - self.clock_advanced > 5:
            raise RuntimeError("ROS clock is not advancing")
        image = self.rgb
        if image is None or image.width <= 0 or image.height <= 0 or image.encoding not in ("rgb8", "bgr8", "rgba8", "bgra8"):
            raise RuntimeError("no valid RGB image")
        channels = 4 if image.encoding in ("rgba8", "bgra8") else 3
        if image.step < image.width * channels or len(image.data) < image.step * image.height:
            raise RuntimeError("RGB image data is incomplete")
        cloud = self.cloud
        if cloud is None or cloud.width <= 0 or cloud.height <= 0:
            raise RuntimeError("no XYZ point cloud")
        if cloud.point_step < 12 or cloud.row_step < cloud.width * cloud.point_step or len(cloud.data) < cloud.row_step * cloud.height:
            raise RuntimeError("XYZ point cloud payload is incomplete")
        from sensor_msgs.point_cloud2 import read_points
        iterator = read_points(cloud, field_names=("x", "y", "z"), skip_nans=False)
        checked = 0
        for index, point in enumerate(iterator):
            if not self.finite(point):
                raise RuntimeError("point cloud contains nonfinite XYZ")
            checked += 1
            if index >= min(127, cloud.width * cloud.height - 1):
                break
        if checked == 0:
            raise RuntimeError("point cloud has no XYZ samples")

    def is_paused(self):
        response = self.service_call("/gazebo/get_physics_properties", self.GetPhysicsProperties)
        self.paused = bool(response.pause)
        return self.paused

    def wait_pause_state(self, desired, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_paused() is desired:
                return
            time.sleep(.1)
        raise RuntimeError("Gazebo did not change to the requested pause state")

    def pause(self):
        from std_srvs.srv import Empty
        self.stop_input()
        if not self.is_paused():
            self.service_call("/gazebo/pause_physics", Empty)
            self.wait_pause_state(True)

    def resume(self):
        from std_srvs.srv import Empty
        if self.is_paused():
            self.service_call("/gazebo/unpause_physics", Empty)
            self.wait_pause_state(False)

    def stop_input(self):
        from geometry_msgs.msg import Twist
        self.zero_pub.publish(Twist())

    def reset(self):
        from std_srvs.srv import Empty
        self.stop_input()
        self.service_call("/gazebo/reset_simulation", Empty)
        self.clock_value = 0.0
        self.clock_advanced = time.monotonic()
        self.paused = self.is_paused()

    def snapshot(self, pause_state):
        model_state = self.service_call("/gazebo/get_model_state", self.GetModelState, "rexrov", "world")
        if not model_state.success:
            raise RuntimeError("could not query RexROV for checkpoint")
        pose, twist = model_state.pose, model_state.twist
        joints = self.joints
        if joints is None:
            joints = self.rospy.wait_for_message(self.JOINT_TOPIC, self.JointState, timeout=5)
        state_values = ([pose.position.x, pose.position.y, pose.position.z,
                         pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w,
                         twist.linear.x, twist.linear.y, twist.linear.z,
                         twist.angular.x, twist.angular.y, twist.angular.z] + list(joints.position))
        if len(joints.name) != len(joints.position) or not self.finite(state_values):
            raise RuntimeError("RexROV pose/twist/joint state is incomplete or nonfinite")
        return {"schema": 1, "model": "rexrov", "pause_state": bool(pause_state),
                "pose": {"position": [pose.position.x, pose.position.y, pose.position.z],
                         "orientation": [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w]},
                "twist": {"linear": [twist.linear.x, twist.linear.y, twist.linear.z],
                          "angular": [twist.angular.x, twist.angular.y, twist.angular.z]},
                "joints": {"names": list(joints.name), "positions": list(joints.position)},
                "simulation_time": self.clock.clock.to_sec(),
                "fidelity": "explicit_state_only"}

    def restore(self, state):
        if state.get("schema") != 1 or state.get("model") != "rexrov":
            raise RuntimeError("unsupported checkpoint schema/model")
        if type(state.get("pause_state")) is not bool:
            raise RuntimeError("checkpoint pause state is missing or invalid")
        arrays = state["pose"]["position"] + state["pose"]["orientation"] + state["twist"]["linear"] + state["twist"]["angular"]
        if not self.finite(arrays):
            raise RuntimeError("nonfinite checkpoint values")
        req = self.SetModelStateRequest()
        model = req.model_state
        model.model_name = "rexrov"
        pos, ori = state["pose"]["position"], state["pose"]["orientation"]
        model.pose.position.x, model.pose.position.y, model.pose.position.z = pos
        model.pose.orientation.x, model.pose.orientation.y, model.pose.orientation.z, model.pose.orientation.w = ori
        lin, ang = state["twist"]["linear"], state["twist"]["angular"]
        model.twist.linear.x, model.twist.linear.y, model.twist.linear.z = lin
        model.twist.angular.x, model.twist.angular.y, model.twist.angular.z = ang
        model.reference_frame = "world"
        if not self.is_paused():
            self.pause()
        if not self.service_call("/gazebo/set_model_state", self.SetModelState, req).success:
            raise RuntimeError("RexROV pose restore failed")
        joints = state.get("joints", {})
        if joints.get("names"):
            if len(joints["names"]) != len(joints.get("positions", [])) or not self.finite(joints["positions"]):
                raise RuntimeError("invalid checkpoint joint configuration")
            from gazebo_msgs.srv import SetModelConfiguration
            result = self.service_call("/gazebo/set_model_configuration", SetModelConfiguration,
                                       "rexrov", "", joints["names"], joints["positions"])
            if not result.success:
                raise RuntimeError("joint configuration restore failed")
        if state["pause_state"]:
            if not self.is_paused():
                self.pause()
        else:
            self.resume()
