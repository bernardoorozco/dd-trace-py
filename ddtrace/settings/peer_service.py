import os

from ddtrace.ext import SpanKind
from ddtrace.internal.schema import SCHEMA_VERSION
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.formats import parse_tags_str
from ddtrace.settings._core import report_telemetry as _report_telemetry


class PeerServiceConfig(object):
    # TODO: Migrate PeerServiceConfig to envier
    remap_tag_name = "_dd.peer.service.remapped_from"
    source_tag_name = "_dd.peer.service.source"
    tag_name = "peer.service"
    enabled_span_kinds = {SpanKind.CLIENT, SpanKind.PRODUCER}
    prioritized_data_sources = ["messaging.kafka.bootstrap.servers", "db.name", "mongodb.db", "rpc.service", "out.host"]

    def __init__(self, set_defaults_enabled=None, peer_service_mapping=None):
        self._set_defaults_enabled = set_defaults_enabled
        self._peer_service_mapping = peer_service_mapping
        self._unparsed_peer_service_mapping = peer_service_mapping

    @property
    def set_defaults_enabled(self):
        if self._set_defaults_enabled is None:
            env_enabled = asbool(os.getenv("DD_TRACE_PEER_SERVICE_DEFAULTS_ENABLED", default=False))
            self._set_defaults_enabled = SCHEMA_VERSION == "v1" or (SCHEMA_VERSION == "v0" and env_enabled)

        return self._set_defaults_enabled

    @property
    def peer_service_mapping(self):
        if self._peer_service_mapping is None:
            self._unparsed_peer_service_mapping = os.getenv("DD_TRACE_PEER_SERVICE_MAPPING", default="")
            self._peer_service_mapping = parse_tags_str(self._unparsed_peer_service_mapping)

        return self._peer_service_mapping


_ps_config = PeerServiceConfig()
_report_telemetry(_ps_config)
