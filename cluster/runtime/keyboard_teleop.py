#!/usr/bin/env python3
"""Terminal keyboard RexROV teleop. Requires a ROS Noetic environment."""
import argparse
import select
import signal
import sys
import termios
import time
import tty

import rospy
from geometry_msgs.msg import Twist


def main():
    p = argparse.ArgumentParser(description="WASD/arrow-style RexROV movement; hold key for deadman")
    p.add_argument("--topic", default="/rexrov/cmd_vel")
    p.add_argument("--linear", type=float, default=0.5)
    p.add_argument("--angular", type=float, default=0.5)
    p.add_argument("--deadman", type=float, default=0.35)
    a = p.parse_args()
    if not 0 < a.linear <= 1 or not 0 < a.angular <= 1 or not 0.1 <= a.deadman <= 1:
        p.error("speed bounds: linear/angular <=1, deadman 0.1..1 seconds")
    rospy.init_node("dave_keyboard_teleop", anonymous=True)
    pub = rospy.Publisher(a.topic, Twist, queue_size=1)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    last = 0
    def zero(): pub.publish(Twist())
    def interrupt(_signum, _frame): raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, interrupt)
    try:
        tty.setraw(fd)
        print("W/S forward, A/D yaw, R/F vertical, space stop, q quit", file=sys.stderr)
        while not rospy.is_shutdown():
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            now = time.monotonic()
            if ready:
                key = sys.stdin.read(1).lower()
                if key == "q": break
                t = Twist()
                if key == "w": t.linear.x = a.linear
                elif key == "s": t.linear.x = -a.linear
                elif key == "a": t.angular.z = a.angular
                elif key == "d": t.angular.z = -a.angular
                elif key == "r": t.linear.z = a.linear
                elif key == "f": t.linear.z = -a.linear
                pub.publish(t)
                last = now
            elif now - last > a.deadman:
                zero()
    finally:
        zero()
        time.sleep(0.1)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__": main()
