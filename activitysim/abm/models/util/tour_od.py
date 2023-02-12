# ActivitySim
# See full license in LICENSE.txt.
import logging

import numpy as np
import pandas as pd
from orca import orca

from activitysim.abm.tables.size_terms import tour_destination_size_terms
from activitysim.core import (
    config,
    expressions,
    inject,
    logit,
    los,
    simulate,
    tracing,
    workflow,
)
from activitysim.core.interaction_sample import interaction_sample
from activitysim.core.interaction_sample_simulate import interaction_sample_simulate
from activitysim.core.util import reindex

from . import logsums as logsum
from . import trip

logger = logging.getLogger(__name__)
DUMP = False

# temp column names for presampling
DEST_MAZ = "dest_MAZ"
DEST_TAZ = "dest_TAZ"

# likewise a temp, but if already in choosers,
# we assume we can use it opportunistically
ORIG_TAZ = "orig_TAZ"
ORIG_MAZ = "orig_MAZ"
ORIG_TAZ_EXT = "orig_TAZ_ext"


def get_od_id_col(origin_col, destination_col):
    colname = "{0}_{1}".format(origin_col, destination_col)
    return colname


def create_od_id_col(df, origin_col, destination_col):
    return df[origin_col].astype(str) + "_" + df[destination_col].astype(str)


def _get_od_cols_from_od_id(
    df, orig_col_name=None, dest_col_name=None, od_id_col="choice"
):
    df[orig_col_name] = df[od_id_col].str.split("_").str[0].astype(int)
    df[dest_col_name] = df[od_id_col].str.split("_").str[1].astype(int)

    return df


def _create_od_alts_from_dest_size_terms(
    size_terms_df,
    segment_name,
    od_id_col=None,
    origin_id_col="origin",
    dest_id_col="destination",
    origin_filter=None,
    origin_attr_cols=None,
):
    """
    Extend destination size terms to create dataframe representing the
    cartesian product of tour origins and destinations. Actual "Size Terms"
    will still only be associated with the destinations, but individual
    attributes of the origins can be preserved.
    """

    land_use = inject.get_table("land_use").to_frame(columns=origin_attr_cols)

    if origin_filter:
        origins = land_use.query(origin_filter)
    else:
        origins = land_use

    n_repeat = len(origins)
    od_alts = size_terms_df.reindex(size_terms_df.index.repeat(n_repeat))
    od_alts[origin_id_col] = list(origins.index.values) * od_alts.index.nunique()
    od_alts.reset_index(inplace=True)
    if dest_id_col not in od_alts.columns:
        od_alts.rename(columns={land_use.index.name: dest_id_col}, inplace=True)

    if od_id_col is None:
        new_index_name = get_od_id_col(origin_id_col, dest_id_col)
    else:
        new_index_name = od_id_col
    od_alts[new_index_name] = (
        od_alts[origin_id_col].astype(str) + "_" + od_alts[dest_id_col].astype(str)
    )
    od_alts.set_index(new_index_name, inplace=True)

    # manually add origin attributes to output since these can't be generated by
    # the destination-based size term calculator
    if origin_attr_cols:
        land_use.index.name = origin_id_col
        land_use.reset_index(inplace=True)
        od_alts.reset_index(inplace=True)
        od_alts = pd.merge(
            od_alts,
            land_use[origin_attr_cols + [origin_id_col]],
            on=origin_id_col,
            how="left",
        ).set_index(new_index_name)

    return od_alts


@workflow.func
def _od_sample(
    whale: workflow.Whale,
    spec_segment_name,
    choosers,
    network_los,
    destination_size_terms,
    origin_id_col,
    dest_id_col,
    skims,
    estimator,
    model_settings,
    alt_od_col_name,
    chunk_size,
    chunk_tag,
    trace_label,
):
    model_spec = simulate.spec_for_segment(
        model_settings,
        spec_id="SAMPLE_SPEC",
        segment_name=spec_segment_name,
        estimator=estimator,
    )
    if alt_od_col_name is None:
        alt_col_name = get_od_id_col(origin_id_col, dest_id_col)
    else:
        alt_col_name = alt_od_col_name

    logger.info("running %s with %d tours", trace_label, len(choosers))

    sample_size = model_settings["SAMPLE_SIZE"]
    if whale.settings.disable_destination_sampling or (
        estimator and estimator.want_unsampled_alternatives
    ):
        # FIXME interaction_sample will return unsampled complete alternatives
        # with probs and pick_count
        logger.info(
            (
                "Estimation mode for %s using unsampled alternatives "
                "short_circuit_choices"
            )
            % trace_label
        )
        sample_size = 0

    locals_d = {
        "skims": skims,
        "timeframe": "timeless",
        "orig_col_name": ORIG_TAZ,
        "dest_col_name": DEST_TAZ,
    }
    constants = config.get_model_constants(model_settings)
    if constants is not None:
        locals_d.update(constants)

    origin_filter = model_settings.get("ORIG_FILTER", None)
    origin_attr_cols = model_settings["ORIGIN_ATTR_COLS_TO_USE"]

    od_alts_df = _create_od_alts_from_dest_size_terms(
        destination_size_terms,
        spec_segment_name,
        od_id_col=alt_col_name,
        origin_id_col=origin_id_col,
        dest_id_col=dest_id_col,
        origin_filter=origin_filter,
        origin_attr_cols=origin_attr_cols,
    )

    if skims.orig_key == ORIG_TAZ:
        od_alts_df[ORIG_TAZ] = network_los.map_maz_to_taz(od_alts_df[origin_id_col])

    elif skims.orig_key not in od_alts_df:
        logger.error("Alts df is missing origin skim key column.")

    choices = interaction_sample(
        whale,
        choosers,
        alternatives=od_alts_df,
        sample_size=sample_size,
        alt_col_name=alt_col_name,
        spec=model_spec,
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        chunk_tag=chunk_tag,
        trace_label=trace_label,
        zone_layer="taz",
    )

    return choices


def od_sample(
    spec_segment_name,
    choosers,
    model_settings,
    network_los,
    destination_size_terms,
    estimator,
    chunk_size,
    trace_label,
):
    chunk_tag = "tour_od.sample"

    origin_col_name = model_settings["ORIG_COL_NAME"]
    dest_col_name = model_settings["DEST_COL_NAME"]
    alt_dest_col_name = model_settings["ALT_DEST_COL_NAME"]

    skim_dict = network_los.get_default_skim_dict()
    skims = skim_dict.wrap(origin_col_name, dest_col_name)

    # the name of the od column to be returned in choices
    alt_od_col_name = get_od_id_col(origin_col_name, dest_col_name)
    choices = _od_sample(
        spec_segment_name,
        choosers,
        network_los,
        destination_size_terms,
        origin_col_name,
        dest_col_name,
        skims,
        estimator,
        model_settings,
        alt_od_col_name,
        chunk_size,
        chunk_tag,
        trace_label,
    )

    choices[origin_col_name] = (
        choices[alt_od_col_name].str.split("_").str[0].astype(int)
    )
    choices[dest_col_name] = choices[alt_od_col_name].str.split("_").str[1].astype(int)

    return choices


def map_maz_to_taz(s, network_los):
    maz_to_taz = network_los.maz_taz_df[["MAZ", "TAZ"]].set_index("MAZ").TAZ
    return s.map(maz_to_taz)


def map_maz_to_ext_taz(s):
    land_use = (
        inject.get_table("land_use").to_frame(columns=["external_TAZ"]).external_TAZ
    )
    return s.map(land_use).astype(int)


def map_maz_to_ext_maz(s):
    land_use = (
        inject.get_table("land_use").to_frame(columns=["external_MAZ"]).external_MAZ
    )
    return s.map(land_use).astype(int)


def map_ext_maz_to_maz(s):
    land_use = (
        inject.get_table("land_use").to_frame(columns=["original_MAZ"]).original_MAZ
    )
    return s.map(land_use).astype(int)


def aggregate_size_terms(dest_size_terms, network_los):
    # aggregate MAZ_size_terms to TAZ_size_terms
    MAZ_size_terms = dest_size_terms.copy()

    # add crosswalk DEST_TAZ column to MAZ_size_terms
    MAZ_size_terms[DEST_TAZ] = network_los.map_maz_to_taz(MAZ_size_terms.index)

    # aggregate to TAZ
    TAZ_size_terms = MAZ_size_terms.groupby(DEST_TAZ).agg({"size_term": "sum"})
    assert not TAZ_size_terms["size_term"].isna().any()

    #           size_term
    # dest_TAZ
    # 2              45.0
    # 3              44.0
    # 4              59.0

    # add crosswalk DEST_TAZ column to MAZ_size_terms
    # MAZ_size_terms = MAZ_size_terms.sort_values([DEST_TAZ, 'size_term'])  # maybe helpful for debugging
    MAZ_size_terms = MAZ_size_terms[[DEST_TAZ, "size_term"]].reset_index(drop=False)
    MAZ_size_terms = MAZ_size_terms.sort_values([DEST_TAZ, "zone_id"]).reset_index(
        drop=True
    )

    #       zone_id  dest_TAZ  size_term
    # 0        6097         2       10.0
    # 1       16421         2       13.0
    # 2       24251         3       14.0

    # print(f"TAZ_size_terms ({TAZ_size_terms.shape})\n{TAZ_size_terms}")
    # print(f"MAZ_size_terms ({MAZ_size_terms.shape})\n{MAZ_size_terms}")

    return MAZ_size_terms, TAZ_size_terms


@workflow.func
def choose_MAZ_for_TAZ(
    whale: workflow.Whale,
    taz_sample,
    MAZ_size_terms,
    trace_label,
    addtl_col_for_unique_key=None,
    dest_maz_id_col=DEST_MAZ,
):
    """
    Convert taz_sample table with TAZ zone sample choices to a table with a MAZ zone chosen for each TAZ
    choose MAZ probabilistically (proportionally by size_term) from set of MAZ zones in parent TAZ

    Parameters
    ----------
    taz_sample: dataframe with duplicated index <chooser_id_col> and columns: <DEST_TAZ>, prob, pick_count
    MAZ_size_terms: dataframe with unique index and columns: <dest_maz_id_col>, dest_TAZ, size_term
    trace_label: str
    addtl_col_for_unique_key: str of col name to use in addition to destination zone if destination
        zone alone will not be unique per chooser as is the case for joint simulation of tour ODs.

    Returns
    -------
    dataframe with with duplicated index <chooser_id_col> and columns: <DEST_MAZ>, prob, pick_count
    """

    # print(f"taz_sample\n{taz_sample}")
    #           dest_TAZ      prob  pick_count  person_id
    # tour_id
    # 542963          18  0.004778           1      13243
    # 542963          53  0.004224           2      13243
    # 542963          59  0.008628           1      13243

    trace_hh_id = whale.settings.trace_hh_id
    have_trace_targets = trace_hh_id and tracing.has_trace_targets(whale, taz_sample)
    if have_trace_targets:
        trace_label = tracing.extend_trace_label(trace_label, "choose_MAZ_for_TAZ")

        CHOOSER_ID = (
            taz_sample.index.name
        )  # zone_id for tours, but person_id for location choice
        assert CHOOSER_ID is not None

        # write taz choices, pick_counts, probs
        trace_targets = tracing.trace_targets(taz_sample)
        tracing.trace_df(
            taz_sample[trace_targets],
            label=tracing.extend_trace_label(trace_label, "taz_sample"),
            transpose=False,
        )

    if addtl_col_for_unique_key is None:
        addtl_col_for_unique_key = []
    else:
        addtl_col_for_unique_key = [addtl_col_for_unique_key]

    # redupe taz_sample[[DEST_TAZ, 'prob']] using pick_count to repeat rows
    taz_choices = taz_sample[[DEST_TAZ, "prob"] + addtl_col_for_unique_key].reset_index(
        drop=False
    )
    taz_choices = taz_choices.reindex(
        taz_choices.index.repeat(taz_sample.pick_count)
    ).reset_index(drop=True)
    taz_choices = taz_choices.rename(columns={"prob": "TAZ_prob"})

    # print(f"taz_choices\n{taz_choices}")
    #        tour_id  dest_TAZ  TAZ_prob  <addtl_col_for_unique_key>
    # 0         856      4522  0.000679      7066
    # 1         856      4666  0.001222      7066
    # 2         856      4802  0.003473      7066
    # 3         856      4927  0.027282      7066
    # 4         856      4961  0.004853      7066

    # print(f"MAZ_size_terms\n{MAZ_size_terms}")
    #       zone_id  dest_TAZ  size_term
    # 0        6097         2      7.420
    # 1       16421         2      9.646
    # 2       24251         2     10.904

    # just to make it clear we are siloing choices by chooser_id
    chooser_id_col = (
        taz_sample.index.name
    )  # should be canonical chooser index name (e.g. 'person_id')

    # for random_for_df, we need df with de-duplicated chooser canonical index
    chooser_df = pd.DataFrame(index=taz_sample.index[~taz_sample.index.duplicated()])
    num_choosers = len(chooser_df)
    assert chooser_df.index.name == chooser_id_col

    # to make choices, <taz_sample_size> rands for each chooser (one rand for each sampled TAZ)
    # taz_sample_size will be model_settings['SAMPLE_SIZE'] samples, except if we are estimating
    taz_sample_size = taz_choices.groupby(chooser_id_col)[DEST_TAZ].count().max()

    # taz_choices index values should be contiguous
    assert (
        taz_choices[chooser_id_col] == np.repeat(chooser_df.index, taz_sample_size)
    ).all()

    # we need to choose a MAZ for each DEST_TAZ choice
    # probability of choosing MAZ based on MAZ size_term fraction of TAZ total
    # there will be a different set (and number) of candidate MAZs for each TAZ
    # (preserve index, which will have duplicates as result of join)
    # maz_sizes.index is the integer offset into taz_choices of the taz for which the maz_size row is a candidate)
    maz_sizes = pd.merge(
        taz_choices[
            [chooser_id_col, DEST_TAZ] + addtl_col_for_unique_key
        ].reset_index(),
        MAZ_size_terms,
        how="left",
        on=DEST_TAZ,
    ).set_index("index")

    #         tour_id  dest_TAZ  <addtl_col_for_unique_key>  zone_id  size_term
    # index
    # 0          856      4522          7066                  1624    899.648
    # 0          856      4522          7066                  1627     52.918
    # 1          856      4666          7066                  8507    253.256
    # 2          856      4802          7066                  8444    904.930
    # 3          856      4927          7066                  6999    712.630

    if have_trace_targets:
        # write maz_sizes: maz_sizes[index,tour_id,dest_TAZ,zone_id,size_term]

        maz_sizes_trace_targets = tracing.trace_targets(maz_sizes, slicer=CHOOSER_ID)
        trace_maz_sizes = maz_sizes[maz_sizes_trace_targets]
        tracing.trace_df(
            trace_maz_sizes,
            label=tracing.extend_trace_label(trace_label, "maz_sizes"),
            transpose=False,
        )

    # number of DEST_TAZ candidates per chooser
    maz_counts = maz_sizes.groupby(maz_sizes.index).size().values

    # max number of MAZs for any TAZ
    max_maz_count = maz_counts.max()

    # offsets of the first and last rows of each chooser in sparse interaction_utilities
    last_row_offsets = maz_counts.cumsum()
    first_row_offsets = np.insert(last_row_offsets[:-1], 0, 0)

    # repeat the row offsets once for each dummy utility to insert
    # (we want to insert dummy utilities at the END of the list of alternative utilities)
    # inserts is a list of the indices at which we want to do the insertions
    inserts = np.repeat(last_row_offsets, max_maz_count - maz_counts)

    # insert zero filler to pad each alternative set to same size
    padded_maz_sizes = np.insert(maz_sizes.size_term.values, inserts, 0.0).reshape(
        -1, max_maz_count
    )

    # prob array with one row TAZ_choice, one column per alternative
    row_sums = padded_maz_sizes.sum(axis=1)
    maz_probs = np.divide(padded_maz_sizes, row_sums.reshape(-1, 1))
    assert maz_probs.shape == (num_choosers * taz_sample_size, max_maz_count)

    rands = whale.get_rn_generator().random_for_df(chooser_df, n=taz_sample_size)
    rands = rands.reshape(-1, 1)
    assert len(rands) == num_choosers * taz_sample_size
    assert len(rands) == maz_probs.shape[0]

    # make choices
    # positions is array with the chosen alternative represented as a column index in probs
    # which is an integer between zero and max_maz_count
    positions = np.argmax((maz_probs.cumsum(axis=1) - rands) > 0.0, axis=1)

    # shouldn't have chosen any of the dummy pad positions
    assert (positions < maz_counts).all()

    # this take can choose same dest more than once (choice with replacement)
    # this is why you need the groupby later on, which is why the prob is a max
    taz_choices[DEST_MAZ] = maz_sizes["zone_id"].take(positions + first_row_offsets)
    taz_choices["MAZ_prob"] = maz_probs[np.arange(maz_probs.shape[0]), positions]
    taz_choices["prob"] = taz_choices["TAZ_prob"] * taz_choices["MAZ_prob"]

    if have_trace_targets:
        taz_choices_trace_targets = tracing.trace_targets(
            taz_choices, slicer=CHOOSER_ID
        )
        trace_taz_choices_df = taz_choices[taz_choices_trace_targets]
        tracing.trace_df(
            trace_taz_choices_df,
            label=tracing.extend_trace_label(trace_label, "taz_choices"),
            transpose=False,
        )

        lhs_df = trace_taz_choices_df[[CHOOSER_ID, DEST_TAZ] + addtl_col_for_unique_key]
        alt_dest_columns = [f"dest_maz_{c}" for c in range(max_maz_count)]

        # following the same logic as the full code, but for trace cutout
        trace_maz_counts = maz_counts[taz_choices_trace_targets]
        trace_last_row_offsets = maz_counts[taz_choices_trace_targets].cumsum()
        trace_inserts = np.repeat(
            trace_last_row_offsets, max_maz_count - trace_maz_counts
        )

        # trace dest_maz_alts
        padded_maz_sizes = np.insert(
            trace_maz_sizes[CHOOSER_ID].values, trace_inserts, 0.0
        ).reshape(-1, max_maz_count)
        df = pd.DataFrame(
            data=padded_maz_sizes,
            columns=alt_dest_columns,
            index=trace_taz_choices_df.index,
        )
        df = pd.concat([lhs_df, df], axis=1)
        tracing.trace_df(
            df,
            label=tracing.extend_trace_label(trace_label, "dest_maz_alts"),
            transpose=False,
        )

        # trace dest_maz_size_terms
        padded_maz_sizes = np.insert(
            trace_maz_sizes["size_term"].values, trace_inserts, 0.0
        ).reshape(-1, max_maz_count)
        df = pd.DataFrame(
            data=padded_maz_sizes,
            columns=alt_dest_columns,
            index=trace_taz_choices_df.index,
        )
        df = pd.concat([lhs_df, df], axis=1)
        tracing.trace_df(
            df,
            label=tracing.extend_trace_label(trace_label, "dest_maz_size_terms"),
            transpose=False,
        )

        # trace dest_maz_probs
        df = pd.DataFrame(
            data=maz_probs[taz_choices_trace_targets],
            columns=alt_dest_columns,
            index=trace_taz_choices_df.index,
        )
        df = pd.concat([lhs_df, df], axis=1)
        df["rand"] = rands[taz_choices_trace_targets]
        tracing.trace_df(
            df,
            label=tracing.extend_trace_label(trace_label, "dest_maz_probs"),
            transpose=False,
        )

    taz_choices = taz_choices.drop(columns=["TAZ_prob", "MAZ_prob"])
    taz_choices_w_maz = taz_choices.groupby(
        [chooser_id_col, dest_maz_id_col] + addtl_col_for_unique_key
    ).agg(prob=("prob", "first"), pick_count=("prob", "count"))

    taz_choices_w_maz.reset_index(inplace=True)
    taz_choices_w_maz.set_index(chooser_id_col, inplace=True)

    return taz_choices_w_maz


@workflow.func
def od_presample(
    whale: workflow.Whale,
    spec_segment_name,
    choosers,
    model_settings,
    network_los,
    destination_size_terms,
    estimator,
    chunk_size,
    trace_label,
):
    trace_label = tracing.extend_trace_label(trace_label, "presample")
    chunk_tag = "tour_od.presample"

    logger.info(f"{trace_label} od_presample")

    alt_od_col_name = get_od_id_col(ORIG_MAZ, DEST_TAZ)

    MAZ_size_terms, TAZ_size_terms = aggregate_size_terms(
        destination_size_terms, network_los
    )

    # create wrapper with keys for this lookup - in this case there is a ORIG_TAZ
    # in the choosers and a DEST_TAZ in the alternatives which get merged during
    # interaction the skims will be available under the name "skims" for any @ expressions
    skim_dict = network_los.get_skim_dict("taz")
    skims = skim_dict.wrap(ORIG_TAZ, DEST_TAZ)

    orig_MAZ_dest_TAZ_sample = _od_sample(
        spec_segment_name,
        choosers,
        network_los,
        TAZ_size_terms,
        ORIG_MAZ,
        DEST_TAZ,
        skims,
        estimator,
        model_settings,
        alt_od_col_name,
        chunk_size,
        chunk_tag,
        trace_label,
    )

    orig_MAZ_dest_TAZ_sample[ORIG_MAZ] = (
        orig_MAZ_dest_TAZ_sample[alt_od_col_name].str.split("_").str[0].astype(int)
    )
    orig_MAZ_dest_TAZ_sample[DEST_TAZ] = (
        orig_MAZ_dest_TAZ_sample[alt_od_col_name].str.split("_").str[1].astype(int)
    )

    # choose a MAZ for each DEST_TAZ choice, choice probability based on
    # MAZ size_term fraction of TAZ total

    maz_choices = choose_MAZ_for_TAZ(
        whale,
        orig_MAZ_dest_TAZ_sample,
        MAZ_size_terms,
        trace_label,
        addtl_col_for_unique_key=ORIG_MAZ,
    )

    # outputs
    assert DEST_MAZ in maz_choices

    alt_dest_col_name = model_settings["ALT_DEST_COL_NAME"]
    chooser_orig_col_name = model_settings["CHOOSER_ORIG_COL_NAME"]
    maz_choices = maz_choices.rename(
        columns={DEST_MAZ: alt_dest_col_name, ORIG_MAZ: chooser_orig_col_name}
    )

    return maz_choices


class SizeTermCalculator(object):
    """
    convenience object to provide size_terms for a selector (e.g.
    non_mandatory) for various segments (e.g. tour_type or purpose)
    returns size terms for specified segment in df or series form.
    """

    def __init__(self, size_term_selector):
        # do this once so they can request size_terms for various segments (tour_type or purpose)
        land_use = inject.get_table("land_use")
        self.land_use = land_use
        size_terms = whale.get_injectable("size_terms")
        self.destination_size_terms = tour_destination_size_terms(
            self.land_use, size_terms, size_term_selector
        )

        assert not self.destination_size_terms.isna().any(axis=None)

    def omnibus_size_terms_df(self):
        return self.destination_size_terms

    def dest_size_terms_df(self, segment_name, trace_label):
        # return size terms as df with one column named 'size_term'
        # convenient if creating or merging with alts

        size_terms = self.destination_size_terms[[segment_name]].copy()
        size_terms.columns = ["size_term"]

        # FIXME - no point in considering impossible alternatives (where dest size term is zero)
        logger.debug(
            f"SizeTermCalculator dropping {(~(size_terms.size_term > 0)).sum()} "
            f"of {len(size_terms)} rows where size_term is zero for {segment_name}"
        )
        size_terms = size_terms[size_terms.size_term > 0]

        if len(size_terms) == 0:
            logger.warning(
                f"SizeTermCalculator: no zones with non-zero size terms for {segment_name} in {trace_label}"
            )

        return size_terms

    def dest_size_terms_series(self, segment_name):
        # return size terms as as series
        # convenient (and no copy overhead) if reindexing and assigning into alts column
        return self.destination_size_terms[segment_name]


def run_od_sample(
    whale,
    spec_segment_name,
    tours,
    model_settings,
    network_los,
    destination_size_terms,
    estimator,
    chunk_size,
    trace_label,
):
    model_spec = simulate.spec_for_segment(
        model_settings,
        spec_id="SAMPLE_SPEC",
        segment_name=spec_segment_name,
        estimator=estimator,
    )

    choosers = tours
    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings["SIMULATE_CHOOSER_COLUMNS"]
    choosers = choosers[chooser_columns]

    # interaction_sample requires that choosers.index.is_monotonic_increasing
    if not choosers.index.is_monotonic_increasing:
        logger.debug(
            f"run_destination_sample {trace_label} sorting choosers because not monotonic_increasing"
        )
        choosers = choosers.sort_index()

    # by default, enable presampling for multizone systems, unless they disable it in settings file
    pre_sample_taz = not (network_los.zone_system == los.ONE_ZONE)
    if pre_sample_taz and not whale.settings.want_dest_choice_presampling:
        pre_sample_taz = False
        logger.info(
            f"Disabled destination zone presampling for {trace_label} "
            f"because 'want_dest_choice_presampling' setting is False"
        )

    if pre_sample_taz:
        logger.info(
            "Running %s destination_presample with %d tours" % (trace_label, len(tours))
        )

        choices = od_presample(
            whale,
            spec_segment_name,
            choosers,
            model_settings,
            network_los,
            destination_size_terms,
            estimator,
            chunk_size,
            trace_label,
        )

    else:
        choices = od_sample(
            spec_segment_name,
            choosers,
            model_settings,
            network_los,
            destination_size_terms,
            estimator,
            chunk_size,
            trace_label,
        )

    return choices


def run_od_logsums(
    whale: workflow.Whale,
    spec_segment_name,
    tours_merged_df,
    od_sample,
    model_settings,
    network_los,
    estimator,
    chunk_size,
    trace_hh_id,
    trace_label,
):
    """
    add logsum column to existing tour_destination_sample table

    logsum is calculated by running the mode_choice model for each sample
    (person, OD_id) pair in od_sample, and computing the logsum of all the utilities
    """
    chunk_tag = "tour_od.logsums"
    logsum_settings = whale.filesystem.read_model_settings(
        model_settings["LOGSUM_SETTINGS"]
    )
    origin_id_col = model_settings["ORIG_COL_NAME"]
    dest_id_col = model_settings["DEST_COL_NAME"]
    tour_od_id_col = get_od_id_col(origin_id_col, dest_id_col)

    # FIXME - MEMORY HACK - only include columns actually used in spec
    tours_merged_df = logsum.filter_chooser_columns(
        tours_merged_df, logsum_settings, model_settings
    )

    # merge ods into choosers table
    choosers = od_sample.join(tours_merged_df, how="left")
    choosers[tour_od_id_col] = (
        choosers[origin_id_col].astype(str) + "_" + choosers[dest_id_col].astype(str)
    )

    logger.info("Running %s with %s rows", trace_label, len(choosers))

    tracing.dump_df(DUMP, choosers, trace_label, "choosers")

    # run trip mode choice to compute tour mode choice logsums
    if logsum_settings.get("COMPUTE_TRIP_MODE_CHOICE_LOGSUMS", False):
        pseudo_tours = choosers.copy()
        trip_mode_choice_settings = whale.filesystem.read_model_settings(
            "trip_mode_choice"
        )

        # tours_merged table doesn't yet have all the cols it needs to be called (e.g.
        # home_zone_id), so in order to compute tour mode choice/trip mode choice logsums
        # in this step we have to pass all tour-level attributes in with the main trips
        # table. see trip_mode_choice.py L56-61 for more details.
        tour_cols_needed = trip_mode_choice_settings.get(
            "TOURS_MERGED_CHOOSER_COLUMNS", []
        )
        tour_cols_needed.append(tour_od_id_col)

        # from tour_mode_choice.py
        not_university = (
            pseudo_tours.tour_type != "school"
        ) | ~pseudo_tours.is_university
        pseudo_tours["tour_purpose"] = pseudo_tours.tour_type.where(
            not_university, "univ"
        )

        pseudo_tours["stop_frequency"] = "0out_0in"
        pseudo_tours["primary_purpose"] = pseudo_tours["tour_purpose"]
        choosers_og_index = choosers.index.name
        pseudo_tours.reset_index(inplace=True)
        pseudo_tours.index.name = "unique_id"

        # need dest_id_col to create dest col in trips, but need to preserve
        # tour dest as separate column in the trips table bc the trip mode choice
        # preprocessor isn't able to get the tour dest from the tours table bc the
        # tours don't yet have ODs.
        stop_frequency_alts = whale.get_injectable("stop_frequency_alts")
        pseudo_tours["tour_destination"] = pseudo_tours[dest_id_col]
        trips = trip.initialize_from_tours(
            pseudo_tours,
            stop_frequency_alts,
            [origin_id_col, dest_id_col, "tour_destination", "unique_id"],
        )
        outbound = trips["outbound"]
        trips["depart"] = reindex(pseudo_tours.start, trips.unique_id)
        trips.loc[~outbound, "depart"] = reindex(
            pseudo_tours.end, trips.loc[~outbound, "unique_id"]
        )

        logsum_trips = pd.DataFrame()
        nest_spec = config.get_logit_model_settings(logsum_settings)

        # actual coeffs dont matter here, just need them to load the nest structure
        coefficients = whale.filesystem.get_segment_coefficients(
            logsum_settings, pseudo_tours.iloc[0]["tour_purpose"]
        )
        nest_spec = simulate.eval_nest_coefficients(
            nest_spec, coefficients, trace_label
        )
        tour_mode_alts = []
        for nest in logit.each_nest(nest_spec):
            if nest.is_leaf:
                tour_mode_alts.append(nest.name)

        # repeat rows from the trips table iterating over tour mode
        for tour_mode in tour_mode_alts:
            trips["tour_mode"] = tour_mode
            logsum_trips = pd.concat((logsum_trips, trips), ignore_index=True)
        assert len(logsum_trips) == len(trips) * len(tour_mode_alts)
        logsum_trips.index.name = "trip_id"

        for col in tour_cols_needed:
            if col not in trips:
                logsum_trips[col] = reindex(pseudo_tours[col], logsum_trips.unique_id)

        whale.add_table("trips", logsum_trips)
        tracing.register_traceable_table(whale, "trips", logsum_trips)
        whale.get_rn_generator().add_channel("trips", logsum_trips)

        # run trip mode choice on pseudo-trips. use orca instead of pipeline to
        # execute the step because pipeline can only handle one open step at a time
        orca.run(["trip_mode_choice"])

        # grab trip mode choice logsums and pivot by tour mode and direction, index
        # on tour_id to enable merge back to choosers table
        trips = whale.get_dataframe("trips")
        trip_dir_mode_logsums = trips.pivot(
            index=["tour_id", tour_od_id_col],
            columns=["tour_mode", "outbound"],
            values="trip_mode_choice_logsum",
        )
        new_cols = [
            "_".join(["logsum", mode, "outbound" if outbound else "inbound"])
            for mode, outbound in trip_dir_mode_logsums.columns
        ]
        trip_dir_mode_logsums.columns = new_cols

        choosers.reset_index(inplace=True)
        choosers.set_index(["tour_id", tour_od_id_col], inplace=True)
        choosers = pd.merge(
            choosers, trip_dir_mode_logsums, left_index=True, right_index=True
        )
        choosers.reset_index(inplace=True)
        choosers.set_index(choosers_og_index, inplace=True)

        whale.get_rn_generator().drop_channel("trips")
        tracing.deregister_traceable_table(whale, "trips")

        assert (od_sample.index == choosers.index).all()
        for col in new_cols:
            od_sample[col] = choosers[col]

    logsums = logsum.compute_logsums(
        whale,
        choosers,
        spec_segment_name,
        logsum_settings,
        model_settings,
        network_los,
        chunk_size,
        chunk_tag,
        trace_label,
        "end",
        "start",
        "duration",
    )

    assert (od_sample.index == logsums.index).all()
    od_sample["tour_mode_choice_logsum"] = logsums

    return od_sample


def run_od_simulate(
    whale: workflow.Whale,
    spec_segment_name,
    tours,
    od_sample,
    want_logsums,
    model_settings,
    network_los,
    destination_size_terms,
    estimator,
    chunk_size,
    trace_label,
):
    """
    run simulate OD choices on tour_od_sample annotated with mode_choice
    logsum to select a tour OD from sample alternatives
    """

    model_spec = simulate.spec_for_segment(
        model_settings,
        spec_id="SPEC",
        segment_name=spec_segment_name,
        estimator=estimator,
    )

    # merge persons into tours
    choosers = tours

    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings["SIMULATE_CHOOSER_COLUMNS"]
    choosers = choosers[chooser_columns]

    # interaction_sample requires that choosers.index.is_monotonic_increasing
    if not choosers.index.is_monotonic_increasing:
        logger.debug(
            f"run_destination_simulate {trace_label} sorting choosers because not monotonic_increasing"
        )
        choosers = choosers.sort_index()

    if estimator:
        estimator.write_choosers(choosers)

    origin_col_name = model_settings["ORIG_COL_NAME"]
    dest_col_name = model_settings["DEST_COL_NAME"]
    alt_dest_col_name = model_settings["ALT_DEST_COL_NAME"]
    origin_attr_cols = model_settings["ORIGIN_ATTR_COLS_TO_USE"]

    alt_od_col_name = get_od_id_col(origin_col_name, dest_col_name)
    od_sample[alt_od_col_name] = create_od_id_col(
        od_sample, origin_col_name, dest_col_name
    )

    # alternatives are pre-sampled and annotated with logsums and pick_count
    # but we have to merge size_terms column into alt sample list
    od_sample["size_term"] = reindex(
        destination_size_terms.size_term, od_sample[alt_dest_col_name]
    )

    # also have to add origin attribute columns
    lu = inject.get_table("land_use").to_frame(columns=origin_attr_cols)
    od_sample = pd.merge(
        od_sample, lu, left_on=origin_col_name, right_index=True, how="left"
    )

    tracing.dump_df(DUMP, od_sample, trace_label, "alternatives")

    constants = config.get_model_constants(model_settings)

    logger.info("Running tour_destination_simulate with %d persons", len(choosers))

    # create wrapper with keys for this lookup - in this case there is an origin ID
    # column and a destination ID columns in the alternatives table.
    # the skims will be available under the name "skims" for any @ expressions
    skim_dict = network_los.get_default_skim_dict()
    skims = skim_dict.wrap(origin_col_name, dest_col_name)

    locals_d = {
        "skims": skims,
        "timeframe": "timeless",
        "orig_col_name": origin_col_name,
        "dest_col_name": dest_col_name,
    }
    if constants is not None:
        locals_d.update(constants)

    tracing.dump_df(DUMP, choosers, trace_label, "choosers")
    choices = interaction_sample_simulate(
        whale,
        choosers,
        od_sample,
        spec=model_spec,
        choice_column=alt_od_col_name,
        want_logsums=want_logsums,
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=trace_label,
        trace_choice_name="origin_destination",
        estimator=estimator,
    )

    if not want_logsums:
        choices = choices.to_frame("choice")

    choices = _get_od_cols_from_od_id(choices, origin_col_name, dest_col_name)

    return choices


def run_tour_od(
    whale,
    tours,
    persons,
    want_logsums,
    want_sample_table,
    model_settings,
    network_los,
    estimator,
    chunk_size,
    trace_hh_id,
    trace_label,
):
    size_term_calculator = SizeTermCalculator(model_settings["SIZE_TERM_SELECTOR"])
    preprocessor_settings = model_settings.get("preprocessor", None)
    origin_col_name = model_settings["ORIG_COL_NAME"]

    chooser_segment_column = model_settings["CHOOSER_SEGMENT_COLUMN_NAME"]

    # maps segment names to compact (integer) ids
    segments = model_settings["SEGMENTS"]

    # interaction_sample_simulate insists choosers appear in same order as alts
    tours = tours.sort_index()

    choices_list = []
    sample_list = []
    for segment_name in segments:
        choosers = tours[tours[chooser_segment_column] == segment_name]

        choosers = pd.merge(
            choosers,
            persons.to_frame(columns=["is_university", "demographic_segment"]),
            left_on="person_id",
            right_index=True,
        )

        # - annotate choosers
        if preprocessor_settings:
            expressions.assign_columns(
                whale,
                df=choosers,
                model_settings=preprocessor_settings,
                trace_label=trace_label,
            )

        # size_term segment is segment_name
        segment_destination_size_terms = size_term_calculator.dest_size_terms_df(
            segment_name, trace_label
        )

        if choosers.shape[0] == 0:
            logger.info(
                "%s skipping segment %s: no choosers", trace_label, segment_name
            )
            continue

        # - od_sample
        spec_segment_name = segment_name  # spec_segment_name is segment_name

        od_sample_df = run_od_sample(
            whale,
            spec_segment_name,
            choosers,
            model_settings,
            network_los,
            segment_destination_size_terms,
            estimator,
            chunk_size=chunk_size,
            trace_label=tracing.extend_trace_label(
                trace_label, "sample.%s" % segment_name
            ),
        )

        if model_settings["ORIG_FILTER"] == "original_MAZ > 0":
            pass
        elif model_settings["ORIG_FILTER"] == "external_TAZ > 0":
            # sampled alts using internal mazs, so now we
            # have to convert to using the external tazs
            od_sample_df[origin_col_name] = map_maz_to_ext_maz(
                od_sample_df[origin_col_name]
            )
        else:
            raise ValueError(
                "Not sure how you identified tour origins but you probably need "
                "to choose a different ORIG_FILTER setting"
            )

        # - destination_logsums
        od_sample_df = run_od_logsums(
            whale,
            spec_segment_name,
            choosers,
            od_sample_df,
            model_settings,
            network_los,
            estimator,
            chunk_size=chunk_size,
            trace_hh_id=trace_hh_id,
            trace_label=tracing.extend_trace_label(
                trace_label, "logsums.%s" % segment_name
            ),
        )

        # - od_simulate
        choices = run_od_simulate(
            spec_segment_name,
            choosers,
            od_sample_df,
            want_logsums=want_logsums,
            model_settings=model_settings,
            network_los=network_los,
            destination_size_terms=segment_destination_size_terms,
            estimator=estimator,
            chunk_size=chunk_size,
            trace_label=tracing.extend_trace_label(
                trace_label, "simulate.%s" % segment_name
            ),
        )

        choices_list.append(choices)
        if estimator:
            assert estimator.want_unsampled_alternatives

        if want_sample_table:
            # FIXME - sample_table
            od_sample_df.set_index(
                model_settings["ALT_DEST_COL_NAME"], append=True, inplace=True
            )
            sample_list.append(od_sample_df)

        # FIXME - want to do this here?
        del od_sample_df

    if len(choices_list) > 0:
        choices_df = pd.concat(choices_list)

    if len(sample_list) > 0:
        save_sample_df = pd.concat(sample_list)
    else:
        # this could happen either with small samples as above, or if no saved sample desired
        save_sample_df = None

    return choices_df, save_sample_df
