# ActivitySim
# See full license in LICENSE.txt.
from __future__ import (absolute_import, division, print_function, )
from future.standard_library import install_aliases
install_aliases()  # noqa: E402

import os
import logging
import warnings

import numpy as np
import pandas as pd

from activitysim.abm.tables import constants

from activitysim.core import tracing
from activitysim.core import config
from activitysim.core import assign
from activitysim.core import inject
from activitysim.core import simulate

from activitysim.core.util import other_than
from activitysim.core.util import assign_in_place
from activitysim.core import util


logger = logging.getLogger(__name__)


def reindex_i(series1, series2, dtype=np.int8):
    """
    version of reindex that replaces missing na values and converts to int
    helpful in expression files that compute counts (e.g. num_work_tours)
    """
    return util.reindex(series1, series2).fillna(0).astype(dtype)


def local_utilities():
    """
    Dict of useful modules and functions to provides as locals for use in eval of expressions

    Returns
    -------
    utility_dict : dict
        name, entity pairs of locals
    """

    utility_dict = {
        'pd': pd,
        'np': np,
        'constants': constants,
        'reindex': util.reindex,
        'reindex_i': reindex_i,
        'setting': config.setting,
        'skim_time_period_label': skim_time_period_label,
        'other_than': other_than,
        'skim_dict': inject.get_injectable('skim_dict', None)
    }

    return utility_dict


def compute_columns(df, model_settings, locals_dict={}, trace_label=None):
    """
    Evaluate expressions_spec in context of df, with optional additional pipeline tables in locals

    Parameters
    ----------
    df : pandas DataFrame
        or if None, expect name of pipeline table to be specified by DF in model_settings
    model_settings : dict or str
        dict with keys:
            DF - df_alias and (additionally, if df is None) name of pipeline table to load as df
            SPEC - name of expressions file (csv suffix optional) if different from model_settings
            TABLES - list of pipeline tables to load and make available as (read only) locals
        str:
            name of yaml file in configs_dir to load dict from
    locals_dict : dict
        dict of locals (e.g. utility functions) to add to the execution environment
    trace_label

    Returns
    -------
    results: pandas.DataFrame
        one column for each expression (except temps with ALL_CAP target names)
        same index as df
    """

    if isinstance(model_settings, str):
        model_settings_name = model_settings
        model_settings = config.read_model_settings('%s.yaml' % model_settings)
        assert model_settings, "Found no model settings for %s" % model_settings_name
    else:
        model_settings_name = 'dict'
        assert isinstance(model_settings, dict)

    assert 'DF' in model_settings, \
        "Expected to find 'DF' in %s" % model_settings_name

    df_name = model_settings.get('DF')
    helper_table_names = model_settings.get('TABLES', [])
    expressions_spec_name = model_settings.get('SPEC', model_settings_name)

    assert expressions_spec_name is not None, \
        "Expected to find 'SPEC' in %s" % model_settings_name

    trace_label = tracing.extend_trace_label(trace_label or '', expressions_spec_name)

    if not expressions_spec_name.endswith(".csv"):
        expressions_spec_name = '%s.csv' % expressions_spec_name
    expressions_spec = assign.read_assignment_spec(config.config_file_path(expressions_spec_name))

    assert expressions_spec.shape[0] > 0, \
        "Expected to find some assignment expressions in %s" % expressions_spec_name

    tables = {t: inject.get_table(t).to_frame() for t in helper_table_names}

    # if df was passed in, df might be a slice, or any other table, but DF is it's local alias
    assert df_name not in tables, "Did not expect to find df '%s' in TABLES" % df_name
    tables[df_name] = df

    # be nice and also give it to them as df?
    tables['df'] = df

    _locals_dict = local_utilities()
    _locals_dict.update(locals_dict)
    _locals_dict.update(tables)

    results, trace_results, trace_assigned_locals \
        = assign.assign_variables(expressions_spec,
                                  df,
                                  _locals_dict,
                                  trace_rows=tracing.trace_targets(df))

    if trace_results is not None:
        tracing.trace_df(trace_results,
                         label=trace_label,
                         slicer='NONE')

    if trace_assigned_locals:
        tracing.write_csv(trace_assigned_locals, file_name="%s_locals" % trace_label)

    return results


def assign_columns(df, model_settings, locals_dict={}, trace_label=None):
    """
    Evaluate expressions in context of df and assign resulting target columns to df

    Can add new or modify existing columns (if target same as existing df column name)

    Parameters - same as for compute_columns except df must not be None
    Returns - nothing since we modify df in place
    """

    assert df is not None
    assert model_settings is not None

    results = compute_columns(df, model_settings, locals_dict, trace_label)

    assign_in_place(df, results)


# ##################################################################################################
# helpers
# ##################################################################################################

def skim_time_period_label(time_period):
    """
    convert time period times to skim time period labels (e.g. 9 -> 'AM')

    Parameters
    ----------
    time_period : pandas Series

    Returns
    -------
    pandas Series
        string time period labels
    """

    skim_time_periods = config.setting('skim_time_periods')

    # Default to 60 minute time periods
    period_minutes = 60
    if 'period_minutes' in skim_time_periods.keys():
        period_minutes = skim_time_periods['period_minutes']

    # Default to a day
    model_time_window_min = 1440
    if ('time_window') in skim_time_periods.keys():
        model_time_window_min = skim_time_periods['time_window']

    # Check to make sure the intervals result in no remainder time throught 24 hour day
    assert 0 == model_time_window_min % period_minutes
    total_periods = model_time_window_min / period_minutes

    # FIXME - eventually test and use np version always?
    period_label = 'periods'
    if 'hours' in skim_time_periods.keys():
        period_label = 'hours'
        warnings.warn('`skim_time_periods` key `hours` in settings.yml will be removed in '
                      'future verions. Use `periods` instead',
                      FutureWarning)

    if np.isscalar(time_period):
        bin = np.digitize([time_period % total_periods],
                          skim_time_periods[period_label], right=True)[0] - 1
        return skim_time_periods['labels'][bin]

    return pd.cut(time_period, skim_time_periods[period_label],
                  labels=skim_time_periods['labels'], right=True).astype(str)


def annotate_preprocessors(
        tours_df, locals_dict, skims,
        model_settings, trace_label):

    locals_d = {}
    locals_d.update(locals_dict)
    locals_d.update(skims)

    preprocessor_settings = model_settings.get('preprocessor', [])
    if not isinstance(preprocessor_settings, list):
        assert isinstance(preprocessor_settings, dict)
        preprocessor_settings = [preprocessor_settings]

    simulate.set_skim_wrapper_targets(tours_df, skims)

    annotations = None
    for model_settings in preprocessor_settings:

        results = compute_columns(
            df=tours_df,
            model_settings=model_settings,
            locals_dict=locals_d,
            trace_label=trace_label)

        assign_in_place(tours_df, results)


def filter_chooser_columns(choosers, chooser_columns):

    missing_columns = [c for c in chooser_columns if c not in choosers]
    if missing_columns:
        logger.debug("filter_chooser_columns missing_columns %s" % missing_columns)

    # ignore any columns not appearing in choosers df
    chooser_columns = [c for c in chooser_columns if c in choosers]

    choosers = choosers[chooser_columns]
    return choosers
