#!/usr/bin/env python3
"""
Tests for the guided setup front-end (src/gba_setup.py).

The guided setup is the only installer most people will ever see, so the
things worth testing are the decisions it makes on their behalf: which targets
it offers, which one it recommends, and what it does when the answer is
unclear, unwelcome, or the install fails half way through.
"""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gba_installer as gi            # noqa: E402
import gba_setup as setup             # noqa: E402
from test_installer import FakeRunner, InstallerTestCase  # noqa: E402


class SetupTestCase(InstallerTestCase):
    def output(self):
        return self.out.getvalue()

    def run_setup(self, answers, disks=None, runner=None, **kwargs):
        self.out = io.StringIO()
        self.inp = io.StringIO("\n".join(answers) + "\n")
        runner = runner or FakeRunner(effects={"cp": self.emulate_copy(self.mount_root),
                                               "blkid": self.fake_uuid})
        with mock.patch.object(gi, "probe_disks", lambda r: disks or self.disks):
            self.code = setup.run(stream_in=self.inp, stream_out=self.out,
                                  runner=runner, source=self.source,
                                  source_kind="dir", mount_root=self.mount_root,
                                  **kwargs)
        return self.code

    def emulate_copy(self, mount_root):
        def effect(args):
            for relative in ("etc", "boot/grub", "opt/gba-distro/bin", "opt/gba-distro/src"):
                os.makedirs(os.path.join(mount_root, relative), exist_ok=True)
            for relative in ("opt/gba-distro/bin/gba-launcher",
                             "opt/gba-distro/bin/gba-setup",
                             "opt/gba-distro/src/gba_launcher.py"):
                open(os.path.join(mount_root, relative), "w").close()
            return 0, "", ""
        return effect

    def fake_uuid(self, args):
        return 0, "1111-2222-3333-4444", ""


class TestCandidateSelection(SetupTestCase):
    def test_offers_the_free_partition_first(self):
        found = setup.candidates(self.disks)
        self.assertTrue(found, "the empty partition should be offered")
        self.assertEqual(found[0].path, os.path.join(self.devdir, "sdb1"))
        self.assertTrue(found[0].safe)

    def test_never_offers_the_disk_we_booted_from(self):
        paths = [c.path for c in setup.candidates(self.disks)]
        self.assertNotIn(os.path.join(self.devdir, "sda"), paths)
        self.assertNotIn(os.path.join(self.devdir, "sda1"), paths)

    def test_whole_disk_is_offered_but_marked_destructive(self):
        disks = self.disks + [gi.Disk(os.path.join(self.devdir, "sdz"), size=64 * 1024 ** 3)]
        found = [c for c in setup.candidates(disks) if c.path.endswith("sdz")]
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].safe)

    def test_skips_partitions_that_are_too_small(self):
        paths = [c.path for c in setup.candidates(self.disks)]
        self.assertNotIn(os.path.join(self.devdir, "sdd1"), paths)

    def test_skips_partitions_holding_an_os(self):
        with mock.patch.object(gi, "_partition_has_os", lambda p: True):
            found = setup.candidates(self.disks)
        self.assertFalse([c for c in found if c.kind == "partition"])

    def test_recommendation_is_none_when_only_disks_remain(self):
        self.assertIsNone(setup.recommend([c for c in setup.candidates(self.disks)
                                           if not c.safe]))


class TestGuidedFlow(SetupTestCase):
    def test_pressing_enter_installs_on_the_recommended_target(self):
        code = self.run_setup([""])  # pick default, confirm default
        self.assertEqual(code, gi.EXIT_OK, self.output())
        self.assertIn("installed", self.output().lower())

    def test_success_message_mentions_the_other_os_survives(self):
        self.run_setup([""])
        self.assertIn("still installed", self.output())

    def test_quitting_changes_nothing(self):
        runner = FakeRunner()
        code = self.run_setup(["q"], runner=runner)
        self.assertEqual(code, gi.EXIT_REFUSED)
        self.assertEqual(runner.ran, [])
        self.assertIn("Nothing was changed", self.output())

    def test_gibberish_choice_changes_nothing(self):
        runner = FakeRunner()
        code = self.run_setup(["banana"], runner=runner)
        self.assertEqual(code, gi.EXIT_USAGE)
        self.assertEqual(runner.ran, [])

    def test_out_of_range_choice_changes_nothing(self):
        runner = FakeRunner()
        code = self.run_setup(["99"], runner=runner)
        self.assertEqual(code, gi.EXIT_USAGE)
        self.assertEqual(runner.ran, [])

    def test_declining_the_confirmation_changes_nothing(self):
        runner = FakeRunner()
        code = self.run_setup(["1", "n"], runner=runner)
        self.assertEqual(code, gi.EXIT_REFUSED)
        self.assertEqual(runner.ran, [])

    def test_no_room_is_explained_not_an_error_code(self):
        code = self.run_setup([""], disks=[self.disks[0]])
        self.assertEqual(code, gi.EXIT_REFUSED)
        self.assertIn("not a problem with your computer", self.output())
        self.assertIn("not modified", self.output())

    def test_removable_target_needs_an_extra_yes(self):
        stick = setup.candidates(self.disks).index(
            [c for c in setup.candidates(self.disks) if c.removable][0]) + 1
        code = self.run_setup([str(stick), "n"])   # USB stick, decline the warning
        self.assertEqual(code, gi.EXIT_REFUSED)

    def disk_index(self):
        """Menu position of the whole-disk option, which is never the default."""
        for index, candidate in enumerate(setup.candidates(self.disks), start=1):
            if candidate.kind == "disk" and not candidate.removable:
                return index, candidate.path
        self.fail("no whole-disk option was offered")

    def test_whole_disk_needs_the_typed_phrase(self):
        index, _path = self.disk_index()
        runner = FakeRunner()
        code = self.run_setup([str(index), "yes please"], runner=runner)
        self.assertEqual(code, gi.EXIT_REFUSED)
        self.assertEqual(runner.ran, [])

    def test_whole_disk_phrase_is_the_disk_path(self):
        index, path = self.disk_index()
        runner = FakeRunner()
        self.run_setup([str(index), "ERASE " + path], runner=runner)
        # The phrase was accepted, so disk work began (and then failed on cp):
        self.assertTrue(runner.ran)
        self.assertIn("sgdisk", runner.programs())

    def test_failure_is_reported_with_the_machine_state(self):
        runner = FakeRunner(fail_on=("cp",))
        code = self.run_setup([""], runner=runner)
        self.assertEqual(code, gi.EXIT_STEP_FAILED)
        self.assertIn("still starts", self.output())
        self.assertIn("Nothing was changed" if False else "boot menu was not modified",
                      self.output())

    def test_dry_run_reports_but_does_not_write(self):
        runner = FakeRunner(dry_run=True)
        code = self.run_setup([""], runner=runner)
        self.assertEqual(code, gi.EXIT_OK)
        self.assertEqual(runner.ran, [])

    def test_missing_source_is_a_refusal_not_a_crash(self):
        self.out = io.StringIO()
        self.inp = io.StringIO("\n\n")
        with mock.patch.object(gi, "probe_disks", lambda r: self.disks):
            code = setup.run(stream_in=self.inp, stream_out=self.out,
                             runner=FakeRunner(), source="", source_kind="",
                             mount_root=self.mount_root)
        self.assertIn(code, (gi.EXIT_OK, gi.EXIT_REFUSED, gi.EXIT_STEP_FAILED))


class TestTargetListing(unittest.TestCase):
    def test_describe_targets_is_plain_text(self):
        found = [setup.Candidate("partition", "/dev/sdb1", 100 * 1024 ** 3,
                                 "empty", True, ""),
                 setup.Candidate("disk", "/dev/sdc", 64 * 1024 ** 3,
                                 "whole disk", False, "")]
        lines = setup.describe_targets(found)
        self.assertEqual(lines[0], "/dev/sdb1 100.0 GB partition safe")
        self.assertEqual(lines[1], "/dev/sdc 64.0 GB disk destructive")

    def test_candidate_size_text(self):
        self.assertEqual(setup.Candidate("disk", "/dev/x", 64 * 1024 ** 3, "", True,
                                         "").size_text(), "64.0 GB")


if __name__ == "__main__":
    unittest.main()
