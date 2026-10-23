import base64
import json
import os
import tempfile
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runtime.dispatch import Dispatcher, DispatchError


class Runner:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}
        self.handler = None

    def __call__(self, argv, **kwargs):
        import subprocess
        self.calls.append(argv)
        if self.handler:
            result = self.handler(argv)
            if result is not None:
                code, out = result
                return subprocess.CompletedProcess(argv, code, out, "")
        out = self.replies.get(argv[0], "")
        if argv[0] == "sbatch": out = "12345"
        return subprocess.CompletedProcess(argv, 0, out, "")


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.job = str(uuid.uuid4())
        self.runner = Runner({"squeue": ""})
        self.d = Dispatcher(self.root, self.runner)

    def tearDown(self): self.temp.cleanup()

    def call(self, op, **values):
        return self.d.run(dict(operation=op, **values))

    def test_rejects_injection_invalid_id_and_operation(self):
        with self.assertRaises(DispatchError): self.call("submit;touch bad")
        with self.assertRaises(DispatchError): self.call("logs", job_id="../../tmp", allocation_index=1)
        with self.assertRaises(DispatchError): self.call("control", job_id=self.job, allocation_index=1, action=";rm")

    def test_symlink_artifact_and_download_confined(self):
        job = os.path.join(self.root, "jobs", self.job)
        os.makedirs(job)
        outside = os.path.join(self.root, "outside")
        with open(outside, "w") as f: f.write("secret")
        os.symlink(outside, os.path.join(job, "escape"))
        self.assertEqual(self.call("artifacts", job_id=self.job)["artifacts"], [])
        with self.assertRaises(DispatchError): self.call("download", job_id=self.job, path="escape")

    def test_controls_atomic_and_allowlisted(self):
        result = self.call("control", job_id=self.job, allocation_index=1, action="pause")
        self.assertEqual(result["sequence"], 1)
        with self.assertRaises(DispatchError): self.call("control", job_id=self.job, allocation_index=1, action="shell")

    def test_log_bound(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        with open(os.path.join(alloc, "runtime.log"), "wb") as f: f.write(b"x" * 70000)
        r = self.call("logs", job_id=self.job, allocation_index=1, limit=999999)
        self.assertEqual(len(r["text"]), 65536)

    def test_submission_idempotent_and_quota(self):
        image = os.path.join(self.root, "fake.sif")
        open(image, "w").close()
        with open(os.path.join(self.root, "config.json"), "w") as f: json.dump({"image": image}, f)
        p = {"job_id": self.job, "allocation_index": 1, "request": {}}
        first = self.d.op_submit(p)
        second = self.d.op_submit(p)
        self.assertEqual(first, second)
        self.assertEqual(sum(c[0] == "sbatch" for c in self.runner.calls), 1)
        sbatch = next(c for c in self.runner.calls if c[0] == "sbatch")
        with open(sbatch[-1]) as stream: script = stream.read()
        self.assertIn("#SBATCH --partition=students", script)
        self.assertIn("#SBATCH --gres=gpu:1", script)
        self.assertIn("--signal=B:USR1@180", script)
        self.assertTrue(any(arg.startswith("--output=") for arg in sbatch))

    def test_ambiguous_recovery_by_exact_name(self):
        name = "dave-{}-1".format(self.job.replace("-", ""))
        self.runner.replies["squeue"] = "555|{}".format(name)
        got = self.call("submit", job_id=self.job, allocation_index=1)
        self.assertEqual(got["slurm_job_id"], "555")
        self.assertFalse(any(c[0] == "sbatch" for c in self.runner.calls))

    def test_unresolved_intent_fails_closed_without_resubmit(self):
        p = {"job_id": self.job, "allocation_index": 1, "request": {}}
        _, alloc = self.d.alloc(p)
        with open(os.path.join(alloc, "submission.intent.json"), "w") as f:
            json.dump({"job_name": "dave-{}-1".format(self.job.replace("-", ""))}, f)
        with self.assertRaises(DispatchError) as ctx: self.d.op_submit(p)
        self.assertEqual(ctx.exception.code, "submission_ambiguous")
        self.assertFalse(any(c[0] == "sbatch" for c in self.runner.calls))

    def test_inspect_unresolved_intent_is_unknown_not_not_submitted(self):
        p = {"job_id": self.job, "allocation_index": 1}
        _, alloc = self.d.alloc(p)
        with open(os.path.join(alloc, "submission.intent.json"), "w") as f: json.dump({}, f)
        result = self.call("inspect", **p)
        self.assertEqual(result["scheduler_state"], "UNKNOWN")
        self.assertIsNone(result["slurm_job_id"])

    def test_json_error_protocol_writes_one_response_to_stdout(self):
        script = os.path.join(os.path.dirname(__file__), "..", "runtime", "dispatch.py")
        result = subprocess.run([sys.executable, script, "not-an-operation", "--root", self.root],
                                input="{}", text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_request")

    def test_squeue_quota_probe_failure_fails_closed(self):
        image = os.path.join(self.root, "fake.sif")
        open(image, "w").close()
        with open(os.path.join(self.root, "config.json"), "w") as f: json.dump({"image": image}, f)
        self.runner.handler = lambda argv: (1, "") if argv[0] == "squeue" else None
        with self.assertRaises(DispatchError): self.call("submit", job_id=self.job, allocation_index=1, request={})
        self.assertFalse(any(c[0] == "sbatch" for c in self.runner.calls))

    def test_integrity_download_roundtrip_bounded(self):
        job = os.path.join(self.root, "jobs", self.job)
        os.makedirs(job)
        os.makedirs(os.path.join(job, "artifacts"))
        with open(os.path.join(job, "artifacts", "ok.bin"), "wb") as f: f.write(b"abc")
        response = self.call("download", job_id=self.job, path="artifacts/ok.bin")
        self.assertEqual(base64.b64decode(response["data_base64"]), b"abc")

    def test_inspect_accounting_unknown_and_slurm_states(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        with open(os.path.join(alloc, "submission.json"), "w") as f: json.dump({"slurm_job_id": "123"}, f)
        self.runner.replies.update({"squeue": "RUNNING", "sacct": ""})
        self.assertEqual(self.call("inspect", job_id=self.job, allocation_index=1)["scheduler_state"], "RUNNING")
        self.runner.replies["squeue"] = ""
        self.runner.replies["sacct"] = "COMPLETED+"
        self.assertEqual(self.call("inspect", job_id=self.job, allocation_index=1)["scheduler_state"], "COMPLETED")

    def test_inspect_intent_recovers_exact_queue_job_and_receipt(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        name = "dave-{}-1".format(self.job.replace("-", ""))
        with open(os.path.join(alloc, "submission.intent.json"), "w") as f: json.dump({"job_name": name}, f)
        self.runner.replies["squeue"] = "765|{}".format(name)
        result = self.call("inspect", job_id=self.job, allocation_index=1)
        self.assertEqual(result["slurm_job_id"], "765")
        self.assertTrue(os.path.isfile(os.path.join(alloc, "submission.json")))

    def test_squeue_invalid_job_falls_back_to_sacct(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        with open(os.path.join(alloc, "submission.json"), "w") as f: json.dump({"slurm_job_id": "321"}, f)
        self.runner.handler = lambda argv: (1, "") if argv[0] == "squeue" and "-j" in argv else None
        self.runner.replies["sacct"] = "COMPLETED+|"
        self.assertEqual(self.call("inspect", job_id=self.job, allocation_index=1)["scheduler_state"], "COMPLETED")

    def test_recovered_intent_with_purged_squeue_uses_accounting(self):
        p = {"job_id": self.job, "allocation_index": 1}
        _, alloc = self.d.alloc(p)
        name = "dave-{}-1".format(self.job.replace("-", ""))
        with open(os.path.join(alloc, "submission.intent.json"), "w") as f: json.dump({}, f)
        self.runner.handler = lambda argv: ((0, "900|{}".format(name)) if argv[0] == "squeue" and "-u" in argv else
                                            (1, "") if argv[0] == "squeue" and "-j" in argv else
                                            (0, "COMPLETED|") if argv[0] == "sacct" and "--format=State%40" in argv else
                                            (0, "900|{}|COMPLETED|".format(name)) if argv[0] == "sacct" else None)
        result = self.call("inspect", **p)
        self.assertEqual(result["scheduler_state"], "COMPLETED")
        self.assertEqual(result["slurm_job_id"], "900")

    def test_rollover_submission_requires_completed_prior_and_full_manifest(self):
        image = os.path.join(self.root, "fake.sif")
        open(image, "w").close()
        with open(os.path.join(self.root, "config.json"), "w") as f: json.dump({"image": image}, f)
        p1 = {"job_id": self.job, "allocation_index": 1}
        _, old = self.d.alloc(p1)
        name = "dave-{}-1".format(self.job.replace("-", ""))
        with open(os.path.join(old, "submission.json"), "w") as f: json.dump({"slurm_job_id": "77", "job_name": name}, f)
        checkpoint = {"version": 1, "complete": True, "job_id": self.job, "scenario": "uw_lidar",
                      "allocation_index": 1, "created": 1, "snapshot": "1-1.json", "sha256": "a" * 64,
                      "fidelity": "explicit_state_only", "rollover": True, "manifest_path": "1-1.manifest.json"}
        with open(os.path.join(old, "runtime.json"), "w") as f:
            json.dump({"ready": False, "rollover": True, "checkpoint": checkpoint}, f)
        self.runner.handler = lambda argv: (0, "COMPLETED") if argv[0] == "sacct" else None
        result = self.call("submit", job_id=self.job, allocation_index=2, request={}, checkpoint=checkpoint)
        self.assertEqual(result["status"], "submitted")
        _, new = self.d.alloc({"job_id": self.job, "allocation_index": 2})
        with open(os.path.join(new, "resume.json")) as f: self.assertEqual(json.load(f), checkpoint)

    def test_cancel_targets_only_receipted_numeric_job(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        with open(os.path.join(alloc, "submission.json"), "w") as f: json.dump({"slurm_job_id": "4321"}, f)
        self.assertEqual(self.call("cancel", job_id=self.job, allocation_index=1)["status"], "cancel_requested")
        self.assertIn(["scancel", "4321"], self.runner.calls)
        with open(os.path.join(alloc, "submission.json"), "w") as f: json.dump({"slurm_job_id": "4321;rm"}, f)
        with self.assertRaises(DispatchError): self.call("cancel", job_id=self.job, allocation_index=1)

    def test_cancel_recovers_lost_receipt_by_exact_deterministic_name(self):
        _, alloc = self.d.alloc({"job_id": self.job, "allocation_index": 1})
        name = "dave-{}-1".format(self.job.replace("-", ""))
        with open(os.path.join(alloc, "submission.intent.json"), "w") as f: json.dump({}, f)
        self.runner.replies["squeue"] = "875|{}".format(name)
        result = self.call("cancel", job_id=self.job, allocation_index=1)
        self.assertEqual(result["status"], "cancel_requested")
        self.assertIn(["scancel", "875"], self.runner.calls)

    def test_artifact_download_denies_private_files_and_unapproved_paths(self):
        job = os.path.join(self.root, "jobs", self.job)
        os.makedirs(os.path.join(job, "artifacts"))
        with open(os.path.join(job, "artifacts", ".vnc-secret"), "w") as f: f.write("private")
        with open(os.path.join(job, "runtime.json"), "w") as f: f.write("private")
        with self.assertRaises(DispatchError): self.call("download", job_id=self.job, path="artifacts/.vnc-secret")
        with self.assertRaises(DispatchError): self.call("download", job_id=self.job, path="runtime.json")
        self.assertEqual(self.call("artifacts", job_id=self.job)["artifacts"], [])


if __name__ == "__main__": unittest.main()
