"""Geometry-lite: a conservative distance-checking subset (v0.4).

This is NOT KiCad's DRC. It checks three things from the copper
objects project.py extracts (pads, tracks, arcs, vias), using exact
shape mathematics where the file gives exact shapes:

  * copper clearance — the minimum distance between copper objects
    on the same layer that belong to DIFFERENT nets, against the
    board's own stated clearance (the .kicad_pro design rules /
    netclass clearances) when stated, else the published fab
    minimum trace spacing of 0.10 mm (JLCPCB + PCBWay standard
    process, 1–2 layer 1 oz; docs/COVERAGE.md sources);
  * copper-to-edge — the minimum distance from any copper object
    to the Edge.Cuts outline, against the board's stated
    min_copper_edge_clearance when stated, else 0.20 mm (the
    cross-fab safe minimum from the same research);
  * outline closure — whether the root-level Edge.Cuts items chain
    into closed loops.

Conservatism rules (a false violation on a legal board is the worst
outcome this module can produce):

  * Shapes are exact: circles, rectangles at their real rotation,
    ovals and arcs (arcs are subdivided into chords whose sagitta
    is bounded; a pair is flagged only when even the largest
    possible true distance is below the threshold). Roundrects are
    computed as their exact core rectangle plus corner radius.
  * Custom and trapezoid pads are not modelled (project.py counts
    them) — no invented outlines.
  * Objects whose net is unknown (no net stated, or net 0) never
    take part in clearance pairs; they are counted in Coverage.
  * Zone copper (v0.6): a zone's STORED filled polygons take part
    in clearance and copper-to-edge in a separate budgeted pass
    (_zone_clearance_pass / _zone_edge_pass below), measured with
    the same pair thresholds as the other copper on the layer.
    KiCad's file format stores each filled polygon as a single
    ring with no hole sub-structure, so a ring is modelled as its
    edge segments plus a containment test (a shape whose core
    lies inside the ring is at distance 0 from the fill — the
    stale-fill case, where a pad was moved after the fill was
    generated). A zone with no readable filled polygon (absent /
    stale / empty fill) abstains INDIVIDUALLY and is counted in
    Coverage (zones_abstained), never silently dropped; zone
    budgets exceeded abstain the zone pass the same way the main
    budgets abstain this layer, with a GEOMETRY_SKIPPED finding.
    Zone-zone pairs use the smaller polygon's edges against the
    larger polygon's grid, and same-net pairs are skipped before
    any enumeration, so a board-filling GND pour does not make
    the pass quadratic.
  * Hard budgets (objects, pair computations): exceeding one
    abstains the whole geometry layer with a CAUTION, never a
    partial verdict presented as complete.

Distances are computed on (core, radius) shape pairs — a core is a
point, a segment, or an oriented rectangle, and the true shape is
the core inflated by its radius (a disk for circles/vias, a capsule
for tracks/ovals, a rounded rectangle for roundrect pads). The
identity dist(A⊕rA, B⊕rB) = max(0, dist(A, B) − rA − rB) makes
every pair exact for these shapes.

Pad positions use KiCad's rotation convention — rotating a local
point by angle t maps (x, y) to (x·cos t + y·sin t, −x·sin t +
y·cos t) in file coordinates — verified empirically: on the three
open-source corpus boards, pad centres computed this way coincide
with same-net track endpoints, while the textbook Y-up convention
matches none.
"""

import fnmatch
import math
from dataclasses import dataclass, field

from .finding import BLOCK, CAUTION, Finding

# Published standard-process minimums (mm) — docs/COVERAGE.md.
FAB_CLEARANCE_MIN = 0.10     # min trace spacing, both fabs, 1–2L 1 oz
FAB_EDGE_CLEARANCE_MIN = 0.20  # copper-to-routed-edge, cross-fab safe

MAX_OBJECTS = 40000          # copper objects after arc subdivision
MAX_PAIR_CHECKS = 2000000    # exact distance computations per board
MAX_CELL_OBJECTS = 8192      # per grid cell — degenerate-distribution
                             # guard (a real 1 mm cell never holds this
                             # many copper objects; a crafted one would
                             # make candidate enumeration quadratic)
MAX_OUTLINE_PIECES = 100000  # subdivided outline centrelines
ARC_SAGITTA = 0.005          # mm; arc/chord approximation bound
# Measurement tolerance: file coordinates are decimal text rounded
# from the CAD database, and routed boards sit exactly ON their
# design-rule clearance all over — manufactured corpus boards show
# dense clusters of pairs 0.0005–0.001 mm under the stated rule
# (rounding noise, two orders below any fab's process tolerance).
# A pair is flagged only when it is below the threshold by more
# than this tolerance, so geometry-lite reports real encroachments,
# not rounding dust.
MEASURE_TOLERANCE = 0.002    # mm
MAX_ARC_PIECES = 1024
CHAIN_TOLERANCE = 0.01       # mm; outline endpoint matching
MAX_EDGE_ELEMS = 5000
# Grid-insertion guards (v0.4 review RT-01): an object whose
# expanded bbox spans more grid cells than this is not inserted
# cell-by-cell at all — it joins a per-layer "wide" list that is
# tested against everything. One 2,000,000 mm segment otherwise
# inserted itself into millions of cells (19 s / 850 MB for a
# two-object board).
MAX_OBJ_CELLS = 2048         # per object / per outline piece
MAX_GRID_INSERTIONS = 8000000
MAX_WIDE_OBJECTS = 256       # wide objects per layer before abstain
# Candidate pairs EXAMINED (before any net/bbox filtering) across
# a board: the pair-measurement budget alone cannot bound the
# enumeration itself — a few thousand same-net tracks packed into
# one cell generate millions of candidates and zero measurements
# (audit F1). Past this, the check abstains honestly.
MAX_CANDIDATE_PAIRS = 5000000
OUTLINE_NEAR_MISS = 0.1      # mm; fragments of a shattered outline
# Containment judgement (an open chain next to closed loops is a
# routed slot only when it lies fully inside a loop's interior):
# budgets on the geometry built and the point/edge tests spent, so
# a crafted outline abstains honestly instead of hanging.
MAX_CONTAINMENT_POINTS = 200000   # subdivided points built
MAX_CONTAINMENT_OPS = 2000000     # point-to-loop-edge tests
# Zone pass budgets (v0.6 WS3): stored zone fills are the largest
# geometry in real files (a board-filling pour's ring can carry
# tens of thousands of points), so the zone pass has its own
# budgets, separate from the object budgets above — exceeding one
# abstains the zone pass only, counted in Coverage.
MAX_ZONE_EDGES = 200000           # filled-polygon edges per board
MAX_ZONE_CANDIDATE_CHECKS = 5000000   # candidate edges examined
MAX_ZONE_PAIR_CHECKS = 2000000    # exact distance computations
MAX_ZONE_CONTAINMENT_OPS = 2000000    # point-in-fill edge tests
ZONE_Y_BUCKET = 1.0               # mm; point-in-fill ray-cast index
# Zone-involving clearance pairs are judged with a wider
# measurement tolerance than pad/track pairs: a stored fill's
# boundary is a polygonal (chord) approximation of the offset
# curve the filler generated, on BOTH sides of a zone pair, so
# chord-to-chord distance undershoots the true clearance.
# Measured against kicad-cli 8.0.9 DRC on esp-rust-board: the
# two zone pairs KiCad flags measure 0.1231 / 0.1251 mm in its
# true-geometry engine and 0.1219 mm here, and four further
# pairs at the same measured 0.1219 mm are at or above the
# 0.127 mm threshold in true geometry (undershoot up to
# ~0.005 mm). 0.006 mm covers the observed undershoot; zone
# encroachments smaller than that are a documented miss, the
# same trade the object pass makes at MEASURE_TOLERANCE.
ZONE_MEASURE_TOLERANCE = 0.006    # mm; zone-involving pairs only


# ---------------------------------------------------------------------------
# Shape cores and exact distances
# ---------------------------------------------------------------------------
# A core is a tuple: ("pt", x, y) | ("seg", x1, y1, x2, y2) |
# ("obb", cx, cy, ux, uy, vx, vy, hw, hh) — centre, two unit axes,
# half extents. A shape is (core, radius).


def _d_pt_pt(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def _d_pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_intersect(ax, ay, bx, by, cx, cy, dx, dy):
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = orient(cx, cy, dx, dy, ax, ay)
    d2 = orient(cx, cy, dx, dy, bx, by)
    d3 = orient(ax, ay, bx, by, cx, cy)
    d4 = orient(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)
            and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0)


def _d_seg_seg(a, b):
    _, ax, ay, bx, by = a
    _, cx, cy, dx, dy = b
    if _seg_intersect(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0
    return min(
        _d_pt_seg(ax, ay, cx, cy, dx, dy),
        _d_pt_seg(bx, by, cx, cy, dx, dy),
        _d_pt_seg(cx, cy, ax, ay, bx, by),
        _d_pt_seg(dx, dy, ax, ay, bx, by))


def _pt_in_obb(px, py, obb):
    _, cx, cy, ux, uy, vx, vy, hw, hh = obb
    dx, dy = px - cx, py - cy
    return abs(dx * ux + dy * uy) <= hw and abs(dx * vx + dy * vy) <= hh


def _d_pt_obb(px, py, obb):
    _, cx, cy, ux, uy, vx, vy, hw, hh = obb
    dx, dy = px - cx, py - cy
    lx, ly = dx * ux + dy * uy, dx * vx + dy * vy
    qx = max(abs(lx) - hw, 0.0)
    qy = max(abs(ly) - hh, 0.0)
    return math.hypot(qx, qy)


def _obb_corners(obb):
    _, cx, cy, ux, uy, vx, vy, hw, hh = obb
    return [(cx + su * hw * ux + sv * hh * vx,
             cy + su * hw * uy + sv * hh * vy)
            for su in (1, -1) for sv in (1, -1)]


def _obb_edges(obb):
    _, cx, cy, ux, uy, vx, vy, hw, hh = obb
    pts = []
    for su, sv in [(1, 1), (1, -1), (-1, -1), (-1, 1)]:
        pts.append((cx + su * hw * ux + sv * hh * vx,
                    cy + su * hw * uy + sv * hh * vy))
    return [("seg", pts[i][0], pts[i][1],
             pts[(i + 1) % 4][0], pts[(i + 1) % 4][1]) for i in range(4)]


def _d_seg_obb(seg, obb):
    _, ax, ay, bx, by = seg
    if _pt_in_obb(ax, ay, obb) or _pt_in_obb(bx, by, obb):
        return 0.0
    return min(_d_seg_seg(seg, edge) for edge in _obb_edges(obb))


def _d_obb_obb(a, b):
    for (px, py) in _obb_corners(a):
        if _pt_in_obb(px, py, b):
            return 0.0
    for (px, py) in _obb_corners(b):
        if _pt_in_obb(px, py, a):
            return 0.0
    return min(_d_seg_seg(ea, eb)
               for ea in _obb_edges(a) for eb in _obb_edges(b))


def _d_core(a, b):
    ka, kb = a[0], b[0]
    if ka == "pt":
        if kb == "pt":
            return _d_pt_pt(a[1], a[2], b[1], b[2])
        if kb == "seg":
            return _d_pt_seg(a[1], a[2], b[1], b[2], b[3], b[4])
        return _d_pt_obb(a[1], a[2], b)
    if ka == "seg":
        if kb == "pt":
            return _d_pt_seg(b[1], b[2], a[1], a[2], a[3], a[4])
        if kb == "seg":
            return _d_seg_seg(a, b)
        return _d_seg_obb(a, b)
    # a is obb
    if kb == "pt":
        return _d_pt_obb(b[1], b[2], a)
    if kb == "seg":
        return _d_seg_obb(b, a)
    return _d_obb_obb(a, b)


def _core_bbox(core, radius):
    kind = core[0]
    if kind == "pt":
        xs, ys = (core[1],), (core[2],)
    elif kind == "seg":
        xs, ys = (core[1], core[3]), (core[2], core[4])
    else:
        corners = _obb_corners(core)
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
    return (min(xs) - radius, min(ys) - radius,
            max(xs) + radius, max(ys) + radius)


# ---------------------------------------------------------------------------
# Arc / curve subdivision (sagitta-bounded)
# ---------------------------------------------------------------------------


def _circle_pieces(cx, cy, radius):
    """(segment, sagitta) pieces for a full circle, generated
    directly. Routing a circle through the 3-point arc path with
    start == end degenerates to a single point (audit F2), which
    silently disabled copper-to-edge on round boards."""
    if radius <= ARC_SAGITTA:
        # The whole disk lies within `radius` of the chord.
        return [((cx - radius, cy, cx + radius, cy), radius)]
    max_step = 2.0 * math.acos(max(-1.0, 1.0 - ARC_SAGITTA / radius))
    n = max(8, min(MAX_ARC_PIECES,
                   int(math.ceil(2.0 * math.pi / max_step))))
    sagitta = radius * (1.0 - math.cos(math.pi / n))
    pts = [(cx + radius * math.cos(2.0 * math.pi * i / n),
            cy + radius * math.sin(2.0 * math.pi * i / n))
           for i in range(n)]
    return [((pts[i][0], pts[i][1],
              pts[(i + 1) % n][0], pts[(i + 1) % n][1]), sagitta)
            for i in range(n)]


def _arc_pieces(sx, sy, mx, my, ex, ey):
    """Subdivide a 3-point arc into (segment, sagitta) pieces.

    Returns a list of ((x1, y1, x2, y2), sagitta). A degenerate arc
    (collinear points) becomes one straight segment whose sagitta
    is the mid point's distance from the chord — the true curve
    stays within that bound of the chord."""
    if (sx, sy) == (ex, ey) and (mx, my) != (sx, sy):
        # A genuine 360-degree arc: start == end zeroes the
        # circle-through-3-points determinant below. KiCad places
        # the mid point on the arc; treating start/mid as
        # antipodal recovers the circle (audit F2).
        ccx, ccy = (sx + mx) / 2.0, (sy + my) / 2.0
        r = math.hypot(sx - ccx, sy - ccy)
        if r > 0 and abs(math.hypot(mx - ccx, my - ccy) - r) \
                <= max(0.01, 0.01 * r):
            return _circle_pieces(ccx, ccy, r)
    chord_sag = _d_pt_seg(mx, my, sx, sy, ex, ey)
    # Circle through the three points (perpendicular bisector).
    d = 2.0 * (sx * (my - ey) + mx * (ey - sy) + ex * (sy - my))
    if abs(d) < 1e-12:
        return [((sx, sy, ex, ey), chord_sag)]
    ux = ((sx * sx + sy * sy) * (my - ey) + (mx * mx + my * my) * (ey - sy)
          + (ex * ex + ey * ey) * (sy - my)) / d
    uy = ((sx * sx + sy * sy) * (ex - mx) + (mx * mx + my * my) * (sx - ex)
          + (ex * ex + ey * ey) * (mx - sx)) / d
    radius = math.hypot(sx - ux, sy - uy)
    if radius <= ARC_SAGITTA or not math.isfinite(radius):
        return [((sx, sy, ex, ey), chord_sag)]
    a0 = math.atan2(sy - uy, sx - ux)
    a1 = math.atan2(my - uy, mx - ux)
    a2 = math.atan2(ey - uy, ex - ux)
    # Sweep from a0 to a2 passing through a1.
    def _norm(a):
        return a % (2.0 * math.pi)
    ccw_total = _norm(a2 - a0)
    ccw_mid = _norm(a1 - a0)
    if ccw_mid <= ccw_total:
        sweep = ccw_total
    else:
        sweep = ccw_total - 2.0 * math.pi
    max_step = 2.0 * math.acos(max(-1.0, 1.0 - ARC_SAGITTA / radius))
    pieces = int(math.ceil(abs(sweep) / max_step)) if max_step > 0 else 1
    pieces = max(1, min(pieces, MAX_ARC_PIECES))
    step = sweep / pieces
    sagitta = radius * (1.0 - math.cos(step / 2.0))
    pts = [(ux + radius * math.cos(a0 + step * i),
            uy + radius * math.sin(a0 + step * i))
           for i in range(pieces + 1)]
    pts[0] = (sx, sy)
    pts[-1] = (ex, ey)
    return [((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]), sagitta)
            for i in range(pieces)]


def _bezier_pieces(pts):
    """Subdivide a cubic Bézier into (segment, bound) pieces by de
    Casteljau splitting until the control polygon is flat within
    ARC_SAGITTA of the chord (the curve stays inside the control
    polygon's convex hull, so the bound is sound)."""
    out = []

    def _flat(p):
        return max(_d_pt_seg(p[1][0], p[1][1], p[0][0], p[0][1],
                             p[3][0], p[3][1]),
                   _d_pt_seg(p[2][0], p[2][1], p[0][0], p[0][1],
                             p[3][0], p[3][1]))

    def _split(p):
        def mid(a, b):
            return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        q0, q1, q2 = mid(p[0], p[1]), mid(p[1], p[2]), mid(p[2], p[3])
        r0, r1 = mid(q0, q1), mid(q1, q2)
        s = mid(r0, r1)
        return (p[0], q0, r0, s), (s, r1, q2, p[3])

    stack = [tuple(pts)]
    guard = 0
    while stack and guard < 4096:
        guard += 1
        curve = stack.pop()
        flat = _flat(curve)
        if flat <= ARC_SAGITTA or guard > 2048:
            out.append(((curve[0][0], curve[0][1],
                         curve[3][0], curve[3][1]), flat))
        else:
            left, right = _split(curve)
            stack.append(right)
            stack.append(left)
    out.sort(key=lambda piece: piece[0])
    return out


# ---------------------------------------------------------------------------
# Copper objects
# ---------------------------------------------------------------------------


@dataclass
class _Obj:
    core: tuple
    radius: float
    unc: float                   # distance uncertainty bound (arcs)
    layers: tuple
    net_key: object              # ("id", n) / ("name", s) / None
    net_name: str
    desc: str
    bbox: tuple = ()
    # True for a pad entry that shares its footprint position
    # AND number with another entry — one electrical pad written
    # as several padstack entries (e.g. an SMD tongue plus a
    # thru-hole barrel for castellated modules). The zone pass
    # skips these (see _zone_clearance_pass); the object pass
    # measures every entry as before.
    pad_group: bool = False


def _net_key(net_id, net_name):
    if net_id is not None and net_id != 0:
        return ("id", net_id)
    if net_name:
        return ("name", net_name)
    return None


def _short(text, limit=40):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _pad_shape(pad):
    """(core, radius) for a modelled pad, in absolute board coords."""
    cx, cy = pad.cx, pad.cy
    if pad.shape == "circle" or (pad.sx == pad.sy and pad.shape == "oval"):
        return ("pt", cx, cy), pad.sx / 2.0
    if pad.shape == "oval":
        r = min(pad.sx, pad.sy) / 2.0
        half = (max(pad.sx, pad.sy) - min(pad.sx, pad.sy)) / 2.0
        if pad.sx >= pad.sy:
            dx, dy = pad.ux * half, pad.uy * half
        else:
            dx, dy = pad.vx * half, pad.vy * half
        return ("seg", cx - dx, cy - dy, cx + dx, cy + dy), r
    if pad.shape == "rect":
        return ("obb", cx, cy, pad.ux, pad.uy, pad.vx, pad.vy,
                pad.sx / 2.0, pad.sy / 2.0), 0.0
    # roundrect: exact core rectangle + corner radius
    r = pad.rratio * min(pad.sx, pad.sy)
    hw = max(pad.sx / 2.0 - r, 0.0)
    hh = max(pad.sy / 2.0 - r, 0.0)
    return ("obb", cx, cy, pad.ux, pad.uy, pad.vx, pad.vy, hw, hh), r


def _build_objects(project, legacy_sign=None):
    """All copper distance objects + the outline centrelines.

    Returns (objects, outline_pieces, total_objects,
    total_outline_pieces). The lists are CAPPED at their budgets
    while the totals count everything: arc subdivision can
    amplify one small file ~6,000x, so the budgets must bite
    during construction, not after it (audit F3). outline_pieces
    are (x1, y1, x2, y2, unc) centreline segments with zero
    width. `legacy_sign` is the sweep direction the
    outline-closure evaluation settled on for legacy
    (angle ...) arcs; without it those arcs contribute no
    centreline (RT-18)."""
    geo = project.geometry
    objects = []
    total_objects = 0

    def _add(obj):
        nonlocal total_objects
        total_objects += 1
        if len(objects) <= MAX_OBJECTS:
            objects.append(obj)

    # Padstack groups: entries sharing (ref, number, position)
    # are one pad written as multiple entries.
    group_keys = {}
    for pad in geo.pads:
        key = (pad.ref, pad.name, round(pad.cx, 3), round(pad.cy, 3))
        group_keys[key] = group_keys.get(key, 0) + 1
    for pad in geo.pads:
        core, radius = _pad_shape(pad)
        desc = f"pad {_short(pad.ref)}"
        if pad.name:
            desc += f"·{_short(pad.name, 12)}"
        key = (pad.ref, pad.name, round(pad.cx, 3), round(pad.cy, 3))
        _add(_Obj(core, radius, 0.0, pad.layers,
                  _net_key(pad.net_id, pad.net_name),
                  pad.net_name, desc,
                  pad_group=group_keys.get(key, 0) > 1))
    for track in geo.tracks:
        _add(_Obj(
            ("seg", track.x1, track.y1, track.x2, track.y2),
            track.width / 2.0, 0.0, (track.layer,),
            _net_key(track.net_id, track.net_name), track.net_name,
            f"track on {_short(track.layer, 16)}"))
    for arc in geo.arcs:
        for (seg, sag) in _arc_pieces(arc.sx, arc.sy, arc.mx, arc.my,
                                      arc.ex, arc.ey):
            _add(_Obj(
                ("seg",) + seg, arc.width / 2.0, sag, (arc.layer,),
                _net_key(arc.net_id, arc.net_name), arc.net_name,
                f"arc track on {_short(arc.layer, 16)}"))
    for via in geo.vias:
        _add(_Obj(
            ("pt", via.cx, via.cy), via.size / 2.0, 0.0, via.layers,
            _net_key(via.net_id, via.net_name), via.net_name, "via"))
    for elem, width in geo.gr_copper:
        for (seg, unc) in _elem_pieces(elem):
            _add(_Obj(
                ("seg",) + seg, width / 2.0, unc,
                tuple(geo.copper_layers), None, "",
                "drawn copper graphic"))
    outline = []
    total_pieces = 0
    for elem in geo.edge_elems:
        for (seg, unc) in _elem_pieces(elem, legacy_sign):
            total_pieces += 1
            if len(outline) <= MAX_OUTLINE_PIECES:
                outline.append(seg + (unc,))
    for obj in objects:
        obj.bbox = _core_bbox(obj.core, obj.radius)
    return objects, outline, total_objects, total_pieces


def _elem_pieces(elem, legacy_sign=None):
    """(segment, unc) centrelines for an outline/graphic element."""
    kind = elem.kind
    if kind == "line":
        (x1, y1), (x2, y2) = elem.pts
        return [((x1, y1, x2, y2), 0.0)]
    if kind == "rect":
        (x1, y1), (x2, y2) = elem.pts
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return [((corners[i][0], corners[i][1],
                  corners[(i + 1) % 4][0], corners[(i + 1) % 4][1]), 0.0)
                for i in range(4)]
    if kind == "poly":
        pts = elem.pts
        return [((pts[i][0], pts[i][1],
                  pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1]),
                 0.0) for i in range(len(pts))]
    if kind == "circle":
        (cx, cy), radius = elem.pts
        return _circle_pieces(cx, cy, radius) if radius > 0 else []
    if kind == "arc":
        if len(elem.pts) == 3:
            (sx, sy), (mx, my), (ex, ey) = elem.pts
            return _arc_pieces(sx, sy, mx, my, ex, ey)
        if elem.angle is not None and legacy_sign is not None:
            # Legacy (angle ...) arc: rebuild start/mid/end from
            # the centre, the on-arc point and the closure pass's
            # chosen sweep direction (RT-18).
            (cx, cy), (ex, ey) = elem.pts
            dx, dy = ex - cx, ey - cy
            mx, my = _rotate_b(dx, dy,
                               legacy_sign * elem.angle / 2.0)
            fx, fy = _rotate_b(dx, dy, legacy_sign * elem.angle)
            return _arc_pieces(ex, ey, cx + mx, cy + my,
                               cx + fx, cy + fy)
        return []       # legacy angle form, sign unknown: chain-only
    if kind == "curve":
        return _bezier_pieces(elem.pts)
    return []


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def _class_clearance(net_name, rules):
    classes = rules.get("netclasses") or {}
    chosen = None
    for pattern, cls in rules.get("netclass_patterns") or []:
        if fnmatch.fnmatchcase(net_name or "", pattern):
            # KiCad's pattern list is priority-ordered: the FIRST
            # matching pattern assigns the class (v0.4 review
            # RT-08/RT-11 — this originally kept the last match,
            # which both over- and under-checked by pattern order).
            chosen = cls
            break
    if chosen is not None and chosen in classes:
        return classes[chosen]
    return classes.get("Default")


def _pair_threshold(name_a, name_b, rules, cache=None):
    """(threshold, source) for a different-net pair.

    `cache` memoises per-net class clearances across a run: the
    pattern list is fnmatched per net name, and without the cache
    a pair-dense board paid that cost per candidate pair."""
    if cache is None:
        cache = {}

    def _cc(name):
        if name not in cache:
            cache[name] = _class_clearance(name, rules)
        return cache[name]

    values = [v for v in (_cc(name_a), _cc(name_b),
                          rules.get("min_clearance")) if v]
    if values:
        return max(values), "board design rules"
    return FAB_CLEARANCE_MIN, "published fab minimum"


def _edge_threshold(rules):
    stated = rules.get("min_copper_edge_clearance") if rules else None
    if stated:
        return stated, "board design rules"
    return FAB_EDGE_CLEARANCE_MIN, "published fab minimum"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@dataclass
class _Stats:
    objects: int = 0
    pairs_checked: int = 0
    pairs_unknown_net: int = 0
    budget_exceeded: bool = False
    clearance_threshold: float = None
    clearance_source: str = ""
    edge_threshold: float = None
    edge_source: str = ""
    outline_closed: bool = None
    outline_elements: int = 0


def _clearance_pass(objects, rules, stats):
    """Yield (upper_bound_distance, lower_bound, a, b, layer, thr,
    source) for every different-net pair that is certainly below
    its threshold. Sets stats.pairs_checked / pairs_unknown_net."""
    by_layer = {}
    for idx, obj in enumerate(objects):
        for layer in obj.layers:
            by_layer.setdefault(layer, []).append(idx)
    thresholds = set()
    results = []
    thr_cache = {}
    examined = 0
    for layer in sorted(by_layer):
        idxs = by_layer[layer]
        layer_objs = [objects[i] for i in idxs]
        # Threshold ceiling for this layer (grid/search radius).
        max_thr = FAB_CLEARANCE_MIN
        for obj in layer_objs:
            value, _ = _pair_threshold(obj.net_name, obj.net_name,
                                       rules, thr_cache)
            max_thr = max(max_thr, value)
        cell = max(1.0, max_thr)
        grid = {}
        wide = []
        insertions = 0
        degenerate = False
        for pos, obj in enumerate(layer_objs):
            x0, y0, x1, y1 = obj.bbox
            nx = int((x1 + max_thr) // cell) \
                - int((x0 - max_thr) // cell) + 1
            ny = int((y1 + max_thr) // cell) \
                - int((y0 - max_thr) // cell) + 1
            if nx * ny > MAX_OBJ_CELLS:
                # Board-spanning object: never inserted cell-wise
                # (RT-01); it is tested against everything instead.
                wide.append(pos)
                continue
            for gx in range(int((x0 - max_thr) // cell),
                            int((x1 + max_thr) // cell) + 1):
                for gy in range(int((y0 - max_thr) // cell),
                                int((y1 + max_thr) // cell) + 1):
                    bucket = grid.setdefault((gx, gy), [])
                    bucket.append(pos)
                    insertions += 1
                    if len(bucket) > MAX_CELL_OBJECTS \
                            or insertions > MAX_GRID_INSERTIONS:
                        degenerate = True
        if len(wide) > MAX_WIDE_OBJECTS:
            degenerate = True
        if degenerate:
            stats.budget_exceeded = True
            return results
        wide_set = set(wide)
        seen = set()
        for pos, obj in enumerate(layer_objs):
            if pos in wide_set:
                candidates = range(len(layer_objs))
            else:
                x0, y0, x1, y1 = obj.bbox
                found = set(wide)
                for gx in range(int((x0 - max_thr) // cell),
                                int((x1 + max_thr) // cell) + 1):
                    for gy in range(int((y0 - max_thr) // cell),
                                    int((y1 + max_thr) // cell) + 1):
                        found.update(grid.get((gx, gy), ()))
                candidates = found
            for other_pos in candidates:
                if other_pos <= pos:
                    continue
                # Charge EVERY examined candidate to a budget
                # (audit F1): the measurement budget below only
                # counts pairs that survive net + bbox filtering,
                # so dense same-net clusters otherwise enumerated
                # without bound. (Iteration order over the
                # candidate set does not affect results: pairs are
                # deduplicated by `seen`, counts are sums, and
                # findings are sorted before reporting.)
                examined += 1
                if examined > MAX_CANDIDATE_PAIRS:
                    stats.budget_exceeded = True
                    return results
                pair = (idxs[pos], idxs[other_pos])
                if pair in seen:
                    continue
                seen.add(pair)
                other = layer_objs[other_pos]
                if obj.net_key is None or other.net_key is None:
                    stats.pairs_unknown_net += 1
                    continue
                if obj.net_key == other.net_key:
                    continue
                # Bounding-box lower bound: cheap reject.
                ax0, ay0, ax1, ay1 = obj.bbox
                bx0, by0, bx1, by1 = other.bbox
                gap = math.hypot(max(bx0 - ax1, ax0 - bx1, 0.0),
                                 max(by0 - ay1, ay0 - by1, 0.0))
                thr, source = _pair_threshold(obj.net_name,
                                              other.net_name, rules,
                                              thr_cache)
                thresholds.add((thr, source))
                if gap >= thr:
                    continue
                stats.pairs_checked += 1
                if stats.pairs_checked > MAX_PAIR_CHECKS:
                    stats.budget_exceeded = True
                    return results
                dist = _d_core(obj.core, other.core) \
                    - obj.radius - other.radius
                upper = dist + obj.unc + other.unc
                if upper < thr - MEASURE_TOLERANCE:
                    results.append((dist - obj.unc - other.unc,
                                    obj, other, layer, thr, source))
    if thresholds:
        top = max(thresholds)
        stats.clearance_threshold, stats.clearance_source = top
    return results


# ---------------------------------------------------------------------------
# Zone filled polygons (v0.6 WS3)
# ---------------------------------------------------------------------------


class _ZoneBudget(Exception):
    """A zone-pass budget ran out: the zone pass abstains."""


@dataclass
class _ZonePoly:
    layer: str = ""
    pts: tuple = ()
    edges: tuple = ()            # (x1, y1, x2, y2), zero-length dropped
    bbox: tuple = ()
    net_key: object = None
    net_name: str = ""
    desc: str = ""
    obj: object = None           # reporting _Obj for this polygon
    y_buckets: dict = field(default_factory=dict)   # row -> [edge idx]
    y_wide: tuple = ()           # edges spanning too many rows


def _build_zone_polys(project):
    """(_ZonePoly list, stats) from project.geometry.zones_geo.

    One _ZonePoly per readable filled-polygon ring. A zone with
    no readable ring is counted abstained (zones_abstained =
    zones_total - zones_modelled), consistent with the
    GEOMETRY_SKIPPED accounting style: the count is disclosed,
    never silent. Rings are closed (last point connects back to
    the first; a stored closing repeat is harmless)."""
    geo = project.geometry
    polys = []
    zones_modelled = 0
    total_edges = 0
    for zone in getattr(geo, "zones_geo", ()) or ():
        modelled = False
        label = zone.name or zone.net_name or "unnamed"
        for layer, pts in zone.polygons:
            if len(pts) < 3:
                continue
            ring = list(pts)
            if ring[0] == ring[-1] and len(ring) > 1:
                ring.pop()
            if len(ring) < 3:
                continue
            edges = []
            for i in range(len(ring)):
                x1, y1 = ring[i]
                x2, y2 = ring[(i + 1) % len(ring)]
                if (x1, y1) != (x2, y2):
                    edges.append((x1, y1, x2, y2))
            if not edges:
                continue
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            poly = _ZonePoly(
                layer=layer, pts=tuple(ring), edges=tuple(edges),
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                net_key=_net_key(zone.net_id, zone.net_name),
                net_name=zone.net_name,
                desc=f"zone {_short(label, 24)} filled polygon")
            poly.obj = _Obj(("pt", ring[0][0], ring[0][1]), 0.0, 0.0,
                            (layer,), poly.net_key, zone.net_name,
                            poly.desc)
            # Ray-cast index: edges bucketed by the 1 mm rows
            # their y-range spans; an edge spanning >256 rows
            # (a board-length straight side) joins y_wide instead
            # of hundreds of buckets.
            wide = []
            for ei, (x1, y1, x2, y2) in enumerate(edges):
                lo = int(min(y1, y2) // ZONE_Y_BUCKET)
                hi = int(max(y1, y2) // ZONE_Y_BUCKET)
                if hi - lo > 256:
                    wide.append(ei)
                    continue
                for row in range(lo, hi + 1):
                    poly.y_buckets.setdefault(row, []).append(ei)
            poly.y_wide = tuple(wide)
            polys.append(poly)
            total_edges += len(edges)
            modelled = True
        if modelled:
            zones_modelled += 1
    stats = {
        "zones_total_geo": len(getattr(geo, "zones_geo", ()) or ()),
        "zones_modelled": zones_modelled,
        "zones_abstained": geo.zones_total - zones_modelled,
        "zones_polygons": len(polys),
        "zones_edges": total_edges,
        "zones_fill_unreadable": getattr(geo, "zones_fill_unreadable", 0),
    }
    return polys, stats


def _zone_point_inside(poly, px, py, ops):
    """True when (px, py) is inside the filled ring (even-odd ray
    casting over the y-bucketed edges). `ops` is a shared [count]
    containment budget; exceeding it raises _ZoneBudget and the
    caller abstains containment only (edge distances still ran).
    A point exactly on an edge is distance 0 from the fill, which
    the edge pass already flags, so the ray test's boundary
    ambiguity cannot hide a violation."""
    x0, y0, x1, y1 = poly.bbox
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    inside = False
    row = int(py // ZONE_Y_BUCKET)
    for ei in list(poly.y_buckets.get(row, ())) + list(poly.y_wide):
        ops[0] += 1
        if ops[0] > MAX_ZONE_CONTAINMENT_OPS:
            raise _ZoneBudget
        ex1, ey1, ex2, ey2 = poly.edges[ei]
        if (ey1 > py) != (ey2 > py):
            xint = ex1 + (py - ey1) * (ex2 - ex1) / (ey2 - ey1)
            if px < xint:
                inside = not inside
    return inside


def _obj_rep_points(obj):
    """Representative core points for a containment test: the
    centre/extremes of the shape's core. If any lies inside a
    fill while the shape's edge distance exceeds the threshold,
    the shape's copper sits inside the fill (distance 0)."""
    core = obj.core
    if core[0] == "pt":
        return [(core[1], core[2])]
    if core[0] == "seg":
        return [(core[1], core[2]),
                ((core[1] + core[3]) / 2.0, (core[2] + core[4]) / 2.0),
                (core[3], core[4])]
    return [(core[1], core[2])] + _obb_corners(core)


def _zone_grid(poly, cell, max_thr):
    """(grid, wide) spatial index over one polygon's edges.
    grid maps a cell to edge indices whose bbox touches it; an
    edge spanning too many cells joins `wide` (tested against
    every query on this polygon) instead of exploding the index
    — the RT-01 guard, per polygon."""
    grid = {}
    wide = []
    for ei, (x1, y1, x2, y2) in enumerate(poly.edges):
        nx = int(max(x1, x2) // cell) - int(min(x1, x2) // cell) + 1
        ny = int(max(y1, y2) // cell) - int(min(y1, y2) // cell) + 1
        if nx * ny > MAX_OBJ_CELLS:
            wide.append(ei)
            continue
        for gx in range(int(min(x1, x2) // cell),
                        int(max(x1, x2) // cell) + 1):
            for gy in range(int(min(y1, y2) // cell),
                            int(max(y1, y2) // cell) + 1):
                grid.setdefault((gx, gy), []).append(ei)
    return grid, wide


def _grid_candidates(grid, bbox, thr, cell):
    """Edge indices stored in the cells a thr-expanded bbox spans."""
    x0, y0, x1, y1 = bbox
    found = set()
    for gx in range(int((x0 - thr) // cell), int((x1 + thr) // cell) + 1):
        for gy in range(int((y0 - thr) // cell),
                        int((y1 + thr) // cell) + 1):
            found.update(grid.get((gx, gy), ()))
    return found


def _zone_clearance_pass(polys, objects, rules):
    """Zone clearance: filled polygons vs pads/tracks/arcs/vias
    and vs other zones' filled polygons, per layer.

    Returns (violations, counters, budget_exceeded) with
    violations in _clearance_pass's tuple shape. A (polygon,
    other) pair is counted ONCE at its minimum distance — a
    polygon is one copper object, however many edges it has.
    Containment (core point inside a different-net fill) records
    distance 0 for the pair when the edge distance did not
    already flag it.

    Two evidence-based scopings (kicad-cli 8.0.9 DRC used as the
    reference implementation; experiments in the v06-ws3 review
    folder):
    - Pairs are judged at ZONE_MEASURE_TOLERANCE, not
      MEASURE_TOLERANCE: fill boundaries are chord approximations
      on both sides of a zone pair (see the constant).
    - Zone vs padstack-group pads (obj.pad_group) is NOT
      measured. KiCad's filler clears a zone around one shape of
      a multi-entry pad (for the RPi Pico footprint on piantor,
      the thru-hole oval), leaving the other entry's copper (the
      SMD tongue) inside the stored fill by up to ~0.39 mm;
      KiCad's own DRC reports no violation there (verified on
      the full board and on a verbatim zone+footprint reduction,
      including with the pad moved to a foreign net), while a
      lone SMD pad at the same position against the same fill
      IS flagged. Measuring every entry's raw shape would
      manufacture false positives on exactly this real-world
      footprint class, so those pairs are skipped and counted
      (padstack_pairs_skipped)."""
    counters = {"pairs_checked": 0, "pairs_unknown_net": 0,
                "candidate_checks": 0, "containment_abstained": False,
                "padstack_pairs_skipped": 0}
    if not polys:
        return [], counters, False
    total_edges = sum(len(p.edges) for p in polys)
    if total_edges > MAX_ZONE_EDGES:
        return [], counters, True
    thr_cache = {}
    by_layer = {}
    for poly in polys:
        by_layer.setdefault(poly.layer, []).append(poly)
    obj_by_layer = {}
    for obj in objects:
        for layer in obj.layers:
            obj_by_layer.setdefault(layer, []).append(obj)
    best = {}                    # (id(a), id(b)) -> violation tuple

    def _record(key, lower, a, b, layer, thr, source):
        old = best.get(key)
        if old is None or lower < old[0]:
            best[key] = (lower, a, b, layer, thr, source)

    ops = [0]
    try:
        for layer in sorted(by_layer):
            layer_polys = by_layer[layer]
            max_thr = FAB_CLEARANCE_MIN
            for poly in layer_polys:
                value, _ = _pair_threshold(poly.net_name, poly.net_name,
                                           rules, thr_cache)
                max_thr = max(max_thr, value)
            for obj in obj_by_layer.get(layer, ()):
                value, _ = _pair_threshold(obj.net_name, obj.net_name,
                                           rules, thr_cache)
                max_thr = max(max_thr, value)
            cell = max(1.0, max_thr)
            grids = {id(p): _zone_grid(p, cell, max_thr)
                     for p in layer_polys}
            # --- zone vs copper objects -------------------------------
            for obj in obj_by_layer.get(layer, ()):
                for poly in layer_polys:
                    if obj.net_key is None or poly.net_key is None:
                        counters["pairs_unknown_net"] += 1
                        continue
                    if obj.net_key == poly.net_key:
                        continue
                    if obj.pad_group:
                        counters["padstack_pairs_skipped"] += 1
                        continue
                    thr, source = _pair_threshold(
                        obj.net_name, poly.net_name, rules, thr_cache)
                    grid, wide = grids[id(poly)]
                    cand = _grid_candidates(grid, obj.bbox, max_thr, cell)
                    cand.update(wide)
                    pair_min = None
                    for ei in cand:
                        counters["candidate_checks"] += 1
                        if counters["candidate_checks"] \
                                > MAX_ZONE_CANDIDATE_CHECKS:
                            return [], counters, True
                        ex1, ey1, ex2, ey2 = poly.edges[ei]
                        gap = math.hypot(
                            max(min(ex1, ex2) - obj.bbox[2],
                                obj.bbox[0] - max(ex1, ex2), 0.0),
                            max(min(ey1, ey2) - obj.bbox[3],
                                obj.bbox[1] - max(ey1, ey2), 0.0))
                        if gap >= thr:
                            continue
                        counters["pairs_checked"] += 1
                        if counters["pairs_checked"] \
                                > MAX_ZONE_PAIR_CHECKS:
                            return [], counters, True
                        dist = _d_core(obj.core,
                                       ("seg", ex1, ey1, ex2, ey2)) \
                            - obj.radius
                        if pair_min is None or dist < pair_min:
                            pair_min = dist
                    if pair_min is not None:
                        upper = pair_min + obj.unc
                        if upper < thr - ZONE_MEASURE_TOLERANCE:
                            _record((id(poly), id(obj)),
                                    pair_min - obj.unc, poly.obj, obj,
                                    layer, thr, source)
                            continue
                    # Containment: the edge distance can exceed the
                    # threshold while the shape sits deep inside
                    # the fill (stale fill over a moved pad).
                    if not counters["containment_abstained"]:
                        try:
                            for (px, py) in _obj_rep_points(obj):
                                if _zone_point_inside(poly, px, py, ops):
                                    _record((id(poly), id(obj)), 0.0,
                                            poly.obj, obj, layer, thr,
                                            source)
                                    break
                        except _ZoneBudget:
                            counters["containment_abstained"] = True
            # --- zone vs zone ------------------------------------------
            for i, poly_a in enumerate(layer_polys):
                for poly_b in layer_polys[i + 1:]:
                    if poly_a.net_key is None or poly_b.net_key is None:
                        counters["pairs_unknown_net"] += 1
                        continue
                    if poly_a.net_key == poly_b.net_key:
                        continue
                    thr, source = _pair_threshold(
                        poly_a.net_name, poly_b.net_name, rules, thr_cache)
                    small, big = (poly_a, poly_b) \
                        if len(poly_a.edges) <= len(poly_b.edges) \
                        else (poly_b, poly_a)
                    grid, wide = grids[id(big)]
                    pair_min = None
                    for (ex1, ey1, ex2, ey2) in small.edges:
                        ebbox = (min(ex1, ex2), min(ey1, ey2),
                                 max(ex1, ex2), max(ey1, ey2))
                        cand = _grid_candidates(grid, ebbox, max_thr,
                                                cell)
                        cand.update(wide)
                        for ei in cand:
                            counters["candidate_checks"] += 1
                            if counters["candidate_checks"] \
                                    > MAX_ZONE_CANDIDATE_CHECKS:
                                return [], counters, True
                            fx1, fy1, fx2, fy2 = big.edges[ei]
                            gap = math.hypot(
                                max(min(fx1, fx2) - ebbox[2],
                                    ebbox[0] - max(fx1, fx2), 0.0),
                                max(min(fy1, fy2) - ebbox[3],
                                    ebbox[1] - max(fy1, fy2), 0.0))
                            if gap >= thr:
                                continue
                            counters["pairs_checked"] += 1
                            if counters["pairs_checked"] \
                                    > MAX_ZONE_PAIR_CHECKS:
                                return [], counters, True
                            dist = _d_seg_seg(
                                ("seg", ex1, ey1, ex2, ey2),
                                ("seg", fx1, fy1, fx2, fy2))
                            if pair_min is None or dist < pair_min:
                                pair_min = dist
                    if pair_min is not None \
                            and pair_min < thr - ZONE_MEASURE_TOLERANCE:
                        _record((id(poly_a), id(poly_b)), pair_min,
                                poly_a.obj, poly_b.obj, layer, thr,
                                source)
                        continue
                    if not counters["containment_abstained"]:
                        try:
                            if _zone_point_inside(
                                    poly_b, poly_a.pts[0][0],
                                    poly_a.pts[0][1], ops) \
                                    or _zone_point_inside(
                                        poly_a, poly_b.pts[0][0],
                                        poly_b.pts[0][1], ops):
                                _record((id(poly_a), id(poly_b)), 0.0,
                                        poly_a.obj, poly_b.obj, layer,
                                        thr, source)
                        except _ZoneBudget:
                            counters["containment_abstained"] = True
    except _ZoneBudget:
        counters["containment_abstained"] = True
    violations = list(best.values())
    # Deterministic order independent of id(): sort by content.
    violations.sort(key=lambda v: (v[0], v[1].desc, v[2].desc, v[3]))
    return violations, counters, False


def _zone_edge_pass(polys, outline, grid, wide_pieces, cell, thr):
    """Zone copper-to-edge: (violating_polygon_count, worst).

    A polygon counts once, at its closest edge's distance to the
    outline — same per-object semantics as the main edge pass.
    `grid`/`wide_pieces`/`cell` are the outline index built by
    run_geometry_checks. Returns worst as (lower, zone _Obj)."""
    count = 0
    worst = None
    for poly in polys:
        best_poly = None       # (upper, lower), as in the main pass
        for (ex1, ey1, ex2, ey2) in poly.edges:
            ebbox = (min(ex1, ex2), min(ey1, ey2),
                     max(ex1, ex2), max(ey1, ey2))
            cand = _grid_candidates(grid, ebbox, thr, cell)
            cand.update(wide_pieces)
            for pidx in cand:
                px1, py1, px2, py2, unc = outline[pidx]
                dist = _d_seg_seg(("seg", ex1, ey1, ex2, ey2),
                                  ("seg", px1, py1, px2, py2))
                pair = (dist + unc, dist - unc)
                if best_poly is None or pair[0] < best_poly[0]:
                    best_poly = pair
        if best_poly is not None \
                and best_poly[0] < thr - ZONE_MEASURE_TOLERANCE:
            count += 1
            if worst is None or best_poly[1] < worst[0]:
                worst = (best_poly[1], poly.obj)
    return count, worst


def _rotate_b(vx, vy, deg):
    """Rotate a vector by `deg` degrees in KiCad's file convention
    (the same convention pad placement uses)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (vx * c + vy * s, -vx * s + vy * c)


def _elem_endpoints(elem, legacy_sign):
    """(p1, p2) chain endpoints for an outline element, or None.

    Legacy (angle ...) arcs are genuinely ambiguous in the wild:
    KiCad <= 5 semantics store the arc circle's CENTRE in the
    (start) node (v0.4 review RT-04), but converter-written files
    exist that store the two endpoints and keep (angle) only as
    the sweep. Callers therefore evaluate several readings —
    `legacy_sign` is +1/-1 for the centre reading (sweep
    direction) or "ends" for the endpoints reading."""
    if elem.kind in ("line", "curve"):
        return elem.pts[0], elem.pts[-1]
    if elem.kind == "arc":
        if elem.angle is not None:
            if legacy_sign == "ends":
                return elem.pts[0], elem.pts[1]
            (cx, cy), (ex, ey) = elem.pts
            rx, ry = _rotate_b(ex - cx, ey - cy,
                               legacy_sign * elem.angle)
            return (ex, ey), (cx + rx, cy + ry)
        return elem.pts[0], elem.pts[-1]
    return None


class _ContainmentAbort(Exception):
    """The containment geometry budgets ran out: abstain."""


def _chain_pieces(pieces, start, end):
    """Order (segment, unc) pieces into a start→end point list by
    shared endpoints, or None when they do not chain cleanly (a
    self-touching curve can make the order ambiguous — callers
    treat that as unrebuildable rather than guess)."""
    segs = [seg for seg, _unc in pieces]
    if not segs:
        return None
    by_point = {}
    for i, (x1, y1, x2, y2) in enumerate(segs):
        by_point.setdefault((x1, y1), []).append(i)
        if (x2, y2) != (x1, y1):
            by_point.setdefault((x2, y2), []).append(i)
    pts = [start]
    cur = start
    used = set()
    while len(used) < len(segs):
        cands = [i for i in by_point.get(cur, ()) if i not in used]
        if not cands:
            return None
        # Prefer a piece that actually leaves `cur` (a zero-length
        # piece is consumed last, never as a stepping stone).
        pick = None
        for i in cands:
            x1, y1, x2, y2 = segs[i]
            other = (x2, y2) if (x1, y1) == cur else (x1, y1)
            if other != cur:
                pick = (i, other)
                break
        if pick is None:
            pick = (cands[0], cur)
        i, cur = pick
        used.add(i)
        pts.append(cur)
    return pts if cur == end else None


def _elem_polyline(elem, legacy_sign, work):
    """Ordered centreline points for one chainable outline
    element, in its _elem_endpoints direction, reusing the
    sagitta-bounded subdivision of the copper-to-edge pass; or
    None when the geometry cannot be rebuilt: a legacy
    (angle ...) arc under the endpoints reading (only its chord
    is knowable), or pieces that do not chain. `work` is the
    shared [points, ops] containment budget; exceeding the points
    budget raises _ContainmentAbort."""
    if elem.kind == "arc" and elem.angle is not None \
            and legacy_sign not in (1, -1):
        return None
    sign = legacy_sign if legacy_sign in (1, -1) else None
    pieces = _elem_pieces(elem, sign)
    if not pieces:
        return None
    work[0] += len(pieces)
    if work[0] > MAX_CONTAINMENT_POINTS:
        raise _ContainmentAbort
    ends = _elem_endpoints(elem, legacy_sign)
    if ends is None:
        return None
    return _chain_pieces(pieces, ends[0], ends[1])


def _chain_test_points(members, edge_elems, legacy_sign, work):
    """Subdivided points of an open chain: every element's piece
    endpoints plus segment midpoints (order is irrelevant to the
    containment test). An element whose curve cannot be rebuilt
    (a legacy endpoints-reading arc) contributes its chord — the
    two endpoints and their midpoint; the bulge side is
    unknowable from the file."""
    pts = []
    for idx in members:
        elem = edge_elems[idx]
        poly = None
        if not (elem.kind == "arc" and elem.angle is not None
                and legacy_sign not in (1, -1)):
            sign = legacy_sign if legacy_sign in (1, -1) else None
            pieces = _elem_pieces(elem, sign)
            if pieces:
                work[0] += len(pieces)
                if work[0] > MAX_CONTAINMENT_POINTS:
                    raise _ContainmentAbort
                poly = [(x1, y1)
                        for ((x1, y1, _x2, _y2), _u) in pieces]
                poly += [(x2, y2)
                         for ((_x1, _y1, x2, y2), _u) in pieces]
                poly += [((x1 + x2) / 2.0, (y1 + y2) / 2.0)
                         for ((x1, y1, x2, y2), _u) in pieces]
        if poly is None:
            ends = _elem_endpoints(elem, legacy_sign)
            if ends is None:
                continue
            (ax, ay), (bx, by) = ends
            poly = [(ax, ay), ((ax + bx) / 2.0, (ay + by) / 2.0),
                    (bx, by)]
        pts.extend(poly)
    return pts


def _self_closed_polygon(elem, work):
    """Ring points for a self-closed element (circle / rect /
    poly), in the order _elem_pieces generates them — ring order
    for all three kinds. [] when the element encloses nothing
    (zero radius, collapsed corners)."""
    pieces = _elem_pieces(elem, None)
    work[0] += len(pieces)
    if work[0] > MAX_CONTAINMENT_POINTS:
        raise _ContainmentAbort
    if len(pieces) < 3:
        return []
    return [(x1, y1) for ((x1, y1, _x2, _y2), _u) in pieces]


def _chained_loop_polygon(members, edge_elems, elem_ends, partner,
                          endpoints, legacy_sign, work):
    """Ring points for a closed chained component, walked in
    cycle order via the endpoint pairing; None when any element's
    geometry cannot be rebuilt (the caller then abstains rather
    than judge containment against an unknown interior)."""
    start = members[0]
    first_entry = elem_ends[start][0]
    ring = []
    elem, entry = start, first_entry
    for _step in range(len(members) + 1):
        i, j = elem_ends[elem]
        exit_idx = j if entry == i else i
        poly = _elem_polyline(edge_elems[elem], legacy_sign, work)
        if poly is None:
            return None
        if entry == j:
            poly = poly[::-1]
        ring.extend(poly[1:] if ring else poly)
        nxt = partner.get(exit_idx)
        if nxt is None:
            return None
        if nxt == first_entry:
            return ring
        elem, entry = endpoints[nxt][2], nxt
    return None


def _loop_container(pts):
    """(ring, bbox) for a closed loop with a real interior, else
    None: fewer than 3 points, or a collapsed / zero-area ring —
    such a loop encloses nothing and can certify no slot."""
    if pts is None or len(pts) < 3:
        return None
    area2 = 0.0
    n = len(pts)
    for k in range(n):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % n]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) <= 1e-9:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return pts, (min(xs), min(ys), max(xs), max(ys))


def _point_in_loop(px, py, container, work):
    """True only when (px, py) is strictly inside the loop:
    inside by even-odd ray casting AND farther than
    CHAIN_TOLERANCE from every edge. A point on (or within the
    chaining tolerance of) the boundary is NOT inside — a chain
    whose endpoint touches the outline is a notch/tab in the
    board edge in KiCad semantics, not an interior slot, so the
    ambiguity resolves in the BLOCK direction."""
    pts, (x0, y0, x1, y1) = container
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    inside = False
    n = len(pts)
    for k in range(n):
        ax, ay = pts[k]
        bx, by = pts[(k + 1) % n]
        work[1] += 1
        if work[1] > MAX_CONTAINMENT_OPS:
            raise _ContainmentAbort
        if _d_pt_seg(px, py, ax, ay, bx, by) <= CHAIN_TOLERANCE:
            return False
        if (ay > py) != (by > py):
            xint = ax + (py - ay) * (bx - ax) / (by - ay)
            if px < xint:
                inside = not inside
    return inside


def _points_contained(test_points, containers, work):
    """True when EVERY test point lies strictly inside at least
    one closed loop."""
    for px, py in test_points:
        if not any(_point_in_loop(px, py, c, work)
                   for c in containers):
            return False
    return True


def _containment_eval(edge_elems, closed_components,
                      open_components, doubtful_components,
                      loose_singles, elem_ends, partner, endpoints,
                      legacy_sign):
    """Judge open chains against the closed loops' interiors.

    Returns a (closed, open_points, abstained) verdict, or None
    when no closed loop encloses anything (every loop collapsed
    to zero area) — the caller then falls back to the no-loop
    rules, since no loop can be the board boundary. A chain
    counts as a routed slot only when every subdivided point
    lies strictly inside some loop; a certain chain that is not
    contained is the outline itself, open. Doubtful chains
    (endpoint pattern too close to the chaining tolerance to
    judge, RT-14) keep their abstention when not contained
    rather than being upgraded to a certain BLOCK."""
    work = [0, 0]
    containers = []
    unrebuildable = False
    try:
        for elem in edge_elems:
            if elem.kind in ("circle", "rect", "poly"):
                box = _loop_container(
                    _self_closed_polygon(elem, work))
                if box is not None:
                    containers.append(box)
        for members in closed_components:
            ring = _chained_loop_polygon(
                members, edge_elems, elem_ends, partner, endpoints,
                legacy_sign, work)
            if ring is None:
                unrebuildable = True
                continue
            box = _loop_container(ring)
            if box is not None:
                containers.append(box)
        if unrebuildable:
            return None, [], True
        if not containers:
            return None
        judged = list(open_components)
        judged += [([idx], [(endpoints[k][0], endpoints[k][1])
                            for k in elem_ends[idx]])
                   for idx in loose_singles]
        bad = []
        for members, pts in judged:
            test = _chain_test_points(members, edge_elems,
                                      legacy_sign, work)
            if not _points_contained(test, containers, work):
                bad.extend(pts)
        if bad:
            return False, bad, False
        for members, _pts in doubtful_components:
            test = _chain_test_points(members, edge_elems,
                                      legacy_sign, work)
            if not _points_contained(test, containers, work):
                return None, [], True
        return True, [], False
    except _ContainmentAbort:
        return None, [], True


def _outline_eval(edge_elems, legacy_sign):
    """One closure evaluation under a legacy-arc sign choice.

    Returns (closed, open_points, abstained); closed None means
    abstain. Endpoints are PAIRED greedily by distance — exact
    coincidences pair first — never transitively clustered, so
    two legal joints a hair apart cannot merge into a phantom
    opening (RT-03), and pairing is grid-bucketed rather than
    all-pairs (RT-02: 17 s at the element budget). Semantics: an
    open chain alongside a closed loop is a routed slot, a legal
    Edge.Cuts construct — but only when the chain lies fully
    inside a closed loop's interior; a chain anywhere else is
    the outline itself, open, however many self-closed elements
    (mounting-hole circles and the like) share the layer (the
    v0.5 benchmark's esp-rust-board mutation: deleting one side
    of the outer chain left a 21.6 mm gap that four gr_circles
    masked into a false PASS). With NO closed loop anywhere,
    chained but open elements — and fragments a hair from
    joining — are the outline itself, broken (RT-12)."""
    self_closed_kinds = {"circle", "rect", "poly"}
    has_closed_elem = False
    endpoints = []       # (x, y, elem_idx)
    elem_ends = {}       # elem_idx -> (i, j) into endpoints
    for idx, elem in enumerate(edge_elems):
        if elem.kind in self_closed_kinds:
            has_closed_elem = True
            continue
        ends = _elem_endpoints(elem, legacy_sign)
        if ends is None:
            continue
        (x1, y1), (x2, y2) = ends
        elem_ends[idx] = (len(endpoints), len(endpoints) + 1)
        endpoints.append((x1, y1, idx))
        endpoints.append((x2, y2, idx))
    if not endpoints:
        return (True, [], False) if has_closed_elem \
            else (None, [], False)
    tol = CHAIN_TOLERANCE
    buckets = {}
    for i, (x, y, _idx) in enumerate(endpoints):
        buckets.setdefault((int(x // tol), int(y // tol)),
                           []).append(i)
    if any(len(v) > 1024 for v in buckets.values()):
        return None, [], True
    cand = []
    tol_sq = tol * tol
    for i, (x, y, _idx) in enumerate(endpoints):
        cx, cy = int(x // tol), int(y // tol)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in buckets.get((gx, gy), ()):
                    if j <= i:
                        continue
                    dx = endpoints[j][0] - x
                    dy = endpoints[j][1] - y
                    d2 = dx * dx + dy * dy
                    if d2 <= tol_sq:
                        cand.append((d2, i, j))
        if len(cand) > 2000000:
            return None, [], True
    cand.sort()
    partner = {}
    for _d2, i, j in cand:
        if i in partner or j in partner:
            continue
        ei, ej = endpoints[i][2], endpoints[j][2]
        if ei == ej and edge_elems[ei].kind != "arc":
            continue    # only a full-circle arc closes on itself
        partner[i] = j
        partner[j] = i
    # Components: elements a pairing links.
    parent = {idx: idx for idx in elem_ends}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, j in partner.items():
        ei, ej = endpoints[i][2], endpoints[j][2]
        if ei != ej:
            ra, rb = find(ei), find(ej)
            if ra != rb:
                parent[ra] = rb
    components = {}
    for idx in elem_ends:
        components.setdefault(find(idx), []).append(idx)

    def _near_foreign(x, y, member_set):
        cx, cy = int(x // tol), int(y // tol)
        limit = (2 * tol) * (2 * tol)
        for gx in range(cx - 2, cx + 3):
            for gy in range(cy - 2, cy + 3):
                for j in buckets.get((gx, gy), ()):
                    fx, fy, fidx = endpoints[j]
                    if fidx in member_set:
                        continue
                    dx, dy = fx - x, fy - y
                    if dx * dx + dy * dy <= limit:
                        return True
        return False

    closed_exists = has_closed_elem
    closed_components = []
    open_components = []        # (members, unmatched points)
    doubtful_components = []    # (members, unmatched points)
    loose_singles = []
    for members in components.values():
        member_set = set(members)
        unmatched = [endpoints[k] for idx in members
                     for k in elem_ends[idx] if k not in partner]
        if not unmatched:
            closed_exists = True
            closed_components.append(members)
            continue
        if len(members) == 1 and len(unmatched) == 2:
            loose_singles.append(members[0])
            continue
        pts = [(x, y) for x, y, _i in unmatched]
        if any(_near_foreign(x, y, member_set)
               for x, y, _i in unmatched):
            doubtful_components.append((members, pts))
        else:
            open_components.append((members, pts))
    open_certain = [p for _m, pts in open_components for p in pts]
    abstained_any = bool(doubtful_components)
    if closed_exists and (open_components or doubtful_components
                          or loose_singles):
        # Closed loops share the layer with open chains: the
        # chains are routed slots only if they lie fully inside
        # a loop's interior (_containment_eval). A verdict of
        # None means no loop encloses anything, so nothing can
        # certify a slot — fall through and judge the chains
        # under the no-loop rules below.
        verdict = _containment_eval(
            edge_elems, closed_components, open_components,
            doubtful_components, loose_singles, elem_ends, partner,
            endpoints, legacy_sign)
        if verdict is not None:
            return verdict
        closed_exists = False   # no loop can be the boundary
    if closed_exists:
        return True, [], False
    if open_certain:
        return False, open_certain, abstained_any
    if abstained_any:
        return None, [], True
    # Only lone elements remain. Fragments of one shattered
    # outline sit within a hair of each other (RT-12); an
    # isolated single abstains (a slot whose outline lives in a
    # footprint, say) rather than guessing.
    loose_pts = [endpoints[k] for idx in loose_singles
                 for k in elem_ends[idx]]
    near = {}
    for n, (x, y, _i) in enumerate(loose_pts):
        near.setdefault(
            (int(x // OUTLINE_NEAR_MISS),
             int(y // OUTLINE_NEAR_MISS)), []).append(n)
    limit = OUTLINE_NEAR_MISS * OUTLINE_NEAR_MISS
    for n, (x, y, idx) in enumerate(loose_pts):
        cx, cy = int(x // OUTLINE_NEAR_MISS), \
            int(y // OUTLINE_NEAR_MISS)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for m in near.get((gx, gy), ()):
                    if m <= n or loose_pts[m][2] == idx:
                        continue
                    dx = loose_pts[m][0] - x
                    dy = loose_pts[m][1] - y
                    if dx * dx + dy * dy <= limit:
                        return False, [(x, y),
                                       (loose_pts[m][0],
                                        loose_pts[m][1])], False
    return None, [], True


def _outline_state(edge_elems):
    """(closed, open_endpoint_points, abstained, legacy_sign).

    closed is True/False, or None when the check abstains (too
    many elements, or the evidence is genuinely ambiguous).
    legacy_sign is the sweep-direction choice under which legacy
    (angle ...) arcs were evaluated — the copper-to-edge pass
    reuses it to rebuild those arcs' centrelines (RT-18). When a
    board has legacy arcs, every plausible reading is evaluated
    (centre semantics under both sweep signs, and the endpoints
    reading some converters write): a reading that closes the
    outline wins; conflicting certain verdicts abstain rather
    than guess (RT-04)."""
    if not edge_elems:
        return None, [], False, None
    if len(edge_elems) > MAX_EDGE_ELEMS:
        return None, [], True, None
    has_legacy = any(elem.kind == "arc" and elem.angle is not None
                     for elem in edge_elems)
    if not has_legacy:
        closed, points, abstained = _outline_eval(edge_elems, 1)
        return closed, points, abstained, None
    # Legacy (angle ...) arcs: evaluate every reading — centre
    # semantics under both sweep signs, and the endpoints
    # reading. A reading that closes the outline wins; the
    # documented centre semantics is preferred on a tie (its
    # centrelines can be rebuilt for the copper-to-edge pass,
    # while an endpoints-reading arc stays chain-only — its
    # bulge side is unknowable from the file). Conflicting
    # certain verdicts across readings abstain rather than guess.
    plus = _outline_eval(edge_elems, 1)
    minus = _outline_eval(edge_elems, -1)
    ends = _outline_eval(edge_elems, "ends")
    if plus[0] is True and minus[0] is not True:
        return plus[0], plus[1], plus[2], 1
    if minus[0] is True and plus[0] is not True:
        return minus[0], minus[1], minus[2], -1
    if plus[0] is True and minus[0] is True:
        return True, [], False, 1
    if ends[0] is True:
        return True, [], False, None
    verdicts = {plus[0], minus[0], ends[0]}
    if verdicts == {False}:
        return False, plus[1], plus[2], 1
    if False in verdicts:
        return None, [], True, None
    return plus[0], plus[1], plus[2], 1


def _fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".") + " mm"


def _net_label(obj):
    if obj.net_key is None:
        return "no net stated"
    name = _short(obj.net_name, 24) if obj.net_name else "unnamed net"
    return f"net {name}"


def run_geometry_checks(project):
    """Run the geometry-lite layer. Returns (findings, stats dict).

    Runs only when a PCB was loaded and parsed; all abstentions are
    expressed in the returned stats for the report's Coverage."""
    findings = []
    stats = {}
    if project.pcb_path is None or project.pcb_failed:
        return findings, stats
    geo = project.geometry
    rules = project.board_rules or {}
    pcb_name = project.pcb_path.name
    # Outline closure runs FIRST: it does not depend on the copper
    # objects, so the object budget must not suppress a broken
    # outline's BLOCK (v0.4 review RT-13). The sign it settles on
    # for legacy arcs feeds the centreline rebuild in
    # _build_objects.
    closed, open_points, abstained, legacy_sign = \
        _outline_state(geo.edge_elems)
    objects, outline, total_objects, total_pieces = _build_objects(
        project, legacy_sign if closed is not None else None)
    zone_polys, zone_stats = _build_zone_polys(project)
    gstats = _Stats(objects=total_objects,
                    outline_elements=len(geo.edge_elems))

    skipped_note = (
        f"{geo.pads_unplaced + geo.items_unplaced} geometric "
        "object(s) had unreadable coordinates and were excluded "
        "from the geometry-lite distance checks")
    stats.update({
        "objects": total_objects,
        "pads_modelled": len(geo.pads),
        "pads_shape_skipped": geo.pads_shape_skipped,
        "pads_non_copper": geo.pads_non_copper,
        "objects_unplaced": geo.pads_unplaced + geo.items_unplaced,
        # v0.6: zones_excluded now counts only zones the zone pass
        # could NOT model (no readable filled polygon) — it was
        # the total zone count in v0.4/v0.5, when every zone was
        # excluded. See zones_modelled / zones_polygons.
        "zones_excluded": zone_stats["zones_abstained"],
        "zones_total_geo": zone_stats["zones_total_geo"],
        "zones_modelled": zone_stats["zones_modelled"],
        "zones_abstained": zone_stats["zones_abstained"],
        "zones_polygons": zone_stats["zones_polygons"],
        "zones_edges": zone_stats["zones_edges"],
        "zones_fill_unreadable": zone_stats["zones_fill_unreadable"],
        "outline_elements": len(geo.edge_elems),
        "outline_closed": closed,
        "outline_abstained": abstained,
    })
    gstats.outline_closed = closed
    if closed is False:
        where_pts = "; ".join(
            f"({x:.2f}, {y:.2f})" for x, y in open_points[:4])
        findings.append(Finding(
            "BOARD_OUTLINE_OPEN", BLOCK,
            "The board outline on Edge.Cuts does not close: "
            f"{len(open_points)} outline endpoint(s) have no "
            f"matching continuation ({where_pts}). An open outline "
            "cannot be cut as drawn — both JLCPCB and PCBWay hold "
            "orders over outline problems. Join the outline into a "
            "closed loop (open chains fully inside a closed loop "
            "are treated as routed slots and never trigger this).",
            where=pcb_name,
            evidence=f"open_endpoints={len(open_points)}"))
    if total_objects > MAX_OBJECTS:
        findings.append(Finding(
            "GEOMETRY_SKIPPED", CAUTION,
            f"The board carries {total_objects} copper objects, over "
            f"the geometry-lite budget of {MAX_OBJECTS}, so the "
            "clearance and copper-to-edge checks did not run "
            "(outline closure above is judged separately — it does "
            "not depend on the copper objects). KiCad's own DRC "
            "covers this board fully; the other Preflight checks "
            "are unaffected.",
            where=pcb_name,
            evidence=f"objects={total_objects} budget={MAX_OBJECTS}"))
        stats["budget_exceeded"] = True
        return findings, stats

    # --- copper clearance --------------------------------------------------
    violations = _clearance_pass(objects, rules, gstats)
    # Zone pass (v0.6): filled-zone polygons against the same
    # thresholds, merged into the same finding — a pair counts
    # once whether it is pad/track/via or zone copper.
    zone_budget = False
    if not gstats.budget_exceeded:
        zviol, zcounters, zone_budget = _zone_clearance_pass(
            zone_polys, objects, rules)
        stats["zones_pairs_checked"] = zcounters["pairs_checked"]
        stats["zones_pairs_unknown_net"] = \
            zcounters["pairs_unknown_net"]
        stats["zones_candidate_checks"] = \
            zcounters["candidate_checks"]
        stats["zones_padstack_pairs_skipped"] = \
            zcounters["padstack_pairs_skipped"]
        if zcounters["containment_abstained"]:
            stats["zones_containment_abstained"] = True
        if zone_budget:
            stats["zones_budget_exceeded"] = True
        else:
            stats["zone_clearance_violations"] = len(zviol)
            violations = violations + zviol
    stats["pairs_checked"] = gstats.pairs_checked
    stats["pairs_unknown_net"] = gstats.pairs_unknown_net
    if gstats.clearance_threshold is not None:
        stats.setdefault("clearance_threshold", gstats.clearance_threshold)
        stats.setdefault("clearance_source", gstats.clearance_source)
    if gstats.budget_exceeded:
        findings.append(Finding(
            "GEOMETRY_SKIPPED", CAUTION,
            "The geometry-lite clearance check exceeded its "
            "computation budget (candidate-pair enumeration or "
            f"{MAX_PAIR_CHECKS} pair measurements) and was "
            "abandoned rather than reported partially. KiCad's "
            "own DRC covers this board fully.",
            where=pcb_name))
        stats["budget_exceeded"] = True
    elif violations:
        violations.sort(key=lambda v: (v[0] - v[4], v[0],
                                       v[1].desc, v[2].desc))
        worst = violations[0]
        lower, a, b, layer, thr, source = worst
        findings.append(Finding(
            "GEOMETRY_CLEARANCE_BELOW_MIN", CAUTION,
            f"{len(violations)} different-net copper pair(s) on "
            f"the same layer sit closer than the applicable "
            f"clearance — worst: {a.desc} ({_net_label(a)}) to "
            f"{b.desc} ({_net_label(b)}) on {_short(layer, 16)} at "
            f"{_fmt(max(lower, 0.0))} apart, below the "
            f"{_fmt(thr)} minimum from the {source}. Geometry-lite "
            "subset: pads, tracks, arcs, vias and filled zone "
            "polygons — custom/trapezoid pads and footprint "
            "graphics are not measured, and this is not a "
            "replacement for KiCad DRC.",
            where=pcb_name,
            evidence=f"pairs_below_min={len(violations)} "
                     f"worst={max(lower, 0.0):.3f} "
                     f"threshold={thr:.3f} source={source}"))
        stats["clearance_violations"] = len(violations)
        stats["clearance_threshold"] = thr
        stats["clearance_source"] = source
    if zone_budget:
        findings.append(Finding(
            "GEOMETRY_SKIPPED", CAUTION,
            "The geometry-lite zone check exceeded its computation "
            "budget (filled-polygon edges, candidate enumeration "
            f"or {MAX_ZONE_PAIR_CHECKS} pair measurements) and "
            "was abandoned rather than reported partially — zone "
            "clearance and zone copper-to-edge did not run. The "
            "pad/track/arc/via checks above are unaffected. "
            "KiCad's own DRC covers this board fully.",
            where=pcb_name,
            evidence=f"zone_edges={zone_stats['zones_edges']} "
                     f"budget_edges={MAX_ZONE_EDGES}"))

    # --- copper-to-edge ----------------------------------------------------
    if total_pieces > MAX_OUTLINE_PIECES:
        stats["edge_abstained"] = True
        outline = []
    if outline:
        thr, source = _edge_threshold(rules)
        stats["edge_threshold"] = thr
        stats["edge_source"] = source
        # Grid-index the outline pieces so a dense outline stays
        # fast. Pieces spanning a huge number of cells are not
        # inserted cell-wise (RT-01's shape, on the outline side):
        # they join a wide list tested against every object.
        cell = 2.0
        grid = {}
        wide_pieces = []
        for pidx, (x1, y1, x2, y2, unc) in enumerate(outline):
            nx = int(max(x1, x2) // cell) \
                - int(min(x1, x2) // cell) + 1
            ny = int(max(y1, y2) // cell) \
                - int(min(y1, y2) // cell) + 1
            if nx * ny > MAX_OBJ_CELLS:
                wide_pieces.append(pidx)
                continue
            for gx in range(int(min(x1, x2) // cell),
                            int(max(x1, x2) // cell) + 1):
                for gy in range(int(min(y1, y2) // cell),
                                int(max(y1, y2) // cell) + 1):
                    grid.setdefault((gx, gy), []).append(pidx)
        if len(wide_pieces) > 512:
            stats["edge_abstained"] = True
            outline = []
    if outline:
        worst = None
        count = 0
        for obj in objects:
            x0, y0, x1, y1 = obj.bbox
            nx = int((x1 + thr) // cell) - int((x0 - thr) // cell) + 1
            ny = int((y1 + thr) // cell) - int((y0 - thr) // cell) + 1
            if nx * ny > 4096:
                # A board-spanning object: test every piece
                # directly instead of walking millions of cells.
                candidates = range(len(outline))
            else:
                found = set(wide_pieces)
                for gx in range(int((x0 - thr) // cell),
                                int((x1 + thr) // cell) + 1):
                    for gy in range(int((y0 - thr) // cell),
                                    int((y1 + thr) // cell) + 1):
                        found.update(grid.get((gx, gy), ()))
                candidates = found
            best = None
            for pidx in sorted(candidates):
                px1, py1, px2, py2, unc = outline[pidx]
                raw = _d_core(obj.core,
                              ("seg", px1, py1, px2, py2)) - obj.radius
                # Certain-violation bound: the true distance is at
                # most raw + BOTH uncertainties (the old code added
                # back only the outline's — arc copper could be
                # flagged up to 2x sagitta early; red-team note).
                upper = raw + obj.unc + unc
                if best is None or upper < best[0]:
                    best = (upper, raw - obj.unc - unc)
            if best is not None and best[0] < thr - MEASURE_TOLERANCE:
                count += 1
                if worst is None or best[1] < worst[0]:
                    worst = (best[1], obj)
        # Zone copper-to-edge (v0.6): each filled polygon counts
        # once at its closest edge, merged into the same finding.
        # Judged only against a board-STATED edge clearance: a
        # zone's inset from the outline is chosen by the filler
        # under the board's own edge rule, so when the project
        # states none, the fab fallback that the object pass uses
        # would flag machine-generated insets KiCad's own DRC
        # accepts (upsy-desky: no stated rule, KiCad reports no
        # copper_edge_clearance, fallback would flag 5 polygons).
        # The abstention is counted, not silent.
        if zone_polys and not zone_budget:
            if rules.get("min_copper_edge_clearance"):
                zcount, zworst = _zone_edge_pass(
                    zone_polys, outline, grid, wide_pieces, cell, thr)
                if zcount:
                    stats["zone_edge_violations"] = zcount
                    count += zcount
                    if worst is None or zworst[0] < worst[0]:
                        worst = zworst
            else:
                stats["zones_edge_abstained"] = len(zone_polys)
        if worst is not None:
            findings.append(Finding(
                "GEOMETRY_COPPER_TO_EDGE", CAUTION,
                f"{count} copper object(s)/filled zone polygon(s) "
                f"sit closer than "
                f"{_fmt(thr)} to the board outline — worst: "
                f"{worst[1].desc} ({_net_label(worst[1])}) at "
                f"{_fmt(max(worst[0], 0.0))} from the edge, below "
                f"the minimum from the {source}. Copper too close "
                "to a routed edge risks exposed copper and "
                "delamination; fabs hold orders over it.",
                where=pcb_name,
                evidence=f"objects_below_min={count} "
                         f"worst={max(worst[0], 0.0):.3f} "
                         f"threshold={thr:.3f} source={source}"))
            stats["edge_violations"] = count
    stats["skipped_note"] = skipped_note \
        if (geo.pads_unplaced + geo.items_unplaced) else ""
    return findings, stats
