import numpy as np
import ctypes
from pyscf import lib
from pyscf.pbc.tools.pyscf_ase import get_space_group
from pyscf import __config__
from pyscf.pbc.lib import symmetry as symm


KPT_DIFF_TOL = getattr(__config__, 'pbc_lib_kpts_helper_kpt_diff_tol', 1e-6)

libpbc = lib.load_library('libpbc')

def make_ibz_k(kpts, time_reversal=True, point_group = True):

    '''
    Constructe IBZ kpoints
    '''

    nbzkpts = len(kpts.bz_k)

    sg = kpts.space_group
    op_rot = sg.rotations

    op_trans = sg.translations
    op_rot = op_rot[np.where((op_trans==0.0).all(1))]
    if not point_group:
        op_rot = np.eye(3,dtype =int).reshape(1,3,3)
    kpts.op_rot = op_rot
    kpts.nrot = len(kpts.op_rot)
    if time_reversal:
        kpts.op_rot = np.concatenate([op_rot, -op_rot])

    bz2bz_ks = map_k_points_fast(kpts.bz_k_scaled, op_rot, time_reversal, KPT_DIFF_TOL)

    bz2bz_k = -np.ones(nbzkpts+1, dtype = int)
    ibz2bz_k = []
    for k in range(nbzkpts - 1, -1, -1):
        if bz2bz_k[k] == -1:
            bz2bz_k[bz2bz_ks[k]] = k
            ibz2bz_k.append(k)
    ibz2bz_k = np.array(ibz2bz_k[::-1])
    bz2bz_k = bz2bz_k[:-1].copy()

    bz2ibz_k = np.empty(nbzkpts, int)
    bz2ibz_k[ibz2bz_k] = np.arange(len(ibz2bz_k))
    bz2ibz_k = bz2ibz_k[bz2bz_k]

    kpts.bz2ibz = bz2ibz_k

    kpts.ibz2bz = ibz2bz_k
    kpts.ibz_weight = np.bincount(bz2ibz_k) *(1.0 / nbzkpts)
    kpts.ibz_k_scaled = kpts.bz_k_scaled[kpts.ibz2bz]
    kpts.ibz_k = kpts.cell.get_abs_kpts(kpts.ibz_k_scaled)
    kpts.nibzk = len(kpts.ibz_k)

    for k in range(len(kpts.bz_k)):
        bz_k_scaled = kpts.bz_k_scaled[k]
        ibz_idx = kpts.bz2ibz[k]
        ibz_k_scaled = kpts.ibz_k_scaled[ibz_idx]
        for io in range(len(kpts.op_rot)):
            op = kpts.op_rot[io]
            diff = bz_k_scaled - np.dot(ibz_k_scaled, op.T)
            if (np.absolute(diff) < KPT_DIFF_TOL).all():
                kpts.sym_conn[k] = io
                break

    for i in range(len(kpts.ibz_k)):
        kpts.sym_group.append([])
        ibz_k_scaled = kpts.ibz_k_scaled[i]
        idx = np.where(kpts.bz2ibz == i)[0]
        kpts.bz_k_group.append(idx)
        for j in range(idx.size):
            bz_k_scaled = kpts.bz_k_scaled[idx[j]]
            for io in range(len(kpts.op_rot)):
                op = kpts.op_rot[io]
                diff = bz_k_scaled - np.dot(ibz_k_scaled, op.T)
                if (np.absolute(diff) < KPT_DIFF_TOL).all():
                    kpts.sym_group[i].append(io)
                    break

def map_k_points_fast(bzk_kc, U_scc, time_reversal, tol=1e-7):
    '''
    Find symmetry relations between k-points.
    Adopted from GPAW
    bz2bz_ks[k1,s] = k2 if k1*U.T = k2
    '''

    nbzkpts = len(bzk_kc)

    if time_reversal:
        U_scc = np.concatenate([U_scc, -U_scc])

    bz2bz_ks = -np.ones((nbzkpts, len(U_scc)), dtype=int)

    for s, U_cc in enumerate(U_scc):
        # Find mapped kpoints
        Ubzk_kc = np.dot(bzk_kc, U_cc.T)

        # Do some work on the input
        k_kc = np.concatenate([bzk_kc, Ubzk_kc])
        #k_kc = np.mod(np.mod(k_kc, 1), 1)
        aglomerate_points(k_kc, tol)
        k_kc = k_kc.round(-np.log10(tol).astype(int))
        #k_kc = np.mod(k_kc, 1)

        # Find the lexicographical order
        order = np.lexsort(k_kc.T)
        k_kc = k_kc[order]
        diff_kc = np.diff(k_kc, axis=0)
        equivalentpairs_k = np.array((diff_kc == 0).all(1), dtype=bool)

        # Mapping array.
        orders = np.array([order[:-1][equivalentpairs_k],
                           order[1:][equivalentpairs_k]])

        # This has to be true.
        assert (orders[0] < nbzkpts).all()
        assert (orders[1] >= nbzkpts).all()
        bz2bz_ks[orders[1] - nbzkpts, s] = orders[0]

    return bz2bz_ks



def aglomerate_points(k_kc, tol):

    '''
    remove numerical error
    Adopted from GPAW
    '''

    nd = k_kc.shape[1]
    nbzkpts = len(k_kc)

    inds_kc = np.argsort(k_kc, axis=0)

    for c in range(nd):
        sk_k = k_kc[inds_kc[:, c], c]
        dk_k = np.diff(sk_k)

        pt_K = np.argwhere(dk_k > tol)[:, 0]
        pt_K = np.append(np.append(0, pt_K + 1), nbzkpts)
        for i in range(len(pt_K) - 1):
            k_kc[inds_kc[pt_K[i]:pt_K[i + 1], c], c] = k_kc[inds_kc[pt_K[i], c], c]


def symmetrize_density(kpts, rhoR_k, ibz_k_idx, mesh):

    rhoR_k = np.asarray(rhoR_k, dtype=np.double, order='C')
    rhoR = np.zeros_like(rhoR_k, dtype=np.double, order='C')

    c_rhoR = rhoR.ctypes.data_as(ctypes.c_void_p)
    c_rhoR_k = rhoR_k.ctypes.data_as(ctypes.c_void_p)

    mesh = np.asarray(mesh, dtype=np.int32, order='C')
    c_mesh = mesh.ctypes.data_as(ctypes.c_void_p)
    for iop in kpts.sym_group[ibz_k_idx]: 
        op = np.asarray(kpts.op_rot[iop], dtype=np.int32, order='C')
        time_reversal = False
        if iop >= kpts.nrot:
            time_reversal = True
            op = -op
        if symm.is_eye(op) or symm.is_inversion(op):
            rhoR += rhoR_k
        else:
            c_op = op.ctypes.data_as(ctypes.c_void_p)
            libpbc.symmetrize(c_rhoR, c_rhoR_k, c_op, c_mesh)

    return rhoR

def transform_mo_coeff(kpts, mo_coeff_ibz):

    mos = []
    for k in range(kpts.nbzk):
        ibz_k_idx = kpts.bz2ibz[k]
        mo_coeff = mo_coeff_ibz[ibz_k_idx]
        iop = kpts.sym_conn[k]
        op = kpts.op_rot[iop]
        time_reversal = False
        if iop >= kpts.nrot:
            time_reversal = True
            op = -op
        if symm.is_eye(op):
            if time_reversal:
                mos.append(mo_coeff.conj())
            else:
                mos.append(mo_coeff)
        elif symm.is_inversion(op):
            mos.append(mo_coeff.conj())
        else:
            mo = symm.symmetrize_mo_coeff(kpts, mo_coeff, op)
            if time_reversal:
                mo = mo.conj()
            mos.append(mo)

    return mos

def transform_dm(kpts, dm_ibz):

    dms = []
    for k in range(kpts.nbzk):
        ibz_k_idx = kpts.bz2ibz[k]
        dm = dm_ibz[ibz_k_idx]
        iop = kpts.sym_conn[k]
        op = kpts.op_rot[iop]
        time_reversal = False
        if iop >= kpts.nrot:
            time_reversal = True
            op = -op
        if symm.is_eye(op):
            if time_reversal:
                dms.append(dm.conj())
            else:
                dms.append(dm)
        elif symm.is_inversion(op):
            dms.append(dm.conj())
        else:
            dm_p = symm.symmetrize_dm(kpts, dm, op)
            if time_reversal:
                dm_p = dm_p.conj()
            dms.append(dm_p)

    return dms

class KPoints():

    '''
    This class handles kpoint symmetries etc.
    '''

    def __init__(self, cell, kpts):

        self.cell = cell
        self.space_group = get_space_group(self.cell)

        self.bz_k_scaled = cell.get_scaled_kpts(kpts)
        self.bz_k = kpts
        self.bz_weight = np.asarray([1./len(kpts)]*len(kpts))
        self.bz2ibz = np.arange(len(kpts), dtype=int)

        self.ibz_k_scaled = self.bz_k_scaled
        self.ibz_k = kpts
        self.ibz_weight = np.asarray([1./len(kpts)]*len(kpts))
        self.ibz2bz = np.arange(len(kpts), dtype=int)

        self.op_rot = np.eye(3,dtype =int).reshape(1,3,3)
        self.nrot = 1
        self.sym_conn = np.zeros(len(kpts), dtype = int)
        self.sym_group = []
        self.bz_k_group = []

        self._nbzk = len(self.bz_k)
        self._nibzk = len(self.ibz_k)

    @property
    def nbzk(self):
        return self._nbzk

    @nbzk.setter
    def nbzk(self, n):
        self._nbzk = n

    @property
    def nibzk(self):
        return self._nibzk

    @nibzk.setter
    def nibzk(self, n):
        self._nibzk = n

    make_ibz_k = make_ibz_k
    symmetrize_density = symmetrize_density
    transform_mo_coeff = transform_mo_coeff
    transform_dm = transform_dm
