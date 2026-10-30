"""pandas' deprecation notices raised from yfinance's code stay out of a run's output.

yfinance installs a filter that shows every DeprecationWarning from its own modules, and
yfinance 1.7 calls pandas features pandas 3.1 deprecates, so each download printed lines
such as "Pandas4Warning: The inplace keyword in DataFrame.sort_index is deprecated" into
the CLI's output, PYTHONWARNINGS=ignore notwithstanding (found on 30 Oct 2026).
"""

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from pandas.errors import Pandas4Warning

from tradingagents.dataflows.vendors.yahoo import common


def _shown(module: str, quieted: bool) -> int:
    """How many of one pandas notice, raised from ``module``, would be shown."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()
        warnings.filterwarnings("default", category=DeprecationWarning, module="^yfinance")  # yfinance's own filter
        if quieted:
            common._quiet_pandas_deprecations_from_yfinance()
        warnings.warn_explicit("The inplace keyword in DataFrame.sort_index is deprecated", Pandas4Warning,
                               "utils.py", 568, module=module)
    return len(caught)


@pytest.mark.unit
def test_pandas_deprecations_raised_from_yfinance_are_not_shown():
    assert _shown("yfinance.utils", quieted=False) == 1      # what a run printed
    assert _shown("yfinance.utils", quieted=True) == 0


@pytest.mark.unit
def test_other_modules_warnings_still_show():
    assert _shown("tradingagents.dataflows.vendors.yahoo.market", quieted=True) == 1


@pytest.mark.unit
def test_a_fresh_process_that_loads_the_yahoo_vendor_shows_no_such_notice():
    # In a process of its own: pytest restores the warning filters after collecting the
    # tests, so one installed at import time is not in place while a test runs.
    notice = ("from pandas.errors import Pandas4Warning; import warnings; "
              "warnings.warn_explicit('The inplace keyword in DataFrame.sort_index is deprecated', "
              "Pandas4Warning, 'utils.py', 568, module='yfinance.utils')")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONWARNINGS"}

    def stderr(imports: str) -> str:
        return subprocess.run([sys.executable, "-c", f"{imports}; {notice}"], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[1], env=env, timeout=120).stderr

    assert "Pandas4Warning" in stderr("import yfinance")                    # what a run printed
    assert "Pandas4Warning" not in stderr("import tradingagents.dataflows.vendors.yahoo.market")
