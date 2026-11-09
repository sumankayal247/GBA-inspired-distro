"""
Contract tests for the keyboard hand-off to mGBA (Rules I3, H5, T-INPUT-4).

The shell draws its own controls from `keybindings.json` (Z = A, X = B).  mGBA
does not read that file: it reads the section `gba.input.SDLK` from
`$XDG_CONFIG_HOME/mgba/config.ini` (see `_makeSectionName()`/_loadKey() in
mGBA's src/core/input.c and the keys mInputBindKey() registers in
src/platform/sdl/sdl-events.c).  Anything written outside that exact section
is a silent no-op and the game falls back to mGBA's built-in X = A / Z = B
defaults - the "controls swap as soon as a game starts" bug.

These tests therefore assert the emulator-visible result, not merely that a
file was created.
"""

import configparser
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import common
import config
import gba_adapter_mgba
from gba_adapter_mgba import (
    ensure_mgba_config,
    resolve_mgba_config_path,
    verify_mgba_bindings,
)

# mGBA's compiled-in SDL defaults (src/platform/sdl/sdl-events.c,
# mSDLInitBindingsGBA): X is A and Z is B.  Every symptom below is measured
# against these, because they are what a game uses when the config is ignored.
MGBA_DEFAULT_A_KEY = 120   # SDLK_x
MGBA_DEFAULT_B_KEY = 122   # SDLK_z

Z_KEY = 122                # SDLK_z -> must act as A
X_KEY = 120                # SDLK_x -> must act as B


def mgba_lookup(config_file, keycode):
    """Resolves a keycode to a GBA button exactly as mGBA's SDL port does.

    Mirrors mInputMapLoad(mInputMapKey()): start from the compiled-in
    defaults, then let [gba.input.SDLK] override them.
    """
    mapping = {"keyA": MGBA_DEFAULT_A_KEY, "keyB": MGBA_DEFAULT_B_KEY,
               "keyL": 97, "keyR": 115, "keyStart": 13, "keySelect": 8,
               "keyUp": 1073741906, "keyDown": 1073741905,
               "keyLeft": 1073741904, "keyRight": 1073741903}
    cp = configparser.ConfigParser()
    cp.optionxform = str
    if os.path.isfile(config_file):
        cp.read(config_file)
    # mGBA only consults the frontend's platform section.
    section = common.MGBA_INPUT_SECTION
    if section in cp:
        for key, value in cp[section].items():
            if key in mapping:
                try:
                    mapping[key] = int(value)
                except ValueError:
                    pass
    for button, bound in mapping.items():
        if bound == keycode:
            return button
    return None


class TestMountSectionIsTheOneMgbaReads(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = os.path.join(self.dir, "home")
        self.config_dir = os.path.join(self.dir, "config")
        os.makedirs(self.home)
        self.env = mock.patch.dict(
            os.environ,
            {"HOME": self.home, "XDG_CONFIG_HOME": self.config_dir,
             "GBA_WINDOWED": "0"})
        self.env.start()
        patcher = mock.patch.object(common, "get_data_dir", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def live_file(self):
        return os.path.join(self.config_dir, "mgba", "config.ini")

    def test_written_profile_resolves_z_to_a_and_x_to_b(self):
        """T-INPUT-4: in-game A/B must match the shell's Z=A, X=B."""
        common.save_keybindings(dict(common.DEFAULT_KEYBINDINGS))
        ensure_mgba_config(self.config_dir)

        live = self.live_file()
        self.assertTrue(os.path.isfile(live))
        self.assertEqual(mgba_lookup(live, Z_KEY), "keyA")
        self.assertEqual(mgba_lookup(live, X_KEY), "keyB")
        self.assertEqual(verify_mgba_bindings(live, common.DEFAULT_KEYBINDINGS), [])

    def test_section_name_is_the_documented_mgba_one(self):
        ensure_mgba_config(self.config_dir)
        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read(self.live_file())
        self.assertIn("gba.input.SDLK", cp)
        self.assertEqual(cp["gba.input.SDLK"]["keyA"], "122")
        self.assertEqual(cp["gba.input.SDLK"]["keyB"], "120")

    def test_verifier_rejects_the_section_older_releases_wrote(self):
        """The exact pre-fix file: mGBA ignores it, so A/B stay swapped."""
        os.makedirs(os.path.dirname(self.live_file()), exist_ok=True)
        with open(self.live_file(), "w", encoding="utf-8") as f:
            f.write("[ports.sdl]\nfullscreen=1\n\n[input.SDLK]\n"
                    "keyA=122\nkeyB=120\n")
        self.assertEqual(mgba_lookup(self.live_file(), Z_KEY), "keyB")
        self.assertEqual(mgba_lookup(self.live_file(), X_KEY), "keyA")

        problems = verify_mgba_bindings(self.live_file(), common.DEFAULT_KEYBINDINGS)
        self.assertTrue(problems)
        self.assertIn("gba.input.SDLK", problems[0])

    def test_existing_swapped_profile_is_repaired(self):
        """A file left behind by an older release is corrected, not trusted."""
        os.makedirs(os.path.dirname(self.live_file()), exist_ok=True)
        with open(self.live_file(), "w", encoding="utf-8") as f:
            f.write("[gba.input.SDLK]\nkeyA=120\nkeyB=122\n")

        ensure_mgba_config(self.config_dir)

        self.assertEqual(mgba_lookup(self.live_file(), Z_KEY), "keyA")
        self.assertEqual(mgba_lookup(self.live_file(), X_KEY), "keyB")
        self.assertEqual(verify_mgba_bindings(self.live_file(),
                                              common.DEFAULT_KEYBINDINGS), [])

    def test_unrelated_mgba_settings_survive_a_launch(self):
        """Rewriting the profile must not wipe the rest of the emulator config."""
        os.makedirs(os.path.dirname(self.live_file()), exist_ok=True)
        with open(self.live_file(), "w", encoding="utf-8") as f:
            f.write("[ports.qt]\nbiospath=/roms/gba_bios.bin\n\n"
                    "[ports.sdl]\naudioBufferSize=2048\n")

        ensure_mgba_config(self.config_dir)

        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read(self.live_file())
        self.assertEqual(cp["ports.qt"]["biospath"], "/roms/gba_bios.bin")
        self.assertEqual(cp["ports.sdl"]["audioBufferSize"], "2048")
        self.assertEqual(cp["ports.sdl"]["fullscreen"], "1")
        self.assertEqual(cp["gba.input.SDLK"]["keyA"], "122")

    def test_remapped_button_reaches_the_emulator(self):
        """A custom binding in the shell must be the one the game receives."""
        custom = dict(common.DEFAULT_KEYBINDINGS, keyA=113, keyB=114, version=3)
        common.save_keybindings(custom)
        ensure_mgba_config(self.config_dir)

        self.assertEqual(mgba_lookup(self.live_file(), 113), "keyA")
        self.assertEqual(mgba_lookup(self.live_file(), 114), "keyB")

    def test_launch_log_states_the_profile_was_verified(self):
        ensure_mgba_config(self.config_dir)
        logger = gba_adapter_mgba.logger
        with self.assertLogs(logger, level="INFO") as captured:
            ensure_mgba_config(self.config_dir)
        joined = "\n".join(captured.output)
        self.assertIn("keyboard profile verified", joined)
        self.assertIn("A=122", joined)
        self.assertIn("B=120", joined)

    def test_template_uses_the_same_section(self):
        """The documented template must not drift back to the dead section."""
        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read_string(gba_adapter_mgba.MIGBA_CONFIG_TEMPLATE)
        self.assertIn(common.MGBA_INPUT_SECTION, cp)
        self.assertNotIn("input.SDLK", cp)          # only the live name, never the bare one
        self.assertEqual(cp[common.MGBA_INPUT_SECTION]["keyA"], "122")
        self.assertEqual(cp[common.MGBA_INPUT_SECTION]["keyB"], "120")

    def test_qt_build_is_reported_instead_of_silently_ignored(self):
        problem = gba_adapter_mgba.config_redirection_warning(
            "/usr/games/mgba-qt", "/data/config/mgba/config.ini")
        self.assertIn("Qt", problem)
        self.assertEqual(
            gba_adapter_mgba.config_redirection_warning("/usr/games/mgba-sdl"), "")

    def test_portable_install_is_reported(self):
        with tempfile.TemporaryDirectory() as bindir:
            binary = os.path.join(bindir, "mgba")
            open(binary, "w").close()
            open(os.path.join(bindir, "portable.ini"), "w").close()
            problem = gba_adapter_mgba.config_redirection_warning(
                binary, "/data/config/mgba/config.ini")
        self.assertIn("portable.ini", problem)


class TestConfigPathResolution(unittest.TestCase):
    """mGBA's own path rules, so we never verify a file mGBA will not open."""

    def test_absolute_xdg_config_home_wins(self):
        self.assertEqual(
            resolve_mgba_config_path({"XDG_CONFIG_HOME": "/data/config",
                                      "HOME": "/home/player"}),
            "/data/config/mgba/config.ini")

    def test_home_is_the_fallback(self):
        self.assertEqual(
            resolve_mgba_config_path({"XDG_CONFIG_HOME": "", "HOME": "/home/player"}),
            "/home/player/.config/mgba/config.ini")
        self.assertEqual(
            resolve_mgba_config_path({"XDG_CONFIG_HOME": "relative", "HOME": "/home/player"}),
            "/home/player/.config/mgba/config.ini")

    def test_ensure_points_xdg_at_the_directory_it_manages(self):
        with tempfile.TemporaryDirectory() as config_dir:
            with mock.patch.object(common, "get_data_dir",
                                   lambda: tempfile.mkdtemp()):
                ensure_mgba_config(config_dir)
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], config_dir)
            self.assertEqual(resolve_mgba_config_path(),
                             os.path.join(config_dir, "mgba", "config.ini"))

            # The emulator is spawned with this environment: if the pointer
            # were dropped here, mGBA would read a different config.ini than
            # the one just verified.
            child_env = config.prepare_child_environment()
            self.assertEqual(child_env["XDG_CONFIG_HOME"], config_dir)
            self.assertEqual(resolve_mgba_config_path(child_env),
                             os.path.join(config_dir, "mgba", "config.ini"))


class TestSharedProfileLoading(unittest.TestCase):
    """The shell reads the same section mGBA does."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.object(common, "get_data_dir", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ini = os.path.join(self.home, ".config", "mgba", "config.ini")
        os.makedirs(os.path.dirname(self.ini))

    def write(self, text):
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(text)

    def test_live_section_is_read(self):
        self.write("[gba.input.SDLK]\nkeyA=122\nkeyB=120\nkeyR=114\n")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            loaded = common.load_keybindings()
        self.assertEqual((loaded["keyA"], loaded["keyB"], loaded["keyR"]),
                         (122, 120, 114))

    def test_live_section_wins_over_a_stale_legacy_section(self):
        self.write("[input.SDLK]\nkeyA=120\nkeyB=122\n"
                   "[gba.input.SDLK]\nkeyA=122\nkeyB=120\n")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            loaded = common.load_keybindings()
        self.assertEqual((loaded["keyA"], loaded["keyB"]), (122, 120))

    def test_legacy_section_still_migrates_when_it_is_all_there_is(self):
        self.write("[input.SDLK]\nkeyA=120\nkeyB=122\n")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            loaded = common.load_keybindings()
        self.assertEqual((loaded["keyA"], loaded["keyB"]), (122, 120))

    def test_saving_writes_the_live_section_and_syncs_a_stale_one(self):
        self.write("[input.SDLK]\nkeyA=120\nkeyB=122\n")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            common.save_keybindings(dict(common.DEFAULT_KEYBINDINGS))
            loaded = common.load_keybindings()
        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read(self.ini)
        self.assertEqual(cp["gba.input.SDLK"]["keyA"], "122")
        self.assertEqual(cp["input.SDLK"]["keyA"], "122")   # stale copy repaired
        self.assertEqual((loaded["keyA"], loaded["keyB"]), (122, 120))


if __name__ == "__main__":
    unittest.main()
