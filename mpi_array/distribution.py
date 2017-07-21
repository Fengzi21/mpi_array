"""
========================================
The :mod:`mpi_array.distribution` Module
========================================

Apportionment of arrays over locales.

Classes
=======

.. autosummary::
   :toctree: generated/

   LocaleComms - Intra-locale and inter-locale communicators.
   CartLocaleComms - Intra-locale and cartesian-inter-locale communicators.
   GlobaleExtent - Indexing and halo info for globale array.
   HaloSubExtent - Indexing sub-extent of globale extent.
   LocaleExtent - Indexing and halo info for locale array region.
   CartLocaleExtent - Indexing and halo info for a tile in a cartesian distribution.
   Distribution - Apportionment of extents amongst locales.
   ClonedDistribution - Entire array occurs in each locale.
   SingleLocaleDistribution - Entire array occurs on a single locale.
   BlockPartition - Block partition distribution of array extents amongst locales.
   CommsAndDistribution - Pair consisting of :obj:`LocaleComms` and :obj:`Distribution`.
   ThisLocaleInfo - Info on inter_locale_comm rank and corresponding rank_comm rank.
   RmaWindowBuffer - Container for array buffer and associated RMA windows.

Factory Functions
=================

.. autosummary::
   :toctree: generated/

   create_distribution - Factory function for creating :obj:`Distribution` instances.
   create_block_distribution - Factory function for creating :obj:`BlockPartition` instances.

"""
from __future__ import absolute_import
from .license import license as _license, copyright as _copyright

import pkg_resources as _pkg_resources

import sys as _sys
import mpi4py.MPI as _mpi

import array_split as _array_split
import array_split.split  # noqa: F401
from array_split.split import convert_halo_to_array_form as _convert_halo_to_array_form

import mpi_array.logging as _logging
from mpi_array.indexing import IndexingExtent, HaloIndexingExtent
from mpi_array.update import MpiHalosUpdate

import numpy as _np
import copy as _copy
import collections as _collections

__author__ = "Shane J. Latham"
__license__ = _license()
__copyright__ = _copyright()
__version__ = _pkg_resources.resource_string("mpi_array", "version.txt").decode()


def mpi_version():
    """
    Return the MPI API version.

    :rtype: :obj:`int`
    :return: MPI major version number.
    """
    return _mpi.VERSION


ThisLocaleInfo = _collections.namedtuple("ThisLocaleInfo", ["inter_locale_rank", "rank"])


class LocaleComms(object):

    """
    Info on possible shared memory allocation for a specified MPI communicator.
    """

    def __init__(self, comm=None, intra_locale_comm=None, inter_locale_comm=None):
        """
        Construct.

        :type comm: :obj:`mpi4py.MPI.Comm`
        :param comm: Communicator which is split according to
           shared memory allocation (uses :meth:`mpi4py.MPI.Comm.Split_type`).
        :type intra_locale_comm: :obj:`mpi4py.MPI.Comm`
        :param intra_locale_comm: Intra-locale communicator.
           Should be a subset of processes returned
           by :samp:`{comm}.Split_type(mpi4py.MPI.COMM_TYPE_SHARED)`.
           If :samp:`None`, :samp:`{comm}` is *split* into groups
           which can use a MPI window to allocate shared memory
           (i.e. locale is a (NUMA) node).
           Can also specify as :samp:`mpi4py.MPI.COMM_SELF`, in which case the
           locale is a single process.
        :type inter_locale_comm: :obj:`mpi4py.MPI.Comm`
        :param inter_locale_comm: Inter-locale communicator used to exchange
            data between different locales.
        """
        if comm is None:
            comm = _mpi.COMM_WORLD
        self._rank_comm = comm
        rank_logger = _logging.get_rank_logger(__name__ + "." + self.__class__.__name__, comm)
        if intra_locale_comm is None:
            if mpi_version() >= 3:
                rank_logger.debug(
                    "BEG: Splitting comm with comm.Split_type(COMM_TYPE_SHARED, ...)"
                )
                intra_locale_comm = comm.Split_type(_mpi.COMM_TYPE_SHARED, key=comm.rank)
                rank_logger.debug(
                    "END: Splitting comm with comm.Split_type(COMM_TYPE_SHARED, ...)"
                )
            else:
                intra_locale_comm = _mpi.COMM_SELF

        self._intra_locale_comm = intra_locale_comm

        # Count the number of self._intra_locale_comm rank-0 processes
        # to work out how many communicators comm was split into.
        is_rank_zero = int(self._intra_locale_comm.rank == 0)

        rank_logger.debug("BEG: comm.allreduce to calculate number of locales...")
        self._num_locales = comm.allreduce(is_rank_zero, _mpi.SUM)
        rank_logger.debug("END: comm.allreduce to calculate number of locales.")

        self._inter_locale_comm = None

        if (self._num_locales > 1):
            if inter_locale_comm is None:
                color = _mpi.UNDEFINED
                if self.intra_locale_comm.rank == 0:
                    color = 0
                rank_logger.debug("BEG: self.rank_comm.Split to create self.inter_locale_comm.")
                inter_locale_comm = self._rank_comm.Split(color, self._rank_comm.rank)
                rank_logger.debug("END: self.rank_comm.Split to create self.inter_locale_comm.")
            self._inter_locale_comm = inter_locale_comm
        elif (inter_locale_comm is not None) and (inter_locale_comm != _mpi.COMM_NULL):
            raise ValueError(
                "Got valid inter_local_comm=%s when self.num_locales <= 1"
                %
                (inter_locale_comm, )
            )

        self._rank_logger = \
            _logging.get_rank_logger(
                __name__ + "." + self.__class__.__name__,
                comm=self._rank_comm
            )

        self._root_logger = \
            _logging.get_root_logger(
                __name__ + "." + self.__class__.__name__,
                comm=self._rank_comm
            )

    @property
    def inter_locale_rank_to_rank_map(self):
        """
        """
        return \
            _mpi.Group.Translate_ranks(
                self.inter_locale_comm.group,
                range(0, self.inter_locale_comm.group),
                self.rank_comm.group
            )

    @property
    def this_locale_rank_info(self):
        """
        """
        return ThisLocaleInfo(self.inter_locale_comm.rank, self.rank_comm.rank)

    @property
    def num_locales(self):
        """
        An integer indicating the number of *locales* over which an array is distributed.
        """
        return self._num_locales

    @property
    def rank_comm(self):
        """
        MPI communicator which is super-set of :attr:`intra_locale_comm`
        and :attr:`inter_locale_comm`.
        """
        return self._rank_comm

    @property
    def intra_locale_comm(self):
        """
        A :obj:`mpi4py.MPI.Comm` object which defines the group of processes
        which can allocate (and access) MPI window shared memory
        (allocated via :meth:`mpi4py.MPI.Win.Allocate_shared` if available).
        """
        return self._intra_locale_comm

    @property
    def inter_locale_comm(self):
        """
        A :obj:`mpi4py.MPI.Comm` communicator defining the group of processes
        which exchange data between locales.
        """
        return self._inter_locale_comm

    @inter_locale_comm.setter
    def inter_locale_comm(self, inter_locale_comm):
        self._inter_locale_comm = inter_locale_comm

    @property
    def rank_logger(self):
        """
        A :attr:`rank_comm` :obj:`logging.Logger`.
        """
        return self._rank_logger

    @property
    def root_logger(self):
        """
        A :attr:`rank_comm` :obj:`logging.Logger`.
        """
        return self._root_logger


RmaWindowBuffer = \
    _collections.namedtuple(
        "RmaWindowBuffer",
        [
            "buffer",
            "shape",
            "dtype",
            "itemsize",
            "rank_win",
            "intra_locale_win",
            "inter_locale_win"
        ]
    )


class CartLocaleComms(object):

    """
    Defines cartesian communication topology for locales.
    """

    def __init__(
        self,
        ndims=None,
        dims=None,
        rank_comm=None,
        intra_locale_comm=None,
        inter_locale_comm=None,
        cart_comm=None
    ):
        """
        Initialises cartesian communicator for inter-locale data exchange.
        Need to specify at least one of the :samp:`{ndims}` or :samp:`{dims}`.
        to indicate the dimension of the cartesian partitioning.

        :type ndims: :obj:`int`
        :param ndims: Dimension of the cartesian partitioning, e.g. 1D, 2D, 3D, etc.
           If :samp:`None`, :samp:`{ndims}=len({dims})`.
        :type dims: sequence of :obj:`int`
        :param dims: The number of partitions along each array axis, zero elements
           are replaced with positive integers such
           that :samp:`numpy.product({dims}) == {rank_comm}.size`.
           If :samp:`None`, :samp:`{dims} = (0,)*{ndims}`.
        :type rank_comm: :obj:`mpi4py.MPI.Comm`
        :param rank_comm: The MPI processes which will have access
           (via a :obj:`mpi4py.MPI.Win` object) to the distributed array.
           If :samp:`None` uses :obj:`mpi4py.MPI.COMM_WORLD`.
        :type intra_locale_comm: :obj:`mpi4py.MPI.Comm`
        :param intra_locale_comm: The MPI communicator used to create a window which
            can be used to allocate shared memory
            via :meth:`mpi4py.MPI.Win.Allocate_shared`.
        :type inter_locale_comm: :obj:`mpi4py.MPI.Comm`
        :param inter_locale_comm: Inter-locale communicator used to exchange
            data between different locales.
        :type cart_comm: :obj:`mpi4py.MPI.Comm`
        :param cart_comm: Cartesian topology inter-locale communicator used to exchange
            data between different locales.
        """
        object.__init__(self)
        # No implementation for periodic boundaries yet
        periods = None
        if (ndims is None) and (dims is None):
            raise ValueError("Must specify one of dims or ndims in CartLocaleComms constructor.")
        elif (ndims is not None) and (dims is not None) and (len(dims) != ndims):
            raise ValueError(
                "Length of dims (len(dims)=%s) not equal to ndims=%s." % (len(dims), ndims)
            )
        elif ndims is None:
            ndims = len(dims)

        if dims is None:
            dims = _np.zeros((ndims,), dtype="int")
        if periods is None:
            periods = _np.zeros((ndims,), dtype="bool")
        if rank_comm is None:
            rank_comm = _mpi.COMM_WORLD

        self._locale_comms = \
            LocaleComms(
                comm=rank_comm,
                intra_locale_comm=intra_locale_comm,
                inter_locale_comm=inter_locale_comm
            )

        self._cart_comm = cart_comm
        rank_logger = \
            _logging.get_rank_logger(__name__ + "." + self.__class__.__name__, comm=self.rank_comm)

        self._dims = \
            _array_split.split.calculate_num_slices_per_axis(
                dims,
                self.num_locales
            )

        # Create a cartesian grid communicator
        inter_locale_comm = self._locale_comms._inter_locale_comm
        if self.num_locales > 1:
            if (inter_locale_comm != _mpi.COMM_NULL) and (cart_comm is None):
                rank_logger.debug("BEG: inter_locale_comm.Create to create cart_comm.")
                cart_comm = \
                    inter_locale_comm.Create_cart(
                        self.dims,
                        periods,
                        reorder=True
                    )
                rank_logger.debug("END: inter_locale_comm.Create to create cart_comm.")
            elif (inter_locale_comm == _mpi.COMM_NULL) and (cart_comm is None):
                cart_comm = _mpi.COMM_NULL
            elif cart_comm != _mpi.COMM_NULL:
                raise ValueError(
                    (
                        "Got object cart_comm=%s when expecting cart_comm to match "
                        +
                        "self._inter_locale_comm=%s"
                    )
                    %
                    (cart_comm, inter_locale_comm)
                )
            self._cart_comm = cart_comm
        elif (cart_comm is not None) and (cart_comm != _mpi.COMM_NULL):
            raise ValueError(
                "Got object cart_comm=%s when self.num_locales <= 1, should be None"
                %
                (cart_comm, )
            )

    def alloc_locale_buffer(self, shape, dtype):
        """
        Allocates a buffer using :meth:`mpi4py.MPI.Win.Allocate_shared` which
        provides storage for the elements of the local (memory-node) multi-dimensional array.

        :rtype: :obj:`RmaWindowBuffer`
        :returns: A :obj:`collections.namedtuple` containing allocated buffer
           and associated RMA MPI windows.
        """
        self.rank_logger.debug("BEG: alloc_locale_buffer")
        num_rank_bytes = 0
        dtype = _np.dtype(dtype)
        rank_shape = shape
        if self.intra_locale_comm.rank == 0:
            num_rank_bytes = int(_np.product(rank_shape) * dtype.itemsize)
        if (mpi_version() >= 3) and (self.intra_locale_comm.size > 1):
            self.rank_logger.debug("BEG: Win.Allocate_shared - allocating %d bytes", num_rank_bytes)
            intra_locale_win = \
                _mpi.Win.Allocate_shared(num_rank_bytes, dtype.itemsize,
                                         comm=self.intra_locale_comm)
            self.rank_logger.debug("END: Win.Allocate_shared - allocating %d bytes", num_rank_bytes)
            buffer, itemsize = intra_locale_win.Shared_query(0)
            self.rank_logger.debug("BEG: Win.Create for self.rank_comm")
            rank_win = _mpi.Win.Create(buffer, itemsize, comm=self.rank_comm)
            self.rank_logger.debug("END: Win.Create for self.rank_comm")
        else:
            self.rank_logger.debug("BEG: Win.Allocate - allocating %d bytes", num_rank_bytes)
            rank_win = \
                _mpi.Win.Allocate(num_rank_bytes, dtype.itemsize, comm=self.rank_comm)
            self.rank_logger.debug("END: Win.Allocate - allocating %d bytes", num_rank_bytes)
            intra_locale_win = rank_win
            buffer = rank_win.memory
            itemsize = dtype.itemsize

        inter_locale_win = None
        if self.num_locales > 1:
            inter_locale_win = _mpi.WIN_NULL
            if self.have_valid_cart_comm:
                self.rank_logger.debug("BEG: Win.Create for self.cart_comm")
                inter_locale_win = _mpi.Win.Create(buffer, itemsize, comm=self.cart_comm)
                self.rank_logger.debug("END: Win.Create for self.cart_comm")

        buffer = memoryview(_np.array(buffer, dtype='B', copy=False).data)

        self.rank_logger.debug("END: alloc_local_buffer")
        return \
            RmaWindowBuffer(
                buffer=buffer,
                shape=rank_shape,
                dtype=dtype,
                itemsize=itemsize,
                rank_win=rank_win,
                intra_locale_win=intra_locale_win,
                inter_locale_win=inter_locale_win
            )

    @property
    def cart_coord_to_cart_rank_map(self):
        """
        A :obj:`dict` of :obj:`tuple`
        cartesian coordinate (:meth:`mpi4py.MPI.CartComm.Get_coords`) keys
        which map to the associated :attr:`cart_comm` rank.
        """
        d = dict()
        if self.have_valid_cart_comm:
            for cart_rank in range(self.cart_comm.size):
                d[tuple(self.cart_comm.Get_coords(cart_rank))] = cart_rank
        elif self.cart_comm is None:
            d = None
        return d

    @property
    def inter_locale_rank_to_rank_map(self):
        """
        """
        m = None
        if self.have_valid_cart_comm:
            m = \
                _mpi.Group.Translate_ranks(
                    self.cart_comm.group,
                    range(0, self.cart_comm.group.size),
                    self.rank_comm.group
                )
        return m

    @property
    def this_locale_rank_info(self):
        """
        """
        if self.have_valid_cart_comm:
            i = ThisLocaleInfo(self.cart_comm.rank, self.rank_comm.rank)
        else:
            i = ThisLocaleInfo(0, 0)
        return i

    @property
    def dims(self):
        """
        The number of partitions along each array axis. Defines
        the cartesian topology over which an array is distributed.
        """
        return self._dims

    @property
    def ndim(self):
        """
        Dimension (:obj:`int`) of the cartesian topology.
        """
        return self._dims.size

    @property
    def have_valid_cart_comm(self):
        """
        Is :samp:`True` if this rank has :samp:`{self}.cart_comm`
        which is not :samp:`None` and is not :obj:`mpi4py.MPI.COMM_NULL`.
        """
        return \
            (
                (self.cart_comm is not None)
                and
                (self.cart_comm != _mpi.COMM_NULL)
            )

    @property
    def rank_comm(self):
        """
        A :obj:`mpi4py.MPI.Comm` communicator defining all processes in all
        locales over which an  array is to be distributed
        (i.e. all processes which have direct memory access
        to some region, possibly empty, of array elements).
        """
        return self._locale_comms.rank_comm

    @property
    def cart_comm(self):
        """
        A :obj:`mpi4py.MPI.CartComm` communicator defining a cartesian topology of
        MPI processes (typically one process per locale) used for inter-locale
        exchange of array data.
        """
        return self._cart_comm

    @property
    def num_locales(self):
        """
        See :attr:`LocaleComms.num_locales`.
        """
        return self._locale_comms.num_locales

    @property
    def intra_locale_comm(self):
        """
        See :attr:`LocaleComms.intra_locale_comm`.
        """
        return self._locale_comms.intra_locale_comm

    @property
    def inter_locale_comm(self):
        """
        See :attr:`LocaleComms.inter_locale_comm`.
        """
        return self._locale_comms._inter_locale_comm

    @property
    def rank_logger(self):
        """
        A :attr:`rank_comm` :obj:`logging.Logger`.
        """
        return self._locale_comms._rank_logger

    @property
    def root_logger(self):
        """
        A :attr:`rank_comm` :obj:`logging.Logger`.
        """
        return self._locale_comms._root_logger


if (_sys.version_info[0] >= 3) and (_sys.version_info[1] >= 5):
    # Set docstring for properties.
    CartLocaleComms.num_locales.__doc__ = LocaleComms.num_locales.__doc__
    CartLocaleComms.intra_locale_comm.__doc__ = LocaleComms.intra_locale_comm.__doc__


class GlobaleExtent(HaloIndexingExtent):

    """
    Indexing extent for an entire array.
    """

    pass


class HaloSubExtent(HaloIndexingExtent):

    """
    Indexing extent for single region of a larger globale extent.
    """

    def __init__(
        self,
        globale_extent,
        slice=None,
        halo=0,
        start=None,
        stop=None
    ):
        """
        Construct. Takes care of trimming the halo of this extent so
        that this extent does not stray outside the halo region of
        the :samp:`{globale_extent}`

        :type globale_extent: :obj:`GlobaleExtent`
        :param globale_extent: The indexing extent of the entire array.
        :type slice: sequence of :obj:`slice`
        :param slice: Per-axis start and stop indices (**not including ghost elements**).
        :type halo: :samp:`(len({split}), 2)` shaped array of :obj:`int`
        :param halo: Desired halo, a :samp:`(len(self.start), 2)` shaped array of :obj:`int`
           indicating the per-axis number of outer ghost elements. :samp:`halo[:,0]` is the
           number of ghost elements on the low-index *side* and :samp:`halo[:,1]` is the number
           of ghost elements on the high-index *side*. **Note**: that the halo will be truncated
           so that this halo extent does not extend beyond the halo :samp:`{globale_extent}`.
        :type start: sequence of :obj:`slice`
        :param start: Per-axis start indices (**not including ghost elements**).
        :type stop: sequence of :obj:`slice`
        :param stop: Per-axis stop indices (**not including ghost elements**).
        """
        HaloIndexingExtent.__init__(self, slice=slice, start=start, stop=stop, halo=None)
        halo = _convert_halo_to_array_form(halo, ndim=self.ndim)
        # Calculate the locale halo, truncate if it strays outside
        # the globale_extent halo region.
        halo = \
            _np.maximum(
                _np.array((0,), dtype=halo.dtype),
                _np.array(
                    (
                        _np.minimum(
                            self.start_n - globale_extent.start_h,
                            halo[:, self.LO]
                        ),
                        _np.minimum(
                            globale_extent.stop_h - self.stop_n,
                            halo[:, self.HI]
                        ),
                    ),
                    dtype=halo.dtype
                ).T
            )
        self._halo = halo


class LocaleExtent(HaloSubExtent):

    """
    Indexing extent for single region of array residing on a locale.
    """

    def __init__(
        self,
        rank,
        inter_locale_rank,
        globale_extent,
        slice=None,
        halo=0,
        start=None,
        stop=None
    ):
        """
        Construct.

        :type rank: :obj:`int`
        :param rank: Rank of MPI process in :samp:`rank_comm` communicator which
           corresponds to :samp:`{inter_locale_rank}` rank of :samp:`{inter_locale_comm}`.
        :type inter_locale_rank: :obj:`int`
        :param inter_locale_rank: Rank of MPI process in :samp:`inter_locale_comm` communicator.
        :type globale_extent: :obj:`GlobaleExtent`
        :param globale_extent: The indexing extent of the entire array.
        :type slice: sequence of :obj:`slice`
        :param slice: Per-axis start and stop indices (**not including ghost elements**).
        :type halo: :samp:`(len({split}), 2)` shaped array of :obj:`int`
        :param halo: Desired halo, a :samp:`(len(self.start), 2)` shaped array of :obj:`int`
           indicating the per-axis number of outer ghost elements. :samp:`halo[:,0]` is the
           number of ghost elements on the low-index *side* and :samp:`halo[:,1]` is the number
           of ghost elements on the high-index *side*. **Note**: that the halo will be truncated
           so that this halo extent does not extend beyond the halo :samp:`{globale_extent}`.
        :type start: sequence of :obj:`slice`
        :param start: Per-axis start indices (**not including ghost elements**).
        :type stop: sequence of :obj:`slice`
        :param stop: Per-axis stop indices (**not including ghost elements**).
        """
        self._rank = rank
        self._inter_locale_rank = inter_locale_rank
        HaloSubExtent.__init__(
            self,
            globale_extent=globale_extent,
            slice=slice,
            start=start,
            stop=stop,
            halo=halo
        )

    def __eq__(self, other):
        """
        Equality
        """
        return \
            (
                HaloSubExtent.__eq__(self, other)
                and
                (self.rank == other.rank)
                and
                (self.inter_locale_rank == other.inter_locale_rank)
            )

    @property
    def rank(self):
        """
        MPI rank of the process in the :samp:`rank_comm` communicator
        which corresponds to the :attr:`inter_locale_rank` in
        the :samp:`inter_locale_comm` communicator.
        """
        return self._rank

    @property
    def inter_locale_rank(self):
        """
        MPI rank of the process in the :samp:`inter_locale_comm`.
        """
        return self._inter_locale_rank

    def halo_slab_extent(self, axis, dir):
        """
        Returns indexing extent of the halo *slab* for specified axis.

        :type axis: :obj:`int`
        :param axis: Indexing extent of halo slab for this axis.
        :type dir: :attr:`LO` or :attr:`HI`
        :param dir: Indicates low-index halo slab or high-index halo slab.
        :rtype: :obj:`IndexingExtent`
        :return: Indexing extent for halo slab.
        """
        start = self.start_h.copy()
        stop = self.stop_h.copy()
        if dir == self.LO:
            stop[axis] = start[axis] + self.halo[axis, self.LO]
        else:
            start[axis] = stop[axis] - self.halo[axis, self.HI]

        return \
            IndexingExtent(
                start=start,
                stop=stop
            )

    def no_halo_extent(self, axis):
        """
        Returns the indexing extent identical to this extent, except
        has the halo trimmed from the axis specified by :samp:`{axis}`.

        :type axis: :obj:`int` or sequence of :obj:`int`
        :param axis: Axis (or axes) for which halo is trimmed.
        :rtype: :obj:`IndexingExtent`
        :return: Indexing extent with halo trimmed from specified axis (or axes) :samp:`{axis}`.
        """
        start = self.start_h.copy()
        stop = self.stop_h.copy()
        if axis is not None:
            start[axis] += self.halo[axis, self.LO]
            stop[axis] -= self.halo[axis, self.HI]

        return \
            IndexingExtent(
                start=start,
                stop=stop
            )

    def __repr__(self):
        """
        Stringize.
        """
        return \
            (
                (
                    "LocaleExtent(start=%s, stop=%s, halo=%s, rank=%s, inter_locale_rank=%s)"
                )
                %
                (
                    repr(self.start_n.tolist()),
                    repr(self.stop_n.tolist()),
                    repr(self.halo.tolist()),
                    repr(self.rank),
                    repr(self.inter_locale_rank),
                )
            )

    def __str__(self):
        """
        """
        return self.__repr__()


class CartLocaleExtent(LocaleExtent):

    """
    Indexing extents for single tile of cartesian domain distribution.
    """

    def __init__(
        self,
        rank,
        inter_locale_rank,
        cart_coord,
        cart_shape,
        globale_extent,
        slice=None,
        halo=None,
        start=None,
        stop=None
    ):
        """
        Construct.

        :type rank: :obj:`int`
        :param rank: Rank of MPI process in :samp:`rank_comm` communicator which
           corresponds to the :samp:`{inter_locale_rank}` rank in the :samp:`cart_comm`
           cartesian communicator.
        :type inter_locale_rank: :obj:`int`
        :param inter_locale_rank: Rank of MPI process in :samp:`cart_comm` cartesian communicator
           which corresponds to the :samp:`{rank_comm}` rank in the :samp:`rank_comm` communicator.
        :type cart_coord: sequence of :obj:`int`
        :param cart_coord: Coordinate index (:meth:`mpi4py.MPI.CartComm.Get_coordinate`) of
           this :obj:`LocaleExtent` in the cartesian domain distribution.
        :type cart_shape: sequence of :obj:`int`
        :param cart_shape: Number of :obj:`LocaleExtent` regions in each axis direction
           of the cartesian distribution.
        :type globale_extent: :obj:`GlobaleExtent`
        :param globale_extent: The indexing extent of the entire array.
        :type slice: sequence of :obj:`slice`
        :param slice: Per-axis start and stop indices (**not including ghost elements**).
        :type halo: :samp:`(len({split}), 2)` shaped array of :obj:`int`
        :param halo: Desired halo, a :samp:`(len(self.start), 2)` shaped array of :obj:`int`
           indicating the per-axis number of outer ghost elements. :samp:`halo[:,0]` is the
           number of ghost elements on the low-index *side* and :samp:`halo[:,1]` is the number
           of ghost elements on the high-index *side*. **Note**: that the halo will be truncated
           so that this halo extent does not extend beyond the halo :samp:`{globale_extent}`.
        :type start: sequence of :obj:`slice`
        :param start: Per-axis start indices (**not including ghost elements**).
        :type stop: sequence of :obj:`slice`
        :param stop: Per-axis stop indices (**not including ghost elements**).
        """
        LocaleExtent.__init__(
            self,
            rank=rank,
            inter_locale_rank=inter_locale_rank,
            globale_extent=globale_extent,
            slice=slice,
            halo=halo,
            start=start,
            stop=stop
        )
        self._cart_coord = _np.array(cart_coord, dtype="int64")
        self._cart_shape = _np.array(cart_shape, dtype=self._cart_coord.dtype)

    def __eq__(self, other):
        """
        Equality.
        """
        return \
            (
                LocaleExtent.__eq__(self, other)
                and
                _np.all(self.cart_coord == other.cart_coord)
                and
                _np.all(self.cart_shape == other.cart_shape)
            )

    @property
    def cart_rank(self):
        """
        Rank of MPI process in :samp:`cart_comm` cartesian communicator
        which corresponds to the :attr:`{rank}` rank in the :samp:`rank_comm` communicator.
        """
        return self.inter_locale_rank

    @property
    def cart_coord(self):
        """
        Coordinate index (:meth:`mpi4py.MPI.CartComm.Get_coordinate`) of
        this :obj:`LocaleExtent` in the cartesian domain distribution.
        """
        return self._cart_coord

    @property
    def cart_shape(self):
        """
        Number of :obj:`LocaleExtent` regions in each axis direction
        of the cartesian distribution.
        """
        return self._cart_shape

    def __repr__(self):
        """
        Stringize.
        """
        return \
            (
                (
                    "CartLocaleExtent(start=%s, stop=%s, halo=%s, rank=%s, inter_locale_rank=%s, "
                    +
                    "cart_coord=%s, cart_shape=%s)"
                )
                %
                (
                    repr(self.start_n.tolist()),
                    repr(self.stop_n.tolist()),
                    repr(self.halo.tolist()),
                    repr(self.rank),
                    repr(self.inter_locale_rank),
                    repr(tuple(self.cart_coord)),
                    repr(tuple(self.cart_shape)),
                )
            )

    def __str__(self):
        """
        """
        return self.__repr__()


class Distribution(object):

    """
    Describes the apportionment of array extents amongst locales.
    """

    def __init__(
        self,
        globale_extent,
        locale_extents,
        halo=0,
        globale_extent_type=LocaleExtent,
        locale_extent_type=GlobaleExtent,
        inter_locale_rank_to_rank=None
    ):
        """
        Initialise.
        """
        self._locale_extent_type = locale_extent_type
        self._globale_extent_type = globale_extent_type
        self._inter_locale_rank_to_rank = inter_locale_rank_to_rank

        self._globale_extent = self.create_globale_extent(globale_extent, halo=0)
        self._halo = \
            _convert_halo_to_array_form(halo=_copy.deepcopy(halo), ndim=self._globale_extent.ndim)
        self._locale_extents = _copy.copy(locale_extents)
        for i in range(len(locale_extents)):
            self._locale_extents[i] = \
                self.create_locale_extent(i, locale_extents[i], self._globale_extent, halo)

    def get_rank(self, inter_locale_rank):
        """
        """
        rank = _mpi.UNDEFINED
        if self._inter_locale_rank_to_rank is not None:
            rank = self._inter_locale_rank_to_rank[inter_locale_rank]
        return rank

    def create_globale_extent(self, globale_extent, halo=0):
        """
        Factory function for creating :obj:`GlobaleExtent` object.
        """

        # Don't support globale halo/border yet.
        halo = 0
        if isinstance(globale_extent, GlobaleExtent):
            globale_extent = _copy.deepcopy(globale_extent)
            globale_extent.halo = halo
        elif (
            hasattr(globale_extent, "__iter__")
            and
            _np.all([isinstance(e, slice) for e in iter(globale_extent)])
        ):
            globale_extent = GlobaleExtent(slice=globale_extent, halo=halo)
        elif (
            (hasattr(globale_extent, "__iter__") or hasattr(globale_extent, "__getitem__"))
            and
            _np.all(
                [
                    (hasattr(e, "__int__") or hasattr(e, "__long__"))
                    for e in iter(globale_extent)
                ]
            )
        ):
            stop = _np.array(globale_extent)
            globale_extent = GlobaleExtent(start=_np.zeros_like(stop), stop=stop, halo=halo)
        elif hasattr(globale_extent, "start") and hasattr(globale_extent, "stop"):
            globale_extent = \
                GlobaleExtent(start=globale_extent.start, stop=globale_extent.stop, halo=halo)
        else:
            raise ValueError(
                "Could not construct %s instance from globale_extent=%s."
                %
                (self._globale_extent.__class__.__name__, globale_extent,)
            )

        return globale_extent

    def create_locale_extent(
            self,
            inter_locale_rank,
            locale_extent,
            globale_extent,
            halo=0,
            **kwargs
    ):
        """
        Factory function for creating :obj:`LocaleExtent` object.
        """
        rank = self.get_rank(inter_locale_rank)
        if hasattr(locale_extent, "start") and hasattr(locale_extent, "stop"):
            locale_extent = \
                self._locale_extent_type(
                    rank=rank,
                    inter_locale_rank=inter_locale_rank,
                    globale_extent=globale_extent,
                    start=locale_extent.start,
                    stop=locale_extent.stop,
                    halo=halo,
                    **kwargs
                )
        elif (
            (hasattr(locale_extent, "__iter__") or hasattr(locale_extent, "__getitem__"))
            and
            _np.all([isinstance(e, slice) for e in locale_extent])
        ):
            locale_extent = \
                self._locale_extent_type(
                    rank=rank,
                    inter_locale_rank=inter_locale_rank,
                    globale_extent=globale_extent,
                    slice=locale_extent,
                    halo=halo,
                    **kwargs
                )
        else:
            raise ValueError(
                "Could not construct %s instance from locale_extent=%s."
                %
                (self._locale_extent_type.__class__.__name__, locale_extent,)
            )

        return locale_extent

    def get_extent_for_rank(self, inter_locale_rank):
        """
        Returns extent associated with the specified rank
        of the :attr:`inter_locale_comm` communicator.
        """
        return self._locale_extents[inter_locale_rank]

    @property
    def halo(self):
        """
        """
        return self._halo

    @property
    def globale_extent(self):
        """
        The global indexing extent (:obj:`GlobaleExtent`) for the distributed array.
        """
        return self._globale_extent

    @property
    def locale_extents(self):
        """
        Sequence of :samp:`LocaleExtent` objects where :samp:`locale_extents[r]`
        is the extent assigned to locale with :samp:`inter_locale_comm` rank :samp:`r`.
        """
        return self._locale_extents

    @property
    def num_locales(self):
        """
        Number (:obj:`int`) of locales in this distribution.
        """
        return len(self._locale_extents)


class ClonedDistribution(Distribution):

    """
    Distribution where entire globale extent elements occur on every locale.
    """

    def __init__(self, globale_extent, num_locales, halo=0):
        """
        Initialise.
        """
        Distribution.__init__(
            self,
            globale_extent=globale_extent,
            locale_extents=[globale_extent.deep_copy() for i in range(num_locales)],
            halo=halo
        )


class SingleLocaleDistribution(Distribution):

    """
    Distribution where entire globale extent elements occur on just a single locale.
    """

    def __init__(self, globale_extent, num_locales, inter_locale_rank=0, halo=0):
        """
        Initialise.
        """
        self._halo = halo
        globale_extent = self.create_globale_extent(globale_extent)
        sidx = _np.array(globale_extent.start_n)
        locale_extents = [HaloIndexingExtent(start=sidx, stop=sidx) for i in range(num_locales)]
        locale_extent = locale_extents[inter_locale_rank]
        locale_extent.start_n = globale_extent.start_n
        locale_extent.stop_n = globale_extent.stop_n
        Distribution.__init__(
            self,
            globale_extent=globale_extent,
            locale_extents=locale_extents,
            halo=halo
        )


class BlockPartition(Distribution):

    """
    Block partition of an array (shape) over locales.
    """

    #: The "low index" indices.
    LO = HaloIndexingExtent.LO

    #: The "high index" indices.
    HI = HaloIndexingExtent.HI

    def __init__(
        self,
        globale_extent,
        dims,
        cart_coord_to_cart_rank,
        halo=0,
        order="C",
        inter_locale_rank_to_rank=None
    ):
        """
        Create a partitioning of :samp:`{shape}` over locales.

        :type globale_extent: :obj:`GlobaleExtent`
        :param globale_extent: The globale extent to be partitioned.
        :type dims: sequence of :obj:`int`
        :param dims: The number of partitions along each
            dimension, :samp:`len({dims}) == len({globale_extent}.shape_n)`
            and :samp:`num_locales = numpy.product({dims})`.
        :type halo: :obj:`int`, sequence of :obj:`int` or :samp:`(len({shape}), 2)` shaped array.
        :param halo: Number of *ghost* elements added per axis
           (low-index number of ghost elements may differ to the
           number of high-index ghost elements).
        :type cart_coord_to_cart_rank: :obj:`dict`
        :param cart_coord_to_cart_rank: Mapping between cartesian
           communicator coordinate (:meth:`mpi4py.MPI.CartComm.Get_coords`)
           and cartesian communicator rank.
        """
        globale_extent = self.create_globale_extent(globale_extent, halo)
        self._num_locales = _np.product(dims)
        self._dims = dims
        self._rank_logger = None
        self._root_logger = None
        self._order = order

        if self._num_locales > 1:
            shape_splitter = \
                _array_split.ShapeSplitter(
                    array_shape=globale_extent.shape_n,
                    array_start=globale_extent.start_n,
                    axis=self._dims,
                    halo=0
                )
            splt = shape_splitter.calculate_split()

            locale_extents = _np.empty(splt.size, dtype="object")
            for i in range(locale_extents.size):
                cart_coord = tuple(_np.unravel_index(i, splt.shape))
                locale_extents[cart_coord_to_cart_rank[cart_coord]] = splt[cart_coord]
        else:
            locale_extents = [globale_extent, ]
            if cart_coord_to_cart_rank is None:
                cart_coord_to_cart_rank = {tuple(_np.zeros_like(globale_extent.shape_n)): 0}

        self._cart_coord_to_cart_rank = cart_coord_to_cart_rank
        self._cart_rank_to_cart_coord_map = \
            {cart_coord_to_cart_rank[c]: c for c in cart_coord_to_cart_rank.keys()}
        Distribution.__init__(
            self,
            globale_extent=globale_extent,
            locale_extents=locale_extents,
            inter_locale_rank_to_rank=inter_locale_rank_to_rank,
            halo=halo,
            locale_extent_type=CartLocaleExtent
        )

    def create_locale_extent(
            self,
            inter_locale_rank,
            locale_extent,
            globale_extent,
            halo=0,
            **kwargs
    ):
        return \
            Distribution.create_locale_extent(
                self,
                inter_locale_rank,
                locale_extent,
                globale_extent,
                halo,
                cart_coord=self._cart_rank_to_cart_coord_map[inter_locale_rank],
                cart_shape=self._dims
            )

    def recalculate(self, new_shape, new_halo):
        """
        Recomputes distribution for :samp:`{new_shape}` and :samp:`{new_halo}`.

        :type new_shape: sequence of :obj:`int`
        :param new_shape: New partition calculated for this shape.
        :type new_halo: :obj:`int`, sequence of :obj:`int` or :samp:`(len{new_shape, 2))` array.
        :param new_halo: New partition calculated for this shape.
        """
        if self._locale_comms is None:
            self._locale_comms = CartLocaleComms(ndims=len(new_shape))
        elif (self._globale_extent is not None) and (self._globale_extent.ndim != len(new_shape)):
            new_shape = _np.array(new_shape)
            self._locale_comms = \
                CartLocaleComms(
                    rank_comm=self._locale_comms.rank_comm,
                    intra_locale_comm=self._locale_comms.intra_locale_comm,
                    inter_locale_comm=self._locale_comms.inter_locale_comm,
                    ndims=len(new_shape)
                )
        self._halo = new_halo
        self._globale_extent = GlobaleExtent(stop=new_shape)

        shape_splitter = \
            _array_split.ShapeSplitter(
                array_shape=self._globale_extent.shape_n,
                axis=self._locale_comms.dims,
                halo=0
            )

        self._halo = _convert_halo_to_array_form(halo=self._halo, ndim=len(self.shape))

        self._shape_decomp = shape_splitter.calculate_split()

        self._cart_rank_to_extents_dict = None
        self._halo_updates_dict = None
        self._lndarray_extent = None
        if self.have_valid_cart_comm:
            cart_dims = _np.array(self.cart_comm.dims)
            self._cart_rank_to_extents_dict = dict()
            self._halo_updates_dict = dict()
            for cart_rank in range(0, self.cart_comm.size):
                cart_coords = _np.array(self.cart_comm.Get_coords(cart_rank))
                self._cart_rank_to_extents_dict[cart_rank] = \
                    CartLocaleExtent(
                        rank=self.rank_comm.rank,
                        cart_rank=cart_rank,
                        cart_coord=cart_coords,
                        cart_shape=cart_dims,
                        globale_extent=self._globale_extent,
                        slice=self._shape_decomp[tuple(cart_coords)],
                        halo=self._halo
                    )  # noqa: E123
            for cart_rank in range(0, self.cart_comm.size):
                self._halo_updates_dict[cart_rank] = \
                    MpiHalosUpdate(
                        cart_rank,
                        self._cart_rank_to_extents_dict
                )
            self._lndarray_extent = self._cart_rank_to_extents_dict[self.cart_comm.rank]
        elif self.num_locales <= 1:
            slice_tuple = tuple([slice(0, self.shape[i]) for i in range(self.ndim)])
            self._lndarray_extent = \
                    CartLocaleExtent(
                        rank=0,
                        cart_rank=0,
                        cart_coord=[0, ] * self._globale_extent.ndim,
                        cart_shape=[1, ] * self._globale_extent.ndim,
                        globale_extent=self._globale_extent,
                        slice=slice_tuple,
                        halo=self._halo
                    )  # noqa: E123
            self._cart_rank_to_extents_dict =\
                {self._lndarray_extent.cart_rank: self._lndarray_extent}

        self._lndarray_extent, self._cart_rank_to_extents_dict = \
            self.intra_locale_comm.bcast(
                (self._lndarray_extent, self._cart_rank_to_extents_dict), 0)

        self._lndarray_view_slice_n = \
            IndexingExtent(
                start=self._lndarray_extent.halo[:, self.LO],
                stop=self._lndarray_extent.halo[:, self.LO] + self._lndarray_extent.shape_n
            ).to_slice()

        self.calculate_rank_view_slices()

    def get_updates_for_cart_rank(self, cart_rank):
        return self._halo_updates_dict[cart_rank]

    def __str__(self):
        """
        Stringify.
        """
        s = [str(le) for le in self.locale_extents]
        return ", ".join(s)

    @property
    def shape_decomp(self):
        """
        The partition of :samp:`self.shape` over memory nodes.
        """
        return self._shape_decomp

    @property
    def intra_locale_comm(self):
        """
        See :attr:`CartLocaleComms.intra_locale_comm`.
        """
        return self._locale_comms.intra_locale_comm

    @property
    def cart_comm(self):
        """
        See :attr:`CartLocaleComms.cart_comm`.
        """
        return self._locale_comms.cart_comm

    @property
    def inter_locale_comm(self):
        """
        See :attr:`cart_comm`.
        """
        return self.cart_comm

    @property
    def cart_win(self):
        """
        Window for RMA updates.
        """
        return self._cart_win

    @property
    def inter_locale_win(self):
        """
        See :attr:`cart_win`.
        """
        return self.cart_win

    @property
    def have_valid_cart_comm(self):
        """
        See :attr:`CartLocaleComms.have_valid_cart_comm`.
        """
        return self._locale_comms.have_valid_cart_comm

    @property
    def have_valid_inter_locale_comm(self):
        """
        See :attr:`have_valid_cart_comm`.
        """
        return self.have_valid_cart_comm

    @property
    def rank_comm(self):
        """
        See :attr:`CartLocaleComms.rank_comm`.
        """
        return self._locale_comms.rank_comm

    @property
    def rank_view_slice_n(self):
        """
        A :obj:`tuple` of :obj:`slice` indicating the tile (no halo)
        associated with this MPI process (i.e. rank :samp:`self.rank_comm.rank`).
        """
        return self._rank_view_slice_n

    @property
    def rank_view_slice_h(self):
        """
        A :obj:`tuple` of :obj:`slice` indicating the tile (including halo)
        associated with this MPI process (i.e. rank :samp:`self.rank_comm.rank`).
        """
        return self._rank_view_slice_h

    @property
    def rank_view_relative_slice_n(self):
        """
        A :obj:`tuple` of :obj:`slice` which can be used to *slice* (remove)
        the halo from a halo rank view. For example::

           import mpi_array.locale
           lary = mpi_array.locale.zeros((10, 10, 100), dtype="float32")
           _np.all(
               lary.rank_view_h[lary.decomp.rank_view_relative_slice_n]
               ==
               lary.rank_view_n
           )

        """
        return self._rank_view_relative_slice_n

    @property
    def lndarray_extent(self):
        """
        The extent of the locale array.
        """
        return self._lndarray_extent

    @property
    def lndarray_view_slice_n(self):
        """
        Indexing slice which can be used to generate a view of :obj:`mpi_array.locale.lndarray`
        which has the halo removed.
        """
        return self._lndarray_view_slice_n

    @property
    def rank_logger(self):
        """
        A :obj:`logging.Logger` for :attr:`rank_comm` communicator ranks.
        """
        if self._rank_logger is None:
            self._rank_logger = \
                _logging.get_rank_logger(
                    __name__ + "." + self.__class__.__name__,
                    comm=self.rank_comm
                )
        return self._rank_logger

    @property
    def root_logger(self):
        """
        A :obj:`logging.Logger` for rank 0 of the :attr:`rank_comm` communicator.
        """
        if self._root_logger is None:
            self._root_logger = \
                _logging.get_root_logger(
                    __name__ + "." + self.__class__.__name__,
                    comm=self.rank_comm
                )
        return self._root_logger


if (_sys.version_info[0] >= 3) and (_sys.version_info[1] >= 5):
    # Set docstring for properties.
    BlockPartition.num_locales.__doc__ = \
        CartLocaleComms.num_locales.__doc__
    BlockPartition.intra_locale_comm.__doc__ = CartLocaleComms.intra_locale_comm.__doc__
    BlockPartition.cart_comm.__doc__ = CartLocaleComms.cart_comm.__doc__
    BlockPartition.have_valid_cart_comm.__doc__ = \
        CartLocaleComms.have_valid_cart_comm.__doc__
    BlockPartition.rank_comm.__doc__ = CartLocaleComms.rank_comm.__doc__

DT_BLOCK = "block"
DT_SLAB = "slab"
_valid_distrib_types = [DT_BLOCK, DT_SLAB]

LT_NODE = "node"
LT_PROCESS = "process"
_valid_locale_types = [LT_NODE, LT_PROCESS]

CommsAndDistribution = \
    _collections.namedtuple("CommsAndDistribution", ["locale_comms", "distribution", "this_locale"])


def create_block_distribution(
    shape,
    locale_type=None,
    dims=None,
    halo=0,
    rank_comm=None,
    intra_locale_comm=None,
    inter_locale_comm=None,
    cart_comm=None
):
    """
    Factory function for creating :obj:`BlockPartition` distribution instance.

    :rtype: :obj:`CommsAndDistribution`
    :return: A :obj:`CommsAndDistribution` pair.
    """
    if dims is None:
        dims = _np.zeros_like(shape, dtype="int64")
    if locale_type.lower() == LT_PROCESS:
        if (intra_locale_comm is not None) and (intra_locale_comm.size > 1):
            raise ValueError(
                "Got locale_type=%s, but intra_locale_comm.size=%s"
                %
                (locale_type, intra_locale_comm.size)
            )
        intra_locale_comm = _mpi.COMM_SELF
    cart_locale_comms = \
        CartLocaleComms(
            dims=dims,
            rank_comm=rank_comm,
            intra_locale_comm=intra_locale_comm,
            inter_locale_comm=inter_locale_comm,
            cart_comm=cart_comm
        )
    cart_coord_to_cart_rank = cart_locale_comms.cart_coord_to_cart_rank_map
    cart_rank_to_rank = cart_locale_comms.inter_locale_rank_to_rank_map
    this_locale = cart_locale_comms.this_locale_rank_info

    # Broadcast on intra_locale_comm to get rank mapping to all
    # rank_comm ranks
    cart_coord_to_cart_rank, cart_rank_to_rank, this_locale = \
        cart_locale_comms.intra_locale_comm.bcast(
            (cart_coord_to_cart_rank, cart_rank_to_rank, this_locale),
            0
        )

    block_distrib = \
        BlockPartition(
            globale_extent=shape,
            dims=cart_locale_comms.dims,
            cart_coord_to_cart_rank=cart_coord_to_cart_rank,
            inter_locale_rank_to_rank=cart_rank_to_rank,
            halo=halo
        )
    return CommsAndDistribution(cart_locale_comms, block_distrib, this_locale)


def check_distrib_type(distrib_type):
    """
    Checks :samp:`{distrib_type}` occurs in :samp:`_valid_distrib_types`.
    """
    if distrib_type.lower() not in _valid_distrib_types:
        raise ValueError(
            "Invalid distrib_type=%s, valid types are: %s."
            %
            (
                distrib_type,
                ", ".join(_valid_distrib_types)
            )
        )


def check_locale_type(locale_type):
    """
    Checks :samp:`{locale_type}` occurs in :samp:`_valid_locale_types`.
    """
    if locale_type.lower() not in _valid_locale_types:
        raise ValueError(
            "Invalid locale_type=%s, valid types are: %s."
            %
            (
                locale_type,
                ", ".join(_valid_locale_types)
            )
        )


def create_distribution(shape, distrib_type=DT_BLOCK, locale_type=LT_NODE, **kwargs):
    """
    Factory function for creating :obj:`Distribution` instance.

    :rtype: :obj:`CommsAndDistribution`
    :return: A :obj:`CommsAndDistribution` pair.
    """
    check_distrib_type(distrib_type)
    check_locale_type(locale_type)
    if distrib_type.lower() == DT_BLOCK:
        comms_and_distrib = create_block_distribution(shape, locale_type, **kwargs)
    elif distrib_type.lower() == DT_SLAB:
        if "axis" in kwargs.keys():
            axis = kwargs["axis"]
            del kwargs["axis"]
        else:
            axis = 0
        dims = _np.ones_like(shape, dtype="int64")
        dims[axis] = 0
        comms_and_distrib = create_block_distribution(shape, locale_type, dims=dims, **kwargs)

    return comms_and_distrib


__all__ = [s for s in dir() if not s.startswith('_')]