# -*- coding: utf-8 -*-

# Copyright (c) 2016 by University of Kassel and Fraunhofer Institute for Wind Energy and Energy
# System Technology (IWES), Kassel. All rights reserved. Use of this source code is governed by a
# BSD-style license that can be found in the LICENSE file.

import numpy as np
import warnings
import copy

from scipy.sparse import csr_matrix as sparse

import pypower.ppoption as ppopt
from pypower.idx_bus import NONE, BUS_I, BUS_TYPE
from pypower.idx_gen import GEN_BUS, GEN_STATUS
from pypower.idx_brch import F_BUS, T_BUS, BR_STATUS, QT
from pypower.idx_area import PRICE_REF_BUS
from pypower.run_userfcn import run_userfcn

from pandapower.runpf import _runpf
from pandapower.auxiliary import ppException
from pandapower.results import _extract_results
from pandapower.build_branch import _build_branch_ppc, _switch_branches\
    , _branches_with_oos_buses, _update_trafo_trafo3w_ppc
from pandapower.build_bus import _build_bus_ppc, _calc_loads_and_add_on_ppc, \
    _calc_shunts_and_add_on_ppc
from pandapower.build_gen import _build_gen_ppc, _update_gen_ppc


class LoadflowNotConverged(ppException):
    """
    Exception being raised in case loadflow did not converge.
    """
    pass


def runpp(net, init="flat", calculate_voltage_angles=False, tolerance_kva=1e-5, trafo_model="t"
          , trafo_loading="current", enforce_q_lims=False, numba=True, recycle=None, **kwargs):
    """
    Runs PANDAPOWER AC Flow

    Note: May raise pandapower.api.run["load"]flowNotConverged

    INPUT:
        **net** - The Pandapower format network

    Optional:

        **init** (str, "flat") - initialization method of the loadflow
        Pandapower supports three methods for initializing the loadflow:

            - "flat"- flat start with voltage of 1.0pu and angle of 0° at all buses as initial solution
            - "dc" - initial DC loadflow before the AC loadflow. The results of the DC loadflow are used as initial solution for the AC loadflow.
            - "results" - voltage vector of last loadflow from net.res_bus is used as initial solution. This can be useful to accelerate convergence in iterative loadflows like time series calculations.

        **calculate_voltage_angles** (bool, False) - consider voltage angles in loadflow calculation

            If True, voltage angles are considered in the  loadflow calculation. In some cases with
            large differences in voltage angles (for example in case of transformers with high
            voltage shift), the difference between starting and end angle value is very large.
            In this case, the loadflow might be slow or it might not converge at all. That is why 
            the possibility of neglecting the voltage angles of transformers and ext_grids is
            provided to allow and/or accelarate convergence for networks where calculation of 
            voltage angles is not necessary. Note that if calculate_voltage_angles is True the
            loadflow is initialized with a DC power flow (init = "dc")

            The default value is False because pandapower was developed for distribution networks.
            Please be aware that this parameter has to be set to True in meshed network for correct
            results!

        **tolerance_kva** (float, 1e-5) - loadflow termination condition referring to P / Q mismatch of node power in kva

        **trafo_model** (str, "t")  - transformer equivalent circuit model
        Pandapower provides two equivalent circuit models for the transformer:

            - "t" - transformer is modelled as equivalent with the T-model. This is consistent with PowerFactory and is also more accurate than the PI-model. We recommend using this transformer model.
            - "pi" - transformer is modelled as equivalent PI-model. This is consistent with Sincal, but the method is questionable since the transformer is physically T-shaped. We therefore recommend the use of the T-model. 

        **trafo_loading** (str, "current") - mode of calculation for transformer loading

            Transformer loading can be calculated relative to the rated current or the rated power. In both cases the overall transformer loading is defined as the maximum loading on the two sides of the transformer.

            - "current"- transformer loading is given as ratio of current flow and rated current of the transformer. This is the recommended setting, since thermal as well as magnetic effects in the transformer depend on the current.
            - "power" - transformer loading is given as ratio of apparent power flow to the rated apparent power of the transformer. 

        **enforce_q_lims** (bool, False) - respect generator reactive power limits

            If True, the reactive power limits in net.gen.max_q_kvar/min_q_kvar are respected in the
            loadflow. This is done by running a second loadflow if reactive power limits are
            violated at any generator, so that the runtime for the loadflow will increase if reactive
            power has to be curtailed.

        **numba** (bool, True) - Usage numba JIT compiler

            If set to True, the numba JIT compiler is used to generate matrices for the powerflow. Massive
            speed improvements are likely.

        **recycle** (dict, none) - Reuse of internal powerflow variables

            Contains a dict with the following parameters:
            is_elems: If True in service elements are not filtered again and are taken from the last result in net["_is_elems"]
            ppc: If True the ppc (PYPOWER case file) is taken from net["_ppc"] and gets updated instead of regenerated entirely
            bus_lookup: If True the bus_lookup variable (Indices Pandapower -> ppc) is taken from net["_bus_lookup"]
            Ybus: If True the admittance matrix (Ybus, Yf, Yt) is taken from ppc["internal"] and not regenerated

        ****kwargs** - options to use for PYPOWER.runpf
    """
    ac = True
    # recycle parameters
    if recycle == None:
        recycle = dict(is_elems=False, ppc=False, Ybus=False)

    _runpppf(net, init, ac, calculate_voltage_angles, tolerance_kva, trafo_model,
             trafo_loading, enforce_q_lims, numba, recycle, **kwargs)


def rundcpp(net, trafo_model="t", trafo_loading="current", suppress_warnings=True, recycle=None, **kwargs):
    """
    Runs PANDAPOWER DC Flow

    Note: May raise pandapower.api.run["load"]flowNotConverged

    INPUT:
        **net** - The Pandapower format network

    Optional:

        **trafo_model** (str, "t")  - transformer equivalent circuit model
        Pandapower provides two equivalent circuit models for the transformer:

            - "t" - transformer is modelled as equivalent with the T-model. This is consistent with PowerFactory and is also more accurate than the PI-model. We recommend using this transformer model.
            - "pi" - transformer is modelled as equivalent PI-model. This is consistent with Sincal, but the method is questionable since the transformer is physically T-shaped. We therefore recommend the use of the T-model. 

        **trafo_loading** (str, "current") - mode of calculation for transformer loading

            Transformer loading can be calculated relative to the rated current or the rated power. In both cases the overall transformer loading is defined as the maximum loading on the two sides of the transformer.

            - "current"- transformer loading is given as ratio of current flow and rated current of the transformer. This is the recommended setting, since thermal as well as magnetic effects in the transformer depend on the current.
            - "power" - transformer loading is given as ratio of apparent power flow to the rated apparent power of the transformer. 

        **suppress_warnings** (bool, True) - suppress warnings in pypower

            If set to True, warnings are disabled during the loadflow. Because of the way data is
            processed in pypower, ComplexWarnings are raised during the loadflow. These warnings are
            suppressed by this option, however keep in mind all other pypower warnings are also suppressed.

        **numba** (bool, True) - Usage numba JIT compiler

            If set to True, the numba JIT compiler is used to generate matrices for the powerflow. Massive
            speed improvements are likely.

        **recycle** (dict, none) - Reuse of internal powerflow variables

            Contains a dict with the following parameters:
            is_elems: If True in service elements are not filtered again and are taken from the last result in net["_is_elems"]
            ppc: If True the ppc (PYPOWER case file) is taken from net["_ppc"] and gets updated instead of regenerated entirely
            bus_lookup: If True the bus_lookup variable (Indices Pandapower -> ppc) is taken from net["_bus_lookup"]
            Ybus: If True the admittance matrix (Ybus, Yf, Yt) is taken from ppc["internal"] and not regenerated

        ****kwargs** - options to use for PYPOWER.runpf
    """
    ac = False
    # the following parameters have no effect if ac = False
    calculate_voltage_angles = True
    enforce_q_lims = False
    init = ''
    tolerance_kva = 1e-5
    numba = True
    if recycle == None:
        recycle = dict(is_elems=False, ppc=False, Ybus=False)

    _runpppf(net, init, ac, calculate_voltage_angles, tolerance_kva, trafo_model,
             trafo_loading, enforce_q_lims, numba, recycle, **kwargs)


def _runpppf(net, init, ac, calculate_voltage_angles, tolerance_kva, trafo_model,
             trafo_loading, enforce_q_lims, numba, recycle, **kwargs):
    """
    Gets called by runpp or rundcpp with different arguments.
    """

    net["converged"] = False
    if (ac and not init == "results") or not ac:
        reset_results(net)

    # select elements in service (time consuming, so we do it once)
    is_elems = _select_is_elements(net, recycle)

    if recycle["ppc"] and "_ppc" in net and net["_ppc"] is not None and "_bus_lookup" in net:
        # update the ppc from last cycle
        ppc, ppci, bus_lookup = _update_ppc(net, is_elems, recycle, calculate_voltage_angles, enforce_q_lims,
                                            trafo_model)
    else:
        # convert pandapower net to ppc
        ppc, ppci, bus_lookup = _pd2ppc(net, is_elems, calculate_voltage_angles, enforce_q_lims,
                                       trafo_model, init_results=(init == "results"))

    # store variables
    net["_ppc"] = ppc
    net["_bus_lookup"] = bus_lookup
    net["_is_elems"] = is_elems

    if not "VERBOSE" in kwargs:
        kwargs["VERBOSE"] = 0

    # run the powerflow
    result = _runpf(ppci, init, ac, numba, recycle, ppopt=ppopt.ppoption(ENFORCE_Q_LIMS=enforce_q_lims,
                                                                   PF_TOL=tolerance_kva * 1e-3, **kwargs))[0]

    # ppci doesn't contain out of service elements, but ppc does -> copy results accordingly
    result = _copy_results_ppci_to_ppc(result, ppc, bus_lookup)

    # raise if PF was not successful. If DC -> success is always 1
    if result["success"] != 1:
        raise LoadflowNotConverged("Loadflow did not converge!")
    else:
        net["_ppc"] = result
        net["converged"] = True

    _extract_results(net, result, is_elems, bus_lookup, trafo_loading, ac)
    _clean_up(net)


def reset_results(net):
    net["res_bus"] = copy.copy(net["_empty_res_bus"])
    net["res_ext_grid"] = copy.copy(net["_empty_res_ext_grid"])
    net["res_line"] = copy.copy(net["_empty_res_line"])
    net["res_load"] = copy.copy(net["_empty_res_load"])
    net["res_sgen"] = copy.copy(net["_empty_res_sgen"])
    net["res_trafo"] = copy.copy(net["_empty_res_trafo"])
    net["res_trafo3w"] = copy.copy(net["_empty_res_trafo3w"])
    net["res_shunt"] = copy.copy(net["_empty_res_shunt"])
    net["res_impedance"] = copy.copy(net["_empty_res_impedance"])
    net["res_gen"] = copy.copy(net["_empty_res_gen"])
    net["res_ward"] = copy.copy(net["_empty_res_ward"])
    net["res_xward"] = copy.copy(net["_empty_res_xward"])

def _select_is_elements(net, recycle=None):
    """
    Selects certain "in_service" elements from net.
    This is quite time consuming so it is done once at the beginning


    @param net: Pandapower Network
    @return: is_elems Certain in service elements
    """

    if recycle is not None and recycle["is_elems"]:
        if "_is_elems" not in net or net["_is_elems"] is None:
            # sort elements according to their in service status
            elems = ['bus', 'line']
            for elm in elems:
                net[elm] = net[elm].sort_values(by=['in_service'], ascending=0)

            # select in service buses. needed for the other elements to be selected
            bus_is = net["bus"]["in_service"].values.astype(bool)
            line_is = net["line"]["in_service"].values.astype(bool)
            bus_is_ind = net["bus"][bus_is].index
            # check if in service elements are at in service buses
            is_elems = {
                "gen": net['gen'][np.in1d(net["gen"].bus.values, bus_is_ind) \
                                  & net["gen"]["in_service"].values.astype(bool)]
                , "load": np.in1d(net["load"].bus.values, bus_is_ind) \
                          & net["load"].in_service.values.astype(bool)
                , "sgen": np.in1d(net["sgen"].bus.values, bus_is_ind) \
                          & net["sgen"].in_service.values.astype(bool)
                , "ward": np.in1d(net["ward"].bus.values, bus_is_ind) \
                          & net["ward"].in_service.values.astype(bool)
                , "xward": np.in1d(net["xward"].bus.values, bus_is_ind) \
                           & net["xward"].in_service.values.astype(bool)
                , "shunt": np.in1d(net["shunt"].bus.values, bus_is_ind) \
                           & net["shunt"].in_service.values.astype(bool)
                , "ext_grid": net["ext_grid"][np.in1d(net["ext_grid"].bus.values, bus_is_ind) \
                                        & net["ext_grid"]["in_service"].values.astype(bool)]
                , 'bus': net['bus'].iloc[:np.count_nonzero(bus_is)]
                , 'line': net['line'].iloc[:np.count_nonzero(line_is)]
            }
        else:
            # just update the elements
            is_elems = net['_is_elems']

            bus_is_ind = is_elems['bus'].index
            #update elements
            elems = ['gen', 'ext_grid']
            for elm in elems:
                is_elems[elm] = net[elm][np.in1d(net[elm].bus.values, bus_is_ind) \
                                     & net[elm]["in_service"].values.astype(bool)]

    else:
        # select in service buses. needed for the other elements to be selected
        bus_is = net["bus"]["in_service"].values.astype(bool)
        line_is = net["line"]["in_service"].values.astype(bool)
        bus_is_ind = net["bus"][bus_is].index
        # check if in service elements are at in service buses
        is_elems = {
            "gen" : net['gen'][np.in1d(net["gen"].bus.values, bus_is_ind) \
                    & net["gen"]["in_service"].values.astype(bool)]
            , "load" : np.in1d(net["load"].bus.values, bus_is_ind) \
                    & net["load"].in_service.values.astype(bool)
            , "sgen" : np.in1d(net["sgen"].bus.values, bus_is_ind) \
                    & net["sgen"].in_service.values.astype(bool)
            , "ward" : np.in1d(net["ward"].bus.values, bus_is_ind) \
                    & net["ward"].in_service.values.astype(bool)
            , "xward" : np.in1d(net["xward"].bus.values, bus_is_ind) \
                    & net["xward"].in_service.values.astype(bool)
            , "shunt" : np.in1d(net["shunt"].bus.values, bus_is_ind) \
                    & net["shunt"].in_service.values.astype(bool)
            , "ext_grid" : net["ext_grid"][np.in1d(net["ext_grid"].bus.values, bus_is_ind) \
                    & net["ext_grid"]["in_service"].values.astype(bool)]
            , 'bus': net['bus'][bus_is]
            , 'line': net['line'][line_is]
        }

    return is_elems


def _copy_results_ppci_to_ppc(result, ppc, bus_lookup):
    '''
    result contains results for all in service elements
    ppc shall get the results for in- and out of service elements
    -> results must be copied

    ppc and ppci are structured as follows:

          [in_service elements]
    ppc = [out_of_service elements]

    result = [in_service elements]

    @author: fschaefer

    @param result:
    @param ppc:
    @return:
    '''

    # copy the results for bus, gen and branch
    # busses are sorted (REF, PV, PQ, NONE) -> results are the first 3 types
    n_cols = np.shape(ppc['bus'])[1]
    ppc['bus'][:len(result['bus']), :n_cols] = result['bus'][:len(result['bus']), :n_cols]
    # in service branches and gens are taken from 'internal'
    n_cols = np.shape(ppc['branch'])[1]
    ppc['branch'][result["internal"]['branch_is'], :n_cols] = result['branch'][:, :n_cols]
    n_cols = np.shape(ppc['gen'])[1]
    ppc['gen'][result["internal"]['gen_is'], :n_cols] = result['gen'][:, :n_cols]
    ppc['internal'] = result['internal']

    ppc['success'] = result['success']
    ppc['et'] = result['et']

    result = ppc
    return result


def _pd2ppc(net, is_elems, calculate_voltage_angles=False, enforce_q_lims=False,
            trafo_model="pi", init_results=False):
    """
    Converter Flow:
        1. Create an empty pypower datatructure
        2. Calculate loads and write the bus matrix
        3. Build the gen (Infeeder)- Matrix
        4. Calculate the line parameter and the transformer parameter,
           and fill it in the branch matrix.
           Order: 1st: Line values, 2nd: Trafo values


    INPUT:
        **net** - The Pandapower format network
        **is_elems** - In service elements from the network (see _select_is_elements())


    RETURN:
        **ppc** - The simple matpower format network. Which consists of:
                  ppc = {
                        "baseMVA": 1., *float*
                        "version": 2,  *int*
                        "bus": np.array([], dtype=float),
                        "branch": np.array([], dtype=np.complex128),
                        "gen": np.array([], dtype=float),
                        "internal": {
                              "Ybus": np.array([], dtype=np.complex128)
                              , "Yf": np.array([], dtype=np.complex128)
                              , "Yt": np.array([], dtype=np.complex128)
                              , "branch_is": np.array([], dtype=bool)
                              , "gen_is": np.array([], dtype=bool)
                              }
        **ppci** - The "internal" pypower format network for PF calculations
        **bus_lookup** - Lookup Pandapower -> ppc / ppci indices
    """

    # init empty ppc
    ppc = {"baseMVA": 1.
           , "version": 2
           , "bus": np.array([], dtype=float)
           , "branch": np.array([], dtype=np.complex128)
           , "gen": np.array([], dtype=float)
           , "internal": {
                  "Ybus": np.array([], dtype=np.complex128)
                  , "Yf": np.array([], dtype=np.complex128)
                  , "Yt": np.array([], dtype=np.complex128)
                  , "branch_is": np.array([], dtype=bool)
                  , "gen_is": np.array([], dtype=bool)
                  }
           }
    # init empty ppci
    ppci = copy.deepcopy(ppc)
    # generate ppc['bus'] and the bus lookup
    bus_lookup = _build_bus_ppc(net, ppc, is_elems, init_results)
    # generate ppc['gen'] and fills ppc['bus'] with generator values (PV, REF nodes)
    _build_gen_ppc(net, ppc, is_elems, bus_lookup, enforce_q_lims, calculate_voltage_angles)
    # generate ppc['branch'] and directly generates branch values
    _build_branch_ppc(net, ppc, is_elems, bus_lookup, calculate_voltage_angles, trafo_model)
    # adds P and Q for loads / sgens in ppc['bus'] (PQ nodes)
    _calc_loads_and_add_on_ppc(net, ppc, is_elems, bus_lookup)
    # adds P and Q for shunts, wards and xwards (to PQ nodes)
    _calc_shunts_and_add_on_ppc(net, ppc, is_elems, bus_lookup)
    # adds auxilary buses for open switches at branches
    _switch_branches(net, ppc, is_elems, bus_lookup)
    # add auxilary buses for out of service buses at in service lines.
    # Also sets lines out of service if they are connected to two out of service buses
    _branches_with_oos_buses(net, ppc, is_elems, bus_lookup)
    # sets buses out of service, which aren't connected to branches / REF buses
    _set_isolated_buses_out_of_service(net, ppc)
    # generates "internal" ppci format (for powerflow calc) from "external" ppc format and updates the bus lookup
    # Note: Also reorders buses and gens in ppc
    ppci, bus_lookup = _ppc2ppci(ppc, ppci, bus_lookup)

    return ppc, ppci, bus_lookup


def _update_ppc(net, is_elems, recycle, calculate_voltage_angles=False, enforce_q_lims=False, 
                trafo_model="pi"):
    """
    Updates P, Q values of the ppc with changed values from net

    @param is_elems:
    @return:
    """

    # get the old ppc and lookup
    ppc = net["_ppc"]
    ppci = copy.deepcopy(ppc)
    bus_lookup = net["_bus_lookup"]
    # adds P and Q for loads / sgens in ppc['bus'] (PQ nodes)
    _calc_loads_and_add_on_ppc(net, ppc, is_elems, bus_lookup)
    # adds P and Q for shunts, wards and xwards (to PQ nodes)
    _calc_shunts_and_add_on_ppc(net, ppc, is_elems, bus_lookup)
    # updates values for gen
    _update_gen_ppc(net, ppc, is_elems, bus_lookup, enforce_q_lims, calculate_voltage_angles)
    if not recycle["Ybus"]:
        # updates trafo and trafo3w values
        _update_trafo_trafo3w_ppc(net, ppc, bus_lookup, calculate_voltage_angles, trafo_model)

    # get OOS busses and place them at the end of the bus array (so that: 3
    # (REF), 2 (PV), 1 (PQ), 4 (OOS))
    oos_busses = ppc['bus'][:, BUS_TYPE] == NONE
    # there are no OOS busses in the ppci
    ppci['bus'] = ppc['bus'][~oos_busses]
    # select in service elements from ppc and put them in ppci
    brs = ppc["internal"]["branch_is"]
    gs = ppc["internal"]["gen_is"]
    ppci["branch"] = ppc["branch"][brs]
    ppci["gen"] = ppc["gen"][gs]

    return ppc, ppci, bus_lookup


def _ppc2ppci(ppc, ppci, bus_lookup):
    # BUS Sorting and lookup
    # sort busses in descending order of column 1 (namely: 4 (OOS), 3 (REF), 2 (PV), 1 (PQ))
    ppc_buses = ppc["bus"]
    ppc['bus'] = ppc_buses[ppc_buses[:, BUS_TYPE].argsort(axis=0)[::-1][:], ]
    # get OOS busses and place them at the end of the bus array (so that: 3
    # (REF), 2 (PV), 1 (PQ), 4 (OOS))
    oos_busses = ppc['bus'][:, BUS_TYPE] == NONE
    # there are no OOS busses in the ppci
    ppci['bus'] = ppc['bus'][~oos_busses]
    # in ppc the OOS busses are included and at the end of the array
    ppc['bus'] = np.r_[ppc['bus'][~oos_busses], ppc['bus'][oos_busses]]
    # generate bus_lookup_ppc_ppci (ppc -> ppci lookup)
    ppc_former_order = (ppc['bus'][:, BUS_I]).astype(int)
    aranged_buses = np.arange(len(ppc_buses))

    # lookup ppc former order -> consecutive order
    e2i = np.zeros(len(ppc_buses), dtype=int)
    e2i[ppc_former_order] = aranged_buses

    # save consecutive indices in ppc and ppci
    ppc['bus'][:, BUS_I] = aranged_buses
    ppci['bus'][:, BUS_I] = ppc['bus'][:len(ppci['bus']), BUS_I]

    # update bus_lookup (pandapower -> ppci internal)
    valid_bus_lookup_entries = bus_lookup >= 0
    bus_lookup[valid_bus_lookup_entries] = e2i[bus_lookup[valid_bus_lookup_entries]]

    if 'areas' in ppc:
        if len(ppc["areas"]) == 0:  # if areas field is empty
            del ppc['areas']  # delete it (so it's ignored)

    # bus types
    bt = ppc["bus"][:, BUS_TYPE]

    # update branch, gen and areas bus numbering
    ppc['gen'][:, GEN_BUS] = \
        e2i[np.real(ppc["gen"][:, GEN_BUS]).astype(int)].copy()
    ppc["branch"][:, F_BUS] = \
        e2i[np.real(ppc["branch"][:, F_BUS]).astype(int)].copy()
    ppc["branch"][:, T_BUS] = \
        e2i[np.real(ppc["branch"][:, T_BUS]).astype(int)].copy()

    # Note: The "update branch, gen and areas bus numbering" does the same as this:
    # ppc['gen'][:, GEN_BUS] = get_indices(ppc['gen'][:, GEN_BUS], bus_lookup_ppc_ppci)
    # ppc["branch"][:, F_BUS] = get_indices(ppc["branch"][:, F_BUS], bus_lookup_ppc_ppci)
    # ppc["branch"][:, T_BUS] = get_indices( ppc["branch"][:, T_BUS], bus_lookup_ppc_ppci)
    # but faster...

    if 'areas' in ppc:
        ppc["areas"][:, PRICE_REF_BUS] = \
            e2i[np.real(ppc["areas"][:, PRICE_REF_BUS]).astype(int)].copy()

    # reorder gens in order of increasing bus number
    ppc['gen'] = ppc['gen'][ppc['gen'][:, GEN_BUS].argsort(), ]

    # determine which buses, branches, gens are connected and
    # in-service
    n2i = ppc["bus"][:, BUS_I].astype(int)
    bs = (bt != NONE)  # bus status

    gs = ((ppc["gen"][:, GEN_STATUS] > 0) &  # gen status
          bs[n2i[np.real(ppc["gen"][:, GEN_BUS]).astype(int)]])
    ppci["internal"]["gen_is"] = gs

    brs = (np.real(ppc["branch"][:, BR_STATUS]).astype(int) &  # branch status
           bs[n2i[np.real(ppc["branch"][:, F_BUS]).astype(int)]] &
           bs[n2i[np.real(ppc["branch"][:, T_BUS]).astype(int)]]).astype(bool)
    ppci["internal"]["branch_is"] = brs

    if 'areas' in ppc:
        ar = bs[n2i[ppc["areas"][:, PRICE_REF_BUS].astype(int)]]
        # delete out of service areas
        ppci["areas"] = ppc["areas"][ar]

    # select in service elements from ppc and put them in ppci
    ppci["branch"] = ppc["branch"][brs]
    ppci["gen"] = ppc["gen"][gs]

    # execute userfcn callbacks for 'ext2int' stage
    if 'userfcn' in ppci:
        ppci = run_userfcn(ppci['userfcn'], 'ext2int', ppci)

    return ppci, bus_lookup


def _set_isolated_buses_out_of_service(net, ppc):
    # set disconnected buses out of service
    # first check if buses are connected to branches
    disco = np.setxor1d(ppc["bus"][:, 0].astype(int),
                        ppc["branch"][ppc["branch"][:, 10] == 1, :2].real.astype(int).flatten())

    # but also check if they may be the only connection to an ext_grid
    disco = np.setdiff1d(disco, ppc['bus'][ppc['bus'][:, 1] == 3, :1].real.astype(int))
    ppc["bus"][disco, 1] = 4

def _clean_up(net):
    if len(net["trafo3w"]) > 0:
        buses_3w = net.trafo3w["ad_bus"].values
        net["res_bus"].drop(buses_3w, inplace=True)
        net["bus"].drop(buses_3w, inplace=True)
        net["trafo3w"].drop(["ad_bus"], axis=1, inplace=True)

    if len(net["xward"]) > 0:
        xward_buses = net["xward"]["ad_bus"].values
        net["bus"].drop(xward_buses, inplace=True)
        net["res_bus"].drop(xward_buses, inplace=True)
        net["xward"].drop(["ad_bus"], axis=1, inplace=True)
