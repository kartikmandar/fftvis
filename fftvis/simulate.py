from __future__ import annotations

import matvis
from . import utils, beams
import numpy as np
import finufft
from typing import Callable
from matvis import conversions

default_accuracy_dict = {
    1: 6e-8,
    2: 1e-12,
}


def simulate_vis(
    antpos: dict,
    sources: np.ndarray,
    ra: np.ndarray,
    dec: np.ndarray,
    freqs: np.ndarray,
    lsts: np.ndarray,
    beam,
    baselines: list[tuple] = None,
    precision: int = 1,
    polarized: bool = False,
    latitude: float = -0.5361913261514378,
    accuracy: float = None,
):
    """
    Parameters:
    ----------
    antpos : dict
        Dictionary of antenna positions
    freqs : np.ndarray
        Frequencies to evaluate visibilities at MHz.
    sources : np.ndarray
        asdf
    beam : UVBeam
        pass
    crd_eq : np.ndarray
        pass
    eq2tops : np.ndarray
        pass
    precision : int, optional
       Which precision level to use for floats and complex numbers
       Allowed values:
       - 1: float32, complex64
       - 2: float64, complex128
    use_redundancy : bool, default = True
        If True,
    check : bool, default = False
        If True, perform checks on the input data array prior to
    accuracy : float, default = None
        Desired accuracy of the non-uniform fast fourier transform. If None, the default accuracy
        for the given precision will be used. For precision 1, the default accuracy is 6e-8, and for
        precision 2, the default accuracy is 1e-12.

    Returns:
    -------
    vis : np.ndarray

    """
    # Get the accuracy for the given precision if not provided
    if accuracy is None:
        accuracy = default_accuracy_dict[precision]

    # Source coordinate transform, from equatorial to Cartesian
    crd_eq = conversions.point_source_crd_eq(ra, dec)

    # Get coordinate transforms as a function of LST
    eq2tops = np.array([conversions.eci_to_enu_matrix(lst, latitude) for lst in lsts])

    return simulate(
        antpos=antpos,
        freqs=freqs,
        sources=sources,
        beam=beam,
        crd_eq=crd_eq,
        eq2tops=eq2tops,
        baselines=baselines,
        precision=precision,
        polarized=polarized,
        accuracy=accuracy,
    )


def simulate(
    antpos: dict,
    freqs: np.ndarray,
    sources: np.ndarray,
    beam,
    crd_eq: np.ndarray,
    eq2tops: np.ndarray,
    baselines: list[tuple] = None,
    precision: int = 1,
    polarized: bool = False,
    accuracy: float = 1e-6,
    use_feed: str = "x",
):
    """
    Parameters:
    ----------
    antpos : dict
        Dictionary of antenna positions
    freqs : np.ndarray
        Frequencies to evaluate visibilities at MHz.
    sources : np.ndarray
        asdf
    beam : UVBeam
        pass
    crd_eq : np.ndarray
        pass
    eq2tops : np.ndarray
        pass
    baselines : list of tuples, default = None
        If provided, only the baselines within the list will be simulated and array of shape
        (nbls, nfreqs, ntimes) will be returned
    precision : int, optional
        Which precision level to use for floats and complex numbers
        Allowed values:
        - 1: float32, complex64
        - 2: float64, complex128
    accuracy : float, default = 1e-6
        pass

    Returns:
    -------
    vis : np.ndarray

    """
    # Get sizes of inputs
    nfreqs = np.size(freqs)
    nants = len(antpos)
    ntimes = len(eq2tops)

    if polarized:
        nax = nfeeds = 2
    else:
        nax = nfeeds = 1

    if precision == 1:
        real_dtype = np.float32
        complex_dtype = np.complex64
    else:
        real_dtype = np.float64
        complex_dtype = np.complex128

    # Get the redundant - TODO handle this better
    if not baselines:
        reds = utils.get_pos_reds(antpos, include_autos=True)
        baselines = [red[0] for red in reds]
        nbls = len(baselines)
        bl_to_red_map = {red[0]: np.array(red) for red in reds}
        expand_vis = True
    else:
        nbls = len(baselines)
        expand_vis = False

    # prepare beam
    # TODO: uncomment and test this when simulating multiple polarizations
    beam = conversions.prepare_beam(beam, polarized=polarized, use_feed=use_feed)

    # Convert to correct precision
    crd_eq = crd_eq.astype(real_dtype)
    eq2tops = eq2tops.astype(real_dtype)

    # Factor of 0.5 accounts for splitting Stokes between polarization channels
    Isky = (0.5 * sources).astype(complex_dtype)

    # Compute baseline vectors
    blx, bly = np.array([antpos[bl[1]] - antpos[bl[0]] for bl in baselines])[
        :, :2
    ].T.astype(real_dtype)

    # Generate visibility array
    if expand_vis:
        vis = np.zeros(
            (ntimes, nants, nants, nfeeds, nfeeds, nfreqs), dtype=complex_dtype
        )
    else:
        vis = np.zeros((ntimes, nbls, nfeeds, nfeeds, nfreqs), dtype=complex_dtype)

    # Loop over time samples
    for ti, eq2top in enumerate(eq2tops):
        # Convert to topocentric coordinates
        tx, ty, tz = np.dot(eq2top, crd_eq)

        # Only simulate above the horizon
        above_horizon = tz > 0
        tx = tx[above_horizon]
        ty = ty[above_horizon]

        # Number of above horizon points
        nsim_sources = above_horizon.sum()

        # Form the visibility array
        if polarized:
            _vis = np.zeros((nfeeds, nfeeds, nbls, nfreqs), dtype=complex_dtype)
        else:
            _vis = np.zeros((nfeeds, nfeeds, nbls, nfreqs), dtype=complex_dtype)

        az, za = conversions.enu_to_az_za(enu_e=tx, enu_n=ty, orientation="uvbeam")

        # TODO: finufft2d3 is not vectorized over time
        # TODO: finufft2d3 gives me warning if I don't use ascontiguousarray
        for fi in range(nfreqs):
            # Compute uv coordinates
            u, v = (
                blx * freqs[fi] / utils.speed_of_light,
                bly * freqs[fi] / utils.speed_of_light,
            )

            # Compute beams - only single beam is supported
            A_s = np.zeros((nax, nfeeds, 1, nsim_sources), dtype=complex_dtype)
            A_s = beams._evaluate_beam(A_s, [beam], az, za, polarized, freqs[fi])[
                ..., 0, :
            ]
            A_s = np.flipud(A_s)
            A_s = np.reshape(A_s, (nax * nfeeds, nsim_sources))

            # Compute sky beam product
            i_sky = A_s * A_s.conj() * Isky[above_horizon, fi]

            # Compute visibilities w/ non-uniform FFT
            v = finufft.nufft2d3(
                2 * np.pi * tx,
                2 * np.pi * ty,
                i_sky,
                u,
                v,
                modeord=0,
                eps=accuracy,
            )

            # Expand out the visibility array
            _vis[..., fi] = v.reshape(nfeeds, nfeeds, nbls)

        # Expand out the visibility array in antenna by antenna matrix
        if expand_vis:
            for bi, bls in enumerate(baselines):
                np.add.at(
                    vis,
                    (ti, bl_to_red_map[bls][:, 0], bl_to_red_map[bls][:, 1]),
                    _vis[..., bi, :],
                )

                # Add the conjugate, avoid auto baselines twice
                if bls[0] != bls[1]:
                    np.add.at(
                        vis,
                        (ti, bl_to_red_map[bls][:, 1], bl_to_red_map[bls][:, 0]),
                        _vis[..., bi, :].conj(),
                    )
        else:
            vis[ti] = _vis

    if expand_vis:
        return (
            np.transpose(vis, (5, 0, 3, 4, 1, 2))
            if polarized
            else np.moveaxis(vis[..., 0, 0, :], 3, 0)
        )
    else:
        return (
            np.transpose(vis, (4, 0, 2, 3, 1))
            if polarized
            else np.moveaxis(vis[..., 0, 0, :], 2, 0)
        )


def simulate_basis(
    antpos: dict,
    eta: np.ndarray,
    freqs: np.ndarray,
    sources: np.ndarray,
    crd_eq: np.ndarray,
    eq2tops: np.ndarray,
    baselines: list[tuple] = None,
    precision: int = 1,
    polarized: bool = False,
    accuracy: float = 1e-6,
):
    """
    Simulate the sky using some basis as simulation

    precision : int, optional
        Which precision level to use for floats and complex numbers
        Allowed values:
        - 1: float32, complex64
        - 2: float64, complex128
    """
    # Check inputs are valid
    nfreqs = np.size(freqs)
    # nax, nfeeds, nants, ntimes = matvis.cpu._validate_inputs(
    #    precision, polarized, antpos, eq2tops, crd_eq, sources
    # )
    nants = len(antpos)
    ntimes = len(eq2tops)

    if precision == 1:
        real_dtype = np.float32
        complex_dtype = np.complex64
    else:
        real_dtype = np.float64
        complex_dtype = np.complex128

    # prepare beam
    # TODO: add this when using multiple polarizations
    # beam = conversions.prepare_beam(beam)

    # Get the redundant - TODO handle this better
    if not baselines:
        reds = utils.get_pos_reds(antpos)
        baselines = [red[0] for red in reds]
        nbls = len(baselines)
        bl_to_red_map = {red[0]: np.array(red) for red in reds}
        expand_vis = True
    else:
        nbls = len(baselines)
        expand_vis = False

    # Convert to correct precision
    crd_eq = crd_eq.astype(real_dtype)
    eq2tops = eq2tops.astype(real_dtype)

    # Factor of 0.5 accounts for splitting Stokes between polarization channels
    Isky = (0.5 * sources).astype(complex_dtype)

    # Compute coordinates
    blx, bly = np.array([antpos[bl[1]] - antpos[bl[0]] for bl in baselines])[
        :, :2
    ].T.astype(real_dtype)

    # Baseline coordinates
    U, tF = np.meshgrid(blx / utils.speed_of_light, freqs)
    V, _ = np.meshgrid(bly / utils.speed_of_light, freqs)

    # output visibility array
    if expand_vis:
        vis = np.zeros((nants, nants, ntimes, nfreqs), dtype=complex_dtype)
    else:
        vis = np.zeros((nbls, ntimes, nfreqs), dtype=complex_dtype)

    # Loop over time samples
    for ti, eq2top in enumerate(eq2tops):
        # Convert to topocentric coordinates
        tx, ty, tz = np.dot(eq2top, crd_eq)
        above_horizon = tz > 0
        tx = tx[above_horizon]
        ty = ty[above_horizon]

        # Sky Coordinates
        tX, _eta = np.meshgrid(tx, eta)
        tY, _ = np.meshgrid(ty, eta)

        # Simulate
        _vis = finufft.nufft3d3(
            2 * np.pi * np.ravel(tX),
            2 * np.pi * np.ravel(tY),
            np.ravel(_eta),
            np.ravel(Isky[above_horizon]),
            np.ravel(U * tF),
            np.ravel(V * tF),
            np.ravel(tF),
            modeord=0,
            eps=accuracy,
        )
        _vis.shape = (nfreqs, nbls)

        if expand_vis:
            for bi, bls in enumerate(baselines):
                np.add.at(
                    vis,
                    (bl_to_red_map[bls][:, 0], bl_to_red_map[bls][:, 1], ti),
                    _vis[:, bi],
                )
                np.add.at(
                    vis,
                    (bl_to_red_map[bls][:, 1], bl_to_red_map[bls][:, 0], ti),
                    _vis[:, bi].conj(),
                )

    return vis
