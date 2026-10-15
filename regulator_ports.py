"""Safeguarded constant-power regulation on a small linear terminal model."""
import numpy as np


def solve_port_currents(open_voltage, resistance, power_w, log=None):
    v0, r, power = np.asarray(open_voltage), np.asarray(resistance), np.asarray(power_w)
    if np.any(~np.isfinite(v0)) or np.any(v0 <= 0) or np.any(~np.isfinite(r)) or np.any(~np.isfinite(power)) or np.any(power < 0):
        raise ValueError('Invalid regulator terminal-response model.')
    if not np.allclose(r, r.T, atol=1e-7, rtol=1e-4):
        raise ValueError('Regulator terminal responses failed reciprocity; refine the linear solve.')
    r = (r+r.T)/2
    if np.min(np.linalg.eigvalsh(r)) < -1e-7:
        raise ValueError('Regulator terminal model is not passive.')
    if len(power) == 1:
        resistance = max(0, float(r[0, 0]))
        discriminant = float(v0[0]**2-4*resistance*power[0])
        if discriminant < 0:
            raise ValueError('Regulator constant-power demand exceeds the upstream network maximum power; no stable DC solution.')
        if discriminant <= 1e-12*max(1, float(v0[0]**2)):
            raise ValueError('Regulator demand is at the voltage-collapse boundary; no stable margin remains.')
        current = np.array([2*power[0]/(v0[0]+np.sqrt(discriminant))])
        if log:
            log(f'Regulator port model: analytic high-voltage solution, {current[0]:.9g} A; no full-grid fixed-point loop.')
        return current, 0
    current = power/v0
    tolerance = 1e-9+1e-8*np.maximum(power, 1e-6)
    for iteration in range(50):
        voltage = v0-r@current
        residual = current*voltage-power
        if np.all(voltage > 0) and np.all(np.abs(residual) <= tolerance):
            if log:
                log(f'Regulator port model: {iteration} small Newton steps; maximum power mismatch {np.max(np.abs(residual)):.3g} W.')
            # Reject the unstable low-voltage branch.
            sensitivity = (power/voltage**2)[:, None]*r
            if np.max(np.abs(np.linalg.eigvals(sensitivity))) >= 1+1e-6:
                raise ValueError('Regulator operating point is unstable (voltage collapse).')
            return current, iteration
        jacobian = np.diag(voltage)-current[:, None]*r
        try:
            delta = np.linalg.solve(jacobian, -residual)
        except np.linalg.LinAlgError as exc:
            raise ValueError('Regulator port model is singular near voltage collapse.') from exc
        error = np.max(np.abs(residual)/tolerance)
        for damping in (.5**i for i in range(30)):
            trial = current+damping*delta
            trial_voltage = v0-r@trial
            if np.all(trial >= 0) and np.all(trial_voltage > 0) and np.max(np.abs(trial*trial_voltage-power)/tolerance) < error:
                current = trial
                break
        else:
            raise ValueError('Regulator demand has no convergent stable DC operating point.')
    raise ValueError('Regulator port Newton solve did not converge; inspect supply impedance and demanded power.')
