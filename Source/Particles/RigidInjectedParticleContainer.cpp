/* Copyright 2019-2020 Andrew Myers, David Grote, Glenn Richardson
 * Ligia Diana Amorim, Luca Fedeli, Maxence Thevenet
 * Remi Lehe, Revathi Jambunathan, Weiqun Zhang
 * Yinjian Zhao
 *
 * This file is part of WarpX.
 *
 * License: BSD-3-Clause-LBNL
 */

#include "Gather/FieldGather.H"
#include "Particles/Gather/GetExternalFields.H"
#include "Particles/PhysicalParticleContainer.H"
#include "Particles/WarpXParticleContainer.H"
#include "Pusher/GetAndSetPosition.H"
#include "Pusher/UpdateMomentumBoris.H"
#include "Pusher/UpdateMomentumBorisWithRadiationReaction.H"
#include "Pusher/UpdateMomentumHigueraCary.H"
#include "Pusher/UpdateMomentumVay.H"
#include "RigidInjectedParticleContainer.H"
#include "Utils/WarpXAlgorithmSelection.H"
#include "Utils/WarpXConst.H"
#include "Utils/WarpXProfilerWrapper.H"
#include "Utils/WarpXUtil.H"
#include "WarpX.H"

#include <AMReX.H>
#include <AMReX_Array.H>
#include <AMReX_Array4.H>
#include <AMReX_BLassert.H>
#include <AMReX_Box.H>
#include <AMReX_Config.H>
#include <AMReX_Dim3.H>
#include <AMReX_Extension.H>
#include <AMReX_FArrayBox.H>
#include <AMReX_FabArray.H>
#include <AMReX_Geometry.H>
#include <AMReX_GpuContainers.H>
#include <AMReX_GpuControl.H>
#include <AMReX_GpuDevice.H>
#include <AMReX_GpuLaunch.H>
#include <AMReX_GpuQualifiers.H>
#include <AMReX_IndexType.H>
#include <AMReX_IntVect.H>
#include <AMReX_MultiFab.H>
#include <AMReX_PODVector.H>
#include <AMReX_ParIter.H>
#include <AMReX_ParmParse.H>
#include <AMReX_Particles.H>
#include <AMReX_StructOfArrays.H>

#include <algorithm>
#include <array>
#include <cmath>
#include <map>

using namespace amrex;

RigidInjectedParticleContainer::RigidInjectedParticleContainer (AmrCore* amr_core, int ispecies,
                                                                const std::string& name)
    : PhysicalParticleContainer(amr_core, ispecies, name)
{

    ParmParse pp_species_name(species_name);

    getWithParser(pp_species_name, "zinject_plane", zinject_plane);
    pp_species_name.query("rigid_advance", rigid_advance);

}

void RigidInjectedParticleContainer::InitData()
{
    zinject_plane_levels.resize(finestLevel()+1, zinject_plane/WarpX::gamma_boost);

    AddParticles(0); // Note - add on level 0

    // Particles added by AddParticles should already be in the boosted frame
    RemapParticles();

    Redistribute();  // We then redistribute
}

void
RigidInjectedParticleContainer::RemapParticles()
{
    // For rigid_advance == false, nothing needs to be done

    if (rigid_advance) {

        // The particle z positions are adjusted to account for the difference between
        // advancing with vzbar and wih vz[i] before injection

        // For now, start with the assumption that this will only happen
        // at the start of the simulation.
        const Real t_lab = 0.;

        const Real uz_boost = WarpX::gamma_boost*WarpX::beta_boost*PhysConst::c;
        const Real csqi = 1./(PhysConst::c*PhysConst::c);

        vzbeam_ave_boosted = meanParticleVelocity(false)[2];

        for (int lev = 0; lev <= finestLevel(); lev++) {

#ifdef AMREX_USE_OMP
#pragma omp parallel if (Gpu::notInLaunchRegion())
#endif
            {
                // Get the average beam velocity in the boosted frame.
                // Note that the particles are already in the boosted frame.
                // This value is saved to advance the particles not injected yet

                for (WarpXParIter pti(*this, lev); pti.isValid(); ++pti)
                {
                    const auto& attribs = pti.GetAttribs();
                    const auto uxp = attribs[PIdx::ux].dataPtr();
                    const auto uyp = attribs[PIdx::uy].dataPtr();
                    const auto uzp = attribs[PIdx::uz].dataPtr();

                    const auto GetPosition = GetParticlePosition(pti);
                          auto SetPosition = SetParticlePosition(pti);

                    // Loop over particles
                    const long np = pti.numParticles();
                    const Real lvzbeam_ave_boosted = vzbeam_ave_boosted;
                    const Real gamma_boost = WarpX::gamma_boost;
                    amrex::ParallelFor( np, [=] AMREX_GPU_DEVICE (long i)
                    {
                        ParticleReal xp, yp, zp;
                        GetPosition(i, xp, yp, zp);

                        const Real gammapr = std::sqrt(1. + (uxp[i]*uxp[i] + uyp[i]*uyp[i] + uzp[i]*uzp[i])*csqi);
                        const Real vzpr = uzp[i]/gammapr;

                        // Back out the value of z_lab
                        const Real z_lab = (zp + uz_boost*t_lab + gamma_boost*t_lab*vzpr)/(gamma_boost + uz_boost*vzpr*csqi);

                        // Time of the particle in the boosted frame given its position in the lab frame at t=0.
                        const Real tpr = gamma_boost*t_lab - uz_boost*z_lab*csqi;

                        // Adjust the position, taking away its motion from its own velocity and adding
                        // the motion from the average velocity
                        zp += tpr*vzpr - tpr*lvzbeam_ave_boosted;

                        SetPosition(i, xp, yp, zp);
                    });
                }
            }
        }
    }
}

void
RigidInjectedParticleContainer::PushPX (WarpXParIter& pti,
                                        amrex::FArrayBox const * exfab,
                                        amrex::FArrayBox const * eyfab,
                                        amrex::FArrayBox const * ezfab,
                                        amrex::FArrayBox const * bxfab,
                                        amrex::FArrayBox const * byfab,
                                        amrex::FArrayBox const * bzfab,
                                        const amrex::IntVect ngE, const int e_is_nodal,
                                        const long offset,
                                        const long np_to_push,
                                        int lev, int gather_lev,
                                        amrex::Real dt, ScaleFields /*scaleFields*/,
                                        DtType a_dt_type)
{
    auto& attribs = pti.GetAttribs();
    auto& uxp = attribs[PIdx::ux];
    auto& uyp = attribs[PIdx::uy];
    auto& uzp = attribs[PIdx::uz];

    // Save the position and momenta, making copies
    Gpu::DeviceVector<ParticleReal> xp_save, yp_save, zp_save;
    RealVector uxp_save, uyp_save, uzp_save;

    const auto GetPosition = GetParticlePosition(pti);
          auto SetPosition = SetParticlePosition(pti);

    ParticleReal* const AMREX_RESTRICT ux = uxp.dataPtr() + offset;
    ParticleReal* const AMREX_RESTRICT uy = uyp.dataPtr() + offset;
    ParticleReal* const AMREX_RESTRICT uz = uzp.dataPtr() + offset;

    if (!done_injecting_lev)
    {
        // If the old values are not already saved, create copies here.
        const auto np = pti.numParticles();

        xp_save.resize(np);
        yp_save.resize(np);
        zp_save.resize(np);

        uxp_save.resize(np);
        uyp_save.resize(np);
        uzp_save.resize(np);

        amrex::Real* const AMREX_RESTRICT xp_save_ptr = xp_save.dataPtr() + offset;
        amrex::Real* const AMREX_RESTRICT yp_save_ptr = yp_save.dataPtr() + offset;
        amrex::Real* const AMREX_RESTRICT zp_save_ptr = zp_save.dataPtr() + offset;

        amrex::Real* const AMREX_RESTRICT uxp_save_ptr = uxp_save.dataPtr() + offset;
        amrex::Real* const AMREX_RESTRICT uyp_save_ptr = uyp_save.dataPtr() + offset;
        amrex::Real* const AMREX_RESTRICT uzp_save_ptr = uzp_save.dataPtr() + offset;

        amrex::ParallelFor( np,
                            [=] AMREX_GPU_DEVICE (long i) {
                                ParticleReal xp, yp, zp;
                                GetPosition(i, xp, yp, zp);
                                xp_save_ptr[i] = xp;
                                yp_save_ptr[i] = yp;
                                zp_save_ptr[i] = zp;
                                uxp_save_ptr[i] = ux[i];
                                uyp_save_ptr[i] = uy[i];
                                uzp_save_ptr[i] = uz[i];
                            });
    }

    const bool do_scale = not done_injecting_lev;
    const Real v_boost = WarpX::beta_boost*PhysConst::c;
    PhysicalParticleContainer::PushPX(pti, exfab, eyfab, ezfab, bxfab, byfab, bzfab,
                                      ngE, e_is_nodal, offset, np_to_push, lev, gather_lev, dt,
                                      ScaleFields(do_scale, dt, zinject_plane_lev_previous,
                                                  vzbeam_ave_boosted, v_boost),
                                      a_dt_type);

    if (!done_injecting_lev) {

        ParticleReal* AMREX_RESTRICT x_save = xp_save.dataPtr() + offset;
        ParticleReal* AMREX_RESTRICT y_save = yp_save.dataPtr() + offset;
        ParticleReal* AMREX_RESTRICT z_save = zp_save.dataPtr() + offset;
        ParticleReal* AMREX_RESTRICT ux_save = uxp_save.dataPtr() + offset;
        ParticleReal* AMREX_RESTRICT uy_save = uyp_save.dataPtr() + offset;
        ParticleReal* AMREX_RESTRICT uz_save = uzp_save.dataPtr() + offset;

        // Undo the push for particles not injected yet.
        // The zp are advanced a fixed amount.
        const Real z_plane_lev = zinject_plane_lev;
        const Real vz_ave_boosted = vzbeam_ave_boosted;
        const bool rigid = rigid_advance;
        const Real inv_csq = 1./(PhysConst::c*PhysConst::c);
        amrex::ParallelFor( pti.numParticles(),
                            [=] AMREX_GPU_DEVICE (long i) {
                                ParticleReal xp, yp, zp;
                                GetPosition(i, xp, yp, zp);
                                if (zp <= z_plane_lev) {
                                    ux[i] = ux_save[i];
                                    uy[i] = uy_save[i];
                                    uz[i] = uz_save[i];
                                    xp = x_save[i];
                                    yp = y_save[i];
                                    if (rigid) {
                                        zp = z_save[i] + dt*vz_ave_boosted;
                                    }
                                    else {
                                        const Real gi = 1./std::sqrt(1. + (ux[i]*ux[i] + uy[i]*uy[i] + uz[i]*uz[i])*inv_csq);
                                        zp = z_save[i] + dt*uz[i]*gi;
                                    }
                                    SetPosition(i, xp, yp, zp);
                                }
                            });
    }
}

void
RigidInjectedParticleContainer::Evolve (int lev,
                                        const MultiFab& Ex, const MultiFab& Ey, const MultiFab& Ez,
                                        const MultiFab& Bx, const MultiFab& By, const MultiFab& Bz,
                                        MultiFab& jx, MultiFab& jy, MultiFab& jz,
                                        MultiFab* cjx, MultiFab* cjy, MultiFab* cjz,
                                        MultiFab* rho, MultiFab* crho,
                                        const MultiFab* cEx, const MultiFab* cEy, const MultiFab* cEz,
                                        const MultiFab* cBx, const MultiFab* cBy, const MultiFab* cBz,
                                        Real t, Real dt, DtType a_dt_type, bool skip_deposition)
{

    // Update location of injection plane in the boosted frame
    zinject_plane_lev_previous = zinject_plane_levels[lev];
    zinject_plane_levels[lev] -= dt*WarpX::beta_boost*PhysConst::c;
    zinject_plane_lev = zinject_plane_levels[lev];

    // Set the done injecting flag whan the inject plane moves out of the
    // simulation domain.
    // It is much easier to do this check, rather than checking if all of the
    // particles have crossed the inject plane.
    const Real* plo = Geom(lev).ProbLo();
    const Real* phi = Geom(lev).ProbHi();
    const int zdir = AMREX_SPACEDIM-1;
    done_injecting_lev = ((zinject_plane_levels[lev] < plo[zdir] && WarpX::moving_window_v + WarpX::beta_boost*PhysConst::c >= 0.) ||
                           (zinject_plane_levels[lev] > phi[zdir] && WarpX::moving_window_v + WarpX::beta_boost*PhysConst::c <= 0.));

    PhysicalParticleContainer::Evolve (lev,
                                       Ex, Ey, Ez,
                                       Bx, By, Bz,
                                       jx, jy, jz,
                                       cjx, cjy, cjz,
                                       rho, crho,
                                       cEx, cEy, cEz,
                                       cBx, cBy, cBz,
                                       t, dt, a_dt_type, skip_deposition);
}

void
RigidInjectedParticleContainer::PushP (int lev, Real dt,
                                       const MultiFab& Ex, const MultiFab& Ey, const MultiFab& Ez,
                                       const MultiFab& Bx, const MultiFab& By, const MultiFab& Bz)
{
    WARPX_PROFILE("RigidInjectedParticleContainer::PushP");

    if (do_not_push) return;

    const std::array<Real,3>& dx = WarpX::CellSize(std::max(lev,0));

#ifdef AMREX_USE_OMP
#pragma omp parallel
#endif
    {
        for (WarpXParIter pti(*this, lev); pti.isValid(); ++pti)
        {
            amrex::Box box = pti.tilebox();
            box.grow(Ex.nGrowVect());

            const long np = pti.numParticles();

            // Data on the grid
            const FArrayBox& exfab = Ex[pti];
            const FArrayBox& eyfab = Ey[pti];
            const FArrayBox& ezfab = Ez[pti];
            const FArrayBox& bxfab = Bx[pti];
            const FArrayBox& byfab = By[pti];
            const FArrayBox& bzfab = Bz[pti];

            const auto getPosition = GetParticlePosition(pti);

            const auto getExternalE = GetExternalEField(pti);
            const auto getExternalB = GetExternalBField(pti);

            const auto& xyzmin = WarpX::GetInstance().LowerCornerWithGalilean(box,m_v_galilean,lev);

            const Dim3 lo = lbound(box);

            bool galerkin_interpolation = WarpX::galerkin_interpolation;
            int nox = WarpX::nox;
            int n_rz_azimuthal_modes = WarpX::n_rz_azimuthal_modes;

            amrex::GpuArray<amrex::Real, 3> dx_arr = {dx[0], dx[1], dx[2]};
            amrex::GpuArray<amrex::Real, 3> xyzmin_arr = {xyzmin[0], xyzmin[1], xyzmin[2]};

            amrex::Array4<const amrex::Real> const& ex_arr = exfab.array();
            amrex::Array4<const amrex::Real> const& ey_arr = eyfab.array();
            amrex::Array4<const amrex::Real> const& ez_arr = ezfab.array();
            amrex::Array4<const amrex::Real> const& bx_arr = bxfab.array();
            amrex::Array4<const amrex::Real> const& by_arr = byfab.array();
            amrex::Array4<const amrex::Real> const& bz_arr = bzfab.array();

            amrex::IndexType const ex_type = exfab.box().ixType();
            amrex::IndexType const ey_type = eyfab.box().ixType();
            amrex::IndexType const ez_type = ezfab.box().ixType();
            amrex::IndexType const bx_type = bxfab.box().ixType();
            amrex::IndexType const by_type = byfab.box().ixType();
            amrex::IndexType const bz_type = bzfab.box().ixType();

            auto& attribs = pti.GetAttribs();
            amrex::ParticleReal* const AMREX_RESTRICT uxpp = attribs[PIdx::ux].dataPtr();
            amrex::ParticleReal* const AMREX_RESTRICT uypp = attribs[PIdx::uy].dataPtr();
            amrex::ParticleReal* const AMREX_RESTRICT uzpp = attribs[PIdx::uz].dataPtr();

            int* AMREX_RESTRICT ion_lev = nullptr;
            if (do_field_ionization) {
                ion_lev = pti.GetiAttribs(particle_icomps["ionization_level"]).dataPtr();
            }

            // Save the position and momenta, making copies
            amrex::Gpu::DeviceVector<ParticleReal> uxp_save(np);
            amrex::Gpu::DeviceVector<ParticleReal> uyp_save(np);
            amrex::Gpu::DeviceVector<ParticleReal> uzp_save(np);
            ParticleReal* const AMREX_RESTRICT ux_save = uxp_save.dataPtr();
            ParticleReal* const AMREX_RESTRICT uy_save = uyp_save.dataPtr();
            ParticleReal* const AMREX_RESTRICT uz_save = uzp_save.dataPtr();

            // Loop over the particles and update their momentum
            const amrex::Real q = this->charge;
            const amrex::Real m = this-> mass;

            const auto pusher_algo = WarpX::particle_pusher_algo;
            const auto do_crr = do_classical_radiation_reaction;

            amrex::ParallelFor( np, [=] AMREX_GPU_DEVICE (long ip)
            {
                ux_save[ip] = uxpp[ip];
                uy_save[ip] = uypp[ip];
                uz_save[ip] = uzpp[ip];

                amrex::ParticleReal xp, yp, zp;
                getPosition(ip, xp, yp, zp);

                amrex::ParticleReal Exp = 0._rt, Eyp = 0._rt, Ezp = 0._rt;
                amrex::ParticleReal Bxp = 0._rt, Byp = 0._rt, Bzp = 0._rt;

                // first gather E and B to the particle positions
                doGatherShapeN(xp, yp, zp, Exp, Eyp, Ezp, Bxp, Byp, Bzp,
                               ex_arr, ey_arr, ez_arr, bx_arr, by_arr, bz_arr,
                               ex_type, ey_type, ez_type, bx_type, by_type, bz_type,
                               dx_arr, xyzmin_arr, lo, n_rz_azimuthal_modes,
                               nox, galerkin_interpolation);
                getExternalE(ip, Exp, Eyp, Ezp);
                getExternalB(ip, Bxp, Byp, Bzp);

                amrex::Real qp = q;
                if (ion_lev) { qp *= ion_lev[ip]; }

                if (do_crr) {
                    UpdateMomentumBorisWithRadiationReaction(uxpp[ip], uypp[ip], uzpp[ip],
                                                             Exp, Eyp, Ezp, Bxp,
                                                             Byp, Bzp, qp, m, dt);
                } else if (pusher_algo == ParticlePusherAlgo::Boris) {
                    UpdateMomentumBoris( uxpp[ip], uypp[ip], uzpp[ip],
                                         Exp, Eyp, Ezp, Bxp,
                                         Byp, Bzp, qp, m, dt);
                } else if (pusher_algo == ParticlePusherAlgo::Vay) {
                    UpdateMomentumVay( uxpp[ip], uypp[ip], uzpp[ip],
                                       Exp, Eyp, Ezp, Bxp,
                                       Byp, Bzp, qp, m, dt);
                } else if (pusher_algo == ParticlePusherAlgo::HigueraCary) {
                    UpdateMomentumHigueraCary( uxpp[ip], uypp[ip], uzpp[ip],
                                               Exp, Eyp, Ezp, Bxp,
                                               Byp, Bzp, qp, m, dt);
                } else {
                    amrex::Abort("Unknown particle pusher");
                }
            });

            // Undo the push for particles not injected yet.
            // It is assumed that PushP will only be called on the first and last steps
            // and that no particles will cross zinject_plane.
            const ParticleReal zz = zinject_plane_levels[lev];
            amrex::ParallelFor( pti.numParticles(), [=] AMREX_GPU_DEVICE (long i)
            {
                ParticleReal xp, yp, zp;
                getPosition(i, xp, yp, zp);
                if (zp <= zz) {
                    uxpp[i] = ux_save[i];
                    uypp[i] = uy_save[i];
                    uzpp[i] = uz_save[i];
                }
            });

            amrex::Gpu::synchronize();
        }
    }
}
