"""Text for panels that have no framebuffer.

A 16x2 character LCD has thirty-two cells and no pixels. A MAX7219 chain is
eight rows of thirty-two dots. Neither can hold the dashboard, and pretending
otherwise produces a grey smear, so these get their own presentation: one fact
at a time, rotated.

The rotation is the design, not a compromise. A gauge is for glancing at, and
a glance at two lines of text is faster than a glance at a bar chart - what a
16x2 loses is history and comparison, not legibility.
"""

from __future__ import annotations

from .layout import fmt_ago, fmt_capacity, fmt_rate, fmt_uptime


def _pct(value):
    return "--" if value is None else "%d%%" % round(value)


def fields(machine):
    """The rotation, in order. (label, value) with the label kept short."""
    out = [
        ("CPU", _pct(machine.cpu_percent)),
        ("RAM", _pct(machine.ram_percent)),
    ]
    if machine.storage_percent is not None:
        free = fmt_capacity(machine.storage_free_gb)
        out.append(("DISK", "%s %s fr" % (_pct(machine.storage_percent), free)))
    if machine.cpu_temp_c is not None:
        out.append(("TEMP", "%.0fC" % machine.cpu_temp_c))
    if machine.rx_kbps is not None or machine.tx_kbps is not None:
        out.append(("NET", "%s/%s" % (fmt_rate(machine.rx_kbps),
                                      fmt_rate(machine.tx_kbps))))
    if machine.has_battery:
        out.append(("BATT", "%d%%%s" % (machine.battery_percent,
                                        " CHG" if machine.battery_charging else "")))
    if machine.uptime_seconds is not None:
        out.append(("UP", fmt_uptime(machine.uptime_seconds)))
    return out


def _fit(text, width):
    return text[:width] if len(text) > width else text


def _pad(text, width):
    """Trailing spaces matter: a character LCD does not clear what it wrote.

    Writing a shorter string over a longer one leaves the tail of the old
    value on screen, so "100%" becoming "9%" reads as "9%0%" until something
    longer happens to overwrite it.
    """
    return _fit(text, width).ljust(width)


def two_line(machine, snapshot, step, cols=16, offline_after_s=150,
             machine_index=0, machine_count=1):
    """Two lines for a character LCD. ``step`` advances the rotation."""
    age = snapshot.age_of(machine)
    online = age < offline_after_s

    # Line 1 is stable: which machine, and whether to believe line 2. A
    # hostname that scrolled would make the whole display feel unreadable, so
    # it is truncated and the page marker earns its two characters.
    marker = ""
    if machine_count > 1:
        marker = "%d/%d" % (machine_index + 1, machine_count)
    head_room = cols - (len(marker) + 1 if marker else 0)
    line1 = _fit(machine.host, head_room)
    if marker:
        line1 = line1.ljust(head_room) + " " + marker

    if not online:
        return [_pad(line1, cols), _pad("OFFLINE %s" % fmt_ago(age), cols)]

    rows = fields(machine)
    if not rows:
        return [_pad(line1, cols), _pad("no data", cols)]
    label, value = rows[step % len(rows)]
    line2 = "%s %s" % (label, value)
    return [_pad(line1, cols), _pad(line2, cols)]


def one_line(machine, snapshot, step, cols=16, offline_after_s=150):
    """A single line, for a 16x1 LCD or a narrow matrix."""
    age = snapshot.age_of(machine)
    if age >= offline_after_s:
        return [_pad("%s OFF" % _fit(machine.host, cols - 4), cols)]
    rows = fields(machine)
    if not rows:
        return [_pad(_fit(machine.host, cols), cols)]
    label, value = rows[step % len(rows)]
    return [_pad("%s %s" % (label, value), cols)]


def marquee(machines, snapshot, offline_after_s=150):
    """One string for a scrolling LED matrix.

    Everything on one pass rather than a rotation: a matrix is already showing
    one character at a time, so adding a second timer on top of the scroll
    would mean a field could vanish mid-word.
    """
    if not machines:
        return "PEEK  no machines reporting"

    parts = []
    for machine in machines:
        age = snapshot.age_of(machine)
        if age >= offline_after_s:
            parts.append("%s OFFLINE %s" % (machine.host, fmt_ago(age)))
            continue
        bits = ["%s" % machine.host,
                "CPU %s" % _pct(machine.cpu_percent),
                "RAM %s" % _pct(machine.ram_percent)]
        if machine.storage_percent is not None:
            bits.append("DISK %s" % _pct(machine.storage_percent))
        if machine.cpu_temp_c is not None:
            bits.append("%.0fC" % machine.cpu_temp_c)
        if machine.has_battery:
            bits.append("BAT %d%%" % machine.battery_percent)
        parts.append("  ".join(bits))

    # Trailing spaces so the message does not butt against its own head when
    # the scroll wraps.
    return "   *   ".join(parts) + "      "
