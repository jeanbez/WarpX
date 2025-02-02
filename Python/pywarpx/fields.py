# Copyright 2017-2019 David Grote
#
# This file is part of WarpX.
#
# License: BSD-3-Clause-LBNL

"""Provides wrappers around field and current density on multiFABs

Available routines:

ExWrapper, EyWrapper, EzWrapper
BxWrapper, ByWrapper, BzWrapper
JxWrapper, JyWrapper, JzWrapper

"""
import numpy as np
try:
    from mpi4py import MPI as mpi
    comm_world = mpi.COMM_WORLD
    npes = comm_world.Get_size()
except ImportError:
    npes = 1

from . import _libwarpx


class _MultiFABWrapper(object):
    """Wrapper around field arrays at level 0
    This provides a convenient way to query and set fields that are broken up into FABs.
    The indexing is based on global indices.
     - direction: component to access, one of the values (0, 1, 2) or None
     - get_lovects: routine that returns the list of lo vectors
     - get_fabs: routine that returns the list of FABs
     - get_nodal_flag: routine that returns the list of nodal flag
     - level: refinement level
    """
    def __init__(self, direction, get_lovects, get_fabs, get_nodal_flag, level, include_ghosts=False):
        self.direction = direction
        self.get_lovects = get_lovects
        self.get_fabs = get_fabs
        self.get_nodal_flag = get_nodal_flag
        self.level = level
        self.include_ghosts = include_ghosts

        self.dim = _libwarpx.dim

        # overlaps is one along the axes where the grid boundaries overlap the neighboring grid,
        # which is the case with node centering.
        # This presumably will never change during a calculation.
        self.overlaps = self.get_nodal_flag()

    def _getlovects(self):
        if self.direction is None:
            lovects, ngrow = self.get_lovects(self.level, self.include_ghosts)
        else:
            lovects, ngrow = self.get_lovects(self.level, self.direction, self.include_ghosts)
        return lovects, ngrow

    def _gethivects(self):
        lovects, ngrow = self._getlovects()
        fields = self._getfields()

        hivects = np.zeros_like(lovects)
        for i in range(len(fields)):
            hivects[:,i] = lovects[:,i] + np.array(fields[i].shape[:self.dim]) - self.overlaps

        return hivects, ngrow

    def _getfields(self):
        if self.direction is None:
            return self.get_fabs(self.level, self.include_ghosts)
        else:
            return self.get_fabs(self.level, self.direction, self.include_ghosts)

    def __len__(self):
        lovects, ngrow = self._getlovects()
        return len(lovects)

    def mesh(self, direction):
        """Returns the mesh along the specified direction with the appropriate centering.
        - direction: In 3d, one of 'x', 'y', or 'z'.
                     In 2d, Cartesian, one of 'x', or 'z'.
                     In RZ, one of 'r', or 'z'
        """

        try:
            if _libwarpx.geometry_dim == '3d':
                idir = ['x', 'y', 'z'].index(direction)
                celldir = idir
            elif _libwarpx.geometry_dim == '2d':
                idir = ['x', 'z'].index(direction)
                celldir = 2*idir
            elif _libwarpx.geometry_dim == 'rz':
                idir = ['r', 'z'].index(direction)
                celldir = 2*idir
        except ValueError:
            raise Exception('Inappropriate direction given')

        # --- Get the total number of cells along the direction
        hivects, ngrow = self._gethivects()
        nn = hivects[idir,:].max() - ngrow[idir] + self.overlaps[idir]
        if npes > 1:
            nn = comm_world.allreduce(nn, op=mpi.MAX)

        # --- Cell size in the direction
        dd = _libwarpx.getCellSize(celldir, self.level)

        # --- Get the nodal flag along direction
        nodal_flag = self.get_nodal_flag()[idir]

        # --- The centering shift
        if nodal_flag == 1:
            # node centered
            shift = 0.
        else:
            # cell centered
            shift = 0.5*dd

        return np.arange(nn)*dd + shift

    def __getitem__(self, index):
        """Returns slices of a decomposed array, The shape of
        the object returned depends on the number of ix, iy and iz specified, which
        can be from none to all three. Note that the values of ix, iy and iz are
        relative to the fortran indexing, meaning that 0 is the lower boundary
        of the whole domain.
        """
        if index == Ellipsis:
            index = tuple(self.dim*[slice(None)])

        if len(index) < self.dim:
            # --- Add extra dims to index if needed
            index = list(index)
            for i in range(len(index), self.dim):
                index.append(slice(None))
            index = tuple(index)

        if self.dim == 2:
            return self._getitem2d(index)
        elif self.dim == 3:
            return self._getitem3d(index)

    def _find_start_stop(self, ii, imin, imax, d):
        """Given the input index, calculate the start and stop range of the indices.
        - ii: input index, either a slice object or an integer
        - imin: the global lowest lovect value in the specified diretion
        - imax: the global highest hivect value in the specified diretion
        - d: the direction, an integer, 0, 1, or 2
        If ii is a slice, the start and stop values are used directly,
        unless they are None, then the lower or upper bound is used.
        An assertion checks if the indices are within the bounds.
        """
        if isinstance(ii, slice):
            if ii.start is None:
                iistart = imin
            else:
                iistart = ii.start
            if ii.stop is None:
                iistop = imax + self.overlaps[d]
            else:
                iistop = ii.stop
        else:
            iistart = ii
            iistop = ii + 1
        assert imin <= iistart <= imax + self.overlaps[d], Exception(f'Dimension {d} lower index is out of bounds')
        assert imin <= iistop <= imax + self.overlaps[d], Exception(f'Dimension {d} upper index is out of bounds')
        return iistart, iistop

    def _getitem3d(self, index):
        """Returns slices of a 3D decomposed array,
        """

        lovects, ngrow = self._getlovects()
        hivects, ngrow = self._gethivects()
        fields = self._getfields()

        ix = index[0]
        iy = index[1]
        iz = index[2]

        if len(fields[0].shape) > self.dim:
            ncomps = fields[0].shape[-1]
        else:
            ncomps = 1

        if len(index) > self.dim:
            if ncomps > 1:
                ic = index[-1]
            else:
                raise Exception('Too many indices given')
        else:
            ic = None

        ixmin = lovects[0,:].min()
        ixmax = hivects[0,:].max()
        iymin = lovects[1,:].min()
        iymax = hivects[1,:].max()
        izmin = lovects[2,:].min()
        izmax = hivects[2,:].max()

        if npes > 1:
            ixmin = comm_world.allreduce(ixmin, op=mpi.MIN)
            ixmax = comm_world.allreduce(ixmax, op=mpi.MAX)
            iymin = comm_world.allreduce(iymin, op=mpi.MIN)
            iymax = comm_world.allreduce(iymax, op=mpi.MAX)
            izmin = comm_world.allreduce(izmin, op=mpi.MIN)
            izmax = comm_world.allreduce(izmax, op=mpi.MAX)

        ixstart, ixstop = self._find_start_stop(ix, ixmin, ixmax, 0)
        iystart, iystop = self._find_start_stop(iy, iymin, iymax, 1)
        izstart, izstop = self._find_start_stop(iz, izmin, izmax, 2)

        # --- Setup the size of the array to be returned and create it.
        # --- Space is added for multiple components if needed.
        sss = (max(0, ixstop - ixstart),
               max(0, iystop - iystart),
               max(0, izstop - izstart))
        if ncomps > 1 and ic is None:
            sss = tuple(list(sss) + [ncomps])
        resultglobal = np.zeros(sss, dtype=_libwarpx._numpy_real_dtype)

        datalist = []
        for i in range(len(fields)):

            # --- The ix1, 2 etc are relative to global indexing
            ix1 = max(ixstart, lovects[0,i])
            ix2 = min(ixstop, lovects[0,i] + fields[i].shape[0])
            iy1 = max(iystart, lovects[1,i])
            iy2 = min(iystop, lovects[1,i] + fields[i].shape[1])
            iz1 = max(izstart, lovects[2,i])
            iz2 = min(izstop, lovects[2,i] + fields[i].shape[2])

            if ix1 < ix2 and iy1 < iy2 and iz1 < iz2:

                sss = (slice(ix1 - lovects[0,i], ix2 - lovects[0,i]),
                       slice(iy1 - lovects[1,i], iy2 - lovects[1,i]),
                       slice(iz1 - lovects[2,i], iz2 - lovects[2,i]))
                if ic is not None:
                    sss = tuple(list(sss) + [ic])

                vslice = (slice(ix1 - ixstart, ix2 - ixstart),
                          slice(iy1 - iystart, iy2 - iystart),
                          slice(iz1 - izstart, iz2 - izstart))

                datalist.append((vslice, fields[i][sss]))

        if npes == 1:
            all_datalist = [datalist]
        else:
            all_datalist = comm_world.allgather(datalist)

        for datalist in all_datalist:
            for vslice, ff in datalist:
                resultglobal[vslice] = ff

        # --- Now remove any of the reduced dimensions.
        sss = [slice(None), slice(None), slice(None)]
        if not isinstance(ix, slice):
            sss[0] = 0
        if not isinstance(iy, slice):
            sss[1] = 0
        if not isinstance(iz, slice):
            sss[2] = 0

        return resultglobal[tuple(sss)]

    def _getitem2d(self, index):
        """Returns slices of a 2D decomposed array,
        """

        lovects, ngrow = self._getlovects()
        hivects, ngrow = self._gethivects()
        fields = self._getfields()

        ix = index[0]
        iz = index[1]

        if len(fields[0].shape) > self.dim:
            ncomps = fields[0].shape[-1]
        else:
            ncomps = 1

        if len(index) > self.dim:
            if ncomps > 1:
                ic = index[2]
            else:
                raise Exception('Too many indices given')
        else:
            ic = None

        ixmin = lovects[0,:].min()
        ixmax = hivects[0,:].max()
        izmin = lovects[1,:].min()
        izmax = hivects[1,:].max()

        if npes > 1:
            ixmin = comm_world.allreduce(ixmin, op=mpi.MIN)
            ixmax = comm_world.allreduce(ixmax, op=mpi.MAX)
            izmin = comm_world.allreduce(izmin, op=mpi.MIN)
            izmax = comm_world.allreduce(izmax, op=mpi.MAX)

        ixstart, ixstop = self._find_start_stop(ix, ixmin, ixmax, 0)
        izstart, izstop = self._find_start_stop(iz, izmin, izmax, 1)

        # --- Setup the size of the array to be returned and create it.
        # --- Space is added for multiple components if needed.
        sss = (max(0, ixstop - ixstart),
               max(0, izstop - izstart))
        if ncomps > 1 and ic is None:
            sss = tuple(list(sss) + [ncomps])
        resultglobal = np.zeros(sss, dtype=_libwarpx._numpy_real_dtype)

        datalist = []
        for i in range(len(fields)):

            # --- The ix1, 2 etc are relative to global indexing
            ix1 = max(ixstart, lovects[0,i])
            ix2 = min(ixstop, lovects[0,i] + fields[i].shape[0])
            iz1 = max(izstart, lovects[1,i])
            iz2 = min(izstop, lovects[1,i] + fields[i].shape[1])

            if ix1 < ix2 and iz1 < iz2:

                sss = (slice(ix1 - lovects[0,i], ix2 - lovects[0,i]),
                       slice(iz1 - lovects[1,i], iz2 - lovects[1,i]))
                if ic is not None:
                    sss = tuple(list(sss) + [ic])

                vslice = (slice(ix1 - ixstart, ix2 - ixstart),
                          slice(iz1 - izstart, iz2 - izstart))

                datalist.append((vslice, fields[i][sss]))

        if npes == 1:
            all_datalist = [datalist]
        else:
            all_datalist = comm_world.allgather(datalist)

        for datalist in all_datalist:
            for vslice, ff in datalist:
                resultglobal[vslice] = ff

        # --- Now remove any of the reduced dimensions.
        sss = [slice(None), slice(None)]
        if not isinstance(ix, slice):
            sss[0] = 0
        if not isinstance(iz, slice):
            sss[1] = 0

        return resultglobal[tuple(sss)]

    def __setitem__(self, index, value):
        """Sets slices of a decomposed array. The shape of
      the input object depends on the number of arguments specified, which can
      be from none to all three.
        - value: input array (must be supplied)
        """
        if index == Ellipsis:
            index = tuple(self.dim*[slice(None)])

        if len(index) < self.dim:
            # --- Add extra dims to index if needed
            index = list(index)
            for i in range(len(index), self.dim):
                index.append(slice(None))
            index = tuple(index)

        if self.dim == 2:
            return self._setitem2d(index, value)
        elif self.dim == 3:
            return self._setitem3d(index, value)

    def _setitem3d(self, index, value):
        """Sets slices of a decomposed 3D array.
        """
        ix = index[0]
        iy = index[1]
        iz = index[2]

        lovects, ngrow = self._getlovects()
        hivects, ngrow = self._gethivects()
        fields = self._getfields()

        if len(index) > self.dim:
            if ncomps > 1:
                ic = index[-1]
            else:
                raise Exception('Too many indices given')
        else:
            ic = None

        ixmin = lovects[0,:].min()
        ixmax = hivects[0,:].max()
        iymin = lovects[1,:].min()
        iymax = hivects[1,:].max()
        izmin = lovects[2,:].min()
        izmax = hivects[2,:].max()

        if npes > 1:
            ixmin = comm_world.allreduce(ixmin, op=mpi.MIN)
            ixmax = comm_world.allreduce(ixmax, op=mpi.MAX)
            iymin = comm_world.allreduce(iymin, op=mpi.MIN)
            iymax = comm_world.allreduce(iymax, op=mpi.MAX)
            izmin = comm_world.allreduce(izmin, op=mpi.MIN)
            izmax = comm_world.allreduce(izmax, op=mpi.MAX)

        ixstart, ixstop = self._find_start_stop(ix, ixmin, ixmax, 0)
        iystart, iystop = self._find_start_stop(iy, iymin, iymax, 1)
        izstart, izstop = self._find_start_stop(iz, izmin, izmax, 2)

        # --- Add extra dimensions so that the input has the same number of
        # --- dimensions as array.
        if isinstance(value, np.ndarray):
            value3d = np.array(value, copy=False)
            sss = list(value3d.shape)
            if not isinstance(ix, slice): sss[0:0] = [1]
            if not isinstance(iy, slice): sss[1:1] = [1]
            if not isinstance(iz, slice): sss[2:2] = [1]
            value3d.shape = sss

        for i in range(len(fields)):

            # --- The ix1, 2 etc are relative to global indexing
            ix1 = max(ixstart, lovects[0,i])
            ix2 = min(ixstop, lovects[0,i] + fields[i].shape[0])
            iy1 = max(iystart, lovects[1,i])
            iy2 = min(iystop, lovects[1,i] + fields[i].shape[1])
            iz1 = max(izstart, lovects[2,i])
            iz2 = min(izstop, lovects[2,i] + fields[i].shape[2])

            if ix1 < ix2 and iy1 < iy2 and iz1 < iz2:

                sss = (slice(ix1 - lovects[0,i], ix2 - lovects[0,i]),
                       slice(iy1 - lovects[1,i], iy2 - lovects[1,i]),
                       slice(iz1 - lovects[2,i], iz2 - lovects[2,i]))
                if ic is not None:
                    sss = tuple(list(sss) + [ic])

                if isinstance(value, np.ndarray):
                    vslice = (slice(ix1 - ixstart, ix2 - ixstart),
                              slice(iy1 - iystart, iy2 - iystart),
                              slice(iz1 - izstart, iz2 - izstart))
                    fields[i][sss] = value3d[vslice]
                else:
                    fields[i][sss] = value

    def _setitem2d(self, index, value):
        """Sets slices of a decomposed 2D array.
        """
        ix = index[0]
        iz = index[1]

        lovects, ngrow = self._getlovects()
        hivects, ngrow = self._gethivects()
        fields = self._getfields()

        if len(fields[0].shape) > self.dim:
            ncomps = fields[0].shape[-1]
        else:
            ncomps = 1

        if len(index) > self.dim:
            if ncomps > 1:
                ic = index[2]
            else:
                raise Exception('Too many indices given')
        else:
            ic = None

        ixmin = lovects[0,:].min()
        ixmax = hivects[0,:].max()
        izmin = lovects[1,:].min()
        izmax = hivects[1,:].max()

        if npes > 1:
            ixmin = comm_world.allreduce(ixmin, op=mpi.MIN)
            ixmax = comm_world.allreduce(ixmax, op=mpi.MAX)
            izmin = comm_world.allreduce(izmin, op=mpi.MIN)
            izmax = comm_world.allreduce(izmax, op=mpi.MAX)

        ixstart, ixstop = self._find_start_stop(ix, ixmin, ixmax, 0)
        izstart, izstop = self._find_start_stop(iz, izmin, izmax, 1)

        # --- Add extra dimensions so that the input has the same number of
        # --- dimensions as array.
        if isinstance(value, np.ndarray):
            value3d = np.array(value, copy=False)
            sss = list(value3d.shape)
            if not isinstance(ix, slice): sss[0:0] = [1]
            if not isinstance(iz, slice): sss[1:1] = [1]
            value3d.shape = sss

        for i in range(len(fields)):

            # --- The ix1, 2 etc are relative to global indexing
            ix1 = max(ixstart, lovects[0,i])
            ix2 = min(ixstop, lovects[0,i] + fields[i].shape[0])
            iz1 = max(izstart, lovects[1,i])
            iz2 = min(izstop, lovects[1,i] + fields[i].shape[1])

            if ix1 < ix2 and iz1 < iz2:

                sss = (slice(ix1 - lovects[0,i], ix2 - lovects[0,i]),
                       slice(iz1 - lovects[1,i], iz2 - lovects[1,i]))
                if ic is not None:
                    sss = tuple(list(sss) + [ic])

                if isinstance(value, np.ndarray):
                    vslice = (slice(ix1 - ixstart, ix2 - ixstart),
                              slice(iz1 - izstart, iz2 - izstart))
                    fields[i][sss] = value3d[vslice]
                else:
                    fields[i][sss] = value


def ExWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_electric_field_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field,
                            get_nodal_flag=_libwarpx.get_Ex_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EyWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_electric_field_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field,
                            get_nodal_flag=_libwarpx.get_Ey_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EzWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_electric_field_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field,
                            get_nodal_flag=_libwarpx.get_Ez_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BxWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field,
                            get_nodal_flag=_libwarpx.get_Bx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ByWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field,
                            get_nodal_flag=_libwarpx.get_By_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BzWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field,
                            get_nodal_flag=_libwarpx.get_Bz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JxWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_current_density_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density,
                            get_nodal_flag=_libwarpx.get_Jx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JyWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_current_density_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density,
                            get_nodal_flag=_libwarpx.get_Jy_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JzWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_current_density_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density,
                            get_nodal_flag=_libwarpx.get_Jz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ExCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp,
                            get_nodal_flag=_libwarpx.get_Ex_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EyCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp,
                            get_nodal_flag=_libwarpx.get_Ey_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EzCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp,
                            get_nodal_flag=_libwarpx.get_Ez_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BxCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp,
                            get_nodal_flag=_libwarpx.get_Bx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ByCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp,
                            get_nodal_flag=_libwarpx.get_By_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BzCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp,
                            get_nodal_flag=_libwarpx.get_Bz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JxCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_cp,
                            get_nodal_flag=_libwarpx.get_Jx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JyCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_cp,
                            get_nodal_flag=_libwarpx.get_Jy_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JzCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_cp,
                            get_nodal_flag=_libwarpx.get_Jz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def RhoCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_charge_density_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_charge_density_cp,
                            get_nodal_flag=_libwarpx.get_Rho_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def FCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_F_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_F_cp,
                            get_nodal_flag=_libwarpx.get_F_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def GCPWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_G_cp_lovects,
                            get_fabs=_libwarpx.get_mesh_G_cp,
                            get_nodal_flag=_libwarpx.get_G_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ExFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp,
                            get_nodal_flag=_libwarpx.get_Ex_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EyFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp,
                            get_nodal_flag=_libwarpx.get_Ey_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EzFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp,
                            get_nodal_flag=_libwarpx.get_Ez_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BxFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp,
                            get_nodal_flag=_libwarpx.get_Bx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ByFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp,
                            get_nodal_flag=_libwarpx.get_By_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BzFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp,
                            get_nodal_flag=_libwarpx.get_Bz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JxFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_fp,
                            get_nodal_flag=_libwarpx.get_Jx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JyFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_fp,
                            get_nodal_flag=_libwarpx.get_Jy_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JzFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_current_density_fp,
                            get_nodal_flag=_libwarpx.get_Jz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def RhoFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_charge_density_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_charge_density_fp,
                            get_nodal_flag=_libwarpx.get_Rho_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def PhiFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_phi_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_phi_fp,
                            get_nodal_flag=_libwarpx.get_Phi_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def FFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_F_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_F_fp,
                            get_nodal_flag=_libwarpx.get_F_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def GFPWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_G_fp_lovects,
                            get_fabs=_libwarpx.get_mesh_G_fp,
                            get_nodal_flag=_libwarpx.get_G_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ExCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_Ex_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EyCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_Ey_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EzCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_electric_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_Ez_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BxCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_Bx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ByCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_By_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BzCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_cp_pml,
                            get_nodal_flag=_libwarpx.get_Bz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JxCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_cp_pml,
                            get_nodal_flag=_libwarpx.get_Jx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JyCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_cp_pml,
                            get_nodal_flag=_libwarpx.get_Jy_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JzCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_current_density_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_cp_pml,
                            get_nodal_flag=_libwarpx.get_Jz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def FCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_F_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_F_cp_pml,
                            get_nodal_flag=_libwarpx.get_F_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def GCPPMLWrapper(level=1, include_ghosts=False):
    assert level>0, Exception('Coarse patch only available on levels > 0')
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_G_cp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_G_cp_pml,
                            get_nodal_flag=_libwarpx.get_G_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ExFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_Ex_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EyFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_Ey_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def EzFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_electric_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_electric_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_Ez_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BxFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_Bx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def ByFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_By_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def BzFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_magnetic_field_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_magnetic_field_fp_pml,
                            get_nodal_flag=_libwarpx.get_Bz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JxFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=0,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_fp_pml,
                            get_nodal_flag=_libwarpx.get_Jx_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JyFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=1,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_fp_pml,
                            get_nodal_flag=_libwarpx.get_Jy_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def JzFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=2,
                            get_lovects=_libwarpx.get_mesh_current_density_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_current_density_fp_pml,
                            get_nodal_flag=_libwarpx.get_Jz_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def FFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_F_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_F_fp_pml,
                            get_nodal_flag=_libwarpx.get_F_pml_nodal_flag,
                            level=level, include_ghosts=include_ghosts)

def GFPPMLWrapper(level=0, include_ghosts=False):
    return _MultiFABWrapper(direction=None,
                            get_lovects=_libwarpx.get_mesh_G_fp_lovects_pml,
                            get_fabs=_libwarpx.get_mesh_G_fp_pml,
                            get_nodal_flag=_libwarpx.get_G_pml_nodal_flag,
                            level=level, include_ghosts=include_ghosts)
