# Lid-driven cavity flow at Re = 1000

I would like to simulate the flow in a two-dimensional lid-driven square cavity.

**Geometry.** A square cavity of side length L = 1 m.

**Fluid.** An incompressible Newtonian fluid with density 1 kg/m^3 and dynamic viscosity 0.001 Pa s.

**Boundary conditions.** All four walls are no-slip. The top wall (the lid) moves in its own plane in
the +x direction at a constant speed of 1 m/s; the other three walls are at rest. With the lid speed
and the side length as scales, the Reynolds number is 1000. The flow is laminar, and I am interested
in the steady-state solution.

**What I need.** Two velocity profiles, each reported as a list of positions together with a list of
values. Each profile must cover the whole line from wall to wall, including the two end points on
the walls.

1. `u_vertical_centreline`: the x-component of velocity u along the vertical line through the centre
   of the cavity (x = 0.5 m), from the bottom wall (y = 0) to the lid (y = L). Position: y / L.
   Value: u divided by the lid speed.
2. `v_horizontal_centreline`: the y-component of velocity v along the horizontal line through the
   centre of the cavity (y = 0.5 m), from the left wall (x = 0) to the right wall (x = L).
   Position: x / L. Value: v divided by the lid speed.

Please use the names in backticks for the two results.
