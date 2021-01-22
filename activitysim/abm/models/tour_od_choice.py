# ActivitySim
# See full license in LICENSE.txt.
import logging

import pandas as pd

from activitysim.core import tracing
from activitysim.core import config
from activitysim.core import inject
from activitysim.core import pipeline
from activitysim.core import simulate

from activitysim.core.util import assign_in_place

from .util import tour_od
from .util import estimation


logger = logging.getLogger(__name__)


@inject.step()
def tour_od_choice(
        tours,
        network_los,
        chunk_size,
        trace_hh_id):

    """
    Given the tour generation from the above, each tour needs to have a
    destination, so in this case tours are the choosers (with the associated
    person that's making the tour)
    """

    trace_label = 'tour_od_choice'
    model_settings_file_name = 'tour_od_choice.yaml'
    model_settings = config.read_model_settings(model_settings_file_name)
    choice_column = model_settings['DEST_CHOICE_COLUMN_NAME']
    origin_col_name = model_settings['ORIG_COL_NAME']
    dest_col_name = model_settings['DEST_COL_NAME']

    sample_table_name = model_settings.get('DEST_CHOICE_SAMPLE_TABLE_NAME')
    want_sample_table = config.setting('want_dest_choice_sample_tables') and sample_table_name is not None

    tours = tours.to_frame()

    # interaction_sample_simulate insists choosers appear in same order as alts
    tours = tours.sort_index()

    estimator = estimation.manager.begin_estimation('tour_od_choice')
    if estimator:
        estimator.write_coefficients(simulate.read_model_coefficients(model_settings))
        # estimator.write_spec(model_settings, tag='SAMPLE_SPEC')
        estimator.write_spec(model_settings, tag='SPEC')
        estimator.set_alt_id(model_settings["ALT_DEST_COL_NAME"])
        estimator.write_table(inject.get_injectable('size_terms'), 'size_terms', append=False)
        estimator.write_table(inject.get_table('land_use').to_frame(), 'landuse', append=False)
        estimator.write_model_settings(model_settings, model_settings_file_name)

    choices_df, save_sample_df = tour_od.run_tour_od(
        tours,
        want_sample_table,
        model_settings,
        network_los,
        estimator,
        chunk_size, trace_hh_id, trace_label)

    if estimator:
        estimator.write_choices(choices_df.choice)
        choices_df.choice = estimator.get_survey_values(choices_df.choice, 'tours', 'destination')
        estimator.write_override_choices(choices_df.choice)
        estimator.end_estimation()

    tours[choice_column] = choices_df.choice
    tours[origin_col_name] = choices_df[origin_col_name]
    tours[dest_col_name] = choices_df[dest_col_name]

    pipeline.replace_table("tours", tours)

    if want_sample_table:
        assert len(save_sample_df.index.get_level_values(0).unique()) == len(choices_df)
        pipeline.extend_table(sample_table_name, save_sample_df)

    if trace_hh_id:
        tracing.trace_df(tours,
                         label="tours_od_choice",
                         slicer='person_id',
                         index_label='tour',
                         columns=None,
                         warn_if_empty=True)