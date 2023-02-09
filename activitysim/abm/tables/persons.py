# ActivitySim
# See full license in LICENSE.txt.
import io
import logging

import pandas as pd

from ...core import inject, pipeline, tracing
from ...core.input import read_input_table
from ...core.workflow import workflow_table

logger = logging.getLogger(__name__)


def read_raw_persons(whale, households):

    df = read_input_table(whale, "persons")

    if whale.get_injectable("households_sliced", False):
        # keep only persons in the sampled households
        df = df[df.household_id.isin(households.index)]

    return df


@workflow_table
def persons(whale):
    households = whale.get_dataframe("households")
    trace_hh_id = whale.settings.trace_hh_id
    df = read_raw_persons(whale, households)

    logger.info("loaded persons %s" % (df.shape,))
    buffer = io.StringIO()
    df.info(buf=buffer)
    logger.debug("persons.info:\n" + buffer.getvalue())

    # replace table function with dataframe
    whale.add_table("persons", df)

    whale.get_rn_generator().add_channel("persons", df)

    tracing.register_traceable_table(whale, "persons", df)
    if trace_hh_id:
        tracing.trace_df(df, "raw.persons", warn_if_empty=True)

    logger.debug(f"{len(df.household_id.unique())} unique household_ids in persons")
    logger.debug(f"{len(households.index.unique())} unique household_ids in households")
    assert not households.index.duplicated().any()
    assert not df.index.duplicated().any()

    persons_without_households = ~df.household_id.isin(households.index)
    if persons_without_households.any():
        logger.error(
            f"{persons_without_households.sum()} persons out of {len(persons)} without households\n"
            f"{pd.Series({'person_id': persons_without_households.index.values})}"
        )
        raise RuntimeError(
            f"{persons_without_households.sum()} persons with bad household_id"
        )

    households_without_persons = (
        df.groupby("household_id").size().reindex(households.index).isnull()
    )
    if households_without_persons.any():
        logger.error(
            f"{households_without_persons.sum()} households out of {len(households.index)} without  persons\n"
            f"{pd.Series({'household_id': households_without_persons.index.values})}"
        )
        raise RuntimeError(
            f"{households_without_persons.sum()} households with no persons"
        )

    return df


# another common merge for persons
# @inject.table()
# def persons_merged(
#     persons, households, land_use, accessibility, disaggregate_accessibility
# ):
#
#     if not disaggregate_accessibility.to_frame().empty:
#         tables = [
#             persons,
#             households,
#             land_use,
#             accessibility,
#             disaggregate_accessibility,
#         ]
#     else:
#         tables = [persons, households, land_use, accessibility]
#
#     return inject.merge_tables(persons.name, tables=tables)


@workflow_table
def persons_merged(whale):

    land_use = whale.get_dataframe("land_use")
    households = whale.get_dataframe("households")
    accessibility = whale.get_dataframe("accessibility")
    persons = whale.get_dataframe("persons")
    disaggregate_accessibility = whale.get_dataframe("disaggregate_accessibility")

    households = pd.merge(
        households,
        land_use,
        left_on="home_zone_id",
        right_index=True,
        suffixes=("_households", "_land_use"),
    )
    households = pd.merge(
        households,
        accessibility,
        left_on="home_zone_id",
        right_index=True,
        suffixes=("_households", "_accessibility"),
    )
    persons = pd.merge(
        persons,
        households,
        left_on="household_id",
        right_index=True,
        suffixes=("_persons", "_households"),
    )
    if not disaggregate_accessibility.empty:
        persons = pd.merge(
            persons,
            disaggregate_accessibility,
            left_on="person_id",
            right_index=True,
            suffixes=("_persons", "_disaggregate_accessibility"),
        )

    return persons
