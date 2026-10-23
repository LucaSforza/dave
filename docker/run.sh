#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(podman-compose -f docker/compose.yaml)

case "${1:-}" in
  setup)
    "${COMPOSE[@]}" build
    "${COMPOSE[@]}" up -d dave
    "${COMPOSE[@]}" exec -T dave bash -lc '
      set -e
      source /opt/ros/noetic/setup.bash
      python3 /catkin_ws/src/dave/docker/import_repos.py \
        /catkin_ws/src/dave/docker/ros-noetic.repos \
        /catkin_ws/src/external
      rosdep update --include-eol-distros
      apt-get update
      rosdep install --from-paths /catkin_ws/src --ignore-src -r -y
      catkin_make
    '
    ;;
  demo)
    "${COMPOSE[@]}" up -d dave
    "${COMPOSE[@]}" exec -T dave env \
      "GUI=${GUI:-true}" \
      "LAUNCH=${LAUNCH:-dave_Santorini_demo.launch}" \
      bash -lc '
      source /opt/ros/noetic/setup.bash
      source /catkin_ws/devel/setup.bash
      roslaunch dave_demo_launch "$LAUNCH" "gui:=${GUI:-true}"
    '
    ;;
  stop)
    "${COMPOSE[@]}" stop dave
    ;;
  *)
    echo "Uso: bash docker/run.sh {setup|demo|stop}" >&2
    echo "Per eseguire senza interfaccia grafica: GUI=false bash docker/run.sh demo" >&2
    exit 2
    ;;
esac
