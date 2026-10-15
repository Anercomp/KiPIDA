"""Reusable SPD sparse solves with warm starts and optional symmetric multigrid."""
import time
import numpy as np
from scipy.sparse import linalg
from threadpoolctl import threadpool_limits


class PreparedReducedSystem:
    def __init__(self, matrix, backend='auto', log=None, preconditioner='auto'):
        if backend not in ('auto', 'cpu', 'gpu') or preconditioner not in ('auto', 'jacobi', 'amg'):
            raise ValueError('Unknown sparse backend or preconditioner.')
        self.matrix = matrix.tocsr()
        self.backend, self.log, self.preconditioner = backend, log, preconditioner
        self.diagonal = self.matrix.diagonal()
        if np.any(~np.isfinite(self.diagonal)) or np.any(self.diagonal <= 0):
            raise ValueError('Reduced conductance has a nonpositive diagonal.')
        self.last = self.factor = self.hierarchy = self.cpu_preconditioner = None
        self.cp = self.gpu_matrix = self.gpu_preconditioner = self.gpu_last = None
        self.gpu_levels = None
        self.calls = 0
        self.notes = []
        self._amg_attempted = False
        self._gpu_failed = False

    def _log(self, text):
        if self.log:
            self.log(text)

    def _amg(self):
        if self._amg_attempted:
            return self.hierarchy
        self._amg_attempted = True
        if self.preconditioner == 'jacobi' or (self.preconditioner == 'auto' and self.matrix.shape[0] < 50000):
            return None
        try:
            import pyamg
            from resource_budget import available_ram
            estimated = 8*(self.matrix.data.nbytes+self.matrix.indices.nbytes+self.matrix.indptr.nbytes)
            if estimated > available_ram()*.5:
                raise MemoryError('AMG setup estimate exceeds half of available RAM.')
            started = time.perf_counter()
            self._log(f'AMG setup: {self.matrix.shape[0]:,} unknowns; building reusable hierarchy.')
            with threadpool_limits(limits=1):
                hierarchy = pyamg.smoothed_aggregation_solver(
                    self.matrix, symmetry='symmetric', max_coarse=64,
                    presmoother=('jacobi', {'iterations': 2}),
                    postsmoother=('jacobi', {'iterations': 2}), coarse_solver='cholesky')
            if hierarchy.levels[-1].A.shape[0] > 256:
                raise ValueError('AMG did not coarsen sufficiently.')
            self.hierarchy = hierarchy
            self._log(f'AMG ready: {len(hierarchy.levels)} levels, operator complexity {hierarchy.operator_complexity():.2f}, setup {time.perf_counter()-started:.2f} s.')
        except InterruptedError:
            raise
        except Exception as exc:
            self.notes.append(f'AMG unavailable: {exc}')
            if self.preconditioner == 'amg':
                raise ValueError(self.notes[-1]) from exc
            self._log(self.notes[-1]+' Using Jacobi.')
        return self.hierarchy

    def _gpu_cycle(self, level, rhs):
        cp = self.cp
        if level == len(self.gpu_levels)-1:
            return self.gpu_coarse_inverse @ rhs
        a, p, r, weight = self.gpu_levels[level]
        x = cp.zeros_like(rhs)
        for _ in range(2):
            x += weight*(rhs-a@x)
        x += p@self._gpu_cycle(level+1, r@(rhs-a@x))
        for _ in range(2):
            x += weight*(rhs-a@x)
        return x

    def _prepare_gpu(self):
        if self.gpu_matrix is not None:
            return
        import cupy as cp
        from cupyx.scipy.sparse import csr_matrix
        from cupyx.scipy.sparse.linalg import LinearOperator
        self.cp = cp
        free, _ = cp.cuda.runtime.memGetInfo()
        n = self.matrix.shape[0]
        required = 3*(self.matrix.data.nbytes+self.matrix.indices.nbytes+self.matrix.indptr.nbytes)+n*128
        if required+256*1024**2 > free*.8:
            raise MemoryError('CUDA workspace exceeds free VRAM budget.')
        self.gpu_matrix = csr_matrix(self.matrix)
        hierarchy = self._amg()
        if hierarchy is not None:
            try:
                required = 0
                for level in hierarchy.levels:
                    for item in (level.A, getattr(level, 'P', None)):
                        if item is not None:
                            required += item.data.nbytes+item.indices.nbytes+item.indptr.nbytes
                    required += level.A.shape[0]*128
                free, _ = cp.cuda.runtime.memGetInfo()
                if required+256*1024**2 > free*.8:
                    raise MemoryError('AMG hierarchy exceeds free VRAM budget.')
                self.gpu_levels = []
                for index, level in enumerate(hierarchy.levels):
                    if index == len(hierarchy.levels)-1:
                        self.gpu_levels.append(None)
                        from scipy.linalg import cho_factor, cho_solve
                        dense = level.A.toarray()
                        inverse = cho_solve(cho_factor(dense), np.eye(len(dense)))
                        self.gpu_coarse_inverse = cp.asarray(inverse)
                    else:
                        a = self.gpu_matrix if index == 0 else csr_matrix(level.A)
                        p = csr_matrix(level.P)
                        # Symmetric weighted-Jacobi V-cycle; the Gershgorin bound
                        # keeps smoothing stable without per-iteration host reads.
                        diagonal = level.A.diagonal()
                        bound = float(np.max(np.asarray(abs(level.A).sum(axis=1)).ravel()/diagonal))
                        weight = cp.asarray((1.0/max(1.0, bound))/diagonal)
                        self.gpu_levels.append((a, p, p.T.tocsr(), weight))
                self.gpu_preconditioner = LinearOperator(self.matrix.shape, matvec=lambda x: self._gpu_cycle(0, x), dtype=cp.float64)
                self.gpu_preconditioner_name = 'AMG V-cycle'
                return
            except InterruptedError:
                raise
            except Exception as exc:
                self.gpu_levels = None
                self.gpu_coarse_inverse = None
                if self.preconditioner == 'amg':
                    raise
                self.notes.append(f'GPU AMG unavailable: {exc}')
                self._log(self.notes[-1]+' Using GPU Jacobi.')
        inverse = cp.asarray(1/self.diagonal)
        self.gpu_preconditioner = LinearOperator(self.matrix.shape, matvec=lambda x: inverse*x, dtype=cp.float64)
        self.gpu_preconditioner_name = 'Jacobi'

    def solve(self, rhs, x0=None):
        started = time.perf_counter()
        rhs = np.asarray(rhs, dtype=np.float64)
        if np.any(~np.isfinite(rhs)):
            raise ValueError('Non-finite sparse RHS.')
        supplied_guess = x0 is not None
        guess = self.last if x0 is None else np.asarray(x0, dtype=np.float64)
        if guess is not None and (guess.shape != rhs.shape or np.any(~np.isfinite(guess))):
            raise ValueError('Invalid sparse warm start.')
        iterations = [0]
        last_log = [started]
        def count(_):
            iterations[0] += 1
            now = time.perf_counter()
            if now-last_log[0] > 2:
                self._log(f'Sparse solve: {iterations[0]} iterations, {now-started:.1f} s (warm start: {guess is not None}).')
                last_log[0] = now
        maximum = min(30000, max(2000, int(12*np.sqrt(len(rhs)))))
        use_gpu = self.backend == 'gpu' or (self.backend == 'auto' and len(rhs) >= 50000 and not self._gpu_failed)
        name, preconditioner = None, 'direct'
        setup_seconds = transfer_seconds = iteration_seconds = validation_seconds = 0
        if not np.any(rhs):
            values = np.zeros_like(rhs)
            self.gpu_last = None
            name, preconditioner = 'zero RHS', 'none'
        if use_gpu and name is None:
            try:
                tick = time.perf_counter()
                self._prepare_gpu()
                cp = self.cp
                cp.cuda.get_current_stream().synchronize()
                setup_seconds = time.perf_counter()-tick
                from cupyx.scipy.sparse.linalg import cg
                tick = time.perf_counter()
                gpu_rhs = cp.asarray(rhs)
                gpu_guess = cp.asarray(guess) if supplied_guess or (self.gpu_last is None and guess is not None) else self.gpu_last
                cp.cuda.get_current_stream().synchronize()
                transfer_seconds = time.perf_counter()-tick
                tick = time.perf_counter()
                result, info = cg(self.gpu_matrix, gpu_rhs, x0=gpu_guess,
                                  M=self.gpu_preconditioner, rtol=1e-10, atol=1e-11,
                                  maxiter=maximum, callback=count)
                if info:
                    raise ValueError(f'CUDA CG did not converge after {iterations[0]} iterations (status {info}).')
                values = cp.asnumpy(result)
                iteration_seconds = time.perf_counter()-tick
                tick = time.perf_counter()
                self._validate(values, rhs)
                validation_seconds = time.perf_counter()-tick
                self.gpu_last = result
                name, preconditioner = 'CUDA CG', self.gpu_preconditioner_name
            except InterruptedError:
                raise
            except Exception as exc:
                if self.backend == 'gpu':
                    raise ValueError(f'GPU solve unavailable or failed: {exc}. Select Auto or CPU to use the CPU backend.') from exc
                self.notes.append(str(exc))
                self._gpu_failed = True
                self._log(f'CUDA unavailable; using CPU: {exc}')
        if name is None:
            iterations[0] = 0
            if len(rhs) < 200000 and self.preconditioner != 'amg':
                tick = time.perf_counter()
                if self.factor is None:
                    self.factor = linalg.splu(self.matrix.tocsc())
                setup_seconds = time.perf_counter()-tick
                tick = time.perf_counter()
                values = self.factor.solve(rhs)
                iteration_seconds = time.perf_counter()-tick
                name = 'CPU direct'
            else:
                tick = time.perf_counter()
                if self.cpu_preconditioner is None:
                    hierarchy = self._amg()
                    self.cpu_preconditioner = hierarchy.aspreconditioner(cycle='V') if hierarchy is not None else linalg.LinearOperator(self.matrix.shape, matvec=lambda x: x/self.diagonal)
                preconditioner = 'AMG V-cycle' if self.hierarchy is not None else 'Jacobi'
                setup_seconds = time.perf_counter()-tick
                tick = time.perf_counter()
                with threadpool_limits(limits=1):
                    values, info = linalg.cg(self.matrix, rhs, x0=guess, M=self.cpu_preconditioner,
                                            rtol=1e-10, atol=1e-11, maxiter=maximum, callback=count)
                if info:
                    raise ValueError(f'CPU CG did not converge after {iterations[0]} iterations; preconditioner {preconditioner}, increase spacing.')
                name = 'CPU CG'
                iteration_seconds = time.perf_counter()-tick
            tick = time.perf_counter()
            self._validate(values, rhs)
            validation_seconds = time.perf_counter()-tick
        self.last = values
        self.calls += 1
        diagnostics = dict(backend=name, preconditioner=preconditioner, iterations=iterations[0],
                           solve_seconds=time.perf_counter()-started, backend_notes=list(self.notes),
                           warm_start=guess is not None, rhs_solve=self.calls, matrix_reused=self.calls > 1)
        diagnostics.update(setup_seconds=setup_seconds, rhs_transfer_seconds=transfer_seconds,
                           iteration_seconds=iteration_seconds, validation_seconds=validation_seconds)
        if self.cp is not None and name == 'CUDA CG':
            gpu = self.cp.cuda.runtime.getDeviceProperties(0)['name']
            diagnostics['gpu'] = gpu.decode() if isinstance(gpu, bytes) else gpu
        self._log(f'{name}, {preconditioner}: RHS {self.calls}, {iterations[0]} iterations, {diagnostics["solve_seconds"]:.2f} s; matrix reused {self.calls > 1}, warm start {guess is not None}.')
        self._log(f'Backend timings: setup {setup_seconds:.2f} s, RHS transfer {transfer_seconds:.2f} s, iterations/result transfer {iteration_seconds:.2f} s, CPU residual check {validation_seconds:.2f} s.')
        return values, diagnostics

    def _validate(self, values, rhs):
        error = float(np.max(np.abs(self.matrix@values-rhs)))
        if not np.all(np.isfinite(values)) or error > 1e-9+1e-8*max(1, float(np.max(np.abs(rhs)))):
            raise ValueError(f'Sparse residual check failed: {error:.3g}.')

    def close(self):
        self.gpu_last = self.gpu_matrix = self.gpu_preconditioner = self.gpu_levels = None
        self.gpu_coarse_inverse = None
        self.factor = self.hierarchy = self.cpu_preconditioner = self.last = None
        if self.cp is not None:
            self.cp.get_default_memory_pool().free_all_blocks()
