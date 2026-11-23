# CadQuery for CFD fluid domains

Scope: CadQuery 2.x (`import cadquery as cq`, Workplane API) used to build the fluid domain as
one solid and export it as STEP for the mesher. Not covered: assemblies, sketches, lofts.

## Rules

1. The fluid domain is one solid. Build the outer shape, then `cut` the bodies that sit in the
   flow, so that their surfaces become boundary faces. Check `solid.solids().size() == 1` and
   `solid.val().isValid()`; if the cut leaves several solids, fix the construction instead of
   exporting them.
2. Coordinates are plain numbers; use metres for every number. Keep each dimension in a named
   variable and use the same variable for the model and for later face selection.
3. `cq.Workplane("XY")` draws in the x-y plane and `extrude(h)` goes along +z from the
   workplane. For a start plane other than z = 0 use `.workplane(offset=zmin)`.
   `extrude(h, both=True)` goes `h` in both directions (z from -h to +h).
4. Centering: `rect(w, h)` and `box(l, w, h)` are centred on the current point or the origin by
   default. Pass `centered=False` to place the corner at the point. `center(x, y)` moves the
   local origin of the workplane, so later `rect(...)` and `circle(...)` start there.
5. A circular obstacle: `Workplane("XY").workplane(offset=-margin).center(x, y).circle(r)
   .extrude(thickness + 2 * margin)`. The tool reaches beyond both faces it cuts, so that
   it opens the hole through the full thickness and leaves no coplanar faces.
6. Booleans: `domain = domain.cut(tool)`, `a.union(b)`, `a.intersect(b)`. `cut` takes a
   Workplane or a Shape. Fuse only shapes that overlap or share a face; the result of
   `union` of two touching boxes is one solid (checked), not two.
7. Profiles: `moveTo(x, y).lineTo(x, y).threePointArc(mid, end).close()` builds a closed
   outline for `extrude` or `revolve`. `polyline([...]).close()`, `hLine`, `vLine`,
   `ellipse`, `polygon`, `sphere(r)` and `box` exist too.
8. Revolve: `revolve(angleDegrees, axisStart, axisEnd)`; the axis points are in the local
   coordinates of the workplane, relative to its origin. Do not call `center(...)` before a
   revolve about a global axis, because the axis moves with the origin (checked: the revolved
   solid sits on the wrong axis). Draw the profile with absolute points (`moveTo`, `lineTo`)
   instead, and rotate the result with `.rotate((0, 0, 0), (1, 0, 0), angle_degrees)`.
9. Measuring: `bb = solid.val().BoundingBox()` gives `xmin, xmax, ymin, ymax, zmin, zmax`;
   `solid.val().Volume()`; `solid.faces().size()`; `solid.faces("<Z").val().Center()` and
   `Area()` for one face. Selectors: `"<Z"` lowest z, `">X"` highest x, `"|Z"` faces whose
   normal is along z.
   Counts on the shape are `len(solid.val().Solids())` and `len(solid.val().Faces())`: these
   methods return Python lists. `.size()` is a Workplane method (`solid.solids().size()`).
   Print the bounding box, volume and counts so that a failed check can be diagnosed.
10. Export: `cq.exporters.export(solid, "geometry.step")` (a Workplane or a Shape). The STEP
    file is labelled as millimetres, but the numbers are written unchanged. With default Gmsh
    options the mesher reads the same numbers back (a length written as 2.0 is 2.0 in Gmsh),
    so metres in the script are metres in the mesh. Do not scale, and do not set
    `Geometry.OCCTargetUnit` in Gmsh (it rescales by 1e-3).
11. Boundary faces are identified later by position, because STEP drops face names. Know
    which faces exist: a boundary face of the domain is a plane at a coordinate value, or a
    curved face described by its bounding box.

## Snippet: 2D domain with a hole, extruded one layer thick

```python
import cadquery as cq

domain = cq.Workplane("XY").workplane(offset=zmin).center(x0, y0).rect(width, height,
                                                                        centered=False).extrude(thickness)
hole = (cq.Workplane("XY").workplane(offset=zmin - margin).center(xc, yc)
        .circle(radius).extrude(thickness + 2 * margin))
solid = domain.cut(hole)
bb = solid.val().BoundingBox()
print(bb.xmin, bb.xmax, bb.ymin, bb.ymax, bb.zmin, bb.zmax, solid.val().Volume(),
      solid.solids().size(), solid.faces().size())
cq.exporters.export(solid, "geometry.step")
```

## Common mistakes

- `box(...)` and `rect(...)` centred on the origin when the corner was meant: bounding box off
  by half a size.
- `center(x, y)` is cumulative in a chain: a second `center` shifts again from the first.
- Obstacle tool that stops short of a face it should cut through: the hole does not open and
  the obstacle is not a boundary face of the domain. Always count solids and faces.
- `revolve` with `center(...)` before it: wrong axis (see rule 8).
- Treating `solid.val()` as the whole model when the Workplane holds several solids: it is the
  first object only; check `solids().size()`.
- `solid.val().Faces().size()` or `.Solids().size()`: `AttributeError`, because `Faces()` and
  `Solids()` of a shape are lists, and `.size()` is a Workplane method. Use `len(...)` on the
  shape or `solid.faces().size()` on the Workplane.
- Converting units twice (script scales by 1000 or 0.001, then the mesher scales again).
