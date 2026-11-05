# Macro regression tests

The tests extract **both actual `PRINT_START` definitions from `README.md`**,
render them using Klipper's Jinja environment, and execute the rendered commands
using Klipper's real `GCodeDispatch` and `GCodeMove` classes. They do not copy the
macros or implement a second extrusion-state model.

The printer, toolhead, XYZ homing, temperatures, fans, display, LEDs and waits
are synthetic. Requested movement is recorded in memory; no printer is
connected, no commands reach hardware, and no physical print is validated.
Optional commented-out configuration is not enabled. Unknown commands fail the
fixture rather than being silently ignored.

## Run

Use Python 3.10+ and a disposable directory outside the repository. Download the
three unmodified Klipper modules from the pinned public revision below, then run
all tests. Downloads/installations are setup only; the tests themselves run
offline and do not start Klipper, an MCU connection, or any service.

```sh
scratch=$(mktemp -d "${TMPDIR:-/tmp}/print-start-tests.XXXXXX")
python3 -m venv "$scratch/venv"
"$scratch/venv/bin/python" -m pip install -r tests/requirements.txt
export KLIPPER_SOURCE="$scratch/klipper"
"$scratch/venv/bin/python" - <<'PY'
import os
from pathlib import Path
import urllib.request

revision = "0cee841e31d701595b3e509a0b502f6e320f52a8"
root = Path(os.environ["KLIPPER_SOURCE"])
for relative in ("klippy/gcode.py", "klippy/extras/gcode_move.py",
                 "klippy/extras/gcode_macro.py"):
    url = f"https://raw.githubusercontent.com/Klipper3d/klipper/{revision}/{relative}"
    data = urllib.request.urlopen(url, timeout=30).read()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
PY
PYTHONDONTWRITEBYTECODE=1 "$scratch/venv/bin/python" -m unittest discover -s tests -v
```

An existing Klipper source checkout can instead be selected with
`KLIPPER_SOURCE=/path/to/klipper`. The modules are imported from that exact path;
missing setup is an error, not a skip. `PRINT_START_README=/path/to/README.md`
optionally selects another revision of the documented macros for regression
comparison without changing test bytes.

## Contract and scope

- A primeline commands 20 mm of **positive relative extrusion**, honoring an
  existing `M221` flow override, for both `M82` and `M83` callers, including
  nonzero/G92-adjusted E coordinates and both heatsoak branches.
- The caller's extrusion-mode selection and logical E coordinate survive, so a
  subsequent absolute slicer extrusion is not turned into a retraction.
- Original XYZ primeline paths and speeds remain unchanged, with no return move
  across the primeline. As before, the macro leaves XYZ in absolute mode (it does
  not restore an entry `G91` mode). Origins, flow/speed overrides and the feed
  rate immediately before the prime are restored.
- Repeated calls and another macro's default saved state remain independent.
  Reserve the named `PRINT_START_PRIME` save slot for this macro.

Klipper's `G91` also makes E relative when `M82` is selected. Thus the original
V2/Trident variant already primed forward, but changed the caller's logical E;
the original v0 variant could also retract when its absolute E exceeded 20.
The tests distinguish these defects from unchanged relative-extrusion controls.

Relevant Klipper semantics:
[`GCodeMove`](https://github.com/Klipper3d/klipper/blob/0cee841e31d701595b3e509a0b502f6e320f52a8/klippy/extras/gcode_move.py)
and [`SAVE_GCODE_STATE` / `RESTORE_GCODE_STATE`](https://www.klipper3d.org/G-Codes.html#save_gcode_state).
There was no pre-existing test suite or build configuration in this repository.
