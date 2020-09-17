from collections import defaultdict
from functools import partial, singledispatch

from ctypes import c_void_p
import cgen as c
import numpy as np
import sympy

from devito.core.operator import OperatorCore
from devito.data import FULL
from devito.exceptions import InvalidOperator
from devito.ir.equations import DummyEq
from devito.ir.iet import (Call, Callable, ElementalFunction, Expression,
                           Lambda, List, LocalExpression, Iteration, FindNodes,
                           FindSymbols, MapExprStmts, MapNodes, Transformer,
                           retrieve_iteration_tree, filter_iterations)
from devito.ir.support import Scope
from devito.logger import warning
from devito.mpi.routines import CopyBuffer, SendRecv, HaloUpdate
from devito.passes.clusters import (Lift, cire, cse, eliminate_arrays, extract_increments,
                                    factorize, fuse, optimize_pows)
from devito.passes.iet import (DataManager, Storage, Ompizer, OpenMPIteration,
                               ParallelTree, optimize_halospots, mpiize, hoist_prodders,
                               iet_pass)
from devito.symbolics import Byref, FieldFromComposite, InlineIf
from devito.tools import as_tuple, filter_sorted, split, timed_pass
from devito.types import Dimension, LocalObject, Symbol

__all__ = ['DeviceOpenMPNoopOperator', 'DeviceOpenMPOperator',
           'DeviceOpenMPCustomOperator']


class DeviceOpenMPIteration(OpenMPIteration):

    @classmethod
    def _make_construct(cls, **kwargs):
        return 'omp target teams distribute parallel for'

    @classmethod
    def _make_clauses(cls, **kwargs):
        kwargs['chunk_size'] = False
        return super(DeviceOpenMPIteration, cls)._make_clauses(**kwargs)


class DeviceOmpizer(Ompizer):

    lang = dict(Ompizer.lang)
    lang.update({
        'map-enter-to': lambda i, j:
            c.Pragma('omp target enter data map(to: %s%s)' % (i, j)),
        'map-enter-alloc': lambda i, j:
            c.Pragma('omp target enter data map(alloc: %s%s)' % (i, j)),
        'map-update': lambda i, j:
            c.Pragma('omp target update from(%s%s)' % (i, j)),
        'map-update-host': lambda i, j:
            c.Pragma('omp target update from(%s%s)' % (i, j)),
        'map-release': lambda i, j:
            c.Pragma('omp target exit data map(release: %s%s)' % (i, j)),
        'map-exit-delete': lambda i, j:
            c.Pragma('omp target exit data map(delete: %s%s)' % (i, j)),
    })

    _Iteration = DeviceOpenMPIteration

    def __init__(self, sregistry, options, key=None):
        super().__init__(sregistry, options, key=key)
        self.device_fit = options['device-fit']

    @classmethod
    def _map_data(cls, f):
        if f.is_Array:
            return f.symbolic_shape
        else:
            return tuple(f._C_get_field(FULL, d).size for d in f.dimensions)

    @classmethod
    def _map_to(cls, f):
        return cls.lang['map-enter-to'](f.name, ''.join('[0:%s]' % i
                                                        for i in cls._map_data(f)))

    @classmethod
    def _map_alloc(cls, f):
        return cls.lang['map-enter-alloc'](f.name, ''.join('[0:%s]' % i
                                                           for i in cls._map_data(f)))

    @classmethod
    def _map_present(cls, f):
        raise NotImplementedError

    @classmethod
    def _map_update(cls, f):
        return cls.lang['map-update'](f.name, ''.join('[0:%s]' % i
                                                      for i in cls._map_data(f)))

    @classmethod
    def _map_update_host(cls, f, datamap):
        datasize = cls._map_data(f)
        assert len(datamap) == len(datasize)
        ranges = []
        for i, j in zip(datamap, datasize):
            if i is FULL:
                ranges.append('[0:%s]' % j)
            else:
                ranges.append('[%s]' % i)
        return cls.lang['map-update-host'](f.name, ''.join(ranges))

    @classmethod
    def _map_release(cls, f):
        return cls.lang['map-release'](f.name, ''.join('[0:%s]' % i
                                                       for i in cls._map_data(f)))

    @classmethod
    def _map_delete(cls, f):
        return cls.lang['map-exit-delete'](f.name, ''.join('[0:%s]' % i
                                                           for i in cls._map_data(f)))

    @classmethod
    def _map_pointers(cls, f):
        raise NotImplementedError

    def _make_threaded_prodders(self, partree):
        # no-op for now
        return partree

    def _make_partree(self, candidates, nthreads=None):
        """
        Parallelize the `candidates` Iterations attaching suitable OpenMP pragmas
        for GPU offloading.
        """
        assert candidates
        root = candidates[0]

        # Get the collapsable Iterations
        collapsable = self._find_collapsable(root, candidates)
        ncollapse = 1 + len(collapsable)

        if is_ondevice(root, self.device_fit):
            # The typical case: all accessed Function's are device Function's, that is
            # all Function's are in the device memory. Then we offload the candidate
            # Iterations to the device
            body = self._Iteration(ncollapse=ncollapse, **root.args)
            partree = ParallelTree([], body, nthreads=nthreads)
            collapsed = [partree] + collapsable
            return root, partree, collapsed
        else:
            return root, None, None

    def _make_parregion(self, partree, *args):
        # no-op for now
        return partree

    def _make_guard(self, partree, *args):
        # no-op for now
        return partree

    def _make_nested_partree(self, partree):
        # no-op for now
        return partree


class HostParallelIteration(List):

    # Can be used anywhere an Iteration is expected
    is_Iteration = True

    def __init__(self, body, pragmas=None):
        # `body` may be a tuple of length 1 upon reconstruction
        body = as_tuple(body)
        assert len(body) == 1
        body = body[0]
        if isinstance(body, CallParfor):
            super().__init__(header=pragmas, body=body)
        else:
            assert body.is_Iteration
            super().__init__(header=pragmas, body=CallParfor(body))

    def __getattr__(self, name):
        return getattr(self.iteration, name)

    @property
    def iteration(self):
        return self.body[0].iteration


class HostParallelizer(object):

    def __init__(self, sregistry, options):
        self.sregistry = sregistry
        self.device_fit = options['device-fit']

    @property
    def nthreads(self):
        return self.sregistry.nthreads

    @iet_pass
    def make_parallel(self, iet):
        key = lambda i: i.is_Parallel and not is_ondevice(i, self.device_fit)

        mapper = {}
        for tree in retrieve_iteration_tree(iet):
            candidates = filter_iterations(tree, key=key)
            if not candidates:
                continue
            root = candidates[0]
            if root in mapper:
                continue
            mapper[root] = HostParallelIteration(root)

        if mapper:
            iet = Transformer(mapper, nested=True).visit(iet)
            parfor = make_host_parallel_for(self.nthreads)
            return iet, {'efuncs': [parfor], 'args': [self.nthreads],
                         'includes': ('vector', 'thread', 'algorithm')}
        else:
            return iet, {}


class DeviceOpenMPDataManager(DataManager):

    _Parallelizer = DeviceOmpizer

    def __init__(self, sregistry, options):
        """
        Parameters
        ----------
        sregistry : SymbolRegistry
            The symbol registry, to quickly access the special symbols that may
            appear in the IET (e.g., `sregistry.threadid`, that is the thread
            Dimension, used by the DataManager for parallel memory allocation).
        options : dict
            The optimization options.
            Accepted: ['device-fit'].
            * 'device-fit': an iterable of `Function`s that are guaranteed to fit
              in the device memory. By default, all `Function`s except saved
              `TimeFunction`'s are assumed to fit in the device memory.
        """
        super().__init__(sregistry)
        self.device_fit = options['device-fit']

    def _alloc_array_on_high_bw_mem(self, site, obj, storage):
        _storage = Storage()
        super()._alloc_array_on_high_bw_mem(site, obj, _storage)

        allocs = _storage[site].allocs + [self._Parallelizer._map_alloc(obj)]
        frees = [self._Parallelizer._map_delete(obj)] + _storage[site].frees
        storage.update(obj, site, allocs=allocs, frees=frees)

    def _map_function_on_high_bw_mem(self, site, obj, storage, read_only=False):
        """
        Place a Function in the high bandwidth memory.
        """
        alloc = self._Parallelizer._map_to(obj)

        if read_only is False:
            free = c.Collection([self._Parallelizer._map_update(obj),
                                 self._Parallelizer._map_release(obj)])
        else:
            free = self._Parallelizer._map_delete(obj)

        storage.update(obj, site, allocs=alloc, frees=free)

    @iet_pass
    def place_ondevice(self, iet):

        @singledispatch
        def _place_ondevice(iet):
            return iet

        @_place_ondevice.register(Callable)
        def _(iet):
            # Collect written and read-only symbols
            writes = set()
            reads = set()
            for i, v in MapExprStmts().visit(iet).items():
                if not i.is_Expression:
                    # No-op
                    continue
                if not any(isinstance(j, self._Parallelizer._Iteration) for j in v):
                    # Not an offloaded Iteration tree
                    continue
                if i.write.is_DiscreteFunction:
                    writes.add(i.write)
                reads = (reads | {r for r in i.reads if r.is_DiscreteFunction}) - writes

            # Populate `storage`
            storage = Storage()
            for i in filter_sorted(writes):
                if is_ondevice(i, self.device_fit):
                    self._map_function_on_high_bw_mem(iet, i, storage)
            for i in filter_sorted(reads):
                if is_ondevice(i, self.device_fit):
                    self._map_function_on_high_bw_mem(iet, i, storage, read_only=True)

            iet = self._dump_storage(iet, storage)

            return iet

        @_place_ondevice.register(ElementalFunction)
        def _(iet):
            return iet

        @_place_ondevice.register(CopyBuffer)
        @_place_ondevice.register(SendRecv)
        @_place_ondevice.register(HaloUpdate)
        def _(iet):
            return iet

        iet = _place_ondevice(iet)

        return iet, {}

    @iet_pass
    def place_onhost(self, iet):
        # Analysis
        mapper = defaultdict(set)
        visitor = MapNodes(Iteration, HostParallelIteration, 'immediate')
        for k, v in visitor.visit(iet).items():
            #TODO: Check with test where len(ih) > 1

            scope = Scope([e.expr for e in FindNodes(Expression).visit(k)])
            for i in v:
                ondevice, onhost = split(FindSymbols('indexeds').visit(i),
                                         lambda i: is_ondevice(i, self.device_fit))
                for indexed in ondevice:
                    f = indexed.function
                    assert i.dim in f.dimensions
                    if f.is_DiscreteFunction and f in scope.writes:
                        n = f.dimensions.index(i.dim)
                        datamap = [idx for idx in indexed.indices[:n]]
                        datamap.extend([FULL for _ in indexed.indices[n:]])
                        mapper[i].add((f, tuple(datamap)))

        # Post-process analysis
        for k, v in list(mapper.items()):
            onhost = [self._Parallelizer._map_update_host(f, dm) for f, dm in v]
            mapper[k] = k._rebuild(pragmas=onhost)

        # Transform the IET adding pragmas triggering a copy of the written data
        # back to the host
        iet = Transformer(mapper, nested=True).visit(iet)

        return iet, {}


class DeviceOpenMPNoopOperator(OperatorCore):

    CIRE_REPEATS_INV = 2
    """
    Number of CIRE passes to detect and optimize away Dimension-invariant expressions.
    """

    CIRE_REPEATS_SOPS = 5
    """
    Number of CIRE passes to detect and optimize away redundant sum-of-products.
    """

    CIRE_MINCOST_INV = 50
    """
    Minimum operation count of a Dimension-invariant aliasing expression to be
    optimized away. Dimension-invariant aliases are lifted outside of one or more
    invariant loop(s), so they require tensor temporaries that can be potentially
    very large (e.g., the whole domain in the case of time-invariant aliases).
    """

    CIRE_MINCOST_SOPS = 10
    """
    Minimum operation count of a sum-of-product aliasing expression to be optimized away.
    """

    PAR_CHUNK_NONAFFINE = 3
    """
    Coefficient to adjust the chunk size in non-affine parallel loops.
    """

    @classmethod
    def _normalize_kwargs(cls, **kwargs):
        o = {}
        oo = kwargs['options']

        # Execution modes
        o['mpi'] = oo.pop('mpi')

        # Strictly unneccesary, but make it clear that this Operator *will*
        # generate OpenMP code, bypassing any `openmp=False` provided in
        # input to Operator
        oo.pop('openmp')

        # CIRE
        o['min-storage'] = False
        o['cire-rotate'] = False
        o['cire-onstack'] = False
        o['cire-maxpar'] = oo.pop('cire-maxpar', True)
        o['cire-repeats'] = {
            'invariants': oo.pop('cire-repeats-inv', cls.CIRE_REPEATS_INV),
            'sops': oo.pop('cire-repeats-sops', cls.CIRE_REPEATS_SOPS)
        }
        o['cire-mincost'] = {
            'invariants': oo.pop('cire-mincost-inv', cls.CIRE_MINCOST_INV),
            'sops': oo.pop('cire-mincost-sops', cls.CIRE_MINCOST_SOPS)
        }

        # GPU parallelism
        o['par-collapse-ncores'] = 1  # Always use a collapse clause
        o['par-collapse-work'] = 1  # Always use a collapse clause
        o['par-chunk-nonaffine'] = oo.pop('par-chunk-nonaffine', cls.PAR_CHUNK_NONAFFINE)
        o['par-dynamic-work'] = np.inf  # Always use static scheduling
        o['par-nested'] = np.inf  # Never use nested parallelism

        # GPU data
        o['device-fit'] = as_tuple(oo.pop('device-fit', None))

        if oo:
            raise InvalidOperator("Unsupported optimization options: [%s]"
                                  % ", ".join(list(oo)))

        kwargs['options'].update(o)

        return kwargs

    @classmethod
    @timed_pass(name='specializing.Clusters')
    def _specialize_clusters(cls, clusters, **kwargs):
        options = kwargs['options']
        platform = kwargs['platform']
        sregistry = kwargs['sregistry']

        # Toposort+Fusion (the former to expose more fusion opportunities)
        clusters = fuse(clusters, toposort=True)

        # Hoist and optimize Dimension-invariant sub-expressions
        clusters = cire(clusters, 'invariants', sregistry, options, platform)
        clusters = Lift().process(clusters)

        # Reduce flops (potential arithmetic alterations)
        clusters = extract_increments(clusters, sregistry)
        clusters = cire(clusters, 'sops', sregistry, options, platform)
        clusters = factorize(clusters)
        clusters = optimize_pows(clusters)

        # Reduce flops (no arithmetic alterations)
        clusters = cse(clusters, sregistry)

        # Lifting may create fusion opportunities, which in turn may enable
        # further optimizations
        clusters = fuse(clusters)
        clusters = eliminate_arrays(clusters)

        return clusters

    @classmethod
    @timed_pass(name='specializing.IET')
    def _specialize_iet(cls, graph, **kwargs):
        options = kwargs['options']
        sregistry = kwargs['sregistry']

        # Distributed-memory parallelism
        if options['mpi']:
            mpiize(graph, mode=options['mpi'])

        # GPU parallelism via OpenMP offloading
        DeviceOmpizer(sregistry, options).make_parallel(graph)

        # Host parallelism via C++ parallel loops
        HostParallelizer(sregistry, options).make_parallel(graph)

        # Symbol definitions
        data_manager = DeviceOpenMPDataManager(sregistry, options)
        data_manager.place_ondevice(graph)
        data_manager.place_onhost(graph)
        data_manager.place_definitions(graph)
        data_manager.place_casts(graph)

        return graph


class DeviceOpenMPOperator(DeviceOpenMPNoopOperator):

    @classmethod
    @timed_pass(name='specializing.IET')
    def _specialize_iet(cls, graph, **kwargs):
        options = kwargs['options']
        sregistry = kwargs['sregistry']

        # Distributed-memory parallelism
        optimize_halospots(graph)
        if options['mpi']:
            mpiize(graph, mode=options['mpi'])

        # GPU parallelism via OpenMP offloading
        DeviceOmpizer(sregistry, options).make_parallel(graph)

        # Host parallelism via C++ parallel loops
        HostParallelizer(sregistry, options).make_parallel(graph)

        # Misc optimizations
        hoist_prodders(graph)

        # Symbol definitions
        data_manager = DeviceOpenMPDataManager(sregistry, options)
        data_manager.place_ondevice(graph)
        data_manager.place_onhost(graph)
        data_manager.place_definitions(graph)
        data_manager.place_casts(graph)

        return graph


class DeviceOpenMPCustomOperator(DeviceOpenMPOperator):

    _known_passes = ('optcomms', 'openmp', 'c++par', 'mpi', 'prodders')
    _known_passes_disabled = ('blocking', 'denormals', 'simd')
    assert not (set(_known_passes) & set(_known_passes_disabled))

    @classmethod
    def _make_passes_mapper(cls, **kwargs):
        options = kwargs['options']
        sregistry = kwargs['sregistry']

        ompizer = DeviceOmpizer(sregistry, options)
        parizer = HostParallelizer(sregistry, options)

        return {
            'optcomms': partial(optimize_halospots),
            'openmp': partial(ompizer.make_parallel),
            'c++par': partial(parizer.make_parallel),
            'mpi': partial(mpiize, mode=options['mpi']),
            'prodders': partial(hoist_prodders)
        }

    @classmethod
    def _build(cls, expressions, **kwargs):
        # Sanity check
        passes = as_tuple(kwargs['mode'])
        for i in passes:
            if i not in cls._known_passes:
                if i in cls._known_passes_disabled:
                    warning("Got explicit pass `%s`, but it's unsupported on an "
                            "Operator of type `%s`" % (i, str(cls)))
                else:
                    raise InvalidOperator("Unknown pass `%s`" % i)

        return super(DeviceOpenMPCustomOperator, cls)._build(expressions, **kwargs)

    @classmethod
    @timed_pass(name='specializing.IET')
    def _specialize_iet(cls, graph, **kwargs):
        options = kwargs['options']
        sregistry = kwargs['sregistry']
        passes = as_tuple(kwargs['mode'])

        # Fetch passes to be called
        passes_mapper = cls._make_passes_mapper(**kwargs)

        # Call passes
        for i in passes:
            try:
                passes_mapper[i](graph)
            except KeyError:
                pass

        # Force-call `mpi` if requested via global option
        if 'mpi' not in passes and options['mpi']:
            passes_mapper['mpi'](graph)

        # GPU parallelism via OpenMP offloading
        if 'openmp' not in passes:
            passes_mapper['openmp'](graph)

        # Symbol definitions
        data_manager = DeviceOpenMPDataManager(sregistry, options)
        data_manager.place_ondevice(graph)
        data_manager.place_onhost(graph)
        data_manager.place_definitions(graph)
        data_manager.place_casts(graph)

        return graph


# Utils

def is_ondevice(maybe_symbol, device_fit):
    """
    True if all functions are allocated in the device memory, False otherwise.
    """
    # `maybe_symbol` may be an Indexed, a Function, or even an actual piece of IET
    try:
        functions = (maybe_symbol.function,)
    except AttributeError:
        assert maybe_symbol.is_Node
        functions = FindSymbols().visit(maybe_symbol)

    return all(not (f.is_TimeFunction and f.save is not None and f not in device_fit)
               for f in functions)


class STDVectorThreads(LocalObject):
    dtype = type('std::vector<std::thread>', (c_void_p,), {})

    def __init__(self):
        self.name = 'threads'

    # Pickling support
    _pickle_args = []


class STDThread(LocalObject):
    dtype = type('std::thread&', (c_void_p,), {})

    def __init__(self, name):
        self.name = name

    # Pickling support
    _pickle_args = ['name']


class FunctionType(LocalObject):
    dtype = type('Function_type&&', (c_void_p,), {})

    def __init__(self, name):
        self.name = name

    # Pickling support
    _pickle_args = ['name']


def make_host_parallel_for(nthreads):
    """
    Generate an IET implementing a parallel-for via C++14 threads.
    """
    # Basic symbols
    threshold = Symbol(name='threshold', is_const=True)
    last = Symbol(name='last', is_const=True)
    first = Symbol(name='first', is_const=True)
    portion = Symbol(name='portion', is_const=True)

    # Composite symbols
    threads = STDVectorThreads()

    # Iteration helper symbols
    begin = Symbol(name='begin')
    l = Symbol(name='l')
    end = Symbol(name='end')

    # Functions
    stdmax = sympy.Function('std::max')

    # Construct the parallel-for body
    func = FunctionType('func')
    i = Dimension(name='i')
    threadobj = Call('std::thread', Lambda(
        Iteration(Call(func.name, i), i, (begin, end-1, 1)),
        ['=', Byref(func.name)],
    ))
    threadpush = Call(FieldFromComposite('push_back', threads), threadobj)
    it = Dimension(name='it')
    iteration = Iteration([
        LocalExpression(DummyEq(begin, it)),
        LocalExpression(DummyEq(l, it + portion)),
        LocalExpression(DummyEq(end, InlineIf(l > last, last, l))),
        threadpush
    ], it, (first, last, portion))
    thread = STDThread('x')
    waitcall = Call('std::for_each', [
        Call(FieldFromComposite('begin', threads)),
        Call(FieldFromComposite('end', threads)),
        Lambda(Call(FieldFromComposite('join', thread.name)), [], [thread])
    ])
    body = [
        LocalExpression(DummyEq(portion, stdmax(threshold, (last - first) / nthreads))),
        LocalExpression(DummyEq(threshold, 1)),
        Call(FieldFromComposite('reserve', threads), nthreads),
        iteration,
        waitcall
    ]

    parameters = [first, last, func, nthreads]
    parfor = ElementalFunction('parallel_for', body, 'void', parameters)

    return parfor


class CallParfor(Call):

    def __init__(self, iteration):
        super().__init__('parallel_for', [
            iteration.symbolic_min,
            iteration.symbolic_max,
            Lambda(iteration.nodes, ['='], [iteration.dim])
        ])
        self.iteration = iteration
