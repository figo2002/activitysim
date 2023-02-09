# ActivitySim
# See full license in LICENSE.txt.
import logging

import pandas as pd

from ..core.pipeline import Whale
from ..core.workflow import workflow_cached_object

# FIXME
# warnings.filterwarnings('ignore', category=pd.io.pytables.PerformanceWarning)
pd.options.mode.chained_assignment = None

logger = logging.getLogger(__name__)


@workflow_cached_object
def households_sample_size(whale: Whale, override_hh_ids):

    if override_hh_ids is None:
        return whale.settings, households_sample_size
    else:
        return 0 if override_hh_ids is None else len(override_hh_ids)


@workflow_cached_object
def override_hh_ids(whale: Whale):

    hh_ids_filename = whale.settings.hh_ids
    if hh_ids_filename is None:
        return None

    file_path = whale.filesystem.get_data_file_path(hh_ids_filename, mandatory=False)
    if not file_path:
        file_path = whale.filesystem.get_config_file_path(
            hh_ids_filename, mandatory=False
        )
    if not file_path:
        logger.error(
            "hh_ids file name '%s' specified in settings not found" % hh_ids_filename
        )
        return None

    df = pd.read_csv(file_path, comment="#")

    if "household_id" not in df.columns:
        logger.error("No 'household_id' column in hh_ids file %s" % hh_ids_filename)
        return None

    household_ids = df.household_id.astype(int).unique()

    if len(household_ids) == 0:
        logger.error("No households in hh_ids file %s" % hh_ids_filename)
        return None

    logger.info(
        "Using hh_ids list with %s households from file %s"
        % (len(household_ids), hh_ids_filename)
    )

    return household_ids


# @workflow_object
# def trace_hh_id(whale: Whale):
#
#     id = whale.settings.trace_hh_id
#
#     if id and not isinstance(id, int):
#         logger.warning(
#             "setting trace_hh_id is wrong type, should be an int, but was %s" % type(id)
#         )
#         id = None
#
#     return id


@workflow_cached_object
def trace_od(whale: Whale):

    od = whale.settings.trace_od

    if od and not (
        isinstance(od, list) and len(od) == 2 and all(isinstance(x, int) for x in od)
    ):
        logger.warning("setting trace_od should be a list of length 2, but was %s" % od)
        od = None

    return od


@workflow_cached_object
def chunk_size(whale: Whale):
    _chunk_size = int(whale.settings.chunk_size or 0)

    return _chunk_size


@workflow_cached_object
def check_for_variability(whale: Whale):
    return bool(whale.settings.check_for_variability)
