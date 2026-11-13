"""Printable fixtures for pen plotting on a Bambu Lab P1P (or any 256 mm bed printer).

  postcard_frame.stl  - print it at the plate centre and LEAVE IT ON THE PLATE. The postcard drops
                        into the pocket, which is exactly where the G-code expects it (page centred).
                        Small corner bridges hold the card down; thumb notches let you pull it out.
                        The arrow notch marks the back edge = top of the card.
  pen_holder.stl      - gravity-fed pen sleeve on a plate you stick to the toolhead front cover with
                        VHB / double-sided tape. The pen slides freely: the head over-travels a bit
                        on pen-down and the pen floats up, so it presses with its own weight
                        (+ an optional rubber band between the hooks for more pressure).
  pen_collar.stl      - C-clip that grips the pen and rests on top of the sleeve so the pen can't
                        fall through. Slide it up/down the pen to set how far the tip hangs.

python -m handwriter.fixtures --pen-diameter 8.5 --out fixtures/ --slice
  --slice uses the locally installed Bambu Studio to produce printer-ready .gcode.3mf files
  (P1P, 0.4 nozzle, 0.20 mm Standard, Bambu PLA Basic).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from manifold3d import CrossSection, Manifold

POSTCARD = (152.4, 101.6)


def _save(m: Manifold, path: Path):
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
    f = np.asarray(mesh.tri_verts).astype(np.int64)
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tri
    with open(path, "wb") as fh:
        fh.write(b"handwriter fixture".ljust(80, b" "))
        fh.write(np.uint32(len(f)).tobytes())
        fh.write(rec.tobytes())
    return path


def _box(x, y, z, at=(0, 0, 0)):
    return Manifold.cube([x, y, z]).translate(list(at))


def _cyl(h, r, at=(0, 0, 0), seg=96):
    return Manifold.cylinder(h, r, circular_segments=seg).translate(list(at))


def _prism(pts, h, z0=0.0):
    return Manifold.extrude(CrossSection([pts]), h).translate([0, 0, z0])


def postcard_frame(card=POSTCARD, clearance=0.5, border=10.0, height=1.2, card_thickness=0.45,
                   corner=7.0) -> Manifold:
    cw, ch = card[0] + 2 * clearance, card[1] + 2 * clearance
    W, H = cw + 2 * border, ch + 2 * border
    frame = _box(W, H, height) - _box(cw, ch, height + 2, (border, border, -1))
    # corner bridges: card corners tuck under these (gap = card_thickness + 0.15)
    z0 = min(height - 0.4, card_thickness + 0.15)
    x0, y0, x1, y1 = border, border, border + cw, border + ch
    for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        pts = [(cx, cy), (cx + sx * corner, cy), (cx, cy + sy * corner)]
        if sx * sy < 0:  # keep counter-clockwise winding
            pts = pts[::-1]
        frame += _prism(pts, height - z0, z0)
    # thumb notches on the front and back edges
    for y in (border, border + ch):
        frame -= _cyl(height + 2, 9, (W / 2 - 25, y, -1))
    # arrow notch on the back border: "top of the card goes this way"
    ax, ay = W / 2 + 25, border + ch + 2.5
    frame -= _prism([(ax - 4, ay), (ax + 4, ay), (ax, ay + 5.5)], height + 2, -1)
    # centre the frame on the origin so a slicer's "centre on plate" == page centre
    return frame.translate([-W / 2, -H / 2, 0])


def pen_holder(pen_d=8.5, clearance=0.6, wall=2.4, sleeve_len=32.0, plate_w=36.0, plate_h=40.0,
               plate_t=3.0) -> Manifold:
    """Printed standing up (as used). Plate back face (y=0) sticks to the toolhead front cover."""
    r_in = pen_d / 2 + clearance / 2
    r_out = r_in + wall
    cy = -(plate_t + r_out - 0.8)  # sleeve axis in front of the plate (fused into it)
    plate = _box(plate_w, plate_t, plate_h, (-plate_w / 2, -plate_t, 0))
    sleeve = _cyl(sleeve_len, r_out, (0, cy, 0))
    web = _box(2 * r_out * 0.8, abs(cy), sleeve_len, (-r_out * 0.8, cy, 0))  # solid bridge to plate
    body = plate + sleeve + web
    body -= _cyl(sleeve_len + 2, r_in, (0, cy, -1))
    # rubber-band hooks low on the plate sides
    for sx in (-1, 1):  # L-shaped hooks, rooted 1.5 mm into the plate
        x0 = -plate_w / 2 if sx < 0 else plate_w / 2 - 4
        body += _box(4, 7.5, 3, (x0, -plate_t - 6, 2)) + _box(4, 2, 6, (x0, -plate_t - 6, 2))
    # zip-tie / strap slots through the plate, either side of the sleeve (backup to tape)
    for sx in (-1, 1):
        for z in (12, plate_h - 10):
            body -= _box(3, plate_t + 2, 5, (sx * (plate_w / 2 - 4.5) - 1.5, -plate_t - 1, z - 2.5))
    return body


def pen_collar(pen_d=8.5, grip=0.3, wall=3.0, height=6.0) -> Manifold:
    r_in = pen_d / 2 - grip / 2
    ring = _cyl(height, r_in + wall) - _cyl(height + 2, r_in, (0, 0, -1))
    ring -= _box(1.6, r_in + wall + 1, height + 2, (-0.8, 0, -1))  # C-slit so it clamps the pen
    for sx in (-1, 1):  # rubber band ears
        ring += _box(4, 3, height, (sx * (r_in + wall + 1.5) - 2, -1.5, 0))
    return ring


BAMBU = Path("/Applications/BambuStudio.app/Contents")
PRESETS = {"machine": "Bambu Lab P1P 0.4 nozzle", "process": "0.20mm Standard @BBL P1P",
           "filament": "Bambu PLA Basic @BBL X1C"}


def _flatten(kind: str, name: str) -> dict:
    """Resolve a Bambu Studio system preset's `inherits` chain into one flat settings dict."""
    d = json.loads((BAMBU / "Resources/profiles/BBL" / kind / f"{name}.json").read_text())
    if "inherits" in d:
        base = _flatten(kind, d.pop("inherits"))
        base.update(d)
        d = base
    return d


def bambu_slice(models: list[Path], out: Path, arrange: bool, presets: dict = PRESETS) -> Path:
    """Slice STL(s) with the Bambu Studio CLI into a plate-sliced .gcode.3mf."""
    exe = BAMBU / "MacOS/BambuStudio"
    if not exe.exists():
        raise FileNotFoundError("Bambu Studio not found in /Applications - slice the STLs in Bambu Studio instead")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        files = {}
        for kind, name in presets.items():
            files[kind] = td / f"{kind}.json"
            files[kind].write_text(json.dumps(_flatten(kind, name)))
        cmd = [str(exe), "--slice", "0", "--arrange", "1" if arrange else "0", "--orient", "0",
               "--load-settings", f"{files['machine']};{files['process']}", "--load-filaments", str(files["filament"]),
               "--export-3mf", out.name, "--outputdir", str(td), *map(str, models)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        res = td / out.name
        if not res.exists():
            raise RuntimeError("Bambu Studio slice failed:\n" + (r.stdout + r.stderr)[-2000:])
        shutil.copy(res, out)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate pen-plotting fixtures (STL).")
    p.add_argument("--out", default="fixtures")
    p.add_argument("--pen-diameter", type=float, default=8.5, help="pen barrel diameter in mm (across flats is fine)")
    p.add_argument("--card", default="152.4x101.6", help="card size WxH mm [postcard 6x4in]")
    p.add_argument("--card-thickness", type=float, default=0.45)
    p.add_argument("--slice", action="store_true", help="also slice for a P1P with the installed Bambu Studio")
    a = p.parse_args(argv)
    w, h = (float(v) for v in a.card.lower().split("x"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, m in (
        ("postcard_frame", postcard_frame((w, h), card_thickness=a.card_thickness)),
        ("pen_holder", pen_holder(a.pen_diameter)),
        ("pen_collar", pen_collar(a.pen_diameter)),
    ):
        bb = m.bounding_box()
        print(_save(m, out / f"{name}.stl"), f"{bb[3]-bb[0]:.1f} x {bb[4]-bb[1]:.1f} x {bb[5]-bb[2]:.1f} mm",
              "watertight" if m.status().name == "NoError" else m.status())

    if a.slice:
        with tempfile.TemporaryDirectory() as td:
            # the frame must land exactly on the plate centre -> place it at (128,128), no auto-arrange
            framed = _save(postcard_frame((w, h), card_thickness=a.card_thickness).translate([128, 128, 0]),
                           Path(td) / "postcard_frame.stl")
            print(bambu_slice([framed], out / "postcard_frame.p1p.gcode.3mf", arrange=False))
        print(bambu_slice([out / "pen_holder.stl", out / "pen_collar.stl"], out / "pen_holder_and_collar.p1p.gcode.3mf",
                          arrange=True))


if __name__ == "__main__":
    main()
