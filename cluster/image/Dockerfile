# Separate cluster image; built locally or in an allocated compute job, never login.
FROM docker.io/osrf/ros:noetic-desktop-full@sha256:7dbfb9576d8e6d226c31e06129a82aaab8702695f38eca2116918cb9b9308797
ENV DEBIAN_FRONTEND=noninteractive
ARG BUILD_JOBS=2
RUN apt-get update && apt-get install -y --no-install-recommends git python3-rosdep python3-vcstool \
 python3-catkin-tools python3-scipy python3-lxml ros-noetic-effort-controllers \
 ros-noetic-joint-trajectory-controller ros-noetic-kdl-parser-py \
 ros-noetic-moveit-commander ros-noetic-moveit-fake-controller-manager \
 ros-noetic-moveit-kinematics ros-noetic-moveit-planners-chomp \
 ros-noetic-moveit-planners-ompl ros-noetic-moveit-ros-move-group \
 ros-noetic-moveit-ros-visualization ros-noetic-moveit-setup-assistant \
 ros-noetic-moveit-simple-controller-manager ros-noetic-joy \
 ros-noetic-joy-teleop ros-noetic-ros-control ros-noetic-ros-controllers \
 ros-noetic-urdfdom-py ros-noetic-velodyne-gazebo-plugins \
 xvfb openbox xterm xfonts-base x11vnc novnc websockify xauth \
 ros-noetic-gazebo-ros-pkgs ros-noetic-gazebo-ros-control ros-noetic-tf \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/dave/src /opt/dave/scripts
WORKDIR /opt/dave/src
# ROS Noetic remains on distro Python 3.8; the lock file freezes all DAVE source commits.
RUN git clone https://github.com/Field-Robotics-Lab/dave.git dave && cd dave && git checkout e54ea16bdaf7d6dbb466cf141846a03f284a44dd
COPY cluster/image/ros-noetic.repos /opt/dave/ros-noetic.repos
COPY cluster/image/import_locked.py /opt/dave/import_locked.py
RUN python3 /opt/dave/import_locked.py /opt/dave/ros-noetic.repos /opt/dave/src/external
RUN rosdep init || true
# The pinned NPS package declares nvidia-cuda but builds no CUDA targets.
# SL3 uses the installed Velodyne OpenGL plugin, not the CUDA toolkit.
RUN /bin/bash -lc 'source /opt/ros/noetic/setup.bash && rosdep update --include-eol-distros && apt-get update && rosdep install --rosdistro noetic --from-paths /opt/dave/src --ignore-src --skip-keys nvidia-cuda -r -y && rm -rf /var/lib/apt/lists/*'
RUN /bin/bash -lc 'source /opt/ros/noetic/setup.bash && catkin_make -C /opt/dave -DCMAKE_BUILD_TYPE=Release -j"${BUILD_JOBS}" -l"${BUILD_JOBS}"'
COPY cluster/image/cluster-launch /opt/dave/cluster-launch
COPY cluster/image/apply_initial_pose.py /opt/dave/apply_initial_pose.py
COPY cluster/runtime/keyboard_teleop.py /opt/dave/keyboard_teleop.py
RUN chmod 0755 /opt/dave/cluster-launch /opt/dave/apply_initial_pose.py /opt/dave/keyboard_teleop.py
ENV DAVE_WORKSPACE=/opt/dave DISPLAY=:99
ENTRYPOINT ["/bin/bash", "-lc", "source /opt/ros/noetic/setup.bash && source /opt/dave/devel/setup.bash && exec \"$@\"", "--"]
