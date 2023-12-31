"""This script contains a collection of preprocessors of EDF data including:

    standard:
        A preprocessor for notch filtering, downsampling and trimming EDF files.
    spindle:
        A preprocessor for performing channel selection for upload to SPINDLE
        software site.

    This module also includes a batch processing function that concurrently
    calls a preprocessor on all files within a directory.

    batch:
        A function that concurrently runs a preprocessor on all the EDF files in
        a supplied directory.

Each preprocessor write a new EDF file.
"""

import copy
import pickle
import time
import warnings
from functools import partial
from multiprocessing import Pool
from pathlib import Path
from typing import Sequence, Union

import numpy as np

from openseize import producer
from openseize.file_io import edf
from openseize.filtering import iir
from openseize.resampling import resampling

from ebb.core import concurrency
from ebb.masking import masks

def standard(
    path: Union[str, Path],
    savedir: Union[str, Path],
    fs: float,
    downsample: int,
    trim_to: Sequence = [48, 72],
    chunksize=30e5,
    axis=-1,
    verbose=True,
) -> None:
    """A standard preprocessor that downsamples and trims data.

    Args:
        path:
            Path to an edf file to process
        savedir:
            Directory where processed EDF will be saved. The filename will match
            the stem of path with the addition of '_PREPROCESSED' before the
            extension.
        fs:
            The sampling rate of the data at path.
        downsample:
            The downsample factor to reduce the sampling rate of the data.
        trim_to:
            A sequence of hours for trimming the data. The trim amount is the
            largest trim_to amount that is below the data's actual length. E. if
            the data is 66 hours long and trim_to is [60, 80] the data will be
            trimmed to 60 hours duration.
        chunksize:
            The number of processed samples along axis to yield from the
            producer at a time. When called in a multiprocessing context this
            value should be set to the amount of samples any single process uses
            at one time.
        axis:
            The axis of reading and production. For EDF files this is the last
            axis.
        verbose:
            A boolean indicating of file write progress should print to stdout.

    Returns:
        None

    Raises:
        A ValueError is issued of the length of the data at path is lower than
        all the trim_to values.
    """

    fp = Path(path)
    reader = edf.Reader(fp)

    # Trim data with floor chosen from trim_to
    trim_samples = np.array(trim_to) * 3600 * fs
    idx = np.searchsorted(trim_samples, reader.shape[axis], side='right') - 1
    if idx < 0:
        hrs = reader.shape[axis] / (3600 * fs)
        msg = f'{path.stem} is {hrs} hrs, a value below all trim_to {trim_to}'
        raise ValueError(msg)
    stop = trim_samples[idx]
    # ensure stop is integer multiple of samples per record / downsample
    stop = int(np.ceil(stop / fs) * fs)

    # build producer
    pro = masks.between_pro(reader, 0, stop, chunksize, axis)
    result = resampling.downsample(pro, downsample, fs, chunksize, axis)
    # FIXME openseize 1.3.0 to support write from producer
    processed = result.to_array()

    # prepare a header to write along with processed
    header = copy.deepcopy(reader.header)
    m = downsample
    # adjust samples per record and number of records in file
    header['samples_per_record'] = [x // m for x in header.samples_per_record]
    header['num_records'] = int(processed.shape[axis] / (fs / m))
    # convert to EDF from EDF+
    header['reserved_0'] = ''
    # FIXME openseize 1.3.0 to assert digital min/max always int type
    header['digital_min'] = [int(m) for m in header['digital_min']]
    header['digital_max'] = [int(m) for m in header['digital_max']]

    # write the standard preprocessed file
    fname = path.stem + '_PREPROCESSED' + path.suffix
    filepath = Path(savedir).joinpath(fname)
    with edf.Writer(filepath) as writer:
        writer.write(header, processed, reader.channels, verbose)

    reader.close()


def spindle(
    path: Union[str, Path],
    savedir: Union[str, Path],
    channels: Sequence,
    verbose: bool = True,
) -> None:
    """Opens an EDF file and writes a new EDF with only channels saved.

    Args:
        path:
            Path to an edf file to reduce channel count for.
        savedir:
            Directory where reduced channel EDF will be saved. The filename
            will match the stem of path with the addition of '_SPINDLE' before
            the extension.
        channels:
            A sequence of channel indices that will be saved to the new EDF.
        verbose:
            A boolean indicating if write progress should print to stdout.

    Returns:
        None
    """

    fp = Path(path)
    reader = edf.Reader(fp)
    data = reader.read(0)

    # prepare a header to write into new EDF file with data
    header = copy.deepcopy(reader.header)
    # FIXME openseize 1.3.0 to assert digital min/max always int type
    header['digital_min'] = [int(m) for m in header['digital_min']]
    header['digital_max'] = [int(m) for m in header['digital_max']]

    # write the reduced channel data
    fname = path.stem + '_SPINDLE' + path.suffix
    filepath = Path(savedir).joinpath(fname)
    with edf.Writer(filepath) as writer:
        writer.write(header, data, channels, verbose)

    reader.close()


def batch(preprocessor, dirpath, target=None, ncores=None, verbose=True, **kwargs):
    """Preprocesses all EDF files in dir path saving the preprocessed data to
    savedir.

    Args:
        preprocessor:
            A callable that preprocesses a single EDF file.
        dirpath:
            A directory containing EDF files to preprocess.
        target:
            A directory where preprocessed files will be saved to. If None,
            a new subdirectory is created in dirpath with the name of the
            preprocessor.
        ncores:
            The number of processing cores to concurrently preprocess EDF files
            in dirpath.
        **kwargs:
            Any valid kwarg for the preprocess function of this module.

    Returns:
        None
    """

    if not target:
        target = Path(dirpath).joinpath(preprocessor.__name__)
    target = Path(target) 
    target.mkdir()

    # get all the edfs and set the number of cpu workers
    paths = list(Path(dirpath).glob('*edf'))
    workers = concurrency.set_cores(ncores, len(paths))
    if verbose:
        msg = (f"Executing Batched Preprocessor '{preprocessor.__name__}' on "
               f'{len(paths)} files using {workers} cores.')
        print(msg)

    # Execute and time this batch preprocess
    t0 = time.perf_counter()
    kwargs.pop('savedir', None)
    func = partial(preprocessor, savedir=target, **kwargs)
    with Pool(workers) as pool:
        pool.map(func, paths)

    elapsed = time.perf_counter() - t0
    msg = f'Saved {len(paths)} files to {target} in {elapsed} s'
    print(msg)



if __name__ == '__main__':

    #Compute standard preprocessed edfs
    basepath = '/media/matt/DataD/Xue/EbbData/6_week_post/'
    batch(standard, basepath, fs=5000, downsample=20)

    """
    # Compute spindle edfs from standard processed edfs
    standard_dir = '/media/matt/DataD/Xue/EbbData/6_week_post/standard/'
    target = '/media/matt/DataD/Xue/EbbData/6_week_post/spindle/'
    batch(spindle, standard_dir, target=target, channels=[0, 1, 3])
    """
