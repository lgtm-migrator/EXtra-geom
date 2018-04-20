# coding: utf-8
"""
Collection of classes and functions to help reading HDF5 file generated at
The European XFEL.

Copyright (c) 2017, European X-Ray Free-Electron Laser Facility GmbH
All rights reserved.

You should have received a copy of the 3-Clause BSD License along with this
program. If not, see <https://opensource.org/licenses/BSD-3-Clause>
"""

from collections import defaultdict
import datetime
from glob import glob
import h5py
import numpy as np
import os.path as osp
import re
from time import time


__all__ = ['H5File', 'RunDirectory', 'RunHandler', 'stack_data',
           'stack_detector_data']


RUN_DATA = 'RUN'
INDEX_DATA = 'INDEX'
METADATA = 'METADATA'

DETECTOR_NAMES = {'AGIPD', 'LPD'}


class FilenameInfo:
    is_detector = False
    detector_name = None
    detector_moduleno = -1

    _rawcorr_descr = {'RAW': 'Raw', 'CORR': 'Corrected'}

    def __init__(self, path):
        self.basename = osp.basename(path)
        nameparts = self.basename[:-3].split('-')
        assert len(nameparts) == 4, self.basename
        rawcorr, runno, datasrc, segment = nameparts
        m = re.match(r'([A-Z]+)(\d+)', datasrc)

        if m and m.group(1) == 'DA':
            self.description = "Aggregated data"
        elif m and m.group(1) in DETECTOR_NAMES:
            self.is_detector = True
            name, moduleno = m.groups()
            self.detector_name = name
            self.detector_moduleno = moduleno
            self.description = "{} detector data from {} module {}".format(
                self._rawcorr_descr.get(rawcorr, '?'), name, moduleno
            )
        else:
            self.description = "Unknown data source ({})", datasrc


class H5File:
    """Access an HDF5 file generated at European XFEL.

    This class helps select data by train and by device ID, following the
    file layout defined for European XFEL data::

        h5f = H5File('/path/to/my/file.h5')
        for data, train_id, index in h5f.trains():
            value = data['device']['parameter']

    Parameters
    ----------
    path: str
        Path to the HDF5 file
    driver: str, optional
        Driver option for h5py. You should usually not set this.
        http://docs.h5py.org/en/latest/high/file.html#file-drivers

    Raises
    ------
    FileNotFoundError
        If the provided path is not a file
    ValueError
        If the path exists but is not an HDF5 file
    """
    def __init__(self, path, driver=None):
        self.path = path
        if not osp.isfile(path):
            raise FileNotFoundError(path)
        if not h5py.is_hdf5(path):
            raise ValueError('%s is not a valid HDF5 file.' % path)
        self.file = h5py.File(path, 'r', driver=driver)

        self.metadata = self.file[METADATA]
        self.index = self.file[INDEX_DATA]
        self.run = self.file[RUN_DATA]

        self.sources = [source.decode() for source in
                        self.metadata['dataSourceId'].value if source]
        self.control_devices = set()
        self.instrument_device_channels = set()
        for src in self.sources:
            category, *nameparts = src.split('/')
            name = '/'.join(nameparts[:3])
            if category == 'CONTROL':
                self.control_devices.add(name)
            elif category == 'INSTRUMENT':
                self.instrument_device_channels.add(name)

        self.train_ids = [tid for tid in self.index['trainId'][()].tolist()
                          if tid != 0]
        self._trains = {tid: idx for idx, tid in enumerate(self.train_ids)}

    def _gen_train_data(self, train_index, only_this=None):
        """Get data for the specified index in file.
        """
        train_data = {}
        for source in self.sources:
            # The 'deviceId' in the data file is not quite the same as a Karabo
            # device name: for instrument devices it includes an output channel
            # name and the first level key of the hash.
            print(source)
            h5_device = source.split('/', 1)[1]
            index = self.index[h5_device]
            table = self.file[source]

            # Which parts of the data to get for this train:
            first = int(index['first'][train_index])
            if 'last' in index:
                # Older (?) format: status (0/1), first, last
                last = int(index['last'][train_index])
                status = index['status'][train_index]
            else:
                # Newer (?) format: first, count
                count = int(index['count'][train_index])
                last = first + count - 1
                status = count > 0

            dev = h5_device.split('/')
            src = '/'.join((dev[:3]))
            path_base = '.'.join((dev[3:]))

            if only_this and src not in only_this:
                continue

            if src not in train_data:
                train_data[src] = {}
            data = train_data[src]

            if status:
                def append_data(key, value):
                    if isinstance(value, h5py.Dataset):
                        path = '.'.join(filter(None,
                                        (path_base,) + tuple(key.split('/'))))
                        if (only_this and only_this[src] and
                                path not in only_this[src]):
                            return

                        if first == last:
                            data[path] = value[first]
                        else:
                            data[path] = value[first:last+1, ]

                table.visititems(append_data)

            sec, frac = str(time()).split('.')
            timestamp = {'tid': int(self.train_ids[train_index]),
                         'sec': int(sec), 'frac': int(frac)}
            data.update({'metadata': {'source': src, 'timestamp': timestamp}})

        return (train_data, self.train_ids[train_index], train_index)

    def trains(self, devices=None):
        """Iterate over all trains in the file.

        Parameters
        ----------
        devices: dict, optional
            Filter data by devices and by parameters.
            keys are the devices names and values are set() of parameter names
            (or empty set if all parameters are requested)

            ::

                dev = {
                    'device1': {'param_m', 'param_n.subparam'},
                    'device2': set(),
                }
                for tid, data in handler.trains(devices=dev):
                    ...

        Examples
        --------

        >>> h5file = H5File('r0450/RAW-R0450-DA01-S00000.h5')

        Iterate over all trains

        >>> for id, data in h5file.trains():
                pos = data['device_x']['param_n']

        Filter devices and parameters

        >>> dev = {'xray_monitor': {'pulseEnergy', 'beamPosition'},
        ...        'sample_x': {}, 'sample_y': {}}
        >>> trains = h5file.trains(devices=dev)
        >>> traind_id, train_1 = next(trains)
        >>> train_1.keys()
        dict_keys(['xray_monitor', 'sample_x', 'sample_y'])

        The returned data will contains the devices 'xray_monitor' and 2 of
        it's parameters (pulseEnergy and beamPosition), sample_x and
        sample_y (with all of their parameters). All other devices are ignored.
        """
        for index in range(len(self.train_ids)):
            yield self._gen_train_data(index, only_this=devices)

    def train_from_id(self, train_id, devices=None):
        """Get Train data for specified train ID.

        Parameters
        ----------
        train_id: int
            The train ID
        devices: dict, optional
            Filter data by devices and by parameters.

            Refer to :meth:`~.H5File.trains` for how to use this.

        Returns
        -------

        data : dict
            The data for this train, keyed by device name
        tid : int
            The train ID of the returned train
        index : int
            The index of the train within this file, starting from 0.

        Raises
        ------
        KeyError
            if `train_id` is not found in the file.
        """
        try:
            index = self._trains[train_id]
        except KeyError:
            raise KeyError("train {} not found in {}.".format(
                            train_id, self.file.filename))
        else:
            return self._gen_train_data(index, only_this=devices)

    def train_from_index(self, index, devices=None):
        """Get train data of the nth train in file.

        Parameters
        ----------
        index: int
            Index of the train in the file.
        devices: dict, optional
            Filter data by devices and by parameters.

            Refer to :meth:`~.H5File.trains` for how to use this.

        Returns
        -------

        data : dict
            The data for this train, keyed by device name
        tid : int
            The train ID of the returned train
        index : int
            The index of the train within this file, starting from 0.
        """
        return self._gen_train_data(index, only_this=devices)

    def close(self):
        self.file.close()

    # Context manager protocol - enables "with H5File(...):"
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def detector_info(self):
        """Get statistics about the detector data.

        Returns a dictionary with keys:
        - 'dims' (pixel dimensions)
        - 'frames_per_train'
        - 'total_frames'
        """

        img_source = [src for src in self.sources
                      if re.match(r'INSTRUMENT/.+/image', src)][0]
        img_ds = self.file[img_source + '/data']
        img_index = self.index[img_source.split('/', 1)[1]]

        return {
            'dims': img_ds.shape[-2:],
            # Some trains have 0 frames; max is the interesting value
            'frames_per_train': img_index['count'][:].max(),
            'total_frames': img_index['count'][:].sum(),
        }


class RunDirectory:
    """Access data from a 'run' generated at European XFEL.

    A 'run' is a directory containing a number of HDF5 files with data from the
    same time period. This class can read data from the collection of files,
    selected by train and by device.

    Parameters
    ----------
    path: str
        Path to the run directory.
    """
    def __init__(self, path):
        self.files = [H5File(f) for f in glob(osp.join(path, '*.h5'))
                      if h5py.is_hdf5(f)]

        self._trains = {}
        for fhandler in self.files:
            for train in fhandler.train_ids:
                if train not in self._trains:
                    self._trains[train] = []
                self._trains[train].append(fhandler)

        self.ordered_trains = list(sorted(self._trains.items()))

    def trains(self, devices=None):
        """Iterate over all trains in the run and gather all sources.

        ::

            run = Run('/path/to/my/run/r0123')
            for train_id, data in run.trains():
                value = data['device']['parameter']

        Parameters
        ----------
        devices: dict, optional
            Filter data by devices and by parameters.

            Refer to :meth:`H5File.trains` for how to use this.

        Yields
        ------

        tid : int
            The train ID of the returned train
        data : dict
            The data for this train, keyed by device name
        """
        for tid, fhs in self.ordered_trains:
            train_data = {}
            for fh in fhs:
                data, _, _ = fh.train_from_id(tid, devices=devices)
                train_data.update(data)

            yield (tid, train_data)

    def train_from_id(self, train_id, devices=None):
        """Get Train data for specified train ID.

        Parameters
        ----------
        train_id: int
            The train ID
        devices: dict, optional
            Filter data by devices and by parameters.

            Refer to :meth:`H5File.trains` for how to use this.

        Returns
        -------

        tid : int
            The train ID of the returned train
        data : dict
            The data for this train, keyed by device name

        Raises
        ------
        KeyError
            if `train_id` is not found in the run.
        """
        try:
            files = self._trains[train_id]
        except KeyError:
            raise KeyError("train {} not found in run.".format(train_id))
        data = {}
        for fh in files:
            d, _, _ = fh.train_from_id(train_id, devices=devices)
            data.update(d)
        return (train_id, data)

    def train_from_index(self, index, devices=None):
        """Get the nth train in the run.

        Parameters
        ----------
        index: int
            The train index within this run
        devices: dict, optional
            Filter data by devices and by parameters.

            Refer to :meth:`H5File.trains` for how to use this.

        Returns
        -------

        tid : int
            The train ID of the returned train
        data : dict
            The data for this train, keyed by device name

        Raises
        ------
        IndexError
            if train `index` is out of range.
        """
        try:
            train_id, files = self.ordered_trains[index]
        except IndexError:
            raise IndexError("Train index {} out of range.".format(index))
        data = {}
        for fh in files:
            d, _, _ = fh.train_from_id(train_id, devices=devices)
            data.update(d)
        return (train_id, data)

    def _get_devices(self, src):
        """Return sets of control and instrument device names.
        control: train data
        instrument: pulse data
        """
        ctrl, inst = set(), set()
        for file in src:
            ctrl.update(file.control_devices)
            inst.update(file.instrument_device_channels)
        return ctrl, inst

    def info(self):
        """Show information about the run.
        """
        # time info
        first_train, _ = self.ordered_trains[0]
        last_train, _ = self.ordered_trains[-1]
        train_count = len(self.ordered_trains)
        span_sec = (last_train - first_train) / 10
        span_txt = str(datetime.timedelta(seconds=span_sec))

        detector_files, non_detector_files = [], []
        detector_modules = defaultdict(list)
        for f in self.files:
            fni = FilenameInfo(f.path)
            if fni.is_detector:
                detector_files.append(f)
                detector_modules[(fni.detector_name, fni.detector_moduleno)].append(f)
            else:
                non_detector_files.append(f)

        # A run should only have one detector, but if that changes, don't hide it
        detector_name = ','.join(sorted(set(k[0] for k in detector_modules)))

        # devices info
        ctrl, inst = self._get_devices(non_detector_files)

        # disp
        print('# of trains:   ', train_count)
        print('Duration:      ', span_txt)
        print('First train ID:', first_train)
        print('Last train ID: ', last_train)
        print()

        print("{} detector modules ({})".format(
            len(detector_modules), detector_name
        ))
        if len(detector_modules) > 0:
            # Show detail on the first module (the others should be similar)
            mod_key = sorted(detector_modules)[0]
            mod_files = detector_modules[mod_key]
            dinfo = [f.detector_info() for f in mod_files]
            print("  e.g. module {}{} : {} × {} pixels".format(
                *mod_key, *dinfo[0]['dims'],
            ))
            print("  {} frames per train, {} total frames".format(
                max(i['frames_per_train'] for i in dinfo),
                sum(i['total_frames'] for i in dinfo),
            ))
        print()

        print(len(inst), 'instrument devices (excluding detectors):')
        for d in sorted(inst):
            print('  -', d)
        print()
        print(len(ctrl), 'control devices:')
        for d in sorted(ctrl):
            print('  -', d)
        print()

    def train_info(self, train_id):
        """Show information about a specific train in the run.

        Parameters
        ----------
        train_id: int
            The specific train ID you get details information.

        Raises
        ------
        ValueError
            if `train_id` is not found in the run.
        """
        tid, files = next((t for t in self.ordered_trains
                          if t[0] == train_id), (None, None))
        if tid is None:
            raise ValueError("train {} not found in run.".format(train_id))
        ctrl, inst = self._get_devices(files)

        # disp
        print('Train [{}] information'.format(train_id))
        print('Devices')
        print('\tInstruments')
        [print('\t-', d) for d in sorted(inst)] or print('\t-')
        print('\tControls')
        [print('\t-', d) for d in sorted(ctrl)] or print('\t-')

# RunDirectory was previously RunHandler; we'll leave it accessible in case
# any code was already using it.
RunHandler = RunDirectory

def stack_data(train, data, axis=-3, xcept=()):
    """Stack data from devices in a train.

    Parameters
    ----------
    train: dict
        Train data.
    data: str
        The path to the device parameter of the data you want to stack.
    axis: int, optional
        Array axis on which you wish to stack.
    xcept: list
        List of devices to ignore (useful if you have reccored slow data with
        detector data in the same run).

    Returns
    -------
    combined: numpy.array
        Stacked data for requested data path.
    """
    devs = [(list(map(int, re.findall(r'\d+', dev))), dev)
            for dev in train.keys() if dev not in xcept]
    devices = [dev for _, dev in sorted(devs)]

    dtype, shape = next(((d[data].dtype, d[data].shape) for d in train.values()
                        if data in d and 0 not in d[data].shape), (None, None))
    if dtype is None or shape is None:
        return np.empty(0)

    combined = np.zeros((len(devices),) + shape, dtype=dtype)
    for index, device in enumerate(devices):
        try:
            if 0 in train[device][data].shape:
                continue
            combined[index, ] = train[device][data]
        except KeyError:
            print('stack_data(): missing {} in {}'.format(data, device))
    return np.moveaxis(combined, 0, axis)


def stack_detector_data(train, data, axis=-3, modules=16, only='', xcept=()):
    """Stack data from detector modules in a train.

    Parameters
    ----------
    train: dict
        Train data.
    data: str
        The path to the device parameter of the data you want to stack.
    axis: int
        Array axis on which you wish to stack (default is -3).
    modules: int
        Number of modules composing a detector (default is 16).
    only: str
        Only use devices in train containing this substring.
    xcept: list
        List of devices to ignore (useful if you have reccored slow data with
        detector data in the same run).

    Returns
    -------
    combined: numpy.array
        Stacked data for requested data path.
    """
    devices = [dev for dev in train.keys() if only in dev and dev not in xcept]

    dtype, shape = next(((d[data].dtype, d[data].shape) for d in train.values()
                        if data in d and 0 not in d[data].shape), (None, None))
    if dtype is None or shape is None:
        return np.array([])

    combined = np.full((modules, ) + shape, np.nan, dtype=dtype)
    for device in devices:
        index = None
        try:
            if 0 in train[device][data].shape:
                continue
            index = int(re.findall(r'\d+', device)[-2])
            combined[index, ] = train[device][data]
        except KeyError:
            print('stack_detector_data(): missing {} in {}'.format(data, device))
        except IndexError:
            print('stack_detector_Data(): module {} is out or range for a'
                  'detector of {} modules'.format(index, modules))
    return np.moveaxis(combined, 0, axis)


if __name__ == '__main__':
    r = RunDirectory('./data/r0185')
    for tid, d in r.trains():
        print(tid)