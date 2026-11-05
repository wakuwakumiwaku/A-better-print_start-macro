"""Execute the README macros with Klipper parsing/movement and fake hardware.

See tests/README.md for the pinned, offline-at-test-time Klipper dependency.
"""
import configparser
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


KLIPPER = Path(os.environ["KLIPPER_SOURCE"])


def load_klipper(name, relative):
    spec = importlib.util.spec_from_file_location(name, KLIPPER / relative)
    module = importlib.util.module_from_spec(spec)
    # dataclasses and tracebacks may need the module to be discoverable.
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gcode = load_klipper("print_start_klipper_gcode", "klippy/gcode.py")
gcode_move = load_klipper("print_start_klipper_move", "klippy/extras/gcode_move.py")
gcode_macro = load_klipper("print_start_klipper_macro", "klippy/extras/gcode_macro.py")


@dataclass
class Move:
    command: str
    before: list
    after: list
    speed: float


class FakeToolhead:
    """Record requested moves, without kinematics, heaters, motors, or IO."""
    def __init__(self, printer):
        self.printer = printer
        self.position = [0., 0., 0., 500.]
        self.moves = []
        self.command = ""

    def get_position(self):
        return list(self.position)

    def move(self, position, speed):
        self.moves.append(Move(self.command, self.get_position(),
                               list(position), speed))
        self.position = list(position)

    def home(self, command):
        # Synthetic XYZ homing only. Real Klipper's callback resets XYZ origins,
        # but does not reset E. No physical homing or motion is attempted.
        self.position[:3] = [0., 0., 0.]
        self.printer.send_event("homing:home_axes",
                               SimpleNamespace(get_axes=lambda: [0, 1, 2]))


class FakePrinter:
    config_error = RuntimeError
    command_error = gcode.CommandError

    def __init__(self):
        self.objects = {}
        self.handlers = {}

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return SimpleNamespace(mutex=lambda: None)

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def send_event(self, event, *args):
        for handler in self.handlers.get(event, []):
            handler(*args)

    def invoke_shutdown(self, message):
        raise AssertionError(message)

    def get_state_message(self):
        return ("synthetic printer", "ready")


class MacroRunner:
    # Explicit mocks: no unknown command is silently accepted. These commands
    # cannot alter extrusion state in this fixture. Optional commented macros
    # are deliberately not enabled.
    NON_MOTION = (
        "CLEAR_PAUSE", "STATUS_HOMING", "STATUS_HEATING", "STATUS_PRINTING",
        "SET_DISPLAY_TEXT", "M106", "M107", "M109", "M190",
        "TEMPERATURE_WAIT", "G4",
    )

    def __init__(self, script, size):
        self.printer = FakePrinter()
        self.dispatch = gcode.GCodeDispatch(self.printer)
        self.printer.objects["gcode"] = self.dispatch
        self.toolhead = FakeToolhead(self.printer)
        self.printer.objects["toolhead"] = self.toolhead
        config = SimpleNamespace(get_printer=lambda: self.printer)
        self.state = gcode_move.GCodeMove(config)
        self.dispatch.register_command("G28", self.toolhead.home)
        self.ignored = []
        for name in self.NON_MOTION:
            self.dispatch.register_command(name, self.ignore)
        self.dispatch.cmd_default = self.reject_unknown
        self.printer.send_event("klippy:ready")
        # Use Klipper's own Jinja environment (single-brace expressions).
        self.template = gcode_macro.PrinterGCodeMacro(config).env.from_string(script)
        self.context = {"printer": {"toolhead": {"axis_maximum": {
            "x": size, "y": size}}}}

    def ignore(self, command):
        self.ignored.append(command.get_commandline())

    def reject_unknown(self, command):
        if command.get_command():
            raise AssertionError("Unmocked command: " + command.get_commandline())

    def execute(self, script):
        for line in script.splitlines():
            self.toolhead.command = line.strip()
            self.dispatch.run_script_from_command(line)

    def run(self, **params):
        params = {"BED": "60", "EXTRUDER": "210", **params}
        self.execute(self.template.render(self.context, params=params))

    @property
    def prime_moves(self):
        return [move for move in self.toolhead.moves
                if re.search(r"\bE[-+\d.]", move.command)]


def read_macros():
    readme = Path(os.environ.get("PRINT_START_README",
                                Path(__file__).resolve().parents[1] / "README.md"))
    macros = []
    for block in re.findall(r"^```[^\n]*\n(.*?)^```", readme.read_text(), re.M | re.S):
        if "[gcode_macro PRINT_START]" not in block:
            continue
        config = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
        config.read_string(block)
        macros.append(config.get("gcode_macro PRINT_START", "gcode"))
    if len(macros) != 2:
        raise AssertionError(f"Expected both documented PRINT_START macros, got {len(macros)}")
    return (("V2/Trident", macros[0], 300), ("v0", macros[1], 120))


class PrintStartTests(unittest.TestCase):
    def runners(self):
        for name, script, size in read_macros():
            yield name, MacroRunner(script, size), size

    def prepare(self, runner, extrusion_mode="M82", e=137, xyz_mode="G90"):
        runner.execute(f"{extrusion_mode}\nG92 E{e}\n{xyz_mode}")

    def assert_prime(self, runner, amount=20):
        self.assertEqual(len(runner.prime_moves), 1)
        prime = runner.prime_moves[0]
        self.assertAlmostEqual(prime.after[3] - prime.before[3], amount,
                               msg="Primeline must extrude forward, independent of prior E")

    def test_absolute_extrusion_primes_forward_from_nonzero_e(self):
        for name, script, size in read_macros():
            for bed in ("60", "110"):
                for e in (0, 5, 20, 137):
                    for xyz_mode in ("G90", "G91"):
                        with self.subTest(variant=name, bed=bed, e=e, xyz_mode=xyz_mode):
                            runner = MacroRunner(script, size)
                            self.prepare(runner, e=e, xyz_mode=xyz_mode)
                            runner.run(BED=bed)
                            self.assert_prime(runner)

    def test_relative_extrusion_control_primes_forward(self):
        for name, script, size in read_macros():
            for bed in ("60", "110"):
                for xyz_mode in ("G90", "G91"):
                    with self.subTest(variant=name, bed=bed, xyz_mode=xyz_mode):
                        runner = MacroRunner(script, size)
                        self.prepare(runner, "M83", xyz_mode=xyz_mode)
                        runner.run(BED=bed)
                        self.assert_prime(runner)

    def test_absolute_slicer_move_continues_from_callers_e(self):
        for name, runner, _ in self.runners():
            with self.subTest(variant=name):
                self.prepare(runner)
                runner.run()
                self.assertTrue(runner.state.get_status()["absolute_extrude"])
                self.assertAlmostEqual(runner.state.get_status()["gcode_position"].e, 137)
                before = runner.toolhead.position[3]
                runner.execute("G1 E138 F600")
                self.assertAlmostEqual(runner.toolhead.position[3] - before, 1)

    def test_relative_mode_and_logical_e_are_restored(self):
        for name, runner, _ in self.runners():
            with self.subTest(variant=name):
                self.prepare(runner, "M83")
                runner.run()
                self.assertFalse(runner.state.get_status()["absolute_extrude"])
                self.assertAlmostEqual(runner.state.get_status()["gcode_position"].e, 137)
                before = runner.toolhead.position[3]
                runner.execute("G1 E1 F600")
                self.assertAlmostEqual(runner.toolhead.position[3] - before, 1)

    def test_original_prime_path_and_no_restore_travel(self):
        for name, runner, size in self.runners():
            with self.subTest(variant=name):
                self.prepare(runner, "M83")
                runner.run()
                center = [size / 2, size / 2, 15]
                if name == "V2/Trident":
                    path = [center, center, [size / 2 - 50, 4, 15],
                            [size / 2 - 50, 4, 0.4], [size / 2 + 50, 4, 0.4]]
                else:
                    path = [center, [5, 4, 0.4], [115, 4, 0.4]]
                self.assertEqual([move.after[:3] for move in runner.toolhead.moves], path)
                self.assertEqual(runner.toolhead.get_position()[:3], path[-1])
                self.assertAlmostEqual(runner.prime_moves[0].speed, 1000 / 60)

    def test_leaves_absolute_xyz_and_preserves_origin_offsets(self):
        for name, runner, size in self.runners():
            with self.subTest(variant=name):
                runner.execute("G92 X7 Y9 Z2\nSET_GCODE_OFFSET Z=0.06")
                self.prepare(runner, xyz_mode="G91")
                runner.run()
                status = runner.state.get_status()
                self.assertTrue(status["absolute_coordinates"])
                self.assertEqual(list(status["homing_origin"]), [0., 0., 0.06, 0.])
                self.assertEqual(runner.state.base_position[:3], [0., 0., 0.06])
                endpoint_x = size / 2 + 50 if name == "V2/Trident" else 115
                self.assertAlmostEqual(status["gcode_position"].x, endpoint_x)
                self.assertAlmostEqual(status["gcode_position"].z, 0.4)

    def test_flow_and_speed_overrides_and_preprime_feed_are_preserved(self):
        for name, script, size in read_macros():
            for mode in ("M82", "M83"):
                with self.subTest(variant=name, mode=mode):
                    runner = MacroRunner(script, size)
                    runner.execute("M220 S80\nM221 S125")
                    self.prepare(runner, mode)
                    runner.run()
                    self.assert_prime(runner, amount=25)
                    status = runner.state.get_status()
                    self.assertAlmostEqual(status["speed_factor"], 0.8)
                    self.assertAlmostEqual(status["extrude_factor"], 1.25)
                    self.assertAlmostEqual(status["speed"], 10000)
                    self.assertAlmostEqual(status["gcode_position"].e, 137)
                    self.assertAlmostEqual(runner.prime_moves[0].speed, 1000 / 60 * 0.8)

    def test_does_not_overwrite_callers_default_saved_state(self):
        for name, runner, _ in self.runners():
            with self.subTest(variant=name):
                self.prepare(runner)
                runner.execute("SAVE_GCODE_STATE")
                saved = runner.state.saved_states["default"]
                runner.run()
                self.assertEqual(runner.state.saved_states["default"], saved)

    def test_repeated_calls_do_not_accumulate_logical_e(self):
        for name, runner, _ in self.runners():
            with self.subTest(variant=name):
                self.prepare(runner)
                runner.run()
                runner.execute("G1 E138 F600")
                runner.run()
                self.assertAlmostEqual(runner.state.get_status()["gcode_position"].e, 138)
                self.assertAlmostEqual(runner.toolhead.position[3], 541)

    def test_renders_heatsoak_and_minimum_chamber_inputs(self):
        for name, script, size in read_macros():
            for params, wait in (({"BED": "110"}, 45),
                                 ({"BED": "110", "CHAMBER": "50", "CHAMBER_MINIMAL": "35"}, 35),
                                 ({"BED": "110", "CHAMBER": "0", "CHAMBER_MINIMAL": "35"}, 35),
                                 ({"BED": "60"}, None)):
                with self.subTest(variant=name, params=params):
                    runner = MacroRunner(script, size)
                    self.prepare(runner, "M83")
                    runner.run(**params)
                    waits = [line for line in runner.ignored
                             if line.startswith("TEMPERATURE_WAIT")]
                    if wait is None:
                        self.assertFalse(waits)
                        self.assertTrue(any(line.startswith("G4 P300000")
                                            for line in runner.ignored))
                    else:
                        self.assertEqual(len(waits), 1)
                        self.assertIn(f"MINIMUM={wait}", waits[0])
                    self.assert_prime(runner)


if __name__ == "__main__":
    unittest.main()
