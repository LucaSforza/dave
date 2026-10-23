set shell := ["bash", "-euo", "pipefail", "-c"]

launch := env_var_or_default("LAUNCH", "dave_uw_lidar_demo.launch")
gui := env_var_or_default("GUI", "false")

default:
    @just --list

setup:
    bash docker/run.sh setup

run:
    GUI={{gui}} LAUNCH={{launch}} bash docker/run.sh demo

stop:
    bash docker/run.sh stop

# Start the local backend and request/reuse a RexROV cluster session.
[positional-arguments]
run-cluster *args:
    uv run --locked --project backend python client/run_cluster.py "$@"

backend-up:
    bash backend/compose.sh up -d --build

backend-down:
    bash backend/compose.sh down

backend-logs:
    bash backend/compose.sh logs -f api worker redis

backend-test:
    uv sync --locked --project backend
    uv run --locked --project backend pytest backend/tests cluster/tests

[positional-arguments]
backend-client *args:
    uv run --locked --project backend python client/dave_client.py "$@"
