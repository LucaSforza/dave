#!/bin/bash
# Slurm batch supervisor. All ROS, desktop, and simulation processes share one
# Singularity execution and its namespaces; the host only supervises that process.
set -u
umask 077
ROOT=$1 JOB=$2 INDEX=$3
ALLOC="$ROOT/jobs/$JOB/allocations/$INDEX"
mkdir -p "$ALLOC"
chmod 700 "$ALLOC"
mkdir -p "$ALLOC/home"
chmod 700 "$ALLOC/home"
rm -f "$ALLOC/runtime.json"
python3 - "$ALLOC/runtime.json" <<'PY'
import json,os,sys,tempfile,time
p=sys.argv[1]
fd,tmp=tempfile.mkstemp(prefix='.runtime-',dir=os.path.dirname(p))
with os.fdopen(fd,'w') as f:
    json.dump({'ready':False,'rollover':False,'checkpoint':None,'control_results':{},'desktop':None,'error':None,'timestamp':time.time()},f)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp,p)
PY
write_error() {
  python3 - "$ALLOC/runtime.json" "$1" <<'PY'
import json,os,sys,tempfile,time
p,msg=sys.argv[1],sys.argv[2]
try:
    data=json.load(open(p))
except Exception:
    data={'checkpoint':None,'control_results':{},'desktop':None}
if not data.get('error'):
    data.update({'ready':False,'rollover':False,'error':msg,'timestamp':time.time()})
    fd,tmp=tempfile.mkstemp(prefix='.runtime-',dir=os.path.dirname(p))
    with os.fdopen(fd,'w') as f:
        json.dump(data,f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,p)
PY
}
CONTAINER_PID=
STOPPING=0
ROLLOVER=0
finish() {
  local signal=${1:-TERM}
  [[ $STOPPING == 1 ]] && return
  STOPPING=1
  if [[ -n "$CONTAINER_PID" ]] && kill -0 "$CONTAINER_PID" 2>/dev/null; then
    kill -"$signal" "$CONTAINER_PID" 2>/dev/null || true
    for _ in $(seq 1 180); do
      kill -0 "$CONTAINER_PID" 2>/dev/null || break
      sleep 1
    done
    kill -TERM "$CONTAINER_PID" 2>/dev/null || true
    wait "$CONTAINER_PID" 2>/dev/null || true
  fi
}
on_usr1() { ROLLOVER=1; finish USR1; }
trap 'on_usr1' USR1
trap 'finish TERM; exit 143' TERM INT
CONFIG="$ROOT/config.json"
IMAGE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$CONFIG") || { write_error 'remote config invalid'; exit 1; }
USE_NV=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1])).get("use_nvidia") is True else "false")' "$CONFIG") || { write_error 'remote config invalid'; exit 1; }
[[ "$IMAGE" == /* && -f "$IMAGE" ]] || { write_error 'configured absolute runtime image is absent on compute node'; exit 1; }
CONTAINER=$(command -v singularity || command -v apptainer) || { write_error 'SingularityCE/Apptainer unavailable'; exit 1; }
CONTAINER_ARGS=()
[[ "$USE_NV" == true ]] && CONTAINER_ARGS+=(--nv)
"$CONTAINER" exec "${CONTAINER_ARGS[@]}" --contain --no-home --home "$ALLOC/home" \
  --pwd "$ALLOC" --bind "$ROOT:$ROOT" "$IMAGE" /bin/bash -lc \
  'source /opt/ros/noetic/setup.bash && source /opt/dave/devel/setup.bash && exec python3 "$1/runtime/session.py" --root "$1" --job "$2" --index "$3"' \
  dave-runtime "$ROOT" "$JOB" "$INDEX" >>"$ALLOC/runtime.log" 2>&1 &
CONTAINER_PID=$!
wait "$CONTAINER_PID"
status=$?
if [[ $ROLLOVER == 1 ]]; then
  if python3 - "$ALLOC/runtime.json" "$JOB" "$INDEX" <<'PY'
import json,sys
try:
    m=json.load(open(sys.argv[1])); c=m.get("checkpoint") or {}
    ok=(m.get("ready") is False and m.get("rollover") is True and c.get("complete") is True
        and c.get("job_id")==sys.argv[2] and c.get("allocation_index")==int(sys.argv[3])
        and c.get("scenario")=="uw_lidar" and c.get("fidelity")=="explicit_state_only")
    sys.exit(0 if ok else 1)
except Exception: sys.exit(1)
PY
  then status=0; else status=1; fi
fi
if [[ $status -ne 0 ]]; then write_error "runtime/container exited with status $status"; fi
exit "$status"
