from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FilterSet(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILTER_UNSPECIFIED: _ClassVar[FilterSet]
    SYBR_GREEN: _ClassVar[FilterSet]
    HEX: _ClassVar[FilterSet]
    ROX: _ClassVar[FilterSet]
    CY5: _ClassVar[FilterSet]

class ExperimentStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STATUS_UNSPECIFIED: _ClassVar[ExperimentStatus]
    PENDING: _ClassVar[ExperimentStatus]
    RUNNING: _ClassVar[ExperimentStatus]
    COMPLETE: _ClassVar[ExperimentStatus]
    ERROR: _ClassVar[ExperimentStatus]
FILTER_UNSPECIFIED: FilterSet
SYBR_GREEN: FilterSet
HEX: FilterSet
ROX: FilterSet
CY5: FilterSet
STATUS_UNSPECIFIED: ExperimentStatus
PENDING: ExperimentStatus
RUNNING: ExperimentStatus
COMPLETE: ExperimentStatus
ERROR: ExperimentStatus

class GetStateRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetStateResponse(_message.Message):
    __slots__ = ("instrument_state", "firmware_version", "controller_count", "connected", "current_experiment_guid")
    INSTRUMENT_STATE_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONTROLLER_COUNT_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    CURRENT_EXPERIMENT_GUID_FIELD_NUMBER: _ClassVar[int]
    instrument_state: int
    firmware_version: str
    controller_count: str
    connected: bool
    current_experiment_guid: str
    def __init__(self, instrument_state: _Optional[int] = ..., firmware_version: _Optional[str] = ..., controller_count: _Optional[str] = ..., connected: _Optional[bool] = ..., current_experiment_guid: _Optional[str] = ...) -> None: ...

class GetLogsRequest(_message.Message):
    __slots__ = ("limit", "since_id")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    SINCE_ID_FIELD_NUMBER: _ClassVar[int]
    limit: int
    since_id: int
    def __init__(self, limit: _Optional[int] = ..., since_id: _Optional[int] = ...) -> None: ...

class LogEntry(_message.Message):
    __slots__ = ("id", "timestamp", "event_code", "sub_code", "message")
    ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    EVENT_CODE_FIELD_NUMBER: _ClassVar[int]
    SUB_CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    id: int
    timestamp: str
    event_code: int
    sub_code: str
    message: str
    def __init__(self, id: _Optional[int] = ..., timestamp: _Optional[str] = ..., event_code: _Optional[int] = ..., sub_code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class GetLogsResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[LogEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[LogEntry, _Mapping]]] = ...) -> None: ...

class RunExperimentRequest(_message.Message):
    __slots__ = ("temp_c", "hold_time_s", "num_acquisitions", "filter", "well_count", "volume_ul")
    TEMP_C_FIELD_NUMBER: _ClassVar[int]
    HOLD_TIME_S_FIELD_NUMBER: _ClassVar[int]
    NUM_ACQUISITIONS_FIELD_NUMBER: _ClassVar[int]
    FILTER_FIELD_NUMBER: _ClassVar[int]
    WELL_COUNT_FIELD_NUMBER: _ClassVar[int]
    VOLUME_UL_FIELD_NUMBER: _ClassVar[int]
    temp_c: float
    hold_time_s: float
    num_acquisitions: int
    filter: FilterSet
    well_count: int
    volume_ul: int
    def __init__(self, temp_c: _Optional[float] = ..., hold_time_s: _Optional[float] = ..., num_acquisitions: _Optional[int] = ..., filter: _Optional[_Union[FilterSet, str]] = ..., well_count: _Optional[int] = ..., volume_ul: _Optional[int] = ...) -> None: ...

class RunExperimentResponse(_message.Message):
    __slots__ = ("guid",)
    GUID_FIELD_NUMBER: _ClassVar[int]
    guid: str
    def __init__(self, guid: _Optional[str] = ...) -> None: ...

class GetExperimentRequest(_message.Message):
    __slots__ = ("guid",)
    GUID_FIELD_NUMBER: _ClassVar[int]
    guid: str
    def __init__(self, guid: _Optional[str] = ...) -> None: ...

class Acquisition(_message.Message):
    __slots__ = ("acquisition_num", "temperature_c", "time_s", "ref_channel", "well_values")
    ACQUISITION_NUM_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_C_FIELD_NUMBER: _ClassVar[int]
    TIME_S_FIELD_NUMBER: _ClassVar[int]
    REF_CHANNEL_FIELD_NUMBER: _ClassVar[int]
    WELL_VALUES_FIELD_NUMBER: _ClassVar[int]
    acquisition_num: int
    temperature_c: float
    time_s: float
    ref_channel: int
    well_values: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, acquisition_num: _Optional[int] = ..., temperature_c: _Optional[float] = ..., time_s: _Optional[float] = ..., ref_channel: _Optional[int] = ..., well_values: _Optional[_Iterable[int]] = ...) -> None: ...

class GetExperimentResponse(_message.Message):
    __slots__ = ("guid", "status", "config", "created_at", "completed_at", "error_message", "acquisitions")
    GUID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ACQUISITIONS_FIELD_NUMBER: _ClassVar[int]
    guid: str
    status: ExperimentStatus
    config: RunExperimentRequest
    created_at: str
    completed_at: str
    error_message: str
    acquisitions: _containers.RepeatedCompositeFieldContainer[Acquisition]
    def __init__(self, guid: _Optional[str] = ..., status: _Optional[_Union[ExperimentStatus, str]] = ..., config: _Optional[_Union[RunExperimentRequest, _Mapping]] = ..., created_at: _Optional[str] = ..., completed_at: _Optional[str] = ..., error_message: _Optional[str] = ..., acquisitions: _Optional[_Iterable[_Union[Acquisition, _Mapping]]] = ...) -> None: ...

class ListExperimentsRequest(_message.Message):
    __slots__ = ("limit",)
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    limit: int
    def __init__(self, limit: _Optional[int] = ...) -> None: ...

class ListExperimentsResponse(_message.Message):
    __slots__ = ("experiments",)
    class ExperimentSummary(_message.Message):
        __slots__ = ("guid", "status", "created_at", "num_acquisitions")
        GUID_FIELD_NUMBER: _ClassVar[int]
        STATUS_FIELD_NUMBER: _ClassVar[int]
        CREATED_AT_FIELD_NUMBER: _ClassVar[int]
        NUM_ACQUISITIONS_FIELD_NUMBER: _ClassVar[int]
        guid: str
        status: ExperimentStatus
        created_at: str
        num_acquisitions: int
        def __init__(self, guid: _Optional[str] = ..., status: _Optional[_Union[ExperimentStatus, str]] = ..., created_at: _Optional[str] = ..., num_acquisitions: _Optional[int] = ...) -> None: ...
    EXPERIMENTS_FIELD_NUMBER: _ClassVar[int]
    experiments: _containers.RepeatedCompositeFieldContainer[ListExperimentsResponse.ExperimentSummary]
    def __init__(self, experiments: _Optional[_Iterable[_Union[ListExperimentsResponse.ExperimentSummary, _Mapping]]] = ...) -> None: ...

class OpenDoorRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class OpenDoorResponse(_message.Message):
    __slots__ = ("success", "error")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    success: bool
    error: str
    def __init__(self, success: _Optional[bool] = ..., error: _Optional[str] = ...) -> None: ...

class CloseDoorRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CloseDoorResponse(_message.Message):
    __slots__ = ("success", "error")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    success: bool
    error: str
    def __init__(self, success: _Optional[bool] = ..., error: _Optional[str] = ...) -> None: ...

class GetDoorStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetDoorStatusResponse(_message.Message):
    __slots__ = ("battery_percent", "available", "error")
    BATTERY_PERCENT_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    battery_percent: int
    available: bool
    error: str
    def __init__(self, battery_percent: _Optional[int] = ..., available: _Optional[bool] = ..., error: _Optional[str] = ...) -> None: ...
