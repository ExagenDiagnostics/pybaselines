# -*- coding: utf-8 -*-
"""Helper functions for using splines.

Created on November 3, 2021
@author: Donald Erb


Several functions were adapted from Cython, Python, and C files from SciPy
(https://github.com/scipy/scipy, accessed November 2, 2021), which was
licensed under the BSD-3-Clause below.

Copyright (c) 2001-2002 Enthought, Inc.  2003-2019, SciPy Developers.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions
are met:

1. Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above
   copyright notice, this list of conditions and the following
   disclaimer in the documentation and/or other materials provided
   with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived
   from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""

import numpy as np
from scipy.interpolate import BSpline, splev
from scipy.linalg import solve_banded, solveh_banded
from scipy.sparse import csc_matrix, csr_matrix, spdiags

from ._compat import _HAS_NUMBA, jit


try:
    from scipy.interpolate import _bspl
    _scipy_btb_bty = _bspl._norm_eq_lsq
except (AttributeError, ImportError):
    # in case scipy ever changes
    _scipy_btb_bty = None


# adapted from scipy (scipy/interpolate/_bspl.pyx/find_interval); see license above
@jit(nopython=True, cache=True)
def _find_interval(knots, spline_degree, x_val, last_left, num_bases):
    """
    Finds the knot interval containing the x-value.

    Parameters
    ----------
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The spline degree.
    x_val : float
        The x-value to find the interval for.
    last_left : int
        The previous output of this function. For the first call, use any value
        less than `spline_degree` to start.
    num_bases : int
        The total number of basis functions. Equals ``len(knots) - spline_degree - 1``,
        but is precomputed rather than having to recompute each function call.

    Returns
    -------
    int
        The index in `knots` such that ``knots[index] <= x_val < knots[index + 1]``.

    """
    left = last_left if spline_degree < last_left < num_bases else spline_degree

    # x_val less than expected so shift knot interval left
    while x_val < knots[left] and left != spline_degree:
        left -= 1

    left += 1
    while x_val >= knots[left] and left != num_bases:
        left += 1

    return left - 1


# adapted from scipy (scipy/interpolate/src/__fitpack.h/_deBoor_D); see license above
@jit(nopython=True, cache=True)
def _de_boor(knots, x_val, spline_degree, left_knot_idx, work):
    """
    Computes the non-zero values of the spline bases for the given x-value.

    Parameters
    ----------
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    x_val : float
        The x-value at which the spline basis is being computed.
    spline_degree : int
        The degree of the spline.
    left_knot_idx : int
        The index in `knots` that defines the interval such that
        ``knots[left_knot_idx] <= x_val < knots[left_knot_idx + 1]``.
    work : numpy.ndarray, shape (``2 * (spline_degree + 1)``,)
        The working array. Modified inplace to store the non-zero values of the spline
        bases for `x_val`.

    Notes
    -----
    Computes the non-zero values for knots from ``knots[left_knot_idx]`` to
    ``knots[left_knot_idx - spline_degree]`` for the x-value using de Boor's recursive
    algorithm.

    """
    temp = work + spline_degree + 1
    work[0] = 1.0
    for i in range(1, spline_degree + 1):
        temp[:i] = work[:i]
        work[0] = 0.0
        for j in range(1, i + 1):
            idx = left_knot_idx + j
            right_knot = knots[idx]
            left_knot = knots[idx - i]
            if left_knot == right_knot:
                work[j] = 0.0
                continue

            factor = temp[j - 1] / (right_knot - left_knot)
            work[j - 1] += factor * (right_knot - x_val)
            work[j] = factor * (x_val - left_knot)


# adapted from scipy (scipy/interpolate/_bspl.pyx/_make_design_matrix); see license above
@jit(nopython=True, cache=True)
def __make_design_matrix(x, knots, spline_degree):
    """
    Calculates the data needed to create the sparse matrix of basis functions for the spline.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The degree of the spline.

    Returns
    -------
    basis_data : numpy.ndarray, shape (``N * (spline_degree + 1)``,)
        The data for all of the basis functions. The basis for each `x[i]` value is represented
        by ``basis_data[i * (spline_degree + 1):(i + 1) * (spline_degree + 1)]``.
    row_ind : numpy.ndarray, shape (``N * (spline_degree + 1)``,)
        The row indices of the data; used for converting `data` into a CSR matrix.
    col_ind : numpy.ndarray, shape (``N * (spline_degree + 1)``,)
        The column indices of the data; used for converting `data` into a CSR matrix.

    """
    len_x = len(x)
    spline_order = spline_degree + 1
    data_length = len_x * spline_order
    num_bases = len(knots) - spline_order
    work = np.zeros(2 * spline_order)
    basis_data = np.zeros(data_length)
    row_ind = np.zeros(data_length, dtype=np.intp)
    col_ind = np.zeros(data_length, dtype=np.intp)

    idx = 0
    left_knot_idx = spline_degree
    for i in range(len_x):
        x_val = x[i]
        left_knot_idx = _find_interval(knots, spline_degree, x_val, left_knot_idx, num_bases)
        _de_boor(knots, x_val, spline_degree, left_knot_idx, work)

        next_idx = idx + spline_order
        basis_data[idx:next_idx] = work[:spline_order]
        row_ind[idx:next_idx] = i
        col_ind[idx:next_idx] = np.arange(
            left_knot_idx - spline_degree, min(left_knot_idx + 1, num_bases)
        )
        idx = next_idx

    return basis_data, row_ind, col_ind


# adapted from scipy (scipy/interpolate/_bspl.pyx/_make_design_matrix); see license above
def _make_design_matrix(x, knots, spline_degree):
    """
    Creates the sparse matrix of basis functions for a B-spline.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The degree of the spline.

    Returns
    -------
    scipy.sparse.csr.csr_matrix, shape (N, K - `spline_degree` - 1)
        The sparse matrix containing all the spline basis functions.

    """
    data, row_ind, col_ind = __make_design_matrix(x, knots, spline_degree)
    return csr_matrix((data, (row_ind, col_ind)), (len(x), len(knots) - spline_degree - 1))


def _slow_design_matrix(x, knots, spline_degree):
    """
    A nieve way of constructing the B-spline basis matrix by evaluating each basis individually.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The degree of the spline.

    Returns
    -------
    scipy.sparse.csr.csr_matrix, shape (N, K - `spline_degree` - 1)
        The sparse matrix containing all the spline basis functions.

    """
    num_bases = len(knots) - spline_degree - 1
    basis = np.empty((num_bases, len(x)))
    coeffs = np.zeros(num_bases)
    # TODO this is still quite slow and memory intensive; could make something similar
    # to __make_design_matrix that is still fast enough without numba; could make a cached
    # version, but would need to be able to use knots and x without cacheing them since numpy
    # arrays are not hashable -> use an inner function probably; also has the benefit that
    # cache gets automatically deleted once basis is created; a cached version would probably
    # also be faster than the current numba version of _deBoor (assuming numba allows inner
    # functions and dictionaries/caches?)

    # evaluate each single basis
    for i in range(num_bases):
        coeffs[i] = 1  # evaluate the i-th basis within splev
        basis[i] = splev(x, (knots, coeffs, spline_degree))
        coeffs[i] = 0  # reset back to zero

    # The last and first coefficients for the first and last bases, respectively,
    # get values == 0 when doing the above calculation, which causes issues when
    # using the resulting csr_matrix's data attribute; instead, explicitly set
    # those values to a very small, non-zero value; if spline_degree==0, it's fine
    if spline_degree > 0:
        small_float = np.finfo(float).tiny
        basis[spline_degree, 0] = small_float
        basis[-(spline_degree + 1), -1] = small_float

    return csc_matrix(basis).T


def _spline_knots(x, num_knots=10, spline_degree=3, penalized=False):
    """
    Creates the basis matrix for B-splines and P-splines.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The array of x-values
    num_knots : int, optional
        The number of interior knots for the spline. Default is 10.
    spline_degree : int, optional
        The degree of the spline. Default is 3, which is a cubic spline.
    penalized : bool, optional
        Whether the basis matrix should be for a penalized spline or a regular
        B-spline. Default is False, which creates the basis for a B-spline.

    Returns
    -------
    knots : numpy.ndarray, shape (``num_knots + 2 * spline_degree``,)
        The array of knots for the spline, properly padded on each side.

    Notes
    -----
    If `penalized` is True, makes the knots uniformly spaced to create penalized
    B-splines (P-splines). That way, can use a finite difference matrix to impose
    penalties on the spline.

    The knots are padded on each end with `spline_degree` extra knots to provide proper
    support for the outermost inner knots.

    Raises
    ------
    ValueError
        Raised if `num_knots` is less than 2.

    References
    ----------
    Eilers, P., et al. Twenty years of P-splines. SORT: Statistics and Operations Research
    Transactions, 2015, 39(2), 149-186.

    Hastie, T., et al. The Elements of Statistical Learning. Springer, 2017. Chapter 5.

    """
    if num_knots < 2:  # num_knots == 2 means the only knots are the two endpoints
        raise ValueError('the number of knots must be at least 2')

    if penalized:
        x_min = x.min()
        x_max = x.max()
        # number of sections is num_knots - 1 since counting the first and last
        # knots as inner knots
        dx = (x_max - x_min) / (num_knots - 1)
        # calculate inner knots separately to ensure x_min and x_max are correct;
        # otherwise, they can be slighly off due to floating point errors
        inner_knots = np.linspace(x_min, x_max, num_knots)
        knots = np.concatenate((
            np.linspace(x_min - spline_degree * dx, x_min - dx, spline_degree),
            inner_knots,
            np.linspace(x_max + dx, x_max + spline_degree * dx, spline_degree),
        ))
    else:
        # TODO maybe provide a better way to select knot positions for regular B-splines
        inner_knots = np.percentile(x, np.linspace(0, 100, num_knots))
        knots = np.concatenate((
            np.repeat(inner_knots[0], spline_degree), inner_knots,
            np.repeat(inner_knots[-1], spline_degree)
        ))

    return knots


def _spline_basis(x, knots, spline_degree=3):
    """
    Constructs the spline basis matrix.

    Chooses the fastest constuction route based on the available options.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int, optional
        The degree of the spline. Default is 3, which is a cubic spline.

    Returns
    -------
    numpy.ndarray, shape (N, K - `spline_degree` - 1)
        The matrix of basis functions for the spline.

    Notes
    -----
    The numba version is ~70% faster than scipy's BSpline.design_matrix (tested
    with python 3.9.7 and scipy 1.8.0.dev0+1981), so the numba version is preferred.

    Most checks on the inputs are skipped since this is an internal function and the
    proper steps are assumed to be done. For more proper error handling in the inputs,
    see :func:`scipy.interpolate.make_lsq_spline`.

    """
    if _HAS_NUMBA:
        validate_inputs = True
        basis_func = _make_design_matrix
    elif hasattr(BSpline, 'design_matrix'):
        validate_inputs = False
        # BSpline.design_matrix introduced in scipy version 1.8.0
        basis_func = BSpline.design_matrix
    else:
        validate_inputs = True
        basis_func = _slow_design_matrix

    # validate inputs only if not using scipy's version
    if validate_inputs:
        len_knots = len(knots)
        if np.any(x < knots[spline_degree]) or np.any(x > knots[len_knots - spline_degree - 1]):
            raise ValueError((
                f'x-values are either < {knots[spline_degree]} or '
                f'> {knots[len_knots - spline_degree - 1]}'
            ))

    return basis_func(x, knots, spline_degree)


# adapted from scipy (scipy/interpolate/_bspl.pyx/_norm_eq_lsq); see license above
@jit(nopython=True, cache=True)
def _numba_btb_bty(x, knots, spline_degree, y, weights, ab, rhs, basis_data):
    """
    Computes ``B.T @ W @ B`` and ``B.T @ W @ y`` for a spline.

    The result of ``B.T @ W @ B`` is stored in LAPACK's lower banded format (see
    :func:`scipy.linalg.solveh_banded`).

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The degree of the spline.
    y : numpy.ndarray, shape (N,)
        The y-values for fitting the spline.
    weights : numpy.ndarray, shape(N,)
        The weights for each y-value.
    ab : numpy.ndarray, shape (`spline_degree` + 1, N)
        An array of zeros that will be modified inplace to contain ``B.T @ W @ B`` in
        lower banded format.
    rhs : numpy.ndarray, shape (N,)
        An array of zeros that will be modified inplace to contain the right-hand
        side of the normal equation, ``B.T @ W @ y``.
    basis_data : numpy.ndarray, shape (``N * (spline_degree + 1)``,)
        The data for all of the basis functions. The basis for each `x[i]` value is represented
        by ``basis_data[i * (spline_degree + 1):(i + 1) * (spline_degree + 1)]``. If the basis,
        `B` is a sparse matrix, then `basis_data` can be gotten using `B.tocsr().data`.

    Notes
    -----
    This function is slightly different than SciPy's `_norm_eq_lst` function in
    scipy.interpolate._bspl.pyx since this function uses the weights directly, rather
    than squaring the weights, and directly uses the basis data (gotten by using the
    `data` attribute of the basis in CSR sparse format) rather than computing the
    basis using de Boor's algorithm. This makes it much faster when solving a spline
    system using iteratively reweighted least squares since the basis only needs to be
    created once.

    There is no significant time difference between calling _find_interval each time this
    function is used compared to calculating all the intervals once and inputting them
    into this function.

    """
    spline_order = spline_degree + 1
    num_bases = len(knots) - spline_order
    work = np.zeros(2 * spline_order)

    left_knot_idx = spline_degree
    idx = 0
    for i in range(len(x)):
        x_val = x[i]
        y_val = y[i]
        weight_val = weights[i]
        left_knot_idx = _find_interval(knots, spline_degree, x_val, left_knot_idx, num_bases)

        next_idx = idx + spline_order
        work[:] = 0
        work[:spline_order] = basis_data[idx:next_idx]
        idx = next_idx
        for j in range(spline_order):
            work_val = work[j]
            # B.T @ W @ B
            for k in range(j + 1):
                column = left_knot_idx - spline_degree + k
                ab[j - k, column] += work_val * work[k] * weight_val

            # B.T @ W @ y
            row = left_knot_idx - spline_degree + j
            rhs[row] += work_val * y_val * weight_val


def _add_diagonals(array_1, array_2, lower_only=True):
    """
    Adds two arrays containing the diagonals of banded matrices.

    The array with the least rows is padded with zeros to allow the sum of the two arrays.

    Parameters
    ----------
    array_1 : numpy.ndarray, shape (A, N)
        An array to add.
    array_2 : numpy.ndarray, shape (B, N)
        An array to add.
    lower_only : bool, optional
        If True (default), will only add zero padding to the bottom of the smaller
        array. If False, will add half of the zero padding to both the top and bottom
        of the smaller array.

    Returns
    -------
    summed_diagonals : numpy.ndarray, shape (`max(A, B)`, N)
        The addition of `a` and `b` after adding the correct zero padding.

    Raises
    ------
    ValueError
        Raised if `a.shape[1]` and `b.shape[1]` are not equal or if `lower` is False
        and `abs(a.shape[0] - b.shape[0])` is not even.

    """
    a, b = np.atleast_2d(array_1, array_2)
    a_shape = a.shape
    b_shape = b.shape
    if a_shape[1] != b_shape[1]:
        raise ValueError((
            f'the diagonal arrays have a dimension mismatch; {a_shape[1]} and {b_shape[1]}'
            ' should be equal'
        ))
    row_mismatch = a_shape[0] - b_shape[0]
    if row_mismatch == 0:
        summed_diagonals = a + b
    else:
        abs_mismatch = abs(row_mismatch)
        if lower_only:
            padding = np.zeros((abs_mismatch, a_shape[1]))
            if row_mismatch > 0:
                summed_diagonals = a + np.concatenate((b, padding))
            else:
                summed_diagonals = np.concatenate((a, padding)) + b
        else:
            if abs_mismatch % 2:
                raise ValueError(
                    'row mismatch between the arrays must be even if lower_only=False, '
                    f'instead got {abs_mismatch}'
                )
            padding = np.zeros((abs_mismatch // 2, a_shape[1]))
            if row_mismatch > 0:
                summed_diagonals = a + np.concatenate((padding, b, padding))
            else:
                summed_diagonals = np.concatenate((padding, a, padding)) + b

    return summed_diagonals


# adapted from scipy (scipy/interpolate/_bsplines.py/make_lsq_spline); see license above
def _solve_pspline(x, y, weights, basis, penalty, knots, spline_degree, rhs_extra=None,
                   lower_only=True):
    """
    Solves the coefficients for a weighted penalized spline.

    Solves the linear equation ``(B.T @ W @ B + P) c = B.T @ W @ y`` for the spline
    coefficients, `c`, given the spline basis, `B`, the weights (diagonal of `W`), the
    penalty `P`, and `y`. Attempts to calculate ``B.T @ W @ B`` and ``B.T @ W @ y`` as
    a banded system to speed up the calculation.

    Parameters
    ----------
    x : numpy.ndarray, shape (N,)
        The x-values for the spline.
    y : numpy.ndarray, shape (N,)
        The y-values for fitting the spline.
    weights : numpy.ndarray, shape (N,)
        The weights for each y-value.
    basis : scipy.sparse.base.spmatrix, shape (N, K - `spline_degree` - 1)
        The sparse spline basis matrix. CSR format is preferred.
    penalty : numpy.ndarray, shape (D, N)
        The finite difference penalty matrix, in LAPACK's lower banded format (see
        :func:`scipy.linalg.solveh_banded`) if `lower_only` is True or the full banded
        format (see :func:`scipy.linalg.solve_banded`) if `lower_only` is False.
    knots : numpy.ndarray, shape (K,)
        The array of knots for the spline. Should be padded on each end with
        `spline_degree` extra knots.
    spline_degree : int
        The degree of the spline.
    rhs_extra : float or numpy.ndarray, shape (N,), optional
        If supplied, `rhs_extra` will be added to the right hand side (``B.T @ W @ y``)
        of the equation before solving. Default is None, which adds nothing.
    lower_only : boolean, optional
        If True (default), will include only the lower non-zero diagonals of
        ``B.T @ W @ B`` and use :func:`scipy.linalg.solveh_banded` to solve the equation.
        If False, will use all of the non-zero diagonals and use
        :func:`scipy.linalg.solve_banded` for solving. `penalty` is not modified, so it
        must be in the correct lower or full format before passing to this function.

    Returns
    -------
    coeffs : numpy.ndarray, shape (K - `spline_degree` - 1,)
        The coefficients for the spline. To calculate the spline, do ``basis @ coeffs``.

    Raises
    ------
    ValueError
        Raised if `penalty` and the calculated `basis.T @ W @ basis` have different number
        of columns.

    Notes
    -----
    Most checks on the inputs are skipped since this is an internal function and the
    proper steps are assumed to be done. For more proper error handling in the inputs,
    see :func:`scipy.interpolate.make_lsq_spline`.

    """
    use_backup = True
    num_bases = basis.shape[1]
    # prefer numba version since it directly uses the basis
    if _HAS_NUMBA:
        # the spline basis must explicitly be created such that the csr matrix's data
        # attribute is not missing any zeros; it is correct for all the internal basis
        # creation functions used, but need to ensure just in case something ever changes;
        # could guess if missing_values==2 that the first and last basis functions are
        # missing a near-zero value, but safer to just move to other options
        basis_data = basis.tocsr().data
        missing_values = len(y) * (spline_degree + 1) - len(basis_data)
        if not missing_values:
            # TODO if using the numba version, does fortran ordering speed up the calc? or
            # can ab just be c ordered?

            # create ab and rhs arrays outside of numba function since numba's implementation
            # of np.zeros is slower than numpy's (https://github.com/numba/numba/issues/7259)
            ab = np.zeros((spline_degree + 1, num_bases), order='F')
            rhs = np.zeros(num_bases)
            _numba_btb_bty(x, knots, spline_degree, y, weights, ab, rhs, basis_data)
            # TODO can probably make the full matrix directly within the numba
            # btb calculation
            if not lower_only:
                ab = _lower_to_full(ab)
            use_backup = False

    if use_backup and _scipy_btb_bty is not None:
        ab = np.zeros((spline_degree + 1, num_bases), order='F')
        rhs = np.zeros((num_bases, 1), order='F')
        _scipy_btb_bty(x, knots, spline_degree, y.reshape(-1, 1), np.sqrt(weights), ab, rhs)
        rhs = rhs.reshape(-1)
        if not lower_only:
            ab = _lower_to_full(ab)
        use_backup = False

    if use_backup:
        # worst case scenario; have to convert weights to a sparse diagonal matrix,
        # do B.T @ W @ B, and convert back to lower banded
        len_y = len(y)
        full_matrix = basis.T @ spdiags(weights, 0, len_y, len_y, 'csr') @ basis
        rhs = basis.T @ (weights * y)
        ab = full_matrix.todia().data[::-1]
        # take only the lower diagonals of the symmetric ab; cannot just do
        # ab[spline_degree:] since some diagonals become fully 0 and are truncated from
        # the data attribute, so have to calculate the number of bands first
        if lower_only:
            ab = ab[len(ab) // 2:]

    lhs = _add_diagonals(ab, penalty, lower_only)
    if rhs_extra is not None:
        rhs = rhs + rhs_extra

    if lower_only:
        coeffs = solveh_banded(
            lhs, rhs, overwrite_ab=True, overwrite_b=True, lower=True,
            check_finite=False
        )
    else:
        bands = len(lhs) // 2
        coeffs = solve_banded(
            (bands, bands), lhs, rhs, overwrite_ab=True, overwrite_b=True,
            check_finite=False
        )

    return coeffs


def _lower_to_full(ab):
    """
    Converts a lower banded array to a symmetric banded array.

    The lower bands are flipped and then shifted to make the upper bands.

    Parameters
    ----------
    ab : numpy.ndarray, shape (N, M)
        The lower banded array.

    Returns
    -------
    ab_full : numpy.ndarray, shape (``2 * N - 1``, M)
        The full, symmetric banded array.

    """
    ab_rows, ab_columns = ab.shape
    ab_full = np.concatenate((np.zeros((ab_rows - 1, ab_columns)), ab))
    ab_full[:ab_rows - 1] = ab[1:][::-1]
    for row, shift in enumerate(range(-(ab_rows - 1), 0)):
        ab_full[row, -shift:] = ab_full[row, :shift]
        ab_full[row, :-shift] = 0

    return ab_full
