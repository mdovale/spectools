""" This module contains signal processing functions.

Miguel Dovale (Hannover, 2024)
E-mail: spectools@pm.me
"""
import os
import numpy as np
import pandas as pd
import zipfile
import tarfile
import gzip
from py7zr import SevenZipFile
from copy import deepcopy
from scipy.integrate import cumulative_trapezoid
from scipy.optimize import curve_fit, minimize
from scipy.signal import welch
from pytdi.dsp import timeshift
from tqdm import tqdm
from typing import List, Optional, Callable
import warnings

import logging
logging.basicConfig(
format='%(asctime)s %(levelname)-8s %(message)s',
level=logging.INFO,
datefmt='%Y-%m-%d %H:%M:%S'
)

def frequency2phase(f, fs):
    """
    Integrate frequency in Hz to find phase in radians.

    Parameters
    ----------
        f (numpy.ndarray): The input signal frequency in Hz.

    Returns
    -------
        numpy.ndarray: The phase in radians.
    """
    return (2*np.pi/fs)*np.cumsum(np.array(f-np.mean(f)))

def numpy_detrend(x, order=1):
    """
    Detrend an input signal using a fast polynomial fit.

    Parameters
    ----------
        x (numpy.ndarray): The input signal to be detrended.
        order (int): The order of the polynomial fit.

    Returns
    -------
        numpy.ndarray: The detrended signal.
    """
    t = np.arange(len(x))
    # Construct the Vandermonde matrix
    T = np.vander(t, order + 1, increasing=True)
    # Solve the least squares problem directly
    coeffs = np.linalg.lstsq(T, x, rcond=None)[0]
    # Subtract the trend
    trend = T @ coeffs
    return x - trend

def crop_data(x, y, xmin, xmax):
    """ Crop data.

    Parameters
    ----------
        x: data in x
        y: data in y
        xmin: lower bound of x
        xmax: upper bound of x
    """
    x = np.array(x)
    y = np.array(y)

    # Create a boolean mask for the range condition
    mask = (x >= xmin) & (x <= xmax)
    
    # Apply the mask to both x and y arrays and return
    return x[mask], y[mask]

def truncation(x, n_trunc):
    """ Truncate both ends of time-series data.

    Args:
        x: data to truncate
        n_trunc: number of points to be truncated at each end of array
    """
    if n_trunc > 0:
        return x[n_trunc:-n_trunc]
    else:
        return x

def integral_rms(fourier_freq, asd, pass_band=None):
    """ Compute the RMS as integral of an ASD.
    
    Parameters
    ----------
        fourier_freq: fourier frequency (Hz)
        asd: amplitude spectral density from which RMS is computed
        pass_band: [0] = min, [1] = max 
    """
    if pass_band is None:
        pass_band = [-np.inf,np.inf]

    integral_range_min = max(np.min(fourier_freq), pass_band[0])
    integral_range_max = min(np.max(fourier_freq), pass_band[1])
    f_tmp, asd_tmp = crop_data(fourier_freq, asd, integral_range_min, integral_range_max)
    integral_rms2 = cumulative_trapezoid(asd_tmp**2, f_tmp, initial=0)
    return np.sqrt(integral_rms2[-1])

def peak_finder(frequency, measurement, cnr=10, edge=True, freq_band=None, rtol=1e-2):
    """
    Detects peaks in a measurement array based on CNR(dB) threshold.

    Parameters
    ----------
    frequency : array-like
        The frequency array corresponding to the measurements.
    measurement : array-like
        The measurement array where peaks are to be detected.
    cnr : float, optional
        Carrier-to-noise density ratio in dB. Peaks must exceed this ratio to be considered valid.
    edge : bool, optional, default=True
        If True, consider peaks that are on the boundary of the spectrum.
    freq_band : tuple of (float, float), optional
        Frequency band to search for peaks, specified as (low_freq, high_freq). Only frequencies within this range are considered.
    rtol : float, optional, default=1e-2
        Relative tolerance for identifying flat peaks.

    Returns
    -------
    peak_frequencies : ndarray
        Array of frequencies at which peaks were detected.
    peak_measurements : ndarray
        Array of measurement values at the detected peak frequencies.

    Notes
    -----
    The function first applies an optional frequency band filter and then manually detects peaks by identifying points that are higher than their immediate neighbors.
    Peaks that do not meet the specified carrier-to-noise density ratio are discarded. The function returns the frequencies and measurements of the detected peaks.

    Example
    -------
    >>> frequency = np.linspace(0, 100, 1000)
    >>> measurement = np.sin(frequency) + 0.5 * np.random.randn(1000)
    >>> peaks_freq, peaks_meas = peak_finder(frequency, measurement, cnr=5, edge=True, freq_band=(10, 90))
    >>> print("Peak Frequencies:", peaks_freq)
    >>> print("Peak Measurements:", peaks_meas)
    """
    def noise_model(x, a, b, alpha):
        return a + b * x**alpha

    frequency = np.array(frequency)
    measurement = np.array(measurement)
    
    if freq_band is not None:
        low_freq, high_freq = freq_band
        mask = (frequency >= low_freq) & (frequency <= high_freq)
        frequency = frequency[mask]
        measurement = measurement[mask]

    if len(frequency) == 0:
        return
    
    # Initial peak finding
    peaks = []
    i = 1
    while i < len(measurement) - 1:
        if measurement[i - 1] < measurement[i] > measurement[i + 1]:
            peaks.append(i)
        elif (measurement[i - 1] < measurement[i]) and np.isclose(measurement[i], measurement[i + 1], rtol=rtol):
            start = i
            while i < len(measurement) - 1 and np.isclose(measurement[i], measurement[i + 1], rtol=rtol):
                i += 1
            if measurement[i] > measurement[i + 1]:
                mid = (start + i) // 2
                peaks.append(mid)
        i += 1

    if edge:
        if measurement[0] > measurement[1]:
            peaks.insert(0, 0)
        if measurement[-1] > measurement[-2]:
            peaks.append(len(measurement) - 1)
    else:
        peaks = [p for p in peaks if p != 0 and p != len(measurement) - 1]
    
    # Exclude peaks for noise fitting
    non_peak_mask = np.ones(len(measurement), dtype=bool)
    non_peak_mask[peaks] = False

    # Fit to noise model
    popt, _ = curve_fit(noise_model, frequency[non_peak_mask], measurement[non_peak_mask])
    noise_level = noise_model(frequency, *popt)
    
    # Calculate CNR threshold
    cnr_threshold = noise_level * (10 ** (cnr / 10))
    valid_peaks = [p for p in peaks if measurement[p] > cnr_threshold[p]]

    peak_frequencies = frequency[valid_peaks]
    peak_measurements = measurement[valid_peaks]
    
    return peak_frequencies, peak_measurements

def optimal_linear_combination(df, inputs, output, timeshifts=False, gradient=False,  domain='time', method='TNC', tol=1e-9, *args, **kwargs):
    """
    Computes the coefficients of a linear combination of optionally timeshifted "input" signals 
    that minimize noise when added to the "output" signal.

    Target: `RMS[ output + Sum [coefficient_i * timeshift(input_i, shift_i) ] ]`

    Parameters
    ----------
        df (DataFrame): Data from signals.
        inputs (list of str): Labels of the input signal columns in the input DataFrame.
        output (str): Label of the output signal column in the input DataFrame.
        timeshifts (bool, optional): Whether the input signals should be timeshifted. Default is False.
        gradient (bool, optional): Whether to minimize rms in the time series or on its derivative.
        domain (str, optional): Whether to compute RMS in the time domain or in the frequency domain
        method (str, optional): The minimizer method.
        tol (float, optional): The minimizer tolerance parameter.

    Returns:
        OptimizeResult: The optimization result object.
        np.ndarray: The output with optimal combination of inputs subtracted
    """
    def print_optimization_result(res):
        logging.info("Optimization Results:")
        logging.info("=====================")
        logging.info(f"Success: {res.success}")
        logging.info(f"Message: {res.message}")
        logging.info(f"Function value at minimum: {res.fun}")
        logging.info("Solution:")
        for idx, val in enumerate(res.x, start=1):
            logging.info(f"Variable {idx}: {val}")

    def fun(x):
        y = np.array(df[output] - np.mean(df[output]))        

        if timeshifts:
            for i, input in enumerate(df[inputs]):
                Si = np.array(df[input] - np.mean(df[input]))
                y += x[len(inputs)+i]*timeshift(Si, x[i])
            max_delay = np.max(x[:len(inputs)])
            y = truncation(y, n_trunc=int(2*max_delay))
        else:
            for i, input in enumerate(df[inputs]):
                Si = np.array(df[input] - np.mean(df[input]))
                y += x[i]*Si

        if gradient:
            y = np.gradient(y)

        if domain == 'time':
            rms_value = np.sqrt(np.mean(np.square(y-np.mean(y))))
        elif domain == 'frequency':
            f, Sxx = welch(y, scaling='density', *args, **kwargs)
            rms_value = np.sqrt(np.trapezoid(Sxx, f))
        else:
            raise ValueError('The `domain` parameter must be set to \'time\' or \'frequency\'')
        
        return rms_value

    if timeshifts:
        x_initial = np.zeros(len(inputs)*2)
    else:
        x_initial = np.zeros(len(inputs))

    logging.info(f"Solving {len(x_initial)}-dimensional problem...")

    res = minimize(fun, x_initial, method=method, tol=tol)

    print_optimization_result(res)

    if timeshifts:
        y = np.array(df[output] - np.mean(df[output]))        
        for i, input in enumerate(df[inputs]):
            Si = np.array(df[input] - np.mean(df[input]))
            y += res.x[len(inputs)+i]*timeshift(Si, res.x[i])
        max_delay = np.max(res.x[:len(inputs)])
        y = truncation(y, int(2*max_delay))
    else:
        y = np.array(df[output] - np.mean(df[output]))        
        for i, input in enumerate(df[inputs]):
            Si = np.array(df[input] - np.mean(df[input]))
            y += res.x[i]*Si

    return res, y

def adaptive_linear_combination(df, inputs, output, method='TNC', tol=1e-9):
    """
    Work in progress.
    """
    def fun(x, t):
        y = df[output].iloc[t]
        S = 0.0

        for i, input in enumerate(df[inputs]):
            Si = df[input].iloc[t]  
            S += x[i]*Si

        obj = (y - S)**2
        
        return obj

    for input in inputs:
        df[input] = df[input] - np.mean(df[input])

    y = []
    x = {}
    for input in df[inputs]:
        x[input] = []

    for t in tqdm(range(len(df))):
        if t == 0:
            x_initial = np.zeros(len(inputs))
        else:
            x_initial = res.x

        res = minimize(fun, x_initial, (t), method=method, tol=tol)
        
        # if not res.success:
            # logging.warning(f"Potential minimizer failure at t={t}")
        
        S = 0.0
        for i, input in enumerate(df[inputs]):
            x[input].append(res.x[i])
            Si = df[input].iloc[t]  
            S += res.x[i]*Si
        
        y.append(df[output].iloc[t] - S)

    return x, y

def df_timeshift(df, fs, seconds, columns=None, truncate=None, inplace=False, suffix='_shifted'):
    """ 
    Timeshift columns of a pandas DataFrame or the entire DataFrame.

    Parameters
    ----------
        df (pd.DataFrame): The input DataFrame.
        fs (float): The sampling frequency of the data.
        seconds (float): Amount of seconds to shift the data by.
        columns (list or None): List of columns to shift. If None, all columns are shifted.
        truncate (bool or int or None): If True, truncate the resulting DataFrame based on the shift. 
                                        If int, specify the exact number of rows to truncate at both ends.
        inplace (bool): If True, overwrite the original columns. If False, add shifted columns with suffix.
        suffix (str): Suffix to add to column names when inplace is False.

    Returns
    -------
        pd.DataFrame: The timeshifted DataFrame.
    """

    if seconds == 0.0:
        return df

    df_shifted = df.copy()

    if columns is None:
        columns = df.columns

    for c in columns:
        shifted = timeshift(df[c].to_numpy(), seconds * fs)
        if inplace:
            df_shifted[c] = shifted
        else:
            df_shifted[f"{c}{suffix}"] = shifted

    if truncate is not None:
        if isinstance(truncate, bool):
            n_trunc = int(2 * abs(seconds * fs))
        else:
            n_trunc = int(truncate)
        if n_trunc > 0:
            df_shifted = df_shifted.iloc[n_trunc:-n_trunc]

    return df_shifted

def df_detrend(df, columns=None, order=1, inplace=False, suffix='_detrended'):
    """
    Detrend all or specified columns of a pandas DataFrame using numpy_detrend.

    Parameters
    ----------
        df (pd.DataFrame): The input DataFrame.
        columns (list, optional): List of column names to detrend. If None, all columns are detrended.
        order (int): The order of the polynomial fit.
        inplace (bool): If True, overwrite the original columns. If False, create new columns with suffix.
        suffix (str): Suffix to add to column names when inplace is False.

    Returns
    -------
        pd.DataFrame: A DataFrame with detrended data.
    """
    df_detrended = df.copy()
    if columns is None:
        columns = df.columns

    for col in columns:
        if df[col].dtype.kind in 'biufc':  # Check if the column is numeric
            detrended_data = numpy_detrend(df[col].values, order=order)
            if inplace:
                df_detrended[col] = detrended_data
            else:
                df_detrended[f"{col}{suffix}"] = detrended_data

    return df_detrended

def multi_file_timeseries_loader(file_list: List[str], fs_list: List[float], names_list: Optional[List[str]],
                                 start_time: Optional[float] = 0.0, duration_hours: Optional[float] = None,
                                 timeshifts: Optional[List[float]] = None, delimiter_list: Optional[List[str]] = None) -> List[pd.DataFrame]:
    """
    Loads time-series data from multiple files, restricting the output to the maximum overlapping time window across the datasets. 

    Parameters
    ----------
    file_list : List[str]
        A list of file paths for the input data files. Each file must contain time-series data sampled with the
        sampling frequencies provided in `fs_list`.
    
    fs_list : List[float]
        A list of sampling frequencies (in Hz) corresponding to each file in `file_list`. The length of `fs_list` must
        match the length of `file_list`, and each value must be positive.
    
    names_list: Optional[List[str]]
        A list of the column names corresponding to each file.

    start_time : float, optional
        The starting time (in seconds) from which the data will be extracted in each file. The function will extract data
        starting at this time in each file, adjusting for the sampling frequency. Default is 0.0 seconds.

    timeshifts : List[float], optional
        Time shifts (in seconds) to apply to each data stream.
    
    delimiter_list : List[str], optional
        A list of delimiters to be used for reading each file. If not provided, a space (' ') will be assumed as the
        delimiter for all files. The length of `delimiter_list` must match the length of `file_list` if provided.

    Returns
    -------
    List[pd.DataFrame]
        A list of pandas DataFrames containing the synchronized time-series data for each file. Each DataFrame will have
        been sliced to ensure the maximum overlap duration between all datasets, starting from `start_time`.
    
    Raises
    ------
    ValueError
        If the length of `file_list` does not match the length of `fs_list` or `delimiter_list` (if provided), or if any
        value in `fs_list` is non-positive.

    Notes
    -----
    - The function assumes that the first row in each file (after skipping comment rows) contains the column names.
    - The data in each file will be truncated based on the maximum overlapping time window. The function computes
      this by calculating the number of rows in each file and their corresponding time durations, based on the sampling
      frequencies (`fs_list`).
    - The function supports files with different delimiters and skips any header rows that begin with comment symbols
      such as `#`, `%`, `!`, etc.
    """
    def count_header_rows(file):
        header_symbols = ['#', '%', '!', '@', ';', '&', '*', "/"]  # Add more symbols as needed
        header_count = 0

        def process_file(file_obj):
            """Helper function to count headers in a file object."""
            nonlocal header_count
            for line in file_obj:
                line = line.decode('utf-8') if isinstance(line, bytes) else line  # Handle binary content
                if any(line.startswith(symbol) for symbol in header_symbols):
                    header_count += 1
                else:
                    break

        if zipfile.is_zipfile(file):  # Check if it's a zip file
            with zipfile.ZipFile(file, 'r') as zip_ref:
                first_file_name = zip_ref.namelist()[0]
                with zip_ref.open(first_file_name, 'r') as target_file:
                    process_file(target_file)

        elif tarfile.is_tarfile(file):  # Check if it's a tar file
            with tarfile.open(file, 'r') as tar_ref:
                first_member = tar_ref.getmembers()[0]
                with tar_ref.extractfile(first_member) as target_file:
                    process_file(target_file)

        elif file.endswith('.gz'):  # Check if it's a gzip file
            with gzip.open(file, 'rt') as target_file:  # 'rt' for reading text
                process_file(target_file)

        elif file.endswith('.7z'):  # Check if it's a 7z file
            with SevenZipFile(file, 'r') as seven_zip_ref:
                first_file_name = seven_zip_ref.getnames()[0]
                with seven_zip_ref.open(first_file_name) as target_file:
                    process_file(target_file)

        else:  # Treat it as a regular text file
            with open(file, 'r') as target_file:
                process_file(target_file)

        return header_count
    
    # Ensure matching lengths of input lists
    if len(file_list) != len(fs_list):
        raise ValueError("The length of `fs_list` must match the length of `file_list`.")
    if names_list is not None and len(file_list) != len(names_list):
        raise ValueError("The length of `names_list` must match the length of `file_list`.")
    if delimiter_list is not None and len(file_list) != len(delimiter_list):
        raise ValueError("The length of `delimiter_list` must match the length of `file_list`.")
    if timeshifts is not None and len(file_list) != len(timeshifts):
        raise ValueError("The length of `timeshifts` must match the length of `file_list`.")
    for fs in fs_list:
        if fs <= 0:
            raise ValueError("Sampling frequency `fs` must be positive.")
    
    delimiter_list = delimiter_list or [' '] * len(file_list)
    timeshifts = timeshifts or [None] * len(file_list)
    header_rows = []  # Stores the number of header rows for each file
    record_lengths = []  # Stores the duration for each file
    df_list = []  # Store the actual dataframes
    max_duration = None  # Will hold the maximum overlapping time duration

    # File names and metadata discovery:
    file_names = []
    header_rows = []
    for i, file in enumerate(file_list):
        file_names.append(os.path.basename(file))
        rows = count_header_rows(file)
        header_rows.append(rows)
        logging.info(f"File \'{file_names[i]}\' contains {header_rows[i]} header rows.")

    # Data ingestion:
    logging.info(f"Loading data and calculating maximum time series overlap...")
    for i, file in enumerate(file_list):
        try:
            if names_list is not None and names_list[i] is not None:
                df = pd.read_csv(file, delimiter=delimiter_list[i], skiprows=header_rows[i], names=names_list[i], engine='c')
            else:
                df = pd.read_csv(file, delimiter=delimiter_list[i], skiprows=header_rows[i], header=0, engine='c')
        except:
            logging.warning(f"Reading {file} with Python engine")
            if names_list is not None and names_list[i] is not None:
                df = pd.read_csv(file, delimiter=delimiter_list[i], skiprows=header_rows[i], names=names_list[i], engine='python')
            else:
                df = pd.read_csv(file, delimiter=delimiter_list[i], skiprows=header_rows[i], header=0, engine='python')       
        logging.info(f"Loaded data from file \'{file_names[i]}\' with length {len(df)}")
        record_lengths.append(len(df)  / fs_list[i]) # Data stream duration in seconds
        df_list.append(df)

    # Drop NaN columns and log warning:
    for i, df in enumerate(df_list):
        initial_columns = list(df.columns)  # Store initial column names
        df.dropna(axis=1, how='all', inplace=True)  # Drop columns with all NaN values
        dropped_columns = set(initial_columns) - set(df.columns)  # Find dropped columns
        # Log a warning if columns were dropped:
        if dropped_columns:
            logging.warning(f"File '{file_names[i]}' had columns dropped due to NaN values: {dropped_columns}")

    # Determine the maximum overlap between datasets:
    max_duration = min(record_lengths) - start_time  # Maximum overlapping time between datasets in seconds
    logging.info(f"Maximum overlap: {max_duration:.2f} seconds ({max_duration/3600.0:.2f} hours)")

    # Apply optional timeshifts:
    samples_shifted = []
    for i, df in enumerate(df_list):
        if (timeshifts[i] is not None) and (timeshifts[i] != 0.0):
            logging.info(f"Applying {timeshifts[i]} seconds timeshift to the \'{file_names[i]}\' data stream")
            df_list[i] = df_timeshift(df, seconds=timeshifts[i], fs=fs_list[i], columns=df.select_dtypes(include=['number']).columns)
            samples_shifted.append(timeshifts[i]*fs_list[i])
        else:
            samples_shifted.append(0.0)

    # Readjust of start_time and max_duration in the case of large time shifts:
    if any(samples_shifted):
        for i, df in enumerate(df_list):
            if (samples_shifted[i] < 0.0) and (abs(samples_shifted[i]) > int(start_time * fs_list[i])):
                start_time = int(2*abs(samples_shifted[i]))
            if (samples_shifted[i] > 0.0) and (abs(samples_shifted[i]) > len(df) - (int(start_time * fs_list[i]) + int(max_duration * fs_list[i]))):
                max_duration = (len(df) - int(start_time * fs_list[i]) - int(2*abs(samples_shifted[i]))) / fs_list[i]
        logging.info(f"Maximum overlap after timeshift and truncation: {max_duration:.2f} seconds")

    # Adjust duration according to user input:
    total_time = max_duration # Total measurement time in seconds
    if duration_hours is not None:
        if (duration_hours*3600.0 > max_duration) or (duration_hours <= 0.0):
            logging.warning(f"Specified duration of {duration_hours:.2f} hours is not possible, setting to {max_duration/3600.0:.2f} hours")
        else:
            total_time = duration_hours*3600.0

    # Truncation to the overlapping section and timestamping:
    final_df_list = []
    for i, df in enumerate(df_list):
        start_row = int(start_time * fs_list[i])  # Convert start time to row index
        end_row = start_row + int(total_time * fs_list[i])  # Calculate the end row based on max overlap

        new_df = df.iloc[start_row:end_row + 1].copy() # Slice the dataframe to get the relevant rows
        new_df.reset_index(drop=True, inplace=True)
        time_column_name = 'time'
        if time_column_name in new_df:
            time_column_name = 'new_time'
        new_df[time_column_name] = np.linspace(start_row/fs_list[i], end_row/fs_list[i], len(new_df))
        logging.info(f"""File \'{file_names[i]}\':
                                        Start row: {start_row}; Start time: {new_df[time_column_name].iloc[0]:.2f} seconds
                                        End row: {end_row}; End time: {new_df[time_column_name].iloc[-1]:.2f} seconds
                                        Total time: {(new_df[time_column_name].iloc[-1] - new_df[time_column_name].iloc[0]):.2f} seconds.""")
        final_df_list.append(new_df)

    return final_df_list

def resample_to_common_grid(df_list: List[pd.DataFrame], fs: float, t_col_list: Optional[List[str]] = None, tolerance: Optional[float] = 0.1,
                            preprocessors: Optional[List[Callable]] = None, suffixes: Optional[bool] = False) -> pd.DataFrame:
    """
    Resample one or multiple DataFrames to a common time grid by interpolating 
    data to align with a unified time axis.

    Parameters
    ----------
    df_list : List[pd.DataFrame]
        A list containing one or more DataFrames, each with a time column 
        and associated data columns to be interpolated onto a common time grid.
        
    fs : float
        Sampling frequency (Hz) for the common time grid. Must be positive.
        
    t_col_list : List[str], optional
        Column names representing time for each DataFrame in `df_list`. If not provided, 
        defaults to 'time' for all DataFrames.

    tolerance : float, optional
        The allowable deviation (in seconds) from the mean sampling interval. Values
        exceeding this tolerance trigger warnings.

    preprocessors: List[Callable], optional
        Pre-processing functions to apply to each data stream before resampling. Defaults to None.
        
    suffixes : Optional[bool], default=True
        If True, suffixes the column names of each DataFrame with its index in `df_list` 
        to avoid name conflicts. If False, original column names are retained (name 
        conflicts may arise if columns have identical names).

    Returns
    -------
    pd.DataFrame
        DataFrame containing interpolated values of each input DataFrame aligned 
        on a common time grid, with time values in 'common_time'. If `suffixes=True`, 
        data columns are suffixed with the DataFrame index to clarify origin.
        
    Raises
    ------
    ValueError
        If `fs` is non-positive, or any DataFrame lacks the specified or default time column.
    """
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    _df_list = deepcopy(df_list)

    if fs <= 0:
        raise ValueError("Sampling frequency `fs` must be positive.")
    if len(_df_list) == 0:
        raise ValueError("At least one DataFrame must be provided.")

    t_col_list = t_col_list or ['time'] * len(_df_list)
    preprocessors = preprocessors or [None] * len(_df_list)

    # If only one DataFrame is provided, handle separately
    if len(_df_list) == 1:
        df = _df_list[0]
        t_col = t_col_list[0]

        if t_col not in df:
            raise ValueError(f"The provided DataFrame does not contain the time column '{t_col}'")

        # Determine time range and common time grid
        start_time, end_time = df[t_col].min(), df[t_col].max()
        common_time = np.arange(start_time, end_time, 1/fs)
        
        logging.info(f"Single DataFrame provided: Resampling to {len(common_time)} samples from {start_time:.2f}s to {end_time:.2f}s")

        # Apply preprocessing if specified
        if preprocessors[0] is not None:
            logging.info(f"Applying preprocessor to DataFrame")
            df = preprocessors[0](df)

        # Interpolation
        df_interp = pd.DataFrame({'common_time': common_time})
        for col in df.columns:
            if col == t_col:
                continue  # Skip the time column
            df_interp[col] = np.interp(common_time, df[t_col], df[col])

        return df_interp

    # Handling multiple DataFrames (original logic)
    start_time, end_time = 0.0, float('inf')
    df_interp_list = []

    # Determine the overlapping time range
    for i, (df, t) in enumerate(zip(_df_list, t_col_list)):
        if t not in df:
            raise ValueError(f"DataFrame #{i+1} does not contain the time column '{t}'")
        start_time = max(start_time, df[t].min())
        end_time = min(end_time, df[t].max())
        logging.info(f"""DataFrame #{i+1}:
                                        Start time: {df[t].iloc[0]:.2f} seconds
                                        End time: {df[t].iloc[-1]:.2f} seconds
                                        Samples: {len(df)}""")
    if start_time >= end_time:
        logging.warning("No overlapping time range between DataFrames; output DataFrame is empty.")
        return pd.DataFrame(columns=["common_time"])

    # Time grid consistency checks:
    for i, (df, t) in enumerate(zip(_df_list, t_col_list)):
        monotonic = np.all(np.diff(df[t]) > 0)
        if not monotonic:
            logging.warning(f"Time array is not monotonically increasing in DataFrame #{i+1}.")

        # Report intervals exceeding tolerance:
        intervals = np.diff(df[t])
        mean_interval = np.mean(intervals)
        problematic_indices = np.where(np.abs(intervals - mean_interval) > tolerance)[0]
        problematic_intervals = [(idx, intervals[idx]) for idx in problematic_indices]
        if problematic_intervals:
            logging.warning(f"DataFrame #{i+1}: found {len(problematic_intervals)} problematic time intervals exceeding the tolerance:")
            for idx, interval in problematic_intervals:
                logging.warning(f"    Interval at index {idx} = {interval:.6f} s")

    # Generate the common time grid
    common_time = np.arange(start_time, end_time, 1/fs)
    logging.info(f"New common time grid created: {len(common_time)} samples from {start_time:.2f}s to {end_time:.2f}s")

    # Application of preprocessors:
    for i, (df, proc) in enumerate(zip(_df_list, preprocessors)):
        if proc is not None:
            logging.info(f"Applying pre-processor {proc} to DataFrame #{i+1}")
            _df_list[i] = proc(df)
            logging.info(f"Columns: {list(_df_list[i].columns)}")

    # Downsampling and interpolation to the common grid:
    for i, (df, t) in enumerate(zip(_df_list, t_col_list)):
        logging.info(f"Resampling DataFrame #{i+1} based on column \'{t}\'...")
        df_interp = pd.DataFrame({'common_time': common_time})
        for col in df.columns:
            col_name = col + f'_{i+1}' if suffixes else col
            df_interp[col_name] = np.interp(common_time, df[t], df[col])
        df_interp_list.append(df_interp)

    # Merging all data streams to single DataFrame:
    logging.info("Merging...")
    resampled_df = pd.concat(df_interp_list, axis=1).loc[:,~pd.concat(df_interp_list, axis=1).columns.duplicated()]
    logging.info("Done.")
    
    return resampled_df

def multi_file_timeseries_resampler(file_list: List[str], fs_list: List[float], fs: float, 
                                    start_time: Optional[float] = 0.0, duration_hours: Optional[float] = None,
                                    timeshifts: Optional[List[float]] = None, delimiter_list: Optional[List[str]] = None,
                                    t_col_list: Optional[List[str]] = None, tolerance: Optional[float] = 0.1,
                                    preprocessors: Optional[List[callable]] = None,
                                    suffixes: bool = False) -> pd.DataFrame:
    """
    Loads time-series data from multiple files, truncates to the maximum overlapping time window, 
    and resamples to a common time grid by interpolating each data stream.

    Parameters
    ----------
    file_list : List[str]
        A list of file paths for the input data files. Each file must contain time-series data sampled with the
        sampling frequencies provided in `fs_list`.
    
    fs_list : List[float]
        A list of sampling frequencies (in Hz) corresponding to each file in `file_list`. The length of `fs_list` must
        match the length of `file_list`, and each value must be positive.
    
    fs : float
        Sampling frequency (Hz) for the common time grid. Must be positive.
    
    start_time : float, optional
        The starting time (in seconds) from which the data will be extracted in each file. Default is 0.0 seconds.

    timeshifts : List[float], optional
        Time shifts (in seconds) to apply to each data stream.
    
    delimiter_list : List[str], optional
        A list of delimiters to be used for reading each file. If not provided, a space (' ') will be assumed as the
        delimiter for all files.
    
    t_col_list : List[str], optional
        Column names representing time for each DataFrame in `file_list`. Defaults to 'time' for all.

    tolerance : float, optional
        The allowable deviation (in seconds) from the mean sampling interval. Values
        exceeding this tolerance trigger warnings.

    preprocessors: List[callable], optional
        Pre-processing functions to apply to each data stream before resampling. Defaults to None for all.
    
    suffixes : bool, default=False
        If True, suffixes the column names of each DataFrame with its index in `file_list` to avoid name conflicts.

    Returns
    -------
    pd.DataFrame
        A single DataFrame containing the resampled time-series data aligned on a common time grid.
    """
    
    # Load the time series data from multiple files using the loader function
    df_list = multi_file_timeseries_loader(file_list=file_list, fs_list=fs_list, 
                                           start_time=start_time, duration_hours=duration_hours,
                                           timeshifts=timeshifts, delimiter_list=delimiter_list)

    # Resample the loaded data to a common time grid using the resampler function
    resampled_df = resample_to_common_grid(df_list=df_list, fs=fs, t_col_list=t_col_list, 
                                           tolerance=tolerance, preprocessors=preprocessors, suffixes=suffixes)

    return resampled_df