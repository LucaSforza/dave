#!/usr/bin/env python3
"""Set initial RexROV pose from validated request values after model spawn."""
import math
import queue
import sys
import threading
import time
import rospy
from gazebo_msgs.srv import GetModelState, SetModelState, SetModelStateRequest
from tf.transformations import quaternion_from_euler

def call_bounded(proxy, *args, timeout=10):
    result = queue.Queue(maxsize=1)
    def invoke():
        try: result.put((True, proxy(*args)))
        except Exception as error: result.put((False, error))
    threading.Thread(target=invoke, daemon=True).start()
    try: ok, value = result.get(timeout=timeout)
    except queue.Empty: raise RuntimeError("Gazebo initial-pose service timed out")
    if not ok: raise value
    return value

def main():
    values = [float(v) for v in sys.argv[1:5]]
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise SystemExit("invalid initial RexROV pose")
    x, y, z, yaw = values
    rospy.init_node("dave_initial_pose", anonymous=True)
    rospy.wait_for_service("/gazebo/set_model_state", timeout=120)
    rospy.wait_for_service("/gazebo/get_model_state", timeout=120)
    req = SetModelStateRequest()
    req.model_state.model_name = "rexrov"
    req.model_state.pose.position.x = x
    req.model_state.pose.position.y = y
    req.model_state.pose.position.z = z
    q = quaternion_from_euler(0, 0, yaw)
    req.model_state.pose.orientation.x, req.model_state.pose.orientation.y, req.model_state.pose.orientation.z, req.model_state.pose.orientation.w = q
    req.model_state.reference_frame = "world"
    get_state = rospy.ServiceProxy("/gazebo/get_model_state", GetModelState)
    set_state = rospy.ServiceProxy("/gazebo/set_model_state", SetModelState)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if call_bounded(get_state, "rexrov", "world", timeout=5).success:
                result = call_bounded(set_state, req, timeout=10)
                if result.success: return
                raise RuntimeError(result.status_message)
        except rospy.ServiceException:
            pass
        time.sleep(.25)
    raise RuntimeError("RexROV did not spawn before initial-pose timeout")

if __name__ == "__main__": main()
