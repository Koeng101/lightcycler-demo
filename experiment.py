#!/usr/bin/env python3
"""LightCycler 480 II - Experiment Configuration & Command Builder

Data model:

    ExperimentConfig
    └── stages: list[Stage]         ← each stage becomes a program (0x012F)
        └── steps: list[Step]       ← each step is a protocol step (0x0132)

Each Step defines a target temperature. The machine executes steps
sequentially: ramp to temp → hold → read fluorescence → next step.

Every step ALWAYS reads fluorescence — there is no non-reading thermal hold.
For PCR, all steps (denature, anneal, extend) produce reads. Filter results
by temperature to extract just the annealing data.

Cycling is achieved by repeating stages. Each stage maps to a separate
program on the wire, so 30 PCR cycles = 30 stages with 3 steps each.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from lightcycler import (
    MSG_CREATE_EXPERIMENT, MSG_DEFINE_PROGRAM, MSG_THERMAL_PARAMS,
    MSG_PROTOCOL_STEP, MSG_FINALIZE_PROGRAM, MSG_FINALIZE_EXPERIMENT,
    MSG_START_RUN,
)


@dataclass
class Step:
    """A single protocol step: ramp to temperature, hold, optionally read.

    Maps directly to a ProtocolStep (0x0132) command on the wire.
    Set filter_set=0 for thermal-only steps (no fluorescence read).
    Set filter_set=1-4 to read with a specific filter.
    """
    temperature_c: float = 37.0
    hold_seconds: float = 1.0
    acquire: bool = True       # if False, filter_set is set to 0 (no read)
    filter_set: int = 1        # 0=no read, 1=SYBR, 2=HEX, 3=ROX, 4=Cy5
    exposure: int = 4800
    ramp_rate: float = 5.0     # C/s

    @property
    def wire_filter(self) -> int:
        """Filter value sent on the wire (0 if acquire=False)."""
        return self.filter_set if self.acquire else 0


@dataclass
class Stage:
    """A group of steps executed sequentially.

    Maps to one DefineProgram (0x012F) + ThermalParams (0x0131) + N
    ProtocolStep (0x0132) commands. For cycling (PCR), create multiple
    identical stages — each becomes a separate program on the wire.
    """
    steps: list[Step] = field(default_factory=list)


@dataclass
class ExperimentConfig:
    """Full experiment definition."""
    guid: str = ''
    well_count: int = 384
    volume: int = 20
    stages: list[Stage] = field(default_factory=list)

    def __post_init__(self):
        if not self.guid:
            self.guid = uuid.uuid4().hex.upper()

    def total_acquisitions(self) -> int:
        """Total number of fluorescence reads (only steps with acquire=True)."""
        return sum(1 for stage in self.stages
                   for step in stage.steps if step.acquire)


def build_commands(config: ExperimentConfig) -> list[tuple[int, bytes]]:
    """Generate the LC480 command sequence from a Stage/Step config.

    Wire mapping:
        Stage  → DefineProgram (0x012F)
        Step   → ProtocolStep (0x0132)
        Shared → ThermalParams (0x0131), one per stage

    Command order per stage:
        ThermalParams → ProtocolStep 1 → ProtocolStep 2 → ... → FinalizeProgram
    """
    commands = []
    seq = 1
    num_programs = len(config.stages)

    # CreateExperiment: seq, GUID, wells, volume, num_programs
    payload = f"{seq}\n{config.guid}\n{config.well_count}\n{config.volume}\n{num_programs}\n"
    commands.append((MSG_CREATE_EXPERIMENT, payload.encode('ascii')))
    seq += 1

    for prog_idx, stage in enumerate(config.stages, 1):
        num_steps = len(stage.steps)

        # DefineProgram: seq, program, 1, 0, 1, 1, num_steps
        payload = f"{seq}\n{prog_idx}\n1\n0\n1\n1\n{num_steps}\n"
        commands.append((MSG_DEFINE_PROGRAM, payload.encode('ascii')))
        seq += 1

        # ThermalParams: shared hold/ramp for all steps in this stage
        hold_cs = int(stage.steps[0].hold_seconds * 100) if stage.steps else 100
        payload = f"{seq}\n{prog_idx}\n1\n0\n3000\n1\n2\n1\n{hold_cs}\n"
        commands.append((MSG_THERMAL_PARAMS, payload.encode('ascii')))
        seq += 1

        # ProtocolStep for each step
        for step_idx, step in enumerate(stage.steps, 1):
            temp_cd = int(step.temperature_c * 100)
            ramp_cd = int(step.ramp_rate * 100)
            payload = (
                f"{seq}\n{prog_idx}\n{step_idx}\n{temp_cd}\n1\n"
                f"{step.exposure}\n{ramp_cd}\n0\n0\n0\n{step.wire_filter}\n"
            )
            commands.append((MSG_PROTOCOL_STEP, payload.encode('ascii')))
            seq += 1

        # FinalizeProgram
        commands.append((MSG_FINALIZE_PROGRAM, f"{seq}\n?".encode('ascii')))
        seq += 1

    # FinalizeExperiment
    commands.append((MSG_FINALIZE_EXPERIMENT, f"{seq}\n?".encode('ascii')))
    seq += 1

    # StartRun
    commands.append((MSG_START_RUN, f"{seq}\n?".encode('ascii')))
    seq += 1

    return commands


# --- Convenience constructors ---

def simple_read(temp_c: float = 37.0, hold_seconds: float = 1.0,
                num_reads: int = 3, filter_set: int = 1,
                well_count: int = 384, volume: int = 20) -> ExperimentConfig:
    """Create a simple isothermal fluorescence read experiment.

    N steps at the same temperature = N sequential plate reads.
    """
    return ExperimentConfig(
        well_count=well_count, volume=volume,
        stages=[Stage(
            steps=[Step(temperature_c=temp_c, hold_seconds=hold_seconds,
                        filter_set=filter_set)
                   for _ in range(num_reads)],
        )],
    )


def pcr_protocol(denature_temp: float = 95.0, denature_seconds: float = 30.0,
                 anneal_temp: float = 55.0, anneal_seconds: float = 30.0,
                 extend_temp: float = 72.0, extend_seconds: float = 60.0,
                 num_cycles: int = 30, filter_set: int = 1,
                 initial_denature_seconds: float = 120.0,
                 final_extend_seconds: float = 300.0,
                 well_count: int = 384, volume: int = 20) -> ExperimentConfig:
    """Create a standard 3-step PCR protocol.

    All steps go into a single program. Only the annealing step in each
    cycle reads fluorescence (filter > 0); denature and extend steps are
    thermal-only (filter=0).

    Total steps = 1 (initial denature) + num_cycles * 3 + 1 (final extend)
    Total reads = num_cycles (one per annealing step)
    """
    steps = []

    # Initial denature
    steps.append(Step(temperature_c=denature_temp,
                      hold_seconds=initial_denature_seconds, acquire=False))

    # Cycling: denature → anneal (read) → extend
    for _ in range(num_cycles):
        steps.append(Step(temperature_c=denature_temp,
                          hold_seconds=denature_seconds, acquire=False))
        steps.append(Step(temperature_c=anneal_temp,
                          hold_seconds=anneal_seconds,
                          acquire=True, filter_set=filter_set))
        steps.append(Step(temperature_c=extend_temp,
                          hold_seconds=extend_seconds, acquire=False))

    # Final extension
    steps.append(Step(temperature_c=extend_temp,
                      hold_seconds=final_extend_seconds, acquire=False))

    return ExperimentConfig(
        well_count=well_count, volume=volume,
        stages=[Stage(steps=steps)],
    )
