#!/usr/bin/env python3
"""LightCycler 480 II - Experiment Configuration & Command Builder

Encodes experiment parameters into the command sequence that the LC expects.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from lightcycler import (
    MSG_CREATE_EXPERIMENT, MSG_DEFINE_PROGRAM, MSG_STEP_PARAMETERS,
    MSG_ACQUISITION_POINT, MSG_FINALIZE_PROGRAM, MSG_FINALIZE_EXPERIMENT,
    MSG_START_RUN,
)


@dataclass
class Acquisition:
    """One fluorescence measurement point within a program."""
    target_temp: float = 37.0   # Celsius
    hold_time: float = 1.0      # Seconds
    ramp_rate: float = 5.0      # C/s
    filter_set: int = 1         # 1 = SYBR Green / FAM
    exposure: int = 4800        # Exposure value (firmware units)


@dataclass
class Program:
    """A thermal program containing one or more acquisition points."""
    cycles: int = 1
    acquisitions: list = field(default_factory=list)


@dataclass
class ExperimentConfig:
    """Full experiment definition."""
    guid: str = ''
    well_count: int = 384       # 96 or 384
    volume: int = 20            # Microliters
    programs: list = field(default_factory=list)

    def __post_init__(self):
        if not self.guid:
            self.guid = uuid.uuid4().hex.upper()


def build_commands(config: ExperimentConfig) -> list[tuple[int, bytes]]:
    """Generate the full command sequence for an experiment.

    Returns list of (msg_type, payload_bytes) tuples matching the exact
    wire format observed in the captured trace:

        CreateExperiment -> DefineProgram -> StepParameters ->
        AcquisitionPoint x N -> FinalizeProgram -> FinalizeExperiment ->
        StartRun

    Sequence numbers auto-increment starting from 1.
    """
    commands = []
    seq = 1

    # 0x012D CreateExperiment: seq, GUID, wells, volume, format(1)
    payload = f"{seq}\n{config.guid}\n{config.well_count}\n{config.volume}\n1\n"
    commands.append((MSG_CREATE_EXPERIMENT, payload.encode('ascii')))
    seq += 1

    for prog_idx, prog in enumerate(config.programs, 1):
        num_acq = len(prog.acquisitions)

        # 0x012F DefineProgram: seq, program, steps(1), mode(0),
        #                       cycles, unknown(1), num_acquisitions
        payload = f"{seq}\n{prog_idx}\n1\n0\n{prog.cycles}\n1\n{num_acq}\n"
        commands.append((MSG_DEFINE_PROGRAM, payload.encode('ascii')))
        seq += 1

        # 0x0131 StepParameters: seq, program, step(1), mode(0),
        #                        ramp_time(3000), unknown(1),
        #                        detection_mode(2), unknown(1),
        #                        hold_time_centisecs
        acq0 = prog.acquisitions[0] if prog.acquisitions else Acquisition()
        hold_cs = int(acq0.hold_time * 100)
        payload = f"{seq}\n{prog_idx}\n1\n0\n3000\n1\n2\n1\n{hold_cs}\n"
        commands.append((MSG_STEP_PARAMETERS, payload.encode('ascii')))
        seq += 1

        # 0x0132 AcquisitionPoint x N: seq, program, acq_num,
        #        target_temp_centideg, unknown(1), exposure,
        #        ramp_rate_centideg_per_s, 0, 0, 0, filter_set
        for acq_idx, acq in enumerate(prog.acquisitions, 1):
            temp_cd = int(acq.target_temp * 100)
            ramp_cd = int(acq.ramp_rate * 100)
            payload = (
                f"{seq}\n{prog_idx}\n{acq_idx}\n{temp_cd}\n1\n"
                f"{acq.exposure}\n{ramp_cd}\n0\n0\n0\n{acq.filter_set}\n"
            )
            commands.append((MSG_ACQUISITION_POINT, payload.encode('ascii')))
            seq += 1

        # 0x0130 FinalizeProgram: seq, "?"
        commands.append((MSG_FINALIZE_PROGRAM, f"{seq}\n?".encode('ascii')))
        seq += 1

    # 0x012E FinalizeExperiment: seq, "?"
    commands.append((MSG_FINALIZE_EXPERIMENT, f"{seq}\n?".encode('ascii')))
    seq += 1

    # 0x00C9 StartRun: seq, "?"
    commands.append((MSG_START_RUN, f"{seq}\n?".encode('ascii')))
    seq += 1

    return commands
