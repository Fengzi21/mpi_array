"""
=========================================
The :mod:`mpi_array.decomposition` Module
=========================================

Sub-division of arrays over nodes and/or MPI processes.

Classes and Functions
=====================

.. autosummary::
   :toctree: generated/

   IndexingExtent - Index range for a tile of a decomposition.
   HaloIndexingExtent - Index range, with ghost elements, for a tile of a decomposition.
   DecompExtent - Indexing and halo info for a tile in a cartesian decomposition.
   SharedMemInfo - Shared-memory communicator generation.
   MemAllocTopology - Topology of MPI processes which allocate shared memory.
   Decomposition - Partition of an array *shape* overs MPI processes and/or nodes.


"""
from __future__ import absolute_import
from .license import license as _license, copyright as _copyright
import pkg_resources as _pkg_resources
import sys as _sys
import mpi4py.MPI as _mpi
import array_split as _array_split
import array_split.split  # noqa: F401
from array_split import ARRAY_BOUNDS
from array_split.split import convert_halo_to_array_form
import numpy as _np

__author__ = "Shane J. Latham"
__license__ = _license()
__copyright__ = _copyright()
__version__ = _pkg_resources.resource_string("mpi_array", "version.txt").decode()


class SharedMemInfo(object):
    """
    Info on possible shared memory allocation for a specified MPI communicator.
    """

    def __init__(self, comm=None, shared_mem_comm=None):
        """
        Construct.

        :type comm: :obj:`mpi4py.MPI.Comm`
        :param comm: Communicator used to split according to
           shared memory allocation (uses :meth:`mpi4py.MPI.Comm.Split_type`).
        :type shared_mem_comm: :obj:`mpi4py.MPI.Comm`
        :param shared_mem_comm: Shared memory communicator, can explicitly
           specify (should be a subset of processes returned
           by :samp:`{comm}.Split_type(_mpi.COMM_TYPE_SHARED)`.
           If :samp:`None`, :samp:`{comm}` is *split* into groups
           which can use a MPI window to allocate shared memory.
        """
        if comm is None:
            comm = _mpi.COMM_WORLD
        if shared_mem_comm is None:
            if _mpi.VERSION >= 3:
                shared_mem_comm = comm.Split_type(_mpi.COMM_TYPE_SHARED, key=comm.rank)
            else:
                shared_mem_comm = comm.Split(comm.rank, key=comm.rank)

        self._shared_mem_comm = shared_mem_comm

        # Count the number of self._shared_mem_comm rank-0 processes
        # to work out how many communicators comm was split into.
        is_rank_zero = 0
        if self._shared_mem_comm.rank == 0:
            is_rank_zero = 1
        self._num_shared_mem_nodes = comm.allreduce(is_rank_zero, _mpi.SUM)

    @property
    def num_shared_mem_nodes(self):
        """
        An integer indicating the number of *memory nodes* over which an array is distributed.
        """
        return self._num_shared_mem_nodes

    @property
    def shared_mem_comm(self):
        """
        A :obj:`mpi4py.MPI.Comm` object which defines the group of processes
        which can allocate (and access) MPI window shared memory
        (via  :meth:`mpi4py.MPI.Win.Allocate_shared`).
        """
        return self._shared_mem_comm


class MemAllocTopology(object):
    """
    Defines cartesian memory allocation (and communication) topology for MPI processes.
    """

    def __init__(
        self,
        ndims=None,
        dims=None,
        rank_comm=None,
        shared_mem_comm=None
    ):
        """
        Initialises cartesian communicator for memory-allocation-nodes.
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
        :type shared_mem_comm: :obj:`mpi4py.MPI.Comm`
        :param shared_mem_comm: The MPI communicator used to create a window which
            can be used to allocate shared memory
            via :meth:`mpi4py.MPI.Win.Allocate_shared`.
        """
        # No implementation for periodic boundaries yet
        periods = None
        if (ndims is None) and (dims is None):
            raise ValueError("Must specify one of dims or ndims in MemAllocTopology constructor.")
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

        self._rank_comm = rank_comm
        self._shared_mem_info = SharedMemInfo(self.rank_comm, shared_mem_comm)
        self._cart_comm = None

        self._dims = \
            _array_split.split.calculate_num_slices_per_axis(
                dims,
                self.num_shared_mem_nodes
            )

        # Create a cartesian grid communicator
        if self.num_shared_mem_nodes > 1:
            color = _mpi.UNDEFINED
            if self.shared_mem_comm.rank == 0:
                color = 0
            splt_comm = self.rank_comm.Split(color, self.rank_comm.rank)
            if splt_comm != _mpi.COMM_NULL:
                self._cart_comm = splt_comm.Create_cart(self.dims, periods, reorder=True)
            else:
                self._cart_comm = _mpi.COMM_NULL

    @property
    def dims(self):
        """
        The number of partitions along each array axis. Defines
        the cartesian topology over which an array is distributed.
        """
        return self._dims

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
        The group of all MPI processes which have access to array elements.
        """
        return self._rank_comm

    @property
    def cart_comm(self):
        """
        The group of MPI processes (typically one process per memory node)
        which communicate to exchange array data (halo data say) between memory nodes.
        """
        return self._cart_comm

    @property
    def num_shared_mem_nodes(self):
        """
        See :attr:`SharedMemInfo.num_shared_mem_nodes`.
        """
        return self._shared_mem_info.num_shared_mem_nodes

    @property
    def shared_mem_comm(self):
        """
        See :attr:`SharedMemInfo.shared_mem_comm`.
        """
        return self._shared_mem_info.shared_mem_comm


if (_sys.version_info[0] >= 3) and (_sys.version_info[1] >= 5):
    # Set docstring for properties.
    MemAllocTopology.num_shared_mem_nodes.__doc__ = SharedMemInfo.num_shared_mem_nodes.__doc__
    MemAllocTopology.shared_mem_comm.__doc__ = SharedMemInfo.shared_mem_comm.__doc__


class IndexingExtent(object):
    """
    Indexing bounds for a single tile of domain decomposition.
    """

    def __init__(self, split=None, start=None, stop=None):
        """
        Construct.

        :type split: sequence of :obj:`slice`
        :param split: Per axis start and stop indices defining the extent.
        :type start: sequence of :obj:`int`
        :param start: Per axis *start* indices defining the start of extent.
        :type stop: sequence of :obj:`int`
        :param stop: Per axis *stop* indices defining the extent.

        """
        object.__init__(self)
        if split is not None:
            self._beg = _np.array([s.start for s in split], dtype="int64")
            self._end = _np.array([s.stop for s in split], dtype=self._beg.dtype)
        elif (start is not None) and (stop is not None):
            self._beg = _np.array(start, dtype="int64")
            self._end = _np.array(stop, dtype="int64")

    def __eq__(self, other):
        """
        Return :samp:`True` for identical :attr:`start` and :attr:`stop`.
        """
        return _np.all(self._beg == other._beg) and _np.all(self._end == other._end)

    @property
    def start(self):
        """
        Sequence of :obj:`int` indicating the per-axis start indices of this extent
        (including halo).
        """
        return self._beg

    @property
    def stop(self):
        """
        Sequence of :obj:`int` indicating the per-axis stop indices of this extent
        (including halo).
        """
        return self._end

    @property
    def shape(self):
        """
        Sequence of :obj:`int` indicating the shape of this extent
        (including halo).
        """
        return self._end - self._beg

    @property
    def ndim(self):
        """
        Dimension of indexing.
        """
        return len(self._beg)

    def calc_intersection(self, other):
        """
        Returns the indexing extent which is the intersection of
        this extent with the :samp:`{other}` extent.

        :type other: :obj:`IndexingExtent`
        :param other: Perform intersection calculation using this extent.
        :rtype: :obj:`IndexingExtent`
        :return: :samp:`None` if the extents do not intersect, otherwise
           returns the extent of overlapping indices.
        """
        intersection_extent = \
            IndexingExtent(
                start=_np.maximum(self._beg, other._beg),
                stop=_np.minimum(self._end, other._end)
            )
        if _np.any(intersection_extent._beg >= intersection_extent._end):
            intersection_extent = None

        return intersection_extent

    def __repr__(self):
        """
        Stringize.
        """
        return "IndexingExtent(start=%s, stop=%s)" % (tuple(self._beg), tuple(self._end))

    def __str__(self):
        """
        """
        return self.__repr__()


class HaloIndexingExtent(IndexingExtent):
    """
    Indexing bounds with ghost (halo) elements, for a single tile of domain decomposition.
    """

    #: The "low index" indices.
    LO = 0

    #: The "high index" indices.
    HI = 1

    def __init__(self, split, halo=None):
        """
        Construct.

        :type split: sequence of :obj:`slice`
        :param split: Per axis start and stop indices defining the extent (**not including ghost
           elements**).
        :type halo: :samp:`(len({split}), 2)` shaped array of :obj:`int`
        :param halo: A :samp:`(len(self.start), 2)` shaped array of :obj:`int` indicating the
           per-axis number of outer ghost elements. :samp:`halo[:,0]` is the number
           of elements on the low-index *side* and :samp:`halo[:,1]` is the number of
           elements on the high-index *side*.

        """
        IndexingExtent.__init__(self, split)
        if halo is None:
            halo = _np.zeros((self._beg.shape[0], 2), dtype=self._beg.dtype)
        self._halo = halo

    @property
    def halo(self):
        """
        A :samp:`(len(self.start), 2)` shaped array of :obj:`int` indicating the
        per-axis number of outer ghost elements. :samp:`halo[:,0]` is the number
        of elements on the low-index *side* and :samp:`halo[:,1]` is the number of
        elements on the high-index *side*.
        """
        return self._halo

    @property
    def start_h(self):
        """
        The start index of the tile with "halo" elements.
        """
        return self._beg - self._halo[:, self.LO]

    @property
    def start_n(self):
        """
        The start index of the tile without "halo" elements ("no halo").
        """
        return self._beg

    @property
    def stop_h(self):
        """
        The stop index of the tile with "halo" elements.
        """
        return self._end + self._halo[:, self.HI]

    @property
    def stop_n(self):
        """
        The stop index of the tile without "halo" elements ("no halo").
        """
        return self._end

    @property
    def shape_h(self):
        """
        The shape of the tile with "halo" elements.
        """
        return self._end + self._halo[:, self.HI] - self._beg + self._halo[:, self.LO]

    @property
    def shape_n(self):
        """
        The shape of the tile without "halo" elements ("no halo").
        """
        return self._end - self._beg

    @property
    def start(self):
        """
        Same as :attr:`start_n`.
        """
        return self.start_n

    @property
    def stop(self):
        """
        Same as :attr:`stop_n`.
        """
        return self.stop_n

    @property
    def shape(self):
        """
        Same as :attr:`shape_n`.
        """
        return self.shape_n

    def __repr__(self):
        """
        Stringize.
        """
        return \
            (
                "HaloIndexingExtent(start=%s, stop=%s, halo=%s)"
                %
                (tuple(self._beg), tuple(self._end), tuple(self._halo))
            )

    def __str__(self):
        """
        """
        return self.__repr__()


class DecompExtent(HaloIndexingExtent):
    """
    Indexing extents for single tile of cartesian domain decomposition.
    """

    def __init__(
        self,
        cart_rank,
        cart_coord,
        cart_shape,
        array_shape,
        slice,
        halo,
        bounds_policy=ARRAY_BOUNDS
    ):
        """
        Construct.

        :type cart_rank: :obj:`int`
        :param cart_rank: Rank of MPI process in cartesian communicator.
        :type cart_coord: sequence of :obj:`int`
        :param cart_coord: Coordinate index of this tile in the cartesian domain decomposition.
        :type cart_shape: sequence of :obj:`int`
        :param cart_shape: Number of tiles in each axis direction.
        :type slice: sequence of :obj:`slice`
        :param slice: Per-axis start and stop indices (**not including ghost elements**).
        :type halo: :samp:`(len({split}), 2)` shaped array of :obj:`int`
        :param halo: A :samp:`(len(self.start), 2)` shaped array of :obj:`int` indicating the
           per-axis number of outer ghost elements. :samp:`halo[:,0]` is the number
           of elements on the low-index *side* and :samp:`halo[:,1]` is the number of
           elements on the high-index *side*.
        """
        self._cart_rank = cart_rank
        self._cart_coord = _np.array(cart_coord, dtype="int64")
        self._cart_shape = _np.array(cart_shape, dtype=self._cart_coord.dtype)
        self._array_shape = _np.array(array_shape, dtype=self._cart_coord.dtype)
        HaloIndexingExtent.__init__(self, slice, halo=None)
        halo = convert_halo_to_array_form(halo, ndim=len(self._cart_coord))
        if (bounds_policy == ARRAY_BOUNDS):
            # Make the halo
            halo = \
                _np.array(
                    (
                        _np.minimum(
                            self.start_n,
                            halo[:, self.LO]
                        ),
                        _np.minimum(
                            self._array_shape - self.stop_n,
                            halo[:, self.HI]
                        ),
                    ),
                    dtype=halo.dtype
                ).T
        self._halo = halo

    @property
    def cart_rank(self):
        """
        MPI rank of the process in the cartesian decomposition.
        """
        return self._cart_rank

    @property
    def cart_coord(self):
        """
        Cartesian coordinate of cartesian decomposition.
        """
        return self._cart_coord

    @property
    def cart_shape(self):
        """
        Shape of cartesian decomposition (number of tiles along each axis).
        """
        return self._cart_shape

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


class SingleExtentUpdate(object):
    """
    Source and destination indexing info for updating the whole of an extent.
    """

    def __init__(self, dst_extent, src_extent, update_extent):
        self._dst_extent = dst_extent  #: whole tile of destination rank
        self._src_extent = src_extent  #: whole tile of source rank
        self._update_extent = update_extent  #: portion from source required for update


class HalosUpdate(object):
    """
    Indexing info for updating the halo regions of a single tile
    on MPI rank :samp:`self.dst_rank`.
    """

    #: The "low index" indices.
    LO = HaloIndexingExtent.LO

    #: The "high index" indices.
    HI = HaloIndexingExtent.HI

    def __init__(self, dst_rank, rank_to_extents_dict):
        """
        Construct.

        :type dst_rank: :obj:`int`
        :param dst_rank: The MPI rank (:samp:`cart_comm`) of the MPI
           process which is to receive the halo updates.
        :type rank_to_extents_dict: :obj:`dict`
        :param rank_to_extents_dict: Dictionary of :samp:`(r, extent)`
           pairs for all ranks :samp:`r` (of :samp:`cart_comm`), where :samp:`extent`
           is a :obj:`DecompExtent` object indicating the indexing extent
           (tile) on MPI rank :samp:`r.`
        """
        self.initialise(dst_rank, rank_to_extents_dict)

    def calc_halo_intersection(self, dst_extent, src_extent, axis, dir):
        """
        Calculates the intersection of :samp:`{dst_extent}` halo slab with
        the update region of :samp:`{src_extent}`.

        :type dst_extent: :obj:`DecompExtent`
        :param dst_extent: Halo slab indicated by :samp:`{axis}` and :samp:`{dir}`
           taken from this extent.
        :type src_extent: :obj:`DecompExtent`
        :param src_extent: This extent, minus the halo in the :samp:`{axis}` dimension,
           is intersected with the halo slab.
        :type axis: :obj:`int`
        :param axis: Axis dimension indicating slab.
        :type dir: :attr:`LO` or :attr:`HI`
        :param dir: :attr:`LO` for low-index slab or :attr:`HI` for high-index slab.
        :rtype: :obj:`IndexingExtent`
        :return: Overlap extent of :samp:{dst_extent} halo-slab and
           the :samp:`{src_extent}` update region.
        """
        return \
            dst_extent.halo_slab_extent(axis, dir).calc_intersection(
                src_extent.no_halo_extent(axis)
            )

    def split_extent_for_max_elements(self, extent, max_elements=None):
        """
        Partitions the specified extent into smaller extents with number
        of elements no more than :samp:`{max_elements}`.

        :type extent: :obj:`DecompExtent`
        :param extent: The extent to be split.
        :type max_elements: :obj:`int`
        :param max_elements: Each partition of the returned split has no more
           than this many elements.
        :rtype: :obj:`list` of :obj:`DecompExtent`
        :return: List of extents forming a partition of :samp:`{extent}`
           with each extent having no more than :samp:`{max_element}` elements.
        """
        return [extent, ]

    def initialise(self, dst_rank, rank_to_extents_dict):
        """
        Calculates the ranks and regions required to update the
        halo regions of the :samp:`dst_rank` MPI rank.

        :type dst_rank: :obj:`int`
        :param dst_rank: The MPI rank (:samp:`cart_comm`) of the MPI
           process which is to receive the halo updates.
        :type rank_to_extents_dict: :obj:`dict`
        :param rank_to_extents_dict: Dictionary of :samp:`(r, extent)`
           pairs for all ranks :samp:`r` (of :samp:`cart_comm`), where :samp:`extent`
           is a :obj:`DecompExtent` object indicating the indexing extent
           (tile) on MPI rank :samp:`r.`
        """
        self._dst_rank = dst_rank
        self._dst_extent = rank_to_extents_dict[dst_rank]
        self._updates = [[[], []]] * self._dst_extent.ndim
        cart_coord_to_extents_dict = \
            {
                tuple(rank_to_extents_dict[r].cart_coord): rank_to_extents_dict[r]
                for r in rank_to_extents_dict.keys()
            }
        for dir in [self.LO, self.HI]:
            for a in range(self._dst_extent.ndim):
                if dir == self.LO:
                    i_range = range(-1, -self._dst_extent.cart_coord[a] - 1, -1)
                else:
                    i_range = \
                        range(1, self._dst_extent.cart_shape[a] - self._dst_extent.cart_coord[a], 1)
                for i in i_range:
                    src_cart_coord = _np.array(self._dst_extent.cart_coord, copy=True)
                    src_cart_coord[a] += i
                    src_extent = cart_coord_to_extents_dict[tuple(src_cart_coord)]
                    halo_extent = self.calc_halo_intersection(self._dst_extent, src_extent, a, dir)
                    if halo_extent is not None:
                        self._updates[a][dir] += \
                            self.split_extent_for_max_elements(
                                SingleExtentUpdate(self._dst_extent, src_extent, halo_extent)
                        )
                    else:
                        break


class Decomposition(object):
    """
    Partitions an array-shape over MPI memory-nodes.
    """

    def __init__(
        self,
        shape,
        halo=0,
        mem_node_topology=None,
    ):
        """
        Create a partitioning of :samp:`{shape}` over memory-nodes.

        :type shape: sequence of :obj:`int`
        :param shape: The shape of the array which is to be partitioned into smaller *sub-shapes*.
        :type halo: :obj:`int`, sequence of :obj:`int` or :samp:`(len({shape}), 2)` shaped array.
        :param halo: Number of *ghost* elements added per axis
           (low and high indices can be different).
        :type mem_node_topology: :obj:`MemAllocTopology`
        :param mem_node_topology: Object which defines how array
           memory is allocated (distributed) over memory nodes and
           the cartesian topology communicator used to exchange (halo)
           data. If :samp:`None` uses :samp:`MemAllocTopology(dims=numpy.zeros_like({shape}))`.
        """
        self._halo = halo
        self._shape = None
        self._mem_node_topology = mem_node_topology
        self._shape_decomp = None

        self.recalculate(shape, halo)

    def recalculate(self, new_shape, new_halo):
        """
        Recomputes decomposition for :samp:`{new_shape}` and :samp:`{new_halo}`.

        :type new_shape: sequence of :obj:`int`
        :param new_shape: New partition calculated for this shape.
        :type new_halo: :obj:`int`, sequence of :obj:`int` or :samp:`(len{new_shape, 2))` array.
        :param new_halo: New partition calculated for this shape.
        """
        if self._mem_node_topology is None:
            self._mem_node_topology = MemAllocTopology(ndims=len(new_shape))
        elif (self._shape is not None) and (len(self._shape) != len(new_shape)):
            self._shape = _np.array(new_shape)
            self._mem_node_topology = MemAllocTopology(ndims=self._shape.size)
        self._shape = _np.array(new_shape)
        self._halo = new_halo

        shape_splitter = \
            _array_split.ShapeSplitter(
                array_shape=self._shape,
                axis=self._mem_node_topology.dims,
                halo=0
            )

        self._halo = convert_halo_to_array_form(halo=self._halo, ndim=len(self._shape))

        self._shape_decomp = shape_splitter.calculate_split()

        self._cart_rank_to_extents_dict = None
        self._halo_updates_dict = None
        if self.have_valid_cart_comm:
            cart_dims = _np.array(self.cart_comm.dims)
            self._cart_rank_to_extents_dict = dict()
            self._halo_updates_dict = dict()
            for cart_rank in range(0, self.cart_comm.size):
                cart_coords = _np.array(self.cart_comm.Get_coords(cart_rank))
                self._cart_rank_to_extents_dict[cart_rank] = \
                    DecompExtent(
                        cart_rank=cart_rank,
                        cart_coord=cart_coords,
                        cart_shape=cart_dims,
                        array_shape=self._shape,
                        slice=self._shape_decomp[tuple(cart_coords)],
                        halo=self._halo,
                        bounds_policy=shape_splitter.tile_bounds_policy
                    )  # noqa: E123
            for cart_rank in range(0, self.cart_comm.size):
                self._halo_updates_dict[cart_rank] = \
                    HalosUpdate(cart_rank, self._cart_rank_to_extents_dict)

    def __str__(self):
        """
        """
        s = []
        if self.have_valid_cart_comm:
            for cart_rank in range(0, self.cart_comm.size):
                s += \
                    [
                        "{cart_rank = %s, cart_coord = %s, extents=%s}"
                        %
                        (
                            cart_rank,
                            self.cart_comm.Get_coords(cart_rank),
                            self._cart_rank_to_extents_dict[cart_rank],
                        )
                    ]
        return ", ".join(s)

    @property
    def halo(self):
        """
        Number of *ghost* elements per axis to pad array shape.
        """
        return self._halo

    @halo.setter
    def halo(self, halo):
        if halo is None:
            halo = 0

        self.recalculate(self._shape, halo)

    @property
    def shape(self):
        """
        The shape of the array to be distributed over MPI memory nodes.
        """
        return self._shape

    @shape.setter
    def shape(self, new_shape):
        self.recalculate(new_shape, self._halo)

    @property
    def shape_decomp(self):
        """
        The partition of :samp:`self.shape` over memory nodes.
        """
        return self._shape_decomp

    @property
    def num_shared_mem_nodes(self):
        """
        See :attr:`MemAllocTopology.num_shared_mem_nodes`.
        """
        return self._mem_node_topology.num_shared_mem_nodes

    @property
    def shared_mem_comm(self):
        """
        See :attr:`MemAllocTopology.shared_mem_comm`.
        """
        return self._mem_node_topology.shared_mem_comm

    @property
    def cart_comm(self):
        """
        See :attr:`MemAllocTopology.cart_comm`.
        """
        return self._mem_node_topology.cart_comm

    @property
    def have_valid_cart_comm(self):
        """
        See :attr:`MemAllocTopology.have_valid_cart_comm`.
        """
        return self._mem_node_topology.have_valid_cart_comm

    @property
    def rank_comm(self):
        """
        See :attr:`MemAllocTopology.rank_comm`.
        """
        return self._mem_node_topology.rank_comm


if (_sys.version_info[0] >= 3) and (_sys.version_info[1] >= 5):
    # Set docstring for properties.
    Decomposition.num_shared_mem_nodes.__doc__ = MemAllocTopology.num_shared_mem_nodes.__doc__
    Decomposition.shared_mem_comm.__doc__ = MemAllocTopology.shared_mem_comm.__doc__
    Decomposition.cart_comm.__doc__ = MemAllocTopology.cart_comm.__doc__
    Decomposition.have_valid_cart_comm.__doc__ = MemAllocTopology.have_valid_cart_comm.__doc__
    Decomposition.rank_comm.__doc__ = MemAllocTopology.rank_comm.__doc__

__all__ = [s for s in dir() if not s.startswith('_')]
