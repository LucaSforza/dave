FROM docker.io/osrf/ros:noetic-desktop-full@sha256:7dbfb9576d8e6d226c31e06129a82aaab8702695f38eca2116918cb9b9308797

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      python3-rosdep \
      python3-vcstool \
      python3-catkin-tools \
      python3-scipy \
      python3-lxml \
      ros-noetic-effort-controllers \
      ros-noetic-joint-trajectory-controller \
      ros-noetic-kdl-parser-py \
      ros-noetic-moveit-commander \
      ros-noetic-moveit-fake-controller-manager \
      ros-noetic-moveit-kinematics \
      ros-noetic-moveit-planners-chomp \
      ros-noetic-moveit-planners-ompl \
      ros-noetic-moveit-ros-move-group \
      ros-noetic-moveit-ros-visualization \
      ros-noetic-moveit-setup-assistant \
      ros-noetic-moveit-simple-controller-manager \
      ros-noetic-joy \
      ros-noetic-joy-teleop \
      ros-noetic-ros-control \
      ros-noetic-ros-controllers \
      ros-noetic-urdfdom-py \
      ros-noetic-velodyne-gazebo-plugins \
    && rm -rf /var/lib/apt/lists/*

RUN rosdep init || true
RUN git config --system http.version HTTP/1.1

WORKDIR /workspace
