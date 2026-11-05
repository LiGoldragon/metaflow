import json
import os
from pathlib import Path
import signal
import socket
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import metaflow


def base_config(root):
    true = os.path.realpath("/bin/true")
    return {
        "runtimeRootEnvironment": "XDG_RUNTIME_DIR", "stateRootEnvironment": "XDG_STATE_HOME",
        "stateRootFallback": ".local/state", "uidRule": "current-login",
        "runtimeNamespace": "metaflow", "stateNamespace": "metaflow",
        "startupTimeoutSeconds": .2, "commandTimeoutSeconds": .2,
        "bootstrapPollMilliseconds": 1, "stopGraceSeconds": .05,
        "herdr": {"metaCli": true, "herdr": true,
                  "readyDatoms": ["one", "two"], "peerDatom": "peer %UID% %PID%"},
        "belief": {"cli": true, "metaCli": true, "resolveDatom": "resolve",
                   "readyDatom": "ready", "peerDatom": "peer %UID% %PID%"},
        "flow": {"cli": true, "readyDatom": "definitions"},
    }


class MetaflowTests(unittest.TestCase):
    def make(self, root, runner=subprocess.run):
        env = {"XDG_RUNTIME_DIR": str(Path(root) / "run"),
               "XDG_STATE_HOME": str(Path(root) / "state")}
        return metaflow.Composition(base_config(root), "test", env, runner)

    def test_start_reports_missing_component_fields(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            with self.assertRaisesRegex(metaflow.CompositionError, "component herdr is missing: pair"):
                composition.start()

    def test_deployment_configuration_shape_is_typed(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "shape.json"
            config.write_text("[]")
            with mock.patch.dict(os.environ, {"METAFLOW_DEPLOYMENT_CONFIG": str(config)}), \
                 mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
                self.assertEqual(metaflow.main(["start", "test"]), 1)
            self.assertIn("must be an object", stderr.getvalue())

    def test_invalid_deployment_configuration_is_user_facing_error(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "broken.json"
            config.write_text("{")
            with mock.patch.dict(os.environ, {"METAFLOW_DEPLOYMENT_CONFIG": str(config)}), \
                 mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
                self.assertEqual(metaflow.main(["start", "test"]), 1)
            self.assertIn("invalid deployment configuration", stderr.getvalue())

    def test_place_rejects_both_datom_delimiters_in_paths(self):
        with tempfile.TemporaryDirectory() as root:
            env = {"XDG_RUNTIME_DIR": str(Path(root) / "run«bad"),
                   "XDG_STATE_HOME": str(Path(root) / "state")}
            with self.assertRaisesRegex(metaflow.CompositionError, "datom delimiters"):
                metaflow.Composition(base_config(root), "test", env)

    def test_path_conflict_refuses_without_changing_contents(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.runtime.mkdir(parents=True)
            sentinel = composition.runtime / "unrelated"
            sentinel.write_text("keep")
            with self.assertRaisesRegex(metaflow.CompositionError, "runtime path conflict"):
                composition.prepare_paths()
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertFalse(composition.state.exists())



    def test_preexisting_empty_runtime_is_stale_and_refused(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.runtime.mkdir(parents=True)
            with self.assertRaisesRegex(metaflow.CompositionError, "runtime path conflict"):
                composition.prepare_paths()
            composition.cleanup()
            self.assertTrue(composition.runtime.is_dir())

    def test_cleanup_never_removes_replacement_runtime_directory(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.prepare_paths()
            # Simulate an external replacement after the owned directory moved.
            moved = composition.runtime.with_name("owned-moved")
            composition.runtime.rename(moved)
            composition.runtime.mkdir()
            composition.cleanup()
            self.assertTrue(composition.runtime.is_dir())
            self.assertTrue((moved / ".metaflow-owner").is_file())

    def test_concurrent_starters_have_one_owner_and_loser_cleanup_is_rival_safe(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.make(root)
            second = self.make(root)
            gate = threading.Barrier(3)
            outcomes = []
            def claim(composition):
                gate.wait()
                try:
                    composition.prepare_paths()
                    outcomes.append(("won", composition))
                except metaflow.CompositionError:
                    outcomes.append(("lost", composition))
            threads = [threading.Thread(target=claim, args=(item,)) for item in (first, second)]
            for thread in threads: thread.start()
            gate.wait()
            for thread in threads: thread.join()
            self.assertEqual(sorted(kind for kind, _ in outcomes), ["lost", "won"])
            winner = next(item for kind, item in outcomes if kind == "won")
            loser = next(item for kind, item in outcomes if kind == "lost")
            rival = winner.runtime / "rival-resource"
            rival.write_text("keep")
            loser.cleanup()
            self.assertEqual(rival.read_text(), "keep")
            self.assertTrue((winner.runtime / ".metaflow-owner").exists())
            winner.cleanup()
            self.assertEqual(rival.read_text(), "keep")
            self.assertTrue(winner.state.exists())

    def test_partial_startup_cleanup_preserves_state_and_unrelated_process(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.prepare_paths()
            state_file = composition.state / "memory"
            state_file.write_text("persistent")
            owned = [subprocess.Popen([shutil.which("sleep"), "30"]) for _ in range(3)]
            unrelated = subprocess.Popen([shutil.which("sleep"), "30"])
            try:
                identities = [metaflow.ProcessIdentity.capture(
                    role, proc.pid, os.getpid(), os.getuid(), shutil.which("sleep"))
                    for role, proc in zip(("pair", "daemon", "translator"), owned)]
                sock_path = composition.runtime / "owned.sock"
                server = socket.socket(socket.AF_UNIX)
                server.bind(str(sock_path)); server.close()
                pair = metaflow.Pair("fake", owned[0], identities[0], identities[1], identities[2],
                                     composition.owned_files[0],
                                     [metaflow.OwnedInode.capture(sock_path, "socket")])
                composition.pairs.append(pair)
                composition.cleanup()
                for proc in owned:
                    proc.wait(timeout=2)
                self.assertIsNone(unrelated.poll())
                self.assertEqual(state_file.read_text(), "persistent")
                self.assertFalse(sock_path.exists())
            finally:
                unrelated.terminate(); unrelated.wait(timeout=2)
                for proc in owned:
                    if proc.poll() is None:
                        proc.kill(); proc.wait()


    def test_failed_preidentity_spawn_is_cleaned_by_retained_pidfd(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.prepare_paths()
            owned = subprocess.Popen([shutil.which("sleep"), "30"])
            unrelated = subprocess.Popen([shutil.which("sleep"), "30"])
            try:
                composition.provisionals.append((owned, os.pidfd_open(owned.pid)))
                composition.cleanup()
                owned.wait(timeout=2)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate(); unrelated.wait(timeout=2)
                if owned.poll() is None:
                    owned.kill(); owned.wait()

    def test_replaced_socket_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "socket"
            first = socket.socket(socket.AF_UNIX); first.bind(str(path))
            owned = metaflow.OwnedInode.capture(path, "socket")
            first.close(); path.unlink()
            replacement = socket.socket(socket.AF_UNIX); replacement.bind(str(path))
            try:
                self.assertFalse(owned.still_same())
                self.assertTrue(path.exists())
                owned.close()
            finally:
                replacement.close(); path.unlink()

    def test_zombie_pidfd_identity_is_rejected(self):
        proc = subprocess.Popen([shutil.which("sleep"), "30"])
        identity = metaflow.ProcessIdentity.capture("owned", proc.pid, os.getpid(), os.getuid(), shutil.which("sleep"))
        try:
            with mock.patch("metaflow.proc_is_zombie", return_value=True):
                with self.assertRaisesRegex(metaflow.CompositionError, "dead"):
                    identity.verify()
        finally:
            identity.send(signal.SIGTERM)
            proc.wait(timeout=2)
            os.close(identity.pidfd)

    def test_dead_pidfd_identity_is_rejected(self):
        proc = subprocess.Popen([shutil.which("sleep"), "30"])
        identity = metaflow.ProcessIdentity.capture("owned", proc.pid, os.getpid(), os.getuid(), shutil.which("sleep"))
        identity.send(signal.SIGTERM); proc.wait(timeout=2)
        with self.assertRaisesRegex(metaflow.CompositionError, "dead"):
            identity.verify()
        os.close(identity.pidfd)

    def test_missing_and_ambiguous_translator_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            parent = SimpleNamespace(pid=40)
            with mock.patch.object(composition, "_children", return_value=[]):
                self.assertIsNone(composition._select_child(parent, "translator", "/x"))
            fields = [(40, 1, os.getuid(), "/x"), (40, 2, os.getuid(), "/x")]
            with mock.patch.object(composition, "_children", return_value=[41, 42]), \
                 mock.patch("metaflow.proc_fields", side_effect=fields):
                with self.assertRaisesRegex(metaflow.CompositionError, "ambiguous"):
                    composition._select_child(parent, "translator", "/x")

    def test_exit_before_pidfd_registration_is_reaped_and_typed(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.prepare_paths()
            true = shutil.which("true")
            composition.config["flow"].update({
                "pair": true, "pairExecutable": true, "daemonExecutable": true,
                "translatorExecutable": true, "sockets": [],
            })
            process = mock.Mock(pid=1234)
            process.poll.return_value = 0
            with mock.patch("metaflow.subprocess.Popen", return_value=process), \
                 mock.patch("metaflow.os.pidfd_open", side_effect=ProcessLookupError()):
                with self.assertRaisesRegex(metaflow.CompositionError, "exited before identity capture"):
                    composition.start_pair("flow")
            process.wait.assert_called_once_with()
            composition.cleanup()

    def test_child_exit_during_identity_capture_is_retryable_absence(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            parent = SimpleNamespace(pid=40)
            with mock.patch.object(composition, "_children", return_value=[41]), \
                 mock.patch("metaflow.proc_fields", return_value=(40, 1, os.getuid(), "/x")), \
                 mock.patch.object(metaflow.ProcessIdentity, "capture", side_effect=ProcessLookupError()):
                self.assertIsNone(composition._select_child(parent, "translator", "/x"))

    def test_daemon_and_translator_must_be_distinct_children(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            parent = SimpleNamespace(pid=40, verify=lambda: None)
            shared = metaflow.ProcessIdentity("daemon", 41, -1, 40, 1, os.getuid(), "/x")
            with mock.patch.object(composition, "_select_child", side_effect=[shared, shared]):
                with self.assertRaisesRegex(metaflow.CompositionError, "share pid 41"):
                    composition._wait_children("flow", parent, {"daemonExecutable": "/x", "translatorExecutable": "/x"}, time.monotonic() + 1)

    def test_semantic_readiness_waits_for_typed_replies(self):
        outputs = iter([
            "Configured.One", "Configured.Two.Ready",
            "Refused.NotReady", "Resolved.{ [] [] }", "Configured.Belief.Ready",
        ])
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, next(outputs), "")
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root, runner)
            composition.verify_all = lambda: None
            composition.ready_herdr()
            composition.ready_belief()

    def test_belief_readiness_does_not_hide_owned_process_failure(self):
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.verify_all = mock.Mock(side_effect=metaflow.CompositionError("owned process ended"))
            with self.assertRaisesRegex(metaflow.CompositionError, "owned process ended"):
                composition.ready_belief()

    def test_command_launch_failure_is_typed(self):
        def runner(argv, **kwargs):
            raise OSError("missing executable")
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root, runner)
            composition.verify_all = lambda: None
            with self.assertRaisesRegex(metaflow.CompositionError, "could not start.*missing executable"):
                composition._command(["missing-cli"])

    def test_flow_readiness_requires_typed_definitions_reply(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "Success", "")
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root, runner)
            composition.verify_all = lambda: None
            with self.assertRaisesRegex(metaflow.CompositionError, "Definitions"):
                composition.ready_flow()

    def test_admission_revalidates_unknown_dead_or_reused_identity(self):
        class Identity:
            uid = os.getuid(); pid = 777
            def __init__(self, fail_at=None): self.calls = 0; self.fail_at = fail_at
            def verify(self):
                self.calls += 1
                if self.calls == self.fail_at:
                    raise metaflow.CompositionError("lifetime identity changed")
        with tempfile.TemporaryDirectory() as root:
            # No retained Flow identity is Unknown.
            composition = self.make(root)
            composition.pairs = []
            with self.assertRaisesRegex(metaflow.CompositionError, "exactly one"):
                composition.admit_flow_peer()
            # A dead identity before admission causes no call.
            dead = Identity(1); calls = []
            composition._flow_translator = lambda: dead
            composition.verify_all = lambda: None
            composition._command = lambda argv: calls.append(argv) or "Configured.Ready"
            with self.assertRaisesRegex(metaflow.CompositionError, "changed"):
                composition.admit_flow_peer()
            self.assertEqual(calls, [])
            # Every before/after boundary of both meta admissions revalidates the
            # same retained lifetime identity. No later admission follows a failure.
            for fail_at, expected_calls in ((2, 1), (3, 1), (4, 2)):
                changed = Identity(fail_at); calls = []
                composition._flow_translator = lambda changed=changed: changed
                with self.assertRaisesRegex(metaflow.CompositionError, "changed"):
                    composition.admit_flow_peer()
                self.assertEqual(len(calls), expected_calls)

    def test_early_child_exit_is_detected_without_restart(self):
        proc = subprocess.Popen([shutil.which("sleep"), "30"])
        identity = metaflow.ProcessIdentity.capture("translator", proc.pid, os.getpid(), os.getuid(), shutil.which("sleep"))
        pair = SimpleNamespace(identities=lambda: [identity], verify=identity.verify)
        with tempfile.TemporaryDirectory() as root:
            composition = self.make(root)
            composition.pairs = [pair]
            identity.send(signal.SIGTERM); proc.wait(timeout=2)
            with self.assertRaisesRegex(metaflow.CompositionError, "dead"):
                composition.verify_all()
        os.close(identity.pidfd)


if __name__ == "__main__":
    unittest.main()
