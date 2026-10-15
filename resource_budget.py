"""Conservative RAM estimates before allocating a mesh or assembled network."""
import math
import os

GIB = 1024**3


class MemoryBudget(float):
    """Keep the selected mode while remaining compatible with numeric budgets."""
    def __new__(cls, amount, dynamic=False):
        value = super().__new__(cls, amount)
        value.dynamic = dynamic
        return value


class MemoryBudgetExceeded(ValueError):
    def __init__(self, additional, retained, allowed_additional, free, dynamic, reason):
        self.additional_bytes = additional
        self.retained_bytes = retained
        self.allowed_additional_bytes = max(0, allowed_additional)
        self.available_bytes = free
        self.dynamic = dynamic
        mode = 'Dynamic' if dynamic else 'Manual'
        super().__init__(
            f'Mesh/solver additional RAM estimate {additional/GIB:.2f} GB exceeds '
            f'{mode} budget allowance {max(0, allowed_additional)/GIB:.2f} GB. '
            f'Already retained: {retained/GIB:.2f} GB; physically available: {free/GIB:.2f} GB. '
            f'{reason}')


def available_ram():
    if os.name == 'nt':
        import ctypes
        class Status(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in
                ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        status = Status(); status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available
    try:
        return os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, ValueError):
        return 4*GIB


def default_budget_gb():
    return max(.25, min(8, available_ram()/GIB*.25))


def auto_budget_gb():
    """Leave physical RAM for KiCad/OS and avoid treating paging as usable RAM."""
    free = available_ram()
    if not math.isfinite(free) or free <= 0:
        raise ValueError('No available physical RAM for the automatic memory budget.')
    reserve = min(free*.5, max(512*1024**2, min(4*GIB, free*.2)))
    return min(free*.65, free-reserve)/GIB


def resolve_budget(value='auto'):
    if isinstance(value, str) and value.strip().lower() in ('auto', 'dynamic'):
        return MemoryBudget(auto_budget_gb(), dynamic=True)
    try:
        amount = float(value) if not isinstance(value, bool) else float('nan')
    except (TypeError, ValueError) as exc:
        raise ValueError('Memory budget must be Auto, Dynamic, or a finite positive number.') from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('Memory budget must be finite and positive.')
    return MemoryBudget(amount)


def grid_estimate(geometry, spacing):
    shapes = [shape for shape in geometry.values() if not shape.is_empty]
    if not shapes:
        return 0, 0
    xmin = min(s.bounds[0] for s in shapes); xmax = max(s.bounds[2] for s in shapes)
    ymin = min(s.bounds[1] for s in shapes); ymax = max(s.bounds[3] for s in shapes)
    plane = (math.ceil((xmax-xmin)/spacing+2)+1)*(math.ceil((ymax-ymin)/spacing+2)+1)
    nodes = min(plane*len(shapes), sum(math.ceil(s.area/spacing**2+s.length/spacing+16) for s in shapes))
    # Includes coordinate/matrix arrays, assembly, CPU validation and CG workspace.
    return plane*len(shapes), plane*40+nodes*800


def check_budget(estimated_bytes, budget_gb, retained_bytes=0):
    if not math.isfinite(budget_gb) or budget_gb <= 0:
        raise ValueError('Memory budget must be finite and positive.')
    if any(not math.isfinite(n) or n < 0 for n in (estimated_bytes, retained_bytes)):
        raise ValueError('Memory estimates must be finite and nonnegative.')
    free = available_ram()
    dynamic = getattr(budget_gb, 'dynamic', False)
    if dynamic:
        # Retained allocations already reduced Windows' free RAM. They are not
        # another future allocation, and the old displayed allowance is not a cap.
        reserve = min(free*.5, max(512*1024**2, min(4*GIB, free*.2)))
        allowed = max(0, min(free*.65, free-reserve))
        reason = 'Dynamic reserves RAM for KiCad/OS; it cannot enlarge physical RAM or change mesh spacing.'
    else:
        # A manual budget caps this run's retained + future RAM; the physical
        # headroom guard applies only to memory still to be allocated.
        allowed = min(budget_gb*GIB-retained_bytes, free*.7)
        reason = 'Increase the manual limit if physical RAM permits, or increase mesh spacing.'
    if estimated_bytes > allowed:
        raise MemoryBudgetExceeded(estimated_bytes, retained_bytes, allowed, free, dynamic, reason)
    return estimated_bytes+retained_bytes


def feasible_spacing(geometries, spacing, additional_limit):
    """Conservative fresh-pass spacing suggestion; never alters the requested mesh."""
    return _feasible_spacing(lambda step: sum(grid_estimate(geo, step)[1] for geo in geometries),
                             spacing, additional_limit)


def feasible_thermal_spacing(region, spacing, additional_limit):
    xmin, ymin, xmax, ymax = region.bounds
    return _feasible_spacing(lambda step: math.ceil((xmax-xmin)/step)*math.ceil((ymax-ymin)/step)*1000,
                             spacing, additional_limit)


def _feasible_spacing(estimate, spacing, additional_limit):
    if not math.isfinite(additional_limit) or additional_limit <= 0:
        return None
    def fits(step):
        return estimate(step) <= additional_limit
    low, high = spacing, spacing
    for _ in range(30):
        if fits(high):
            break
        high *= 2
    else:
        return None
    for _ in range(25):
        middle = (low+high)/2
        if fits(middle):
            high = middle
        else:
            low = middle
    # Round upward so the displayed suggestion still satisfies the estimate.
    increment = .001 if high < .1 else .01
    return math.ceil(high/increment)*increment
