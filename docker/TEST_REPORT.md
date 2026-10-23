# DAVE ROS Noetic / Gazebo Classic test report

## Environment

- Host changes: none; ROS/Gazebo ran in Podman.
- Base image: `osrf/ros:noetic-desktop-full@sha256:7dbfb9576d8e6d226c31e06129a82aaab8702695f38eca2116918cb9b9308797`.
- DAVE checkout: `e54ea16bdaf7d6dbb466cf141846a03f284a44dd`.
- External Git dependencies: revisions pinned in `docker/ros-noetic.repos`.
- Gazebo Classic 11.15, ROS Noetic. The run used `GUI=false` and the existing `dave_uw_lidar_demo.launch`.
- LiDAR runtime dependency: `ros-noetic-velodyne-gazebo-plugins` 1.0.13, providing `libgazebo_ros_velodyne_gpu_laser.so`.

## Results

| Check | Result | Evidence |
|---|---|---|
| Container setup and catkin build | PASS | `rosdep` reported all required dependencies installed; `catkin_make` reached 100%. |
| RexROV + Oberon7 + SL3 spawn | PASS | Gazebo reported the robot spawned successfully; the Velodyne GPU plugin initialized with 145 lasers. |
| LiDAR data | PASS | `/rexrov/sl3_points`, `sensor_msgs/PointCloud2`; 37 messages in 12 s (about 3.1 Hz), width 21,025, fields `x,y,z,intensity,ring,time`, 462,550 data bytes. |
| RGB camera data | PASS | `/rexrov/image_raw` and the front/left/right camera image topics under `/rexrov/rexrov/...`; each reported `rgb8`, 768×492, 1,133,568 data bytes. Each produced about 3.4 Hz in the 12 s sample. |
| Control and odometry | PASS | Published `linear.x=0.2` on `/rexrov/cmd_vel` for 3 s. `/rexrov/pose_gt` changed from `(3.9990, 3.9989, -93.1189)` to `(3.8823, 3.5022, -93.1846)`, a displacement of approximately `(-0.1166, -0.4967, -0.0657)` m. |
| ROS clock and core topics | PASS | `/clock`, `/rexrov/cmd_vel` (`geometry_msgs/Twist`) and `/rexrov/pose_gt` (`nav_msgs/Odometry`) were present. |

The standard RexROV launch was also previously checked: its DVL, IMU, pressure, RPT, magnetometer, odometry and control topics were present; a 3 s `linear.x=0.3` command changed the measured odometry by about `(-0.186, -0.789, -0.071)` m. That launch did not include LiDAR or camera topics, hence the dedicated SL3 launch used above.

## Limitations and warnings

- The measured sensor rates are from software-rendered, headless Gazebo and are not a real-time performance guarantee.
- The launch logs an unavailable `/dev/input/js0`; joystick input was not tested. Direct ROS velocity commands were tested instead.
- The legacy launch logs a KDL warning about inertia on the root link, a missing optional `pymap3d` package, and Xacro warnings. These did not prevent the measured sensor or motion checks.
- A prior GUI attempt exited with code 134. GUI operation is therefore **not verified**; this report's sensor and motion results are from headless execution only. No GUI client was opened for these checks.
- ROS Noetic and Gazebo Classic are end-of-life. The container pins its base image by digest and Git dependencies by commit, but apt package versions are not all individually locked.

## Reproduction

```bash
bash docker/run.sh setup
GUI=false LAUNCH=dave_uw_lidar_demo.launch bash docker/run.sh demo
```

The demo command remains in the foreground until interrupted. Use `bash docker/run.sh stop` to stop the container afterward.
