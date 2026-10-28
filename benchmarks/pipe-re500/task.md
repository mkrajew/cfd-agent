# Laminar flow in a circular pipe at Re = 500

I would like to simulate water flowing through a straight circular pipe.

**Geometry.** The pipe has an inner diameter D = 10 mm (radius R = 5 mm) and a length of 0.5 m. Let
x be the distance from the inlet along the pipe axis.

**Fluid.** Water, treated as an incompressible Newtonian fluid with density 1000 kg/m^3 and dynamic
viscosity 1.0e-3 Pa s.

**Boundary conditions.** Water enters through the inlet with a uniform axial velocity of 0.05 m/s.
At the outlet the static pressure is fixed at the ambient value (gauge pressure 0). The pipe wall
is no-slip. The Reynolds number based on the diameter and the mean velocity is 500, and the flow
is laminar. I am interested in the steady-state solution.

**What I need.**

1. `u_profile_fully_developed`: the axial velocity profile across the pipe in the fully developed
   part of the flow, taken in the cross-section at x = 0.45 m, from the axis to the wall. Position:
   r / R, with r the distance from the axis (0 on the axis, 1 at the wall). Value: the axial
   velocity divided by the mean velocity of 0.05 m/s. The profile must cover the whole radius,
   including the point on the axis and the point on the wall.
2. `pressure_drop_fully_developed`: the pressure drop, in Pa, between the cross-sections at
   x = 0.35 m and x = 0.45 m, i.e. the area-averaged static pressure at x = 0.35 m minus that at
   x = 0.45 m.

Please use the names in backticks for the two results.
