"""Human-readable formatting helpers."""

# ############################################################################# #
# human() #############################################################################
def human(number):
    """ Return a concise number description."""
    if number <= 0:
        return 0
    suffixes = ['K', 'M', 'G', 'T']
    while suffixes:
        suffix = suffixes.pop(0)
        number /= 1024
        if number < 999.95 or not suffixes:
            # Drop the decimal once there are three+ digits before it
            # (100.0G -> 100G): one column narrower and easier to read.
            # Truncate (not round) so 999.9 stays '999', not '1000'.
            if number >= 100:
                return f'{int(number)}{suffix}'
            return f'{number:.1f}{suffix}'
    return '' # impossible, but make pylint happy


def human_kb(kb):
    """Human form of a KB value (``human()`` expects bytes)."""
    return human(kb * 1024)


def ago_str(delta_secs):
    """Compact two-component interval, e.g. '18h39m', '2m1s', '45s'.

    The lower unit is omitted when it is zero, so exact values show '1h' rather than '1h0m'.
    """
    ago = int(max(0, round(abs(delta_secs))))
    divs = (60, 60, 24, 7, 52, 9999999)
    units = ('s', 'm', 'h', 'd', 'w', 'y')
    vals = (ago % 60, int(ago / 60))  # seed with secs, mins
    uidx = 1
    for div in divs[1:]:
        if vals[1] < div:
            break
        vals = (vals[1] % div, int(vals[1] / div))
        uidx += 1
    if vals[1]:
        rv = f'{vals[1]}{units[uidx]}'
        if vals[0]:
            rv += f'{vals[0]}{units[uidx-1]}'
        return rv
    return f'{vals[0]}{units[uidx-1]}'

