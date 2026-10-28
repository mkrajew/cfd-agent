# Flow past a circular cylinder at Re = 100

I would like to compute the time-dependent two-dimensional flow of a fluid past a single circular
cylinder, including the vortex shedding in its wake.

**Geometry.** One circular cylinder of diameter D = 0.1 m, with its axis perpendicular to the flow
plane. The cylinder is isolated in an unbounded stream: there are no channel walls or other bodies,
so the flow far from the cylinder is the undisturbed free stream.

**Fluid.** An incompressible Newtonian fluid with density 1 kg/m^3 and dynamic viscosity
0.001 Pa s.

**Boundary conditions.** The free stream is uniform and parallel to the x axis with speed
U = 1 m/s. The cylinder surface is a no-slip wall. The Reynolds number based on the cylinder
diameter and the free-stream speed is 100, and the flow is laminar.

**What I need.** Both quantities refer to the periodic shedding regime, after the initial transient
has died out.

1. `strouhal`: the Strouhal number of the vortex shedding, St = f D / U, where f is the shedding
   frequency (the frequency of the oscillation of the lift force on the cylinder).
2. `cd_mean`: the time-averaged drag coefficient, Cd = F / (0.5 rho U^2 D), where F is the total
   drag force per unit length of cylinder (pressure plus viscous contributions) in the free-stream
   direction, averaged over a whole number of shedding periods.

Please use the names in backticks for the two results.
