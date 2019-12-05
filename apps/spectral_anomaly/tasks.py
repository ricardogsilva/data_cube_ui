from django.db.models import F
from celery.task import task
from celery import chain, group, chord
from celery.utils.log import get_task_logger
from datetime import datetime, timedelta
import shutil
import xarray as xr
import numpy as np
from xarray.ufuncs import logical_or as xr_or
from xarray.ufuncs import logical_and as xr_and
from xarray.ufuncs import logical_not as xr_not
import os

from utils.data_cube_utilities.data_access_api import DataAccessApi
from utils.data_cube_utilities.dc_utilities import (create_cfmask_clean_mask, create_bit_mask, write_geotiff_from_xr,
                                                    write_png_from_xr, add_timestamp_data_to_xr, clear_attrs)
from utils.data_cube_utilities.dc_chunker import (create_geographic_chunks, create_time_chunks,
                                                  combine_geographic_chunks)
from utils.data_cube_utilities.clean_mask import landsat_clean_mask_invalid
from apps.dc_algorithm.utils import create_2d_plot
from utils.data_cube_utilities.import_export import export_xarray_to_netcdf

from .models import SpectralAnomalyTask
from apps.dc_algorithm.models import Satellite
from apps.dc_algorithm.tasks import DCAlgorithmBase, check_cancel_task, task_clean_up

import matplotlib.pyplot as plt
import matplotlib as mpl

from utils.data_cube_utilities.dc_ndvi_anomaly import NDVI, EVI
from utils.data_cube_utilities.dc_water_classifier import NDWI
from utils.data_cube_utilities.urbanization import NDBI
from utils.data_cube_utilities.dc_fractional_coverage_classifier import frac_coverage_classify

logger = get_task_logger(__name__)


spectral_indices_function_map = {
    'ndvi': NDVI, 'ndwi': NDWI,
    'ndbi': NDBI, 'evi': EVI,
    'fractional_cover': frac_coverage_classify
}
spectral_indices_range_map = {
    'ndvi': (-1, 1), 'ndwi': (-1, 1),
    'ndbi': (-1, 1), 'evi': (-1, 1),
    'fractional_cover': (0, 100)
}
spectral_indices_name_map = {
    'ndvi': 'NDVI', 'ndwi': 'NDWI',
    'ndbi': 'NDBI', 'evi': 'EVI',
    'fractional_cover': 'Fractional Cover'
}


class BaseTask(DCAlgorithmBase):
    app_name = 'spectral_anomaly'


@task(name="spectral_anomaly.run", base=BaseTask)
def run(task_id=None):
    """Responsible for launching task processing using celery asynchronous processes

    Chains the parsing of parameters, validation, chunking, and the start to data processing.
    """
    return chain(parse_parameters_from_task.s(task_id=task_id),
                 validate_parameters.s(task_id=task_id),
                 perform_task_chunking.s(task_id=task_id),
                 start_chunk_processing.s(task_id=task_id))()


@task(name="spectral_anomaly.parse_parameters_from_task", base=BaseTask, bind=True)
def parse_parameters_from_task(self, task_id=None):
    """Parse out required DC parameters from the task model.

    See the DataAccessApi docstrings for more information.
    Parses out platforms, products, etc. to be used with DataAccessApi calls.

    If this is a multisensor app, platform and product should be pluralized and used
    with the get_stacked_datasets_by_extent call rather than the normal get.

    Returns:
        parameter dict with all keyword args required to load data.

    """
    task = SpectralAnomalyTask.objects.get(pk=task_id)

    parameters = {
        'platform': task.satellite.datacube_platform,
        'product': task.satellite.get_product(task.area_id),
        'time': (task.time_start, task.time_end),
        'baseline_time': (task.baseline_time_start, task.baseline_time_end),
        'analysis_time': (task.analysis_time_start, task.analysis_time_end),
        'longitude': (task.longitude_min, task.longitude_max),
        'latitude': (task.latitude_min, task.latitude_max),
        'measurements': task.satellite.get_measurements(),
        'composite_range': (task.composite_threshold_min, task.composite_threshold_max),
        'change_range': (task.change_threshold_min, task.change_threshold_max),
    }

    task.execution_start = datetime.now()
    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Parsed out parameters.")

    return parameters


@task(name="spectral_anomaly.validate_parameters", base=BaseTask, bind=True)
def validate_parameters(self, parameters, task_id=None):
    """Validate parameters generated by the parameter parsing task

    All validation should be done here - are there data restrictions?
    Combinations that aren't allowed? etc.

    Returns:
        parameter dict with all keyword args required to load data.
        -or-
        updates the task with ERROR and a message, returning None

    """
    task = SpectralAnomalyTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    dc = DataAccessApi(config=task.config_path)

    baseline_parameters = parameters.copy()
    baseline_parameters['time'] = parameters['baseline_time']
    baseline_acquisitions = dc.list_acquisition_dates(**baseline_parameters)

    analysis_parameters = parameters.copy()
    analysis_parameters['time'] = parameters['analysis_time']
    analysis_acquisitions = dc.list_acquisition_dates(**analysis_parameters)

    if len(baseline_acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquisitions for this parameter set "
                                    "for the baseline time period.")
        return None

    if len(analysis_acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquisitions for this parameter set "
                                    "for the analysis time period.")
        return None

    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Validated parameters.")

    if not dc.validate_measurements(parameters['product'], parameters['measurements']):
        task.complete = True
        task.update_status(
            "ERROR",
            "The provided Satellite model measurements aren't valid for the product. Please check the measurements listed in the {} model.".
                format(task.satellite.name))
        return None

    dc.close()
    return parameters


@task(name="spectral_anomaly.perform_task_chunking", base=BaseTask, bind=True)
def perform_task_chunking(self, parameters, task_id=None):
    """Chunk parameter sets into more manageable sizes

    Uses functions provided by the task model to create a group of
    parameter sets that make up the arg.

    Args:
        parameters: parameter stream containing all kwargs to load data

    Returns:
        parameters with a list of geographic and time ranges
    """
    if parameters is None:
        return None

    task = SpectralAnomalyTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    dc = DataAccessApi(config=task.config_path)
    task_chunk_sizing = task.get_chunk_size()

    geographic_chunks = create_geographic_chunks(
        longitude=parameters['longitude'],
        latitude=parameters['latitude'],
        geographic_chunk_size=task_chunk_sizing['geographic'])

    # This app does not currently support time chunking.

    dc.close()
    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Chunked parameter set.")

    return {'parameters': parameters, 'geographic_chunks': geographic_chunks}


@task(name="spectral_anomaly.start_chunk_processing", base=BaseTask, bind=True)
def start_chunk_processing(self, chunk_details, task_id=None):
    """Create a fully asyncrhonous processing pipeline from paramters and a list of chunks.

    The most efficient way to do this is to create a group of time chunks for each geographic chunk,
    recombine over the time index, then combine geographic last.
    If we create an animation, this needs to be reversed - e.g. group of geographic for each time,
    recombine over geographic, then recombine time last.

    The full processing pipeline is completed, then the create_output_products task is triggered, completing the task.
    """
    if chunk_details is None:
        return None

    parameters = chunk_details.get('parameters')
    geographic_chunks = chunk_details.get('geographic_chunks')

    task = SpectralAnomalyTask.objects.get(pk=task_id)

    api = DataAccessApi(config=task.config_path)

    # Get an estimate of the amount of work to be done: the number of scenes
    # to process, also considering intermediate chunks to be combined.
    # Determine the number of scenes for the baseline and analysis extents.
    num_scenes = {}
    params_temp = parameters.copy()
    for composite_name in ['baseline', 'analysis']:
        num_scenes[composite_name] = 0
        for geographic_chunk in geographic_chunks:
            params_temp.update(geographic_chunk)
            params_temp['measurements'] = []
            # Use the corresponding time range for the baseline and analysis data.
            params_temp['time'] = \
                params_temp['baseline_time' if composite_name == 'baseline' else 'analysis_time']
            params_temp_clean = params_temp.copy()
            del params_temp_clean['baseline_time'], params_temp_clean['analysis_time'], \
                params_temp_clean['composite_range'], params_temp_clean['change_range']
            data = api.dc.load(**params_temp_clean)
            if 'time' in data.coords:
                num_scenes[composite_name] += len(data.time)
    # The number of scenes per geographic chunk for baseline and analysis extents.
    num_scn_per_chk_geo = {k: round(v/len(geographic_chunks)) for k, v in num_scenes.items()}
    # Scene processing progress is tracked in processing_task().
    task.total_scenes = sum(num_scenes.values())
    task.scenes_processed = 0
    task.save(update_fields=['total_scenes', 'scenes_processed'])

    if check_cancel_task(self, task): return
    task.update_status("WAIT", "Starting processing.")

    processing_pipeline = (group([
            processing_task.s(
                task_id=task_id,
                geo_chunk_id=geo_index,
                geographic_chunk=geographic_chunk,
                num_scn_per_chk=num_scn_per_chk_geo,
                **parameters) for geo_index, geographic_chunk in enumerate(geographic_chunks)
    ]) | recombine_geographic_chunks.s(task_id=task_id) | create_output_products.s(task_id=task_id) \
       | task_clean_up.si(task_id=task_id, task_model='SpectralAnomalyTask')).apply_async()

    return True


@task(name="spectral_anomaly.processing_task", acks_late=True, base=BaseTask, bind=True)
def processing_task(self,
                    task_id=None,
                    geo_chunk_id=None,
                    geographic_chunk=None,
                    num_scn_per_chk=None,
                    **parameters):
    """Process a parameter set and save the results to disk.

    Uses the geographic and time chunk id to identify output products.
    **params is updated with time and geographic ranges then used to load data.
    the task model holds the iterative property that signifies whether the algorithm
    is iterative or if all data needs to be loaded at once.

    Args:
        task_id, geo_chunk_id: identification for the main task and what chunk this is processing
        geographic_chunk: range of latitude and longitude to load - dict with keys latitude, longitude
        num_scn_per_chk: A dictionary of the number of scenes per chunk for the baseline
                         and analysis extents. Used to determine task progress.
        parameters: all required kwargs to load data.

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    chunk_id = str(geo_chunk_id)
    task = SpectralAnomalyTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    if not os.path.exists(task.get_temp_path()):
        return None

    metadata = {}

    # For both the baseline and analysis time ranges for this
    # geographic chunk, load, calculate the spectral index, composite,
    # and filter the data according to user-supplied parameters -
    # recording where the data was out of the filter's range so we can
    # create the output product (an image).
    dc = DataAccessApi(config=task.config_path)
    updated_params = parameters
    updated_params.update(geographic_chunk)
    spectral_index = task.query_type.result_id
    composites = {}
    composites_out_of_range = {}
    no_data_value = task.satellite.no_data_value
    for composite_name in ['baseline', 'analysis']:
        if check_cancel_task(self, task): return

        # Use the corresponding time range for the baseline and analysis data.
        updated_params['time'] = \
            updated_params['baseline_time' if composite_name == 'baseline' else 'analysis_time']
        time_column_data = dc.get_dataset_by_extent(**updated_params)
        # If this geographic chunk is outside the data extents, return None.
        if len(time_column_data.dims) == 0: return None

        # Obtain the clean mask for the satellite.
        time_column_clean_mask = task.satellite.get_clean_mask_func()(time_column_data)
        measurements_list = task.satellite.measurements.replace(" ", "").split(",")
        # Obtain the mask for valid Landsat values.
        time_column_invalid_mask = landsat_clean_mask_invalid(time_column_data).values
        # Also exclude data points with the no_data value.
        no_data_mask = time_column_data[measurements_list[0]].values != no_data_value
        # Combine the clean masks.
        time_column_clean_mask = time_column_clean_mask | time_column_invalid_mask | no_data_mask

        # Obtain the composite.
        composite = task.get_processing_method()(time_column_data,
                                                 clean_mask=time_column_clean_mask,
                                                 no_data=task.satellite.no_data_value)
        # Obtain the mask for valid Landsat values.
        composite_invalid_mask = landsat_clean_mask_invalid(composite).values
        # Also exclude data points with the no_data value via the compositing mask.
        composite_no_data_mask = composite[measurements_list[0]].values != no_data_value
        composite_clean_mask = composite_invalid_mask | composite_no_data_mask

        # Compute the spectral index for the composite.
        spec_ind_params = dict()
        if spectral_index == 'fractional_cover':
            spec_ind_params = dict(clean_mask=composite_clean_mask, no_data=no_data_value)
        spec_ind_result = spectral_indices_function_map[spectral_index](composite, **spec_ind_params)
        if spectral_index in ['ndvi', 'ndbi', 'ndwi', 'evi']:
            composite[spectral_index] = spec_ind_result
        else:  # Fractional Cover
            composite = xr.merge([composite, spec_ind_result])
            # Fractional Cover is supposed to have a range of [0, 100], with its bands -
            # 'bs', 'pv', and 'npv' - summing to 100. However, the function we use
            # can have the sum of those bands as high as 106.
            # frac_cov_min, frac_cov_max = spectral_indices_range_map[spectral_index]
            frac_cov_min, frac_cov_max = 0, 106
            for band in ['bs', 'pv', 'npv']:
                composite[band].values = \
                    np.interp(composite[band].values, (frac_cov_min, frac_cov_max),
                              spectral_indices_range_map[spectral_index])

        composites[composite_name] = composite

        # Determine where the composite is out of range.
        # We rename the resulting xarray.DataArray because calling to_netcdf()
        # on it at the end of this function will save it as a Dataset
        # with one data variable with the same name as the DataArray.
        if spectral_index in ['ndvi', 'ndbi', 'ndwi', 'evi']:
            composites_out_of_range[composite_name] = \
                xr_or(composite[spectral_index] < task.composite_threshold_min,
                      task.composite_threshold_max < composite[spectral_index]).rename(spectral_index)
        else:  # Fractional Cover
            # For fractional cover, a composite pixel is out of range if any of its
            # fractional cover bands are out of range.
            composites_out_of_range[composite_name] = xr_or(
                xr_or(xr_or(composite['bs'] < task.composite_threshold_min,
                            task.composite_threshold_max < composite['bs']),
                      xr_or(composite['pv'] < task.composite_threshold_min,
                            task.composite_threshold_max < composite['pv'])),
                xr_or(composite['npv'] < task.composite_threshold_min,
                      task.composite_threshold_max < composite['npv'])
            ).rename(spectral_index)

        # Update the metadata with the current data (baseline or analysis).
        metadata = task.metadata_from_dataset(metadata, time_column_data,
                                              time_column_clean_mask, parameters)
        # Record task progress (baseline or analysis composite data obtained).
        task.scenes_processed = F('scenes_processed') + num_scn_per_chk[composite_name]
        task.save(update_fields=['scenes_processed'])
    dc.close()

    if check_cancel_task(self, task): return
    # Create a difference composite.
    diff_composite = composites['analysis'] - composites['baseline']
    # Find where either the baseline or analysis composite was out of range for a pixel.
    composite_out_of_range = xr_or(*composites_out_of_range.values())
    # Find where either the baseline or analysis composite was no_data.
    if spectral_index in ['ndvi', 'ndbi', 'ndwi', 'evi']:
        composite_no_data = xr_or(composites['baseline'][measurements_list[0]] == no_data_value,
                                  composites['analysis'][measurements_list[0]] == no_data_value)
        if spectral_index == 'evi':  # EVI returns no_data for values outside [-1,1].
            composite_no_data = xr_or(
                composite_no_data,
                xr_or(composites['baseline'][spectral_index] == no_data_value,
                      composites['analysis'][spectral_index] == no_data_value)
            )
    else:  # Fractional Cover
        composite_no_data = xr_or(
            xr_or(xr_or(composites['baseline']['bs'] == no_data_value,
                        composites['baseline']['pv'] == no_data_value),
                  composites['baseline']['npv'] == no_data_value),
            xr_or(xr_or(composites['baseline']['bs'] == no_data_value,
                        composites['baseline']['pv'] == no_data_value),
                  composites['baseline']['npv'] == no_data_value)
        )
    composite_no_data = composite_no_data.rename(spectral_index)

    # Drop unneeded data variables.
    diff_composite = diff_composite.drop(measurements_list)

    if check_cancel_task(self, task): return

    composite_path = os.path.join(task.get_temp_path(), chunk_id + ".nc")
    export_xarray_to_netcdf(diff_composite, composite_path)
    composite_out_of_range_path = os.path.join(task.get_temp_path(), chunk_id + "_out_of_range.nc")
    logger.info("composite_out_of_range:" + str(composite_out_of_range))
    export_xarray_to_netcdf(composite_out_of_range, composite_out_of_range_path)
    composite_no_data_path = os.path.join(task.get_temp_path(), chunk_id + "_no_data.nc")
    export_xarray_to_netcdf(composite_no_data, composite_no_data_path)
    return composite_path, composite_out_of_range_path, composite_no_data_path, \
           metadata, {'geo_chunk_id': geo_chunk_id}


@task(name="spectral_anomaly.recombine_geographic_chunks", base=BaseTask, bind=True)
def recombine_geographic_chunks(self, chunks, task_id=None):
    """Recombine processed data over the geographic indices

    For each geographic chunk process spawned by the main task, open the resulting dataset
    and combine it into a single dataset. Combine metadata as well, writing to disk.

    Args:
        chunks: list of the return from the processing_task function - path, metadata, and {chunk ids}

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    total_chunks = [chunks] if not isinstance(chunks, list) else chunks
    total_chunks = [chunk for chunk in total_chunks if chunk is not None]
    if len(total_chunks) == 0: return None

    task = SpectralAnomalyTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    metadata = {}
    composite_chunk_data = []
    out_of_range_chunk_data = []
    no_data_chunk_data = []
    for index, chunk in enumerate(total_chunks):
        metadata = task.combine_metadata(metadata, chunk[3])
        composite_chunk_data.append(xr.open_dataset(chunk[0]))
        out_of_range_chunk_data.append(xr.open_dataset(chunk[1]))
        no_data_chunk_data.append(xr.open_dataset(chunk[2]))

    combined_composite_data = combine_geographic_chunks(composite_chunk_data)
    combined_out_of_range_data = combine_geographic_chunks(out_of_range_chunk_data)
    combined_no_data = combine_geographic_chunks(no_data_chunk_data)

    composite_path = os.path.join(task.get_temp_path(), "full_composite.nc")
    export_xarray_to_netcdf(combined_composite_data, composite_path)
    composite_out_of_range_path = os.path.join(task.get_temp_path(), "full_composite_out_of_range.nc")
    export_xarray_to_netcdf(combined_out_of_range_data, composite_out_of_range_path)
    no_data_path = os.path.join(task.get_temp_path(), "full_composite_no_data.nc")
    export_xarray_to_netcdf(combined_no_data, no_data_path)
    return composite_path, composite_out_of_range_path, no_data_path, metadata


@task(name="spectral_anomaly.create_output_products", base=BaseTask, bind=True)
def create_output_products(self, data, task_id=None):
    """Create the final output products for this algorithm.

    Open the final dataset and metadata and generate all remaining metadata.
    Convert and write the dataset to various formats and register all values in the task model
    Update status and exit.

    Args:
        data: tuple in the format of processing_task function - path, metadata, and {chunk ids}

    """
    if data is None: return None

    task = SpectralAnomalyTask.objects.get(pk=task_id)
    if check_cancel_task(self, task): return

    spectral_index = task.query_type.result_id

    full_metadata = data[3]
    # This is the difference (or "change") composite.
    diff_composite = xr.open_dataset(data[0])
    # This indicates where either the baseline or analysis composite
    # was outside the corresponding user-specified range.
    orig_composite_out_of_range = xr.open_dataset(data[1]) \
        [spectral_index].astype(np.bool).values
    # This indicates where either the baseline or analysis composite
    # was the no_data value.
    composite_no_data = xr.open_dataset(data[2]) \
        [spectral_index].astype(np.bool).values

    # Obtain a NumPy array of the data to create a plot later.
    if spectral_index in ['ndvi', 'ndbi', 'ndwi', 'evi']:
        diff_comp_np_arr = diff_composite[spectral_index].values
    else:  # Fractional Cover
        diff_comp_np_arr = diff_composite['pv'].values
    diff_comp_np_arr[composite_no_data] = np.nan

    task.data_netcdf_path = os.path.join(task.get_result_path(), "data_netcdf.nc")
    task.data_path = os.path.join(task.get_result_path(), "data_tif.tif")
    task.result_path = os.path.join(task.get_result_path(), "png_mosaic.png")
    task.final_metadata_from_dataset(diff_composite)
    task.metadata_from_dict(full_metadata)

    # 1. Prepare to save the spectral index net change as a GeoTIFF and NetCDF.
    if spectral_index in ['ndvi', 'ndbi', 'ndwi', 'evi']:
        bands = [spectral_index]
    else:  # Fractional Coverage
        bands = ['bs', 'pv', 'npv']
    # 2. Prepare to create a PNG of the spectral index change composite.
    # 2.1. Find the min and max possible difference for the selected spectral index.
    spec_ind_min, spec_ind_max = spectral_indices_range_map[spectral_index]
    diff_min_possible, diff_max_possible = spec_ind_min - spec_ind_max, spec_ind_max - spec_ind_min
    # 2.2. Scale the difference composite to the range [0, 1] for plotting.
    image_data = np.interp(diff_comp_np_arr, (diff_min_possible, diff_max_possible), (0, 1))
    # 2.3. Color by region.
    # 2.3.1. First, color by change.
    # If the user specified a change value range, the product is binary -
    # denoting which pixels fall within the net change threshold.
    cng_min, cng_max = task.change_threshold_min, task.change_threshold_max
    if cng_min is not None and cng_max is not None:
        image_data = np.empty((*image_data.shape, 4), dtype=image_data.dtype)
        image_data[:, :] = mpl.colors.to_rgba('red')
    else:  # otherwise, use a red-green gradient.
        cmap = plt.get_cmap('RdYlGn')
        image_data = cmap(image_data)
    # 2.3.2. Second, color regions in which the change was outside
    #        the optional user-specified change value range.
    change_out_of_range_color = mpl.colors.to_rgba('black')
    if cng_min is not None and cng_max is not None:
        diff_composite_out_of_range = (diff_comp_np_arr < cng_min) | (cng_max < diff_comp_np_arr)
        image_data[diff_composite_out_of_range] = change_out_of_range_color
    # 2.3.3. Third, color regions in which either the baseline or analysis
    #        composite was outside the user-specified composite value range.
    composite_out_of_range_color = mpl.colors.to_rgba('white')
    image_data[orig_composite_out_of_range] = composite_out_of_range_color
    #  2.3.4. Fourth, color regions in which either the baseline or analysis
    #         composite was the no_data value as transparent.
    composite_no_data_color = np.array([0., 0., 0., 0.])
    image_data[composite_no_data] = composite_no_data_color

    # Create output products (NetCDF, GeoTIFF, PNG).
    export_xarray_to_netcdf(diff_composite, task.data_netcdf_path)
    write_geotiff_from_xr(task.data_path, diff_composite.astype('float32'),
                          bands=bands, no_data=task.satellite.no_data_value)
    plt.imsave(task.result_path, image_data)

    # Plot metadata.
    dates = list(map(lambda x: datetime.strptime(x, "%m/%d/%Y"), task._get_field_as_list('acquisition_list')))
    if len(dates) > 1:
        task.plot_path = os.path.join(task.get_result_path(), "plot_path.png")
        create_2d_plot(
            task.plot_path,
            dates=dates,
            datasets=task._get_field_as_list('clean_pixel_percentages_per_acquisition'),
            data_labels="Clean Pixel Percentage (%)",
            titles="Clean Pixel Percentage Per Acquisition")

    task.complete = True
    task.execution_end = datetime.now()
    task.update_status("OK", "All products have been generated. Your result will be loaded on the map.")
    return True