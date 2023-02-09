# ActivitySim
# See full license in LICENSE.txt.
import logging

import pandas as pd

from activitysim.core.input import read_input_table

from ...core.pipeline import Whale
from ...core.workflow import workflow_table

logger = logging.getLogger(__name__)


@workflow_table
def accessibility(whale: Whale):
    """
    If 'accessibility' is in input_tables list, then read it in,
    otherwise create skeleton table with same index as landuse.

    This allows loading of pre-computed accessibility table, which is particularly useful
    for single-process small household sample runs when there are many zones in landuse

    skeleton table only required if multiprocessing wants to slice accessibility,
    otherwise it will simply be replaced when accessibility model is run
    """

    land_use = whale.get_dataframe("land_use")
    accessibility_df = read_input_table(whale, "accessibility", required=False)

    if accessibility_df is None:
        accessibility_df = pd.DataFrame(index=land_use.index)
        logger.debug(
            "created placeholder accessibility table %s" % (accessibility_df.shape,)
        )
    else:
        try:
            assert accessibility_df.sort_index().index.equals(
                land_use.sort_index().index
            ), f"loaded accessibility table index does not match index of land_use table"
        except AssertionError:
            land_use_index = land_use.index
            if f"_original_{land_use_index.name}" in land_use:
                land_use_zone_ids = land_use[f"_original_{land_use_index.name}"]
                remapper = dict(zip(land_use_zone_ids, land_use_zone_ids.index))
                accessibility_df.index = accessibility_df.index.map(remapper.get)
                assert accessibility_df.sort_index().index.equals(
                    land_use.sort_index().index
                ), f"loaded accessibility table index does not match index of land_use table"
            else:
                raise
        logger.info("loaded land_use %s" % (accessibility_df.shape,))

    # replace table function with dataframe
    whale.add_table("accessibility", accessibility_df)

    return accessibility_df
