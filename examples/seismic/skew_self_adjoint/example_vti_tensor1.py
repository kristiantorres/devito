import numpy as np
import sympy
from sympy import sqrt, sin, cos, Matrix

from devito import (Grid, Function, TimeFunction, Eq, Operator, div, grad, diag)
from devito import VectorFunction, TensorFunction, NODE
from examples.seismic import RickerSource, TimeAxis


### This will have to go in devito but hacked here for now

def grads(func, side="left"):
    shift = 1 if side == "right" else -1
    print(func, shift)
    comps = [getattr(func, 'd%s' % d.name)(x0=d + shift * d.spacing/2)
             for d in func.dimensions if d.is_Space]
    return VectorFunction(name='grad_%s' % func.name, space_order=func.space_order,
                          components=comps, grid=func.grid, staggered=(None, None, None))
            
def divs(func, side="left"):
    shift = 1 if side == "right" else -1
    print(func, shift)
    return sum([getattr(func[i], 'd%s' % d.name)(x0=d + shift * d.spacing/2)
                for i, d in enumerate(func.space_dimensions)])

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
phi = Function(name='phi', grid=grid, space_order=space_order)
theta = Function(name='theta', grid=grid, space_order=space_order)
vel0 = Function(name='vel0', grid=grid, space_order=space_order)
eps0 = Function(name='eps0', grid=vel0.grid, space_order=space_order)
eta0 = Function(name='eta0', grid=vel0.grid, space_order=space_order)
wOverQ = Function(name='wOverQ', grid=vel0.grid, space_order=space_order)

_b = 1.0
_f = 0.84
_eps = 0.2
_eta = 0.4
_phi = np.pi / 10
_theta = np.pi / 6

b._data_with_outhalo[:] = _b
f._data_with_outhalo[:] = _f
vel0._data_with_outhalo[:] = 1.5
eps0._data_with_outhalo[:] = _eps
eta0._data_with_outhalo[:] = _eta
phi._data_with_outhalo[:] = _phi
theta._data_with_outhalo[:] = _theta
wOverQ._data_with_outhalo[:] = 0.0

# theta = phi = 0
t0 = 0.0
t1 = 250.0
dt = 1.0
time_axis = TimeAxis(start=t0, stop=t1, step=dt)
t = grid.time_dim

p0 = TimeFunction(name='p0', grid=grid, time_order=2, space_order=space_order)
m0 = TimeFunction(name='m0', grid=grid, time_order=2, space_order=space_order)

src_coords = np.empty((1, len(shape)), dtype=dtype)
src_coords[0, :] = [d * (s-1)//2 for d, s in zip(spacing, shape)]

src = RickerSource(name='src', grid=vel0.grid, f0=fpeak, npoint=1, time_range=time_axis)
src.coordinates.data[:] = src_coords[:]

src_term = src.inject(field=p0.forward, expr=src * t.spacing**2 * vel0**2 / b)

# Vector for gradients
P_I = VectorFunction(name="P_I", grid=grid, space_order=space_order, staggered=(None, None, None))
M_I = VectorFunction(name="M_I", grid=grid, space_order=space_order, staggered=(None, None, None))

# Rotation matrix
theta = phi = 0
Rt = sympy.rot_axis2(theta)
Rp = sympy.rot_axis3(phi)
R = TensorFunction(name="R", grid=grid, components=Rt * Rp, symmetric=False)

# Functions for additional factorization
b1m2e = Function(name='b1m2e', grid=grid, space_order=space_order)
b1mf = Function(name='b1mf', grid=grid, space_order=space_order)
b1mfa2 = Function(name='b1mfa2', grid=grid, space_order=space_order)
bfas1ma2 = Function(name='bfas1ma2', grid=grid, space_order=space_order)
b1mfpfa2 = Function(name='b1mfpfa2', grid=grid, space_order=space_order)

# Equations for additional factorization
eq_b1m2e = Eq(b1m2e, b * (1 + 2 * eps0))
eq_b1mf = Eq(b1mf, b * (1 - f))
eq_b1mfa2 = Eq(b1mfa2, b * (1 - f * eta0**2))
eq_bfas1ma2 = Eq(bfas1ma2, b * f * eta0 * sqrt(1 - eta0**2))
eq_b1mfpfa2 = Eq(b1mfpfa2, b * (1 - f + f * eta0**2))

# Diagonal matrices
# a_ii = [[b * (1 + 2 * eps0), 0, 0],
#         [0, b * (1 + 2 * eps0), 0],
#         [0, 0, b * (1 - f * eta0**2)]]
# b_ii = [[0, 0, 0],
#         [0, 0, 0],
#         [0, 0, b * f * eta0 * sqrt(1 - eta0**2)]]
# c_ii = [[b * (1 - f), 0, 0],
#         [0, b * (1 - f), 0],
#         [0, 0, b * (1 - f + f * eta0**2)]]

a_ii = [[b1m2e, 0, 0],
        [0, b1m2e, 0],
        [0, 0, b1mfa2]]

b_ii = [[0, 0, 0],
        [0, 0, 0],
        [0, 0, bfas1ma2]]

c_ii = [[b1mf, 0, 0],
        [0, b1mf, 0],
        [0, 0, b1mfpfa2]]

A = TensorFunction(name="A", grid=grid, components=a_ii, diagonal=True)
B = TensorFunction(name="B", grid=grid, components=b_ii, diagonal=True)
C = TensorFunction(name="C", grid=grid, components=c_ii, diagonal=True)

# P_I, M_I
eq_PI = Eq(P_I, R.T * (A * R * grads(p0, side="right") + B * R * grads(m0, side="right")))
eq_MI = Eq(M_I, R.T * (B * R * grads(p0, side="right") + C * R * grads(m0, side="right")))
# Time update equation for quasi-P state variable p
update_p_nl = t.spacing**2 * vel0**2 / b * divs(P_I, side="left") + \
    (2 - t.spacing * wOverQ) * p0 + \
    (t.spacing * wOverQ - 1) * p0.backward

# Time update equation for quasi-S state variable m
update_m_nl = t.spacing**2 * vel0**2 / b * divs(M_I, side="left") + \
    (2 - t.spacing * wOverQ) * m0 + \
    (t.spacing * wOverQ - 1) * m0.backward

stencil_p_nl = Eq(p0.forward, update_p_nl)
stencil_m_nl = Eq(m0.forward, update_m_nl)

dt = time_axis.step
spacing_map = vel0.grid.spacing_map
spacing_map.update({t.spacing: dt})

op = Operator([eq_PI, eq_MI, stencil_p_nl, stencil_m_nl, src_term],
              subs=spacing_map, name='OpExampleVtiTensor')

f = open("operator.vti_tensor.c", "w")
print(op, file=f)
f.close()

bx = 8
by = 8
op.apply(x0_blk0_size=bx, y0_blk0_size=by)
