"""Separate finite-grid discretization error from sparse-solver residual error."""


def release_coarse_arrays(coarse):
    """Keep every convergence metric while freeing optional warm-start storage."""
    for data in coarse.values():
        data.pop('mesh', None)
        data.pop('results', None)


def compare_meshes(coarse, refined, components, refined_components, log=None):
    failures = []
    for net in refined.keys()-coarse.keys():
        failures.append(f'Rail {net} is powered only in the refined mesh; topology changed.')
    def compare(label, a, b, unit, absolute):
        difference = abs(a-b)
        allowed = max(absolute, .05*abs(b))
        percent = 100*difference/abs(b) if b else (0 if not difference else float('inf'))
        message = f'{label}: coarse {a:.9g} {unit}, refined {b:.9g} {unit}, change {percent:.3g}% (absolute {difference:.3g} {unit}; allowed {allowed:.3g} {unit})'
        if log:
            log('Mesh comparison / '+message)
        if difference > allowed:
            failures.append(message)
    for net, data in coarse.items():
        if net not in refined:
            failures.append(f'Rail {net} is missing from the refined powered mesh.')
            continue
        compare(f'{net} voltage drop', data['stats'][2], refined[net]['stats'][2], 'V', 1e-6)
        compare(f'{net} copper loss', data['diagnostics']['copper_loss_w'], refined[net]['diagnostics']['copper_loss_w'], 'W', 1e-8)
    fine_parts = {(p['ref'], p['net_a'], p['net_b']): p for p in refined_components}
    for part in components:
        fine = fine_parts.get((part['ref'], part['net_a'], part['net_b']))
        if fine is None:
            failures.append(f'Series component {part["ref"]} is missing from the refined results.')
            continue
        for key, unit, absolute in [('current_a', 'A', 1e-6), ('drop_v', 'V', 1e-6), ('loss_w', 'W', 1e-8)]:
            a, b = part[key], fine[key]
            if (a is None) != (b is None):
                failures.append(f'Series component {part["ref"]} changed powered/contact state.')
            elif a is not None:
                compare(f'{part["ref"]} {key}', a, b, unit, absolute)
    from regulator_heat import regulator_losses
    coarse_regs = {reg['name']: reg for reg in regulator_losses(coarse)}
    fine_regs = {reg['name']: reg for reg in regulator_losses(refined)}
    if coarse_regs.keys() != fine_regs.keys():
        failures.append('Regulator set changed between coarse and refined meshes.')
    for name in coarse_regs.keys() & fine_regs.keys():
        for key, unit, absolute in [('input_current_a', 'A', 1e-6), ('loss_w', 'W', 1e-8)]:
            compare(f'Regulator {name} {key}', coarse_regs[name][key], fine_regs[name][key], unit, absolute)
    return failures
