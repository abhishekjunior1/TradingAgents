"""Tables at a terminal with every value whole: a close date, price or percentage is never split
across lines or cut short, whatever the terminal's width."""

from rich.console import Console, ConsoleOptions, Group, RenderableType
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text


def fit(table: Table, console: Console, options: ConsoleOptions) -> RenderableType:
    """``table`` laid out within ``options.max_width`` with no value split or cut.

    rich narrows a table that is too wide by taking from its widest columns, even below their
    longest word, which then folds: '2026-10-30' read '2026-10-3' with the '0' on the next line.
    Here the table keeps its natural width when that fits; else its widest columns are narrowed,
    never below their longest word, so cells wrap only at spaces; when even that is too wide,
    each row becomes a block of label and value lines. A table without headers already is label
    and value rows, and is left to fold.
    """
    unbounded = options.update_width(10_000)
    columns = [[column.header, *column.cells] for column in table.columns]
    widths = [max(Measurement.get(console, unbounded, cell).maximum for cell in cells) for cells in columns]
    floors = [max(Measurement.get(console, unbounded, cell).minimum for cell in cells) for cells in columns]
    while (excess := Measurement.get(console, unbounded, table).maximum - options.max_width) > 0:
        for _ in range(excess):
            narrowable = [index for index, width in enumerate(widths) if width > floors[index]]
            if not narrowable:
                return _records(table, console, options) if table.show_header and table.row_count else table
            widths[max(narrowable, key=widths.__getitem__)] -= 1
        for column, width in zip(table.columns, widths, strict=True):
            column.max_width = width
    return table


def _records(table: Table, console: Console, options: ConsoleOptions) -> Group:
    """Each row of ``table`` as a block of label and value lines, with a blank line after it."""
    labels = [column.header for column in table.columns]
    blocks = []
    for row in zip(*(column.cells for column in table.columns), strict=False):   # the rows rich draws
        block = Table.grid(padding=(0, 2))
        block.add_column(style=table.header_style, overflow="fold")
        block.add_column(overflow="fold")
        for label, value in zip(labels, row, strict=True):
            block.add_row(label, value)
        blocks += [fit(block, console, options), Text()]
    return Group(*blocks)
