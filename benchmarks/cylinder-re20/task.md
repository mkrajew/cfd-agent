# Flow past a circular cylinder at Re = 20

I would like to compute the steady two-dimensional flow of a fluid past a single circular
cylinder.

**Geometry.** One circular cylinder of diameter D = 0.1 m, with its axis perpendicular to the flow
plane. The cylinder is isolated in an unbounded stream: there are no channel walls or other bodies,
so the flow far from the cylinder is the undisturbed free stream.

**Fluid.** An incompressible Newtonian fluid with density 1 kg/m^3 and dynamic viscosity
0.005 Pa s.

**Boundary conditions.** The free stream is uniform and parallel to the x axis with speed
U = 1 m/s. The cylinder surface is a no-slip wall. The Reynolds number based on the cylinder
diameter and the free-stream speed is 20, and the flow is laminar.

**What I need.**

1. `cd`: the drag coefficient of the cylinder, Cd = F / (0.5 rho U^2 D), where F is the total drag
   force per unit length of cylinder (pressure plus viscous contributions) in the free-stream
   direction.
2. `recirculation_length_over_d`: the length of the closed recirculation region behind the cylinder,
   measured along the wake centreline from the rear surface of the cylinder to the downstream point
   where the reversed flow ends (where the streamwise velocity on the centreline returns to zero),
   divided by D.

Please use the names in backticks for the two results.
