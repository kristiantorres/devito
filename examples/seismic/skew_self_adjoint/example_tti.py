import numpy as np
from sympy import sqrt, sin, cos

from devito import (Grid, Function, TimeFunction, Eq, Operator)
from examples.seismic import RickerSource, TimeAxis

space_order = 8
dtype = np.float32
npad = 20
qmin = 0.1
qmax = 1000.0
tmax = 250.0
fpeak = 0.010
omega = 2.0 * np.pi * fpeak

shape = (1201, 1201, 601)
spacing = (10.0, 10.0, 10.0)
origin = tuple([0.0 for s in shape])
extent = tuple([d * (s - 1) for s, d in zip(shape, spacing)])
grid = Grid(extent=extent, shape=shape, origin=origin, dtype=dtype)

b = Function(name='b', grid=grid, space_order=space_order)
f = Function(name='f', grid=grid, space_order=space_order)
phi0 = Function(name='phi', grid=grid, space_order=space_order)
theta0 = Function(name='theta', grid=grid, space_order=space_order)
vel0 = Function(name='vel0', grid=grid, space_order=space_order)
eps0 = Function(name='eps0', grid=vel0.grid, space_order=space_order)
eta0 = Function(name='eta0', grid=vel0.grid, space_order=space_order)
wOverQ = Function(name='wOverQ', grid=vel0.grid, space_order=space_order)

_b = 1.0
_f = 0.84
_eps = 0.2
_eta = 0.4
_phi = np.pi / 3
_theta = np.pi / 6

b.data[:] = _b
f.data[:] = _f
vel0.data[:] = 1.5
eps0.data[:] = _eps
eta0.data[:] = _eta
phi0.data[:] = _phi
theta0.data[:] = _theta
wOverQ.data[:] = 1.0

t0 = 0.0
t1 = 250.0
dt = 1.0
time_axis = TimeAxis(start=t0, stop=t1, step=dt)

p0 = TimeFunction(name='p0', grid=grid, time_order=2, space_order=space_order)
m0 = TimeFunction(name='m0', grid=grid, time_order=2, space_order=space_order)
t, x, y, z = p0.dimensions

src_coords = np.empty((1, len(shape)), dtype=dtype)
src_coords[0, :] = [d * (s-1)//2 for d, s in zip(spacing, shape)]
src = RickerSource(name='src', grid=vel0.grid, f0=fpeak, npoint=1, time_range=time_axis)
src.coordinates.data[:] = src_coords[:]
src_term = src.inject(field=p0.forward, expr=src * t.spacing**2 * vel0**2 / b)


def g1(field, phi, theta):
    return (cos(theta) * cos(phi) * field.dx(x0=x+x.spacing/2) +
            cos(theta) * sin(phi) * field.dy(x0=y+y.spacing/2) -
            sin(theta) * field.dz(x0=z+z.spacing/2))


def g2(field, phi, theta):
    return - (sin(phi) * field.dx(x0=x+x.spacing/2) -
              cos(phi) * field.dy(x0=y+y.spacing/2))


def g3(field, phi, theta):
    return (sin(theta) * cos(phi) * field.dx(x0=x+x.spacing/2) +
            sin(theta) * sin(phi) * field.dy(x0=y+y.spacing/2) +
            cos(theta) * field.dz(x0=z+z.spacing/2))


def g1_tilde(field, phi, theta):
    return ((cos(theta) * cos(phi) * field).dx(x0=x-x.spacing/2) +
            (cos(theta) * sin(phi) * field).dy(x0=y-y.spacing/2) -
            (sin(theta) * field).dz(x0=z-z.spacing/2))


def g2_tilde(field, phi, theta):
    return - ((sin(phi) * field).dx(x0=x-x.spacing/2) -
              (cos(phi) * field).dy(x0=y-y.spacing/2))


def g3_tilde(field, phi, theta):
    return ((sin(theta) * cos(phi) * field).dx(x0=x-x.spacing/2) +
            (sin(theta) * sin(phi) * field).dy(x0=y-y.spacing/2) +
            (cos(theta) * field).dz(x0=z-z.spacing/2))


# Time update equation for quasi-P state variable p
update_p_nl = t.spacing**2 * vel0**2 / b * \
    (g1_tilde(b * (1 + 2 * eps0) * g1(p0, phi0, theta0), phi0, theta0) +
     g2_tilde(b * (1 + 2 * eps0) * g2(p0, phi0, theta0), phi0, theta0) +
     g3_tilde(b * (1 - f * eta0**2) * g3(p0, phi0, theta0) +
              b * f * eta0 * sqrt(1 - eta0**2) * g3(m0, phi0, theta0), phi0, theta0)) + \
    (2 - t.spacing * wOverQ) * p0 + \
    (t.spacing * wOverQ - 1) * p0.backward

# Time update equation for quasi-S state variable m
update_m_nl = t.spacing**2 * vel0**2 / b * \
    (g1_tilde(b * (1 - f) * g1(m0, phi0, theta0), phi0, theta0) +
     g2_tilde(b * (1 - f) * g2(m0, phi0, theta0), phi0, theta0) +
     g3_tilde(b * (1 - f + f * eta0**2) * g3(m0, phi0, theta0) +
              b * f * eta0 * sqrt(1 - eta0**2) * g3(p0, phi0, theta0), phi0, theta0)) + \
    (2 - t.spacing * wOverQ) * m0 + \
    (t.spacing * wOverQ - 1) * m0.backward

stencil_p_nl = Eq(p0.forward, update_p_nl)
stencil_m_nl = Eq(m0.forward, update_m_nl)

dt = time_axis.step
spacing_map = vel0.grid.spacing_map
spacing_map.update({t.spacing: dt})

op = Operator([stencil_p_nl, stencil_m_nl, src_term],
              subs=spacing_map, name='OpExampleTti')

f = open("operator.tti.c", "w")
print(op, file=f)
f.close()

bx = 8
by = 8
op.apply(x0_blk0_size=bx, y0_blk0_size=by)
